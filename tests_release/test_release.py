import sys,json,tempfile,unittest,os
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integral_filter import IntegralStore,atomic_npz
from retention import cleanup,MIB,StorageLimitError,storage_lock
from watches import WatchStore

class GateTests(unittest.TestCase):
    def test_nine_cases_and_restart_every_sample(self):
        with np.load(Path(__file__).with_name('nine_cases.npz')) as z:
            for meta in json.loads(z['metadata'].item()):
                name=meta['name'];v=z[name];times=z[name+'_times'];expected=z[name+'_candidate']
                with tempfile.TemporaryDirectory() as tmp:
                    for k,(values,t) in enumerate(zip(v,times)):
                        T,rise,N,S,eu,ed,g,area=values
                        cv=lambda x:float(x) if np.isfinite(x) else None
                        p=dict(id='P0001C0001',native_row_1based=1,native_column_1based=1,latitude=35.,longitude=128.,current_K=cv(T),rise_K=cv(rise),normal_skin_K=cv(N),joint_score=cv(S),condition_rise=bool(np.isfinite(S) and S>=5 and rise>=.6),condition_high_score=bool(np.isfinite(S) and S>=40))
                        result=dict(all_pixels=[p],grid_signature='test',scoring={},filters={});stamp=datetime.fromtimestamp(float(t),timezone.utc);cur=SimpleNamespace(nominal=stamp,line_time=lambda r:'test');prev=SimpleNamespace(nominal=stamp-timedelta(seconds=150),line_time=lambda r:'test');geo=SimpleNamespace(region=lambda a,b:'test')
                        out=IntegralStore(tmp).apply(result,prev,cur,geo,10)
                        self.assertEqual(p['eligible'],bool(expected[k]),(name,k))
                        for field,want in [('ema_rise_sum',eu),('ema_fall_sum',ed),('ema_gap',g),('integral_area',area)]:
                            if np.isfinite(want):self.assertAlmostEqual(p[field],want,places=6,msg=(name,k,field))
                            else:self.assertIsNone(p[field],(name,k,field))
                        if expected[k]:
                            p['current_K']=p['normal_skin_K']-1e-8
                            IntegralStore(tmp).apply(out,prev,cur,geo,10)
                            self.assertFalse(p['eligible'])
                            p['current_K']=p['normal_skin_K']
                            IntegralStore(tmp).apply(out,prev,cur,geo,10)
                            self.assertTrue(p['eligible'])
                        before=IntegralStore(tmp).load()['area'].copy()
                        IntegralStore(tmp).apply(out,prev,cur,geo,10)
                        np.testing.assert_equal(before,IntegralStore(tmp).load()['area'])
    def test_missing_pixels_keep_fixed_grid_and_cadence_resets(self):
        def pixel(col,score):return dict(id=f'P0001C{col:04d}',native_row_1based=1,native_column_1based=col,latitude=35.,longitude=128.,current_K=290.,normal_skin_K=280.,joint_score=score,rise_K=1.,condition_rise=True,condition_high_score=False)
        def frame(t):return SimpleNamespace(nominal=t,line_time=lambda r:'test',first_line=1)
        geo=SimpleNamespace(contrast_rows=(np.array([0,0]),np.array([0,1])),region=lambda a,b:'test')
        t=datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            for k in range(12):
                pixels=[pixel(1,float(k)),pixel(2,float(k))] if k!=10 else [pixel(1,float(k))]
                r=dict(all_pixels=pixels,grid_signature='test',scoring={},filters={})
                IntegralStore(d).apply(r,frame(t+timedelta(seconds=150*(k-1))),frame(t+timedelta(seconds=150*k)),geo,10)
            state=IntegralStore(d).load();self.assertEqual(state['ring'].shape,(10,2));self.assertTrue(np.isfinite(state['gap'][0]));self.assertTrue(np.isnan(state['gap'][1]))
            r=dict(all_pixels=[pixel(1,15.),pixel(2,15.)],grid_signature='test',scoring={},filters={})
            IntegralStore(d).apply(r,frame(t+timedelta(seconds=150*12)),frame(t+timedelta(seconds=150*13)),geo,10)
            state=IntegralStore(d).load();self.assertEqual(len(state['ring']),1);self.assertTrue(np.isnan(state['gap']).all())

class RetentionTests(unittest.TestCase):
    def test_expiry_count_budget_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as d,tempfile.TemporaryDirectory() as outside:
            root=Path(d);(root/'series').mkdir();(root/'cache').mkdir();(root/'history/old').mkdir(parents=True);(root/'noise_cache').mkdir()
            sentinel=Path(outside)/'keep';sentinel.write_text('keep');(root/'linked').symlink_to(outside,target_is_directory=True)
            for n in range(600):(root/'series'/f'{n:04}.npz').write_bytes(b'x')
            expired=root/'cache'/'old.DAT.bz2';expired.write_bytes(b'x');os.utime(expired,(0,0))
            for n in range(40):(root/'history/old'/f'{n:08}.json').write_text('{}')
            r=cleanup(root);self.assertTrue(r['healthy']);self.assertEqual(len(list((root/'series').glob('*.npz'))),49);self.assertFalse(expired.exists());self.assertEqual(len(list((root/'history').rglob('*.json'))),32);self.assertTrue(sentinel.exists());self.assertFalse((root/'noise_cache').exists())
            cleanup(root,datetime.now(timezone.utc)+timedelta(days=2));self.assertEqual(list((root/'series').glob('*')),[])
    def test_active_archive_protected_completed_expired(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for name,phase in [('a'*32,'analyzing'),('b'*32,'ready')]:
                f=root/'archive_queries'/name;f.mkdir(parents=True);(f/'query.json').write_text(json.dumps(dict(phase=phase)))
                if name=='b'*32:os.utime(f/'query.json',(0,0))
            cleanup(root);self.assertTrue((root/'archive_queries'/('a'*32)).exists());self.assertFalse((root/'archive_queries'/('b'*32)).exists())
    def test_cleanup_lock_prevents_simultaneous_writers(self):
        with tempfile.TemporaryDirectory() as d:
            with storage_lock(d):
                with self.assertRaises((BlockingIOError,OSError)):
                    with storage_lock(d,blocking=False):pass
    def test_byte_cap_removes_oldest_without_waiting_for_age(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'series';p.mkdir()
            for n in range(4):(p/f'{n}.npz').write_bytes(b'x'*200)
            with patch('retention.MIB',1):cleanup(d)
            self.assertLessEqual(sum(f.stat().st_size for f in p.iterdir()),96)
    def test_over_budget_blocks_new_acquisition(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'protected.bin';p.write_bytes(b'x'*256)
            with patch('retention.LIMIT',128):
                with self.assertRaises(StorageLimitError):cleanup(d)
            self.assertEqual(p.stat().st_size,256)
    def test_sqlite_rows_expire_and_watch_migration(self):
        with tempfile.TemporaryDirectory() as d:
            import sqlite3
            p=Path(d)/'state.sqlite3'
            with sqlite3.connect(p) as db:
                db.executescript('CREATE TABLE processed(nominal TEXT);CREATE TABLE gaps(nominal TEXT);');db.execute('INSERT INTO processed VALUES (?)',('2000-01-01T00:00:00+00:00',))
            WatchStore(Path(d)/'watchpoints.sqlite3');cleanup(d)
            with sqlite3.connect(p) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM processed').fetchone()[0],0)
if __name__=='__main__':unittest.main()
