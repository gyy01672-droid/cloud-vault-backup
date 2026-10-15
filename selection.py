"""Review candidate changes before any plaintext staging, packing, or upload."""
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
from pathlib import Path

def size(n):
    for unit in ['B','KiB','MiB','GiB','TiB']:
        if n<1024:return f'{n:,.1f} {unit}'
        n/=1024
    return f'{n:,.1f} PiB'

class FileSelection:
    PAGE_SIZE=500
    def __init__(self,parent,request,submit,cancel):
        self.request=request;self.files=request['files'];self.by_id={r['selection_id']:r for r in self.files}
        self.selected=set(self.by_id);self.submit_callback=submit;self.cancel_callback=cancel;self.page=0
        self.window=tk.Toplevel(parent);self.window.title('选择本次上传的文件')
        self.window.bind('<Destroy>',self.dispose)
        scale=min(max(1.0,float(parent.tk.call('tk','scaling'))/(96/72)),(parent.winfo_screenwidth()-100)/1000,(parent.winfo_screenheight()-130)/650)
        self.window.geometry(f'{int(1000*scale)}x{int(650*scale)}');self.window.minsize(int(850*scale),int(550*scale))
        self.window.configure(bg='#0b1220');self.window.transient(parent)
        self.window.option_add('*Button.background','#20324c');self.window.option_add('*Button.foreground','#edf5ff')
        try:self.window.iconbitmap(str(Path(__file__).resolve().parent/'backup.ico'))
        except tk.TclError:pass
        tk.Label(self.window,text='扫描已完成，确认选择后才会封包上传',bg='#0b1220',fg='#edf5ff',font=('Microsoft YaHei UI',14,'bold')).pack(anchor='w',padx=22,pady=(18,5))
        tk.Label(self.window,text='默认勾选全部新增和修改文件。未勾选项保留到下次扫描；不会删除本地或云端文件。',bg='#0b1220',fg='#93a9c5',wraplength=int(930*scale),justify='left').pack(anchor='w',padx=22,pady=(0,12))
        searchbar=tk.Frame(self.window,bg='#0b1220');searchbar.pack(fill='x',padx=22)
        tk.Label(searchbar,text='搜索文件名或路径',bg='#0b1220',fg='#edf5ff').pack(side='left')
        self.query=tk.StringVar();tk.Entry(searchbar,textvariable=self.query,bg='#20324c',fg='#edf5ff',insertbackground='#44ddc2',relief='flat').pack(side='left',fill='x',expand=True,padx=10)
        tk.Button(searchbar,text='搜索',command=self.filter).pack(side='left');self.window.bind('<Return>',lambda e:self.filter())
        controls=tk.Frame(self.window,bg='#0b1220');controls.pack(fill='x',padx=22,pady=10)
        for text,callback in [('全部勾选',lambda:self.select_all(True)),('全部取消',lambda:self.select_all(False)),('勾选搜索结果',lambda:self.select_filtered(True)),('取消搜索结果',lambda:self.select_filtered(False))]:
            tk.Button(controls,text=text,command=callback).pack(side='left',padx=(0,8))
        table=tk.Frame(self.window);table.pack(fill='both',expand=True,padx=22)
        style=ttk.Style(self.window);row_font=tkfont.Font(self.window,font=('Microsoft YaHei UI',10))
        style.configure('Selection.Treeview',rowheight=row_font.metrics('linespace')+int(8*scale),font=('Microsoft YaHei UI',10),background='#152238',fieldbackground='#152238',foreground='#edf5ff')
        style.configure('Selection.Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'),background='#20324c',foreground='#edf5ff')
        style.map('Selection.Treeview',background=[('selected','#2a4262')],foreground=[('selected','#edf5ff')])
        self.tree=ttk.Treeview(table,columns=('kind','size','path'),show='tree headings',selectmode='browse',style='Selection.Treeview')
        self.tree.heading('#0',text='上传');self.tree.column('#0',width=int(55*scale),stretch=False)
        for key,text,width in [('kind','变化',70),('size','大小',100),('path','文件完整路径',730)]:
            self.tree.heading(key,text=text);self.tree.column(key,width=int(width*scale),stretch=key=='path')
        vertical=ttk.Scrollbar(table,orient='vertical',command=self.tree.yview);horizontal=ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.tree.grid(row=0,column=0,sticky='nsew');vertical.grid(row=0,column=1,sticky='ns');horizontal.grid(row=1,column=0,sticky='ew');table.rowconfigure(0,weight=1);table.columnconfigure(0,weight=1)
        self.tree.bind('<Button-1>',self.click);self.tree.bind('<space>',self.space)
        footer=tk.Frame(self.window,bg='#0b1220');footer.pack(fill='x',padx=22,pady=12)
        self.summary=tk.StringVar();tk.Label(footer,textvariable=self.summary,bg='#0b1220',fg='#44ddc2').pack(side='left')
        tk.Button(footer,text='上一页',command=lambda:self.change_page(-1)).pack(side='right')
        tk.Button(footer,text='下一页',command=lambda:self.change_page(1)).pack(side='right',padx=8)
        buttons=tk.Frame(self.window,bg='#0b1220');buttons.pack(fill='x',padx=22,pady=(0,18))
        self.confirm=tk.Button(buttons,text='确认选择并开始备份',command=self.submit,bg='#44ddc2',fg='#0b1220',padx=16,pady=8);self.confirm.pack(side='left')
        tk.Button(buttons,text='暂不备份',command=self.cancel,padx=16,pady=8).pack(side='right')
        self.window.protocol('WM_DELETE_WINDOW',self.cancel);self.filter();self.window.lift()
    def filter(self):
        query=self.query.get().strip().casefold();self.filtered=[r for r in self.files if query in r['source'].casefold()];self.page=0;self.render()
    def dispose(self,event=None):
        if event is not None and event.widget!=self.window:return
        self.request={};self.files=[];self.filtered=[];self.by_id={};self.selected=set()
        self.submit_callback=None;self.cancel_callback=None
    def render(self):
        for i in self.tree.get_children():self.tree.delete(i)
        for row in self.filtered[self.page*self.PAGE_SIZE:(self.page+1)*self.PAGE_SIZE]:
            key=row['selection_id'];self.tree.insert('','end',iid=key,text='☑' if key in self.selected else '☐',values=('新增' if row['kind']=='new' else '修改',size(row['size']),row['source']))
        self.update_summary()
    def update_summary(self):
        total=sum(self.by_id[i]['size'] for i in self.selected)
        self.summary.set(f'已选 {len(self.selected):,} / {len(self.files):,} 个 · {size(total)}   |   第 {self.page+1} / {max(1,(len(self.filtered)+self.PAGE_SIZE-1)//self.PAGE_SIZE)} 页')
    def toggle(self,key):
        if key in self.selected:self.selected.remove(key)
        else:self.selected.add(key)
        self.tree.item(key,text='☑' if key in self.selected else '☐');self.update_summary()
    def click(self,event):
        key=self.tree.identify_row(event.y)
        if key and self.tree.identify_column(event.x)=='#0':self.toggle(key);return 'break'
    def space(self,event):
        selected=self.tree.selection()
        if selected:self.toggle(selected[0])
        return 'break'
    def select_all(self,enabled):self.selected=set(self.by_id) if enabled else set();self.render()
    def select_filtered(self,enabled):
        keys={r['selection_id'] for r in self.filtered}
        if enabled:self.selected.update(keys)
        else:self.selected.difference_update(keys)
        self.render()
    def change_page(self,delta):
        self.page=max(0,min(self.page+delta,max(0,(len(self.filtered)-1)//self.PAGE_SIZE)));self.render()
    def submit(self):
        try:self.submit_callback(self.request['request_id'],self.selected)
        except Exception as ex:messagebox.showerror('无法保存选择',str(ex),parent=self.window);return
        self.window.destroy()
    def cancel(self):
        self.cancel_callback();self.window.destroy()
