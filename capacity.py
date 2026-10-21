"""User-selected storage budgets, not a GitHub account-suspension prediction."""
import hashlib
from engine import BackupError,Paused,atomic_json,read_json,now
GIB=1024**3

def measure(remote,snapshot):
    used=0;present={};page=1
    while True:
        releases=remote.api('GET',f'/releases?per_page=100&page={page}')
        for release in releases:
            assets=remote.assets(release);used+=sum(a['size'] for a in assets.values())
            if release['tag_name']==snapshot['tag']:present=assets
        if len(releases)<100:break
        page+=1
    pending=sum(a['size'] for a in snapshot['assets'] if not (a['name'] in present and present[a['name']].get('state')=='uploaded' and present[a['name']].get('digest')=='sha256:'+a['sha256'] and present[a['name']]['size']==a['size']))
    return used,used+pending

def enforce(engine,snapshot):
    if len(snapshot['assets'])>1000 or any(a['size']>=2*GIB for a in snapshot['assets']):
        raise BackupError('超过 GitHub 单版本 1000 个附件或单附件小于 2 GiB 的限制，请结束本次任务后减少选择范围。')
    used,projected=measure(engine.remote,snapshot)
    soft=engine.c.get('capacity_soft_gib',20)*GIB;hard=engine.c.get('capacity_hard_gib',30)*GIB
    info={'repo':engine.c['repo'],'used':used,'projected':projected,'soft':soft,'hard':hard}
    engine.emit('stats',{'repository_bytes':used,'projected_repository_bytes':projected})
    if projected>=hard:
        raise BackupError(f'达到软件容量停止值：当前 {used/GIB:.2f} GiB，完成本轮预计 {projected/GIB:.2f} GiB，停止值 {hard/GIB:g} GiB。已停止新增上传；请更换新仓库或结束任务后减少选择。这不是官方封号阈值，换仓库不能消除账号风险。')
    ack=engine.state/('capacity-warning-'+hashlib.sha256(engine.c['repo'].lower().encode()).hexdigest()[:16]+'.json')
    if projected>=soft and not read_json(ack,{}).get('acknowledged'):
        confirm=getattr(engine,'confirm_capacity',None)
        if confirm is None or not confirm(info):raise Paused()
        atomic_json(ack,dict(info,acknowledged=True,at=now()))
