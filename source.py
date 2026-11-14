"""Anonymous NOAA S3 access, cache and Japan-area timeline discovery."""
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
import logging
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from hsd import NAME, STEP, UTC, nominal_from_name, read_frame, ObservationQualityError

LOG = logging.getLogger(__name__)
NS = {'s':'http://s3.amazonaws.com/doc/2006-03-01/'}


def key_for(stamp, satellite=9):
    stamp = stamp.astimezone(UTC)
    if stamp.microsecond or int(stamp.timestamp()) % 150:
        raise ValueError('Timestamp is not on a 2.5-minute timeline')
    base = stamp.replace(minute=stamp.minute//10*10, second=0, microsecond=0)
    number = int((stamp-base).total_seconds()//150)+1
    return base.strftime('AHI-L1b-Japan/%Y/%m/%d/%H%M/') + (
        f'HS_H{satellite:02}_{base:%Y%m%d_%H%M}_B07_JP{number:02}_R20_S0101.DAT.bz2')


def get_bytes(url, timeout=25):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent':'HimawariRiseMonitor/1.0'})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise
        time.sleep(2**attempt)


class Source:
    def __init__(self, cache_dir, satellite=9, allow_unloading=False, allow_quality_flags=False):
        self.allow_unloading = allow_unloading
        self.allow_quality_flags = allow_quality_flags
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.satellite = satellite
        self.base = f'https://noaa-himawari{satellite}.s3.amazonaws.com'

    def url(self, stamp):
        return self.base + '/' + key_for(stamp, self.satellite)

    def available(self, start, end):
        # At most a few hour prefixes, rather than listing the whole archive.
        hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        found = set()
        while hour <= end:
            prefix = hour.strftime('AHI-L1b-Japan/%Y/%m/%d/%H')
            token = None
            while True:
                params = {'list-type':'2', 'prefix':prefix}
                if token:
                    params['continuation-token'] = token
                root = ET.fromstring(get_bytes(self.base + '/?' + urllib.parse.urlencode(params)))
                for key in root.findall('s:Contents/s:Key', NS):
                    name = Path(key.text).name
                    match = NAME.fullmatch(name)
                    if not match or int(match[1]) != self.satellite:
                        continue
                    stamp = nominal_from_name(name)
                    if start <= stamp <= end:
                        found.add(stamp)
                token = root.findtext('s:NextContinuationToken', namespaces=NS)
                if not token:
                    break
            hour += timedelta(hours=1)
        return found

    @lru_cache(maxsize=4)
    def frame(self, stamp):
        path = self.cache / Path(key_for(stamp, self.satellite)).name
        if path.exists():
            try:
                return read_frame(path,allow_unloading=self.allow_unloading,allow_quality_flags=self.allow_quality_flags)
            except ObservationQualityError:
                raise  # Keep the valid flagged source; downloading it again cannot fix its flags.
            except (ValueError, OSError, EOFError, struct.error):
                LOG.warning('Invalid cached HSD, downloading again: %s', path.name)
                path.unlink()
        blob = get_bytes(self.url(stamp))
        temporary = path.with_name(path.name + '.part')
        try:
            temporary.write_bytes(blob)
            # Keep the canonical filename for the reader while validating bytes.
            os.replace(temporary, path)
            try:
                return read_frame(path,allow_unloading=self.allow_unloading,allow_quality_flags=self.allow_quality_flags)
            except ObservationQualityError:
                raise
            except Exception:
                path.unlink(missing_ok=True)
                raise
        finally:
            temporary.unlink(missing_ok=True)

    def prune_cache(self, now, keep_hours=6):
        cutoff = now-timedelta(hours=keep_hours)
        for path in self.cache.glob('HS_H*_B07_JP*_R20_S0101.DAT.bz2'):
            if NAME.fullmatch(path.name) and nominal_from_name(path.name) < cutoff:
                path.unlink(missing_ok=True)


def ready_pairs(available, processed, start, cutoff):
    """Missing exact predecessor stays pending; never compare across a gap."""
    return [t for t in sorted(available) if t >= max(start, cutoff)
            and t not in processed and t-STEP in available]
