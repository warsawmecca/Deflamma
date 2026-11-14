import sys,tempfile,unittest,json,os,hashlib,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maps import start_server

class ReviewHTTPTests(unittest.TestCase):
 def test_private_api_rejects_legacy_admin_header_and_cross_origin(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);out=root/'output';out.mkdir();users=root/'reviewers.json';users.write_text(json.dumps([dict(name='Approved',active=True,token_sha256=hashlib.sha256(b'test').hexdigest())]))
   with patch.dict(os.environ,DEFLAMMA_REVIEWERS_FILE=str(users)):
    server=start_server(out,port=0)
    try:
     base='http://localhost:'+str(server.server_port)
     def status(path,headers=None):
      try:
       with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers or {})) as r:return r.status
      except urllib.error.HTTPError as e:return e.code
     self.assertEqual(status('/reviews'),200);self.assertEqual(status('/api/reviews'),401);self.assertEqual(status('/api/reviews',{'X-Himawari-Admin':'anything'}),401);self.assertEqual(status('/api/reviews',{'Authorization':'Bearer test'}),200);self.assertEqual(status('/api/reviews',{'Authorization':'Bearer test','Origin':'https://evil.example'}),403);self.assertEqual(status('/reviews.sqlite3'),403)
     users.write_text('[]');self.assertEqual(status('/api/reviews',{'Authorization':'Bearer test'}),401)
    finally:server.shutdown();server.server_close()
