#!/usr/bin/env python3
"""One-time build of a small offline Korea subset from NOAA 1991-2020 normals.

Builder only: pip install netCDF4. Runtime needs only numpy.
Download explicitly with --download; normal monitor execution never calls this tool.
"""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import numpy as np

SOURCE_URL = ('https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis.derived/'
              'surface_gauss/skt.sfc.4Xday.ltm.1991-2020.nc')


LAND_URL = "https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis/surface_gauss/land.sfc.gauss.nc"


def build(source, output, land_mask):
    from netCDF4 import Dataset, num2date
    with Dataset(source) as z:
        lat = np.asarray(z['lat'][:], float)
        lon = np.asarray(z['lon'][:], float)
        ilat = np.where((lat >= 27) & (lat <= 45))[0]
        ilon = np.where((lon >= 118) & (lon <= 141))[0]
        times = num2date(z['time'][:], z['time'].units)
        expected = [(d, h) for d in range(1, 366) for h in (0, 6, 12, 18)]
        got = [(t.dayofyr, t.hour) for t in times]
        if got != expected or '1991' not in z['time'].climo_period or '2020' not in z['time'].climo_period:
            raise ValueError('Source is not complete 1991-2020 daily/6-hour normals')
        if z['skt'].units not in ('degK', 'K'):
            raise ValueError('Expected Kelvin skin temperature')
        values = np.ma.filled(z['skt'][:, ilat, ilon], np.nan).astype('f4')
        years = np.asarray(z['valid_yr_count'][:, ilat, ilon])
        if not np.isfinite(values).all() or np.any(years < 25):
            raise ValueError('Climatology has missing/insufficient-year cells')
        order = np.argsort(lat[ilat])
        values = values[:, order, :].reshape(365, 4, len(ilat), len(ilon))
        metadata = {'format':'deflamma-skin-normals-v1', 'variable':'surface_skin_temperature',
                    'period':'1991-2020', 'calendar':'365_day', 'timezone':'UTC',
                    'hours_UTC':[0,6,12,18], 'source':'NOAA PSL NCEP/NCAR Reanalysis 1',
                    'source_url':SOURCE_URL, 'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                    'spatial_resolution':'Gaussian latitude ~1.9 degrees; longitude 1.875 degrees',
                    'interpolation':'bilinear in space; linear in 6-hour time; cyclic year; Feb29 same-hour Feb28/Mar1 mean',
                    'minimum_source_years':int(years.min()),
                    'limitation':'Coarse skin-temperature proxy; not 2m air temperature or clear-sky B07 brightness-temperature normals. No terrain correction.'}
    with Dataset(land_mask) as z:
        if not np.allclose(z['lat'][:],lat) or not np.allclose(z['lon'][:],lon):
            raise ValueError('Land mask and climatology grids differ')
        land = np.asarray(z['land'][0, ilat, ilon])[order] >= .5
    metadata.update(land_mask_url=LAND_URL, land_mask_sha256=hashlib.sha256(Path(land_mask).read_bytes()).hexdigest(),
                    land_interpolation='bilinear weights renormalized on land only; nearest land within 3 angular degrees when no land corners')
    output = Path(output); output.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(output, lat=lat[ilat][order], lon=lon[ilon], skin_K=values, land=land,
                        metadata=json.dumps(metadata, ensure_ascii=False))
    output.with_suffix('.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{output}: {output.stat().st_size} bytes; shape={values.shape}')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path('skt.sfc.4Xday.ltm.1991-2020.nc'))
    p.add_argument('--download', action='store_true')
    p.add_argument('--land-mask', type=Path, default=Path('land.sfc.gauss.nc'))
    p.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'data/climatology/skin_normals_1991_2020.npz')
    a = p.parse_args()
    if a.download:
        a.source.parent.mkdir(parents=True, exist_ok=True)
        temporary = a.source.with_suffix('.part')
        with urllib.request.urlopen(SOURCE_URL, timeout=60) as r, temporary.open('wb') as f:
            while True:
                chunk = r.read(1024*1024)
                if not chunk: break
                f.write(chunk)
        temporary.replace(a.source)
        a.land_mask.parent.mkdir(parents=True,exist_ok=True)
        with urllib.request.urlopen(LAND_URL,timeout=60) as r:
            a.land_mask.write_bytes(r.read())
    build(a.source, a.output, a.land_mask)
