import sys,tempfile,unittest,json
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import bootstrap as batch
from integral_filter import IntegralStore
from std_trigger import StdStore
from hsd import STEP

class BootstrapTests(unittest.TestCase):
    def exercise(self,fail=None,interrupt=False):
        z=np.load(Path(__file__).with_name('nine_cases.npz'));values=z['uiseong2025'];times=z['uiseong2025_times'];anchor=datetime.fromtimestamp(float(times[48]),timezone.utc);events=[];row_by_t={float(t):r for t,r in zip(times,values)}
        class FakeSource:
            def __init__(self,*a,**kw):pass
            def frame(self,t):
                events.append(('fetch',t))
                if fail is not None and t==fail:raise OSError('missing source')
                return SimpleNamespace(nominal=t,first_line=1,bt=np.array([[300.]]),satellite='Himawari-9',sha256='test',line_time=lambda r:'test')
        def compare(previous,current,*a,**kw):
            if interrupt and current.nominal==anchor-40*STEP:raise KeyboardInterrupt('simulated stop')
            events.append(('calculate',current.nominal));v=row_by_t.get(current.nominal.timestamp(),values[0]);T,rise,N,S,*_=v;num=lambda x:float(x) if np.isfinite(x) else None
            p=dict(id='P0001C0001',native_row_1based=1,native_column_1based=1,latitude=35.,longitude=128.,current_K=num(T),normal_skin_K=num(N),rise_K=num(rise),joint_score=num(S),score_rank=1,condition_rise=bool(S>=5 and rise>=.6),condition_high_score=bool(S>=40))
            return dict(all_pixels=[p],grid_signature='test',scoring={},filters={})
        with tempfile.TemporaryDirectory() as tmp:
            e=SimpleNamespace(output=Path(tmp),integral=IntegralStore(tmp),std=StdStore(tmp),geography=SimpleNamespace(contrast_rows=(np.array([0]),np.array([0])),region=lambda a,b:'test'),top=10,latest_only=True)
            with patch.object(batch,'Source',FakeSource),patch.object(batch,'compare_frames',compare),patch.object(batch,'reference_arrays',lambda *a,**kw:[]),patch.object(batch,'apply_contrast',lambda r,*a:r),patch.object(batch,'finalize',lambda r,*a:r):
                if interrupt:
                    with self.assertRaises(KeyboardInterrupt):batch.bootstrap(e,FakeSource().frame(anchor))
                    self.assertFalse(e.integral.path.exists());self.assertEqual(list(Path(tmp).glob('.bootstrap_*')),[]);return
                report=batch.bootstrap(e,FakeSource().frame(anchor));state=e.integral.load()
            self.assertEqual(report['total'],48);self.assertEqual(report['raw_total'],51);self.assertEqual(report['raw_completed'],51);self.assertEqual(report['anchor_utc'],anchor.isoformat());self.assertEqual(float(state['minute']),(anchor-STEP).timestamp()/60)
            calculation=[t for kind,t in events if kind=='calculate'];self.assertEqual(calculation,sorted(calculation));self.assertEqual(len(set(calculation)),49 if fail is None else 47)
            first_calc=next(i for i,v in enumerate(events) if v[0]=='calculate');prefetched={t for kind,t in events[:first_calc] if kind=='fetch'}
            self.assertEqual(len(prefetched),51)
            if fail is None:
                self.assertEqual(report['completed'],48);self.assertEqual(report['failed_samples'],0)
                for key,col in [('emaU',4),('emaD',5),('gap',6),('area',7)]:np.testing.assert_allclose(state[key][0],values[47,col],rtol=0,atol=1e-7,equal_nan=True)
            else:
                self.assertEqual(report['failed_samples'],2);self.assertEqual(report['phase'],'partial')
    def test_bulk_prefetch_and_chronological_results_match(self):self.exercise()
    def test_missing_observation_does_not_invent_history(self):
        with np.load(Path(__file__).with_name('nine_cases.npz')) as z:fail=datetime.fromtimestamp(float(z['uiseong2025_times'][20]),timezone.utc)
        self.exercise(fail)
    def test_interrupted_batch_does_not_publish_partial_state(self):self.exercise(interrupt=True)
if __name__=='__main__':unittest.main()
