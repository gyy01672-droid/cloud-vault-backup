"""Incremental, authenticated-encrypted, checkpointed GitHub backup engine."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
import tempfile
import re
from urllib.parse import urlsplit
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from configuration import validate_config, parse_repo

MAGIC = b'PCBK001\n'
IO = 1024 * 1024
UTC = dt.timezone.utc
SKIP_DIRS = {'.git', '.ssh', '.aws', '.azure', '.kube', '.sandbox-secrets', 'node_modules',
             '.venv', 'venv', '__pycache__', '.cache', '.pytest_cache', '.mypy_cache',
             '.ruff_cache', '.next', 'site-packages', 'browser', 'cefflashbrowser',
             'netsarang computer', 'todesk', 'flashbrowser', 'cache', 'caches',
             'logs', 'log', 'nt_temp', 'temp', 'login', 'cookies', 'session-env', 'shell-snapshots'}

class Paused(Exception): pass
class BackupError(Exception): pass

def now(): return dt.datetime.now(UTC).isoformat(timespec='seconds')
def norm(path): return os.path.normcase(os.path.abspath(path))
def contained(path, parent):
    try: return os.path.commonpath([norm(path), norm(parent)]) == norm(parent)
    except ValueError: return False
def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(IO), b''): h.update(b)
    return h.hexdigest()
def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with open(temp, 'w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
def read_json(path, default=None):
    if not Path(path).exists(): return default
    return json.loads(Path(path).read_text(encoding='utf-8'))
def encrypt(src, dst, key):
    head = MAGIC + os.urandom(12)
    e = Cipher(algorithms.AES(key), modes.GCM(head[8:])).encryptor()
    e.authenticate_additional_data(head)
    temp = Path(str(dst) + '.tmp')
    with open(src, 'rb') as inp, open(temp, 'wb') as out:
        out.write(head)
        for b in iter(lambda: inp.read(IO), b''): out.write(e.update(b))
        out.write(e.finalize()); out.write(e.tag); out.flush(); os.fsync(out.fileno())
    os.replace(temp, dst)
def decrypt(src, dst, key):
    size = Path(src).stat().st_size
    if size < 36: raise BackupError('备份包不完整')
    temp = None
    try:
        with open(src, 'rb') as inp, tempfile.NamedTemporaryFile(dir=Path(dst).parent, prefix='.decrypt-', delete=False) as out:
            temp = Path(out.name); head = inp.read(20)
            if head[:8] != MAGIC: raise BackupError('无法识别备份格式')
            inp.seek(-16, 2); tag = inp.read(16); inp.seek(20)
            d = Cipher(algorithms.AES(key), modes.GCM(head[8:], tag)).decryptor()
            d.authenticate_additional_data(head); left = size - 36
            while left:
                b = inp.read(min(left, IO))
                if not b: raise BackupError('备份包被截断')
                left -= len(b); out.write(d.update(b))
            out.write(d.finalize()); out.flush(); os.fsync(out.fileno())
        os.replace(temp, dst)
    finally:
        if temp and temp.exists(): temp.unlink()

def exclusion(path, config, attributes=0):
    p = Path(path); name = p.name.lower(); parts = {x.lower() for x in p.parts}
    if any(contained(p, x) for x in config['exclude_paths']): return '备份工具及其缓存'
    if parts & SKIP_DIRS: return '凭据、缓存或依赖'
    if any(x in name for x in ('github-recovery-codes', 'recovery-key', '.ssh')): return '凭据'
    if name in {'auth.json', 'auth-profiles.json', 'credentials', 'credentials.json',
                'cookies', 'login data', 'web data', 'desktop.ini', 'recovery-key.txt'}: return '凭据或系统文件'
    if name == 'models.json' and '.openclaw' in parts: return '模型服务凭据'
    if name.startswith('.env') or name.endswith(('.pem', '.pfx', '.p12', '.key', '.lic')): return '凭据'
    if name.endswith(('.lnk', '.crdownload', '.tmp')): return '临时文件或快捷方式'
    if name.endswith(('.exe', '.dll', '.msi', '.msix', '.appx', '.iso')) and 'outputs' not in parts: return '可重装程序'
    if name in {'flashbrowser.zip', 'tectonic.zip'}: return '可重装程序'
    if name.endswith('.conf') and 'shadowrocket' in name: return '代理凭据'
    if 'all_users' in parts or ('nt_qq' in parts and 'global' in parts): return '聊天应用缓存'
    if attributes & 0x1000: return '未下载的云文件'
    return None

class Index:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, source TEXT, size INTEGER, mtime_ns INTEGER, sha256 TEXT, tag TEXT, present INTEGER NOT NULL DEFAULT 1)')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        self.db.commit()
    def _put(self, row, tag):
        self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,1)',
                        (norm(row['source']), row['source'], row['size'], row['mtime_ns'], row['sha256'], tag))
    def all(self):
        return {r[0]: dict(zip(('path', 'source', 'size', 'mtime_ns', 'sha256', 'tag', 'present'), r))
                for r in self.db.execute('SELECT * FROM files')}
    def unchanged_content(self, row):
        with self.db:
            self.db.execute('UPDATE files SET size=?,mtime_ns=? WHERE path=?',
                            (row['size'], row['mtime_ns'], norm(row['source'])))
    def commit(self, snapshot):
        with self.db:
            for row in snapshot['files']: self._put(row, snapshot['tag'])
            for row in snapshot['deleted']:
                self.db.execute('UPDATE files SET present=0,tag=? WHERE path=?', (snapshot['tag'], norm(row['source'])))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('last_tag',?)",(snapshot['tag'],))
    def last_tag(self):
        row=self.db.execute("SELECT value FROM meta WHERE key='last_tag'").fetchone()
        return row[0] if row else None
    def close(self): self.db.close()

class ProgressFile:
    def __init__(self, path, callback, cancel):
        self.f = open(path, 'rb'); self.size = Path(path).stat().st_size
        self.callback = callback; self.cancel = cancel; self.sent = 0
    def __len__(self): return self.size
    def read(self, size=-1):
        if self.cancel.is_set(): raise Paused()
        b = self.f.read(min(size if size >= 0 else IO, IO))
        self.sent += len(b); self.callback(self.sent); return b
    def close(self): self.f.close()

class GitHub:
    def __init__(self, config, emit, cancel):
        self.config = config; self.emit = emit; self.cancel = cancel
        self.prefix = '/repos/' + parse_repo(config['repo'])
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            p = subprocess.run([config['gh'], 'auth', 'token', '--hostname', 'github.com'],
                               capture_output=True, text=True, encoding='utf-8', timeout=30, creationflags=flags)
        except Exception: raise BackupError('无法运行 GitHub CLI，请检查安装路径。') from None
        if p.returncode or not p.stdout.strip(): raise BackupError('GitHub 登录已失效。请在终端运行 gh auth login 后重试。')
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + p.stdout.strip(),
                                     'Accept': 'application/vnd.github+json',
                                     'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'CloudVault-Backup/1.0'})
    def check(self):
        if self.cancel.is_set(): raise Paused()
    def wait(self, seconds):
        if self.cancel.wait(seconds): raise Paused()
    def api(self, method, path, payload=None, missing_ok=False):
        url = 'https://api.github.com' + self.prefix + path
        attempts = 8 if method in ('GET', 'PATCH', 'DELETE') else 1
        for attempt in range(attempts):
            self.check()
            try:
                r = self.session.request(method, url, json=payload, timeout=(15, 90), allow_redirects=False)
                if r.status_code == 404 and missing_ok: return None
                if r.status_code == 404 and method == 'DELETE': return None
                if r.status_code in (401, 403) and not (r.headers.get('X-RateLimit-Remaining') == '0' or r.headers.get('Retry-After')):
                    raise BackupError('GitHub 权限不足或登录失效，请检查 gh auth status。')
                if r.status_code >= 500 or r.status_code in (408, 429) or r.headers.get('X-RateLimit-Remaining') == '0':
                    raise requests.ConnectionError('GitHub 暂时不可用或限流')
                if r.status_code >= 400: raise BackupError(f'GitHub 返回 HTTP {r.status_code}，未更新备份索引。')
                if 300 <= r.status_code < 400: raise BackupError('GitHub 返回了意外跳转，已停止发送凭据。')
                return r.json() if r.content else None
            except requests.RequestException:
                if attempt == attempts - 1: raise BackupError('网络暂不可用，进度已保存；重新打开即可继续。') from None
                delay = min(60, 2 ** (attempt + 1))
                self.emit('log', f'连接中断，{delay} 秒后重试…'); self.wait(delay)
    def private(self):
        repo = self.api('GET', '')
        if not repo.get('private'): raise BackupError('备份仓库不是私密仓库，已停止上传。')
        if self.config.get('repo_id') and repo['id'] != self.config['repo_id']:
            raise BackupError('目标仓库的身份已变化，不能使用旧索引；请创建新的配置。')
        self.default_branch = repo.get('default_branch')
        return repo
    def release(self, snapshot):
        def find():
            if snapshot.get('release_id'):
                found = self.api('GET', '/releases/' + str(snapshot['release_id']), missing_ok=True)
                if found and found['tag_name'] == snapshot['tag']: return found
            found = self.api('GET', '/releases/tags/' + snapshot['tag'], missing_ok=True)
            if found: return found
            page = 1
            while True:
                rows = self.api('GET', f'/releases?per_page=100&page={page}')
                for r in rows:
                    if r['tag_name'] == snapshot['tag']: return r
                if len(rows) < 100: return None
                page += 1
        release = find()
        if release is None:
            # A previous create may have succeeded despite a lost response: always search before retrying.
            payload = {'tag_name': snapshot['tag'], 'name': '加密备份 ' + snapshot['created'],
                       'draft': True, 'body': '加密增量备份上传中。完成前请勿视为完整快照。'}
            if getattr(self, 'default_branch', None): payload['target_commitish'] = self.default_branch
            try: release = self.api('POST', '/releases', payload)
            except BackupError:
                release = find()
                if release is None: raise
        return release
    def assets(self, release):
        result = {}; page = 1
        while True:
            rows = self.api('GET', f'/releases/{release["id"]}/assets?per_page=100&page={page}')
            result.update({r['name']: r for r in rows})
            if len(rows) < 100: return result
            page += 1
    def upload(self, release, asset, path, callback):
        expected = 'sha256:' + asset['sha256']
        url = release['upload_url'].split('{', 1)[0]
        if urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'uploads.github.com':
            raise BackupError('上传地址校验失败')
        for attempt in range(10):
            self.check()
            self.private()
            remote = self.assets(release).get(asset['name'])
            if remote:
                if remote.get('digest') == expected and remote['size'] == asset['size'] and remote.get('state') == 'uploaded': return remote
                # Only replace a failed/incomplete asset with this exact run's generated name.
                self.api('DELETE', f'/releases/assets/{remote["id"]}')
            stream = ProgressFile(path, callback, self.cancel)
            try:
                r = self.session.post(url, params={'name': asset['name']}, data=stream,
                                      headers={'Content-Type': 'application/octet-stream', 'Content-Length': str(asset['size'])},
                                      timeout=(15, 90), allow_redirects=False)
                if r.status_code == 201:
                    remote = r.json()
                    if remote.get('digest') == expected and remote['size'] == asset['size']: return remote
                    raise requests.ConnectionError('上传后校验不匹配')
                limited = r.headers.get('Retry-After') or r.headers.get('X-RateLimit-Remaining') == '0'
                if r.status_code == 401 or (r.status_code == 403 and not limited):
                    raise BackupError('上传权限不足；进度已保存。')
                if r.status_code not in (403, 408, 409, 422, 429, 500, 502, 503, 504):
                    raise BackupError(f'上传返回 HTTP {r.status_code}')
            except requests.RequestException:
                self.check()
            finally: stream.close()
            if attempt == 9: raise BackupError('分包多次上传失败，进度已保存。下次启动会自动续传。')
            delay = min(60, 2 ** (attempt + 1)); callback(0)
            self.emit('log', f'本包传输中断，{delay} 秒后检查远端并重传…'); self.wait(delay)

class RunLock:
    def __init__(self, path): self.path=Path(path);self.f=None
    def acquire(self):
        self.f=open(self.path,'a+b');self.f.seek(0)
        if self.path.stat().st_size==0:self.f.write(b'1');self.f.flush();self.f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.f.close();self.f=None;raise BackupError('此配置已有备份任务正在运行。') from None
    def close(self):
        if self.f:self.f.close();self.f=None

class Engine:
    def __init__(self, config, emit=lambda *a: None, cancel=None, remote=None):
        self.c = config = validate_config(config); self.emit = emit; self.cancel = cancel or threading.Event(); self.remote = remote
        self.state = Path(config['state_dir']); self.state.mkdir(parents=True, exist_ok=True)
        self.runs = self.state / 'runs'; self.runs.mkdir(exist_ok=True)
        self.key = bytes.fromhex(Path(config['key_file']).read_text().strip())
        if len(self.key) != 32: raise BackupError('恢复密钥格式不正确')
        self.identity = {'repo':config['repo'].lower(), 'key_id':hashlib.sha256(self.key).hexdigest(), 'repo_id':config.get('repo_id')}
        self.index = Index(self.state / 'index.sqlite')
        self.rate = read_json(self.state / 'speed.json', {}).get('rate', 700 * 1024)
    def check(self):
        if self.cancel.is_set(): raise Paused()
    def log(self, message): self.emit('log', message)
    def scan(self, deep=False):
        old = self.index.all(); seen = {}; issues = []; excluded = 0; roots_ok = []; skipped = []
        self.emit('phase', '扫描最近变动'); self.emit('stats', {'stage': 'scan'})
        def visit(root, directory):
            nonlocal excluded
            self.check()
            try:
                with os.scandir(directory) as entries:
                    for e in entries:
                        self.check(); p = Path(e.path)
                        try:
                            if e.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()): excluded += 1; skipped.append(str(p)); continue
                            st = e.stat(follow_symlinks=False)
                            if exclusion(p, self.c, getattr(st, 'st_file_attributes', 0)): excluded += 1; skipped.append(str(p)); continue
                            if e.is_dir(follow_symlinks=False): visit(root, p)
                            elif e.is_file(follow_symlinks=False):
                                seen[norm(p)] = {'source': str(p), 'root': str(root), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
                                if len(seen) % 250 == 0: self.emit('stats', {'scanned': len(seen)})
                        except OSError as ex: issues.append({'source': str(p), 'error': str(ex), 'root': str(root)})
            except OSError as ex: issues.append({'source': str(directory), 'error': str(ex), 'root': str(root)})
        for source in self.c['roots']:
            before = len(issues); root = Path(source)
            try:
                if root.is_symlink() or (hasattr(root,'is_junction') and root.is_junction()):
                    skipped.append(str(root));excluded+=1;continue
                root_stat=root.stat()
                if exclusion(root, self.c, getattr(root_stat,'st_file_attributes',0)):
                    skipped.append(str(root));excluded+=1;continue
                if root.is_file():
                    st = root.stat(); seen[norm(root)] = {'source': str(root), 'root': str(root), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}
                else: visit(root, root)
            except OSError as ex: issues.append({'source':str(root),'error':str(ex),'root':str(root)})
            if len(issues) == before: roots_ok.append(root)
        changed = []
        for key, row in seen.items():
            prev = old.get(key)
            if deep or not prev or not prev['present'] or (prev['size'], prev['mtime_ns']) != (row['size'], row['mtime_ns']):
                row['kind'] = 'modified' if prev and prev['present'] else 'new'; changed.append(row)
        deleted = [r for k, r in old.items() if r['present'] and k not in seen and not exclusion(r['source'], self.c)
                   and any(contained(r['source'], root) for root in roots_ok)
                   and not any(contained(r['source'], skip) for skip in skipped)
                   and not any(contained(r['source'], x['source']) or contained(r['source'], x['root']) for x in issues)]
        self.emit('stats', {'scanned': len(seen), 'changed': len(changed), 'changed_bytes': sum(r['size'] for r in changed),
                            'estimate': sum(r['size'] for r in changed) / max(self.rate, 1), 'stage': 'prepare'})
        return changed, deleted, issues, excluded
    def make_snapshot(self, deep=False):
        rows, deleted, issues, excluded = self.scan(deep)
        if not rows and not deleted:
            return None, {'status': 'warning' if issues else 'unchanged', 'files': 0, 'issues': issues, 'excluded': excluded, 'finished': now()}
        tag = 'increment-' + dt.datetime.now(UTC).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        folder = self.runs / tag; folder.mkdir()
        snapshot = {'version': 2, 'tag': tag, 'created': now(), 'status': 'building', 'selected': rows, 'deleted': deleted,
                    'issues': issues, 'files': [], 'assets': [], 'excluded': excluded, 'repo': self.c['repo'], 'key_id': self.identity['key_id'],
                    'parent_tag':self.index.last_tag()}
        atomic_json(folder / 'run.json', snapshot)
        return (folder, snapshot), None
    def save(self, folder, snapshot): atomic_json(folder / 'run.json', snapshot)
    def stage(self, folder, snapshot):
        staged = {norm(r['source']): r for r in snapshot['files']}
        old = self.index.all(); spool = folder / 'spool'; spool.mkdir(exist_ok=True)
        self.emit('phase', '读取变动文件并生成安全副本')
        for row in snapshot['selected']:
            self.check(); key = norm(row['source'])
            if key in staged: continue
            file_id = hashlib.sha256(key.encode()).hexdigest()
            dest = spool / file_id; temp = spool / (file_id + '.tmp')
            try:
                p = Path(row['source']); before = p.stat(); h = hashlib.sha256(); size = 0
                if shutil.disk_usage(folder).free < before.st_size + 128 * IO: raise OSError('本地可用空间不足，未修改原文件')
                with open(p, 'rb') as inp, open(temp, 'wb') as out:
                    for b in iter(lambda: inp.read(IO), b''):
                        self.check(); out.write(b); h.update(b); size += len(b)
                after = p.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
                    raise OSError('读取期间文件仍在变化，本轮跳过，下次再备份')
                row = dict(row, size=size, mtime_ns=before.st_mtime_ns, sha256=h.hexdigest(), file_id=file_id)
                prev = old.get(key)
                if prev and prev['present'] and prev['sha256'] == row['sha256']:
                    self.index.unchanged_content(row); row['same_content'] = True
                else: os.replace(temp, dest)
                snapshot['files'].append(row); staged[key] = row
                self.save(folder, snapshot)
                self.emit('stats', {'staged': len(staged), 'stage_total': len(snapshot['selected'])})
            except Paused: raise
            except OSError as ex:
                snapshot['issues'].append({'source': row['source'], 'error': str(ex)})
                snapshot.setdefault('failed_sources', []).append(row['source'])
                self.log('暂未备份：' + Path(row['source']).name)
            finally:
                if temp.exists(): temp.unlink()
        snapshot['files'] = [r for r in snapshot['files'] if not r.get('same_content')]
        snapshot['status'] = 'packing'; self.save(folder, snapshot)
    def package(self, folder, snapshot):
        limit = self.c.get('part_mib', 64) * IO
        self.emit('phase', '加密分包 · 校验可恢复性')
        chunks = []
        for row in snapshot['files']:
            self.check()
            if digest(folder / 'spool' / row['file_id']) != row['sha256']: raise BackupError('暂存副本校验失败，请检查磁盘；未上传损坏数据。')
            for offset in range(0, max(row['size'], 1), limit):
                chunks.append({'file_id': row['file_id'], 'offset': offset, 'size': min(limit, row['size'] - offset)})
        groups = []; group = []; total = 0
        for c in chunks:
            if group and (total + c['size'] > limit or len(group) >= 1000): groups.append(group); group = []; total = 0
            group.append(c); total += c['size']
        if group: groups.append(group)
        assets_dir = folder / 'assets'; assets_dir.mkdir(exist_ok=True)
        existing = {a['name']: a for a in snapshot['assets']}
        for number, group in enumerate(groups, 1):
            self.check(); name = f'part-{number:05d}.pcbackup'; dest = assets_dir / name
            if name in existing and dest.exists() and digest(dest) == existing[name]['sha256']: continue
            if shutil.disk_usage(folder).free < 3*sum(c['size'] for c in group)+16*IO:
                raise BackupError('可用空间不足以加密和校验分包；进度已保存，请释放空间后重试。')
            plain = folder / 'packing.zip'; descriptions = []
            with zipfile.ZipFile(plain, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for c in group:
                    self.check(); arc = f'chunks/{c["file_id"]}/{c["offset"]}'
                    h = hashlib.sha256(); left = c['size']
                    with open(folder / 'spool' / c['file_id'], 'rb') as inp, z.open(arc, 'w') as out:
                        inp.seek(c['offset'])
                        while left:
                            self.check(); b = inp.read(min(left, IO))
                            if not b: raise BackupError('暂存文件不完整，未推进备份索引')
                            h.update(b); out.write(b); left -= len(b)
                    descriptions.append(dict(c, archive_path=arc, sha256=h.hexdigest()))
                z.writestr('_chunks.json', json.dumps(descriptions))
            self.seal(plain, dest)
            a = {'name': name, 'size': dest.stat().st_size, 'sha256': digest(dest), 'chunks': descriptions}
            snapshot['assets'] = [x for x in snapshot['assets'] if x['name'] != name] + [a]
            self.save(folder, snapshot); self.emit('stats', {'packed': number, 'pack_total': len(groups)})
        # The encrypted manifest is the restore plan. No original filenames are published in release notes.
        manifest = {k: snapshot[k] for k in ('version', 'tag', 'created', 'repo', 'files', 'deleted', 'issues')}
        if 'parent_tag' in snapshot:manifest['parent_tag']=snapshot['parent_tag']
        manifest['assets'] = [a for a in snapshot['assets'] if a['name'] != 'manifest.pcbackup']
        plain = folder / 'packing.zip'; dest = assets_dir / 'manifest.pcbackup'
        # Rebuild even after an interrupted packing pass: repaired parts have new nonces/hashes.
        with zipfile.ZipFile(plain, 'w', zipfile.ZIP_DEFLATED) as z: z.writestr('snapshot.json', json.dumps(manifest, ensure_ascii=False))
        self.seal(plain, dest)
        snapshot['assets']=[a for a in snapshot['assets'] if a['name']!='manifest.pcbackup']
        snapshot['assets'].append({'name': dest.name, 'size': dest.stat().st_size, 'sha256': digest(dest)})
        snapshot['status'] = 'uploading'; self.save(folder, snapshot)
    def seal(self, plain, dest):
        encrypt(plain, dest, self.key)
        verify = plain.with_suffix('.verify.zip'); decrypt(dest, verify, self.key)
        if digest(plain) != digest(verify): raise BackupError('加密后恢复校验失败')
        with zipfile.ZipFile(verify) as z:
            if z.testzip(): raise BackupError('压缩包完整性校验失败')
        verify.unlink(); plain.unlink()
    def transfer(self, folder, snapshot):
        if self.remote is None: self.remote = GitHub(self.c, self.emit, self.cancel)
        self.remote.private(); release = self.remote.release(snapshot)
        # Store the release identity so a lost response never advances the local index.
        snapshot['release_id'] = release['id']; self.save(folder, snapshot)
        total = sum(a['size'] for a in snapshot['assets']); completed = 0; wire_bytes = 0
        self.emit('phase', '上传并校验 GitHub 私密备份')
        self.emit('stats', {'changed': len(snapshot['files']), 'changed_bytes': sum(r['size'] for r in snapshot['files']), 'stage': 'upload', 'total': total, 'uploaded': 0, 'inflight': 0, 'wire_bytes': 0, 'retrying': False})
        for a in snapshot['assets']:
            self.check(); path = folder / 'assets' / a['name']
            if digest(path) != a['sha256']: raise BackupError('本地分包损坏，请保留日志后检查磁盘')
            start = time.monotonic(); last_ui = [0.0]; transferred = [0]
            def update(sent):
                nonlocal wire_bytes
                wire_bytes += max(0, sent - transferred[0])
                transferred[0] = sent; elapsed = max(time.monotonic() - start, 0.1)
                if time.monotonic() - last_ui[0] < 0.2 and sent != 0: return
                last_ui[0] = time.monotonic(); rate = sent / elapsed if sent else self.rate
                self.emit('stats', {'stage': 'upload', 'uploaded': completed, 'inflight': sent, 'total': total,
                                    'rate': rate, 'estimate': max(0, total - completed - sent) / max(rate, 1), 'part': a['name'],
                                    'wire_bytes': wire_bytes, 'retrying': sent == 0})
            self.remote.upload(release, a, path, update)
            if transferred[0]: self.rate = a['size'] / max(time.monotonic() - start, 0.1)
            completed += a['size']; a['verified'] = True; self.save(folder, snapshot)
            self.emit('stats', {'stage': 'upload', 'uploaded': completed, 'inflight': 0, 'total': total, 'rate': self.rate,
                                'estimate': (total - completed) / max(self.rate, 1), 'wire_bytes': wire_bytes, 'retrying': False})
            self.log('已校验 ' + a['name'])
        self.remote.private()
        remote_assets=self.remote.assets(release)
        for asset in snapshot['assets']:
            found=remote_assets.get(asset['name'],{})
            if found.get('state')!='uploaded' or found.get('size')!=asset['size'] or found.get('digest')!='sha256:'+asset['sha256']:
                raise BackupError('最终远端校验失败，未发布完成版本、未更新成功索引；可重试。')
        self.remote.api('PATCH', f'/releases/{release["id"]}', {'draft': False, 'body':
                        f'增量备份已完成。{len(snapshot["files"])} 个新增/修改文件，{len(snapshot["deleted"])} 条删除记录，{len(snapshot["issues"])} 条待检查项。所有附件已核对 SHA-256。请使用独立保管的原恢复密钥与 restore_incremental.py。历史备份不会删除。'})
        self.index.commit(snapshot)
        snapshot['status'] = 'completed'; snapshot['finished'] = now(); self.save(folder, snapshot)
        atomic_json(self.state / 'speed.json', {'rate': self.rate})
        # Remove only this run's plaintext staging files; retained encrypted parts support audit/recovery.
        for row in snapshot['files']:
            p = folder / 'spool' / row['file_id']
            if p.exists(): p.unlink()
        return {'status': 'warning' if snapshot['issues'] else 'success', 'files': len(snapshot['files']),
                'bytes': sum(r['size'] for r in snapshot['files']), 'uploaded_bytes': total,
                'deleted': len(snapshot['deleted']), 'issues': snapshot['issues'], 'finished': now(),
                'tag': snapshot['tag'], 'url': f'https://github.com/{self.c["repo"]}/releases/tag/{snapshot["tag"]}'}
    def process(self, folder, snapshot):
        if snapshot.get('repo','').lower()!=self.c['repo'].lower() or snapshot.get('key_id',self.identity['key_id'])!=self.identity['key_id']:
            raise BackupError('未完成任务属于其他仓库或密钥，拒绝混用。')
        if snapshot['status'] == 'building': self.stage(folder, snapshot)
        if not snapshot['files'] and not snapshot['deleted']:
            snapshot['status'] = 'completed'; snapshot['finished'] = now(); self.save(folder, snapshot)
            return {'status': 'warning' if snapshot['issues'] else 'unchanged', 'files': 0, 'issues': snapshot['issues'], 'finished': now()}
        if snapshot['status'] == 'packing': self.package(folder, snapshot)
        return self.transfer(folder, snapshot)
    def run(self, deep=False):
        reports = [];lock=RunLock(self.state/'engine.lock')
        try:
            lock.acquire()
            identity=read_json(self.state/'identity.json')
            if identity and identity!=self.identity:raise BackupError('此状态目录已绑定其他仓库或恢复密钥，不能直接修改配置复用。')
            if not identity:atomic_json(self.state/'identity.json',self.identity)
            for folder in sorted(self.runs.iterdir()):
                snapshot = read_json(folder / 'run.json')
                if snapshot and snapshot['status'] != 'completed':
                    self.log('发现未完成备份，先恢复 ' + snapshot['tag'])
                    reports.append(self.process(folder, snapshot))
            pending, report = self.make_snapshot(deep)
            if pending:
                folder, snapshot = pending; reports.append(self.process(folder, snapshot))
            else: reports.append(report)
            result = {'finished': now(), 'runs': reports, 'status': 'warning' if any(r['status'] == 'warning' for r in reports) else 'success'}
            atomic_json(self.state / 'last-report.json', result)
            self.emit('done', result); return result
        finally:
            self.index.close();lock.close()
            if isinstance(self.remote,GitHub):self.remote.session.close()
