"""Strict, small AHI B07 Japan-area HSD reader (JMA HSD v1.3)."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import bz2
import hashlib
import re
import struct
import numpy as np

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
STEP = timedelta(seconds=150)
NAME = re.compile(r'HS_H(08|09)_(\d{8})_(\d{4})_B07_JP(0[1-4])_R20_S0101\.DAT\.bz2$')


class ObservationQualityError(ValueError):
    """Valid source file whose operation flags disallow normal analysis."""


def nominal_from_name(name):
    match = NAME.fullmatch(Path(name).name)
    if not match:
        raise ValueError(f'Not an AHI B07 Japan-area filename: {Path(name).name}')
    stamp = datetime.strptime(match[2] + match[3], '%Y%m%d%H%M').replace(tzinfo=UTC)
    if stamp.minute % 10:
        raise ValueError('Invalid HSD base timeline')
    return stamp + (int(match[4]) - 1) * STEP


@dataclass
class Frame:
    name: str
    nominal: datetime
    satellite: str
    bt: np.ndarray
    projection: tuple
    first_line: int
    sha256: str
    quality_flags: list
    line_times: list
    observation_start_mjd: float
    observation_end_mjd: float
    wavelength_um: float

    @property
    def grid(self):
        return self.satellite, self.bt.shape, self.first_line, self.projection

    def line_time(self, row):
        if not self.line_times or not self.line_times[0][0] <= row <= self.line_times[-1][0]:
            return None
        mjd = float(np.interp(row, [x[0] for x in self.line_times], [x[1] for x in self.line_times]))
        return (datetime(1858, 11, 17, tzinfo=UTC) + timedelta(days=mjd)).astimezone(KST).isoformat()

    def metadata(self):
        return {'file': self.name, 'nominal_utc': self.nominal.isoformat(),
                'nominal_kst': self.nominal.astimezone(KST).isoformat(),
                'sha256': self.sha256, 'quality_flags': self.quality_flags,
                'wavelength_um': self.wavelength_um,
                'observation_start_mjd': self.observation_start_mjd,
                'observation_end_mjd': self.observation_end_mjd}


def read_frame(path, allow_unloading=False, allow_quality_flags=False):
    path = Path(path)
    nominal = nominal_from_name(path.name)
    blob = path.read_bytes()
    raw = bz2.decompress(blob)  # validates the bzip2 CRC
    if len(raw) < 282 or raw[0] != 1 or raw[5] not in (0, 1):
        raise ValueError('Invalid HSD basic header')
    endian = '<' if raw[5] == 0 else '>'
    def unpack(fmt, offset):
        return struct.unpack_from(endian + fmt, raw, offset)
    header_length, data_length = unpack('II', 70)
    if len(raw) != header_length + data_length:
        raise ValueError('HSD payload is truncated or inconsistent')
    satellite = raw[6:22].decode('ascii').strip(' \0')
    expected_sat = 'Himawari-' + str(int(NAME.fullmatch(path.name)[1]))
    if satellite != expected_sat:
        raise ValueError('Filename satellite and HSD satellite disagree')
    if raw[38:42].decode('ascii') != 'JP'+NAME.fullmatch(path.name)[4]:
        raise ValueError('Filename observation number and HSD area disagree')
    flags = list(raw[78:82])
    if flags[0] != 0 and not allow_quality_flags and not (allow_unloading and flags[0] == 4):
        raise ObservationQualityError(f'HSD operation quality flag is nonzero: {flags[0]}')
    blocks, offset = {}, 0
    for _ in range(unpack('H', 3)[0]):
        number = raw[offset]
        length = unpack('I' if number == 10 else 'H', offset + 1)[0]
        if length < 3 or offset + length > header_length or number in blocks:
            raise ValueError('Invalid HSD block structure')
        blocks[number] = offset
        offset += length
    if offset != header_length or not all(i in blocks for i in range(1, 12)):
        raise ValueError('Incomplete HSD header')
    b2, b3, b5, b7, b8, b9 = [blocks[i] for i in (2, 3, 5, 7, 8, 9)]
    bits, cols, rows = unpack('HHH', b2 + 3)
    if (bits, cols, rows, raw[b2 + 9]) != (16, 1500, 1200, 0):
        raise ValueError('Unsupported Japan-area dimensions or internal compression')
    total, sequence, first_line = unpack('BBH', b7 + 3)
    if (total, sequence, first_line) != (1, 1, 1) or data_length != rows * cols * 2:
        raise ValueError('Unsupported segment layout')
    band, wavelength, valid_bits, error, outside = unpack('HdHHH', b5 + 3)
    if band != 7 or valid_bits != 14:
        raise ValueError('Expected 14-bit B07 data')
    gain, intercept, c0, c1, c2, _, _, _, light, planck, boltzmann = unpack('11d', b5 + 19)
    counts = np.frombuffer(raw, dtype=endian + 'u2', count=rows * cols,
                           offset=header_length).reshape(rows, cols)
    radiance = counts.astype(float) * gain + intercept
    valid = (counts != error) & (counts != outside) & (counts < 2**valid_bits) & (radiance > 0)
    wavelength_m = wavelength * 1e-6
    with np.errstate(invalid='ignore', divide='ignore', over='ignore'):
        te = planck * light / (boltzmann * wavelength_m * np.log1p(
            2 * planck * light**2 / (wavelength_m**5 * radiance * 1e6)))
        bt = c0 + c1 * te + c2 * te**2
    bt[~valid] = np.nan
    projection = unpack('dIIffddd', b3 + 3)
    rotation = unpack('d', b8 + 11)[0]
    corrections = [unpack('Hff', b8 + 21 + 10*i) for i in range(unpack('H', b8 + 19)[0])]
    if rotation != 0 or any(dx != 0 or dy != 0 for _, dx, dy in corrections):
        raise ValueError('Nonzero navigation correction: precise registration is required; pair rejected')
    line_times = [unpack('Hd', b9 + 5 + 10*i) for i in range(unpack('H', b9 + 3)[0])]
    if not line_times or any(a[0] >= b[0] for a, b in zip(line_times, line_times[1:])):
        raise ValueError('Invalid line time information')
    start, end = unpack('dd', 46)
    epoch = datetime(1858, 11, 17, tzinfo=UTC)
    actual_start, actual_end = epoch+timedelta(days=start), epoch+timedelta(days=end)
    if not (nominal <= actual_start <= actual_end <= nominal+timedelta(minutes=3)):
        raise ValueError('HSD observation interval disagrees with the nominal file time')
    return Frame(path.name, nominal, satellite, bt.astype('f4'), projection, first_line,
                 hashlib.sha256(blob).hexdigest(), flags, line_times, start, end, wavelength)
