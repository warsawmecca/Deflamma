"""Bulk startup backfill: frozen two-hour anchor, bounded parallel I/O, causal EMA."""
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime
import json,logging,os,tempfile
from pathlib import Path
from hsd import STEP,UTC
from source import Source
from core import compare_frames
from contrast import apply_contrast,reference_arrays
from rank_adjustment import finalize
from integral_filter import IntegralStore
from std_trigger import StdStore, save_series

LOG=logging.getLogger('monitor')
SAMPLES=48
WORKERS=4

def bootstrap(engine,current,mode='live'):
    """Prepare [anchor-120min,anchor), atomically publish only completed state.

    Current/previous and one predecessor for base-rank bonus require51 raw
    observations including the anchor. No future/live observation is awaited.
    Only two nationwide dictionaries and four decoded frames are retained.
    """
    from monitor import write_json
    anchor=current.nominal;root=Path(engine.output);source=Source(root/'cache',int(current.satellite.split('-')[-1]),allow_quality_flags=True)
    report=dict(anchor_utc=anchor.isoformat(),start_utc=(anchor-SAMPLES*STEP).isoformat(),end_utc=(anchor-STEP).isoformat(),total=SAMPLES,completed=0,failed_samples=0,errors=[],phase='fetching',raw_total=SAMPLES+3,raw_completed=0)
    def progress():
        report['updated_utc']=datetime.now(UTC).isoformat()
        write_json(root/'bootstrap_status.json',report)
        write_json(root/'status.json',dict(mode=mode,phase='bootstrapping',data_healthy=False,checked_utc=report['updated_utc'],poll_seconds=30,bootstrap=report))
    LOG.info('Initial batch: %s to %s;48 past scores,4 download workers; no two-hour wait',report['start_utc'],report['end_utc']);progress()
    # Each worker touches a unique source timestamp. No concurrent writer shares
    # a .part filename; history references still use the existing two-worker pool.
    def fetch(stamp):
        f=source.frame(stamp)
        return stamp,f.sha256
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending={pool.submit(fetch,anchor-k*STEP):anchor-k*STEP for k in reversed(range(SAMPLES+3))}
        for task in as_completed(pending):
            stamp=pending[task]
            try:task.result()
            except (OSError,ValueError,RuntimeError,EOFError) as exc:
                if len(report['errors'])<16:report['errors'].append(dict(nominal_utc=stamp.isoformat(),stage='fetch',error=str(exc)))
            report['raw_completed']+=1
            if report['raw_completed']%8==0 or report['raw_completed']==report['raw_total']:progress()
    report['phase']='calculating';progress()
    with tempfile.TemporaryDirectory(prefix='.bootstrap_',dir=root) as tmp:
        store=IntegralStore(tmp);std=StdStore(tmp);prior={'all_pixels':[]};prior_stamp=None
        for k in reversed(range(1,SAMPLES+2)):
            stamp=anchor-k*STEP
            try:
                previous,frame=source.frame(stamp-STEP),source.frame(stamp)
                base=compare_frames(previous,frame,engine.geography,engine.top,allow_quality_flags=True)
                refs=reference_arrays(frame,engine.geography,root/'reference_cache',allow_quality_flags=True)
                base=apply_contrast(base,previous,frame,engine.geography,refs,engine.top)
                # finalize mutates scores/ranks: retain ONLY unadjusted base ranks.
                next_prior={'all_pixels':[dict(id=p['id'],score_rank=p['score_rank']) for p in base['all_pixels']]}
                if k<=SAMPLES:
                    valid_prior=prior if prior_stamp==stamp-STEP else {'all_pixels':[]}
                    snap=finalize(base,valid_prior,previous,frame,engine.geography,engine.top)
                    store.apply(snap,previous,frame,engine.geography,engine.top,save_series=False)
                    std.apply(snap,previous,frame,engine.geography,engine.top)
                    save_series(snap,frame,engine.geography,tmp)
                    report['completed']+=1
                prior,prior_stamp=next_prior,stamp
            except (OSError,ValueError,RuntimeError,EOFError) as exc:
                prior,prior_stamp={'all_pixels':[]},None
                if k<=SAMPLES:report['failed_samples']+=1
                if len(report['errors'])<16:report['errors'].append(dict(nominal_utc=stamp.isoformat(),stage='calculate',error=str(exc)))
                LOG.warning('Initial batch missing %s: %s',stamp,exc)
            if k<=SAMPLES and ((SAMPLES-k+1)%8==0 or k==1):
                LOG.info('Initial batch progress %s/%s, missing %s',report['completed'],SAMPLES,report['failed_samples']);progress()
        if store.path.exists():os.replace(store.path,engine.integral.path)
        if std.path.exists():os.replace(std.path,engine.std.path)
        (root/'series').mkdir(exist_ok=True)
        for p in (Path(tmp)/'series').glob('*.npz'):os.replace(p,root/'series'/p.name)
    report['phase']='complete' if not report['failed_samples'] else 'partial';progress()
    LOG.info('Initial batch done: %s/%s; now process frozen anchor %s',report['completed'],SAMPLES,anchor.isoformat())
    return report
