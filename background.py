"""Window-independent worker, one-second heartbeats, and explicit scan approval."""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import uuid
from configuration import load_config
from engine import Engine, Paused, BackupError, atomic_json, read_json, now

TERMINAL = {'done', 'error', 'paused'}

def safe_read(path, default=None):
    try: return read_json(path, default)
    except (OSError, ValueError): return default

class WorkerLock:
    def __init__(self, path): self.path=Path(path);self.f=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
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
            self.close();return False
        return True
    def close(self):
        if self.f:self.f.close();self.f=None

def worker_active(state):
    lock=WorkerLock(Path(state)/'background.lock')
    try:return not lock.acquire()
    finally:lock.close()

class PowerGuard:
    """Hold an OS sleep request on the worker thread; never change a power plan."""
    def __init__(self):self.active=False;self.kernel=None;self.saved=None;self.throttle_changed=False
    def acquire(self):
        if os.name!='nt':return {'sleep_inhibited':False,'platform':sys.platform}
        from ctypes import wintypes
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.SetThreadExecutionState.argtypes=[wintypes.DWORD]
        self.kernel.SetThreadExecutionState.restype=wintypes.DWORD
        self.active=bool(self.kernel.SetThreadExecutionState(0x80000001))
        class Throttling(ctypes.Structure):
            _fields_=[('Version',wintypes.DWORD),('ControlMask',wintypes.DWORD),('StateMask',wintypes.DWORD)]
        try:
            self.kernel.GetCurrentProcess.restype=wintypes.HANDLE
            self.kernel.GetProcessInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            self.kernel.SetProcessInformation.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
            handle=self.kernel.GetCurrentProcess();saved=Throttling(1,0,0)
            if self.kernel.GetProcessInformation(handle,4,ctypes.byref(saved),ctypes.sizeof(saved)):
                self.saved=saved
                state=Throttling(1,saved.ControlMask|1,saved.StateMask&~1)
                self.throttle_changed=bool(self.kernel.SetProcessInformation(handle,4,ctypes.byref(state),ctypes.sizeof(state)))
        except (AttributeError,OSError):pass
        return {'sleep_inhibited':self.active,'background_throttling_disabled':self.throttle_changed}
    def release(self):
        if self.kernel:
            if self.active:self.kernel.SetThreadExecutionState(0x80000000)
            if self.throttle_changed and self.saved is not None:
                self.kernel.SetProcessInformation(self.kernel.GetCurrentProcess(),4,ctypes.byref(self.saved),ctypes.sizeof(self.saved))
        self.active=False;self.throttle_changed=False

class Worker:
    def __init__(self, config, run_id, deep=False, engine_factory=Engine):
        self.config=config;self.run_id=run_id;self.deep=deep;self.engine_factory=engine_factory
        self.state=Path(config['state_dir']);self.state.mkdir(parents=True,exist_ok=True)
        self.cancel=threading.Event();self.stop=threading.Event();self.mutex=threading.RLock();self.power=PowerGuard()
        self.data={'run_id':run_id,'pid':os.getpid(),'status':'running','phase':'后台任务正在启动','phase_id':0,'stats':{},'logs':[],'log_id':0,'review_id':None}
    def emit(self,kind,value):
        with self.mutex:
            if kind=='phase':self.data.update(phase=value,phase_id=self.data['phase_id']+1,stats={})
            elif kind=='stats':self.data['stats'].update(value)
            elif kind=='log':
                self.data['log_id']+=1;self.data['logs'].append([self.data['log_id'],value]);self.data['logs']=self.data['logs'][-60:]
    def save_status(self):
        with self.mutex:
            self.data['heartbeat']=time.time();atomic_json(self.state/'worker-status.json',self.data)
    def heartbeat(self):
        while not self.stop.wait(1):
            command=safe_read(self.state/'worker-command.json',{})
            if command.get('run_id')==self.run_id and command.get('command')=='pause':self.cancel.set()
            try:self.save_status()
            except OSError:pass  # Engine checkpoints remain authoritative if the status file is busy.
    def choose(self, rows):
        if not rows:return []
        request_id=uuid.uuid4().hex
        request={'run_id':self.run_id,'request_id':request_id,'files':rows}
        atomic_json(self.state/'selection-request.json',request)
        self.emit('phase','扫描完成 · 等待选择要上传的文件')
        self.emit('stats',{'stage':'review','changed':len(rows),'changed_bytes':sum(r['size'] for r in rows)})
        with self.mutex:self.data['review_id']=request_id
        self.power.release();self.save_status()
        try:
            while not self.cancel.wait(.25):
                reply=safe_read(self.state/'selection-response.json',{})
                if reply.get('run_id')!=self.run_id or reply.get('request_id')!=request_id:continue
                selected=reply.get('selected')
                allowed={row['selection_id'] for row in rows}
                if not isinstance(selected,list) or any(not isinstance(i,str) or i not in allowed for i in selected):
                    raise BackupError('文件选择结果无效，未开始封包或上传。')
                return set(selected)
            raise Paused()
        finally:
            with self.mutex:self.data['review_id']=None
            self.data['power']=self.power.acquire()
    def run(self):
        lock=WorkerLock(self.state/'background.lock')
        if not lock.acquire():return 2
        beat=None;terminal='error';value='后台任务未能启动'
        try:
            self.data['power']=self.power.acquire()
            if os.name=='nt' and not self.data['power']['sleep_inhibited']:
                self.emit('log','Windows 未接受防休眠请求，请检查系统电源策略。')
            self.save_status();beat=threading.Thread(target=self.heartbeat,daemon=True);beat.start()
            engine=self.engine_factory(self.config,self.emit,self.cancel)
            engine.select_files=self.choose
            value=engine.run(self.deep);terminal='done'
        except Paused:
            terminal='paused';value='进度已保存，下次继续时会跳过已校验的分包。'
            atomic_json(self.state/'last-report.json',{'status':'paused','finished':now(),'runs':[]})
        except Exception as ex:
            value=str(ex) or type(ex).__name__
            (self.state/'last-error.log').write_text(traceback.format_exc(),encoding='utf8')
            atomic_json(self.state/'last-report.json',{'status':'error','finished':now(),'error':value,'runs':[]})
        finally:
            self.power.release();self.stop.set()
            if beat:beat.join(timeout=3)
            with self.mutex:self.data.update(status=terminal,result=value,review_id=None,power={'sleep_inhibited':False})
            try:self.save_status()
            finally:lock.close()
        return 0 if terminal in {'done','paused'} else 1

class BackgroundClient:
    def __init__(self,config_path,state):
        self.config_path=Path(config_path).resolve();self.state=Path(state);self.process=None;self.run_id=None
    def start(self,deep=False):
        self.state.mkdir(parents=True,exist_ok=True)
        if worker_active(self.state):
            self.run_id=safe_read(self.state/'worker-status.json',{}).get('run_id');return False
        self.run_id=uuid.uuid4().hex
        executable=Path(sys.executable)
        if os.name=='nt' and executable.name.lower()=='python.exe' and executable.with_name('pythonw.exe').exists():
            executable=executable.with_name('pythonw.exe')
        command=[str(executable),str(Path(__file__).resolve()),'--config',str(self.config_path),'--run-id',self.run_id]
        if deep:command.append('--deep')
        options={'stdin':subprocess.DEVNULL,'stdout':subprocess.DEVNULL,'stderr':subprocess.DEVNULL,'close_fds':True}
        if os.name=='nt':options['creationflags']=subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP
        else:options['start_new_session']=True
        self.process=subprocess.Popen(command,**options)
        return True
    def status(self):
        value=safe_read(self.state/'worker-status.json',{})
        if self.run_id is None and worker_active(self.state):self.run_id=value.get('run_id')
        return value if value.get('run_id')==self.run_id else {}
    def pause(self):atomic_json(self.state/'worker-command.json',{'run_id':self.run_id,'command':'pause'})
    def approve(self,request_id,selected):
        atomic_json(self.state/'selection-response.json',{'run_id':self.run_id,'request_id':request_id,'selected':list(selected)})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True,type=Path);parser.add_argument('--run-id',required=True);parser.add_argument('--deep',action='store_true')
    args=parser.parse_args();config=load_config(args.config)
    return Worker(config,args.run_id,args.deep).run()

if __name__=='__main__':raise SystemExit(main())
