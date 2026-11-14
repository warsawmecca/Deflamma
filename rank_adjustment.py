import numpy as np
from contrast import candidate_conditions

EXPORT_FIELDS = ['base_score', 'previous_base_rank', 'base_rank', 'rank_gain',
                 'rank_bonus', 'cluster_support', 'cluster_neighbors', 'cluster_penalty',
                 'adjusted_score', 'adjusted_eligible', 'adjusted_rank']

def adjust(current,previous):
    old={p['id']:p for p in previous['all_pixels']}
    cells={(p['native_row_1based'],p['native_column_1based']):p for p in current['all_pixels']}
    for p in current['all_pixels']:
        s=p['joint_score'];q=old.get(p['id']);rp=q['score_rank'] if q else None;rc=p['score_rank']
        bonus=0.
        if s is not None and rp and rc:
            bonus=10*min(1,max(0,np.log2(rp/rc)/4))*min(1,s/20)
        r,c=p['native_row_1based'],p['native_column_1based']
        near=[cells.get((r+dr,c+dc)) for dr in range(-5,6) for dc in range(-5,6) if dr or dc]
        near=[n for n in near if n and n['joint_score'] is not None]
        support=[n for n in near if s is not None and n['joint_score']>=max(5,.5*s)]
        count=len(support)
        penalty=.4*s*min(1,count/(.25*len(near))) if s is not None and len(near)>=48 and count>=8 and count/len(near)>=.1 else 0.
        adjusted=None if s is None else float(np.clip(s+bonus-penalty,0,100))
        p.update(base_score=s,previous_base_rank=rp,base_rank=rc,rank_gain=None if not rp or not rc else rp-rc,rank_bonus=float(bonus),cluster_support=count,cluster_neighbors=len(near),cluster_penalty=float(penalty),adjusted_score=adjusted)
        a,b,e=candidate_conditions(adjusted,p['rise_K'],p['current_K'])
        p['adjusted_eligible']=bool(e)
    ordered=sorted(current['all_pixels'],key=lambda p:(-(p['adjusted_score'] if p['adjusted_score'] is not None else -1),p['native_row_1based'],p['native_column_1based']))
    scores=np.sort([p['adjusted_score'] for p in ordered if p['adjusted_score'] is not None])
    for p in ordered:p['adjusted_rank']=int(len(scores)-np.searchsorted(scores,p['adjusted_score'],side='right')+1) if p['adjusted_score'] is not None else None
    return ordered


def finalize(result, previous_result, previous, current, geography, top):
    """Use unadjusted ranks for the bonus; publish adjusted scores everywhere."""
    pixels = adjust(result, previous_result)
    for item in pixels:
        item['joint_score'] = item['adjusted_score']
        item['score_rank'] = item['adjusted_rank']
        score = item['joint_score']
        rising, high, eligible = candidate_conditions(score, item['rise_K'], item['current_K'])
        item.update(eligible=bool(eligible), condition_rise=bool(rising), condition_high_score=bool(high))
        item['gate_status'] = ('unscored' if score is None else
                               'both' if rising and high else 'rise_only' if rising else
                               'score_only' if high else 'below')
    candidates = [p for p in pixels if p['eligible']]
    points = []
    for rank, item in enumerate(candidates[:top], 1):
        point = dict(item)
        point.update(rank=rank, region=geography.region(item['latitude'], item['longitude']),
                     adjacent8_median_rise_K=None, rise_over_adjacent8_K=None,
                     previous_pixel_line_time_interpolated_kst=previous.line_time(item['native_row_1based']),
                     current_pixel_line_time_interpolated_kst=current.line_time(item['native_row_1based']))
        points.append(point)
    result.update(all_pixels=pixels, top_pixels=points, candidate_pixels=len(candidates),
                  eligible_rising_pixels=len(candidates),
                  ranking='descending adjusted score, then native row and column',
                  score_rank_definition='1 + number of land pixels with strictly higher adjusted score')
    result['gate_counts'] = {status:sum(p['gate_status']==status for p in pixels)
                            for status in ('rise_only','score_only','both','below','unscored')}
    result['scoring'].update(base_formula=result['scoring'].get('formula'),
                            formula='clip(base_score + rank_bonus - cluster_penalty, 0, 100)',
                            rank_bonus_max=10, rank_bonus_full_ratio=16, rank_bonus_score_scale=20,
                            cluster_window=11, cluster_min_neighbors=48, cluster_min_support=8,
                            cluster_min_fraction=.1, cluster_full_fraction=.25,
                            cluster_score_floor=5, cluster_relative_score=.5,
                            cluster_max_penalty_fraction=.4,
                            rank_bonus_pixels=sum(p['rank_bonus']>0 for p in pixels),
                            cluster_penalty_pixels=sum(p['cluster_penalty']>0 for p in pixels))
    return result


def apply_rank_cluster(result, previous, current, geography, top, output):
    """Rebuild exact predecessor base ranks; reference failure disables only bonuses."""
    from pathlib import Path
    from hsd import STEP
    from source import Source
    from core import compare_frames
    from contrast import reference_arrays, apply_contrast
    output = Path(output)
    previous_result = {'all_pixels': []}
    status, error, reference_errors = 'ok', None, []
    try:
        source = Source(output/'cache', int(current.satellite.split('-')[-1]), allow_quality_flags=True)
        older = source.frame(previous.nominal-STEP)
        prior = compare_frames(older, previous, geography, top, allow_quality_flags=True)
        references = reference_arrays(previous, geography, output/'reference_cache', allow_quality_flags=True)
        previous_result = apply_contrast(prior, older, previous, geography, references, top)
        reference_errors = previous_result['scoring']['reference_errors']
        if reference_errors or any(p['joint_score'] is None for p in previous_result['all_pixels']):
            status = 'partial_reference'
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        status, error = 'unavailable', str(exc)
    result = finalize(result, previous_result, previous, current, geography, top)
    result['scoring'].update(previous_rank_nominal_utc=previous.nominal.isoformat(),
                             previous_rank_status=status, previous_rank_error=error,
                             previous_rank_reference_errors=reference_errors,
                             previous_rank_missing_pixels=sum(p['previous_base_rank'] is None for p in result['all_pixels']))
    return result
