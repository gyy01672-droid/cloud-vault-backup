"""Download, authenticate, stage, review, then restore original file locations."""
from __future__ import annotations
import hashlib, json, os, re, shutil, tempfile, threading, uuid, zipfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit
import requests
from configuration import protect, read_recovery_key
from cryptography.exceptions import InvalidTag
from engine import BackupError, GitHub, IO, Paused, atomic_json, decrypt, digest, now, read_json
from restore_incremental import mapped_target, check_target, restore
from cache_cleanup import clean_restore

def checked_row(row):
    if not isinstance(row,dict) or not isinstance(row.get('source'),str):raise BackupError('备份缺少原始路径，不能原位恢复')
    if type(row.get('size')) is not int or row['size']<0:raise BackupError('文件大小无效')
    if type(row.get('mtime_ns')) is not int or row['mtime_ns']<0:raise BackupError('文件时间无效')
    if not re.fullmatch('[0-9a-f]{64}',str(row.get('sha256',''))):raise BackupError('文件校验信息无效')
    return {k:row[k] for k in ('source','size','mtime_ns','sha256')}

def no_links(target):
    for p in [target,*target.parents]:
        try:st=p.lstat()
        except FileNotFoundError:continue
        if p.is_symlink() or getattr(st,'st_file_attributes',0)&0x400:
            raise BackupError('原路径包含链接、目录连接或云占位项：'+str(p))

def original_target(source,protected=()):
    # Apply the same strict path component checks as safe-folder recovery first.
    mapped_target(Path(tempfile.gettempdir()).resolve(),source)
    if os.name=='nt':
        p=PureWindowsPath(source)
        if not p.is_absolute() or not re.fullmatch('[A-Za-z]:',p.drive):raise BackupError('原位恢复需要本机盘符路径；网络路径请使用单独目录恢复')
    elif not source.startswith('/') or '\\' in source:raise BackupError('此路径来自其他操作系统，请使用单独目录恢复')
    target=Path(source)
    if not target.is_absolute() or not target.name:raise BackupError('无效的原始文件路径')
    no_links(target)
    for p in protected:
        if target.resolve().is_relative_to(Path(p).resolve()):raise BackupError('不能覆盖正在使用的备份程序、密钥或状态目录')
    if not Path(target.anchor).exists():raise BackupError('原盘符不可用：'+target.anchor)
    return target

class RecoveryRemote(GitHub):
    def releases(self):
        result=[];page=1
        while True:
            rows=self.api('GET',f'/releases?per_page=100&page={page}')
            result.extend(r for r in rows if not r.get('draft') and not r.get('prerelease'))
            if len(rows)<100:break
            page+=1
        return sorted(result,key=lambda r:(r.get('published_at') or r.get('created_at',''),r['id']))
    def download(self,asset,path,callback):
        expected=asset.get('digest','')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',expected or ''):raise BackupError('云端分包缺少 SHA-256，不能安全续传')
        if type(asset.get('size')) is not int or asset['size']<36 or type(asset.get('id')) is not int:raise BackupError('无效云端附件')
        no_links(path);part=path.with_suffix('.partial');no_links(part)
        if path.exists() and path.stat().st_size==asset['size'] and digest(path)==expected[7:]:return path
        for attempt in range(6):
            self.check();offset=part.stat().st_size if part.exists() else 0
            if offset>=asset['size']:
                if offset==asset['size'] and digest(part)==expected[7:]:os.replace(part,path);return path
                part.unlink();offset=0
            headers={'Accept':'application/octet-stream','Accept-Encoding':'identity'}
            if offset:headers['Range']=f'bytes={offset}-'
            response=None
            try:
                response=self.session.get('https://api.github.com'+self.prefix+f'/releases/assets/{asset["id"]}',headers=headers,stream=True,timeout=(15,45),allow_redirects=False)
                if response.status_code in (301,302,303,307,308):
                    url=response.headers.get('Location','');parsed=urlsplit(url)
                    if parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.githubusercontent.com') or parsed.username or parsed.password:
                        raise BackupError('附件下载跳转地址不安全')
                    response.close()
                    # A signed asset URL must never receive the GitHub access token.
                    response=requests.get(url,headers=headers,stream=True,timeout=(15,45),allow_redirects=False)
                if response.status_code==206:
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                    if not match or int(match[1])!=offset or int(match[3])!=asset['size']:raise BackupError('续传范围不匹配')
                elif response.status_code==200:offset=0
                else:response.raise_for_status();raise BackupError('下载服务器未返回文件内容')
                received=offset
                with open(part,'ab' if offset else 'wb') as out:
                    for block in response.iter_content(IO):
                        self.check()
                        if not block:continue
                        received+=len(block)
                        if received>asset['size']:raise BackupError('下载内容超过声明大小')
                        out.write(block);callback(len(block),received)
                    out.flush();os.fsync(out.fileno())
                if received!=asset['size']:raise requests.ConnectionError('Incomplete download')
                if digest(part)!=expected[7:]:part.unlink();raise BackupError('下载分包校验失败，请重试')
                os.replace(part,path);return path
            except requests.RequestException:
                if attempt==5:raise BackupError('下载中断，已保留续传文件；再次恢复即可继续') from None
                self.wait(min(30,2**(attempt+1)))
            finally:
                if response is not None:response.close()

class RecoveryEngine:
    def __init__(self,config,emit=lambda *a:None,cancel=None,remote=None):
        self.c=config;self.emit=emit;self.cancel=cancel or threading.Event();self.remote=remote
        self.key=read_recovery_key(config['key_file'])
        if len(self.key)!=32:raise BackupError('恢复密钥必须是 32 字节')
        identity=hashlib.sha256((config['repo'].lower()+':'+hashlib.sha256(self.key).hexdigest()).encode()).hexdigest()[:24]
        self.base=Path(config['state_dir'])/'restore'/identity;self.base.mkdir(parents=True,exist_ok=True)
        no_links(self.base);protect(self.base,directory=True)
        self.staging=(self.base/'prepared').resolve();self.staging.mkdir(exist_ok=True)
        self.downloads=self.base/'downloads';self.downloads.mkdir(exist_ok=True)
        self.catalog={};self.wire=0;self.total=0;self.downloaded=0
        self.protected=[config['state_dir'],config['key_file'],str(Path(__file__).resolve().parent)]
        self.protected += [r['state_dir'] for r in config.get('repository_history',[]) if r.get('state_dir')]
        if config.get('profile_root'):self.protected.append(config['profile_root'])
    def check(self):
        if self.cancel.is_set():raise Paused()
    def stagepath(self,source):
        mapped_target(self.staging,source)
        identity=str(PureWindowsPath(source)).casefold() if PureWindowsPath(source).is_absolute() else source
        path=self.staging/'objects'/hashlib.sha256(identity.encode()).hexdigest()
        check_target(self.staging,path);return path
    def fetch(self,asset,folder):
        self.check();name=asset['name']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.pcbackup',name):raise BackupError('不安全的附件名')
        path=folder/name;no_links(path)
        def progress(amount,current):
            self.wire+=amount
            self.emit('stats',{'stage':'download','wire_bytes':self.wire,'total':self.total,'uploaded':self.downloaded,'inflight':current})
        self.remote.download(asset,path,progress);self.downloaded+=asset['size']
        self.emit('stats',{'stage':'download','wire_bytes':self.wire,'total':self.total,'uploaded':self.downloaded,'inflight':0})
        return path
    def rows(self,rows):
        for raw in rows:
            row=checked_row(raw);target=self.stagepath(row['source'])
            self.catalog[str(target).casefold()]=row
    def legacy(self,path):
        with tempfile.TemporaryDirectory(dir=self.base,prefix='legacy-') as temp:
            plain=Path(temp)/'archive.zip';decrypt(path,plain,self.key)
            with zipfile.ZipFile(plain) as archive:
                if '_manifest.json' not in archive.namelist():raise BackupError('未知完整备份格式')
                rows=json.loads(archive.read('_manifest.json'))
                # Early metadata-only archives have no source: they contain backup reports,
                # not user files. Never infer their destinations from archive member names.
                if rows and all('source' not in r for r in rows):return []
                seen=set()
                if shutil.disk_usage(self.base).free<sum(checked_row(r)['size'] for r in rows):raise BackupError('解密缓存空间不足；请释放本地空间后继续')
                for raw in rows:
                    self.check();row=checked_row(raw);target=self.stagepath(row['source'])
                    if str(target).casefold() in seen:raise BackupError('完整备份中包含重复路径')
                    seen.add(str(target).casefold());target.parent.mkdir(parents=True,exist_ok=True)
                    if archive.getinfo(raw['archive_path']).file_size!=row['size']:raise BackupError('原始文件长度不匹配')
                    fd,name=tempfile.mkstemp(dir=target.parent,prefix='.restore-');h=hashlib.sha256();size=0
                    try:
                        with os.fdopen(fd,'wb') as out,archive.open(raw['archive_path']) as inp:
                            for b in iter(lambda:inp.read(IO),b''):
                                self.check();size+=len(b)
                                if size>row['size']:raise BackupError('原始文件超出声明大小')
                                h.update(b);out.write(b)
                        if size!=row['size'] or h.hexdigest()!=row['sha256']:raise BackupError('完整备份文件校验失败')
                        check_target(self.staging,target);os.replace(name,target)
                    finally:
                        if Path(name).exists():Path(name).unlink()
                return rows
    def prepare(self):
        from history_remote import HistoryRemote,checked_chain
        if self.remote is None:
            self.remote=HistoryRemote(self.c,self.emit,self.cancel)
            # Discover portable history from the newest authenticated manifest, before applying any version.
            for release in reversed(self.remote.current_releases()):
                assets=self.remote.assets(release)
                if 'manifest.pcbackup' not in assets:continue
                folder=self.downloads/str(int(release['id']));folder.mkdir(exist_ok=True);no_links(folder)
                manifest=self.fetch(assets['manifest.pcbackup'],folder)
                with tempfile.TemporaryDirectory(dir=self.base) as temp:
                    plain=Path(temp)/'history.zip';decrypt(manifest,plain,self.key)
                    with zipfile.ZipFile(plain) as z:plan=json.loads(z.read('snapshot.json'))
                if plan.get('repo','').lower()!=self.c['repo'].lower() or plan.get('tag')!=release['tag_name']:raise BackupError('历史清单与当前仓库不匹配')
                if plan.get('repositories'):self.remote.adopt(plan['repositories'])
                break
            self.downloaded=0;self.wire=0
        self.remote.private();self.emit('phase','读取云端完整备份与历次增量备份')
        releases=[]
        for release in self.remote.releases():
            self.check();assets={n:a for n,a in self.remote.assets(release).items() if n.endswith('.pcbackup')}
            if assets:releases.append((release,assets))
        if not releases:raise BackupError('没有找到已完成的加密备份；上传中的版本不会用于恢复')
        self.total=sum(a['size'] for _,assets in releases for a in assets.values());tags={r['tag_name'] for r,_ in releases}
        if len(tags)!=len(releases):raise BackupError('跨仓库出现重复备份版本，已停止恢复')
        previous=None
        for release,assets in releases:
            self.check();tag=release['tag_name']
            folder=self.downloads/str(int(release['id']));folder.mkdir(exist_ok=True);no_links(folder)
            fingerprint=hashlib.sha256(json.dumps(sorted((n,a['id'],a['size'],a.get('digest')) for n,a in assets.items())).encode()).hexdigest()
            receipt=folder/'prepared.json';saved=read_json(receipt,{})
            if saved and saved.get('fingerprint')!=fingerprint:raise BackupError('已准备的云端版本发生变化；请使用新的本地状态目录恢复')
            if saved:
                if any(t not in tags for t in saved.get('required_tags',[])) or (saved.get('parent_tag') is not None and saved['parent_tag']!=previous):raise BackupError('恢复缓存依赖的云端版本缺失或顺序错误')
                self.rows(saved['files']);self.downloaded+=sum(a['size'] for a in assets.values());previous=tag;continue
            self.wire=0;self.emit('phase','下载并校验备份：'+tag)
            rows=[]
            if 'manifest.pcbackup' in assets:
                manifest=self.fetch(assets['manifest.pcbackup'],folder)
                with tempfile.TemporaryDirectory(dir=self.base) as temp:
                    plain=Path(temp)/'manifest.zip';decrypt(manifest,plain,self.key)
                    with zipfile.ZipFile(plain) as z:plan=json.loads(z.read('snapshot.json'))
                expected_repo=release.get('_repo',self.c['repo'])
                if plan.get('version')!=2 or plan.get('tag')!=tag or plan.get('repo','').lower()!=expected_repo.lower():raise BackupError('备份清单与仓库或版本不匹配')
                if plan.get('repositories') and isinstance(self.remote,HistoryRemote):
                    required=checked_chain(plan['repositories'],expected_repo)
                    actual=self.remote.chain[:len(required)]
                    if [r['repo'].lower() for r in required]!=[r['repo'].lower() for r in actual]:raise BackupError('恢复所需的仓库历史缺失或顺序错误')
                    if any(r.get('repo_id') is not None and r['repo_id']!=a.get('repo_id') for r,a in zip(required,actual)):raise BackupError('恢复仓库身份不一致')
                parent=plan.get('parent_tag')
                required_tags=plan.get('required_tags',[])
                if not isinstance(required_tags,list) or any(not isinstance(t,str) or t not in tags for t in required_tags):raise BackupError('完整恢复依赖的历史版本缺失')
                if parent is not None and (parent not in tags or parent!=previous):raise BackupError('增量备份链缺失或分叉，不能宣称完整恢复')
                for raw in plan['files']:checked_row(raw);mapped_target(self.staging,raw['source'])
                required=2*sum(r['size'] for r in plan['files'])+sum(a['size'] for a in plan['assets'])
                if shutil.disk_usage(self.base).free<required:raise BackupError('准备此版本的解密和校验缓存空间不足；请释放空间后继续')
                for asset in plan['assets']:
                    remote=assets.get(asset['name'])
                    if not remote or remote['size']!=asset['size'] or remote.get('digest')!='sha256:'+asset['sha256']:raise BackupError('云端备份附件不完整或校验值不匹配')
                    self.fetch(remote,folder)
                self.emit('phase','解密并验证文件：'+tag)
                restore(folder,self.staging,self.key,apply=True,cancel=self.cancel,temp_root=self.base,target_mapper=self.stagepath)
                rows=plan['files']
            else:
                groups={}
                for name in assets:
                    match=re.fullmatch(r'(.+)-(\d+)-of-(\d+)\.pcbackup',name)
                    if match:
                        count=int(match[3]);number=int(match[2])
                        if not 1<=number<=count<=1000000:raise BackupError('旧版分包编号无效')
                        groups.setdefault((match[1],count),set()).add(number)
                if any(numbers!=set(range(1,count+1)) for (_,count),numbers in groups.items()):raise BackupError('旧版完整备份缺少分包，已停止恢复')
                for asset in sorted(assets.values(),key=lambda a:a['name']):
                    self.check();rows.extend(self.legacy(self.fetch(asset,folder)))
                atomic_json(self.staging/'.cloud-vault-restore.json',{'tag':tag,'repo':self.c['repo']})
            saved={'fingerprint':fingerprint,'files':rows}
            if 'manifest.pcbackup' in assets:saved.update(parent_tag=plan.get('parent_tag'),required_tags=plan.get('required_tags',[]))
            self.rows(rows);atomic_json(receipt,saved);previous=tag
        self.emit('phase','检查恢复位置、文件读写权限与可用空间')
        rows=list(self.catalog.values());issues=[];conflicts=0;identical=0;volumes={};total_bytes=sum(r['size'] for r in rows)
        for i,row in enumerate(rows):
            self.check();source=self.stagepath(row['source'])
            if not source.is_file() or source.stat().st_size!=row['size'] or digest(source)!=row['sha256']:raise BackupError('恢复缓存校验失败；请使用新的本地状态目录重新恢复')
            try:
                target=original_target(row['source'],self.protected)
                if target.exists():
                    if not target.is_file():raise BackupError('原位置不是普通文件')
                    if digest(target)==row['sha256']:row['action']='unchanged';identical+=1
                    else:row['action']='replace';conflicts+=1
                else:row['action']='create'
                ancestor=target.parent
                while not ancestor.exists():ancestor=ancestor.parent
                if not ancestor.is_dir():raise BackupError('父路径不是目录')
                if not os.access(ancestor,os.W_OK):raise PermissionError('当前用户没有目录写入权限')
                if row['action']!='unchanged':
                    key=str(target.anchor);v=volumes.setdefault(key,{'required':0,'free':shutil.disk_usage(ancestor).free});v['required']+=row['size']+(target.stat().st_size if target.exists() else 0)
            except (OSError,BackupError) as ex:row['action']='blocked';issues.append({'source':row['source'],'error':str(ex)})
            if i%50==0:self.emit('stats',{'stage':'restore_check','scanned':i+1,'changed':len(rows),'changed_bytes':total_bytes})
        space_errors=[drive for drive,v in volumes.items() if v['required']>v['free']]
        review={'files':rows,'count':len(rows),'bytes':sum(r['size'] for r in rows),'conflicts':conflicts,'identical':identical,'issues':issues,'volumes':volumes,'space_errors':space_errors,'releases':len(releases)}
        atomic_json(self.base/'review.json',review);return review
    def install_one(self,row):
        self.check();target=original_target(row['source'],self.protected)
        source=self.stagepath(row['source'])
        if not source.is_file() or digest(source)!=row['sha256']:raise BackupError('恢复缓存已变化，拒绝写入')
        if target.exists() and target.is_file() and digest(target)==row['sha256']:return 'unchanged',None
        target.parent.mkdir(parents=True,exist_ok=True);no_links(target)
        fd,name=tempfile.mkstemp(dir=target.parent,prefix='.cloud-vault-');temp=Path(name);backup=None
        try:
            with os.fdopen(fd,'wb') as out,open(source,'rb') as inp:
                for b in iter(lambda:inp.read(IO),b''):self.check();out.write(b)
                out.flush();os.fsync(out.fileno())
            if digest(temp)!=row['sha256']:raise BackupError('写入后校验失败')
            os.utime(temp,ns=(row['mtime_ns'],row['mtime_ns']));self.check();no_links(target)
            if target.exists():
                if not target.is_file():raise BackupError('原位置不是普通文件')
                backup=target.with_name(target.stem+'.before-cloud-vault-'+uuid.uuid4().hex[:12]+target.suffix)
                # Copy the local version first; the original stays in place until atomic replacement.
                with open(target,'rb') as inp,open(backup,'xb') as out:
                    for b in iter(lambda:inp.read(IO),b''):self.check();out.write(b)
                    out.flush();os.fsync(out.fileno())
                if digest(backup)!=digest(target):raise BackupError('本地文件正在变化，请关闭占用它的软件后重试')
                shutil.copystat(target,backup)
            no_links(target);os.replace(temp,target)
            return 'restored',str(backup) if backup else None
        finally:
            if temp.exists():temp.unlink()
    def run(self,deep=False):
        try:return self._run(deep)
        except InvalidTag:raise BackupError('解密校验失败：所选密钥不匹配，或备份内容已损坏。未将未验证内容写回原位置。') from None
        finally:
            if isinstance(self.remote,GitHub):self.remote.session.close()
            from history_remote import HistoryRemote
            if isinstance(self.remote,HistoryRemote):self.remote.close()
            self.catalog.clear();self.key=b''
            self.confirm_restore=None
    def _run(self,deep=False):
        review=self.prepare()
        if review['space_errors']:raise BackupError('恢复所需空间不足：'+', '.join(review['space_errors']))
        approve=getattr(self,'confirm_restore',None)
        if approve is None:raise BackupError('恢复必须先由界面确认原路径和同名文件处理方式')
        if not approve(review):raise Paused()
        self.emit('phase','正在恢复到原位置 · 同名本地文件先保留副本')
        result={'operation':'restore','status':'running','finished':None,'files':0,'unchanged':0,'issues':[],'preserved':[],'releases':review['releases']}
        report=Path(self.c['state_dir'])/'last-restore-report.json'
        for i,row in enumerate(review['files']):
            self.check()
            try:
                status,backup=self.install_one(row)
                result['unchanged' if status=='unchanged' else 'files']+=1
                if backup:result['preserved'].append({'source':row['source'],'copy':backup})
            except (OSError,BackupError) as ex:result['issues'].append({'source':row['source'],'error':str(ex)})
            result['finished']=now()
            atomic_json(report,result)
            self.emit('stats',{'stage':'restore_write','staged':i+1,'stage_total':len(review['files']),'changed':len(review['files']),'changed_bytes':review['bytes']})
        result['finished']=now();result['status']='warning' if result['issues'] else 'success';atomic_json(report,result)
        if not result['issues'] and not self.c.get('keep_restore_cache',False):
            try:
                # Recheck installed contents before removing the only downloaded local cache.
                for row in review['files']:
                    self.check();target=original_target(row['source'],self.protected)
                    if not target.is_file() or digest(target)!=row['sha256']:raise BackupError('恢复后的文件已变化，保留缓存以便重试')
                result['cache_freed_bytes']=clean_restore(self.base,self.c['state_dir']);result['cache_cleaned']=True
            except (OSError,ValueError,BackupError) as ex:
                result['cleanup_error']=str(ex);self.emit('log','恢复缓存暂未清理：'+str(ex))
            atomic_json(report,result)
        return result
