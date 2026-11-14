const archiveRemote=live&&['http:','https:'].includes(location.protocol);
let archivePollTimer=null,archiveGeneration=0,archivePosting=false;
const archivePhases={queued:'대기 중',fetching_previous:'직전 관측 원자료 수신 중',fetching_current:'요청 관측 원자료 수신 중',analyzing:'점수 분석·지도 생성 중',rendering:'조회 화면·다운로드 준비 중',ready:'조회 완료',error:'조회 실패'};
function archiveMessage(text,bad=false){$('archiveFeedback').textContent=text;$('archiveFeedback').className=bad?'watch-message error':'watch-message';}
async function archiveRequest(path,method='GET',body){
 const options={method,cache:'no-store'};
 if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
 const response=await fetch(path,options),data=await response.json();
 if(!response.ok)throw Error(data.error||'과거 조회 요청 실패');
 return data;
}
function archiveLink(url,label){
 const a=document.createElement('a');a.textContent=label;
 if(typeof url==='string'&&/^\/archive_queries\/[0-9a-f]{32}\/[a-z_]+\.(html|png|csv|zip)$/.test(url)){a.href=url;a.target='_blank';a.rel='noopener';}
 return a;
}
function showArchiveJob(job){
 $('archiveResultLinks').replaceChildren();$('archivePreview').hidden=true;$('archivePreview').removeAttribute('src');
 const time=kstTime(job.requested_kst),previous=kstTime(job.previous_kst);
 let message=`${time} KST · 직전 ${previous} · Himawari-${job.satellite} · ${archivePhases[job.phase]||job.phase}`;
 if(job.phase==='error')message+=' · '+job.error;
 if(job.phase==='ready'){
  message+=` · 유효 ${Number(job.valid_pixels).toLocaleString()}픽셀 · 최고 ${fmt(job.max_score)}점${job.cached?' · 저장 결과 재사용':''}`;
  if(job.diagnostic_only)message+=' · 참고용: 언로딩 플래그, 위치 정확도 저하 가능';
  for(const[key,label]of [['result_url','점수 지도·순위 열기'],['png_url','PNG 보기'],['csv_url','상위 후보 CSV'],['all_pixels_csv_url','전체 픽셀 CSV'],['download_url','결과 ZIP 다운로드']])$('archiveResultLinks').appendChild(archiveLink(job[key],label));
  const image=archiveLink(job.png_url,'');
  if(image.hasAttribute('href')){$('archivePreview').src=job.png_url;$('archivePreview').alt=time+' KST 과거 관측 히트맵';$('archivePreview').hidden=false;}
 }
 archiveMessage(message,job.phase==='error'||job.diagnostic_only===true);
}
async function refreshArchiveHistory(){
 if(!archiveRemote)return;
 try{const data=await archiveRequest('/api/archive');if(!Array.isArray(data.queries))throw Error('조회 목록 형식 오류');$('archiveRecent').replaceChildren();
  for(const job of data.queries){const li=document.createElement('li'),text=document.createElement('span');text.textContent=`${kstTime(job.requested_kst)} KST · H${job.satellite} · ${archivePhases[job.phase]||job.phase}${job.allow_unloading?' · 참고용 허용':''} `;li.appendChild(text);
   if(job.phase==='ready')li.appendChild(archiveLink(job.result_url,'결과 열기'));
   const fill=document.createElement('button');fill.type='button';fill.textContent='시각 입력';fill.onclick=()=>{$('archiveTime').value=kstTime(job.requested_kst).replace(' ','T');$('archiveSatellite').value=String(job.satellite);$('archiveAllowUnloading').checked=job.allow_unloading===true;};li.appendChild(fill);$('archiveRecent').appendChild(li);
  }
 }catch(error){$('archiveRecent').textContent='저장 조회 목록을 읽지 못했습니다: '+error.message;}
}
async function pollArchive(id,generation){
 if(generation!==archiveGeneration)return;
 try{const job=await archiveRequest('/api/archive/'+encodeURIComponent(id));if(generation!==archiveGeneration)return;showArchiveJob(job);
  if(!['ready','error'].includes(job.phase))archivePollTimer=setTimeout(()=>pollArchive(id,generation),2000);
  else await refreshArchiveHistory();
 }catch(error){if(generation===archiveGeneration)archiveMessage('조회 상태 확인 실패: '+error.message+' · 같은 시각을 다시 조회하면 진행 중 작업에 다시 연결합니다.',true);}
}
async function submitArchive(event){
 event?.preventDefault();if(!archiveRemote||archivePosting)return;
 const value=$('archiveTime').value;if(!value){archiveMessage('한국시간 날짜와 시각을 입력하세요.',true);return;}
 const time=value+'+09:00',stamp=Date.parse(time);
 if(!Number.isFinite(stamp)||stamp%150000!==0){archiveMessage('2분 30초 간격의 시각을 입력하세요. 예: 11:25:00 또는 11:27:30',true);return;}
 archivePosting=true;$('archiveSearch').disabled=true;clearTimeout(archivePollTimer);const generation=++archiveGeneration;
 try{const job=await archiveRequest('/api/archive','POST',{time,satellite:Number($('archiveSatellite').value),allow_unloading:$('archiveAllowUnloading').checked});if(generation!==archiveGeneration)return;showArchiveJob(job);await refreshArchiveHistory();if(!['ready','error'].includes(job.phase))await pollArchive(job.id,generation);}
 catch(error){archiveMessage('조회 요청 실패: '+error.message,true);}
 finally{archivePosting=false;$('archiveSearch').disabled=false;}
}
function shiftArchive(seconds){const stamp=Date.parse($('archiveTime').value+'+09:00');if(Number.isFinite(stamp))$('archiveTime').value=kstTime(new Date(stamp+seconds*1000).toISOString()).replace(' ','T');}
$('archiveForm').onsubmit=submitArchive;$('archivePrevious').onclick=()=>shiftArchive(-150);$('archiveNext').onclick=()=>shiftArchive(150);
async function initArchive(){
 if(!archiveRemote){$('archivePanel').hidden=true;if(location.protocol.startsWith('http')&&location.pathname.startsWith('/archive_queries/'))$('archiveBack').hidden=false;return;}
 try{const info=await archiveRequest('/api/archive/config');if(!info.config)throw Error('새 서버 버전이 필요합니다. 프로그램을 재시작하세요.');const c=info.config;
  $('archiveConfigText').textContent=`한국시간 KST · 직전 2분 30초와 비교 · 계산 ${c.score_mode==='contrast'?'네 입력 대비 지표 (0~100점)':'기존 점수'}, 상위 ${c.top}개 · 실시간 결과·관심 지점 이력과 별도 저장`;
  $('archiveSatellite').value=String(c.satellite);
  if(!$('archiveTime').value){const stamp=Date.parse(info.server_time_utc),rounded=new Date(Math.floor(stamp/150000)*150000).toISOString();$('archiveTime').value=(result?.current_nominal_kst?.slice(0,19)||kstTime(rounded).replace(' ','T'));}
  $('archiveSearch').disabled=false;await refreshArchiveHistory();
 }catch(error){archiveMessage('과거 조회 준비 실패: '+error.message,true);$('archiveSearch').disabled=true;}
}
window.refreshArchiveHistory=refreshArchiveHistory;
