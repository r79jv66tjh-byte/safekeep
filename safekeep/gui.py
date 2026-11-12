"""Tkinter-Oberfläche: Tab „Backups“ und Tab „Musik-Duplikate“."""
from __future__ import annotations

import datetime as _dt
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import music, trash
from .backup import (LABEL_SAFETY, SAFETY_JOB_ID, BackupError, BackupStore, Job,
                     human_size, parse_snapshot_time)
from .config import Config

APP_TITLE = "SafeKeep – Backups & Musik-Duplikate"
AUTO_CHECK_MS = 30_000


# --------------------------------------------------------------------- Helfer
class Worker:
    """Führt Arbeit im Hintergrund aus und liefert Ergebnisse im GUI-Thread."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.root.after(80, self._poll)

    def run(self, fn, done=None, error=None, progress=None):
        last = [0.0]

        def prog(msg):
            now = time.monotonic()
            if now - last[0] > 0.08:
                last[0] = now
                self.q.put((progress, (msg,)))

        def target():
            try:
                res = fn(prog)
            except Exception as e:        # noqa: BLE001
                self.q.put((error, (e, traceback.format_exc())))
            else:
                self.q.put((done, (res,)))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        try:
            while True:
                cb, args = self.q.get_nowait()
                if cb:
                    cb(*args)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def open_in_explorer(path):
    path = os.path.abspath(path)
    if os.path.isfile(path):
        subprocess.Popen(["explorer", "/select,", path])
    elif os.path.isdir(path):
        os.startfile(path)


def fmt_time(sid: str, created: str | None = None) -> str:
    t = parse_snapshot_time(sid)
    if t is None and created:
        try:
            t = _dt.datetime.fromisoformat(created)
        except ValueError:
            t = None
    return t.strftime("%d.%m.%Y %H:%M:%S") if t else sid


def fmt_interval(m: int) -> str:
    if m <= 0:
        return "manuell"
    if m % 1440 == 0:
        return f"alle {m // 1440} Tag(e)"
    if m % 60 == 0:
        return f"alle {m // 60} Std."
    return f"alle {m} Min."


def quick_locations() -> list[tuple[str, str]]:
    home = Path.home()
    env = os.environ
    cands = [
        ("Gespeicherte Spiele", home / "Saved Games"),
        ("Dokumente\\My Games", home / "Documents" / "My Games"),
        ("Dokumente", home / "Documents"),
        ("AppData\\Roaming", Path(env.get("APPDATA", home))),
        ("AppData\\Local", Path(env.get("LOCALAPPDATA", home))),
        ("AppData\\LocalLow", home / "AppData" / "LocalLow"),
        ("Bilder", home / "Pictures"),
        ("Desktop", home / "Desktop"),
    ]
    return [(n, str(p)) for n, p in cands if p.is_dir()]


# ------------------------------------------------------------ Job-Dialog
class JobDialog(tk.Toplevel):
    def __init__(self, master, job: Job | None = None):
        super().__init__(master)
        self.title("Sicherung bearbeiten" if job else "Neue Sicherung")
        self.transient(master)
        self.resizable(False, False)
        self.result: Job | None = None
        self.job = job
        pad = {"padx": 6, "pady": 4}

        self.v_name = tk.StringVar(value=job.name if job else "")
        self.v_src = tk.StringVar(value=job.source if job else "")
        self.v_int = tk.IntVar(value=job.interval_min if job else 30)
        self.v_keep = tk.IntVar(value=job.keep if job else 0)
        self.v_excl = tk.StringVar(value=", ".join(job.excludes) if job else "")
        self.v_en = tk.BooleanVar(value=job.enabled if job else True)

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Name:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.v_name, width=44).grid(row=0, column=1, columnspan=2, sticky="we", **pad)

        ttk.Label(f, text="Ordner:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.v_src, width=44).grid(row=1, column=1, sticky="we", **pad)
        ttk.Button(f, text="Durchsuchen …", command=self._browse).grid(row=1, column=2, **pad)

        ttk.Label(f, text="Schnellauswahl:").grid(row=2, column=0, sticky="w", **pad)
        self.quick = quick_locations()
        cb = ttk.Combobox(f, state="readonly", width=41, values=[n for n, _ in self.quick])
        cb.grid(row=2, column=1, sticky="we", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self._browse(self.quick[cb.current()][1]))
        ttk.Label(f, text="(z. B. für Spielstände)", foreground="gray").grid(row=2, column=2, sticky="w")

        ttk.Label(f, text="Automatisch:").grid(row=3, column=0, sticky="w", **pad)
        row = ttk.Frame(f)
        row.grid(row=3, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(row, text="alle").pack(side="left")
        ttk.Spinbox(row, from_=0, to=100000, width=7, textvariable=self.v_int).pack(side="left", padx=4)
        ttk.Label(row, text="Minuten  (0 = nur manuell; es wird nur gesichert, wenn sich etwas geändert hat)").pack(side="left")

        ttk.Label(f, text="Versionen behalten:").grid(row=4, column=0, sticky="w", **pad)
        row = ttk.Frame(f)
        row.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Spinbox(row, from_=0, to=100000, width=7, textvariable=self.v_keep).pack(side="left")
        ttk.Label(row, text="  (0 = alle; ältere wandern in den Papierkorb)").pack(side="left")

        ttk.Label(f, text="Ausschließen:").grid(row=5, column=0, sticky="w", **pad)
        ttk.Entry(f, textvariable=self.v_excl, width=44).grid(row=5, column=1, columnspan=2, sticky="we", **pad)
        ttk.Label(f, text="Muster, kommagetrennt, z. B.  *.tmp, cache, logs/*",
                  foreground="gray").grid(row=6, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Checkbutton(f, text="Aktiv (automatische Sicherung)", variable=self.v_en).grid(
            row=7, column=1, sticky="w", **pad)

        b = ttk.Frame(f)
        b.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(b, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(b, text="Abbrechen", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.wait_window()

    def _browse(self, initial=None):
        d = filedialog.askdirectory(parent=self, initialdir=initial or self.v_src.get() or None,
                                    title="Zu sichernden Ordner wählen")
        if d:
            self.v_src.set(os.path.normpath(d))
            if not self.v_name.get().strip():
                self.v_name.set(os.path.basename(os.path.normpath(d)))

    def _ok(self):
        name = self.v_name.get().strip()
        src = self.v_src.get().strip()
        if not name or not src:
            messagebox.showwarning("Fehlt", "Bitte Name und Ordner angeben.", parent=self)
            return
        if not os.path.isdir(src):
            messagebox.showwarning("Ordner", "Der Ordner existiert nicht.", parent=self)
            return
        try:
            interval = max(0, int(self.v_int.get()))
            keep = max(0, int(self.v_keep.get()))
        except (tk.TclError, ValueError):
            messagebox.showwarning("Zahl", "Intervall und Versionen müssen Zahlen sein.", parent=self)
            return
        excl = [x.strip() for x in self.v_excl.get().split(",") if x.strip()]
        if self.job:
            j = self.job
            j.name, j.source, j.interval_min, j.keep, j.excludes, j.enabled = (
                name, src, interval, keep, excl, self.v_en.get())
        else:
            j = Job(name=name, source=src, interval_min=interval, keep=keep,
                    excludes=excl, enabled=self.v_en.get())
        self.result = j
        self.destroy()


# ------------------------------------------------------- Wiederherstellen
STATUS_TEXT = {"neu": "wird erstellt", "geändert": "wird ersetzt", "gleich": "unverändert",
               "nur_aktuell": "nicht im Backup", "konflikt": "Konflikt"}


class RestoreDialog(tk.Toplevel):
    def __init__(self, app: "BackupTab", job: Job, store: BackupStore, manifest: dict):
        super().__init__(app)
        self.app, self.job, self.store, self.manifest = app, job, store, manifest
        self.plan = []
        self.title(f"Wiederherstellen – {job.name} – {fmt_time(manifest['id'], manifest.get('created'))}")
        self.geometry("900x600")
        self.transient(app.winfo_toplevel())

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        self.v_mode = tk.StringVar(value="orig")
        self.v_target = tk.StringVar(value=manifest.get("source") or job.source)
        ttk.Radiobutton(top, text=f"In den Originalordner:  {job.source}", value="orig",
                        variable=self.v_mode, command=self._mode).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(top, text="In einen anderen Ordner:", value="other",
                        variable=self.v_mode, command=self._mode).grid(row=1, column=0, sticky="w")
        self.e_target = ttk.Entry(top, textvariable=self.v_target, width=70, state="disabled")
        self.e_target.grid(row=1, column=1, sticky="we", padx=4)
        self.b_target = ttk.Button(top, text="…", width=3, command=self._pick, state="disabled")
        self.b_target.grid(row=1, column=2)
        self.v_hide = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Unveränderte ausblenden", variable=self.v_hide,
                        command=self._fill).grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Button(top, text="Vorschau aktualisieren", command=self._compute).grid(row=2, column=1, sticky="w", pady=(6, 0))

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="both", expand=True)
        cols = ("status", "size", "mtime")
        self.tree = ttk.Treeview(mid, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Datei")
        self.tree.heading("status", text="Aktion")
        self.tree.heading("size", text="Größe")
        self.tree.heading("mtime", text="Stand im Backup")
        self.tree.column("#0", width=430)
        self.tree.column("status", width=130)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("mtime", width=140)
        for tag, color in (("neu", "#1a7f37"), ("geändert", "#b35900"), ("gleich", "gray"),
                           ("nur_aktuell", "#0b5cad"), ("konflikt", "red")):
            self.tree.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._summary())
        self.tree.bind("<Double-1>", lambda e: self._open_version())

        bot = ttk.Frame(self, padding=8)
        bot.pack(fill="x")
        self.v_extra = tk.BooleanVar(value=False)
        ttk.Checkbutton(bot, text="Dateien, die es in diesem Backup nicht gab, in den Papierkorb verschieben",
                        variable=self.v_extra, command=self._compute).pack(anchor="w")
        ttk.Label(bot, text="Tipp: Zeilen markieren, um nur diese wiederherzustellen. Ohne Markierung wird alles "
                            "wiederhergestellt. Doppelklick öffnet die gesicherte Version.",
                  foreground="gray").pack(anchor="w")
        self.l_sum = ttk.Label(bot, text="", font=("Segoe UI", 9, "bold"))
        self.l_sum.pack(anchor="w", pady=4)
        b = ttk.Frame(bot)
        b.pack(anchor="e")
        ttk.Button(b, text="Gesicherte Version öffnen", command=self._open_version).pack(side="left", padx=4)
        self.b_go = ttk.Button(b, text="Wiederherstellen", command=self._restore)
        self.b_go.pack(side="left", padx=4)
        ttk.Button(b, text="Schließen", command=self.destroy).pack(side="left")
        self._compute()

    def target(self) -> str:
        return self.job.source if self.v_mode.get() == "orig" else self.v_target.get().strip()

    def _mode(self):
        st = "normal" if self.v_mode.get() == "other" else "disabled"
        self.e_target.configure(state=st)
        self.b_target.configure(state=st)
        self._compute()

    def _pick(self):
        d = filedialog.askdirectory(parent=self, title="Zielordner wählen")
        if d:
            self.v_target.set(os.path.normpath(d))
            self._compute()

    def _compute(self):
        tgt = self.target()
        if not tgt:
            return
        self.l_sum.configure(text="Vergleiche mit aktuellem Stand …")
        self.b_go.configure(state="disabled")
        self.app.worker.run(
            lambda p: self.store.plan_restore(self.manifest, tgt, None, self.job.excludes, with_extras=True),
            done=self._planned,
            error=lambda e, tb: self.l_sum.configure(text=f"Fehler: {e}"))

    def _planned(self, plan):
        if not self.winfo_exists():
            return
        self.plan = plan
        self._fill()
        self.b_go.configure(state="normal")

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.plan:
            if self.v_hide.get() and p.status == "gleich":
                continue
            mt = ""
            if p.status != "nur_aktuell" and p.mtime_ns:
                mt = _dt.datetime.fromtimestamp(p.mtime_ns / 1e9).strftime("%d.%m.%Y %H:%M")
            status = STATUS_TEXT[p.status]
            if p.status == "nur_aktuell":
                status = "→ Papierkorb" if self.v_extra.get() else "bleibt liegen"
            self.tree.insert("", "end", iid=p.rel, text=p.rel,
                             values=(status, human_size(p.size), mt), tags=(p.status,))
        self._summary()

    def _selected_plan(self):
        sel = set(self.tree.selection())
        return [p for p in self.plan if not sel or p.rel in sel]

    def _summary(self):
        items = self._selected_plan()
        c = {k: 0 for k in STATUS_TEXT}
        for p in items:
            c[p.status] += 1
        txt = (f"{'Auswahl' if self.tree.selection() else 'Alles'}:  {c['neu']} neu erstellen · "
               f"{c['geändert']} ersetzen · {c['gleich']} unverändert")
        if c["nur_aktuell"]:
            txt += (f" · {c['nur_aktuell']} nicht im Backup "
                    f"({'→ Papierkorb' if self.v_extra.get() else 'bleiben liegen'})")
        if c["geändert"] or (self.v_extra.get() and c["nur_aktuell"]):
            txt += "\nDer aktuelle Stand wird vorher automatisch gesichert."
        self.l_sum.configure(text=txt)

    def _open_version(self):
        sel = self.tree.selection()
        if not sel:
            return
        rel = sel[0]
        e = self.manifest["files"].get(rel)
        if not e:
            messagebox.showinfo("Nicht im Backup", "Diese Datei gibt es in dem Backup nicht.", parent=self)
            return
        try:
            path = self.store.export_file(e, rel)
            os.startfile(path)
        except OSError as ex:
            messagebox.showerror("Fehler", str(ex), parent=self)

    def _restore(self):
        items = self._selected_plan()
        sel = [p.rel for p in items] if self.tree.selection() else None
        c = {k: 0 for k in STATUS_TEXT}
        for p in items:
            c[p.status] += 1
        n_extra = c["nur_aktuell"] if self.v_extra.get() else 0
        if c["neu"] + c["geändert"] + n_extra == 0:
            messagebox.showinfo("Nichts zu tun", "Der Ziel-Ordner entspricht bereits diesem Backup.", parent=self)
            return
        msg = (f"Ziel: {self.target()}\n\n{c['neu']} Datei(en) neu erstellen\n"
               f"{c['geändert']} Datei(en) ersetzen\n")
        if n_extra:
            msg += f"{n_extra} Datei(en) in den Papierkorb verschieben\n"
        if c["geändert"] or n_extra:
            msg += "\nDer aktuelle Stand wird vorher automatisch als neue Version gesichert."
        if not messagebox.askyesno("Wiederherstellen?", msg, parent=self):
            return
        if not self.app.begin_busy("Wiederherstellen"):
            return
        self.b_go.configure(state="disabled")
        tgt, extra = self.target(), self.v_extra.get()
        self.app.worker.run(
            lambda p: self.store.restore(self.job, self.manifest, tgt, sel, extra, progress=p),
            done=self._restored, error=self._failed, progress=self.app.status)

    def _restored(self, res):
        self.app.end_busy()
        text = f"{len(res.written)} Datei(en) wiederhergestellt."
        if res.trashed:
            text += f"\n{len(res.trashed)} Datei(en) in den Papierkorb verschoben."
        if res.safety_snapshot:
            where = ("im Verlauf dieser Sicherung" if res.safety_job == self.job.id
                     else "unter „Sicherungen vor Wiederherstellung“")
            text += f"\n\nDer vorherige Stand wurde {where} gesichert ({LABEL_SAFETY})."
        self.app.status(text.replace("\n", " "))
        self.app.refresh_snapshots()
        messagebox.showinfo("Fertig", text, parent=self)
        if self.winfo_exists():
            self._compute()

    def _failed(self, e, tb):
        self.app.end_busy()
        self.app.status(f"Wiederherstellung fehlgeschlagen: {e}")
        if self.winfo_exists():
            self.b_go.configure(state="normal")
            messagebox.showerror("Wiederherstellung fehlgeschlagen", str(e), parent=self)


# --------------------------------------------------------------- Tab Backups
class BackupTab(ttk.Frame):
    def __init__(self, master, cfg: Config, worker: Worker, status_cb):
        super().__init__(master, padding=8)
        self.cfg, self.worker, self.status = cfg, worker, status_cb
        self.busy = False
        self.store = BackupStore(cfg["backup_root"])

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Backup-Speicherort:").pack(side="left")
        self.v_root = tk.StringVar(value=cfg["backup_root"])
        ttk.Entry(top, textvariable=self.v_root, state="readonly", width=60).pack(side="left", padx=4)
        ttk.Button(top, text="Ändern …", command=self.change_root).pack(side="left")
        ttk.Button(top, text="Öffnen", command=lambda: open_in_explorer(self.store.root)).pack(side="left", padx=4)
        self.v_auto = tk.BooleanVar(value=cfg["auto"])
        ttk.Checkbutton(top, text="Automatische Sicherung aktiv", variable=self.v_auto,
                        command=self._auto_changed).pack(side="right")

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, pady=(8, 0))

        # Jobs
        left = ttk.Labelframe(pw, text="Gesicherte Ordner", padding=6)
        pw.add(left, weight=1)
        self.jobs = ttk.Treeview(left, columns=("int", "last"), selectmode="browse", height=12)
        self.jobs.heading("#0", text="Name")
        self.jobs.heading("int", text="Intervall")
        self.jobs.heading("last", text="Zuletzt geprüft")
        self.jobs.column("#0", width=170)
        self.jobs.column("int", width=90)
        self.jobs.column("last", width=120)
        self.jobs.pack(fill="both", expand=True)
        self.jobs.bind("<<TreeviewSelect>>", lambda e: self.refresh_snapshots())
        self.jobs.bind("<Double-1>", lambda e: self.edit_job())
        self.l_src = ttk.Label(left, text="", foreground="gray", wraplength=380)
        self.l_src.pack(anchor="w", pady=4)
        jb = ttk.Frame(left)
        jb.pack(fill="x")
        ttk.Button(jb, text="Neu …", command=self.new_job).pack(side="left")
        ttk.Button(jb, text="Bearbeiten …", command=self.edit_job).pack(side="left", padx=3)
        ttk.Button(jb, text="Entfernen", command=self.remove_job).pack(side="left")
        ttk.Button(jb, text="Ordner öffnen", command=self.open_job_folder).pack(side="left", padx=3)
        jb2 = ttk.Frame(left)
        jb2.pack(fill="x", pady=(4, 0))
        ttk.Button(jb2, text="Jetzt sichern", command=self.backup_selected).pack(side="left")
        ttk.Button(jb2, text="Alle sichern", command=self.backup_all).pack(side="left", padx=3)

        # Verlauf
        right = ttk.Labelframe(pw, text="Verlauf (Zeitmaschine)", padding=6)
        pw.add(right, weight=2)
        cols = ("label", "files", "size", "diff")
        self.snaps = ttk.Treeview(right, columns=cols, selectmode="extended", height=12)
        self.snaps.heading("#0", text="Zeitpunkt")
        self.snaps.heading("label", text="Art")
        self.snaps.heading("files", text="Dateien")
        self.snaps.heading("size", text="Größe")
        self.snaps.heading("diff", text="Änderungen (neu/geändert/weg)")
        self.snaps.column("#0", width=150)
        self.snaps.column("label", width=170)
        self.snaps.column("files", width=60, anchor="e")
        self.snaps.column("size", width=80, anchor="e")
        self.snaps.column("diff", width=180)
        self.snaps.tag_configure("safety", foreground="#b35900")
        self.snaps.tag_configure("err", foreground="red")
        sb = ttk.Scrollbar(right, orient="vertical", command=self.snaps.yview)
        self.snaps.configure(yscrollcommand=sb.set)
        bb = ttk.Frame(right)
        bb.pack(side="bottom", fill="x", pady=(6, 0))
        self.snaps.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.snaps.bind("<Double-1>", lambda e: self.restore_selected())
        ttk.Button(bb, text="Wiederherstellen …", command=self.restore_selected).pack(side="left")
        ttk.Button(bb, text="Fehler anzeigen", command=self.show_errors).pack(side="left", padx=3)
        ttk.Button(bb, text="Version löschen (Papierkorb)", command=self.delete_snapshots).pack(side="left")
        ttk.Button(bb, text="Speicher aufräumen", command=self.gc).pack(side="left", padx=3)
        self.l_store = ttk.Label(bb, text="", foreground="gray")
        self.l_store.pack(side="right")

        self.refresh_jobs()
        self.after(1500, self.auto_tick)

    # ---- Zustand
    def begin_busy(self, what: str) -> bool:
        if self.busy:
            messagebox.showinfo("Bitte warten", "Es läuft bereits eine Sicherung/Wiederherstellung.")
            return False
        self.busy = True
        self.status(f"{what} …")
        self.winfo_toplevel().configure(cursor="watch")
        return True

    def end_busy(self):
        self.busy = False
        self.winfo_toplevel().configure(cursor="")

    def selected_job(self) -> Job | None:
        sel = self.jobs.selection()
        if not sel:
            return None
        if sel[0] == SAFETY_JOB_ID:
            return Job(name="Sicherungen vor Wiederherstellung", source="", id=SAFETY_JOB_ID)
        return next((j for j in self.cfg.jobs if j.id == sel[0]), None)

    def refresh_jobs(self):
        sel = self.jobs.selection()
        self.jobs.delete(*self.jobs.get_children())
        for j in self.cfg.jobs:
            last = ""
            if j.last_run:
                try:
                    last = _dt.datetime.fromisoformat(j.last_run).strftime("%d.%m. %H:%M")
                except ValueError:
                    pass
            name = j.name + ("" if j.enabled else " (pausiert)")
            self.jobs.insert("", "end", iid=j.id, text=name, values=(fmt_interval(j.interval_min), last))
        if self.store.snapshot_ids(SAFETY_JOB_ID):
            self.jobs.insert("", "end", iid=SAFETY_JOB_ID, text="↺ Sicherungen vor Wiederherstellung",
                             values=("", ""))
        if sel and self.jobs.exists(sel[0]):
            self.jobs.selection_set(sel[0])
        elif self.jobs.get_children():
            self.jobs.selection_set(self.jobs.get_children()[0])
        self.refresh_snapshots()

    def refresh_snapshots(self):
        self.snaps.delete(*self.snaps.get_children())
        job = self.selected_job()
        self.l_src.configure(text=f"Ordner: {job.source}" if job and job.source else "")
        if not job:
            return
        snaps = self.store.list_snapshots(job.id)
        for i, m in enumerate(snaps):
            older = snaps[i + 1] if i + 1 < len(snaps) else None
            if m.get("partial"):
                diff = f"{len(m['files'])} Datei(en) gesichert"
            else:
                a, c, r = BackupStore.diff_manifests(older, m)
                diff = f"+{a}  ~{c}  −{r}" if older else "erste Sicherung"
            tags = []
            label = m.get("label", "")
            if label.startswith(LABEL_SAFETY):
                tags.append("safety")
            if m.get("errors"):
                tags.append("err")
                label += f"  ⚠ {len(m['errors'])} Fehler"
            self.snaps.insert("", "end", iid=m["id"], text=fmt_time(m["id"], m.get("created")),
                              values=(label, len(m["files"]), human_size(m.get("total_size", 0)), diff),
                              tags=tags)
        self.l_store.configure(text=f"{len(snaps)} Version(en)")

    # ---- Jobs
    def new_job(self):
        d = JobDialog(self.winfo_toplevel())
        if d.result:
            self.cfg.jobs.append(d.result)
            self.cfg.save()
            self.refresh_jobs()
            self.jobs.selection_set(d.result.id)
            if messagebox.askyesno("Erste Sicherung", "Jetzt gleich die erste Sicherung anlegen?"):
                self.run_backup([d.result], "Manuell")

    def edit_job(self):
        job = self.selected_job()
        if not job or job.id == SAFETY_JOB_ID:
            return
        d = JobDialog(self.winfo_toplevel(), job)
        if d.result:
            self.cfg.save()
            self.refresh_jobs()

    def remove_job(self):
        job = self.selected_job()
        if not job or job.id == SAFETY_JOB_ID:
            return
        if messagebox.askyesno("Entfernen", f"„{job.name}“ nicht mehr sichern?\n\n"
                               "Die vorhandenen Backups bleiben im Backup-Speicher erhalten."):
            self.cfg.jobs.remove(job)
            self.cfg.save()
            self.refresh_jobs()

    def open_job_folder(self):
        job = self.selected_job()
        if job and job.source:
            open_in_explorer(job.source)

    def change_root(self):
        d = filedialog.askdirectory(title="Backup-Speicherort wählen", initialdir=self.cfg["backup_root"])
        if not d:
            return
        d = os.path.normpath(d)
        for j in self.cfg.jobs:
            src = os.path.normcase(os.path.abspath(j.source))
            if os.path.normcase(d) == src:
                messagebox.showerror("Ungünstig", "Der Speicherort darf kein gesicherter Ordner sein.")
                return
        messagebox.showinfo("Hinweis", "Bisherige Backups bleiben am alten Ort liegen und werden "
                                       "nicht verschoben. Der Verlauf zeigt ab jetzt den neuen Ort.")
        self.cfg["backup_root"] = d
        self.cfg.save()
        self.v_root.set(d)
        self.store = BackupStore(d)
        self.refresh_jobs()

    # ---- Sichern
    def backup_selected(self):
        job = self.selected_job()
        if job and job.id != SAFETY_JOB_ID:
            self.run_backup([job], "Manuell", only_if_changed=False)

    def backup_all(self):
        self.run_backup([j for j in self.cfg.jobs], "Manuell")

    def run_backup(self, jobs, label, only_if_changed=True, quiet=False):
        if not jobs or not self.begin_busy("Sichere"):
            return
        store = self.store

        def work(prog):
            out = []
            for j in jobs:
                try:
                    r = store.backup_job(j, label, only_if_changed, progress=lambda m, j=j: prog(f"{j.name}: {m}"))
                    out.append((j, r, None))
                except Exception as e:      # noqa: BLE001
                    out.append((j, None, e))
            return out

        def done(results):
            self.end_busy()
            self.cfg.save()
            msgs = []
            for j, r, e in results:
                if e:
                    msgs.append(f"{j.name}: FEHLER – {e}")
                elif r.snapshot_id:
                    m = f"{j.name}: gesichert ({r.file_count} Dateien, {human_size(r.new_bytes)} neu)"
                    if r.errors:
                        m += f", {len(r.errors)} nicht lesbar"
                    msgs.append(m)
                else:
                    msgs.append(f"{j.name}: keine Änderungen")
            self.status(" | ".join(msgs))
            self.refresh_jobs()
            if not quiet and any(e for _, _, e in results):
                messagebox.showerror("Sicherung", "\n".join(msgs))

        self.worker.run(work, done=done, error=lambda e, tb: (self.end_busy(), self.status(f"Fehler: {e}")),
                        progress=self.status)

    def _auto_changed(self):
        self.cfg["auto"] = self.v_auto.get()
        self.cfg.save()

    def auto_tick(self):
        try:
            if self.v_auto.get() and not self.busy:
                due = [j for j in self.cfg.jobs if j.is_due() and os.path.isdir(j.source)]
                if due:
                    self.run_backup(due, "Automatisch", only_if_changed=True, quiet=True)
        finally:
            self.after(AUTO_CHECK_MS, self.auto_tick)

    # ---- Verlauf
    def _selected_manifests(self):
        job = self.selected_job()
        if not job:
            return None, []
        return job, [self.store.load_manifest(job.id, sid) for sid in self.snaps.selection()]

    def restore_selected(self):
        job, ms = self._selected_manifests()
        if not ms:
            messagebox.showinfo("Version wählen", "Bitte im Verlauf eine Version auswählen.")
            return
        m = ms[0]
        if job.id == SAFETY_JOB_ID:
            job = Job(name="Sicherung vor Wiederherstellung", source=m.get("source", ""), id=SAFETY_JOB_ID)
        RestoreDialog(self, job, self.store, m)

    def show_errors(self):
        _job, ms = self._selected_manifests()
        if not ms:
            return
        errs = ms[0].get("errors") or []
        if not errs:
            messagebox.showinfo("Keine Fehler", "Bei dieser Sicherung konnten alle Dateien gelesen werden.")
            return
        text = "\n".join(f"{e['pfad']}: {e['fehler']}" for e in errs[:40])
        messagebox.showwarning("Nicht gesicherte Dateien",
                               "Diese Dateien waren gesperrt/nicht lesbar. Von ihnen steckt der letzte "
                               "bekannte Stand in der Version:\n\n" + text)

    def delete_snapshots(self):
        job, ms = self._selected_manifests()
        if not ms:
            return
        if not messagebox.askyesno("Versionen löschen",
                                   f"{len(ms)} Version(en) in den Papierkorb verschieben?\n"
                                   "Nicht mehr benötigte Dateiinhalte werden ebenfalls in den Papierkorb verschoben."):
            return
        try:
            n = self.store.delete_snapshots(job.id, [m["id"] for m in ms])
            cnt, size = self.store.collect_garbage()
            self.status(f"{n} Version(en) und {cnt} Inhalt(e) ({human_size(size)}) in den Papierkorb verschoben.")
        except (OSError, trash.TrashError) as e:
            messagebox.showerror("Fehler", str(e))
        self.refresh_jobs()

    def gc(self):
        try:
            cnt, size = self.store.collect_garbage()
            self.status(f"Aufgeräumt: {cnt} ungenutzte Datei(en), {human_size(size)} → Papierkorb. "
                        f"Belegt: {human_size(self.store.storage_size())}")
        except (OSError, trash.TrashError) as e:
            messagebox.showerror("Fehler", str(e))


# ---------------------------------------------------------- Tab Musik
class MusicTab(ttk.Frame):
    def __init__(self, master, cfg: Config, worker: Worker, status_cb):
        super().__init__(master, padding=8)
        self.cfg, self.worker, self.status = cfg, worker, status_cb
        self.groups: list[music.DupGroup] = []
        self.marks: dict[str, bool] = {}       # path -> True = in den Papierkorb
        self.cancel = threading.Event()
        self.running = False

        top = ttk.Frame(self)
        top.pack(fill="x")
        lf = ttk.Labelframe(top, text="Musik-Ordner", padding=6)
        lf.pack(side="left", fill="x", expand=True)
        self.lb = tk.Listbox(lf, height=4)
        self.lb.pack(side="left", fill="x", expand=True)
        for f in cfg["music_folders"]:
            self.lb.insert("end", f)
        fb = ttk.Frame(lf)
        fb.pack(side="left", padx=4)
        ttk.Button(fb, text="Hinzufügen …", command=self.add_folder).pack(fill="x")
        ttk.Button(fb, text="Entfernen", command=self.remove_folder).pack(fill="x", pady=3)

        of = ttk.Labelframe(top, text="Suche", padding=6)
        of.pack(side="left", fill="y", padx=(8, 0))
        self.v_same = tk.BooleanVar(value=cfg["music_same_song"])
        ttk.Checkbutton(of, text="Auch gleiche Songs in anderer Qualität / mit anderem Namen",
                        variable=self.v_same).pack(anchor="w")
        r = ttk.Frame(of)
        r.pack(anchor="w", pady=3)
        ttk.Label(r, text="Längen-Toleranz:").pack(side="left")
        self.v_tol = tk.IntVar(value=cfg["music_tolerance"])
        ttk.Spinbox(r, from_=0, to=30, width=4, textvariable=self.v_tol).pack(side="left", padx=4)
        ttk.Label(r, text="Sekunden").pack(side="left")
        r = ttk.Frame(of)
        r.pack(anchor="w")
        self.b_scan = ttk.Button(r, text="Duplikate suchen", command=self.scan)
        self.b_scan.pack(side="left")
        ttk.Button(r, text="Abbrechen", command=self.cancel.set).pack(side="left", padx=4)
        if not music.HAVE_MUTAGEN:
            ttk.Label(of, text="Hinweis: Paket „mutagen“ fehlt – nur Dateinamen + identische Dateien.\n"
                               "Installieren:  python -m pip install mutagen", foreground="#b35900").pack(anchor="w")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, pady=(8, 0))
        cols = ("action", "artist", "dur", "quality", "size", "folder")
        self.tree = ttk.Treeview(mid, columns=cols, selectmode="extended")
        for c, t, w in (("#0", "Datei / Gruppe", 300), ("action", "Aktion", 110), ("artist", "Interpret", 140),
                        ("dur", "Länge", 55), ("quality", "Qualität", 190), ("size", "Größe", 75),
                        ("folder", "Ordner", 260)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="e" if c in ("dur", "size") else "w")
        self.tree.tag_configure("group", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("keep", foreground="#1a7f37")
        self.tree.tag_configure("del", foreground="#b3261e")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.toggle())
        self.tree.bind("<space>", lambda e: self.toggle())

        bot = ttk.Frame(self)
        bot.pack(fill="x", pady=(6, 0))
        ttk.Button(bot, text="Behalten ⇄ Löschen", command=self.toggle).pack(side="left")
        ttk.Button(bot, text="Als beste Version behalten", command=self.make_best).pack(side="left", padx=3)
        ttk.Button(bot, text="Abspielen", command=self.play).pack(side="left")
        ttk.Button(bot, text="Im Explorer zeigen", command=self.reveal).pack(side="left", padx=3)
        self.b_del = ttk.Button(bot, text="Markierte in den Papierkorb", command=self.delete_marked)
        self.b_del.pack(side="right")
        self.l_sum = ttk.Label(bot, text="", foreground="gray")
        self.l_sum.pack(side="right", padx=8)

    # ---- Ordner
    def _save_folders(self):
        self.cfg["music_folders"] = list(self.lb.get(0, "end"))
        self.cfg.save()

    def add_folder(self):
        d = filedialog.askdirectory(title="Musik-Ordner wählen")
        if d:
            self.lb.insert("end", os.path.normpath(d))
            self._save_folders()

    def remove_folder(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
        self._save_folders()

    # ---- Suche
    def scan(self):
        folders = [f for f in self.lb.get(0, "end") if os.path.isdir(f)]
        if not folders:
            messagebox.showinfo("Ordner", "Bitte zuerst einen Musik-Ordner hinzufügen.")
            return
        if self.running:
            return
        try:
            tol = max(0, int(self.v_tol.get()))
        except (tk.TclError, ValueError):
            tol = 3
        self.cfg["music_tolerance"] = tol
        self.cfg["music_same_song"] = self.v_same.get()
        self.cfg.save()
        self.cancel.clear()
        self.running = True
        self.b_scan.configure(state="disabled")
        same = self.v_same.get()
        self.worker.run(
            lambda p: music.find_duplicates(folders, same, tol, progress=p, cancel=self.cancel.is_set),
            done=self._scanned, error=self._scan_failed, progress=self.status)

    def _scan_failed(self, e, tb):
        self.running = False
        self.b_scan.configure(state="normal")
        self.status("Suche abgebrochen." if isinstance(e, InterruptedError) else f"Fehler bei der Suche: {e}")

    def _scanned(self, groups):
        self.running = False
        self.b_scan.configure(state="normal")
        self.groups = groups
        self.marks = {}
        for g in groups:
            for t in g.tracks:
                self.marks[t.path] = t is not g.best
        self._fill()
        self.status(f"{len(groups)} Duplikat-Gruppe(n) gefunden.")

    def _fill(self):
        self.tree.delete(*self.tree.get_children())
        for gi, g in enumerate(self.groups):
            gid = f"g{gi}"
            self.tree.insert("", "end", iid=gid, open=True, tags=("group",),
                             text=f"{g.label}   ({len(g.tracks)}× · {g.reason})",
                             values=("", "", "", "", "", ""))
            for t in g.tracks:
                dur = f"{int(t.duration // 60)}:{int(t.duration % 60):02d}" if t.duration else ""
                d = self.marks.get(t.path)
                self.tree.insert(gid, "end", iid=t.path, text=t.name, tags=("del" if d else "keep",),
                                 values=("✗ Papierkorb" if d else "★ behalten", t.artist, dur,
                                         t.quality_text(), human_size(t.size), os.path.dirname(t.path)))
        self._summary()

    def _summary(self):
        paths = [p for p, d in self.marks.items() if d]
        size = 0
        for g in self.groups:
            for t in g.tracks:
                if self.marks.get(t.path):
                    size += t.size
        self.l_sum.configure(text=f"{len(paths)} Datei(en) markiert · {human_size(size)} frei werdend")

    def _set(self, path, delete):
        self.marks[path] = delete
        if self.tree.exists(path):
            self.tree.set(path, "action", "✗ Papierkorb" if delete else "★ behalten")
            self.tree.item(path, tags=("del" if delete else "keep",))

    def _sel_tracks(self):
        return [i for i in self.tree.selection() if i in self.marks]

    def toggle(self):
        for p in self._sel_tracks():
            self._set(p, not self.marks[p])
        self._summary()

    def make_best(self):
        for p in self._sel_tracks():
            parent = self.tree.parent(p)
            for sib in self.tree.get_children(parent):
                self._set(sib, sib != p)
        self._summary()

    def play(self):
        sel = self._sel_tracks()
        if sel:
            os.startfile(sel[0])

    def reveal(self):
        sel = self._sel_tracks()
        if sel:
            open_in_explorer(sel[0])

    def delete_marked(self):
        to_del, blocked = [], []
        for g in self.groups:
            dels = [t for t in g.tracks if self.marks.get(t.path)]
            if len(dels) == len(g.tracks):
                blocked.append(g.label)
                continue
            to_del.extend(dels)
        if blocked:
            messagebox.showwarning("Mindestens eine behalten",
                                   "In diesen Gruppen wäre keine Version mehr übrig – sie werden übersprungen:\n\n"
                                   + "\n".join(blocked[:15]))
        if not to_del:
            return
        size = sum(t.size for t in to_del)
        if not messagebox.askyesno("In den Papierkorb",
                                   f"{len(to_del)} Datei(en) ({human_size(size)}) in den Papierkorb verschieben?\n"
                                   "Sie lassen sich von dort wiederherstellen."):
            return
        try:
            trash.move_to_trash([t.path for t in to_del])
        except (OSError, trash.TrashError) as e:
            messagebox.showerror("Fehler", str(e))
        gone = {t.path for t in to_del if not os.path.exists(t.path)}
        for g in self.groups:
            g.tracks = [t for t in g.tracks if t.path not in gone]
        self.groups = [g for g in self.groups if len(g.tracks) > 1]
        for p in gone:
            self.marks.pop(p, None)
        self._fill()
        self.status(f"{len(gone)} Datei(en) in den Papierkorb verschoben.")


# ------------------------------------------------------------------- App
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1150x680")
        self.minsize(900, 500)
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass
        self.cfg = Config()
        self.worker = Worker(self)
        self.v_status = tk.StringVar(value="Bereit.")
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.backup_tab = BackupTab(nb, self.cfg, self.worker, self.set_status)
        self.music_tab = MusicTab(nb, self.cfg, self.worker, self.set_status)
        nb.add(self.backup_tab, text="  Backups  ")
        nb.add(self.music_tab, text="  Musik-Duplikate  ")
        ttk.Label(self, textvariable=self.v_status, relief="sunken", anchor="w",
                  padding=(6, 2)).pack(fill="x", side="bottom")

    def set_status(self, msg: str):
        self.v_status.set(msg)


def main():
    App().mainloop()
