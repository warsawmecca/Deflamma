"""Restart-safe national EMA/integral gate. No score rewriting or noise veto."""
import hashlib,json,os,tempfile
from pathlib import Path
import numpy as np
from noise_filter import analyze_window
from hsd import STEP

VERSION='normal0-ema8-area10-v1'
EXPORT_FIELDS=['temperature_pass','ema_rise_sum','ema_fall_sum','ema_gap','integral_area','integral_status','condition_area','eligible_before_integral']

def atomic_npz(path,**arrays):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='pending_',suffix='.npz',dir=path.parent);os.close(fd)
    try:
        np.savez_compressed(name,**arrays)
        with open(name,'rb') as f:os.fsync(f.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)

class IntegralStore:
    def __init__(self,output):
        self.output=Path(output);self.path=self.output/'integral_state.npz'
    def load(self):
        if not self.path.exists():return None
        with np.load(self.path,allow_pickle=False) as z:return {k:z[k].copy() for k in z.files}
    def apply(self,result,previous,current,geography,top,save_series=True):
        ordered=sorted(result['all_pixels'],key=lambda p:(p['native_row_1based'],p['native_column_1based']))
        if hasattr(geography,'contrast_rows'):
            rr,cc=geography.contrast_rows[:2]
            cells=[(int(r+current.first_line),int(c+1)) for r,c in zip(rr,cc)]
        else:cells=[(p['native_row_1based'],p['native_column_1based']) for p in ordered]
        ids=[f'P{r:04d}C{c:04d}' for r,c in cells];lookup={cell:i for i,cell in enumerate(cells)}
        indices=[lookup[(p['native_row_1based'],p['native_column_1based'])] for p in ordered]
        n=len(ids);t=current.nominal.timestamp()/60
        signature=hashlib.sha256(json.dumps([VERSION,result['grid_signature'],ids,result['scoring'].get('climatology')],sort_keys=True).encode()).hexdigest()
        state=self.load()
        if state is not None and state['signature'].item()!=signature:raise ValueError('Integral grid/model changed; use a new output directory')
        if state is not None and float(state['minute'])>t:raise ValueError('Out-of-order integral update; use a separate archive query')
        # Retry the same observation without integrating it twice.
        if state is None or float(state['minute'])!=t:
            contiguous=state is not None and abs(t-float(state['minute'])-2.5)<1e-7
            ring=state['ring'] if contiguous else np.empty((0,n))
            score=np.full(n,np.nan)
            score[indices]=[np.nan if p['joint_score'] is None else p['joint_score'] for p in ordered]
            ring=np.concatenate([ring,score[None,:]])[-10:]
            U=np.full(n,np.nan);D=U.copy();cancel=U.copy()
            if len(ring)==10:
                stats=analyze_window(ring);valid=stats['valid'];U=np.where(valid,stats['rise'],np.nan);D=np.where(valid,stats['fall'],np.nan);cancel=np.where(valid,stats['canceled'],np.nan)
            oldU=state['emaU'] if contiguous else np.full(n,np.nan);oldD=state['emaD'] if contiguous else np.full(n,np.nan)
            eu=np.where(np.isfinite(U),np.where(np.isfinite(oldU),2/9*U+7/9*oldU,U),np.nan)
            ed=np.where(np.isfinite(D),np.where(np.isfinite(oldD),2/9*D+7/9*oldD,D),np.nan);g=.8*eu-ed
            pg=state['gap'] if contiguous else np.full(n,np.nan);known=state['known'].astype(bool) if contiguous else np.zeros(n,bool)
            oldarea=state['area'] if contiguous else np.zeros(n)
            finite=np.isfinite(g);cross=finite&np.isfinite(pg)&(pg<=0)&(g>0)
            stay=finite&np.isfinite(pg)&(pg>0)&(g>0)&known
            area=np.where(finite&(g<=0),0.,np.nan)
            area[stay]=oldarea[stay]+.5*(pg[stay]+g[stay])*2.5
            area[cross]=.5*g[cross]*(2.5*g[cross]/(g[cross]-pg[cross]))
            known=cross|stay
            state=dict(signature=np.array(signature),minute=np.array(t),ring=ring,emaU=eu,emaD=ed,gap=g,area=area,known=known,U=U,D=D,canceled=cancel)
            atomic_npz(self.path,**state)
        def number(x):return float(x) if np.isfinite(x) else None
        for i,p in zip(indices,ordered):
            T=p['current_K'];N=p.get('normal_skin_K');zero=bool(T is not None and N is not None and np.isfinite(T) and np.isfinite(N) and T>=N)
            area=state['area'][i];gap=state['gap'][i]
            eligible_area=bool(state['known'][i] and np.isfinite(area) and area>10 and gap>0)
            branch=bool(p['condition_rise'] or p['condition_high_score'])
            status='missing' if not np.isfinite(gap) else 'nonpositive' if gap<=0 else 'positive_confirmed' if state['known'][i] else 'awaiting_cross'
            p.update(temperature_pass=zero,ema_rise_sum=number(state['emaU'][i]),ema_fall_sum=number(state['emaD'][i]),ema_gap=number(gap),integral_area=number(area),integral_status=status,condition_area=eligible_area,eligible_before_integral=branch,eligible=bool(zero and eligible_area and branch),adjusted_eligible=bool(zero and eligible_area and branch))
            p['gate_status']='unscored' if p['joint_score'] is None else 'below_normal' if not zero else 'integral_pending' if not eligible_area else 'both' if p['condition_rise'] and p['condition_high_score'] else 'rise_only' if p['condition_rise'] else 'score_only' if p['condition_high_score'] else 'below'
        pixels=result['all_pixels'];candidates=[p for p in pixels if p['eligible']];points=[]
        for rank,item in enumerate(candidates[:top],1):
            p=dict(item);p.update(rank=rank,region=geography.region(p['latitude'],p['longitude']),adjacent8_median_rise_K=None,rise_over_adjacent8_K=None,previous_pixel_line_time_interpolated_kst=previous.line_time(p['native_row_1based']),current_pixel_line_time_interpolated_kst=current.line_time(p['native_row_1based']));points.append(p)
        result.update(top_pixels=points,candidate_pixels=len(candidates),eligible_rising_pixels=len(candidates),gate_counts={s:sum(p['gate_status']==s for p in pixels) for s in ['unscored','below_normal','integral_pending','both','rise_only','score_only','below']})
        result['integral_filter']=dict(version=VERSION,ema_span=8,ema_alpha=2/9,area_threshold=10,area_unit='score-point minute',current_rule='T >= normal_skin_K',history_rule='unchanged: reference T > normal - 5 K, minimum 4 days',noise_classification=False,strict_observed_crossing=True)
        result['filters']['candidate_rule']=VERSION
        if save_series:
            keys=['current_K','rise_K','joint_score','base_score','rank_bonus','cluster_penalty','normal_skin_K','local_excess_K','past_excess_K','component_a','component_b','component_c','component_d','ema_rise_sum','ema_fall_sum','ema_gap','integral_area']
            values=np.full((len(keys),n),np.nan,dtype=np.float32)
            values[:,indices]=np.array([[np.nan if p.get(k) is None else p[k] for p in ordered] for k in keys],dtype=np.float32)
            eligible=np.zeros(n,bool);eligible[indices]=[p['eligible'] for p in ordered]
            atomic_npz(self.output/'series'/current.nominal.strftime('%Y%m%dT%H%M%S.npz'),keys=np.array(keys),values=values,eligible=eligible,signature=np.array(signature),minute=np.array(t),rows=np.array([r for r,c in cells],np.int16),columns=np.array([c for r,c in cells],np.int16))
        return result
