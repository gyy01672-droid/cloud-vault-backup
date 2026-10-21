"""Read the ordered repository chain; all links remain inside encrypted manifests."""
from engine import BackupError
from configuration import parse_repo

def checked_chain(rows,current):
    if not isinstance(rows,list) or not rows or len(rows)>1000:raise BackupError('仓库历史格式无效')
    result=[];names=set();ids=set()
    for row in rows:
        repo=parse_repo(row['repo']);identity=row.get('repo_id')
        if repo.lower() in names or (identity is not None and (type(identity) is not int or identity in ids)):raise BackupError('仓库历史包含重复或无效身份')
        names.add(repo.lower())
        if identity is not None:ids.add(identity)
        result.append({'repo':repo,'repo_id':identity})
    if result[-1]['repo'].lower()!=current.lower():raise BackupError('仓库历史末项与当前仓库不符')
    return result

class HistoryRemote:
    def __init__(self,config,emit,cancel,factory=None):
        from recovery import RecoveryRemote
        self.c=config;self.emit=emit;self.cancel=cancel;self.factory=factory or RecoveryRemote;self.remotes={}
        self.chain=checked_chain([*config.get('repository_history',[]),{'repo':config['repo'],'repo_id':config.get('repo_id')}],config['repo'])
    def adopt(self,rows):
        candidate=checked_chain(rows,self.c['repo'])
        # An encrypted cloud plan can add history on a fresh machine, never remove known history.
        if [r['repo'].lower() for r in candidate][-len(self.chain):]!=[r['repo'].lower() for r in self.chain]:raise BackupError('云端仓库历史与本地记录不一致')
        for old,new in zip(reversed(self.chain),reversed(candidate)):
            if old.get('repo_id') is not None and old['repo_id']!=new.get('repo_id'):raise BackupError('云端仓库身份与本地记录不一致')
        self.chain=candidate
    def remote(self,row):
        name=row['repo'].lower()
        if name not in self.remotes:
            config=dict(self.c,repo=row['repo']);config.pop('repo_id',None)
            if row.get('repo_id') is not None:config['repo_id']=row['repo_id']
            self.remotes[name]=self.factory(config,self.emit,self.cancel)
        return self.remotes[name]
    def private(self):
        for row in self.chain:self.validate(row)
    def validate(self,row):
        remote=self.remote(row);info=remote.private()
        if row.get('repo_id') is not None and (not isinstance(info,dict) or info.get('id')!=row['repo_id']):raise BackupError('恢复仓库的实际身份与加密历史记录不一致')
        return remote
    def current_releases(self):return [dict(r,_repo=self.chain[-1]['repo']) for r in self.validate(self.chain[-1]).releases()]
    def releases(self):
        result=[]
        for row in self.chain:
            remote=self.validate(row)
            result.extend(dict(r,_repo=row['repo']) for r in remote.releases())
        return result
    def assets(self,release):
        repo=release['_repo'];remote=self.remote(next(r for r in self.chain if r['repo'].lower()==repo.lower()))
        return {name:dict(asset,_repo=repo) for name,asset in remote.assets(release).items()}
    def download(self,asset,path,callback):return self.remotes[asset['_repo'].lower()].download(asset,path,callback)
    def close(self):
        for remote in self.remotes.values():
            if hasattr(remote,'session'):remote.session.close()
        self.remotes.clear()
