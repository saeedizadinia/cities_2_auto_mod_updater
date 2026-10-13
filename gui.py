import json
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from pathlib import Path
import updater as u


def launch():
    u.setup_log()
    cfg = u.load_config()
    root = tk.Tk()
    root.title('CS2 Manual Mod Updater')
    root.geometry('1000x640')
    events = queue.Queue()
    busy = [False]
    plans = []
    container = ttk.Frame(root, padding=14)
    container.pack(fill='both', expand=True)
    ttk.Label(container, text='Cities: Skylines II — Manual Mod Updater', font=('Segoe UI', 16, 'bold')).pack(anchor='w')
    ttk.Label(container, text='Preview before installing. Close the game before applying updates. Unmapped folders are never changed.').pack(anchor='w', pady=(0, 12))
    top = ttk.Frame(container)
    top.pack(fill='x')
    folder = tk.StringVar(value=cfg['mods_dir'])
    ttk.Label(top, text='Mods folder').pack(side='left')
    ttk.Entry(top, textvariable=folder).pack(side='left', fill='x', expand=True, padx=8)
    def choose():
        f = filedialog.askdirectory(initialdir=folder.get())
        if f:
            folder.set(f)
            save()
    ttk.Button(top, text='Browse', command=choose).pack(side='left')
    columns = ('folder','installed','online','modid','status','message')
    tree = ttk.Treeview(container, columns=columns, show='headings', selectmode='browse')
    for column, width in [('folder',220),('installed',90),('online',90),('modid',95),('status',115),('message',310)]:
        tree.heading(column, text=column.title())
        tree.column(column, width=width, minwidth=55)
    tree.pack(fill='both', expand=True, pady=12)
    ttk.Label(container, text='IDs are found automatically. Select a folder and click Edit Mod ID to correct or manually map it.').pack(anchor='w')
    actions = ttk.Frame(container)
    actions.pack(fill='x', pady=8)
    output = tk.Text(container, height=7, wrap='word', state='disabled')
    output.pack(fill='x')
    def report(s):
        output.configure(state='normal')
        output.insert('end', str(s)+'\n')
        output.see('end')
        output.configure(state='disabled')
    def save():
        cfg['mods_dir'] = folder.get()
        u.atomic_json(u.CONFIG, cfg)
    def scan_local():
        save()
        tree.delete(*tree.get_children())
        plans.clear()
        try:
            found = u.installed_mods(Path(folder.get()))
            for key, entries in sorted(found.items()):
                for p, revision in entries:
                    mid = cfg['mappings'].get(key, {}).get('mod_id','')
                    tree.insert('', 'end', iid=p.name, values=(p.name, revision, '-', mid, 'NOT CHECKED', ''))
            report(f'Found {sum(map(len, found.values()))} revision-named folders.')
        except Exception as e:
            messagebox.showerror('Scan error', str(e))
    def mapping():
        selected = tree.selection()
        if not selected:
            messagebox.showinfo('Choose folder', 'Select a mod folder first')
            return
        parsed = u.parse_folder(selected[0])
        if not parsed:
            return
        key = parsed[0]
        previous = str(cfg['mappings'].get(key,{}).get('mod_id',''))
        value = simpledialog.askstring('Map Paradox mod', f'Enter Paradox mod ID or mod page URL for {key}:', initialvalue=previous, parent=root)
        if value is None:
            return
        match = re.search(r'/mods/(\d+)', value)
        number = match.group(1) if match else value.strip()
        if not number.isdigit() or int(number) <= 0:
            messagebox.showerror('Invalid ID','Enter an integer or a Paradox mod page URL')
            return
        cfg['mappings'][key] = {'mod_id':int(number), 'source':'manual'}
        save()
        scan_local()
        report(f'Mapped {key} to ID {number}. Use Check Online to verify name and revision.')
    def run(apply):
        if busy[0]:
            return
        save()
        busy[0] = True
        for b in (check_btn, update_btn, map_btn, scan_btn, auto_btn):
            b.configure(state='disabled')
        def worker():
            try:
                session = u.network()
                mapped, _ = u.auto_map_mods(cfg, session, lambda x: events.put(('log', x)))
                if mapped:
                    u.atomic_json(u.CONFIG, cfg)
                p = u.plan_updates(cfg, session, lambda x: events.put(('log', x)))
                events.put(('plans', p))
                if apply:
                    for item in p:
                        if item.status == 'UPDATE':
                            try:
                                u.deploy(item, cfg, session, lambda x: events.put(('log', x)))
                            except Exception as e:
                                events.put(('log', f'ERROR {item.key}: {e}'))
                events.put(('log', 'Operation finished.'))
            except Exception as e:
                events.put(('error', str(e)))
            finally:
                events.put(('done', None))
        threading.Thread(target=worker,daemon=True).start()
    def render_plans(items):
        plans[:] = items
        tree.delete(*tree.get_children())
        for p in items:
            tree.insert('', 'end', iid=p.installed_path.name,
                        values=(p.installed_path.name,p.installed_revision,p.online_revision if p.online_revision is not None else '-',p.mod_id or '-',p.status,p.detail))
    def rollback_ui():
        selected=tree.selection()
        if not selected:
            return
        key = u.parse_folder(selected[0])[0]
        if not messagebox.askyesno('Restore backup', f'Rollback last update for {key}? Close the game first.'):
            return
        try:
            report(u.rollback(key,cfg))
            scan_local()
        except Exception as e:
            messagebox.showerror('Rollback error',str(e))
    scan_btn=ttk.Button(actions,text='Scan Local', command=scan_local)
    scan_btn.pack(side='left',padx=3)
    auto_btn=ttk.Button(actions,text='Auto-map IDs',command=lambda:run(False))
    auto_btn.pack(side='left',padx=3)
    map_btn=ttk.Button(actions,text='Edit Mod ID',command=mapping)
    map_btn.pack(side='left',padx=3)
    check_btn=ttk.Button(actions,text='Check Online (Preview)',command=lambda:run(False))
    check_btn.pack(side='left',padx=3)
    def approve():
        if messagebox.askyesno('Confirm update','Close Cities: Skylines II before updating. Download and install all validated updates, keeping backups?'):
            run(True)
    update_btn=ttk.Button(actions,text='Download & Install Updates',command=approve)
    update_btn.pack(side='left',padx=3)
    ttk.Button(actions,text='Rollback Selected',command=rollback_ui).pack(side='left',padx=3)
    def drain():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind=='log':report(value)
                elif kind=='plans':render_plans(value)
                elif kind=='error':report('ERROR: '+value)
                elif kind=='done':
                    busy[0]=False
                    for b in (check_btn,update_btn,map_btn,scan_btn,auto_btn): b.configure(state='normal')
        except queue.Empty:
            pass
        root.after(125,drain)
    scan_local()
    drain()
    root.mainloop()

if __name__=='__main__':launch()
