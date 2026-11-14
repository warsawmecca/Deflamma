"""Offline calendar/diurnal skin-temperature normals. No network access at runtime."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import numpy as np

DEFAULT_MODEL = Path(__file__).parent/'data/climatology/skin_normals_1991_2020.npz'
COLD_LIMIT_K = 5.0


class SkinClimatology:
    def __init__(self, path):
        path = Path(path)
        try:
            blob = path.read_bytes()
            with np.load(path, allow_pickle=False) as z:
                self.lat = z['lat'].astype(float)
                self.lon = z['lon'].astype(float)
                self.skin = z['skin_K'].astype('f4')
                self.land = z['land'].astype(bool) if 'land' in z else np.ones(self.skin.shape[2:],bool)
                self.metadata = json.loads(z['metadata'].item())
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(f'Offline skin climatology unavailable: {path}: {exc}') from exc
        if (self.metadata.get('format') != 'deflamma-skin-normals-v1'
                or self.metadata.get('timezone') != 'UTC'
                or self.metadata.get('calendar') != '365_day'
                or self.lat.ndim != 1 or self.lon.ndim != 1
                or len(self.lat) < 2 or len(self.lon) < 2
                or not np.all(np.diff(self.lat) > 0) or not np.all(np.diff(self.lon) > 0)
                or self.skin.shape != (365, 4, len(self.lat), len(self.lon))
                or self.land.shape != (len(self.lat),len(self.lon)) or not self.land.any()
                or not np.isfinite(self.skin).all()
                or np.any((self.skin < 150) | (self.skin > 400))):
            raise ValueError('Invalid offline skin climatology grid/metadata/temperature')
        self.sha256 = hashlib.sha256(blob).hexdigest()

    def identity(self):
        return {**self.metadata, 'sha256': self.sha256,
                'cold_limit_K': COLD_LIMIT_K, 'comparison': 'strictly_greater_than_normal_minus_5K'}

    def spatial_weights(self, lat, lon):
        lat, lon = np.broadcast_arrays(np.asarray(lat, float), np.asarray(lon, float))
        inside = (np.isfinite(lat) & np.isfinite(lon) & (lat >= self.lat[0])
                  & (lat <= self.lat[-1]) & (lon >= self.lon[0]) & (lon <= self.lon[-1]))
        i = np.clip(np.searchsorted(self.lat, lat, side='right')-1, 0, len(self.lat)-2)
        j = np.clip(np.searchsorted(self.lon, lon, side='right')-1, 0, len(self.lon)-2)
        wy = (lat-self.lat[i])/(self.lat[i+1]-self.lat[i])
        wx = (lon-self.lon[j])/(self.lon[j+1]-self.lon[j])
        li, lj = np.nonzero(self.land)
        # Approximate angular distance, accounting for longitude convergence.
        dist = ((self.lat[li]-lat[...,None])**2
                + ((self.lon[lj]-lon[...,None])*np.cos(np.deg2rad(lat[...,None])))**2)
        dist = np.where(np.isfinite(dist), dist, np.inf)
        nearest = np.argmin(dist, axis=-1)
        ni, nj = li[nearest], lj[nearest]
        nearest_valid = np.take_along_axis(dist, np.asarray(nearest)[...,None],axis=-1)[...,0] <= 9
        return i, j, wy, wx, inside, ni, nj, nearest_valid

    def _at(self, day, slot, weights):
        i, j, wy, wx, inside, ni, nj, nearest_valid = weights
        a = self.skin[day % 365, slot, i, j].astype(float)
        b = self.skin[day % 365, slot, i, j+1].astype(float)
        c = self.skin[day % 365, slot, i+1, j].astype(float)
        d = self.skin[day % 365, slot, i+1, j+1].astype(float)
        w0=(1-wy)*(1-wx)*self.land[i,j]
        w1=(1-wy)*wx*self.land[i,j+1]
        w2=wy*(1-wx)*self.land[i+1,j]
        w3=wy*wx*self.land[i+1,j+1]
        total=w0+w1+w2+w3
        value=np.divide(w0*a+w1*b+w2*c+w3*d,total,out=np.full(np.shape(total),np.nan),where=total>0)
        fallback=self.skin[day % 365,slot,ni,nj].astype(float)
        value=np.where((total<=0)&nearest_valid,fallback,value)
        return np.where(inside,value,np.nan)

    def evaluate(self, stamp, lat=None, lon=None, weights=None):
        if stamp.tzinfo is None:
            raise ValueError('Climatology lookup needs a timezone-aware observation timestamp')
        stamp = stamp.astimezone(timezone.utc)
        if weights is None:
            weights = self.spatial_weights(lat, lon)
        hour = stamp.hour+stamp.minute/60+stamp.second/3600+stamp.microsecond/3.6e9
        slot = int(hour//6)
        fraction = (hour-slot*6)/6
        def on_day(day):
            return ((1-fraction)*self._at(day, slot, weights)
                    + fraction*self._at(day+(slot==3), (slot+1) % 4, weights))
        if stamp.month == 2 and stamp.day == 29:
            # Interpolate the SAME hour on Feb 28 and Mar 1, not a shifted noon.
            return .5*(on_day(58)+on_day(59))
        day = (datetime(2001, stamp.month, stamp.day)-datetime(2001, 1, 1)).days
        return on_day(day)


@lru_cache(maxsize=1)
def get_model():
    return SkinClimatology(DEFAULT_MODEL)


def normals_for_geography(stamp, geography):
    model = get_model()
    if getattr(geography, '_climatology_sha256', None) != model.sha256:
        rows, cols = geography.contrast_rows[:2]
        geography._climatology_weights = model.spatial_weights(geography.lat[rows, cols], geography.lon[rows, cols])
        geography._climatology_sha256 = model.sha256
    return model.evaluate(stamp, weights=geography._climatology_weights)


def reference_allowed(temperature, normal):
    temperature, normal = np.broadcast_arrays(np.asarray(temperature, float), np.asarray(normal, float))
    return np.isfinite(temperature) & np.isfinite(normal) & (temperature > normal-COLD_LIMIT_K)
