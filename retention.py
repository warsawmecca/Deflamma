"""Bounded app-owned retention; no traversal outside --output, no symlink following."""
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json,os,re,shutil,sqlite3,time
from pathlib import Path

MIB=1024**2
LIMIT=2048*MIB
class StorageLimitError(RuntimeError):pass

@contextmanager
def storage_lock(root,blocking=True):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    f=(root/'storage.lock').open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            f.seek(0);f.write(b'0');f.flush();f.seek(0)
            msvcrt.locking(f.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB))
        yield
    finally:f.close()

def files(root):
    root=Path(root)
    for base,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not (Path(base)/d).is_symlink()]
        for n in names:
            p=Path(base)/n
            if p.is_file() and not p.is_symlink():yield p

def size(root):return sum(p.stat().st_size for p in files(root))

def unlink(p):
    if not p.is_symlink():p.unlink(missing_ok=True)

def prune_flat(root,age_seconds,max_count,max_bytes,now,pattern='*'):
    if root.is_symlink():return
    paths=sorted((p for p in root.glob(pattern) if p.is_file() and not p.is_symlink()),key=lambda p:p.stat().st_mtime)
    for p in paths[:]:
        if now-p.stat().st_mtime>age_seconds:unlink(p);paths.remove(p)
    total=sum(p.stat().st_size for p in paths)
    while paths and (len(paths)>max_count or total>max_bytes):
        p=paths.pop(0);total-=p.stat().st_size;unlink(p)

def cleanup(root,now=None,reserve_bytes=0,strict=True):
    root=Path(root).resolve();now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc);clock=now.timestamp()
    # Root already selected by caller; remove only app-owned paths below it.
    prune_flat(root/'cache',900,16,32*MIB,clock)
    prune_flat(root/'reference_cache',8*86400,4096,512*MIB,clock, '*.npz')
    prune_flat(root/'series',7380,49,96*MIB,clock,'*.npz')
    for sub in ['noise_cache']:
        p=root/sub
        if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
    # Historical snapshots are grouped, so no broken partial HTML/CSV exports.
    hist=root/'history';groups={}
    if hist.exists() and not hist.is_symlink():
        for p in files(hist):groups.setdefault((p.parent,p.name.split('.')[0].split('_')[0]),[]).append(p)
    ordered=sorted(groups.values(),key=lambda group:max(p.stat().st_mtime for p in group))
    total=sum(p.stat().st_size for group in ordered for p in group)
    while ordered and (len(ordered)>32 or total>128*MIB or clock-max(p.stat().st_mtime for p in ordered[0])>86400):
        for p in ordered.pop(0):total-=p.stat().st_size;unlink(p)
    archive=root/'archive_queries';done=[];active=[]
    if archive.exists() and not archive.is_symlink():
        for folder in archive.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r'[0-9a-f]{32}',folder.name):continue
            try:q=json.loads((folder/'query.json').read_text())
            except (OSError,ValueError):q={}
            latest=max((p.stat().st_mtime for p in files(folder)),default=folder.stat().st_mtime)
            if q.get('phase') in ('ready','error') or clock-latest>7200:done.append((latest,folder,size(folder)))
            else:active.append(folder)
        done.sort();total=sum(x[2] for x in done)
        while done and (len(done)+len(active)>32 or total>256*MIB or clock-done[0][0]>86400):
            _,p,s=done.pop(0);shutil.rmtree(p);total-=s
    # Atomic-write leftovers and failed download parts are short-lived.
    for p in list(files(root)):
        if any(a in ('archive_queries',) for a in p.relative_to(root).parts):continue
        if (p.name.startswith('pending_') or p.suffix in ('.part','.pending')) and clock-p.stat().st_mtime>3600:unlink(p)
    for p in root.glob('.bootstrap_*'):
        if p.is_dir() and not p.is_symlink() and clock-p.stat().st_mtime>3600:shutil.rmtree(p)
    # Expired integral state is no longer a valid continuation. A new frame also
    # checks the exact 150s cadence and resets on any gap.
    from reviews import ReviewStore
    ReviewStore(root).cleanup(clock)
    std=root/'std_state.npz'
    if std.exists() and clock-std.stat().st_mtime>86400:unlink(std)
    p=root/'integral_state.npz'
    if p.exists() and clock-p.stat().st_mtime>86400:unlink(p)
    for name in ['state.sqlite3','watchpoints.sqlite3']:
        path=root/name
        if not path.exists() or path.is_symlink():continue
        with sqlite3.connect(path,timeout=15) as db:
            cutoff=(now-timedelta(hours=24)).isoformat()
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if name=='state.sqlite3':
                for table in ['processed','gaps']:
                    if table in tables:db.execute('DELETE FROM '+table+' WHERE nominal<?',(cutoff,))
            elif 'watch_samples' in tables:
                db.execute('DELETE FROM watch_samples WHERE nominal_utc<=?',(cutoff,))
            db.commit();db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            if db.execute('PRAGMA freelist_count').fetchone()[0]>128:db.execute('VACUUM')
    # Reclaim empty date folders.
    if hist.exists():
        for p in hist.iterdir():
            if p.is_dir() and not p.is_symlink() and not any(p.iterdir()):p.rmdir()
    used=size(root)
    report=dict(checked_utc=datetime.now().astimezone().isoformat(),used_bytes=used,limit_bytes=LIMIT,reserve_bytes=reserve_bytes,healthy=used+reserve_bytes<=LIMIT)
    (root/'retention_status.json').write_text(json.dumps(report,separators=(',',':')))
    if strict and (not report['healthy'] or shutil.disk_usage(root).free<reserve_bytes+32*MIB):raise StorageLimitError('Storage budget/free space exhausted; acquisition paused, protected state not deleted')
    return report

def main():
    import argparse
    from datetime import timezone
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    try:
        with storage_lock(args.output,blocking=False):print(json.dumps(cleanup(args.output,datetime.now(timezone.utc))))
    except (BlockingIOError,PermissionError):print('Busy or not writable; cleanup will retry on the next timer');return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
