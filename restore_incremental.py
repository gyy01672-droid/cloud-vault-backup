"""Restore the first release, then apply subsequent releases chronologically.
python restore_incremental.py --key recovery-key.txt --archives RELEASE_FOLDER --dest restored --apply
Without --apply, existing files with different contents are never overwritten.
Deleted paths are reported, never automatically removed.
"""
import argparse, hashlib, json, os, re
from pathlib import Path, PureWindowsPath, PurePosixPath
import shutil, tempfile, zipfile
from engine import decrypt, digest, IO, BackupError, read_json

def mapped_target(destination, source):
    if not isinstance(source,str) or '\x00' in source:raise BackupError('不安全的原始路径')
    original=PureWindowsPath(source)
    if original.is_absolute():
        if re.fullmatch('[A-Za-z]:',original.drive):parts=[original.drive[0].upper(),*original.parts[1:]]
        elif original.drive.startswith('\\\\'):
            network=original.drive[2:].split('\\')
            if len(network)!=2:raise BackupError('不安全的网络路径')
            parts=['UNC',*network,*original.parts[1:]]
        else:raise BackupError('不安全的原始路径')
    elif source.startswith('/') and '\\' not in source:
        parts=['posix',*PurePosixPath(source).parts[1:]]
    else:raise BackupError('不安全的原始路径')
    for part in parts:
        if part in {'','.', '..'} or part.endswith((' ','.')) or re.search(r'[<>:"|?*\x00-\x1f/\\]',part):
            raise BackupError('不安全的原始路径')
        if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',part):
            raise BackupError('恢复路径包含保留设备名')
    target=destination/'data'/Path(*parts)
    check_target(destination,target)
    return target

def check_target(destination,target):
    if not target.resolve().is_relative_to(destination):raise BackupError('不安全的恢复路径')
    for p in [target,*target.parents]:
        if p==destination:break
        if p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()):raise BackupError('恢复目标不能包含符号链接或目录连接')

def restore(archives, destination, key, apply=False, cancel=None, temp_root=None, target_mapper=None):
    def check():
        if cancel is not None and cancel.is_set():
            from engine import Paused
            raise Paused()
    archives = Path(archives); destination = Path(destination).resolve()
    with tempfile.TemporaryDirectory(prefix='backup-restore-',dir=temp_root) as temp:
        temp = Path(temp); plain = temp / 'manifest.zip'
        decrypt(archives / 'manifest.pcbackup', plain, key)
        with zipfile.ZipFile(plain) as z: plan = json.loads(z.read('snapshot.json'))
        if plan['version'] != 2: raise BackupError('不支持此增量格式')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,120}',plan['tag']):raise BackupError('不安全的版本标识')
        receipt=destination/'.cloud-vault-restore.json';check_target(destination,receipt)
        previous=read_json(receipt,{})
        if 'parent_tag' in plan and previous.get('tag') not in (plan['parent_tag'],plan['tag']):
            raise BackupError('恢复顺序不完整：请先恢复此版本的前一个备份 '+str(plan['parent_tag']))
        files = {r['file_id']: r for r in plan['files']}; offsets = {key: [] for key in files}
        if len(files)!=len(plan['files']):raise BackupError('重复文件标识')
        if any(not re.fullmatch('[0-9a-f]{64}', key) for key in files): raise BackupError('无效文件标识')
        if any(type(r['size']) is not int or r['size']<0 for r in files.values()):raise BackupError('无效文件大小')
        names=[a['name'] for a in plan['assets']]
        if len(set(names))!=len(names):raise BackupError('重复附件名')
        for asset in plan['assets']:
            check()
            if not re.fullmatch(r'part-[0-9]{5,}\.pcbackup',asset['name']):raise BackupError('不安全的附件名')
            source = archives / asset['name']
            if source.is_symlink() or digest(source) != asset['sha256']: raise BackupError('附件缺失或校验不匹配')
            plain = temp / 'part.zip'; decrypt(source, plain, key)
            with zipfile.ZipFile(plain) as z:
                descriptions = json.loads(z.read('_chunks.json'))
                if descriptions != asset['chunks']: raise BackupError('分片清单不匹配')
                for c in descriptions:
                    if c['file_id'] not in files or type(c['offset']) is not int or type(c['size']) is not int or c['offset'] < 0 or c['size'] < 0: raise BackupError('无效分片')
                    if c['offset'] + c['size'] > files[c['file_id']]['size']: raise BackupError('分片超出文件边界')
                    output = temp / c['file_id']; h = hashlib.sha256(); size = 0
                    with open(output, 'r+b' if output.exists() else 'wb') as out, z.open(c['archive_path']) as inp:
                        out.seek(c['offset'])
                        for b in iter(lambda: inp.read(IO), b''):
                            check()
                            size += len(b)
                            if size>c['size']:raise BackupError('分片超出声明大小')
                            h.update(b);out.write(b)
                    if size != c['size'] or h.hexdigest() != c['sha256']: raise BackupError('分片内容校验失败')
                    offsets[c['file_id']].append((c['offset'], c['size']))
            plain.unlink()
        # Validate all files before touching the requested output directory.
        for file_id, row in files.items():
            position = 0
            for offset, length in sorted(offsets[file_id]):
                if offset != position: raise BackupError('分片缺失或重叠')
                position += length
            if position != row['size'] or digest(temp / file_id) != row['sha256']: raise BackupError('完整文件哈希校验失败')
        targets = [];seen=set()
        for file_id, row in files.items():
            target=(target_mapper(row['source']) if target_mapper else mapped_target(destination,row['source']))
            check_target(destination,target)
            identity=str(target).casefold()
            if identity in seen:raise BackupError('多个文件映射到同一恢复路径')
            seen.add(identity)
            if target.exists() and digest(target) != row['sha256'] and not apply: raise FileExistsError('需要 --apply 才能覆盖：' + str(target))
            targets.append((file_id, row, target))
        for file_id, row, target in targets:
            check()
            target.parent.mkdir(parents=True, exist_ok=True)
            check_target(destination,target)
            fd,name=tempfile.mkstemp(dir=target.parent,prefix='.restore-');os.close(fd);staging=Path(name)
            try:shutil.copyfile(temp / file_id, staging);os.replace(staging,target)
            finally:
                if staging.exists():staging.unlink()
            os.utime(target, ns=(row['mtime_ns'], row['mtime_ns']))
        destination.mkdir(parents=True, exist_ok=True)
        report=destination/(plan['tag']+'-deletions.json');check_target(destination,report)
        fd,name=tempfile.mkstemp(dir=destination,prefix='.report-')
        try:
            with os.fdopen(fd,'w',encoding='utf8') as f:json.dump(plan['deleted'],f,ensure_ascii=False,indent=2)
            os.replace(name,report)
        finally:
            if Path(name).exists():Path(name).unlink()
        fd,name=tempfile.mkstemp(dir=destination,prefix='.receipt-')
        try:
            with os.fdopen(fd,'w',encoding='utf8') as f:json.dump({'tag':plan['tag'],'repo':plan.get('repo')},f)
            os.replace(name,receipt)
        finally:
            if Path(name).exists():Path(name).unlink()
        return {'files': len(files), 'deletions_recorded': len(plan['deleted']), 'tag': plan['tag']}

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--key', required=True); p.add_argument('--archives', required=True)
    p.add_argument('--dest', required=True); p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    print(restore(a.archives, a.dest, bytes.fromhex(Path(a.key).read_text().strip()), a.apply))
