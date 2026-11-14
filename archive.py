"""Bounded background archive queries isolated from the live output/state."""
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
from threading import RLock,Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit
from zipfile import ZipFile,ZIP_DEFLATED
from core import ALGORITHM_VERSION
from contrast import VERSION as CONTRAST_VERSION
from hsd import UTC,KST,STEP,ObservationQualityError
from watches import WatchError,WatchStore

LOG=logging.getLogger(__name__)
TERMINAL={'ready','error'}
MAX_ARCHIVE_QUERIES=32


def atomic_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='pending_',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


class ArchiveService:
    def __init__(self,output,config=None,watch_store=None,source_factory=None):
        self.output=Path(output).resolve();self.root=self.output/'archive_queries'
        defaults={'satellite':9,'top':10,'temperature_weight':.5,'deviation_strength':.5,
                  'score_mode':'legacy','min_rise_k':0.,'min_current_k':None,'score_scale_max':500.,
                  'boundary':str(Path(__file__).parent/'data/KOR_ADM1.geojson')}
        self.config={**defaults,**(config or {})}
        self.config['boundary']=str(Path(self.config['boundary']).resolve())
        self.config['boundary_sha256']=hashlib.sha256(Path(self.config['boundary']).read_bytes()).hexdigest()
        self.watch_store=watch_store
        self.source_factory=source_factory
        self._queue=queue.Queue(maxsize=4);self._lock=RLock();self._active=set();self._closed=False
        self._prune()
        self._thread=Thread(target=self._work,name='himawari-archive',daemon=True);self._thread.start()

    def _folders(self):
        return [p for p in self.root.glob('*') if p.is_dir() and re.fullmatch(r'[0-9a-f]{32}',p.name)]

    @staticmethod
    def _created(folder):
        try:
            data=json.loads((folder/'query.json').read_text(encoding='utf-8'))
            return data.get('created_utc',''),folder.name
        except (OSError,ValueError):return '',folder.name

    def _prune(self,reserve=0,keep=None):
        # Called under the service lock (or before its worker starts).
        folders=self._folders()
        excess=len(folders)+reserve-MAX_ARCHIVE_QUERIES
        candidates=sorted((p for p in folders if p.name not in self._active and p.name!=keep),key=self._created)
        if excess>len(candidates):raise WatchError('진행 중인 조회가 많습니다. 잠시 뒤 다시 시도하세요.',429)
        for folder in candidates[:max(0,excess)]:
            shutil.rmtree(folder)

    def close(self):
        with self._lock:self._closed=True
        self._thread.join(timeout=.5)

    def info(self):
        return {'timezone':'Asia/Seoul','cadence_seconds':150,'config':{k:v for k,v in self.config.items() if k!='boundary'},
                'server_time_utc':datetime.now(UTC).isoformat(),'max_waiting_queries':4,
                'max_saved_queries':MAX_ARCHIVE_QUERIES}

    def _folder(self,job_id):
        if not re.fullmatch(r'[0-9a-f]{32}',job_id):raise WatchError('조회 작업을 찾을 수 없습니다.',404)
        return self.root/job_id

    def _load(self,job_id):
        try:return json.loads((self._folder(job_id)/'query.json').read_text(encoding='utf-8'))
        except FileNotFoundError:raise WatchError('조회 작업을 찾을 수 없습니다.',404)

    @staticmethod
    def _exports_complete(folder):
        return all((folder/name).is_file() for name in ('latest.json','latest_heatmap.html','latest_map.html',
                   'latest_heatmap.png','latest_map.png','latest.csv','latest_all_pixels.csv','archive_result.zip'))

    def status(self,job_id):
        with self._lock:
            data=self._load(job_id)
            if data['phase'] not in TERMINAL and job_id not in self._active:
                data={**data,'phase':'error','error':'이전 실행 중 조회가 중단됐습니다. 같은 시각을 다시 조회하세요.','error_kind':'interrupted'}
            if data['phase']=='ready' and not self._exports_complete(self._folder(job_id)):
                data={**data,'phase':'error','error':'일부 결과 파일이 없습니다. 같은 시각을 다시 조회하세요.','error_kind':'missing_exports'}
            # Never expose local paths to the client.
            return {k:v for k,v in data.items() if k!='configuration'}

    def recent(self):
        with self._lock:
            folders=sorted(self._folders(),key=self._created,reverse=True)
            return {'queries':[self.status(p.name) for p in folders if (p/'query.json').exists()],
                    'max_saved_queries':MAX_ARCHIVE_QUERIES}

    def submit(self,body):
        from monitor import parse_stamp
        if not isinstance(body,dict) or not isinstance(body.get('time'),str):
            raise WatchError('조회할 한국시간 날짜·시각을 입력하세요.')
        try:stamp=parse_stamp(body['time'])
        except (ValueError,OverflowError):raise WatchError('한국시간의 정확한 2분 30초 관측 시각을 입력하세요. 예: 11:25:00, 11:27:30')
        if stamp>datetime.now(UTC):raise WatchError('미래 시각은 과거 조회할 수 없습니다.')
        satellite=body.get('satellite',self.config['satellite'])
        if type(satellite) is not int or satellite not in (8,9):raise WatchError('위성은 8 또는 9를 선택하세요.')
        allow=body.get('allow_unloading',False)
        if not isinstance(allow,bool):raise WatchError('참고용 언로딩 허용 값은 true/false여야 합니다.')
        from climatology import get_model
        climate = get_model().identity() if self.config.get('score_mode')=='contrast' else None
        configuration={**self.config,'climatology':climate,'satellite':satellite,'allow_unloading':allow,'algorithm':CONTRAST_VERSION if self.config.get('score_mode')=='contrast' else ALGORITHM_VERSION,'archive_version':1}
        key={'time_utc':stamp.isoformat(),'configuration':configuration}
        job_id=hashlib.sha256(json.dumps(key,sort_keys=True).encode()).hexdigest()[:32]
        folder=self._folder(job_id)
        with self._lock:
            if self._closed:raise WatchError('서버가 종료 중입니다.',503)
            if job_id in self._active:return 202,self.status(job_id)
            if (folder/'query.json').exists():
                cached=self.status(job_id)
                if cached['phase']=='ready':return 200,{**cached,'cached':True}
            if self._queue.full():raise WatchError('대기 중인 조회가 많습니다. 잠시 뒤 다시 시도하세요.',429)
            self._prune(reserve=0 if folder.exists() else 1,keep=job_id)
            data={'id':job_id,'phase':'queued','requested_kst':stamp.astimezone(KST).isoformat(),
                  'previous_kst':(stamp-STEP).astimezone(KST).isoformat(),'nominal_utc':stamp.isoformat(),
                  'satellite':satellite,'allow_unloading':allow,'configuration':configuration,
                  'temperature_weight':configuration['temperature_weight'],'deviation_strength':configuration['deviation_strength'],
                  'created_utc':datetime.now(UTC).isoformat(),'updated_utc':datetime.now(UTC).isoformat()}
            atomic_json(folder/'query.json',data)
            self._active.add(job_id);self._queue.put_nowait(job_id)
            return 202,self.status(job_id)

    def _update(self,job_id,**values):
        with self._lock:
            data=self._load(job_id);data.update(values,updated_utc=datetime.now(UTC).isoformat())
            atomic_json(self._folder(job_id)/'query.json',data)

    def _work(self):
        while True:
            with self._lock:
                if self._closed:return
            try:job_id=self._queue.get(timeout=.2)
            except queue.Empty:continue
            try:self._run(job_id)
            except Exception as exc:
                LOG.exception('Archive query failed: %s',job_id)
                if isinstance(exc,HTTPError) and exc.code==404:
                    message='이 위성·시각의 원자료가 공급자에 없습니다. 시각 또는 위성을 확인하세요.';kind='not_found'
                elif isinstance(exc,HTTPError) and exc.code==403:
                    message='공급자가 원자료 접근을 거부했습니다(HTTP 403). 다른 위성·시각을 확인하거나 나중에 재시도하세요.';kind='access_denied'
                elif isinstance(exc,ObservationQualityError):
                    message='원자료 품질 검사에서 제외됐습니다: '+str(exc)+'. 언로딩 flag 4만 참고용 허용으로 다시 조회할 수 있습니다.';kind='quality_rejected'
                else:message='과거 조회 실패: '+str(exc);kind='processing_error'
                try:self._update(job_id,phase='error',error=message,error_kind=kind)
                except OSError:LOG.exception('Could not write archive error status')
            finally:
                folder=self._folder(job_id)
                for sub in ['cache','reference_cache','series','noise_cache']:
                    shutil.rmtree(folder/sub,ignore_errors=True)
                try:
                    from retention import cleanup,storage_lock
                    with storage_lock(self.output):cleanup(self.output,strict=False)
                except OSError:LOG.exception('Archive cleanup failed')
                with self._lock:self._active.discard(job_id)
                self._queue.task_done()

    def _snapshot(self,result,previous,current):
        if self.watch_store is None:return None
        points=self.watch_store.list()['watches']
        if not points:return None
        lookup={(p['native_row_1based'],p['native_column_1based']):p for p in result['all_pixels']}
        snapshot={'watches':points,'histories':{},'latest_nominal_utc':result['current_nominal_utc'],
                  'description':'현재 등록 지점의 요청 시각 단일 관측입니다. 실시간 관심 지점 이력에 추가하지 않습니다.'}
        for point in points:
            sample=WatchStore.sample_values(point,result,lookup,previous,current)
            point['last_sample']=sample
            snapshot['histories'][str(point['id'])]={'watch':point,'hours':6,'start_utc':result['previous_nominal_utc'],
                'end_utc':result['current_nominal_utc'],'cadence_seconds':150,'samples':[sample]}
        return snapshot

    def _run(self,job_id):
        from source import Source
        from monitor import State,Engine,run_lock,write_json,atomic_text
        from maps import render_html
        data=self._load(job_id);config=data['configuration'];folder=self._folder(job_id)
        if hashlib.sha256(Path(config['boundary']).read_bytes()).hexdigest()!=config['boundary_sha256']:
            raise ValueError('경계 파일이 바뀌었습니다. 프로그램을 새 설정으로 다시 실행하세요.')
        from retention import cleanup,storage_lock,MIB
        with storage_lock(self.output):cleanup(self.output,reserve_bytes=256*MIB)
        stamp=datetime.fromisoformat(data['nominal_utc'])
        factory=self.source_factory or Source
        source=factory(folder/'cache',config['satellite'],allow_unloading=config['allow_unloading'],allow_quality_flags=True)
        self._update(job_id,phase='fetching_previous')
        previous=source.frame(stamp-STEP)
        self._update(job_id,phase='fetching_current')
        current=source.frame(stamp)
        self._update(job_id,phase='analyzing')
        signature=json.dumps(config,sort_keys=True)
        with run_lock(folder):
            state=State(folder,signature)
            try:
                # One query folder contains exactly one observation. Rebuild any incomplete exports on retry.
                if not self._exports_complete(folder):
                    state.db.execute('DELETE FROM processed');state.db.commit()
                engine=Engine(folder,Path(config['boundary']),state,config['top'],config['min_rise_k'],config['min_current_k'],
                    history_maps=False,temperature_weight=config['temperature_weight'],deviation_strength=config['deviation_strength'],
                    score_scale_max=config['score_scale_max'],allow_unloading=config['allow_unloading'],latest_only=True,score_mode=config.get('score_mode','legacy'))
                result=engine.process(previous,current,mode='replay',urls=[source.url(stamp-STEP),source.url(stamp)])
                if result is None:result=json.loads((folder/'latest.json').read_text())
            finally:state.close()
        self._update(job_id,phase='rendering')
        features=json.loads(Path(config['boundary']).read_text(encoding='utf-8'))['features']
        snapshot=self._snapshot(result,previous,current)
        for filename,heat in [('latest_heatmap.html',True),('latest_map.html',False)]:
            atomic_text(folder/filename,render_html(result,features,live=False,heat_default=heat,watch_snapshot=snapshot))
        write_json(folder/'status.json',{'mode':'replay','phase':'replay_complete','diagnostic_only':result['quality_assessment']['diagnostic_only'],
                                      'checked_utc':datetime.now(UTC).isoformat()})
        summary={'requested_kst':data['requested_kst'],'previous_kst':data['previous_kst'],'satellite':config['satellite'],
            'valid_pixels':result['valid_pair_pixels'],'max_score':result['all_pixels'][0]['joint_score'],
            'quality_assessment':result['quality_assessment'],'scoring':result['scoring'],'source_urls':result['source_urls']}
        write_json(folder/'query_summary.json',summary)
        pending=folder/'archive_result.pending'
        with ZipFile(pending,'w',ZIP_DEFLATED) as archive:
            for name in ('latest.json','latest.csv','latest_all_pixels.csv','latest_heatmap.html','latest_map.html',
                         'latest_heatmap.png','latest_map.png','query_summary.json'):
                archive.write(folder/name,name)
            # Raw HSDs are reproducible from source_urls; exclude duplicate raw data.
        os.replace(pending,folder/'archive_result.zip')
        for sub in ['cache','reference_cache','series','noise_cache']:
            shutil.rmtree(folder/sub,ignore_errors=True)
        for name in ['integral_state.npz','state.sqlite3','state.sqlite3-wal','state.sqlite3-shm','watchpoints.sqlite3','watchpoints.sqlite3-wal','watchpoints.sqlite3-shm']:
            (folder/name).unlink(missing_ok=True)
        prefix='/archive_queries/'+job_id+'/'
        self._update(job_id,phase='ready',cached=False,diagnostic_only=result['quality_assessment']['diagnostic_only'],
                     valid_pixels=result['valid_pair_pixels'],max_score=summary['max_score'],
                     result_url=prefix+'latest_heatmap.html',map_url=prefix+'latest_map.html',
                     png_url=prefix+'latest_heatmap.png',csv_url=prefix+'latest.csv',
                     all_pixels_csv_url=prefix+'latest_all_pixels.csv',download_url=prefix+'archive_result.zip')

    def dispatch(self,method,path,body=None):
        route=urlsplit(path).path
        if route=='/api/archive/config' and method=='GET':return 200,self.info(),'application/json'
        if route=='/api/archive':
            if method=='GET':return 200,self.recent(),'application/json'
            if method=='POST':
                status,data=self.submit(body);return status,data,'application/json'
        match=re.fullmatch(r'/api/archive/([0-9a-f]{32})',route)
        if match and method=='GET':return 200,self.status(match[1]),'application/json'
        raise WatchError('지원하지 않는 과거 조회 요청입니다.',404)
