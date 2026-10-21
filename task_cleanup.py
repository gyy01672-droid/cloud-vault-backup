"""Discard only unfinished local work after both worker and engine have stopped."""
from pathlib import Path
from background import WorkerLock
from engine import RunLock,BackupError,read_json,atomic_json,now,GitHub,Index
import threading
from cache_cleanup import clean_backup,clean_restore,confined

def reconcile_published(state,snapshot,remote):
    """Resolve a lost publication response before discarding or migrating its index."""
    if not snapshot.get('release_id') or snapshot.get('status') in ('completed','cancelled'):return False
    release=remote.api('GET','/releases/'+str(snapshot['release_id']),missing_ok=True)
    if not release:return False
    if release.get('tag_name')!=snapshot['tag']:raise BackupError('云端版本身份不符，未结束任务')
    if release.get('draft') is True:return False
    if release.get('draft') is not False:raise BackupError('无法确认云端发布状态，保留缓存')
    assets=remote.assets(release)
    if not snapshot.get('assets'):raise BackupError('已发布任务缺少本地附件清单')
    for asset in snapshot['assets']:
        found=assets.get(asset['name'],{})
        if found.get('state')!='uploaded' or found.get('size')!=asset['size'] or found.get('digest')!='sha256:'+asset['sha256']:raise BackupError('已发布版本不完整，保留缓存并停止操作')
    index=Index(Path(state)/'index.sqlite')
    try:
        if snapshot['tag'] not in index.history():index.commit(snapshot)
    finally:index.close()
    snapshot.update(status='completed',finished=now())
    atomic_json(Path(state)/'runs'/snapshot['tag']/'run.json',snapshot)
    return True

def discard_tasks(config,remote_factory=GitHub):
    state=Path(config['state_dir']);worker=WorkerLock(state/'background.lock');lock=RunLock(state/'engine.lock')
    if not worker.acquire():raise BackupError('后台尚未退出，请等待安全暂停完成。')
    result={'finished':now(),'freed_bytes':0,'tasks':0,'errors':[],'drafts':[]};remote=None
    try:
        lock.acquire()
        for record in (state/'runs').glob('*/run.json'):
            confined(record,state/'runs');snapshot=read_json(record,{})
            if snapshot.get('tag')!=record.parent.name:raise BackupError('任务目录与版本不一致，拒绝清理')
            if snapshot.get('status')=='completed':continue
            if snapshot.get('status') not in ('building','packing','uploading','cancelled'):raise BackupError('未知任务状态，拒绝清理')
            if snapshot.get('repo','').lower()!=config['repo'].lower():raise BackupError('缓存仓库身份不匹配')
            if snapshot.get('release_id') and snapshot.get('status')!='cancelled':
                if remote is None:remote=remote_factory(config,lambda *a:None,threading.Event());remote.private()
                if reconcile_published(state,snapshot,remote):
                    result['freed_bytes']+=clean_backup(record.parent,state,snapshot)
                    snapshot['cache_cleaned']=True;atomic_json(record,snapshot);continue
            # Persist the tombstone first: an interruption during cleanup must never resume upload.
            snapshot.update(status='cancelled',cancelled_at=now());atomic_json(record,snapshot)
            try:
                result['freed_bytes']+=clean_backup(record.parent,state,snapshot,discard=True)
                snapshot.update(cache_cleaned=True,selected=[],files=[],stage_failed=[],issue_count=len(snapshot.get('issues',[])),issues=snapshot.get('issues',[])[:100])
                atomic_json(record,snapshot);result['tasks']+=1
                if snapshot.get('release_id'):result['drafts'].append(snapshot['tag'])
            except (OSError,ValueError) as ex:result['errors'].append(str(ex))
        for base in (state/'restore').glob('*'):
            try:result['freed_bytes']+=clean_restore(base,state)
            except (OSError,ValueError) as ex:result['errors'].append(str(ex))
        # Large UI selection documents are transient, unlike indexes, keys and reports.
        for name in ('selection-request.json','selection-response.json','restore-request.json','restore-response.json','capacity-request.json','capacity-response.json'):
            p=confined(state/name,state)
            if p.is_file():result['freed_bytes']+=p.stat().st_size;p.unlink()
        atomic_json(state/'last-discard-report.json',result)
        return result
    finally:
        lock.close();worker.close()
        if remote is not None and hasattr(remote,'session'):remote.session.close()
