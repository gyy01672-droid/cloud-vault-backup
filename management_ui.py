"""Repository and task controls; network and disk work never block Tk."""
import queue,threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from background import worker_active
from configuration import load_config
from repositories import repository_chain,switch_repository
from task_cleanup import discard_tasks

def launch_job(app,work,done):
    app.admin_busy=True;app.button.configure(state='disabled');app.restore_button.configure(state='disabled')
    results=queue.Queue(maxsize=1)
    def run():
        try:results.put((True,work()))
        except Exception as ex:results.put((False,str(ex)))
    thread=threading.Thread(target=run,name='backup-management');thread.start()
    def poll():
        if thread.is_alive():app.root.after(150,poll);return
        thread.join();ok,value=results.get()
        app.admin_busy=False;app.button.configure(state='normal');app.restore_button.configure(state='normal')
        if ok:done(value)
        else:messagebox.showerror('操作未完成',value,parent=app.root)
    app.root.after(150,poll)

def repository_dialog(app):
    if app.admin_busy:return
    w=tk.Toplevel(app.root);w.title('仓库与容量管理');w.geometry('780x480');w.configure(bg='#0b1220')
    tk.Label(w,text=f'容量提醒 {app.config.get("capacity_soft_gib",20):g} GiB  ·  停止上传 {app.config.get("capacity_hard_gib",30):g} GiB',bg='#0b1220',fg='#44ddc2',font=('Microsoft YaHei UI',12,'bold')).pack(pady=14)
    text=tk.Text(w,height=10,bg='#152238',fg='#edf5ff',wrap='word',padx=12,pady=10);text.pack(fill='both',expand=True,padx=18)
    text.insert('end','已使用仓库（恢复时按顺序读取，不能重复使用）：\n\n'+'\n'.join('https://github.com/'+r['repo'] for r in repository_chain(app.config)))
    text.insert('end','\n\n容量值是软件自设预算，并非 GitHub 官方封号阈值。更换仓库不会消除账号或带宽风险。旧仓库和恢复密钥需继续保留。');text.configure(state='disabled')
    tk.Label(w,text='新的私密仓库地址（先在 GitHub 创建并添加 README）',bg='#0b1220',fg='#edf5ff').pack(pady=(12,4))
    entry=tk.Entry(w,font=('Microsoft YaHei UI',11));entry.pack(fill='x',padx=18)
    def change():
        if app.admin_busy:return
        if app.running or worker_active(app.config['state_dir']):messagebox.showinfo('先暂停任务','等待后台完全暂停后再切换仓库。',parent=w);return
        target=entry.get().strip()
        if not target:return
        if not messagebox.askyesno('确认切换仓库','将保留原密钥、成功索引和旧仓库记录。旧仓库未完成缓存会保留，可在结束任务时清理。\n\n以后新增备份写入新仓库，恢复时仍需访问旧仓库。继续？',parent=w):return
        w.destroy();app.phase.set('正在验证新仓库并迁移成功索引…')
        def done(config):
            app.config=config;app.client=None;app.stats={};app.phase.set('仓库切换完成 · 可以开始新任务')
            app.log('当前仓库：'+config['repo']);messagebox.showinfo('切换完成','成功索引已保留，内容未变的文件无需重新上传。',parent=app.root)
        launch_job(app,lambda:switch_repository(app.config_path,target),done)
    tk.Button(w,text='验证并切换仓库',command=change,bg='#44ddc2',relief='flat',padx=15,pady=8).pack(pady=12)

def end_task(app):
    if app.admin_busy:return
    if not app.config_path:return
    if not messagebox.askyesno('结束任务并清理本地缓存','将安全停止后台，清理当前及历史仓库的未完成备份缓存、恢复缓存和文件选择清单。下次重新扫描。\n\n原文件、密钥、成功索引和已完成记录会保留。云端草稿不会删除，其附件仍计入容量。\n\n确认结束任务？',parent=app.root):return
    app.admin_busy=True
    if app.client:app.client.pause()
    app.phase.set('正在等待后台安全退出后清理缓存…');app.button.configure(state='disabled')
    def wait():
        if worker_active(app.config['state_dir']) or (app.client and not app.client.reap_finished()):app.root.after(250,wait);return
        app.running=False
        if app.selection_window:
            app.selection_window.window.destroy();app.selection_window=None
        def work():
            config=load_config(app.config_path);total={'freed_bytes':0,'tasks':0,'errors':[],'drafts':[]}
            # Historical state was retained during migration; use each profile's own repository identity.
            for row in repository_chain(config):
                if not row.get('state_dir') or not Path(row['state_dir']).exists():continue
                result=discard_tasks(dict(config,repo=row['repo'],state_dir=row['state_dir']))
                for key in ('freed_bytes','tasks'):total[key]+=result[key]
                for key in ('errors','drafts'):total[key].extend(result[key])
            return total
        def done(result):
            app.client=None;app.stats={};app.last_review=None;app.bar.stop();app.bar.configure(mode='determinate');app.bar['value']=0
            app.button.configure(text='开始备份');app.check.configure(state='normal')
            app.phase.set('任务已结束 · 下次将重新扫描');app.values['eta'].set('—');app.values['speed'].set('—')
            message=f'已清理 {result["freed_bytes"]/1024**3:.2f} GiB 本地缓存。'
            if result['drafts']:message+='\n云端未完成草稿仍保留，可在 GitHub 检查。'
            if result['errors']:message+='\n部分缓存未能清理，可重试：\n'+'\n'.join(result['errors'][:5])
            app.detail.set(message);app.log(message)
            (messagebox.showwarning if result['errors'] else messagebox.showinfo)('任务已结束',message,parent=app.root)
        launch_job(app,work,done)
    app.root.after(250,wait)
