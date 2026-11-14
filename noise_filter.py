"""Causal rolling noise decisions; scores and ranks are never rewritten."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np

VERSION = 'rolling20-reversal-noise-v1'
WINDOW_SECONDS = 1200
SAMPLES = 8  # (t-20 min, t] on the native 150-second cadence
CONTEXT = 10  # one additional difference at the left boundary
EXPORT_FIELDS = ['eligible_before_noise', 'is_noise', 'noise_status', 'noise_mean',
                 'noise_rise_sum', 'noise_fall_sum', 'noise_canceled_fall']


def analyze_window(scores):
    """Ten consecutive raw scores, oldest first, shape (10, pixels).

    Difference endpoints select membership in (t-20min,t]. The outgoing
    positive difference cancels only the immediately following equal negative
    difference (atol=1e-6 points, rtol=1e-9). Scores remain in the mean.
    """
    scores = np.asarray(scores, dtype=float)
    if scores.ndim == 1:
        scores = scores[:, None]
    if scores.ndim != 2 or scores.shape[0] != CONTEXT:
        raise ValueError('Expected ten consecutive 150-second score vectors')
    valid = np.isfinite(scores).all(axis=0)
    safe = np.where(np.isfinite(scores), scores, 0.)
    values = safe[-SAMPLES:]
    delta = np.diff(safe, axis=0)
    changes = delta[1:].copy()
    paired = ((delta[0] > 0) & (delta[1] < 0) &
              np.isclose(delta[0], -delta[1], atol=1e-6, rtol=1e-9))
    canceled = np.where(paired, -changes[0], 0.)
    changes[0, paired] = 0.
    mean = values.mean(axis=0)
    rise = np.maximum(changes, 0.).sum(axis=0)
    fall = np.maximum(-changes, 0.).sum(axis=0)
    qualifies = valid & (mean >= 5) & (mean <= 40) & (fall >= .8*rise)
    marks = qualifies[None, :] & (values >= 5) & (values <= 2.5*mean)
    return dict(valid=valid, mean=mean, rise=rise, fall=fall,
                canceled=canceled, qualifies=qualifies, marks=marks)


def raw_snapshot(stamp, geography, top, output, satellite, allow_unloading=False):
    from source import Source
    from hsd import STEP
    from core import compare_frames
    from contrast import reference_arrays, apply_contrast
    from rank_adjustment import apply_rank_cluster
    output = Path(output)
    source = Source(output/'cache', int(satellite.split('-')[-1]), allow_unloading=allow_unloading, allow_quality_flags=True)
    previous, current = source.frame(stamp-STEP), source.frame(stamp)
    result = compare_frames(previous, current, geography, top, allow_unloading=allow_unloading, allow_quality_flags=True)
    result = apply_contrast(result, previous, current, geography,
                            reference_arrays(current, geography, output/'reference_cache', allow_quality_flags=True), top)
    return apply_rank_cluster(result, previous, current, geography, top, output)
