"""First-run setup. GitHub credentials stay in GitHub CLI's credential store."""
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
from urllib.parse import quote
from configuration import app_home, find_gh, parse_repo, protect, validate_config, write_key
from engine import GitHub, atomic_json

class Setup:
    def __init__(self, config_path, root=None):
        self.path=Path(config_path);self.result=None;self.roots=[];self.events=queue.Queue();self.busy=False
        self.root=root or tk.Tk();self.root.title('云端保险箱 · 首次设置')
        scale=min(max(1.0,float(self.root.tk.call('tk','scaling'))/(96/72)),
                  (self.root.winfo_screenwidth()-100)/760,(self.root.winfo_screenheight()-130)/650)
        self.root.tk.call('tk','scaling',(96/72)*scale)
        self.root.geometry(f'{int(760*scale)}x{int(650*scale)}');self.root.minsize(int(720*scale),int(620*scale))
        self.root.configure(bg='#0b1220');self.root.option_add('*Font', ('Microsoft YaHei UI',10))
        self.root.option_add('*Button.background','#20324c');self.root.option_add('*Button.foreground','#edf5ff')
        self.root.option_add('*Button.activeBackground','#2a4262');self.root.option_add('*Button.activeForeground','#edf5ff')
        try:self.root.iconbitmap(str(Path(__file__).resolve().parent/'backup.ico'))
        except tk.TclError:pass
        panel=tk.Frame(self.root,bg='#152238',padx=25,pady=20);panel.pack(fill='both',expand=True,padx=22,pady=22)
        def label(text): tk.Label(panel,text=text,bg='#152238',fg='#edf5ff',anchor='w',justify='left',wraplength=int(650*scale)).pack(fill='x',pady=(8,5))
        label('欢迎使用云端保险箱\n选择自己的私密仓库与备份目录。配置只保存在这台电脑。')
        label('1. GitHub 私密仓库地址（仓库需已有 README 等至少一次提交）')
        self.repo=tk.StringVar();tk.Entry(panel,textvariable=self.repo,font=('Segoe UI',11),bg='#20324c',fg='#edf5ff',insertbackground='#44ddc2',relief='flat').pack(fill='x',ipady=5)
        buttons=tk.Frame(panel,bg='#152238');buttons.pack(fill='x',pady=8)
        tk.Button(buttons,text='登录 GitHub',command=self.login).pack(side='left')
        tk.Button(buttons,text='去 GitHub 创建私密仓库',command=lambda:webbrowser.open('https://github.com/new')).pack(side='left',padx=8)
        label('2. 只备份你选择的目录（可添加多个）')
        self.listbox=tk.Listbox(panel,height=6,bg='#20324c',fg='#edf5ff',selectbackground='#2a4262',relief='flat',highlightthickness=0);self.listbox.pack(fill='both',expand=True)
        buttons=tk.Frame(panel,bg='#152238');buttons.pack(fill='x',pady=8)
        tk.Button(buttons,text='添加目录',command=self.add).pack(side='left')
        tk.Button(buttons,text='移除选中',command=self.remove).pack(side='left',padx=8)
        label('3. 校验仓库后生成恢复密钥，并要求你另存一份。\n密钥不会上传；在其他设备恢复时必须使用它。请另存到 U 盘或密码管理器。')
        self.status=tk.StringVar(value='文件会先加密，公开仓库不能作为备份目标。')
        tk.Label(panel,textvariable=self.status,bg='#152238',fg='#44ddc2',wraplength=650,justify='left').pack(fill='x',pady=10)
        self.go=tk.Button(panel,text='校验仓库并保存设置',command=self.verify,bg='#44ddc2',fg='#0b1220',relief='flat',padx=20,pady=10);self.go.pack(anchor='w')
        self.root.after(150,self.poll)
    def login(self):
        try:
            gh=find_gh()
            if os.name=='nt': subprocess.Popen([gh,'auth','login','--hostname','github.com','--web','--git-protocol','https'],creationflags=subprocess.CREATE_NEW_CONSOLE)
            else: messagebox.showinfo('登录 GitHub','请在终端运行 gh auth login，完成后回来校验仓库。',parent=self.root)
        except Exception as ex: messagebox.showerror('GitHub CLI',str(ex),parent=self.root)
    def add(self):
        path=filedialog.askdirectory(parent=self.root,title='选择备份目录')
        if path and path not in self.roots:self.roots.append(path);self.listbox.insert('end',path)
    def remove(self):
        for i in reversed(self.listbox.curselection()):self.roots.pop(i);self.listbox.delete(i)
    def verify(self):
        if self.busy:return
        try:
            repo=parse_repo(self.repo.get());gh=find_gh()
            if not self.roots:raise ValueError('请至少添加一个备份目录。')
            if any(not Path(p).is_dir() for p in self.roots):raise ValueError('有目录已不存在，请重新选择。')
        except Exception as ex:messagebox.showerror('请检查设置',str(ex),parent=self.root);return
        self.busy=True;self.go.configure(state='disabled');self.status.set('正在校验仓库的私密状态、写入权限和默认分支…')
        roots=list(self.roots)
        def worker():
            remote=None
            try:
                remote=GitHub({'repo':repo,'gh':gh},lambda *args:None,threading.Event())
                info=remote.private()
                if not info.get('permissions',{}).get('push'):raise ValueError('此账号没有仓库写入权限。')
                remote.api('GET','/commits/'+quote(info['default_branch'],safe=''))
                self.events.put(('ok',{'repo':repo,'repo_id':info['id'],'gh':gh,'roots':roots}))
            except Exception as ex:self.events.put(('error',str(ex)))
            finally:
                if remote:remote.session.close()
        threading.Thread(target=worker,daemon=True).start()
    def poll(self):
        try:
            kind,value=self.events.get_nowait();self.busy=False;self.go.configure(state='normal')
            if kind=='error':self.status.set(value);messagebox.showerror('仓库校验失败',value,parent=self.root)
            else:
                self.save(value)
                if self.result:return
        except queue.Empty:pass
        if self.root.winfo_exists():self.root.after(150,self.poll)
    def save(self,c):
        export=filedialog.asksaveasfilename(parent=self.root,title='另存恢复密钥（请保存到独立设备）',initialfile='cloud-vault-recovery-key.txt',defaultextension='.txt')
        if not export:self.status.set('未保存密钥，设置尚未完成。');return
        try:
            home=app_home();home.mkdir(parents=True,exist_ok=True);protect(home,True)
            state=home/'profiles'/str(c['repo_id']);state.mkdir(parents=True,exist_ok=True);protect(state,True)
            private=state/'private';private.mkdir(exist_ok=True);protect(private,True)
            key_path=private/'recovery-key.txt'
            if key_path.exists():
                key=bytes.fromhex(key_path.read_text().strip())
                if len(key)!=32:raise ValueError('已有密钥格式错误，请保留文件并检查，程序不会替换它。')
            else:key=os.urandom(32);write_key(key_path,key)
            write_key(export,key)
            c.update(state_dir=str(state),key_file=str(key_path),part_mib=64,
                     exclude_paths=[str(home),str(Path(__file__).resolve().parent),str(Path(export).resolve())])
            c=validate_config(c);atomic_json(self.path,c);protect(self.path)
            self.result=c;self.root.destroy()
        except Exception as ex:messagebox.showerror('设置未完成',str(ex)+'\n已有密钥不会被覆盖，请选择新的导出文件名。',parent=self.root)
    def run(self):
        self.root.mainloop();return self.result

def configure(path):return Setup(path).run()
