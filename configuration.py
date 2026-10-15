"""Portable configuration, strict repository parsing, and private local state."""
from __future__ import annotations
import csv
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

APP_NAME = 'CloudVault'

def read_recovery_key(path):
    """Bounded read: a key is 64 hexadecimal characters, never an arbitrary file."""
    try:
        with open(path,'rb') as f:raw=f.read(257)
        text=raw.decode('ascii').strip()
    except (OSError,UnicodeError):raise ValueError('无法读取密钥文件，请选择备份时保存的恢复密钥文本文件。') from None
    if len(raw)>256 or not re.fullmatch(r'[0-9a-fA-F]{64}',text):raise ValueError('密钥格式错误：应为 64 个十六进制字符的恢复密钥文件。')
    return bytes.fromhex(text)

def app_home():
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / APP_NAME
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'cloud-vault'

def parse_repo(value):
    value = value.strip()
    if value.startswith('https://'):
        u = urlsplit(value)
        if u.netloc.lower() != 'github.com' or u.query or u.fragment:
            raise ValueError('请填写 github.com 上的仓库主页地址，不含查询参数或登录信息。')
        value = u.path.strip('/')
    if value.endswith('.git'): value = value[:-4]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', value):
        raise ValueError('仓库格式应为 https://github.com/用户名/仓库名 或 用户名/仓库名。')
    if value.split('/')[1] in {'.', '..'}: raise ValueError('无效仓库名。')
    return value

def find_gh():
    found = shutil.which('gh')
    if found: return found
    if os.name == 'nt':
        candidate = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'GitHub CLI' / 'gh.exe'
        if candidate.is_file(): return str(candidate)
    raise ValueError('未找到 GitHub CLI。请先从 https://cli.github.com 安装，并登录。')

def protect(path, directory=False):
    """Limit local metadata/key access to the current OS account."""
    path = Path(path)
    if os.name != 'nt':
        path.chmod(0o700 if directory else 0o600); return
    flags = subprocess.CREATE_NO_WINDOW
    who = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                         text=True, check=True, timeout=15, creationflags=flags)
    sid = next(csv.reader(io.StringIO(who.stdout)))[1]
    if not re.fullmatch(r'S-1-[0-9-]+', sid): raise ValueError('无法确定当前用户权限。')
    grant = '*' + sid + (':(OI)(CI)F' if directory else ':F')
    result = subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', grant],
                            capture_output=True, timeout=15, creationflags=flags)
    if result.returncode: raise ValueError('无法保护本地密钥目录，请检查文件系统权限。')

def write_key(path, key):
    """Exclusive creation: an existing recovery key is never overwritten."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(key.hex() + '\n'); f.flush(); os.fsync(f.fileno())
    protect(path)

def validate_config(config):
    c = dict(config)
    c['repo'] = parse_repo(c['repo'])
    for name in ('state_dir', 'key_file'):
        if not Path(c[name]).is_absolute(): raise ValueError(name + ' 必须是绝对路径。')
    if not c.get('roots') or not isinstance(c['roots'], list): raise ValueError('至少选择一个备份目录。')
    if any(not isinstance(p, str) or not Path(p).is_absolute() for p in c['roots']):
        raise ValueError('备份目录必须是绝对路径。')
    if type(c.get('part_mib', 64)) is not int or not 1 <= c.get('part_mib', 64) <= 512:
        raise ValueError('分包大小必须介于 1 和 512 MiB。')
    c['exclude_paths'] = list(dict.fromkeys(c.get('exclude_paths', []) + [c['state_dir'], c['key_file'], str(app_home())]))
    return c

def load_config(path):
    return validate_config(json.loads(Path(path).read_text(encoding='utf-8')))
