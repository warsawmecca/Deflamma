"""Bounded four-input thermal contrast score and seven-day reference acquisition."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
import warnings

import numpy as np
import shapely
from shapely.geometry import shape

from scoring import midpoint_percentiles
from climatology import get_model, normals_for_geography, reference_allowed, COLD_LIMIT_K

VERSION = 'b07-four-input-contrast-rankcluster-normal0-integral10-v8'
FORMULA = '100*A^0.1*(b+/(b++5K))^0.4*(c+/(c++5K))^0.4*sigmoid((T-300K)/10K)^0.1'
OFFSETS = [(i, j) for i in (-1, 0, 1) for j in (-1, 0, 1) if i or j]


def setup(geography):
    if hasattr(geography, 'contrast_rows'):
        return
    r, c = np.nonzero(geography.mask)
    rr = np.array([r+i for i, _ in OFFSETS])
    cc = np.array([c+j for _, j in OFFSETS])
    inside = (rr >= 0) & (rr < geography.mask.shape[0]) & (cc >= 0) & (cc < geography.mask.shape[1])
    rr = np.clip(rr, 0, geography.mask.shape[0]-1)
    cc = np.clip(cc, 0, geography.mask.shape[1]-1)
    path = Path(__file__).parent/'data/ne_10m_land.geojson'
    blob = path.read_bytes()
    polygons = shapely.union_all([shape(f['geometry']) for f in json.loads(blob)['features']])
    land = inside & shapely.intersects_xy(polygons, geography.lon[rr, cc], geography.lat[rr, cc])
    geography.contrast_rows = r, c, rr, cc, land
    signature = repr(geography.grid).encode()+geography.mask.tobytes()+hashlib.sha256(blob).digest()
    geography.contrast_signature = hashlib.sha256(signature).hexdigest()


def observations(frame, geography):
    setup(geography)
    if frame.grid != geography.grid:
        raise ValueError('Historical satellite/navigation grid differs')
    r, c, rr, cc, land = geography.contrast_rows
    neighbors = np.where(land, frame.bt[rr, cc], np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        background = np.nanmedian(neighbors, axis=0)
    return frame.bt[r, c].astype(float), frame.bt[r, c]-background, np.isfinite(neighbors).sum(axis=0)



def cache_current_reference(current, geography, cache):
    """Save current compact T/b once, allowing next-week reuse without raw HSD."""
    from integral_filter import atomic_npz
    setup(geography);cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    signature=hashlib.sha256((geography.contrast_signature+'|flags-included-v1').encode()).hexdigest()
    T,b,_=observations(current,geography)
    atomic_npz(cache/(current.nominal.strftime('%Y%m%dT%H%M%S')+'_'+signature[:16]+'.npz'),signature=signature,temperature=np.asarray(T,np.float32),background_contrast=np.asarray(b,np.float32))

def reference_arrays(current, geography, cache, allow_quality_flags=False):
    """Cache small calibrated reference vectors and discard downloaded raw reference files."""
    from source import Source
    setup(geography)
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256((geography.contrast_signature+'|flags-included-v1').encode()).hexdigest() if allow_quality_flags else geography.contrast_signature
    def one(day):
        stamp = current.nominal-timedelta(days=day)
        path = cache/(stamp.strftime('%Y%m%dT%H%M%S')+'_'+signature[:16]+'.npz')
        try:
            if path.exists():
                with np.load(path, allow_pickle=False) as saved:
                    if saved['signature'].item() == signature:
                        return saved['temperature'], saved['background_contrast'], None
        except (OSError, ValueError, KeyError, EOFError):
            path.unlink(missing_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='deflamma-reference-') as raw:
                source = Source(raw, int(current.satellite.split('-')[-1]), **({'allow_quality_flags': True} if allow_quality_flags else {}))
                frame = source.frame(stamp)
                temperature, local, _ = observations(frame, geography)
                if frame.quality_flags[0] != 0 and not allow_quality_flags:
                    raise ValueError('Historical operation quality flag is nonzero')
                with tempfile.NamedTemporaryFile(dir=cache, suffix='.npz', delete=False) as tmp:
                    temporary = Path(tmp.name)
                try:
                    np.savez_compressed(temporary, signature=signature, temperature=temperature,
                                        background_contrast=local)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                return temperature, local, None
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            missing = np.full(len(geography.contrast_rows[0]), np.nan)
            return missing, missing, {'nominal_utc': stamp.isoformat(), 'error': str(exc)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(one, range(1, 8)))
    # Bound compact reference cache independently of observation time and live raw-file pruning.
    files = sorted(cache.glob('*.npz'), key=lambda p: p.stat().st_mtime)
    for path in files[:-4096]:
        path.unlink(missing_ok=True)
    return values


def score_inputs(temperature, local, excess):
    """Temperature percentiles use all currently valid land centers."""
    a = np.full(len(temperature), np.nan)
    valid = np.isfinite(temperature)
    if valid.any():
        a[valid] = midpoint_percentiles(temperature[valid])
    b = np.maximum(local, 0)
    c = np.maximum(excess, 0)
    b = b/(b+5)
    c = c/(c+5)
    d = 1/(1+np.exp(-np.clip((temperature-300)/10, -700, 700)))
    score = 100*a**.1*b**.4*c**.4*d**.1
    return a, b, c, d, score


def candidate_conditions(score, rise, temperature):
    """Finite data and independent inclusive score/rise branches; no temperature floor."""
    score = np.asarray(score, dtype=float)
    rise = np.asarray(rise, dtype=float)
    temperature = np.asarray(temperature, dtype=float)
    valid = np.isfinite(score) & np.isfinite(temperature)
    rising = valid & (score >= 5) & np.isfinite(rise) & (rise >= .6)
    high = valid & (score >= 40)
    return rising, high, rising | high


def apply_contrast(result, previous, current, geography, references, top):
    """Keep the stable export fields used by maps, named watches and archive queries."""
    temperature, local, neighbors = observations(current, geography)
    old_temperature, _, _ = observations(previous, geography)
    if len(references) != 7:
        raise ValueError('Expected exactly seven chronological daily references')
    normals = np.array([normals_for_geography(current.nominal-timedelta(days=day), geography)
                        for day in range(1, 8)])
    history = np.array([np.where(reference_allowed(t, normals[day]), b, np.nan)
                        for day, (t, b, _) in enumerate(references)])
    current_normal = normals_for_geography(current.nominal, geography)
    floors = normals-COLD_LIMIT_K
    days = np.isfinite(history).sum(axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        background = np.nanmedian(history, axis=0)
    excess = np.where(days >= 4, local-background, np.nan)
    a, b, c, d, score = score_inputs(temperature, local, excess)
    rising, high, eligible = candidate_conditions(score, temperature-old_temperature, temperature)
    ordered = np.sort(score[np.isfinite(score)])
    ranks = len(ordered)-np.searchsorted(ordered, score, side='right')+1
    rows, cols, _, _, land = geography.contrast_rows
    indices = {(int(r+current.first_line), int(c+1)): i for i, (r, c) in enumerate(zip(rows, cols))}
    def number(value):
        return float(value) if np.isfinite(value) else None
    for item in result['all_pixels']:
        i = indices[item['native_row_1based'], item['native_column_1based']]
        item.update(joint_score=number(score[i]), score_rank=int(ranks[i]) if np.isfinite(score[i]) else None,
                    eligible=bool(eligible[i]), condition_rise=bool(rising[i]),
                    condition_high_score=bool(high[i]),
                    temperature_pass=bool(np.isfinite(temperature[i])),
                    gate_status=('unscored' if not np.isfinite(score[i]) else
                                 'both' if rising[i] and high[i] else
                                 'rise_only' if rising[i] else
                                 'score_only' if high[i] else 'below'),
                    temperature_percentile=number(100*a[i]),
                    local_excess_K=number(local[i]), past_excess_K=number(excess[i]),
                    history_days=int(days[i]), history_median_K=number(background[i]),
                    normal_skin_K=number(current_normal[i]),
                    history_temperature_floor_min_K=number(np.min(floors[:, i])),
                    history_temperature_floor_max_K=number(np.max(floors[:, i])),
                    land_neighbors=int(land[:, i].sum()), valid_neighbors=int(neighbors[i]),
                    component_a=number(a[i]), component_b=number(b[i]),
                    component_c=number(c[i]), component_d=number(d[i]),
                    score_status=('ok' if np.isfinite(score[i]) else
                                  'climatology_unavailable' if not np.isfinite(normals[:,i]).all() else
                                  'insufficient_reference_or_neighbors'))
    result['all_pixels'].sort(key=lambda p: (-(p['joint_score'] if p['joint_score'] is not None else -1),
                                             p['native_row_1based'], p['native_column_1based']))
    candidates = [item for item in result['all_pixels'] if item['eligible']]
    points = []
    for rank, item in enumerate(candidates[:top], 1):
        point = dict(item)
        point.update(rank=rank, region=geography.region(item['latitude'], item['longitude']),
                     adjacent8_median_rise_K=None, rise_over_adjacent8_K=None,
                     previous_pixel_line_time_interpolated_kst=previous.line_time(item['native_row_1based']),
                     current_pixel_line_time_interpolated_kst=current.line_time(item['native_row_1based']))
        points.append(point)
    result.update(top_pixels=points, eligible_rising_pixels=len(candidates), algorithm=VERSION,
                  ranking='descending bounded contrast score, then native row and column',
                  score_rank_definition='1 + number of scored land pixels with strictly higher score',
                  score_scale_max=100)
    result['scoring'].update(metric=VERSION, formula=FORMULA, weights=[.1,.4,.4,.1],
                             contrast_scale_K=5, temperature_center_K=300, temperature_scale_K=10,
                             missing_reference_pixels=int((days < 4).sum()),
                             reference_errors=[e for _, _, e in references if e],
                             reference_days=7, minimum_reference_days=4,
                             reference_filter='climatology-minus5-strict-v1',
                             climatology=get_model().identity(),
                             climatology_missing_pixels=int((~np.isfinite(normals).all(axis=0)).sum()),
                             score_range=[0,100], is_probability=False)
    result['filters'] = {'candidate_rule':'score-rise-or-high-v2-no-temperature-floor',
                         'current_at_least_K':None,
                         'score_with_rise_at_least':5, 'rise_at_least_K':.6,
                         'independent_score_at_least':40, 'consecutive_observations':1,
                         'historical_temperature_rule':'T_ref > normal_skin_K(date_ref,time_ref,location) - 5K',
                         'historical_max_cold_anomaly_K':COLD_LIMIT_K,
                         'historical_boundary':'strict', 'minimum_reference_days':4}
    available = result['all_pixels']
    result['gate_counts'] = {status:sum(p['gate_status']==status for p in available)
                             for status in ('rise_only','score_only','both','below','unscored')}
    result['candidate_pixels'] = len(candidates)
    return result
