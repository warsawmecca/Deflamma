"""Causal national sigma60 anomaly gate; restart/retry safe, bounded state."""
import hashlib,json
from pathlib import Path
import numpy as np
from integral_filter import atomic_npz

FIELDS=['eligible_before_std','score_std60','std_trend','std_excess','std_trend_score','std_previous_score','std_confirmed']
class StdStore:
    def __init__(self,output):self.path=Path(output)/'std_state.npz'
    def load(self):
        if not self.path.exists():return None
        with np.load(self.path,allow_pickle=False) as z:return {k:z[k].copy() for k in z.files}
    def apply(self,result,previous,current,geo,top):
        rr,cc=geo.contrast_rows[:2];ids=[f'P{int(r+current.first_line):04}C{int(c+1):04}' for r,c in zip(rr,cc)];lookup={p:i for i,p in enumerate(ids)};n=len(ids);t=current.nominal.timestamp()
        signature=hashlib.sha256(json.dumps([ids,result['grid_signature'],result['scoring'].get('climatology')],sort_keys=True).encode()).hexdigest();st=self.load()
        if st is not None and st['signature'].item()!=signature:raise ValueError('STD grid/model changed')
        if st is not None and float(st['time'])>t:raise ValueError('Out-of-order STD update')
        if st is None or float(st['time'])!=t:
            contiguous=st is not None and abs(t-float(st['time'])-150)<1e-6
            ring=st['ring'] if contiguous else np.empty((0,n));base=st['baseline'] if contiguous else np.full(n,np.nan);lag=st['sigma_lag'] if contiguous else np.empty((0,n));streak=st['streak'] if contiguous else np.zeros(n,int)
            scores=np.full(n,np.nan)
            for p in result['all_pixels']:scores[lookup[p['id']]]=np.nan if p['joint_score'] is None else p['joint_score']
            ring=np.concatenate([ring,scores[None]])[-24:];sigma=np.full(n,np.nan)
            if len(ring)==24:
                ok=np.isfinite(ring).all(axis=0);sigma[ok]=ring[:,ok].std(axis=0,ddof=0)
            excess=np.maximum(0,sigma-base);R=np.full(n,np.nan)
            if len(lag)==4:R=.8*excess/np.maximum(2,base)+.2*np.maximum(0,sigma-lag[0])/np.maximum(2,base)
            previous_R=st['R'] if contiguous else np.full(n,np.nan);streak=np.where((R>.5)&(excess>=2),streak+1,0)
            baseline=np.where(np.isfinite(sigma),np.where(np.isfinite(base),.92*base+.08*sigma,sigma),np.nan)
            st=dict(signature=np.array(signature),time=np.array(t),ring=ring,sigma_lag=np.concatenate([lag,sigma[None]])[-4:],baseline=baseline,trend=base,sigma=sigma,excess=excess,R=R,previous_R=previous_R,streak=streak)
            atomic_npz(self.path,**st)
        def val(x):return float(x) if np.isfinite(x) else None
        for p in result['all_pixels']:
            i=lookup[p['id']];old=bool(p.get('eligible_before_std',p['eligible']));confirmed=bool(st['streak'][i]>=2)
            p.update(eligible_before_std=old,score_std60=val(st['sigma'][i]),std_trend=val(st['trend'][i]),std_excess=val(st['excess'][i]),std_trend_score=val(st['R'][i]),std_previous_score=val(st['previous_R'][i]),std_confirmed=confirmed,eligible=old and confirmed,adjusted_eligible=old and confirmed)
            if old and not confirmed:p['gate_status']='std_missing' if p['std_trend_score'] is None else 'std_pending'
        points=[]
        for p in result['all_pixels']:
            if p['eligible'] and len(points)<top:
                q=dict(p);q.update(rank=len(points)+1,region=geo.region(p['latitude'],p['longitude']),adjacent8_median_rise_K=None,rise_over_adjacent8_K=None,previous_pixel_line_time_interpolated_kst=previous.line_time(p['native_row_1based']),current_pixel_line_time_interpolated_kst=current.line_time(p['native_row_1based']));points.append(q)
        count=sum(p['eligible'] for p in result['all_pixels']);result.update(top_pixels=points,candidate_pixels=count,eligible_rising_pixels=count)
        result.setdefault('filters',{})['candidate_rule']='normal0-integral10-std60-v1'
        statuses={p.get('gate_status','unknown') for p in result['all_pixels']}
        result['gate_counts']={status:sum(p.get('gate_status','unknown')==status for p in result['all_pixels']) for status in statuses}
        result['std_trigger']=dict(version='sigma60-trend80-growth20-v1',span=24,alpha=.08,threshold=.5,min_excess=2,consecutive=2,ddof=0,window_samples=24)
        return result

def save_series(result,current,geo,output):
    """Compact, shared two-hour review context; full storage capped by retention."""
    rr,cc=geo.contrast_rows[:2];lookup={(int(r+current.first_line),int(c+1)):i for i,(r,c) in enumerate(zip(rr,cc))};n=len(rr)
    keys=['current_K','rise_K','joint_score','base_score','rank_bonus','cluster_penalty','normal_skin_K','local_excess_K','past_excess_K','component_a','component_b','component_c','component_d','ema_rise_sum','ema_fall_sum','ema_gap','integral_area']+FIELDS[1:]+[f'temperature9_{i}' for i in range(9)]
    values=np.full((len(keys),n),np.nan,np.float32)
    for p in result['all_pixels']:
        i=lookup[(p['native_row_1based'],p['native_column_1based'])]
        for k,key in enumerate(keys[:-9]):
            if p.get(key) is not None:values[k,i]=p[key]
    for index,(dr,dc) in enumerate(( (a,b) for a in [-1,0,1] for b in [-1,0,1] )):
        r=rr+dr;c=cc+dc;ok=(r>=0)&(r<current.bt.shape[0])&(c>=0)&(c<current.bt.shape[1]);values[len(keys)-9+index,ok]=current.bt[r[ok],c[ok]]
    atomic_npz(Path(output)/'series'/current.nominal.strftime('%Y%m%dT%H%M%S.npz'),keys=np.array(keys),values=values,minute=np.array(current.nominal.timestamp()/60),rows=rr+current.first_line,columns=cc+1)
