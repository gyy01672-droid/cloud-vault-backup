"""Bounded local I/O workers; only the coordinator mutates SQLite and checkpoints."""
import hashlib,os,shutil,time,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED

def stage_files(engine,folder,snapshot):
    from engine import IO,Paused,norm
    spool=folder/'spool';spool.mkdir(exist_ok=True)
    staged={norm(r['source']) for r in snapshot['files']}
    failed=set(snapshot.get('stage_failed',[]))|{norm(p) for p in snapshot.get('failed_sources',[])};staged.update(failed)
    count=len(staged);last_save=time.monotonic();dirty=0
    workers=engine.c.get('stage_workers',4)
    reservation=threading.Lock();reserved=0
    engine.emit('phase',f'并行核对变动文件并生成安全副本 · {workers} 路')
    def prepare(row,prev):
        nonlocal reserved
        engine.check();p=Path(row['source']);before=p.stat();file_id=hashlib.sha256(norm(p).encode()).hexdigest()
        temp=spool/(file_id+'.tmp');dest=spool/file_id
        allowance=0
        def stable():
            after=p.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise OSError('读取期间文件变化，本轮跳过')
        try:
            # Existing files: hash first, avoiding an unnecessary full plaintext copy.
            if prev and prev['present']:
                h=hashlib.sha256()
                with open(p,'rb') as inp:
                    for b in iter(lambda:inp.read(IO),b''):engine.check();h.update(b)
                stable()
                if h.hexdigest()==prev['sha256']:
                    return dict(row,size=before.st_size,mtime_ns=before.st_mtime_ns,sha256=h.hexdigest(),file_id=file_id,same_content=True)
            with reservation:
                if shutil.disk_usage(folder).free<reserved+before.st_size+128*IO:raise OSError('可用空间不足，未修改原文件')
                allowance=before.st_size;reserved+=allowance
            h=hashlib.sha256();size=0
            with open(p,'rb') as inp,open(temp,'wb') as out:
                for b in iter(lambda:inp.read(IO),b''):engine.check();out.write(b);h.update(b);size+=len(b)
            stable()
            if size!=before.st_size:raise OSError('读取长度变化，本轮跳过')
            os.replace(temp,dest)
            return dict(row,size=size,mtime_ns=before.st_mtime_ns,sha256=h.hexdigest(),file_id=file_id)
        finally:
            if temp.exists():temp.unlink()
            with reservation:reserved-=allowance
    iterator=(r for r in snapshot['selected'] if norm(r['source']) not in staged)
    pending={};pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='backup-reader')
    try:
        exhausted=False
        while pending or not exhausted:
            engine.check()
            while len(pending)<workers*2 and not exhausted:
                try:row=next(iterator)
                except StopIteration:exhausted=True;break
                pending[pool.submit(prepare,row,engine.index.get(row['source']))]=row
            if not pending:break
            ready,_=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
            for future in ready:
                original=pending.pop(future)
                try:
                    row=future.result()
                    if row.get('same_content'):engine.index.unchanged_content(row)
                    snapshot['files'].append(row)
                except OSError as ex:
                    snapshot['issues'].append({'source':original['source'],'error':str(ex)})
                    failed.add(norm(original['source']))
                    engine.log('暂未备份：'+Path(original['source']).name)
                count+=1;dirty+=1
            if ready:engine.emit('stats',{'staged':count,'stage_total':len(snapshot['selected']),'workers':workers})
            if dirty and time.monotonic()-last_save>=2:
                snapshot['stage_failed']=sorted(failed)
                engine.save(folder,snapshot);last_save=time.monotonic();dirty=0
    except BaseException:
        engine.cancel.set()
        for future in pending:future.cancel()
        raise
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        snapshot['stage_failed']=sorted(failed)
        # Completed but uncheckpointed copies may be regenerated on restart; never mark unverified data successful.
        engine.save(folder,snapshot)
    snapshot['files']=[r for r in snapshot['files'] if not r.get('same_content')]
    snapshot['status']='packing';engine.save(folder,snapshot)
