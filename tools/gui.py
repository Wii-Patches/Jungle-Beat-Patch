#!/usr/bin/env python3
"""Drag-and-drop patcher: pick the controllers you want, drop a disc image on
the window, done.

Extracts the disc, checks sys/main.dol against the exact retail instructions
for its region (USA, Europe or Japan), adds an executable DOL section holding
the chosen controller support, branches to it directly, and rebuilds the
image with wit (Wiimms ISO Tool).

The rebuilt image replaces the original *in place*, keeping its filename and
folder -- USB loaders key off the `/wbfs/<Title> [ID6]/` layout, so a renamed
file next to it can leave the loader unable to find the title. The untouched
original is kept alongside as `<name>.bak`.
"""
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from disc_ids import match_disc_id
import jbpatch
from regions import REGIONS

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except ImportError:                                    # fall back to click-to-browse
    HAVE_DND = False


def find_wit():
    """A wit bundled with this app (PyInstaller build) wins over PATH."""
    name = 'wit.exe' if os.name == 'nt' else 'wit'
    if getattr(sys, 'frozen', False):
        for base in (getattr(sys, '_MEIPASS', None), os.path.dirname(sys.executable)):
            if base:
                bundled = os.path.join(base, name)
                if os.path.isfile(bundled):
                    return bundled
    return shutil.which('wit')


def find_file(root, name):
    for r, _, files in os.walk(root):
        if name in files:
            return os.path.join(r, name)
    return None


def read_disc_id(fst):
    boot = find_file(fst, 'boot.bin')
    if not boot:
        return None
    with open(boot, 'rb') as f:
        return f.read(6).decode('ascii', 'replace')


def run_patch(image_path, classic, gamecube, log, done):
    try:
        wit = find_wit()
        if wit is None:
            raise RuntimeError('wit (Wiimms ISO Tool) not found: not bundled with this '
                               'build and not on PATH')

        fmt = '--iso' if image_path.lower().endswith('.iso') else '--wbfs'

        with tempfile.TemporaryDirectory(prefix='jbpatch_') as tmp:
            fst = os.path.join(tmp, 'fst')
            log('extracting %s...' % os.path.basename(image_path))
            r = subprocess.run([wit, 'extract', image_path, '--dest', fst,
                                '--psel', 'data', '--overwrite', '-q'],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError('extract failed:\n' + (r.stderr or r.stdout))

            disc_id = read_disc_id(fst)
            if not disc_id:
                raise RuntimeError('could not read sys/boot.bin from the extracted disc')
            region = match_disc_id(disc_id, REGIONS)
            if region is None:
                raise RuntimeError(
                    'disc id %s is not a supported target (%s)' % (disc_id, ', '.join(
                        '%s %s' % (r.disc_id, r.name) for r in REGIONS.values())))
            log('disc: %s (%s)' % (disc_id, REGIONS[region].name))

            dol_path = find_file(fst, 'main.dol')
            if not dol_path or os.path.basename(os.path.dirname(dol_path)) != 'sys':
                raise RuntimeError('could not find sys/main.dol in the extracted disc')

            patched_dol = dol_path + '.patched'
            section, sites, size, _ = jbpatch.inject(dol_path, patched_dol, disc_id=region,
                                                     classic=classic, gamecube=gamecube)
            os.replace(patched_dol, dol_path)
            log('  injected %d hooks into DOL text section %d (%d bytes)' %
                (len(sites), section, size))

            staged = os.path.join(tmp, 'patched.img')
            log('rebuilding...')
            r = subprocess.run([wit, 'copy', fst, '--dest', staged, fmt, '--overwrite', '-q'],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError('rebuild failed:\n' + (r.stderr or r.stdout))

            # Only touch the user's file once the rebuild has actually succeeded.
            backup = image_path + '.bak'
            if os.path.exists(backup):
                log('  backup already exists, keeping it: %s' % os.path.basename(backup))
            else:
                shutil.copyfile(image_path, backup)
                log('  backed up original -> %s' % os.path.basename(backup))
            shutil.move(staged, image_path)
            log('done: patched in place, %s' % os.path.basename(image_path))
            done(True, image_path)
    except Exception as e:
        log('ERROR: %s' % e)
        done(False, str(e))


def asset(name):
    if getattr(sys, 'frozen', False):
        return os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(sys.executable)), 'assets', name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets', name)


BASE = TkinterDnD.Tk if HAVE_DND else tk.Tk


class App(BASE):
    def __init__(self):
        super().__init__()
        self.title('Jungle Beat Controller Patcher')
        self.geometry('560x600')
        self.msgq = queue.Queue()
        self.busy = False

        try:
            img = tk.PhotoImage(file=asset('logo.png'))
            self.logo = img.subsample(max(1, img.width() // 260))
            tk.Label(self, image=self.logo).pack(pady=(10, 0))
        except Exception:                              # the window is fine without its logo
            pass
        tk.Label(self, text='Donkey Kong Jungle Beat · USA / Europe / Japan',
                 font=('TkDefaultFont', 11, 'bold')).pack(fill='x', padx=10, pady=(4, 0))

        opts = tk.LabelFrame(self, text='Add support for')
        opts.pack(fill='x', padx=10, pady=(10, 0))
        self.classic = tk.BooleanVar(value=True)
        self.gamecube = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text='Classic Controller', variable=self.classic,
                       anchor='w').pack(fill='x', padx=8)
        tk.Checkbutton(opts, text='GameCube controller and DK Bongos',
                       variable=self.gamecube, anchor='w').pack(fill='x', padx=8)

        hint = ('Drop a .wbfs or .iso here\n\n(or click to choose one)'
                if HAVE_DND else 'Click to choose a .wbfs or .iso')
        self.drop = tk.Label(self, text=hint, relief='ridge', bd=2,
                             padx=10, pady=26, cursor='hand2')
        self.drop.pack(fill='x', padx=10, pady=10)
        self.drop.bind('<Button-1>', lambda e: self.pick())

        if HAVE_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind('<<Drop>>', self.on_drop)

        tk.Label(self, text='The original is kept alongside as <name>.bak',
                 fg='#666').pack()

        self.log = tk.Text(self, height=12, state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True, padx=10, pady=10)

        self.after(100, self.poll_queue)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)      # handles {braced paths with spaces}
        if paths:
            self.start(paths[0])

    def pick(self):
        if self.busy:
            return
        p = filedialog.askopenfilename(
            title='Select disc image',
            filetypes=[('Wii disc image', '*.wbfs *.iso'), ('All files', '*')])
        if p:
            self.start(p)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.msgq.get_nowait()
                if kind == 'log':
                    self.append_log(payload)
                elif kind == 'done':
                    ok, msg = payload
                    self.busy = False
                    self.drop.configure(state='normal')
                    if ok:
                        messagebox.showinfo('Done', 'Patched in place:\n%s' % msg)
                    else:
                        messagebox.showerror('Patch failed', msg)
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def start(self, image_path):
        if self.busy:
            return
        if not os.path.isfile(image_path):
            messagebox.showerror('Not a file', '%s is not a file.' % image_path)
            return
        classic, gamecube = self.classic.get(), self.gamecube.get()
        if not (classic or gamecube):
            messagebox.showerror('Nothing to patch', 'Tick at least one controller option.')
            return

        self.busy = True
        self.drop.configure(state='disabled')
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

        threading.Thread(
            target=run_patch,
            args=(image_path, classic, gamecube,
                  lambda t: self.msgq.put(('log', t)),
                  lambda ok, m: self.msgq.put(('done', (ok, m)))),
            daemon=True,
        ).start()


if __name__ == '__main__':
    App().mainloop()
