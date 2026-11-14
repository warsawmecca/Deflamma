"""Offline point maps and optional localhost viewer; no tiles/API key needed."""
from datetime import datetime
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import urllib.parse
import webbrowser
import numpy as np
from hsd import KST
from watches import WatchAPI,WatchError
from render_lock import serialized_render

LOG = logging.getLogger(__name__)
MAP_VERSION = 'deflamma-v9-noise-nogate'


def _script_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')


def render_html(result, features, live=False, heat_default=False, watch_snapshot=None):
    template = (Path(__file__).parent/'templates/dashboard.html').read_text(encoding='utf-8')
    replacements = {'__BOUNDARY_JSON__':_script_json(features),
                    '__SNAPSHOT_JSON__':_script_json(result), '__LIVE_MODE__':'true' if live else 'false',
                    '__HEAT_DEFAULT__':'true' if heat_default else 'false',
                    '__WATCH_SNAPSHOT__':_script_json(watch_snapshot),
                    '__WATCH_JS__':(Path(__file__).parent/'templates/watchpoints.js').read_text(encoding='utf-8'),
                    '__ARCHIVE_JS__':(Path(__file__).parent/'templates/archive.js').read_text(encoding='utf-8')}
    return re.sub('|'.join(replacements),lambda m:replacements[m.group(0)],template)


@serialized_render
def render_png(result, features):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import colors, cm, font_manager
    fonts = set(font_manager.get_font_names())
    family = next((name for name in ['Malgun Gothic','AppleGothic','Noto Sans CJK KR',
                       'Noto Sans CJK JP','NanumGothic'] if name in fonts),None)
    ko = family is not None
    points = result['top_pixels']
    max_rise = result.get('score_scale_max',500)
    norm = colors.Normalize(vmin=0,vmax=max_rise)
    with plt.rc_context({'font.family':family or 'DejaVu Sans','axes.unicode_minus':False}):
        fig,axs = plt.subplots(1,2,figsize=(12,7),layout='constrained')
        for ax in axs:
            ax.set_facecolor('#edf3f7')
            for feature in features:
                geometry = feature['geometry']
                polygons = geometry['coordinates'] if geometry['type']=='MultiPolygon' else [geometry['coordinates']]
                for polygon in polygons:
                    ring = np.asarray(polygon[0])
                    ax.fill(ring[:,0],ring[:,1],facecolor='#f7f9fb',edgecolor='#8898a7',lw=.5,zorder=1)
            ax.grid(alpha=.18)
            ax.set_xlabel('경도 (°E)' if ko else 'Longitude (E)')
            ax.set_ylabel('위도 (°N)' if ko else 'Latitude (N)')
        axs[0].set(xlim=(124.3,132.2),ylim=(32.8,38.9))
        axs[0].set_aspect(1/np.cos(np.deg2rad(36)))
        axs[0].set_title('남한 전체 위치' if ko else 'ROK land overview')
        if points:
            lons = [p['longitude'] for p in points];lats = [p['latitude'] for p in points]
            padx=max(.12,(max(lons)-min(lons))*.25);pady=max(.12,(max(lats)-min(lats))*.25)
            axs[1].set(xlim=(min(lons)-padx,max(lons)+padx),ylim=(min(lats)-pady,max(lats)+pady))
            axs[1].set_aspect(1/np.cos(np.deg2rad(np.mean(lats))))
            for ax in axs:
                ax.scatter(lons,lats,c=[p['joint_score'] for p in points],cmap='YlOrRd',norm=norm,
                           s=75,edgecolor='#142c41',lw=.9,zorder=4)
                # Keep the overview uncluttered; label individual ranks in the zoom panel.
                if ax is axs[1]:
                    fig.canvas.draw()
                    locations=ax.transData.transform(list(zip(lons,lats)))
                    placed=[]
                    scale=fig.dpi/72
                    for p,(x,y) in zip(points,locations):
                        candidates=[(dx,dy) for radius in (12,22,32,44)
                                    for dx,dy in [(radius,6),(-radius,6),(radius,-12),(-radius,-12),(0,radius),(0,-radius)]]
                        for dx,dy in candidates:
                            cx,cy=x+dx*scale,y+dy*scale
                            width=12 if p['rank']<10 else 18
                            box=(cx-width/2,cy-6,cx+width/2,cy+6)
                            if any(box[0]<b[2]+3 and box[2]>b[0]-3 and box[1]<b[3]+3 and box[3]>b[1]-3 for b in placed): continue
                            if any(abs(cx-px)<width/2+7 and abs(cy-py)<13 for px,py in locations): continue
                            break
                        placed.append(box)
                        ax.annotate(str(p['rank']),(p['longitude'],p['latitude']),
                            xytext=(dx,dy),textcoords='offset points',ha='center',va='center',
                            fontsize=8,color='#183248',zorder=5,
                            arrowprops={'arrowstyle':'-','color':'#6f8293','lw':.5},
                            bbox={'facecolor':'white','alpha':.85,'edgecolor':'none','pad':.8})
        else:
            axs[1].set(xlim=(124.3,132.2),ylim=(32.8,38.9))
            axs[1].set_aspect(1/np.cos(np.deg2rad(36)))
            axs[1].text(.5,.5,'조건을 만족하는 상승 픽셀 없음' if ko else 'No qualifying rising pixels',
                         transform=axs[1].transAxes,ha='center')
        axs[1].set_title('상위 픽셀 확대' if ko else 'Top-pixel extent')
        fig.colorbar(cm.ScalarMappable(norm=norm,cmap='YlOrRd'),ax=axs,
                     label='최종 결합 점수' if ko else 'Joint score',shrink=.68,extend='max')
        previous=datetime.fromisoformat(result['previous_nominal_kst'])
        current=datetime.fromisoformat(result['current_nominal_kst'])
        if ko:
            title=f"Band 7 결합 점수 상위 {len(points)}픽셀 | {current:%Y-%m-%d} KST\n{previous:%H:%M:%S} → {current:%H:%M:%S} · 화재 판정 아님"
            note='JMA / NOAA · Natural Earth / geoBoundaries · 명목 관측시각, 픽셀 중심 좌표'
        else:
            title=f"Band 7 top {len(points)} scored pixels | {current:%Y-%m-%d} KST\n{previous:%H:%M:%S} -> {current:%H:%M:%S} | Not a fire classification"
            note='JMA / NOAA | Natural Earth / geoBoundaries | Nominal times; native pixel centers'
        if result.get('quality_assessment',{}).get('diagnostic_only'):
            title+='\n참고용: 언로딩 플래그 · 위치 정확도 저하 가능' if ko else '\nDIAGNOSTIC ONLY: UNLOADING FLAG'
        fig.suptitle(title,fontsize=14)
        fig.supxlabel(note,fontsize=8,color='#5d6b78')
        buffer=io.BytesIO()
        try:
            fig.savefig(buffer,format='png',dpi=160)
            return buffer.getvalue()
        finally:
            plt.close(fig)


def start_server(output, port=8765, open_browser=False,watch_store=None,archive_config=None,archive_service=None):
    output=Path(output).resolve()
    public_origin=os.environ.get('HIMAWARI_PUBLIC_ORIGIN','').rstrip('/')
    public=urllib.parse.urlsplit(public_origin)
    if public_origin and (public.scheme!='https' or not public.hostname or public.path or public.query or public.fragment or public.username or public.password):
        raise ValueError('HIMAWARI_PUBLIC_ORIGIN must be an HTTPS origin without a path')
    from reviews import ReviewAPI,authorize
    reviews=ReviewAPI(output)
    (output/'reviews.html').write_text((Path(__file__).parent/'templates/reviews.html').read_text(),encoding='utf-8')
    watch_api=WatchAPI(output,watch_store)
    from archive import ArchiveService
    archives=archive_service or ArchiveService(output,archive_config,watch_api.store)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,directory=str(output),**kwargs)

        def send_head(self):
            if urllib.parse.urlsplit(self.path).path=='/reviews':self.path='/reviews.html'
            if urllib.parse.urlsplit(self.path).path=='/':
                self.path='/latest_heatmap.html' if (output/'latest_heatmap.html').exists() else '/latest_map.html'
            path=Path(self.translate_path(self.path)).resolve()
            if not path.is_relative_to(output) or path.suffix.lower() not in {'.html','.json','.csv','.png','.zip'}:
                self.send_error(403,'Only map/data exports are served')
                return None
            return super().send_head()

        def end_headers(self):
            self.send_header('Cache-Control','no-store, max-age=0')
            super().end_headers()

        def _api(self,method):
            try:
                authority=self.headers.get('Host','')
                host=urllib.parse.urlsplit('http://'+authority)
                local=host.hostname in ('127.0.0.1','localhost') and host.port==self.server.server_port
                forwarded=(bool(public_origin) and authority==public.netloc
                           and self.client_address[0]=='127.0.0.1'
                           and self.headers.get('X-Forwarded-Proto')=='https')
                if not (local or forwarded):
                    raise WatchError('허용되지 않은 서버 주소입니다.',403)
                expected_origin='http://'+authority if local else public_origin
                origin=self.headers.get('Origin')
                if origin and origin!=expected_origin:
                    raise WatchError('다른 출처의 요청은 허용하지 않습니다.',403)
                public_create=method=='POST' and urllib.parse.urlsplit(self.path).path in ('/api/watches','/api/archive')
                if not local and method not in ('GET','HEAD') and not public_create and not urllib.parse.urlsplit(self.path).path.startswith('/api/reviews') and not self.headers.get('X-Himawari-Admin'):
                    raise WatchError('관리자 로그인이 필요합니다.',403)
                body=None
                if method=='POST':
                    if self.headers.get_content_type()!='application/json':
                        raise WatchError('JSON 요청만 허용합니다.',415)
                    try:length=int(self.headers.get('Content-Length','0'))
                    except ValueError:raise WatchError('요청 길이를 확인하세요.')
                    if not 0<length<=16384:raise WatchError('요청 크기가 허용 범위를 벗어났습니다.',413)
                    try:body=json.loads(self.rfile.read(length).decode('utf-8'))
                    except (ValueError,UnicodeError):raise WatchError('JSON 요청을 확인하세요.')
                    if not isinstance(body,dict):raise WatchError('JSON 객체가 필요합니다.')
                if urllib.parse.urlsplit(self.path).path.startswith('/api/reviews'):
                    reviewer=authorize(self.headers.get('Authorization'),output)
                    status,payload,content_type=reviews.dispatch(method,self.path,body,reviewer)
                else:
                    target=archives if urllib.parse.urlsplit(self.path).path.startswith('/api/archive') else watch_api
                    status,payload,content_type=target.dispatch(method,self.path,body)
            except WatchError as exc:
                status,payload,content_type=exc.status,{'error':str(exc)},'application/json'
            except Exception:
                LOG.exception('Local API request failed')
                status,payload,content_type=500,{'error':'요청 처리 실패. 수집 로그와 저장 공간을 확인하세요.'},'application/json'
            blob=(json.dumps(payload,ensure_ascii=False,allow_nan=False) if content_type=='application/json' else payload).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type',content_type+'; charset=utf-8' if content_type=='application/json' else content_type)
            self.send_header('Content-Length',str(len(blob)))
            if content_type.startswith('text/csv'):
                self.send_header('Content-Disposition','attachment; filename="watch_history.csv"')
            self.end_headers();self.wfile.write(blob)

        def do_GET(self):
            if urllib.parse.urlsplit(self.path).path.startswith('/api/'):
                self._api('GET')
            else:super().do_GET()

        def do_POST(self):self._api('POST')

        def do_DELETE(self):self._api('DELETE')

        def log_message(self,format,*args):
            LOG.debug('Map HTTP: '+format,*args)
    class Server(ThreadingHTTPServer):
        def handle_error(self,request,client_address):
            # A cancelled download ends only this HTTP request. Keep unrelated
            # exceptions visible instead of suppressing all request failures.
            error=sys.exc_info()[1]
            if isinstance(error,(BrokenPipeError,ConnectionResetError)):
                LOG.debug('HTTP client disconnected during request: %s (%s)',
                          client_address,type(error).__name__)
                return
            super().handle_error(request,client_address)

        def server_close(self):
            archives.close()
            super().server_close()
    try:server=Server(('127.0.0.1',port),Handler)
    except Exception:
        archives.close();raise
    server.daemon_threads=True
    Thread(target=server.serve_forever,daemon=True).start()
    page='latest_heatmap.html' if (output/'latest_heatmap.html').exists() else 'latest_map.html'
    url=f'http://127.0.0.1:{server.server_port}/{page}'
    LOG.info('Map viewer: %s',url)
    if open_browser:
        webbrowser.open(url)
    return server
