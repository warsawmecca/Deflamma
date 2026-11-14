"""Named native pixels and compact, durable monitoring history.

Connections are opened per operation so the acquisition thread and HTTP
threads never share a SQLite connection. Missing samples remain null.
"""
from contextlib import contextmanager
import csv
from datetime import datetime,timedelta
import io
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit,parse_qs
from hsd import UTC

EXTRA_FIELDS=['normal_skin_K', 'base_score', 'rank_bonus', 'cluster_penalty', 'local_excess_K', 'past_excess_K', 'component_a', 'component_b', 'component_c', 'component_d', 'ema_rise_sum', 'ema_fall_sum', 'ema_gap', 'integral_area']
MAX_WATCHES=32
WATCH_RETENTION_HOURS=24
MAX_WATCH_SAMPLES=576  # (reference - 24h, reference], at a 150-second cadence.


class WatchError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message)
        self.status=status


def clean_name(value):
    if not isinstance(value,str):raise WatchError('이름을 입력하세요.')
    value=value.strip()
    if not 1<=len(value)<=80 or any(ord(c)<32 for c in value):
        raise WatchError('이름은 제어문자 없이 1~80자로 입력하세요.')
    return value


def finite(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


class WatchStore:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS watch_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS watchpoints(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,
                    grid_signature TEXT NOT NULL,native_row INTEGER NOT NULL,native_column INTEGER NOT NULL,
                    latitude REAL NOT NULL,longitude REAL NOT NULL,
                    target_latitude REAL NOT NULL,target_longitude REAL NOT NULL,
                    created_utc TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(grid_signature,native_row,native_column));
                CREATE TABLE IF NOT EXISTS watch_samples(
                    watch_id INTEGER NOT NULL REFERENCES watchpoints(id),nominal_utc TEXT NOT NULL,
                    previous_nominal_utc TEXT NOT NULL,current_K REAL,rise_K REAL,joint_score REAL,
                    score_rank INTEGER,eligible INTEGER NOT NULL,diagnostic_only INTEGER NOT NULL,
                    sample_status TEXT NOT NULL,temperature_weight REAL NOT NULL,deviation_strength REAL NOT NULL,
                    PRIMARY KEY(watch_id,nominal_utc));
            ''')
            existing={r[1] for r in db.execute('PRAGMA table_info(watch_samples)')}
            for key in EXTRA_FIELDS:
                if key not in existing:db.execute('ALTER TABLE watch_samples ADD COLUMN '+key+' REAL')
            self._prune(db)
            self._prune_samples(db)
        # Recover disk space from a large pre-upgrade database once, at startup.
        with self.connect() as db:
            if db.execute('PRAGMA freelist_count').fetchone()[0]>128:
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                db.execute('VACUUM')

    @staticmethod
    def _prune(db):
        # Delete samples first because older databases have a non-cascading FK.
        stale=db.execute('SELECT id FROM watchpoints ORDER BY created_utc DESC,id DESC LIMIT -1 OFFSET ?',
                         (MAX_WATCHES,)).fetchall()
        for row in stale:
            db.execute('DELETE FROM watch_samples WHERE watch_id=?',(row['id'],))
            db.execute('DELETE FROM watchpoints WHERE id=?',(row['id'],))

    @staticmethod
    def _prune_samples(db,now=None):
        references=[WatchStore._latest(db)]
        row=db.execute("SELECT value FROM watch_meta WHERE key='retention_reference_utc'").fetchone()
        if row:references.append(row[0])
        if now is not None:references.append(now.astimezone(UTC).isoformat())
        stamps=[datetime.fromisoformat(s).astimezone(UTC) for s in references if s]
        if not stamps:return
        reference=max(stamps)
        db.execute("INSERT OR REPLACE INTO watch_meta VALUES ('retention_reference_utc',?)",(reference.isoformat(),))
        cutoff=(reference-timedelta(hours=WATCH_RETENTION_HOURS)).isoformat()
        db.execute('DELETE FROM watch_samples WHERE nominal_utc<=?',(cutoff,))
        # Keep a count bound even if an old database contains off-cadence samples.
        db.execute('''DELETE FROM watch_samples WHERE rowid IN (
            SELECT sample_row FROM (
                SELECT rowid AS sample_row,ROW_NUMBER() OVER (
                    PARTITION BY watch_id ORDER BY nominal_utc DESC) AS position
                FROM watch_samples) WHERE position>?)''',(MAX_WATCH_SAMPLES,))

    def prune(self,now=None):
        """Live mode passes wall time, including during satellite outages.

        Replay/offline use their observation watermark so historical analysis
        remains possible. The watermark never moves backwards on late arrivals.
        """
        with self.connect() as db:
            self._prune_samples(db,now)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_size_limit=4194304')
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def _point(row):
        value=dict(row);value['active']=bool(value['active'])
        return value

    @staticmethod
    def _sample(row):
        if row is None:return None
        value=dict(row)
        for key in ('eligible','diagnostic_only'):value[key]=bool(value[key])
        return value

    @staticmethod
    def _latest(db):
        row=db.execute("SELECT value FROM watch_meta WHERE key='latest_nominal_utc'").fetchone()
        return row[0] if row else None

    @staticmethod
    def _advance(db,result):
        stamp=result['current_nominal_utc']
        previous=WatchStore._latest(db)
        if previous is None or stamp>previous:
            db.execute("INSERT OR REPLACE INTO watch_meta VALUES ('latest_nominal_utc',?)",(stamp,))

    @staticmethod
    def sample_values(point,result,lookup,previous=None,current=None):
        same_grid=point['grid_signature']==result.get('grid_signature')
        item=lookup.get((point['native_row'],point['native_column'])) if same_grid else None
        temperature=finite(item['current_K']) if item else None
        delta=finite(item['rise_K']) if item else None
        if not item and same_grid and current is not None:
            r=point['native_row']-current.first_line;c=point['native_column']-1
            if 0<=r<current.bt.shape[0] and 0<=c<current.bt.shape[1]:
                temperature=finite(current.bt[r,c])
                old=finite(previous.bt[r,c]) if previous is not None else None
                delta=temperature-old if temperature is not None and old is not None else None
        diagnostic=bool(result.get('quality_assessment',{}).get('diagnostic_only'))
        status='grid_mismatch' if not same_grid else 'missing' if item is None else 'diagnostic' if diagnostic else 'ok'
        if item and item.get('is_noise') and status=='ok':
            status='noise'
        config=result['scoring']
        return {**{k:finite(item.get(k)) if item else None for k in EXTRA_FIELDS},'watch_id':point['id'],'nominal_utc':result['current_nominal_utc'],
                'previous_nominal_utc':result['previous_nominal_utc'],'current_K':temperature,'rise_K':delta,
                'joint_score':finite(item['joint_score']) if item else None,
                'score_rank':item['score_rank'] if item else None,'eligible':bool(item and item['eligible']),
                'diagnostic_only':diagnostic,'sample_status':status,
                'temperature_weight':config['temperature_weight'],'deviation_strength':config['deviation_strength']}

    @staticmethod
    def _record(db,point,result,lookup,previous=None,current=None):
        sample=WatchStore.sample_values(point,result,lookup,previous,current)
        fields=['watch_id','nominal_utc','previous_nominal_utc','current_K','rise_K','joint_score',
                'score_rank','eligible','diagnostic_only','sample_status','temperature_weight','deviation_strength']
        fields+=EXTRA_FIELDS
        db.execute('INSERT OR REPLACE INTO watch_samples ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+')',tuple(sample[key] for key in fields))

    def record_result(self,result,previous=None,current=None):
        lookup={(p['native_row_1based'],p['native_column_1based']):p for p in result['all_pixels']}
        with self.connect() as db:
            points=db.execute('SELECT * FROM watchpoints WHERE active=1').fetchall()
            for point in points:self._record(db,point,result,lookup,previous,current)
            self._advance(db,result)
            self._prune_samples(db)

    def seed_latest(self,point_id,result):
        """Close a registration/acquisition race without replacing richer stored samples."""
        lookup={(p['native_row_1based'],p['native_column_1based']):p for p in result['all_pixels']}
        with self.connect() as db:
            point=db.execute('SELECT * FROM watchpoints WHERE id=? AND active=1',(point_id,)).fetchone()
            if point and not db.execute('SELECT 1 FROM watch_samples WHERE watch_id=? AND nominal_utc=?',
                                        (point_id,result['current_nominal_utc'])).fetchone():
                self._record(db,point,result,lookup)
            self._advance(db,result)
            self._prune_samples(db)

    def add(self,name,pixel,result,target=None):
        name=clean_name(name)
        if not result.get('grid_signature'):raise WatchError('새 버전으로 관측 한 쌍을 처리한 뒤 저장하세요.',409)
        row,col=pixel['native_row_1based'],pixel['native_column_1based']
        lat,lon=target or (pixel['latitude'],pixel['longitude'])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM watchpoints WHERE grid_signature=? AND native_row=? AND native_column=?',
                                (result['grid_signature'],row,col)).fetchone()
            if existing and existing['active']:
                raise WatchError('이 픽셀은 이미 "'+existing['name']+'"으로 저장되어 있습니다.',409)
            if db.execute('SELECT 1 FROM watchpoints WHERE name=? AND active=1',(name,)).fetchone():
                raise WatchError('이미 사용 중인 이름입니다. 다른 이름을 입력하세요.',409)
            if existing:
                point_id=existing['id']
                db.execute('UPDATE watchpoints SET name=?,active=1,target_latitude=?,target_longitude=?,created_utc=? WHERE id=?',
                           (name,lat,lon,datetime.now(UTC).isoformat(),point_id))
            else:
                cursor=db.execute('''INSERT INTO watchpoints(name,grid_signature,native_row,native_column,
                    latitude,longitude,target_latitude,target_longitude,created_utc) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (name,result['grid_signature'],row,col,pixel['latitude'],pixel['longitude'],lat,lon,datetime.now(UTC).isoformat()))
                point_id=cursor.lastrowid
            point=db.execute('SELECT * FROM watchpoints WHERE id=?',(point_id,)).fetchone()
            self._record(db,point,result,{(row,col):pixel})
            self._advance(db,result)
            self._prune(db)
            self._prune_samples(db)
            return self._point(point)

    def rename(self,point_id,name):
        name=clean_name(name)
        with self.connect() as db:
            point=db.execute('SELECT * FROM watchpoints WHERE id=? AND active=1',(point_id,)).fetchone()
            if point is None:raise WatchError('저장 지점을 찾을 수 없습니다.',404)
            if db.execute('SELECT 1 FROM watchpoints WHERE name=? AND active=1 AND id!=?',(name,point_id)).fetchone():
                raise WatchError('이미 사용 중인 이름입니다.',409)
            db.execute('UPDATE watchpoints SET name=? WHERE id=?',(name,point_id))
            return self._point(db.execute('SELECT * FROM watchpoints WHERE id=?',(point_id,)).fetchone())

    def deactivate(self,point_id):
        with self.connect() as db:
            cursor=db.execute('UPDATE watchpoints SET active=0 WHERE id=? AND active=1',(point_id,))
            if not cursor.rowcount:raise WatchError('저장 지점을 찾을 수 없습니다.',404)
        return {'deactivated':point_id,'history_preserved':True}

    def list(self):
        with self.connect() as db:
            items=[]
            for row in db.execute('SELECT * FROM watchpoints WHERE active=1 ORDER BY id'):
                point=self._point(row)
                point['last_sample']=self._sample(db.execute('SELECT * FROM watch_samples WHERE watch_id=? ORDER BY nominal_utc DESC LIMIT 1',(row['id'],)).fetchone())
                items.append(point)
            return {'watches':items,'latest_nominal_utc':self._latest(db),'max_saved_watches':MAX_WATCHES,
                    'retention_hours':WATCH_RETENTION_HOURS,'max_samples_per_watch':MAX_WATCH_SAMPLES}

    def history(self,point_id,hours=6):
        if not math.isfinite(hours) or not 0<hours<=168:raise WatchError('조회 기간은 0~168시간 이내여야 합니다.')
        hours=min(hours,WATCH_RETENTION_HOURS)  # Older bookmarked API URLs remain usable.
        with self.connect() as db:
            row=db.execute('SELECT * FROM watchpoints WHERE id=?',(point_id,)).fetchone()
            if row is None:raise WatchError('저장 지점을 찾을 수 없습니다.',404)
            end=self._latest(db)
            start=(datetime.fromisoformat(end)-timedelta(hours=hours)).isoformat() if end else None
            rows=db.execute('SELECT * FROM watch_samples WHERE watch_id=? AND nominal_utc>? ORDER BY nominal_utc',
                            (point_id,start or '')).fetchall()
            return {'watch':self._point(row),'hours':hours,'start_utc':start,'end_utc':end,
                    'cadence_seconds':150,'retention_hours':WATCH_RETENTION_HOURS,'samples':[self._sample(p) for p in rows]}


def choose_pixel(result,body):
    """Use an exact native cell; coordinate input is projected, never nearest-valid snapped."""
    if not result.get('grid') or not result.get('grid_signature'):
        raise WatchError('새 버전으로 관측 한 쌍을 처리한 뒤 지점을 저장하세요.',409)
    if body.get('grid_signature') and body['grid_signature']!=result['grid_signature']:
        raise WatchError('관측 격자가 바뀌었습니다. 지점을 다시 선택하세요.',409)
    target=None
    if 'latitude' in body or 'longitude' in body:
        try:lat,lon=float(body['latitude']),float(body['longitude'])
        except (KeyError,ValueError,TypeError):raise WatchError('위도·경도를 숫자로 입력하세요.')
        if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180:
            raise WatchError('위도·경도 범위를 확인하세요.')
        from pyproj import CRS,Transformer
        lon0,cf,lf,co,lo,rs,a,b=result['grid']['projection'];height=(rs-a)*1000
        crs=CRS.from_proj4(f'+proj=geos +lon_0={lon0} +h={height} +a={a*1000} +b={b*1000} +sweep=y +units=m')
        x,y=Transformer.from_crs(4326,crs,always_xy=True).transform(lon,lat)
        if not math.isfinite(x) or not math.isfinite(y):raise WatchError('위성 관측 영역 밖의 좌표입니다.')
        col=math.floor(math.degrees(x/height)*cf/65536+co+.5)
        row=math.floor(-math.degrees(y/height)*lf/65536+lo+.5)
        target=(lat,lon)
        matches=[p for p in result['all_pixels'] if p['native_row_1based']==row and p['native_column_1based']==col]
    else:
        matches=[p for p in result['all_pixels'] if p['id']==body.get('pixel_id')]
    if len(matches)!=1:raise WatchError('선택한 지점이 현재 유효한 남한 육지 픽셀이 아닙니다. 결측·영역을 확인하세요.')
    return matches[0],target


class WatchAPI:
    def __init__(self,output,store=None):
        self.output=Path(output)
        self.store=store or WatchStore(self.output/'watchpoints.sqlite3')

    def dispatch(self,method,path,body=None):
        parsed=urlsplit(path)
        if parsed.path=='/api/watches':
            if method=='GET':return 200,self.store.list(),'application/json'
            if method=='POST':
                clean_name((body or {}).get('name'))
                try:result=json.loads((self.output/'latest.json').read_text(encoding='utf-8'))
                except FileNotFoundError:raise WatchError('아직 처리된 관측이 없습니다.',409)
                pixel,target=choose_pixel(result,body)
                point=self.store.add(body['name'],pixel,result,target)
                latest=json.loads((self.output/'latest.json').read_text(encoding='utf-8'))
                if latest['current_nominal_utc']!=result['current_nominal_utc']:
                    self.store.seed_latest(point['id'],latest)
                return 201,point,'application/json'
        match=re.fullmatch(r'/api/watches/(\d+)(?:/(history|export.csv))?',parsed.path)
        if not match:raise WatchError('지원하지 않는 관심 지점 요청입니다.',404)
        point_id=int(match[1]);action=match[2]
        if action is None:
            if method=='POST':return 200,self.store.rename(point_id,(body or {}).get('name')),'application/json'
            if method=='DELETE':return 200,self.store.deactivate(point_id),'application/json'
        if method=='GET' and action in ('history','export.csv'):
            try:hours=float(parse_qs(parsed.query).get('hours',['6'])[0])
            except ValueError:raise WatchError('조회 기간을 확인하세요.')
            result=self.store.history(point_id,hours)
            if action=='history':return 200,result,'application/json'
            stream=io.StringIO(newline='')
            fields=['nominal_utc','previous_nominal_utc','current_K','rise_K','joint_score','score_rank','eligible',
                    'diagnostic_only','sample_status','temperature_weight','deviation_strength']
            fields+=EXTRA_FIELDS
            writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore')
            writer.writeheader();writer.writerows(result['samples'])
            return 200,'\ufeff'+stream.getvalue(),'text/csv; charset=utf-8'
        raise WatchError('지원하지 않는 요청 방식입니다.',405)
