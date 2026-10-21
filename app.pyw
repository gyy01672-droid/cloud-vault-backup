"""云端保险箱 — one-click personal backup UI."""
import ctypes, json, os, sys, time, webbrowser
from pathlib import Path
from collections import deque
from configuration import app_home, load_config, read_recovery_key
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
try:
    from engine import BackupError, read_json
    from background import BackgroundClient, worker_active, safe_read
    from selection import FileSelection
    from recovery_ui import RestoreReview
except ImportError:
    if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,'缺少 Python 组件。请先运行 install.ps1 安装依赖。','云端保险箱 · 启动失败',16)
    raise SystemExit(1)

APP = Path(__file__).resolve().parent
BG = '#0b1220'; CARD = '#152238'; TEXT = '#edf5ff'; MUTED = '#93a9c5'; TEAL = '#44ddc2'
def size(n):
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if abs(n) < 1024: return f'{n:,.1f} {unit}'
        n /= 1024
    return f'{n:,.1f} PiB'
def duration(n):
    if n is None: return '测算中'
    if n < 60: return f'{max(1,int(n))} 秒'
    if n < 3600: return f'{int(n//60)} 分 {int(n%60)} 秒'
    return f'{int(n//3600)} 小时 {int(n%3600//60)} 分'

class UploadEstimate:
    """Recent throughput, including idle time; verified/skipped bytes are not traffic."""
    def __init__(self, clock=time.monotonic):
        self.clock=clock;self.samples=deque();self.wire=0;self.last_progress=None
        self.speed_time=clock();self.speed_bytes=0;self.current_speed=0
    def observe(self, wire):
        t=self.clock()
        if not self.samples:self.samples.append((t,0))
        if wire>self.wire:self.last_progress=t
        self.wire=wire;self.samples.append((t,wire))
        self.prune(t)
    def prune(self,t):
        while len(self.samples)>2 and self.samples[1][0]<=t-15:self.samples.popleft()
    def speed(self):
        # Count this upload's payload bytes, without packet capture or system-wide traffic.
        t=self.clock();elapsed=t-self.speed_time
        if elapsed>=1:
            self.current_speed=max(0,self.wire-self.speed_bytes)/elapsed
            self.speed_time=t;self.speed_bytes=self.wire
        return self.current_speed
    def read(self,total,uploaded,inflight,retrying=False):
        t=self.clock();self.prune(t)
        remaining=max(0,total-uploaded-inflight)
        if remaining==0:return 0,None,'校验中'
        if retrying:return 0,None,'等待重试'
        if self.last_progress is None:return 0,None,'测算中'
        if t-self.last_progress>=10:return 0,None,'等待响应'
        elapsed=t-self.samples[0][0]
        if elapsed<1:return 0,None,'测算中'
        rate=max(0,self.wire-self.samples[0][1])/elapsed
        if rate<=0:return 0,None,'测算中'
        return rate,remaining/rate,None

class Window:
    def __init__(self, preview=False, config=None, config_path=None):
        self.config = config or {'repo':'example/private-backup','roots':[], 'state_dir':str(app_home()/'preview')}
        self.running = False; self.stats = {}; self.preview = preview
        self.operation='backup'
        self.closing=False;self.client=None;self.config_path=config_path;self.selection_window=None;self.last_review=None
        self.retired_clients=[]
        self.admin_busy=False
        self.backend_phase=-1;self.backend_log=0;self.last_backend_poll=0;self.started_at=0
        self.eta_meter=UploadEstimate();self.last_refresh=0
        self.root = tk.Tk(); self.root.title('云端保险箱 · GitHub 增量备份')
        self.root.configure(bg=BG)
        scale=min(max(1.0,float(self.root.tk.call('tk','scaling'))/(96/72)),
                  (self.root.winfo_screenwidth()-100)/840,(self.root.winfo_screenheight()-130)/670)
        self.root.tk.call('tk','scaling',(96/72)*scale)
        self.root.geometry(f'{int(840*scale)}x{int(670*scale)}'); self.root.minsize(int(760*scale),int(620*scale))
        try: self.root.iconbitmap(str(APP / 'backup.ico'))
        except tk.TclError: pass
        self.root.option_add('*Font', ('Microsoft YaHei UI', 10))
        style = ttk.Style(); style.theme_use('clam')
        style.configure('Cloud.Horizontal.TProgressbar', background=TEAL, troughcolor='#22334e', bordercolor='#22334e', lightcolor=TEAL, darkcolor=TEAL, thickness=10)
        header = tk.Frame(self.root,bg=BG); header.pack(fill='x',padx=32,pady=(25,16))
        try:
            self.logo = tk.PhotoImage(file=str(APP/'backup.png')).subsample(4,4)
            tk.Label(header,image=self.logo,bg=BG).pack(side='left',padx=(0,15))
        except tk.TclError: pass
        titles=tk.Frame(header,bg=BG);titles.pack(side='left')
        tk.Label(titles,text='云端保险箱',font=('Microsoft YaHei UI',23,'bold'),fg=TEXT,bg=BG).pack(anchor='w')
        tk.Label(titles,text='GitHub 私密仓库  /  增量加密备份',fg=MUTED,bg=BG).pack(anchor='w',pady=(4,0))
        tk.Label(header,text='●  仅上传变动',fg=TEAL,bg='#142e32',padx=12,pady=7).pack(side='right')
        top=tk.Frame(self.root,bg=CARD,padx=20,pady=15);top.pack(fill='x',padx=32,pady=(0,15))
        self.phase=tk.StringVar(value='准备开始备份');tk.Label(top,textvariable=self.phase,fg=TEXT,bg=CARD,font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w')
        self.detail=tk.StringVar(value='先续传未完成任务，再扫描新增与修改的文件。');tk.Label(top,textvariable=self.detail,fg=MUTED,bg=CARD,wraplength=710,justify='left').pack(anchor='w',pady=(6,12))
        self.bar=ttk.Progressbar(top,style='Cloud.Horizontal.TProgressbar',maximum=100);self.bar.pack(fill='x')
        self.progress_text=tk.StringVar(value='正在初始化');tk.Label(top,textvariable=self.progress_text,fg=TEAL,bg=CARD).pack(anchor='w',pady=(9,0))
        cards=tk.Frame(self.root,bg=BG);cards.pack(fill='x',padx=32,pady=(0,15));self.values={}
        for col,(key,label,value) in enumerate([('files','本次文件','—'),('bytes','本次数据','—'),('speed','传输速度','—'),('eta','预计剩余','测算中')]):
            cards.columnconfigure(col,weight=1,uniform='cards');f=tk.Frame(cards,bg=CARD,padx=14,pady=12);f.grid(row=0,column=col,sticky='nsew',padx=(0,8) if col<3 else 0)
            tk.Label(f,text=label,bg=CARD,fg=MUTED,font=('Microsoft YaHei UI',9)).pack(anchor='w')
            var=tk.StringVar(value=value);self.values[key]=var
            tk.Label(f,textvariable=var,bg=CARD,fg=TEXT,font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w',pady=(6,0))
        body=tk.Frame(self.root,bg=CARD,padx=15,pady=12);body.pack(fill='both',expand=True,padx=32)
        tk.Label(body,text='备份动态',bg=CARD,fg=TEXT,font=('Microsoft YaHei UI',10,'bold')).pack(anchor='w',pady=(0,6))
        self.logbox=tk.Text(body,bg=CARD,fg=MUTED,relief='flat',height=7,font=('Microsoft YaHei UI',9),state='disabled',wrap='word',highlightthickness=0)
        self.logbox.pack(fill='both',expand=True)
        options=tk.Frame(self.root,bg=BG);options.pack(fill='x',padx=32,pady=(12,6))
        self.deep=tk.BooleanVar(value=False)
        self.check=tk.Checkbutton(options,text='深度检查所有文件内容（较慢）',variable=self.deep,bg=BG,fg=MUTED,selectcolor=CARD,activebackground=BG,activeforeground=TEXT,highlightthickness=0)
        self.check.pack(side='left')
        tk.Button(options,text='查看备份范围',command=self.scope,bg=BG,fg=MUTED,relief='flat',activebackground=CARD,activeforeground=TEXT,cursor='hand2').pack(side='right')
        from management_ui import repository_dialog,end_task
        tk.Button(options,text='仓库管理',command=lambda:repository_dialog(self),bg=BG,fg=TEAL,relief='flat').pack(side='right',padx=6)
        tk.Button(options,text='结束任务 / 清缓存',command=lambda:end_task(self),bg=BG,fg='#ffbd88',relief='flat').pack(side='right',padx=6)
        actions=tk.Frame(self.root,bg=BG);actions.pack(fill='x',padx=32,pady=(3,14))
        self.button=tk.Button(actions,text='暂停备份',command=self.toggle,bg=TEAL,fg=BG,activebackground='#78ead7',relief='flat',padx=22,pady=9,font=('Microsoft YaHei UI',11,'bold'),cursor='hand2');self.button.pack(side='left')
        self.restore_button=tk.Button(actions,text='一键恢复',command=self.start_restore,bg='#20324c',fg=TEXT,relief='flat',padx=15,pady=10,cursor='hand2');self.restore_button.pack(side='left',padx=(10,0))
        for label,command in [('打开私密仓库',lambda:webbrowser.open('https://github.com/'+self.config['repo']+'/releases')),('查看本次报告',self.report)]:
            tk.Button(actions,text=label,command=command,bg='#20324c',fg=TEXT,activebackground='#2a4262',activeforeground=TEXT,relief='flat',padx=15,pady=10,cursor='hand2').pack(side='left',padx=(10,0))
        tk.Label(self.root,text='AES-256 加密  ·  分包续传  ·  上传后核对 SHA-256  ·  保留历史版本',bg=BG,fg=MUTED,font=('Microsoft YaHei UI',8)).pack(pady=(0,14))
        self.root.protocol('WM_DELETE_WINDOW',self.close);self.root.after(100,self.poll)
        self.root.bind('<Unmap>',self.minimized)
        if preview: self.demo()
        else:self.root.after(700,self.start)
    def log(self,text):
        self.logbox.configure(state='normal');self.logbox.insert('end',time.strftime('%H:%M:%S')+'  '+text+'\n')
        excess=int(self.logbox.index('end-1c').split('.')[0])-601
        if excess>0:self.logbox.delete('1.0',f'{excess+1}.0')
        self.logbox.see('end');self.logbox.configure(state='disabled')
    def start_restore(self):
        if self.admin_busy:return
        if self.running:
            messagebox.showinfo('请先暂停当前任务','备份与恢复不能同时写入数据。请先暂停当前任务，再点击一键恢复。',parent=self.root);return
        configured=Path(self.config.get('key_file','recovery-key.txt'))
        chosen=filedialog.askopenfilename(title='选择备份时保存的恢复密钥',parent=self.root,initialdir=str(configured.parent),initialfile=configured.name,filetypes=[('恢复密钥文本','*.txt'),('所有文件','*.*')])
        if not chosen:return
        try:read_recovery_key(chosen)
        except ValueError as ex:messagebox.showerror('密钥文件不可用',str(ex),parent=self.root);return
        if messagebox.askyesno('确认下载并解密',f'仓库：{self.config["repo"]}\n密钥文件：{chosen}\n\n此密钥只用于本次恢复。将下载、解密并校验云端备份，占用本地缓存空间。\n完成后先查看恢复清单，再次确认才写回原位置。\n\n开始准备？',parent=self.root):self.start('restore',recovery_key=chosen)
    def start(self,operation='backup',recovery_key=None):
        if self.running or self.admin_busy:return
        if not self.config_path:
            messagebox.showinfo('预览模式','这是界面预览，请正常启动程序以配置备份。',parent=self.root);return
        if operation=='restore' and recovery_key is None and not worker_active(self.config['state_dir']):self.start_restore();return
        self.operation=operation
        self.running=True;self.stats={};self.eta_meter=UploadEstimate();self.button.configure(text='暂停任务',state='normal');self.check.configure(state='disabled')
        self.backend_phase=-1;self.backend_log=0;self.last_review=None;self.started_at=time.monotonic()
        self.bar.configure(mode='indeterminate');self.bar.start(12);deep=self.deep.get()
        try:
            if self.client and not self.client.reap_finished():self.retired_clients.append(self.client)
            self.client=BackgroundClient(self.config_path,self.config['state_dir'])
            created=self.client.start(deep,operation,recovery_key)
            self.phase.set('独立后台任务已启动' if created else '已连接正在运行的后台任务')
            self.log('最小化后后台继续运行；文件选择或原位恢复需要在窗口中确认。')
        except Exception as ex:self.finish('error',str(ex))
    def toggle(self):
        if self.admin_busy:return
        if self.running:
            self.client.pause();self.phase.set('正在安全暂停…');self.button.configure(state='disabled')
        else:self.start()
    def minimized(self,event):
        if event.widget==self.root and self.running:self.root.title('云端保险箱 · 后台任务继续运行')
    def poll_backend(self):
        value=self.client.status()
        if not value:
            if self.client.process and self.client.process.poll() is not None:
                self.finish('error','后台进程未能启动，请检查配置、运行环境及 last-error.log。')
            elif time.monotonic()-self.started_at>15 and not worker_active(self.config['state_dir']):
                self.finish('error','后台任务启动超时，未推进备份索引；可以重试。')
            return
        if value['phase_id']!=self.backend_phase:
            self.backend_phase=value['phase_id'];self.stats={};self.eta_meter=UploadEstimate();self.phase.set(value['phase'])
            self.values['eta'].set('测算中');self.values['speed'].set('—');self.log(value['phase'])
        self.operation=value.get('operation','backup')
        for number,line in value.get('logs',[]):
            if number>self.backend_log:self.log(line);self.backend_log=number
        self.stats.update(value.get('stats',{}))
        if 'wire_bytes' in self.stats:self.eta_meter.observe(self.stats['wire_bytes'])
        if value['status'] in ('done','error','paused'):
            self.finish(value['status'],value['result']);return
        request_id=value.get('review_id')
        if request_id and request_id!=self.last_review:
            capacity=value.get('review_kind')=='capacity'
            request=safe_read(Path(self.config['state_dir'])/('capacity-request.json' if capacity else 'restore-request.json' if self.operation=='restore' else 'selection-request.json'),{})
            if request.get('run_id')==self.client.run_id and request.get('request_id')==request_id:
                self.last_review=request_id;self.root.deiconify();self.root.lift()
                if capacity:
                    approved=messagebox.askyesno('仓库容量提醒',f'当前容量 {size(request["used"])}，完成本轮预计 {size(request["projected"])}。\n已达到提醒值 {size(request["soft"])}；停止值为 {size(request["hard"])}。\n\n这是软件自设预算，不是 GitHub 官方封号阈值。换仓库不能消除账号风险。\n\n忽略本仓库首次提醒并继续？选择“否”将暂停，可在仓库管理中更换仓库。',parent=self.root)
                    self.client.approve_capacity(request_id,approved)
                else:self.selection_window=(RestoreReview(self.root,request,self.client.approve_restore,self.toggle) if self.operation=='restore' else FileSelection(self.root,request,self.client.approve,self.toggle))
        age=time.time()-value.get('heartbeat',0)
        if age>5:
            if not worker_active(self.config['state_dir']):
                self.finish('error','后台任务意外结束，已完成分包保留；重新开始即可续传。');return
            self.phase.set('后台进程仍在运行 · 等待状态更新')
        self.render_stats()
    def poll(self):
        self.retired_clients=[c for c in self.retired_clients if not c.reap_finished()]
        if not self.running and self.client:self.client.reap_finished()
        if self.selection_window and not self.selection_window.window.winfo_exists():self.selection_window=None
        if self.running and self.client and time.monotonic()-self.last_backend_poll>=1:
            try:self.poll_backend()
            except (OSError,ValueError) as ex:self.phase.set('等待后台状态同步…')
            self.last_backend_poll=time.monotonic()
        if self.closing and not self.running:self.root.destroy();return
        self.root.after(150,self.poll)
    def render_stats(self):
        s=self.stats
        if 'changed' in s:self.values['files'].set(f'{s["changed"]:,} 个')
        if 'changed_bytes' in s:self.values['bytes'].set(size(s['changed_bytes']))
        if s.get('stage') not in ('upload','download') and 'estimate' in s:
            self.values['eta'].set('约 '+duration(s['estimate']))
            self.detail.set('这是按历史速度粗估的上传耗时；文件准备完成后，将按实际速度动态更新。')
        if s.get('stage')=='restore_review':
            self.bar.stop();self.values['eta'].set('等待确认');self.values['speed'].set('—')
            self.detail.set('云端内容已验证，请检查恢复位置。确认后才写回文件，同名不同内容的本地文件会先保留副本。')
            self.progress_text.set('等待确认恢复到原位置')
        elif s.get('stage')=='review':
            self.bar.stop();self.values['eta'].set('等待选择');self.values['speed'].set('—')
            self.detail.set('请在文件选择窗口勾选本次要上传的内容。确认前不会封包上传。')
            self.progress_text.set('扫描完成 · 等待你的确认')
        elif s.get('stage') in ('upload','download'):
            _,eta,status=self.eta_meter.read(s.get('total',0),s.get('uploaded',0),s.get('inflight',0),s.get('retrying',False))
            rate=self.eta_meter.speed()
            self.values['speed'].set(size(rate)+'/s')
            self.values['eta'].set(status or duration(eta))
            self.bar.stop();self.bar.configure(mode='determinate');total=max(s.get('total',1),1)
            self.bar['value']=min(99.9,(s.get('uploaded',0)+s.get('inflight',0))/total*100)
            self.detail.set(('正在下载云端备份' if s.get('stage')=='download' else '独立后台进程正在上传')+'，最小化不会暂停。速度每秒更新；剩余时间按近 15 秒均速估算。')
            self.progress_text.set(f'已校验 {size(s.get("uploaded",0))} / {size(total)}   ·   当前包 {size(s.get("inflight",0))}')
        elif 'packed' in s:self.progress_text.set(f'已加密并校验 {s["packed"]} / {s["pack_total"]} 个分包')
        elif 'staged' in s:self.progress_text.set(f'已处理 {s["staged"]:,} / {s["stage_total"]:,} 个候选变动文件')
        elif 'scanned' in s:self.progress_text.set(f'已扫描 {s["scanned"]:,} 个文件')
    def finish(self,kind,value):
        self.running=False;self.bar.stop();self.button.configure(text='开始备份',state='normal');self.check.configure(state='normal')
        self.values['speed'].set('—')
        if self.selection_window:
            try:self.selection_window.window.destroy()
            except tk.TclError:pass
            self.selection_window=None
        self.root.title('云端保险箱 · '+('备份完成' if kind=='done' else '任务已暂停或未完成'))
        if not self.closing and not self.preview:self.root.deiconify();self.root.lift()
        if kind=='done' and isinstance(value,dict) and value.get('operation')=='restore':
            issues=len(value['issues']);message=f'已恢复 {value["files"]:,} 个文件；内容相同跳过 {value["unchanged"]:,} 个。\n保留本地副本 {len(value["preserved"]):,} 个；待处理 {issues:,} 项。'
            self.root.title('云端保险箱 · 恢复结果');self.phase.set('恢复结束 · 有待处理项' if issues else '恢复完成');self.detail.set(message)
            self.values['eta'].set('已结束');self.values['files'].set(f'{value["files"]:,} 个');self.bar.configure(mode='determinate');self.bar['value']=100
            self.progress_text.set('详情见恢复报告；未成功的文件可以处理后再次恢复。');self.log(message)
            if not self.preview and not self.closing:(messagebox.showwarning if issues else messagebox.showinfo)('恢复结果',message,parent=self.root)
            return
        if kind=='done':
            count=sum(r.get('files',0) for r in value['runs']);issues=sum(len(r.get('issues',[])) for r in value['runs'])
            self.values['files'].set(f'{count:,} 个');self.values['eta'].set('已完成');self.bar.configure(mode='determinate');self.bar['value']=100
            removed=sum(r.get('deleted',0) for r in value['runs'])
            message=f'本轮完成：{count:,} 个新增或修改文件。' if count else '没有需要上传的新文件内容。'
            if removed:message+=f'\n已记录 {removed} 项删除，云端历史仍保留。'
            skipped=sum(r.get('unselected',0) for r in value['runs'])
            if skipped:message+=f'\n本次未勾选 {skipped:,} 个文件，下次扫描仍会显示。'
            if issues:message+=f'\n有 {issues} 项未能读取或需要检查，详情见报告。'
            self.phase.set('备份完成 · 有待检查项' if issues else '备份完成');self.detail.set(message);self.progress_text.set('任务结束，已释放自动休眠限制。');self.log(message)
            if not self.preview and not self.closing:(messagebox.showwarning if issues else messagebox.showinfo)('云端保险箱',message,parent=self.root)
        elif kind=='paused':self.phase.set('已暂停 · 进度已保存');self.values['eta'].set('已暂停');self.detail.set(value);self.log(value)
        else:
            self.values['eta'].set('等待续传')
            self.phase.set('本轮未完成 · 可以稍后续传');self.detail.set(value);self.log(value)
            if not self.preview and not self.closing:messagebox.showerror('备份未完成',value+'\n已完成的分包会保留，重新打开即可重试。',parent=self.root)
    def scope(self):
        w=tk.Toplevel(self.root);w.title('当前备份范围');w.geometry('730x510');w.configure(bg=BG)
        text=tk.Text(w,bg=CARD,fg=TEXT,wrap='word',font=('Microsoft YaHei UI',10),padx=16,pady=16);text.pack(fill='both',expand=True,padx=15,pady=15)
        text.insert('end','扫描以下目录及其子目录：\n\n'+'\n'.join(self.config['roots'])+'\n\n排除已识别的凭据、缓存、依赖环境和备份工具自身。删除只记录，不清理云端历史。请将恢复密钥保存在独立设备。聊天数据库是文件级副本，未保证应用内导入。\n\n配置保存在用户本地数据目录的 config.json。只修改 roots 和 exclude_paths 可调整范围；不能复用旧状态目录更换仓库或密钥。');text.configure(state='disabled')
    def report(self):
        if self.operation=='restore':
            path=Path(self.config['state_dir'])/'last-restore-report.json'
            if not path.exists():messagebox.showinfo('恢复报告','尚未写回文件，当前状态请查看任务动态。',parent=self.root);return
            r=read_json(path);lines=[f'恢复文件：{r.get("files",0)}',f'内容相同跳过：{r.get("unchanged",0)}',f'更新时间：{r.get("finished","")}','本地保留副本：']
            lines += [x['source']+' → '+x['copy'] for x in r.get('preserved',[])]+['待处理：']+[x['source']+'\n'+x['error'] for x in r.get('issues',[])]
            w=tk.Toplevel(self.root);w.title('恢复报告');w.geometry('850x620');t=tk.Text(w,wrap='word');t.pack(fill='both',expand=True);t.insert('end','\n'.join(lines));t.configure(state='disabled');return
        path=Path(self.config['state_dir'])/'last-report.json'
        if not path.exists():
            messagebox.showinfo('报告','尚未完成一次备份，当前进度请查看窗口中的备份动态。',parent=self.root);return
        r=read_json(path);status={'success':'已完成','warning':'已结束，有待检查项','error':'未完成，可续传','paused':'已暂停'}.get(r.get('status'),'待检查')
        lines=[f'状态：{status}',f'更新时间：{r.get("finished","")}','']
        if r.get('error'):lines+=['原因：'+r['error'],'']
        for run in r.get('runs',[]):
            lines += [f'新增或修改：{run.get("files",0):,} 个',f'原始内容：{size(run.get("bytes",0))}',f'删除记录：{run.get("deleted",0)} 条（历史备份保留）']
            if run.get('url'):lines+=['备份地址：'+run['url']]
            for issue in run.get('issues',[]):lines += ['待检查：'+issue.get('source','')+'\n'+issue.get('error','')]
            lines+=['']
        w=tk.Toplevel(self.root);w.title('本次备份报告');w.geometry('850x620');w.configure(bg=BG)
        text=tk.Text(w,bg=CARD,fg=TEXT,wrap='word',font=('Microsoft YaHei UI',10),padx=15,pady=15);text.pack(fill='both',expand=True,padx=15,pady=15)
        text.insert('end','\n'.join(lines));text.configure(state='disabled')
    def close(self):
        if self.admin_busy:
            messagebox.showinfo('操作进行中','正在安全停止、清理或迁移，请等待完成后退出。',parent=self.root);return
        if self.closing:return
        if self.running and not messagebox.askyesno('保存进度并退出','现在退出会暂停本轮任务。下次双击自动续传，已上传内容不会丢失。\n\n保存进度并退出？',parent=self.root):return
        if self.running and self.client:
            self.client.pause()
            self.closing=True;self.button.configure(state='disabled');self.phase.set('正在保存进度并退出…')
            self.detail.set('正在等待当前网络请求结束，窗口会自动关闭。')
        else:self.root.destroy()
    def demo(self):
        self.phase.set('已准备好 · 双击即开始');self.detail.set('后台扫描 → 勾选文件 → 加密上传。最小化窗口时后台任务继续运行。');self.progress_text.set('支持网络重试、退出续传和完成弹窗。')
        self.button.configure(text='开始备份');self.log('预览模式：没有扫描或上传个人文件。')

def main():
    import argparse
    parser=argparse.ArgumentParser(description='Cloud Vault encrypted GitHub backup')
    parser.add_argument('--config',type=Path,default=app_home()/'config.json')
    parser.add_argument('--preview',action='store_true');parser.add_argument('--ui-check',action='store_true')
    args=parser.parse_args()
    if os.name=='nt':
        try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:pass
    preview=args.preview
    lock=None
    config=None
    if not preview:
        if args.config.exists():config=load_config(args.config)
        else:
            from onboarding import configure
            config=configure(args.config)
            if config is None:return
        from engine import RunLock
        folder=Path(config['state_dir']);folder.mkdir(parents=True,exist_ok=True)
        lock=RunLock(args.config.with_suffix('.app.lock'))
        try:lock.acquire()
        except BackupError:
            r=tk.Tk();r.withdraw();messagebox.showinfo('云端保险箱','备份窗口已经在运行，请切换到已有窗口。');r.destroy();return
    try:
        window=Window(preview,config,args.config if not preview else None)
        if args.ui_check:
            window.root.update();print(json.dumps({'width':window.root.winfo_width(),'height':window.root.winfo_height(),'widgets':len(window.root.winfo_children())}));window.root.destroy()
        else:window.root.mainloop()
    finally:
        if lock:lock.close()
if __name__=='__main__':
    try:main()
    except Exception as ex:
        if os.name=='nt':ctypes.windll.user32.MessageBoxW(None,'启动失败：'+str(ex)+'\n请检查本地配置和 Python 安装。','云端保险箱',16)
        raise
