"""Authenticated, revisioned trigger-episode review with bounded private evidence."""
import argparse,hashlib,hmac,json,os,re,secrets,sqlite3,time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
import numpy as np
from watches import WatchError

LABELS={'pending':'검토 대기','possible':'산불 가능성 있음','not_fire':'산불이 아님','expired':'기간 지남'}
def reviewers_file():return Path(os.environ.get('DEFLAMMA_REVIEWERS_FILE',str(Path(__file__).with_name('reviewers.json'))))
def authorize(header,output):
    path=reviewers_file().resolve()
    if path.is_relative_to(Path(output).resolve()):raise WatchError('관리원 설정을 공개 출력 폴더 밖으로 옮기세요.',503)
    token=(header or '').removeprefix('Bearer ')
    if not header or not header.startswith('Bearer ') or len(token)>256:raise WatchError('승인된 관리원 인증이 필요합니다.',401)
    digest=hashlib.sha256(token.encode()).hexdigest()
    try:users=json.loads(path.read_text())
    except (OSError,ValueError):raise WatchError('관리원 계정 설정이 필요합니다.',503)
    for u in users:
        if u.get('active') and hmac.compare_digest(str(u.get('token_sha256','')),digest):return u['name']
    raise WatchError('관리원 인증에 실패했습니다.',401)

class ReviewStore:
    def __init__(self,output):
        self.output=Path(output);self.output.mkdir(parents=True,exist_ok=True);self.path=self.output/'reviews.sqlite3'
        if self.path.is_symlink():raise ValueError('Review database cannot be a symlink')
        self.ttl=max(5,min(240,int(os.environ.get('DEFLAMMA_REVIEW_TTL_MINUTES','30'))))*60
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY AUTOINCREMENT, pixel TEXT NOT NULL, first_nominal TEXT NOT NULL, last_nominal TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, deadline REAL NOT NULL, summary TEXT NOT NULL, evidence TEXT, audit TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS pixel_time ON cases(pixel,last_nominal);
            CREATE INDEX IF NOT EXISTS status_updated ON cases(status,updated);
            CREATE TABLE IF NOT EXISTS frames(nominal TEXT PRIMARY KEY,created REAL NOT NULL);''')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30);db.row_factory=sqlite3.Row;db.execute('PRAGMA secure_delete=ON');db.execute('PRAGMA max_page_count=16384')
        try:
            with db:yield db
        finally:db.close()
    def _clean(self,db,now):
        db.execute("UPDATE cases SET status='expired',evidence=NULL,revision=revision+1,updated=? WHERE status='pending' AND deadline<=?",(now,now))
        db.execute("DELETE FROM cases WHERE (status='expired' AND updated<?) OR (status IN ('possible','not_fire') AND updated<?)",(now-86400,now-7*86400))
        for status,limit in [('expired',500),('possible',1000),('not_fire',1000)]:
            db.execute('DELETE FROM cases WHERE id IN (SELECT id FROM cases WHERE status=? ORDER BY updated DESC,id DESC LIMIT -1 OFFSET ?)',(status,limit))
        excess=max(0,db.execute("SELECT count(*) FROM cases WHERE status='pending'").fetchone()[0]-500)
        if excess:db.execute("UPDATE cases SET status='expired',evidence=NULL,revision=revision+1,updated=? WHERE id IN (SELECT id FROM cases WHERE status='pending' ORDER BY created,id LIMIT ?)",(now,excess))
        # Capacity expiry can introduce additional tombstones in the same pass.
        db.execute("DELETE FROM cases WHERE id IN (SELECT id FROM cases WHERE status='expired' ORDER BY updated DESC,id DESC LIMIT -1 OFFSET 500)")
        db.execute('DELETE FROM frames WHERE created<?',(now-86400,))
        db.execute('DELETE FROM frames WHERE nominal IN (SELECT nominal FROM frames ORDER BY created DESC,nominal DESC LIMIT -1 OFFSET 576)')
    def cleanup(self,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            self._clean(db,now);db.commit()
            if db.execute('PRAGMA freelist_count').fetchone()[0]>128:db.execute('VACUUM')
        return {'max_pending':500,'expired_keep_hours':24,'completed_keep_days':7,'evidence_deleted_on_completion':True}
    def record(self,result,contexts,now=None):
        now=time.time() if now is None else now;nominal=result['current_nominal_utc']
        from datetime import datetime,timedelta
        prior=(datetime.fromisoformat(nominal)-timedelta(seconds=150)).isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._clean(db,now)
            if db.execute('SELECT 1 FROM frames WHERE nominal=?',(nominal,)).fetchone():return
            for p in result['all_pixels']:
                if not p['eligible']:continue
                old=db.execute('SELECT * FROM cases WHERE pixel=? ORDER BY last_nominal DESC,id DESC LIMIT 1',(p['id'],)).fetchone()
                evidence=json.dumps(contexts.get(p['id'],{}),ensure_ascii=False,allow_nan=False,separators=(',',':'))
                if len(evidence.encode())>64*1024:raise ValueError('Review evidence exceeds bounded payload')
                summary=json.dumps(p,ensure_ascii=False,allow_nan=False,separators=(',',':'))
                if old is not None and old['last_nominal']==prior:
                    # Every newly observed trigger needs review, even after a negative decision.
                    deadline=old['deadline'] if old['status']=='pending' else now+self.ttl
                    db.execute("UPDATE cases SET last_nominal=?,revision=revision+1,status='pending',updated=?,deadline=?,summary=?,evidence=? WHERE id=?",(nominal,now,deadline,summary,evidence,old['id']))
                else:
                    db.execute("INSERT INTO cases(pixel,first_nominal,last_nominal,revision,status,created,updated,deadline,summary,evidence,audit) VALUES (?,?,?,1,'pending',?,?,?,?,?,'[]')",(p['id'],nominal,nominal,now,now,now+self.ttl,summary,evidence))
            db.execute('INSERT INTO frames VALUES (?,?)',(nominal,now));self._clean(db,now)
    def list(self,status='pending',offset=0,q='',now=None):
        if status not in LABELS:raise WatchError('검토 분류를 확인하세요.')
        now=time.time() if now is None else now
        with self.db() as db:
            self._clean(db,now);counts={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM cases GROUP BY status')}
            rows=db.execute('SELECT id,pixel,first_nominal,last_nominal,revision,status,deadline,summary,audit FROM cases WHERE status=? AND pixel LIKE ? ORDER BY updated DESC,id DESC LIMIT 100 OFFSET ?',(status,'%'+q+'%',offset)).fetchall()
        return dict(items=[self.unpack(r) for r in rows],counts=counts,offset=offset,limit=100,ttl_minutes=self.ttl//60,labels=LABELS)
    def unpack(self,r):
        p=dict(r)
        for k in ['summary','evidence','audit']:
            if k in p:p[k]=json.loads(p[k]) if p[k] is not None else None
        return p
    def get(self,case_id,now=None):
        with self.db() as db:
            self._clean(db,time.time() if now is None else now);r=db.execute('SELECT * FROM cases WHERE id=?',(case_id,)).fetchone()
        if r is None:raise WatchError('검토가 없거나 보관 기간이 지났습니다.',404)
        return self.unpack(r)
    def decide(self,case_id,revision,verdict,reviewer,note='',now=None):
        if verdict not in ('possible','not_fire') or type(revision) is not int:raise WatchError('검토 내용을 확인하세요.')
        if not isinstance(note,str) or len(note)>500:raise WatchError('메모는 500자 이하입니다.')
        now=time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._clean(db,now);r=db.execute('SELECT * FROM cases WHERE id=?',(case_id,)).fetchone()
            if r is None:raise WatchError('검토가 없습니다.',404)
            if r['status']!='pending' or r['revision']!=revision:raise WatchError('새 트리거가 들어왔거나 상태가 바뀌었습니다. 다시 열어 확인하세요.',409)
            audit=json.loads(r['audit']);audit.append(dict(reviewer=reviewer,verdict=verdict,at=now,through_nominal=r['last_nominal'],revision=revision,note=note))
            audit=audit[-8:]
            while len(json.dumps(audit,ensure_ascii=False).encode())>4096 and len(audit)>1:audit.pop(0)
            db.execute('UPDATE cases SET status=?,updated=?,revision=revision+1,evidence=NULL,audit=? WHERE id=?',(verdict,now,json.dumps(audit,ensure_ascii=False),case_id))
        return self.get(case_id,now)

def context_for(result,output,current):
    targets={p['id']:p for p in result['all_pixels'] if p['eligible']}
    if not targets:return {}
    samples={pid:[] for pid in targets};cutoff=current.nominal.timestamp()-7200
    for path in sorted((Path(output)/'series').glob('*.npz')):
        if path.is_symlink():continue
        with np.load(path,allow_pickle=False) as z:
            stamp=float(z['minute'])*60
            if stamp<cutoff or stamp>current.nominal.timestamp():continue
            lookup={(int(r),int(c)):i for i,(r,c) in enumerate(zip(z['rows'],z['columns']))};keys=z['keys'].tolist();values=z['values']
            for pid,p in targets.items():
                i=lookup.get((p['native_row_1based'],p['native_column_1based']))
                if i is None:continue
                row={k:float(values[j,i]) if np.isfinite(values[j,i]) else None for j,k in enumerate(keys)};row['utc_seconds']=stamp;samples[pid].append(row)
    out={}
    for pid,rows in samples.items():
        context=dict(samples=rows[-49:],quality_flags=current.quality_flags,observed_kst=current.line_time(targets[pid]['native_row_1based']))
        while len(json.dumps(context,ensure_ascii=False).encode())>64*1024 and context['samples']:context['samples'].pop(0)
        out[pid]=context
    return out

class ReviewAPI:
    def __init__(self,output):self.store=ReviewStore(output)
    def dispatch(self,method,path,body,reviewer):
        parsed=urlsplit(path);q=parse_qs(parsed.query)
        if parsed.path=='/api/reviews' and method=='GET':
            try:offset=int(q.get('offset',['0'])[0])
            except ValueError:raise WatchError('페이지를 확인하세요.')
            if not 0<=offset<=10000:raise WatchError('페이지를 확인하세요.')
            return 200,self.store.list(q.get('status',['pending'])[0],offset,q.get('q',[''])[0][:30]),'application/json'
        m=re.fullmatch(r'/api/reviews/(\d+)',parsed.path)
        if m:
            if method=='GET':return 200,self.store.get(int(m[1])),'application/json'
            if method=='POST':return 200,self.store.decide(int(m[1]),(body or {}).get('revision'),(body or {}).get('verdict'),reviewer,(body or {}).get('note','')),'application/json'
        raise WatchError('지원하지 않는 검토 요청입니다.',404)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['add','revoke']);p.add_argument('name');a=p.parse_args();path=reviewers_file()
    if not re.fullmatch(r'[\w가-힣 .-]{1,60}',a.name):p.error('관리원 이름은 1~60자의 글자·숫자·공백만 허용합니다.')
    users=json.loads(path.read_text()) if path.exists() else [];users=[u for u in users if u['name']!=a.name]
    if a.action=='add':
        token=secrets.token_urlsafe(32);users.append(dict(name=a.name,active=True,token_sha256=hashlib.sha256(token.encode()).hexdigest()))
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.pending');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(users,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    os.replace(temp,path);os.chmod(path,0o600)
    if a.action=='add':print('관리원:',a.name,'\n접속 토큰(한 번만 표시):',token)
    else:print('관리원 권한을 해제했습니다:',a.name)
if __name__=='__main__':main()
