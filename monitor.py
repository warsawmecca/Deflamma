#!/usr/bin/env python3
"""Monitor top-N B07 percentile/deviation joint scores over ROK land; no fire declaration."""
import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contrast import VERSION as CONTRAST_VERSION, apply_contrast, reference_arrays
from climatology import get_model
from rank_adjustment import apply_rank_cluster, EXPORT_FIELDS
from integral_filter import IntegralStore, EXPORT_FIELDS as INTEGRAL_FIELDS
from std_trigger import StdStore, FIELDS as STD_FIELDS, save_series
from reviews import ReviewStore, context_for
from retention import cleanup, storage_lock, StorageLimitError, MIB
from core import ALGORITHM_VERSION, Geography, compare_frames
from hsd import KST, STEP, UTC, read_frame
from source import Source, ready_pairs
from maps import render_html, render_png, start_server
from heatmap import build_heatmap, render_heatmap_png
from watches import WatchStore

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('monitor')


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='pending_', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, payload):
    atomic_text(path, json.dumps(payload, ensure_ascii=False, separators=(',',':'), allow_nan=False)+'\n')


def atomic_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='pending_', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def run_lock(output):
    """OS-released lock prevents two writers using the same output folder."""
    path = output/'monitor.lock'
    f = path.open('a+b')
    try:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another monitor is already using this --output folder') from exc
        yield
    finally:
        f.close()


class State:
    def __init__(self, output, signature):
        self.db = sqlite3.connect(output/'state.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS processed (nominal TEXT PRIMARY KEY, completed TEXT NOT NULL, result_file TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS gaps (nominal TEXT PRIMARY KEY, reason TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0)')
        previous = self.get('configuration')
        if previous is not None and previous != signature:
            self.db.close()
            raise ValueError('Configuration/mask changed: choose a new --output folder to avoid mixing results')
        self.set('configuration', signature)

    def get(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
        self.db.commit()

    def contains(self, stamp):
        return self.db.execute('SELECT 1 FROM processed WHERE nominal=?', (stamp.isoformat(),)).fetchone() is not None

    def processed(self, cutoff):
        return {datetime.fromisoformat(row[0]) for row in self.db.execute(
            'SELECT nominal FROM processed WHERE nominal>=?', (cutoff.isoformat(),))}

    def latest(self):
        row = self.db.execute('SELECT nominal FROM processed ORDER BY nominal DESC LIMIT 1').fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def finish(self, stamp, result_file):
        self.db.execute('INSERT OR IGNORE INTO processed VALUES (?,?,?)',
                        (stamp.isoformat(), datetime.now(UTC).isoformat(), str(result_file)))
        self.db.execute('UPDATE gaps SET resolved=1 WHERE nominal=?', (stamp.isoformat(),))
        self.db.commit()

    def gap(self, stamp, reason):
        now = datetime.now(UTC).isoformat()
        self.db.execute('INSERT INTO gaps VALUES (?,?,?,?,0) ON CONFLICT(nominal) DO UPDATE SET reason=excluded.reason,last_seen=excluded.last_seen,resolved=0',
                        (stamp.isoformat(),reason,now,now))
        self.db.commit()

    def gap_count(self):
        return self.db.execute('SELECT COUNT(*) FROM gaps WHERE resolved=0').fetchone()[0]

    def close(self):
        self.db.close()


class Engine:
    def __init__(self, output, boundary, state, top=10, min_rise_k=0, min_current_k=None, history_maps=True,
                 temperature_weight=.5, deviation_strength=.5, score_scale_max=500, allow_unloading=False,
                 latest_only=False, score_mode='legacy'):
        self.output, self.boundary, self.state = output, boundary, state
        self.score_mode = score_mode
        self.climatology_identity = get_model().identity() if score_mode=='contrast' else None
        self.top, self.min_rise_k, self.min_current_k = top, min_rise_k, min_current_k
        self.temperature_weight,self.deviation_strength=temperature_weight,deviation_strength
        self.score_scale_max,self.allow_unloading=score_scale_max,allow_unloading
        self.geography = None
        self.features = json.loads(boundary.read_text(encoding='utf-8'))['features']
        self.history_maps = history_maps and not latest_only
        self.latest_only = latest_only
        self.integral = IntegralStore(output)
        self.std = StdStore(output)
        self.reviews = ReviewStore(output)
        self.watches = WatchStore(output/'watchpoints.sqlite3')
        latest = output/'latest.json'
        if latest.exists():
            result = json.loads(latest.read_text(encoding='utf-8'))
            if result.get('algorithm') != (CONTRAST_VERSION if self.score_mode=='contrast' else ALGORITHM_VERSION):
                raise ValueError('Old ranking format: use a new --output folder; raw source is required for joint scores')
            if self.score_mode=='contrast' and result.get('scoring',{}).get('climatology',{}).get('sha256') != self.climatology_identity['sha256']:
                raise ValueError('Climatology changed: use a new --output folder')
            self.save_maps(result, repair=True)
        else:
            atomic_text(output/'latest_map.html', render_html(None,self.features,live=True))
            atomic_text(output/'latest_heatmap.html', render_html(None,self.features,live=True,heat_default=True))

    def save_heatmaps(self, result, base, latest=False):
        if 'heatmap' not in result:
            if latest:
                atomic_text(self.output/'latest_heatmap.html',render_html(result,self.features,live=True,heat_default=True))
            return  # Old ranking-only JSON cannot reconstruct all source pixels.
        if not latest and not self.history_maps:
            return
        png=render_heatmap_png(result,self.features)
        if latest:
            atomic_bytes(self.output/'latest_heatmap.png',png)
            atomic_text(self.output/'latest_heatmap.html',render_html(result,self.features,live=True,heat_default=True))
        if self.history_maps:
            atomic_bytes(base.parent/(base.name+'_heatmap.png'),png)
            atomic_text(base.parent/(base.name+'_heatmap.html'),render_html(result,self.features,heat_default=True))

    def save_maps(self, result, repair=False):
        stamp = datetime.fromisoformat(result['current_nominal_utc'])
        base = self.output/'history'/stamp.strftime('%Y%m%d')/stamp.strftime('%Y%m%dT%H%M%SZ')
        png_paths = [self.output/'latest_map.png']
        html_paths = [(self.output/'latest_map.html',True)]
        if self.history_maps:
            png_paths.append(base.with_suffix('.png'))
            html_paths.append((base.with_suffix('.html'),False))
        # On upgrade, backfill missing maps without rerunning the analysis.
        png_paths = [p for p in png_paths if not repair or p.name=='latest_map.png' or not p.exists()]
        if png_paths:
            png = render_png(result,self.features)
            for path in png_paths: atomic_bytes(path,png)
        for path,live in html_paths:
            if not repair or live or not path.exists():
                atomic_text(path,render_html(result,self.features,live=live))
        self.save_heatmaps(result,base,latest=True)

    def process(self, previous, current, mode='live', urls=None):
        # Shared with independent cleanup timer; no pruning during atomic output.
        with storage_lock(self.output):
            cleanup(self.output, reserve_bytes=128*MIB)
            try:
                return self._process(previous,current,mode,urls)
            finally:
                cleanup(self.output, strict=False)

    def _process(self, previous, current, mode='live', urls=None):
        if self.state.contains(current.nominal):
            return None
        if self.geography is None or self.geography.grid != current.grid:
            self.geography = Geography(current, self.boundary)
        result = compare_frames(previous, current, self.geography, self.top,
                                self.min_rise_k, self.min_current_k,self.temperature_weight,self.deviation_strength,
                                allow_unloading=self.allow_unloading and mode=='replay', allow_quality_flags=True)
        if self.score_mode == 'contrast':
            checkpoint=self.integral.load()
            if checkpoint is not None and current.nominal.timestamp()/60-float(checkpoint['minute'])>2.500001:
                self.integral.path.unlink(missing_ok=True)
            std_checkpoint=self.std.load()
            integral_checkpoint=self.integral.load()
            if integral_checkpoint is None or std_checkpoint is None or abs(float(std_checkpoint['time'])/60-float(integral_checkpoint['minute']))>1e-7:
                from bootstrap import bootstrap
                bootstrap(self,current,mode=mode)
            references = reference_arrays(current, self.geography, self.output/'reference_cache', allow_quality_flags=True)
            result = apply_contrast(result, previous, current, self.geography, references, self.top)
            result = apply_rank_cluster(result, previous, current, self.geography, self.top, self.output)
            result = self.integral.apply(result, previous, current, self.geography, self.top)
            result = self.std.apply(result, previous, current, self.geography, self.top)
            save_series(result,current,self.geography,self.output)
            if mode=='live':self.reviews.record(result,context_for(result,self.output,current))
            from contrast import cache_current_reference
            cache_current_reference(current,self.geography,self.output/'reference_cache')
        result.update({'mode':mode, 'generated_utc':datetime.now(UTC).isoformat(),
                       'source_urls':urls or [], 'boundary_sha256':hashlib.sha256(self.boundary.read_bytes()).hexdigest()})
        result['score_scale_max']=100 if self.score_mode=='contrast' else self.score_scale_max
        result['heatmap']=build_heatmap(previous,current,self.geography,result['scoring'],result['score_scale_max'], pixel_fields=result['all_pixels'] if self.score_mode=='contrast' else None)
        base = self.output/'history'/current.nominal.strftime('%Y%m%d')/current.nominal.strftime('%Y%m%dT%H%M%SZ')
        score_fields=['score_rank','eligible','temperature_percentile','rise_percentile',
                      'temperature_minus_mean_K','rise_minus_mean_K','temperature_z','rise_z',
                      'base_joint_score','deviation_weight','joint_score']
        fields = ['rank','id','region','latitude','longitude','previous_K','current_K','rise_K',
                  'adjacent8_median_rise_K','rise_over_adjacent8_K','native_row_1based','native_column_1based',
                  'previous_pixel_line_time_interpolated_kst','current_pixel_line_time_interpolated_kst']+score_fields
        buffer = io.StringIO(newline='')
        fields += EXPORT_FIELDS if self.score_mode=='contrast' else []
        fields += [f for f in INTEGRAL_FIELDS+STD_FIELDS if f not in fields] if self.score_mode=='contrast' else []
        fields += ['local_excess_K','past_excess_K','history_days','history_median_K','normal_skin_K','history_temperature_floor_min_K','history_temperature_floor_max_K','land_neighbors','valid_neighbors','component_a','component_b','component_c','component_d','score_status','condition_rise','condition_high_score','gate_status'] if self.score_mode=='contrast' else []
        writer = csv.DictWriter(buffer, fieldnames=fields)
        writer.writeheader(); writer.writerows(result['top_pixels'])
        csv_text = '\ufeff'+buffer.getvalue()
        json_file = self.output/'latest.json' if self.latest_only else base.with_suffix('.json')
        if not self.latest_only:
            write_json(json_file, result)
            atomic_text(base.with_suffix('.csv'), csv_text)
        all_buffer=io.StringIO(newline='')
        all_writer=csv.DictWriter(all_buffer,fieldnames=list(result['all_pixels'][0]))
        all_writer.writeheader();all_writer.writerows(result['all_pixels'])
        all_csv='\ufeff'+all_buffer.getvalue()
        last = self.state.latest()
        if last is None or current.nominal >= last:
            self.save_maps(result)
            atomic_text(self.output/'latest.csv', csv_text)
            atomic_text(self.output/'latest_all_pixels.csv',all_csv)
            write_json(self.output/'latest.json', result)
        elif self.history_maps:
            atomic_bytes(base.with_suffix('.png'),render_png(result,self.features))
            atomic_text(base.with_suffix('.html'),render_html(result,self.features))
            self.save_heatmaps(result,base)
        # Mark done only after every required output succeeded. Retries overwrite
        # the same paths, rather than appending duplicate data rows.
        self.watches.record_result(result,previous,current)
        self.state.finish(current.nominal, json_file)
        print(f"\n{previous.nominal.astimezone(KST):%Y-%m-%d %H:%M:%S} -> "
              f"{current.nominal.astimezone(KST):%H:%M:%S} KST | "
              f"{len(result['top_pixels'])} candidate pixels / {result['valid_pair_pixels']} valid", flush=True)
        print('rank region latitude longitude previous_K current_K rise_K joint_score', flush=True)
        for item in result['top_pixels']:
            print(f"{item['rank']:>2} {item['region']} {item['latitude']:.5f} {item['longitude']:.5f} "
                  f"{item['previous_K']:.2f} {item['current_K']:.2f} +{item['rise_K']:.2f} {item['joint_score']:.2f}", flush=True)
        if not result['top_pixels']:
            print('Valid pair; no pixels passed the configured candidate thresholds.', flush=True)
        LOG.info('Saved %s', json_file)
        return result


def poll_once(source, engine, state, now, lookback_minutes=60, max_age_minutes=30):
    cutoff = now-timedelta(minutes=lookback_minutes)
    available = source.available(cutoff-STEP, now)
    if not available:
        return {'phase':'waiting_for_data', 'data_healthy':False, 'new_results':0}
    latest = max(available)
    age = (now-latest).total_seconds()/60
    info = {'latest_available_nominal_utc':latest.isoformat(), 'latest_available_age_minutes':round(age, 2),
            'new_results':0}
    if age > max_age_minutes:
        return {**info, 'phase':'stale_data', 'data_healthy':False}
    start_text = state.get('start_nominal')
    if start_text is None:
        complete = [t for t in available if t-STEP in available and t >= cutoff]
        if not complete:
            return {**info, 'phase':'waiting_for_adjacent', 'data_healthy':False}
        start = max(complete)  # first startup deliberately begins with newest pair
        state.set('start_nominal', start.isoformat())
    else:
        start = datetime.fromisoformat(start_text)
    previous_last = state.latest()
    if previous_last is not None and previous_last+STEP < cutoff:
        reason = 'outage_exceeded_lookback; older interval before '+cutoff.isoformat()+' not automatically backfilled'
        state.gap(previous_last+STEP, reason)
        LOG.warning('%s (last saved %s)', reason, previous_last.isoformat())
    expected = max(start, cutoff)
    expected = datetime.fromtimestamp(math.ceil(expected.timestamp()/150)*150,tz=UTC)
    processed = state.processed(cutoff)
    while expected <= latest:
        if expected not in processed:
            if expected not in available:
                state.gap(expected,'current_observation_missing')
            elif expected-STEP not in available:
                state.gap(expected,'exact_previous_observation_missing')
        expected += STEP
    errors = []
    for stamp in ready_pairs(available, state.processed(cutoff), start, cutoff):
        try:
            result = engine.process(source.frame(stamp-STEP), source.frame(stamp),
                                    urls=[source.url(stamp-STEP),source.url(stamp)])
            if result is not None:
                info['new_results'] += 1
        except (ValueError, OSError, EOFError, RuntimeError, struct.error) as exc:
            LOG.error('Pair failed %s: %s', stamp.isoformat(), exc)
            state.gap(stamp,'pair_error: '+str(exc))
            errors.append({'nominal_utc':stamp.isoformat(), 'error':str(exc)})
    last = state.latest()
    info['latest_output_nominal_utc'] = last.isoformat() if last else None
    info['latest_output_age_minutes'] = round((now-last).total_seconds()/60, 2) if last else None
    info['unresolved_gap_records'] = state.gap_count()
    if errors:
        return {**info, 'phase':'processing_error', 'data_healthy':False, 'errors':errors}
    if not state.contains(latest):
        return {**info, 'phase':'waiting_for_adjacent', 'data_healthy':False,
                'missing_previous_nominal_utc':(latest-STEP).isoformat()}
    return {**info, 'phase':'up_to_date', 'data_healthy':True}


def parse_stamp(value):
    value = value.replace('Z', '+00:00')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=KST)
    stamp = stamp.astimezone(UTC)
    if stamp.microsecond or int(stamp.timestamp()) % 150:
        raise ValueError('--at must be on an exact 2.5-minute timeline')
    return stamp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--boundary', type=Path, default=ROOT/'data/KOR_ADM1.geojson')
    parser.add_argument('--satellite', type=int, choices=[8,9], default=9)
    parser.add_argument('--score-mode', choices=['contrast','legacy'], default='contrast', help='Bounded contrast score (default) or original web score')
    parser.add_argument('--top', type=int, default=10)
    parser.add_argument('--temperature-weight',type=float,default=.5)
    parser.add_argument('--deviation-strength',type=float,default=.5)
    parser.add_argument('--score-scale-max',type=float,default=500,help='Fixed score heatmap color ceiling; does not change scores')
    parser.add_argument('--allow-unloading-replay',action='store_true',help='Diagnostic replay only: allow operation flag 4')
    parser.add_argument('--min-rise-k', type=float, default=0.0, help='Strict lower bound; default selects positive rises')
    parser.add_argument('--min-current-k', type=float, help='Optional absolute BT filter; omitted by default')
    parser.add_argument('--poll-seconds', type=float, default=30)
    parser.add_argument('--lookback-minutes', type=int, default=60)
    parser.add_argument('--max-age-minutes', type=float, default=30)
    parser.add_argument('--cache-keep-hours', type=float, default=.25)
    parser.add_argument('--no-history-maps', action='store_true', help='Keep latest maps only; JSON/CSV history remains')
    parser.add_argument('--latest-only', action='store_true', default=True, help='Overwrite latest exports; do not create observation history')
    parser.add_argument('--keep-history', action='store_true', help='Retain at most 32 snapshots / 24h / 128MiB')
    parser.add_argument('--serve', action='store_true', help='Serve live map on localhost')
    parser.add_argument('--open-browser', action='store_true', help='Open map viewer; implies --serve')
    parser.add_argument('--port', type=int, default=8765)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true', help='One live poll, then exit')
    mode.add_argument('--at', help='One archive pair; ISO date/time, omitted timezone means KST')
    mode.add_argument('--current-file', type=Path, help='Offline current HSD')
    parser.add_argument('--previous-file', type=Path, help='Offline exact preceding HSD')
    args = parser.parse_args(argv)
    if args.keep_history:args.latest_only=False
    if args.output is None:
        args.output = Path('monitor_integral_output' if args.score_mode=='contrast' else 'monitor_score_output')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be 1..65535')
    args.serve = args.serve or args.open_browser
    if not 0<args.temperature_weight<1 or not math.isfinite(args.deviation_strength) or args.deviation_strength<0:
        parser.error('Use 0<w<1 and finite nonnegative deviation strength')
    if not math.isfinite(args.score_scale_max) or args.score_scale_max<=0:
        parser.error('--score-scale-max must be finite and positive')
    if args.allow_unloading_replay and not (args.at or args.current_file):
        parser.error('--allow-unloading-replay is allowed only with --at or offline files')
    if bool(args.current_file) != bool(args.previous_file):
        parser.error('--current-file and --previous-file must be used together')
    if not 1 <= args.top <= 1000 or not args.min_rise_k >= 0:
        parser.error('Use 1..1000 for --top and a finite nonnegative --min-rise-k')
    if not math.isfinite(args.min_rise_k) or (args.min_current_k is not None and not math.isfinite(args.min_current_k)):
        parser.error('Temperature filters must be finite')
    if (not all(math.isfinite(x) for x in (args.poll_seconds,args.max_age_minutes,args.cache_keep_hours))
            or args.poll_seconds < 5 or not 5 <= args.lookback_minutes <= 1440
            or not 0 < args.max_age_minutes <= args.lookback_minutes or args.cache_keep_hours <= 0):
        parser.error('Invalid timing/cache options')
    args.output = args.output.resolve(); args.boundary = args.boundary.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, format='%(asctime)sZ %(levelname)s %(message)s',datefmt='%Y-%m-%dT%H:%M:%S',
                        handlers=[logging.StreamHandler(), RotatingFileHandler(args.output/'monitor.log',
                                   maxBytes=5_000_000, backupCount=3, encoding='utf-8')])
    mode_name = 'replay' if args.at or args.current_file else 'live'
    climatology_identity = get_model().identity() if args.score_mode=='contrast' else None
    signature = json.dumps({'climatology':climatology_identity, 'algorithm':CONTRAST_VERSION if args.score_mode=='contrast' else ALGORITHM_VERSION, 'score_mode':args.score_mode, 'mode':mode_name,'satellite':args.satellite,
        'temperature_weight':args.temperature_weight,'deviation_strength':args.deviation_strength,
        'score_scale_max':args.score_scale_max,'allow_unloading_replay':args.allow_unloading_replay,
        'top':args.top,'min_rise_k':args.min_rise_k,'min_current_k':args.min_current_k,
        'boundary_sha256':hashlib.sha256(args.boundary.read_bytes()).hexdigest()},sort_keys=True)
    with run_lock(args.output):
        with storage_lock(args.output):cleanup(args.output)
        state = State(args.output, signature)
        server = None
        try:
            engine = Engine(args.output,args.boundary,state,args.top,args.min_rise_k,args.min_current_k,
                            history_maps=not args.no_history_maps,temperature_weight=args.temperature_weight,
                            deviation_strength=args.deviation_strength,score_scale_max=args.score_scale_max,
                            allow_unloading=args.allow_unloading_replay,latest_only=args.latest_only,score_mode=args.score_mode)
            if mode_name=='live':engine.watches.prune(datetime.now(UTC))
            if args.serve:
                server = start_server(args.output,args.port,args.open_browser,watch_store=engine.watches,
                    archive_config={'score_mode':args.score_mode,'satellite':args.satellite,'top':args.top,'boundary':str(args.boundary),
                                    'temperature_weight':args.temperature_weight,'deviation_strength':args.deviation_strength,
                                    'min_rise_k':args.min_rise_k,'min_current_k':args.min_current_k,
                                    'score_scale_max':args.score_scale_max})
            source = Source(args.output/'cache', args.satellite,allow_unloading=args.allow_unloading_replay,allow_quality_flags=True)
            if mode_name == 'replay':
                if args.current_file:
                    previous,current = read_frame(args.previous_file,args.allow_unloading_replay,allow_quality_flags=True),read_frame(args.current_file,args.allow_unloading_replay,allow_quality_flags=True)
                    urls = []
                else:
                    stamp = parse_stamp(args.at)
                    previous,current = source.frame(stamp-STEP),source.frame(stamp)
                    urls = [source.url(stamp-STEP),source.url(stamp)]
                if current.satellite != f'Himawari-{args.satellite}':
                    raise ValueError('Frame satellite differs from --satellite')
                result = engine.process(previous,current,mode='replay',urls=urls)
                write_json(args.output/'status.json', {'mode':'replay','phase':'replay_complete',
                          'diagnostic_only':False,
                          'quality_flags_included':True,
                          'new_results':int(result is not None),'checked_utc':datetime.now(UTC).isoformat()})
                if server:
                    LOG.info('Replay complete; map viewer stays open until Ctrl+C')
                    while True: time.sleep(1)
                return 0
            last_phase = None
            while True:
                now = datetime.now(UTC)
                try:
                    status = poll_once(source,engine,state,now,args.lookback_minutes,args.max_age_minutes)
                    source.prune_cache(now,min(.25,args.cache_keep_hours))
                except (ValueError, OSError, EOFError, RuntimeError, struct.error, ET.ParseError) as exc:
                    LOG.exception('Acquisition/poll failed')
                    status = {'phase':'acquisition_error','data_healthy':False,'new_results':0,'error':str(exc)}
                finally:
                    engine.watches.prune(datetime.now(UTC))
                    with storage_lock(args.output):cleanup(args.output,strict=False)
                status.update({'mode':'live','checked_utc':datetime.now(UTC).isoformat(),
                               'poll_seconds':args.poll_seconds})
                write_json(args.output/'status.json',status)
                if status['phase'] != last_phase:
                    LOG.info('Monitor status: %s', status['phase'])
                    last_phase = status['phase']
                if args.once:
                    if server:
                        LOG.info('Single poll complete; map viewer stays open until Ctrl+C')
                        while True: time.sleep(1)
                    return 0 if status['data_healthy'] else 3
                time.sleep(args.poll_seconds)
        except Exception as exc:
            if mode_name=='replay':
                write_json(args.output/'status.json',{'mode':'replay','phase':'processing_error',
                           'data_healthy':False,'new_results':0,'error':str(exc),
                           'checked_utc':datetime.now(UTC).isoformat()})
            raise
        finally:
            if server:
                server.shutdown(); server.server_close()
            state.close()


if __name__ == '__main__':
    try:
        if hasattr(sys.stdout,'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.info('Stopped by user')
        sys.exit(0)
    except Exception:
        LOG.exception('Monitor stopped')
        sys.exit(1)
