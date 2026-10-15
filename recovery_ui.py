"""Explicit original-location restore review, with no background file writes before approval."""
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

def size(n):return f'{n/1024**3:,.2f} GiB' if n>=1024**3 else f'{n/1024**2:,.1f} MiB'

class RestoreReview:
    PAGE_SIZE=500
    def __init__(self,parent,request,approve,pause):
        self.request=request;self.files=request['files'];self.approve=approve;self.pause=pause;self.page=0
        self.window=tk.Toplevel(parent);w=self.window;w.title('确认恢复到原位置');w.configure(bg='#0b1220')
        w.bind('<Destroy>',self.dispose)
        scale=min(float(w.tk.call('tk','scaling'))/(96/72),(w.winfo_screenwidth()-80)/980)
        w.geometry(f'{int(980*scale)}x{min(int(650*scale),w.winfo_screenheight()-120)}')
        try:w.iconbitmap(str(Path(__file__).with_name('backup.ico')))
        except tk.TclError:pass
        w.protocol('WM_DELETE_WINDOW',self.cancel)
        tk.Label(w,text='云端文件已验证，准备恢复到原位置',bg='#0b1220',fg='#edf5ff',font=('Microsoft YaHei UI',17,'bold')).pack(anchor='w',padx=24,pady=(20,8))
        summary=f'{request["count"]:,} 个文件 · {size(request["bytes"])} · {request["releases"]} 个备份版本\n内容相同：{request["identical"]:,} 个；同名不同内容：{request["conflicts"]:,} 个；待处理：{len(request["issues"]):,} 项'
        tk.Label(w,text=summary,justify='left',bg='#0b1220',fg='#93a9c5').pack(anchor='w',padx=24,pady=(0,8))
        tk.Label(w,text='同名文件先保存为 .before-cloud-vault-… 副本，再写回云端版本。不会删除本地其他文件。\n请先关闭正在编辑文件的软件；恢复聊天数据库前请退出对应聊天软件。',justify='left',bg='#0b1220',fg='#44ddc2').pack(anchor='w',padx=24,pady=(0,12))
        frame=tk.Frame(w,bg='#152238');frame.pack(fill='both',expand=True,padx=24)
        text=tk.Text(frame,wrap='none',bg='#152238',fg='#edf5ff',relief='flat',font=('Microsoft YaHei UI',10))
        y=ttk.Scrollbar(frame,orient='vertical',command=text.yview);y.pack(side='right',fill='y')
        x=ttk.Scrollbar(frame,orient='horizontal',command=text.xview);x.pack(side='bottom',fill='x')
        text.configure(yscrollcommand=y.set,xscrollcommand=x.set);text.pack(fill='both',expand=True)
        self.text=text
        navigation=tk.Frame(w,bg='#0b1220');navigation.pack(fill='x',padx=24,pady=8)
        self.page_label=tk.StringVar();tk.Label(navigation,textvariable=self.page_label,bg='#0b1220',fg='#93a9c5').pack(side='left')
        for label,delta in [('下一页',1),('上一页',-1)]:tk.Button(navigation,text=label,command=lambda d=delta:self.change_page(d),bg='#20324c',fg='#edf5ff',relief='flat',padx=10,pady=5).pack(side='right',padx=5)
        actions=tk.Frame(w,bg='#0b1220');actions.pack(fill='x',padx=24,pady=18)
        tk.Button(actions,text='确认恢复到原位置',command=self.submit,bg='#44ddc2',fg='#0b1220',relief='flat',padx=18,pady=10).pack(side='right')
        tk.Button(actions,text='暂不恢复',command=self.cancel,bg='#20324c',fg='#edf5ff',relief='flat',padx=18,pady=10).pack(side='right',padx=10)
        self.render();w.transient(parent);w.lift()
    def render(self):
        labels={'create':'新建','replace':'先留本地副本，再恢复','unchanged':'内容相同，跳过','blocked':'需要处理权限或路径'}
        lines=[f'{labels.get(r["action"],r["action"])}  |  {r["source"]}' for r in self.files[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]]
        issues=self.request['issues']
        if issues:lines+=['','待处理问题（最多显示 50 项，完整内容见恢复报告）：']+[r['source']+'  '+r['error'] for r in issues[:50]]
        lines+=['','原盘可用空间 / 预计新增占用：']+[d+'  '+size(v['free'])+' / '+size(v['required']) for d,v in self.request['volumes'].items()]
        self.text.configure(state='normal');self.text.delete('1.0','end');self.text.insert('end','\n'.join(lines));self.text.configure(state='disabled')
        self.page_label.set(f'第 {self.page+1} / {max(1,(len(self.files)+self.PAGE_SIZE-1)//self.PAGE_SIZE)} 页 · 每页最多 {self.PAGE_SIZE} 个文件')
    def change_page(self,delta):
        self.page=max(0,min(self.page+delta,max(0,(len(self.files)-1)//self.PAGE_SIZE)));self.render()
    def submit(self):
        if not messagebox.askyesno('最后确认：写回原位置',f'将按清单处理 {self.request["count"]:,} 个文件。\n其中 {self.request["conflicts"]:,} 个同名不同内容文件会先保留本地副本，再写回云端版本。\n\n请确认已关闭占用这些文件的软件。现在开始写回？',parent=self.window):return
        try:self.approve(self.request['request_id'])
        except Exception as ex:messagebox.showerror('无法确认恢复',str(ex),parent=self.window);return
        self.window.destroy()
    def cancel(self):self.pause();self.window.destroy()
    def dispose(self,event=None):
        if event is not None and event.widget!=self.window:return
        self.request={};self.files=[];self.approve=None;self.pause=None
