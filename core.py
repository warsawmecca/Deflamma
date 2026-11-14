"""Same-native-grid population percentile/deviation scoring and candidate ranking."""
from pathlib import Path
import json
import hashlib
import numpy as np
from pyproj import CRS, Transformer
import shapely
from shapely.geometry import Point, shape
from hsd import KST, STEP
from scoring import population_scores

ALGORITHM_VERSION = 'b07-percentile-harmonic-deviation-v2'


def grid_metadata(frame):
    grid={'satellite':frame.satellite,'shape':list(frame.bt.shape),
          'first_line':frame.first_line,'projection':list(frame.projection)}
    signature=hashlib.sha256(json.dumps(grid,sort_keys=True).encode()).hexdigest()
    return grid,signature
REGIONS = {'Gangwon':'강원','Gyeonggi':'경기','North Chungcheong':'충북','South Chungcheong':'충남',
 'North Jeolla':'전북','South Jeolla':'전남','North Gyeongsang':'경북','South Gyeongsang':'경남',
 'Seoul':'서울','Incheon':'인천','Daejeon':'대전','Daegu':'대구','Ulsan':'울산','Busan':'부산',
 'Gwangju':'광주','Jeju':'제주','Sejong':'세종'}


class Geography:
    def __init__(self, frame, boundary_file):
        self.grid = frame.grid
        self.features = json.loads(Path(boundary_file).read_text(encoding='utf-8'))['features']
        self.geometries = [shape(f['geometry']) for f in self.features]
        country = shapely.union_all(self.geometries)
        lon0, cf, lf, co, lo, rs, a, b = frame.projection
        height = (rs-a)*1000
        crs = CRS.from_proj4(f'+proj=geos +lon_0={lon0} +h={height} +a={a*1000} +b={b*1000} +sweep=y +units=m')
        inverse = Transformer.from_crs(crs, 4326, always_xy=True)
        self.inverse, self.height = inverse, height
        self.projection = frame.projection
        self.first_line = frame.first_line
        columns, rows = np.meshgrid(np.arange(frame.bt.shape[1])+1,
                                    np.arange(frame.bt.shape[0])+frame.first_line)
        x = np.deg2rad((columns-co)*65536/cf)*height
        y = -np.deg2rad((rows-lo)*65536/lf)*height
        self.lon, self.lat = inverse.transform(x, y)
        self.mask = shapely.contains_xy(country, self.lon, self.lat)

    def pixel_corners(self, valid):
        """Native pixel footprints, with four transformed half-grid corners."""
        rows, columns = np.nonzero(valid)
        lon0, cf, lf, co, lo, *_ = self.projection
        corners = []
        for dc,dr in ((-.5,-.5),(.5,-.5),(.5,.5),(-.5,.5)):
            x=np.deg2rad((columns+1+dc-co)*65536/cf)*self.height
            y=-np.deg2rad((rows+self.first_line+dr-lo)*65536/lf)*self.height
            lon,lat=self.inverse.transform(x,y)
            corners.append(np.column_stack((lon,lat)))
        return np.stack(corners,axis=1)

    def region(self, lat, lon):
        point = Point(lon, lat)
        for feature, geometry in zip(self.features, self.geometries):
            if geometry.covers(point):
                name = feature['properties'].get('shapeName', '')
                return REGIONS.get(name, name)
        return ''


def ranked_indices(previous, current, mask, top=10, min_rise_k=0.0, min_current_k=None):
    if previous.shape != current.shape or mask.shape != current.shape:
        raise ValueError('Array/mask shapes differ')
    valid = mask & np.isfinite(previous) & np.isfinite(current)
    rise = current.astype('f8') - previous.astype('f8')
    eligible = valid & (rise > min_rise_k)
    if min_current_k is not None:
        eligible &= current >= min_current_k
    indices = np.argwhere(eligible)
    ordering = np.lexsort((-current[eligible], -rise[eligible]))
    return indices[ordering[:top]], rise, valid, eligible


def compare_frames(previous, current, geography, top=10, min_rise_k=0.0, min_current_k=None,
                   temperature_weight=.5, deviation_strength=.5, allow_unloading=False, allow_quality_flags=False):
    if current.nominal - previous.nominal != STEP:
        raise ValueError('Only exact adjacent 150-second nominal observations may be compared')
    if previous.grid != current.grid or current.grid != geography.grid:
        raise ValueError('Satellite/projection/shape differs; no silent resampling is allowed')
    for frame in (previous,current):
        flag=frame.quality_flags[0]
        if flag and not allow_quality_flags and not (allow_unloading and flag==4):
            raise ValueError(f'Operation quality flag {flag}; unloading replay requires explicit opt-in')
    indices, rise, valid, eligible = ranked_indices(previous.bt, current.bt, geography.mask,
                                                   top, min_rise_k, min_current_k)
    if not valid.any():
        raise ValueError('No valid land pixels in both observations')
    rows,cols=np.nonzero(valid)
    temperature=current.bt[valid].astype('f8')
    delta=rise[valid]
    fields=population_scores(temperature,delta,temperature_weight,deviation_strength)
    statistics=fields.pop('population_statistics')
    score=fields['joint_score']
    order=np.lexsort((cols,rows,-temperature,-delta,-score))
    sorted_scores=np.sort(score)
    ranks=len(score)-np.searchsorted(sorted_scores,score,side='right')+1
    all_pixels=[]
    by_cell={}
    for i in order:
        r,c=int(rows[i]),int(cols[i]);row=r+current.first_line
        item={'id':f'P{row:04}C{c+1:04}',
              'score_rank':int(ranks[i]),'eligible':bool(eligible[r,c]),
              'latitude':float(geography.lat[r,c]),'longitude':float(geography.lon[r,c]),
              'native_row_1based':row,'native_column_1based':c+1,
              'previous_K':float(previous.bt[r,c]),'current_K':float(temperature[i]),'rise_K':float(delta[i]),
              'temperature_minus_mean_K':float(temperature[i]-statistics['temperature']['mean_K']),
              'rise_minus_mean_K':float(delta[i]-statistics['rise']['mean_K'])}
        item.update({name:float(values[i]) for name,values in fields.items()})
        all_pixels.append(item);by_cell[(r,c)]=item
    indices=[(int(rows[i]),int(cols[i])) for i in order if eligible[rows[i],cols[i]]][:top]
    results = []
    for rank, (r, c) in enumerate(indices, 1):
        r, c = int(r), int(c)
        lat, lon = float(geography.lat[r, c]), float(geography.lon[r, c])
        row = r + current.first_line
        neighbor_values = []
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r+dr, c+dc
                if (dr or dc) and 0 <= nr < rise.shape[0] and 0 <= nc < rise.shape[1]:
                    if np.isfinite(previous.bt[nr, nc]) and np.isfinite(current.bt[nr, nc]):
                        neighbor_values.append(float(rise[nr, nc]))
        median = float(np.median(neighbor_values)) if neighbor_values else None
        item={**by_cell[(r,c)],'rank':rank,'region':geography.region(lat,lon),
              'adjacent8_median_rise_K':median,
              'rise_over_adjacent8_K':float(rise[r,c])-median if median is not None else None,
              'previous_pixel_line_time_interpolated_kst':previous.line_time(row),
              'current_pixel_line_time_interpolated_kst':current.line_time(row)}
        results.append(item)
    grid,grid_signature=grid_metadata(current)
    return {'algorithm':ALGORITHM_VERSION, 'status':'ok', 'is_fire_detection':False,
            'grid':grid,'grid_signature':grid_signature,
            'previous_nominal_utc':previous.nominal.isoformat(),
            'current_nominal_utc':current.nominal.isoformat(),
            'previous_nominal_kst':previous.nominal.astimezone(KST).isoformat(),
            'current_nominal_kst':current.nominal.astimezone(KST).isoformat(),
            'nominal_interval_seconds':150, 'land_pixels':int(geography.mask.sum()),
            'valid_pair_pixels':int(valid.sum()), 'eligible_rising_pixels':int(eligible.sum()),
            'requested_top':top, 'filters':{'rise_strictly_greater_than_K':min_rise_k,
                                           'current_at_least_K':min_current_k},
            'previous_frame':previous.metadata(), 'current_frame':current.metadata(),
            'scoring':{'metric':ALGORITHM_VERSION,'temperature_weight':temperature_weight,'deviation_strength':deviation_strength,
                       'formula':'S0=100/(w/pT+(1-w)/pD); S=S0*(1+lambda*sqrt(max(zT,0)*max(zD,0))); rise<=0 => 0',
                       'percentile_definition':'(count_less + 0.5*count_equal)/N; reference is all valid paired land pixels',
                       'population_statistics':statistics,'is_probability':False},
            'quality_assessment':{'diagnostic_only':any(f.quality_flags[0]!=0 for f in (previous,current)) and not allow_quality_flags,
                                  'quality_flags_included':bool(allow_quality_flags),
                                  'operation_flags':[previous.quality_flags[0],current.quality_flags[0]],
                                  'warning':('Quality-flagged observations included by user policy; inspect recorded flags' if allow_quality_flags else 'Unloading flag 4: possible image navigation degradation') if any(f.quality_flags[0] for f in (previous,current)) else None},
            'ranking':'descending joint score, rise, current brightness temperature, then ascending native row/column',
            'score_rank_definition':'1 + count of valid pixels with strictly higher score; zero-score ties are not eligible',
            'all_pixels':all_pixels,
            'top_pixels':results}
