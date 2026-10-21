"""Explicit repository migration, preserving successful file identities and recovery history."""
import hashlib,sqlite3,threading,uuid
from pathlib import Path
from configuration import parse_repo,load_config,protect,read_recovery_key
from engine import GitHub,BackupError,RunLock,Index,atomic_json,now,read_json
from background import WorkerLock

def repository_chain(config):
    rows=[*config.get('repository_history',[]),{'repo':config['repo'],'repo_id':config.get('repo_id'),'state_dir':config['state_dir']}]
    seen=set()
    for row in rows:
        name=parse_repo(row['repo']).lower()
        if name in seen:raise BackupError('仓库历史包含重复地址')
        seen.add(name)
    return rows

def switch_repository(config_path,target,remote_factory=GitHub):
    c=load_config(config_path);target=parse_repo(target);history=repository_chain(c)
    if target.lower() in {r['repo'].lower() for r in history}:raise BackupError('此仓库已经使用过，请选择新的私密仓库。')
    state=Path(c['state_dir']);worker=WorkerLock(state/'background.lock');lock=RunLock(state/'engine.lock');remotes=[]
    if not worker.acquire():raise BackupError('请先暂停任务，等待后台退出后再切换仓库。')
    try:
        lock.acquire()
        old=remote_factory(c,lambda *a:None,threading.Event());remotes.append(old);old_info=old.private()
        candidate=dict(c,repo=target);candidate.pop('repo_id',None)
        new=remote_factory(candidate,lambda *a:None,threading.Event());remotes.append(new);info=new.private()
        if info['id'] in {r.get('repo_id') for r in history}|{old_info['id']}:raise BackupError('此仓库的身份已存在于历史记录中，即使改名也不能重复使用。')
        if new.api('GET','/releases?per_page=1'):raise BackupError('新仓库已有 Release，请使用没有备份历史的新私密仓库。')
        if not info.get('default_branch') or not new.api('GET','/commits?per_page=1'):raise BackupError('新仓库需要先添加 README，形成初始提交。')
        from task_cleanup import reconcile_published
        from cache_cleanup import confined
        for record in sorted((state/'runs').glob('*/run.json')):
            confined(record,state/'runs');snapshot=read_json(record,{})
            if snapshot.get('tag')!=record.parent.name:raise BackupError('旧任务目录与版本不一致，未切换')
            if snapshot.get('repo','').lower()!=c['repo'].lower():raise BackupError('旧任务仓库身份不匹配，未切换')
            reconcile_published(state,snapshot,old)
        profile_root=Path(c.get('profile_root',state.parent/'CloudVaultProfiles')).resolve()
        profile_root.mkdir(parents=True,exist_ok=True);protect(profile_root,True)
        destination=profile_root/('profile-'+uuid.uuid4().hex);destination.mkdir();protect(destination,True)
        source_db=state/'index.sqlite'
        if not source_db.is_file():raise BackupError('成功索引不存在，已停止切换，避免意外全量重传。')
        source=sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True);dest=sqlite3.connect(destination/'index.sqlite')
        try:source.backup(dest)
        finally:source.close();dest.close()
        index=Index(destination/'index.sqlite')
        try:
            # Older imports may only record baseline tags on individual file rows.
            tags=index.history()
            if not tags:
                tags=[r[0] for r in index.db.execute('SELECT DISTINCT tag FROM files WHERE tag IS NOT NULL ORDER BY tag')]
                with index.db:
                    index.db.execute("INSERT OR REPLACE INTO meta VALUES('history_tags',?)",(__import__('json').dumps(tags),))
                    if tags:index.db.execute("INSERT OR REPLACE INTO meta VALUES('last_tag',?)",(tags[-1],))
        finally:index.close()
        history[-1].update(repo_id=old_info['id'],retired_at=now())
        result=dict(c,repo=target,repo_id=info['id'],state_dir=str(destination),profile_root=str(profile_root),repository_history=history,baseline_manifests=[])
        result['exclude_paths']=list(dict.fromkeys(c.get('exclude_paths',[])+[str(profile_root),*[r['state_dir'] for r in history if r.get('state_dir')]]))
        key=read_recovery_key(result['key_file'])
        atomic_json(destination/'identity.json',{'repo':target.lower(),'repo_id':info['id'],'key_id':hashlib.sha256(key).hexdigest()})
        atomic_json(destination/'repository-history.json',{'repositories':[{k:r[k] for k in ('repo','repo_id') if r.get(k)} for r in repository_chain(result)]})
        atomic_json(destination/'migration.json',{'from_repo':c['repo'],'from_state':str(state),'created':now(),'successful_index_copied':True,'pending_old_tasks_preserved':True})
        # The config replacement is the commit point; old state and cloud data remain intact.
        atomic_json(Path(config_path),result)
        return result
    finally:
        lock.close();worker.close()
        for remote in remotes:
            if hasattr(remote,'session'):remote.session.close()
