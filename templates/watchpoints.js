// Named pixels use the local server's durable store, never browser-only storage.
const watchRemote=live&&['http:','https:'].includes(location.protocol);
const watchSnapshot=JSON.parse($('watch-snapshot-data').textContent);
let watchDraft=null,watchList=[],watchHistory=null,watchListVersion=0,watchHistoryVersion=0,watchSaving=false;
const kstTime=value=>{const stamp=Date.parse(value);return Number.isFinite(stamp)?new Date(stamp+9*3600000).toISOString().slice(0,19).replace('T',' '):'—';};
function watchMessage(text,bad=false){$('watchFeedback').textContent=text;$('watchFeedback').className=bad?'watch-message error':'watch-message';}
function updateWatchAvailability(){
 const ready=watchRemote&&Boolean(result?.grid_signature);
 $('watchSave').disabled=!ready||watchSaving;
 for(const id of ['watchName','watchMode'])$(id).disabled=!watchRemote;
 const coordinates=$('watchMode').value==='coordinates';
 for(const id of ['watchLatitude','watchLongitude']){$(id).disabled=!watchRemote||!coordinates;$(id).required=coordinates;}
 $('watchDraft').textContent=coordinates?'입력 좌표를 포함하는 원본 픽셀을 저장합니다.':watchDraft?`선택 픽셀 ${watchDraft.row},${watchDraft.column} · 중심 ${fmt(watchDraft.latitude,5)}°N, ${fmt(watchDraft.longitude,5)}°E`:'지도·표에서 픽셀을 선택하거나 위경도를 입력하세요.';
 $('watchAvailability').textContent=watchRemote?(ready?'이 출력 폴더에 지점과 이력을 저장합니다. 상위 순위와 관계없이 새 관측마다 기록하며 최근 24시간만 보관합니다.':'지점 저장은 새 버전의 첫 관측 처리 후 가능합니다. 저장된 이력 조회는 계속 사용할 수 있습니다.'):(watchSnapshot?(watchSnapshot.description||'이 화면에 포함된 관심 지점 관측입니다. 추가 저장·실시간 기록은 --serve 모드에서 사용하세요.'):'관심 지점 저장·기록은 --serve로 실행한 로컬 웹페이지에서 사용할 수 있습니다.');
 const selected=watchList.some(p=>String(p.id)===$('watchSelect').value);
 $('watchRename').disabled=!watchRemote||!selected;$('watchDeactivate').disabled=!watchRemote||!selected;
 $('watchRenameName').disabled=!watchRemote||!selected;
}
function setWatchDraft(p){
 watchDraft={pixel_id:p.id,grid_signature:result?.grid_signature,row:p.native_row_1based,column:p.native_column_1based,latitude:p.latitude,longitude:p.longitude};
 $('watchMode').value='selected';updateWatchAvailability();
}
async function watchRequest(path,method='GET',body){
 const options={method,cache:'no-store'};
 if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
 const response=await fetch(path,options);
 const payload=await response.json();
 if(!response.ok)throw Error(payload.error||'관심 지점 요청 실패');
 return payload;
}
function renderWatchList(data,preferred){
 if(!Array.isArray(data.watches))throw Error('관심 지점 응답 형식이 올바르지 않습니다.');
 const old=preferred??$('watchSelect').value;
 const editingName=document.activeElement===$('watchRenameName')&&preferred===undefined;
 watchList=data.watches;$('watchSelect').replaceChildren();
 for(const point of watchList){const option=document.createElement('option');option.value=String(point.id);option.textContent=`${point.name} · ${point.native_row},${point.native_column}`;$('watchSelect').appendChild(option);}
 if(watchList.some(p=>String(p.id)===String(old)))$('watchSelect').value=String(old);
 $('watchSelect').disabled=!watchList.length;
 const point=watchList.find(p=>String(p.id)===$('watchSelect').value);
 if(!editingName||String(point?.id)!==String(old))$('watchRenameName').value=point?.name||'';
 updateWatchAvailability();drawSavedWatchMarkers();
}
async function refreshWatches(preferred){
 if(!watchRemote){if(watchSnapshot){renderWatchList(watchSnapshot,preferred);await loadWatchHistory();}return;}
 const version=++watchListVersion;
 try{const data=await watchRequest('/api/watches');if(version!==watchListVersion)return;renderWatchList(data,preferred);await loadWatchHistory();}
 catch(error){if(version===watchListVersion)watchMessage('관심 지점 갱신 실패: '+error.message+' · 표시된 이력 시각을 확인하세요.',true);}
}
async function loadWatchHistory(){
 const version=++watchHistoryVersion,id=$('watchSelect').value,hours=$('watchRange').value;
 if(!id){watchHistory=null;drawWatchHistory();return;}
 try{
  let data;
  if(watchRemote)data=await watchRequest(`/api/watches/${encodeURIComponent(id)}/history?hours=${encodeURIComponent(hours)}`);
  else{const original=watchSnapshot?.histories?.[id];if(!original)throw Error('저장된 이력이 없습니다.');const start=Date.parse(original.end_utc)-Number(hours)*3600000;data={...original,hours:Number(hours),start_utc:new Date(start).toISOString(),samples:original.samples.filter(p=>Date.parse(p.nominal_utc)>=start)};}
  if(version!==watchHistoryVersion)return;
  watchHistory=data;drawWatchHistory();
  if($('watchFeedback').textContent.startsWith('관심 지점 갱신 실패:')||$('watchFeedback').textContent.startsWith('이력 조회 실패:'))watchMessage('이력을 다시 불러왔습니다.');
  $('watchExport').hidden=!watchRemote;
  if(watchRemote)$('watchExport').href=`/api/watches/${encodeURIComponent(id)}/export.csv?hours=${encodeURIComponent(hours)}`;
 }catch(error){if(version===watchHistoryVersion)watchMessage('이력 조회 실패: '+error.message+' · 이전 표시 자료가 남아 있을 수 있습니다.',true);}
}
function watchReading(p){
 if(!p){$('watchReading').textContent='표시할 관측이 없습니다. 지점을 저장하면 현재 관측부터 이력이 쌓입니다.';return;}
 $('watchReading').textContent=`${kstTime(p.nominal_utc)} KST · 최종 점수 ${fmt(p.joint_score)} · 원래 온도 ${fmt(p.current_K,3)} K · 원래 변화량 ${signed(p.rise_K)} K · ${p.sample_status==='noise'?'20분 노이즈 (원점수 보존)':p.sample_status==='ok'?'유효 관측':p.sample_status==='diagnostic'?'언로딩: 참고용':p.sample_status==='grid_mismatch'?'저장 격자와 불일치':'일부 값 결측'}`;
}
function drawWatchChart(id,field,label,unit,color){
 const chart=$(id);chart.replaceChildren();
 const samples=watchHistory?.samples||[],valid=samples.filter(p=>Number.isFinite(p[field])&&Number.isFinite(Date.parse(p.nominal_utc)));
 const W=Math.max(320,chart.clientWidth||640),H=240,L=60,R=20,T=22,B=43;
 chart.setAttribute('viewBox',`0 0 ${W} ${H}`);
 chart.setAttribute('aria-label',`${watchHistory?.watch.name||'관심 지점'} ${label} 시계열`);
 function node(name,attrs={},text){const e=element(name,attrs);if(text!==undefined)e.textContent=text;chart.appendChild(e);return e;}
 node('title',{},`${label} (${unit}) · 선으로 잇지 않은 구간은 결측 또는 관측 간격 초과입니다.`);
 if(!valid.length){node('text',{x:W/2,y:H/2,'text-anchor':'middle',fill:'#607488','font-size':13},'이 기간에 표시할 유효 관측이 없습니다.');return;}
 let xmin=Math.min(...samples.map(p=>Date.parse(p.nominal_utc)).filter(Number.isFinite)),xmax=Math.max(...samples.map(p=>Date.parse(p.nominal_utc)).filter(Number.isFinite));
 if(xmin===xmax){xmin-=150000;xmax+=150000;}
 const values=valid.map(p=>p[field]);let ymin=Math.min(...values),ymax=Math.max(...values);
 if(field!=='current_K'){ymin=Math.min(0,ymin);ymax=Math.max(0,ymax);}
 const pad=Math.max(field==='current_K'?0.2:field==='rise_K'?0.05:1,(ymax-ymin)*.12);ymin-=pad;ymax+=pad;
 const X=t=>L+(t-xmin)/(xmax-xmin)*(W-L-R),Y=v=>H-B-(v-ymin)/(ymax-ymin)*(H-T-B);
 for(let i=0;i<=4;i++){const value=ymin+(ymax-ymin)*i/4,y=Y(value);node('line',{x1:L,x2:W-R,y1:y,y2:y,stroke:'#e3eaf0'});node('text',{x:L-7,y:y+4,'text-anchor':'end',fill:'#607488','font-size':11},fmt(value,field==='joint_score'?1:2));}
 if(field==='integral_area'&&ymin<=10&&ymax>=10)node('line',{x1:L,x2:W-R,y1:Y(10),y2:Y(10),stroke:'#bd793d','stroke-dasharray':'4 3'});
 if(ymin<=0&&ymax>=0)node('line',{x1:L,x2:W-R,y1:Y(0),y2:Y(0),stroke:'#8296a6','stroke-dasharray':'3 3'});
 for(let i=0;i<3;i++){const t=xmin+(xmax-xmin)*i/2,x=X(t);node('text',{x,y:H-22,'text-anchor':i===0?'start':i===2?'end':'middle',fill:'#607488','font-size':11},(xmax-xmin>=86400000?kstTime(new Date(t).toISOString()).slice(5,16):kstTime(new Date(t).toISOString()).slice(11)));}
 node('text',{x:L,y:13,fill:'#4a6578','font-size':11},unit);
 node('text',{x:W-R,y:H-5,'text-anchor':'end',fill:'#607488','font-size':10},'명목 관측시각 · KST');
 let previous=null,path='',pathDiagnostic=false;
 function flush(){if(path)node('path',{d:path,fill:'none',stroke:pathDiagnostic?'#cc7a14':color,'stroke-width':2,'stroke-dasharray':pathDiagnostic?'5 3':'none'});path='';}
 for(const p of samples){const t=Date.parse(p.nominal_utc);if(!Number.isFinite(p[field])||!Number.isFinite(t)){flush();previous=null;continue;}
  const diagnostic=Boolean(p.diagnostic_only||previous?.diagnostic_only),continuous=previous&&t-Date.parse(previous.nominal_utc)===150000;
  if(!continuous){flush();path=`M${X(t)},${Y(p[field])}`;pathDiagnostic=Boolean(p.diagnostic_only);}
  else{if(pathDiagnostic!==diagnostic){flush();path=`M${X(Date.parse(previous.nominal_utc))},${Y(previous[field])}`;pathDiagnostic=diagnostic;}path+=` L${X(t)},${Y(p[field])}`;}
  previous=p;
 }
 flush();
 const stride=Math.max(1,Math.ceil(valid.length/400));
 valid.forEach((p,i)=>{if(i%stride&&i!==valid.length-1&&!p.diagnostic_only)return;const circle=node('circle',{cx:X(Date.parse(p.nominal_utc)),cy:Y(p[field]),r:3,fill:p.diagnostic_only?'#cc7a14':color,'data-nominal':p.nominal_utc});const title=element('title');title.textContent=`${kstTime(p.nominal_utc)} KST\n${label}: ${fmt(p[field],3)} ${unit}${p.diagnostic_only?'\n언로딩: 참고용':''}`;circle.appendChild(title);circle.onclick=()=>watchReading(p);});
}
function resizeWatchMarkers(){
 if(!view)return;
 const unit=Math.max(view[2]/Math.max(svg.clientWidth,320),view[3]/Math.max(svg.clientHeight,300));
 document.querySelectorAll('.watch-marker circle').forEach(p=>{p.setAttribute('r',5*unit);p.setAttribute('stroke-width',1.4*unit);});
 document.querySelectorAll('.watch-marker text').forEach(p=>{p.setAttribute('font-size',11*unit);p.setAttribute('stroke-width',3*unit);p.setAttribute('dx',8*unit);p.setAttribute('dy',-6*unit);});
}
function drawSavedWatchMarkers(){
 $('watchPoints').replaceChildren();
 for(const point of watchList){const[x,y]=proj(point.longitude,point.latitude),g=element('g',{class:'watch-marker'+(String(point.id)===$('watchSelect').value?' selected':''),transform:`translate(${x},${y})`,tabindex:'0',role:'button','aria-label':point.name+' 관심 지점 보기'});
  g.appendChild(element('circle',{fill:'white',stroke:'#167f9e'}));const text=element('text');text.textContent=[...point.name].slice(0,10).join('')+([...point.name].length>10?'…':'');g.appendChild(text);const title=element('title');title.textContent=point.name;g.appendChild(title);
  const activate=()=>{$('watchSelect').value=String(point.id);$('watchRenameName').value=point.name;updateWatchAvailability();loadWatchHistory();};g.onclick=activate;g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();activate();}};$('watchPoints').appendChild(g);
 }
 resizeWatchMarkers();
}
function drawWatchHistory(){
 const data=watchHistory;drawSavedWatchMarkers();
 $('watchHistoryTitle').textContent=data?`${data.watch.name} · 픽셀 중심 ${fmt(data.watch.latitude,5)}°N, ${fmt(data.watch.longitude,5)}°E`:'저장 지점을 선택하세요';
 $('watchHistoryNote').textContent=data?`${data.samples.length}개 기록 · 조회 기준 ${kstTime(data.end_utc)} KST · 점을 누르면 세 값을 확인합니다. 주황 점선은 참고용 관측이며, 누락 구간은 연결하지 않습니다.`:'상위 후보에 들지 않아도 저장 지점은 계속 기록합니다.';
 if(data&&watchRemote&&health?.mode==='live')$('watchHistoryNote').textContent+=' · 명목 관측 후 '+fmt(Math.max(0,(Date.now()-Date.parse(data.end_utc))/60000),1)+'분'+(health.data_healthy===true?'':' · 수집/품질 상태 확인 필요');
 watchReading(data?.samples.at(-1));
 drawWatchChart('watchScoreChart','joint_score','최종 점수','점수','#be364b');
 drawWatchChart('watchTemperatureChart','current_K','원래 B07 밝기온도','K','#236fbb');
 drawWatchChart('watchRiseChart','rise_K','원래 온도 변화량','K / 2.5분','#22816b');
 drawWatchChart('watchLocalChart','local_excess_K','주변 대비','K','#216db4');
 drawWatchChart('watchPastChart','past_excess_K','과거 대비','K','#d68442');
 drawWatchChart('watchUpChart','ema_rise_sum','상승 합 EMA','점수 변화 합','#216db4');
 drawWatchChart('watchDownChart','ema_fall_sum','하락 합 EMA','점수 변화 합','#d68442');
 drawWatchChart('watchGapChart','ema_gap','EMA 차이 g','점수 차이','#53697f');
 drawWatchChart('watchAreaChart','integral_area','누적 면적','점·분','#198e73');
 if(!data)$('watchExport').hidden=true;
}
$('watchMode').onchange=updateWatchAvailability;
$('watchForm').onsubmit=async event=>{
 event.preventDefault();if(!watchRemote)return;
 const name=$('watchName').value.trim();let body={name};
 if($('watchMode').value==='coordinates'){
  if(!$('watchLatitude').value.trim()||!$('watchLongitude').value.trim()){watchMessage('위도와 경도를 모두 입력하세요.',true);return;}
  body.latitude=Number($('watchLatitude').value);body.longitude=Number($('watchLongitude').value);
 }else{if(!watchDraft){watchMessage('먼저 지도나 표에서 픽셀을 선택하세요.',true);return;}body={...body,pixel_id:watchDraft.pixel_id,grid_signature:watchDraft.grid_signature};}
 watchSaving=true;updateWatchAvailability();
 try{const point=await watchRequest('/api/watches','POST',body);watchMessage(`“${point.name}” 저장 완료. 현재 관측부터 이력을 기록합니다.`);$('watchName').value='';await refreshWatches(point.id);}
 catch(error){watchMessage('저장 실패: '+error.message,true);}
 finally{watchSaving=false;updateWatchAvailability();}
};
$('watchSelect').onchange=()=>{const p=watchList.find(p=>String(p.id)===$('watchSelect').value);$('watchRenameName').value=p?.name||'';updateWatchAvailability();loadWatchHistory();};
$('watchRange').onchange=loadWatchHistory;
$('watchRename').onclick=async()=>{const id=$('watchSelect').value;if(!id||!watchRemote)return;try{await watchRequest('/api/watches/'+id,'POST',{name:$('watchRenameName').value});watchMessage('이름을 변경했습니다.');await refreshWatches(id);}catch(error){watchMessage('이름 변경 실패: '+error.message,true);}};
$('watchDeactivate').onclick=async()=>{const id=$('watchSelect').value;if(!id||!watchRemote)return;try{await watchRequest('/api/watches/'+id,'DELETE');watchMessage('추적을 해제했습니다. 최근 24시간 이력만 보존되며 같은 픽셀을 다시 등록하면 복원됩니다(32건 초과 삭제 전까지).');await refreshWatches();}catch(error){watchMessage('추적 해제 실패: '+error.message,true);}};
window.addEventListener('resize',()=>{if(watchHistory)drawWatchHistory();});
window.refreshWatchpoints=refreshWatches;
updateWatchAvailability();drawWatchHistory();
