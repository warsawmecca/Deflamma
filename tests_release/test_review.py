import sys,unittest,tempfile,json,os
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from reviews import ReviewStore,authorize
from std_trigger import StdStore
from watches import WatchError

class ReviewTests(unittest.TestCase):
 def result(self,k,pixel='P0001C0001'):
  t=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=150*k)
  return dict(current_nominal_utc=t.isoformat(),all_pixels=[dict(id=pixel,eligible=True,joint_score=30,latitude=35,longitude=128)])
 def test_reopen_only_new_observation_stale_decision_and_later_episode(self):
  with tempfile.TemporaryDirectory() as d:
   store=ReviewStore(d);store.record(self.result(0),{'P0001C0001':{'samples':['UNIQUE_PRIVATE_EVIDENCE_TO_DELETE']}},now=10000);c=store.list(now=10000)['items'][0]
   store.decide(c['id'],c['revision'],'not_fire','Alice',now=10001);self.assertIsNone(store.get(c['id'],10001)['evidence'])
   self.assertNotIn(b'UNIQUE_PRIVATE_EVIDENCE_TO_DELETE',store.path.read_bytes())
   store.record(self.result(0),{},now=10002);self.assertEqual(store.get(c['id'],10002)['status'],'not_fire')
   store.record(self.result(1),{'P0001C0001':{'samples':[2]}},now=10150);new=store.get(c['id'],10150);self.assertEqual(new['status'],'pending');self.assertEqual(len(new['audit']),1)
   with self.assertRaises(WatchError) as e:store.decide(c['id'],c['revision'],'possible','Bob',now=10151)
   self.assertEqual(e.exception.status,409)
   store.decide(c['id'],new['revision'],'possible','Bob',now=10152)
   store.record(self.result(4),{},now=10600);self.assertEqual(store.list(now=10600)['counts']['pending'],1);self.assertEqual(store.list('possible',now=10600)['counts']['possible'],1)
 def test_expiry_not_extended_by_continuation_deletion_and_bounds(self):
  with tempfile.TemporaryDirectory() as d:
   store=ReviewStore(d);store.record(self.result(0),{'P0001C0001':{'samples':[1]}},now=10000);c=store.list(now=10000)['items'][0]
   store.record(self.result(1),{},now=10150);self.assertEqual(store.get(c['id'],10150)['deadline'],11800)
   store.cleanup(11801);c=store.get(c['id'],11801);self.assertEqual(c['status'],'expired');self.assertIsNone(c['evidence'])
   store.cleanup(11801+86401)
   with self.assertRaises(WatchError):store.get(c['id'],11801+86401)
   r=self.result(100);r['all_pixels']=[dict(id=f'P0001C{i:04}',eligible=True) for i in range(510)];store.record(r,{},now=200000)
   counts=store.list(now=200000)['counts'];self.assertEqual(counts['pending'],500);self.assertEqual(counts['expired'],10)
 def test_case_identity_is_never_reused_after_pruning(self):
  with tempfile.TemporaryDirectory() as d:
   store=ReviewStore(d);store.record(self.result(0),{},now=10000);old=store.list(now=10000)['items'][0]
   store.cleanup(12000);store.cleanup(12000+86401);store.record(self.result(100),{},now=110000);new=store.list(now=110000)['items'][0]
   self.assertGreater(new['id'],old['id'])
   with self.assertRaises(WatchError):store.decide(old['id'],old['revision'],'not_fire','A',now=110000)
 def test_auth_revocation_and_no_public_config(self):
  import hashlib
  with tempfile.TemporaryDirectory() as d:
   cfg=Path(d)/'users.json';out=Path(d)/'output';out.mkdir();cfg.write_text(json.dumps([dict(name='A',active=True,token_sha256=hashlib.sha256(b'secret').hexdigest())]))
   with patch.dict(os.environ,DEFLAMMA_REVIEWERS_FILE=str(cfg)):
    self.assertEqual(authorize('Bearer secret',out),'A')
    for h in [None,'Bearer wrong','secret']:
     with self.assertRaises(WatchError):authorize(h,out)
    cfg.write_text('[]')
    with self.assertRaises(WatchError):authorize('Bearer secret',out)
   with patch.dict(os.environ,DEFLAMMA_REVIEWERS_FILE=str(out/'users.json')):
    with self.assertRaises(WatchError):authorize('Bearer secret',out)

class SigmaTests(unittest.TestCase):
 def test_actual_cases_restart_retry_and_gap(self):
  # Packaged 9-case score series are historical, not fitted synthetic outputs.
  with np.load(Path(__file__).with_name('nine_cases.npz')) as z:
   for meta in json.loads(z['metadata'].item()):
    name=meta['name'];scores=z[name][:,3];times=z[name+'_times'];geo=SimpleNamespace(contrast_rows=(np.array([0]),np.array([0])),region=lambda a,b:'test');expected_base=None;sigmas=[];streak=0
    with tempfile.TemporaryDirectory() as d:
     for k,(score,t) in enumerate(zip(scores,times)):
      frame=SimpleNamespace(nominal=datetime.fromtimestamp(float(t),timezone.utc),first_line=1,line_time=lambda r:'test');p=dict(id='P0001C0001',eligible=True,joint_score=float(score) if np.isfinite(score) else None,latitude=35,longitude=128,native_row_1based=1,native_column_1based=1);r=dict(all_pixels=[p],grid_signature='test',scoring={})
      StdStore(d).apply(r,frame,frame,geo,10)
      sigma=float(np.std(scores[k-23:k+1])) if k>=23 and np.isfinite(scores[k-23:k+1]).all() else np.nan
      R=(.8*max(0,sigma-expected_base)+.2*max(0,sigma-sigmas[-4]))/max(2,expected_base) if expected_base is not None and np.isfinite(sigma) and len(sigmas)>=4 and np.isfinite(sigmas[-4]) else np.nan
      # explicit finite baseline/lag requirement
      streak=streak+1 if np.isfinite(R) and R>.5 and sigma-expected_base>=2 else 0
      self.assertEqual(p['std_confirmed'],streak>=2,(name,k))
      if np.isfinite(R):self.assertAlmostEqual(p['std_trend_score'],R,places=10)
      else:self.assertIsNone(p['std_trend_score'])
      sigmas.append(sigma);expected_base=(sigma if expected_base is None else .92*expected_base+.08*sigma) if np.isfinite(sigma) else None
      before=StdStore(d).load();StdStore(d).apply(r,frame,frame,geo,10);np.testing.assert_equal(before['baseline'],StdStore(d).load()['baseline'])
     frame.nominal+=timedelta(seconds=300);StdStore(d).apply(r,frame,frame,geo,10);self.assertIsNone(p['std_trend_score'])

if __name__=='__main__':unittest.main()
