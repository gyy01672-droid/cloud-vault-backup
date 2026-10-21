"""Confined cache deletion; never delete originals, keys, indexes, or remote assets."""
import os,re
from pathlib import Path

def confined(path,root):
    path=Path(path).absolute();root=Path(root).absolute()
    if path==root or not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):raise ValueError('清理路径越界')
    for p in [path,*path.parents]:
        try:st=p.lstat()
        except FileNotFoundError:continue
        if p.is_symlink() or getattr(st,'st_file_attributes',0)&0x400:raise ValueError('缓存清理拒绝链接或目录连接')
    return path

def clean_backup(folder,state,snapshot,discard=False):
    runs=Path(state).absolute()/'runs';folder=confined(folder,runs)
    if folder.parent!=runs or folder.name!=snapshot['tag'] or snapshot.get('status')!=('cancelled' if discard else 'completed'):raise ValueError('任务状态不允许清理缓存')
    candidates=[]
    for sub,pattern in [('assets',r'(?:part-[0-9]{5,}|manifest)\.pcbackup(?:\.tmp)?'),('spool',r'[0-9a-f]{64}(?:\.tmp)?')]:
        directory=confined(folder/sub,runs)
        if directory.exists():
            for p in directory.iterdir():
                confined(p,runs)
                if p.is_file() and re.fullmatch(pattern,p.name):candidates.append(p)
    for name in ('packing.zip','packing.verify.zip'):
        p=confined(folder/name,runs)
        if p.is_file():candidates.append(p)
    for p in folder.glob('.decrypt-*'):
        confined(p,runs)
        if p.is_file():candidates.append(p)
    freed=0
    for p in candidates:confined(p,runs);size=p.stat().st_size;p.unlink();freed+=size
    return freed

def clean_restore(base,state):
    root=Path(state).absolute()/'restore';base=confined(base,root)
    if base.parent!=root or not re.fullmatch('[0-9a-f]{24}',base.name):raise ValueError('不是本程序的恢复缓存目录')
    paths=list(base.rglob('*'))
    # Validate the entire tree before removing any member.
    for p in paths:confined(p,root)
    freed=0
    for p in paths:
        if p.is_file():confined(p,root);size=p.stat().st_size;p.unlink();freed+=size
    for p in sorted((p for p in paths if p.is_dir()),key=lambda p:len(p.parts),reverse=True):p.rmdir()
    base.rmdir();return freed
