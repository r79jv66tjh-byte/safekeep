"""Versionierte Ordner-Backups ("Zeitmaschine").

Aufbau des Backup-Speichers:
    <root>/objects/ab/abcdef…   Dateiinhalte, benannt nach SHA-256 (unveränderlich)
    <root>/snapshots/<job>/<id>.json   ein Manifest pro Sicherung
    <root>/tmp/                  Zwischendateien

Grundregeln:
* Ein Objekt wird nie überschrieben (os.rename schlägt fehl, wenn das Ziel existiert).
* Ein Manifest wird nie überschrieben – jede Sicherung bekommt eine neue Datei.
* Vor jeder Wiederherstellung, die vorhandene Dateien ersetzt oder entfernt,
  wird der aktuelle Stand automatisch gesichert. Klappt das nicht vollständig,
  wird abgebrochen.
* Gelöscht wird nur über den Papierkorb.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import trash

CHUNK = 1024 * 1024
MANIFEST_VERSION = 1
SAFETY_JOB_ID = "_wiederherstellung"
LABEL_SAFETY = "Vor Wiederherstellung"


class BackupError(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Job:
    name: str
    source: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    interval_min: int = 0          # 0 = nur manuell
    keep: int = 0                  # 0 = alle Versionen behalten
    excludes: list = field(default_factory=list)
    enabled: bool = True
    last_run: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    def is_due(self, now: _dt.datetime | None = None) -> bool:
        if not self.enabled or self.interval_min <= 0:
            return False
        if not self.last_run:
            return True
        now = now or _dt.datetime.now()
        try:
            last = _dt.datetime.fromisoformat(self.last_run)
        except ValueError:
            return True
        return (now - last).total_seconds() >= self.interval_min * 60


@dataclass
class SnapshotResult:
    snapshot_id: str | None
    changed: bool
    file_count: int
    total_size: int
    new_bytes: int
    errors: list
    manifest: dict | None = None


@dataclass
class PlanItem:
    rel: str
    status: str          # neu | geändert | gleich | nur_aktuell | konflikt
    size: int
    mtime_ns: int | None = None


@dataclass
class RestoreResult:
    safety_snapshot: str | None
    safety_job: str | None
    written: list
    trashed: list


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def is_excluded(rel: str, name: str, patterns) -> bool:
    rel = rel.replace("\\", "/").lower()
    name = name.lower()
    for p in patterns or ():
        p = p.strip().replace("\\", "/").lower()
        if p and (fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p)):
            return True
    return False


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _in_selection(rel: str, selection) -> bool:
    if not selection:
        return True
    for s in selection:
        s = s.rstrip("/")
        if rel == s or rel.startswith(s + "/"):
            return True
    return False


def parse_snapshot_time(sid: str) -> _dt.datetime | None:
    try:
        return _dt.datetime.strptime(sid[:22], "%Y%m%d-%H%M%S-%f")
    except ValueError:
        return None


class BackupStore:
    def __init__(self, root):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.snapshots = self.root / "snapshots"
        self.tmp = self.root / "tmp"
        self._cache: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------ Grundlagen
    def ensure(self):
        for d in (self.objects, self.snapshots, self.tmp):
            d.mkdir(parents=True, exist_ok=True)

    def object_path(self, h: str) -> Path:
        return self.objects / h[:2] / h

    def has_object(self, h: str) -> bool:
        return self.object_path(h).is_file()

    def _ingest(self, src: Path, cancel=None) -> tuple[str, int, bool]:
        """Kopiert src in den Objektspeicher und hasht dabei. -> (hash, größe, neu?)"""
        tmp = self.tmp / (uuid.uuid4().hex + ".part")
        h = hashlib.sha256()
        size = 0
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                while True:
                    if cancel and cancel():
                        raise Cancelled()
                    block = fi.read(CHUNK)
                    if not block:
                        break
                    h.update(block)
                    fo.write(block)
                    size += len(block)
                fo.flush()
                os.fsync(fo.fileno())
            digest = h.hexdigest()
            dest = self.object_path(digest)
            if dest.exists():
                tmp.unlink()
                return digest, size, False
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(tmp, dest)       # überschreibt nie
            except FileExistsError:
                tmp.unlink()
                return digest, size, False
            return digest, size, True
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    # -------------------------------------------------------------- Manifeste
    def _job_dir(self, job_id: str) -> Path:
        return self.snapshots / job_id

    def _write_manifest(self, job_id: str, manifest: dict) -> str:
        d = self._job_dir(job_id)
        d.mkdir(parents=True, exist_ok=True)
        base = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        sid, i = base, 1
        while (d / f"{sid}.json").exists():
            sid = f"{base}-{i}"
            i += 1
        manifest["id"] = sid
        tmp = self.tmp / (uuid.uuid4().hex + ".json")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        try:
            os.rename(tmp, d / f"{sid}.json")   # überschreibt nie
        except FileExistsError:
            tmp.unlink()
            return self._write_manifest(job_id, manifest)
        return sid

    def load_manifest(self, job_id: str, sid: str) -> dict:
        p = self._job_dir(job_id) / f"{sid}.json"
        mt = p.stat().st_mtime
        cached = self._cache.get(str(p))
        if cached and cached[0] == mt:
            return cached[1]
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("id", sid)
        self._cache[str(p)] = (mt, data)
        return data

    def snapshot_ids(self, job_id: str) -> list[str]:
        d = self._job_dir(job_id)
        if not d.is_dir():
            return []
        return sorted((p.stem for p in d.glob("*.json")), reverse=True)

    def list_snapshots(self, job_id: str) -> list[dict]:
        """Neueste zuerst."""
        out = []
        for sid in self.snapshot_ids(job_id):
            try:
                out.append(self.load_manifest(job_id, sid))
            except (OSError, ValueError):
                continue
        return out

    def latest_manifest(self, job_id: str) -> dict | None:
        for sid in self.snapshot_ids(job_id):
            try:
                return self.load_manifest(job_id, sid)
            except (OSError, ValueError):
                continue
        return None

    def job_ids(self) -> list[str]:
        if not self.snapshots.is_dir():
            return []
        return [p.name for p in self.snapshots.iterdir() if p.is_dir()]

    @staticmethod
    def diff_manifests(old: dict | None, new: dict) -> tuple[int, int, int]:
        """(hinzugefügt, geändert, entfernt) von old nach new."""
        of = old["files"] if old else {}
        nf = new["files"]
        added = sum(1 for k in nf if k not in of)
        removed = sum(1 for k in of if k not in nf)
        changed = sum(1 for k, v in nf.items() if k in of and of[k]["h"] != v["h"])
        return added, changed, removed

    # ---------------------------------------------------------------- Sichern
    def snapshot(self, job_id: str, job_name: str, source, label: str = "Manuell",
                 excludes=(), only_if_changed: bool = True, only_paths=None,
                 progress=None, cancel=None) -> SnapshotResult:
        source = Path(source)
        if not source.is_dir():
            raise BackupError(f"Quellordner nicht gefunden: {source}")
        self.ensure()
        root_res = self.root.resolve()
        if _same_path(root_res, source.resolve()):
            raise BackupError("Der Backup-Speicher darf nicht der Quellordner selbst sein.")

        prev = self.latest_manifest(job_id)
        prev_files = prev["files"] if prev else {}
        files: dict[str, dict] = {}
        dirs: list[str] = []
        errors: list[dict] = []
        new_bytes = 0
        total = 0
        only = set(only_paths) if only_paths is not None else None

        def onerror(e):
            errors.append({"pfad": str(getattr(e, "filename", "")), "fehler": str(e)})

        for cur, dnames, fnames in os.walk(source, onerror=onerror):
            cur_p = Path(cur)
            rel_dir = cur_p.relative_to(source)
            kept = []
            for dn in dnames:
                full = cur_p / dn
                rel = (rel_dir / dn).as_posix()
                if is_excluded(rel, dn, excludes):
                    continue
                try:
                    if full.is_symlink() or full.is_junction():
                        continue
                    if _same_path(full.resolve(), root_res):
                        continue
                except OSError:
                    continue
                kept.append(dn)
                if only is None:
                    dirs.append(rel)
            dnames[:] = kept
            for fn in fnames:
                rel = (rel_dir / fn).as_posix()
                if is_excluded(rel, fn, excludes):
                    continue
                if only is not None and rel not in only:
                    continue
                full = cur_p / fn
                try:
                    if full.is_symlink():
                        continue
                    st = full.stat()
                    p = prev_files.get(rel)
                    if (p and not p.get("alt") and p["s"] == st.st_size
                            and p["m"] == st.st_mtime_ns and self.has_object(p["h"])):
                        h, size = p["h"], st.st_size
                    else:
                        h, size, new = self._ingest(full, cancel)
                        if new:
                            new_bytes += size
                    files[rel] = {"h": h, "s": size, "m": st.st_mtime_ns}
                    total += size
                except Cancelled:
                    raise
                except OSError as e:
                    errors.append({"pfad": rel, "fehler": e.strerror or str(e)})
                    old = prev_files.get(rel)
                    if old and only is None:
                        # Letzten bekannten Stand übernehmen, aber kennzeichnen.
                        files[rel] = dict(old, alt=True)
                        total += old["s"]
                if progress:
                    progress(f"{len(files)} Dateien gesichert … {rel}")

        dirs.sort()
        changed = True
        if prev is not None and only is None:
            same_files = ({k: v["h"] for k, v in files.items()}
                          == {k: v["h"] for k, v in prev_files.items()})
            changed = not (same_files and dirs == prev.get("dirs", []))
        if only_if_changed and not changed:
            return SnapshotResult(None, False, len(files), total, 0, errors, prev)

        manifest = {
            "version": MANIFEST_VERSION,
            "job_id": job_id,
            "job_name": job_name,
            "source": str(source),
            "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "partial": only is not None,
            "files": files,
            "dirs": dirs,
            "errors": errors,
            "total_size": total,
        }
        sid = self._write_manifest(job_id, manifest)
        return SnapshotResult(sid, True, len(files), total, new_bytes, errors, manifest)

    def backup_job(self, job: Job, label="Manuell", only_if_changed=True,
                   progress=None, cancel=None) -> SnapshotResult:
        res = self.snapshot(job.id, job.name, job.source, label, job.excludes,
                            only_if_changed, progress=progress, cancel=cancel)
        job.last_run = _dt.datetime.now().isoformat(timespec="seconds")
        if res.snapshot_id and job.keep > 0:
            self.apply_retention(job)
        return res

    # --------------------------------------------------------- Wiederherstellen
    def plan_restore(self, manifest: dict, target, selection=None, excludes=(),
                     with_extras=True) -> list[PlanItem]:
        target = Path(target)
        plan: list[PlanItem] = []
        files = manifest["files"]
        for rel, e in sorted(files.items()):
            if not _in_selection(rel, selection):
                continue
            dest = target / rel
            if not os.path.lexists(dest):
                status = "neu"
            elif not dest.is_file():
                status = "konflikt"
            else:
                st = dest.stat()
                if st.st_size != e["s"]:
                    status = "geändert"
                elif st.st_mtime_ns == e["m"]:
                    status = "gleich"
                else:
                    try:
                        status = "gleich" if file_hash(dest) == e["h"] else "geändert"
                    except OSError:
                        status = "geändert"
            plan.append(PlanItem(rel, status, e["s"], e["m"]))
        if with_extras and target.is_dir():
            for cur, dnames, fnames in os.walk(target):
                cur_p = Path(cur)
                rel_dir = cur_p.relative_to(target)
                dnames[:] = [d for d in dnames
                             if not is_excluded((rel_dir / d).as_posix(), d, excludes)
                             and not (cur_p / d).is_symlink()]
                for fn in fnames:
                    rel = (rel_dir / fn).as_posix()
                    if rel in files or is_excluded(rel, fn, excludes):
                        continue
                    if ".dw-" in fn and fn.endswith(".tmp"):
                        continue
                    if selection and not _in_selection(rel, selection):
                        continue
                    try:
                        st = (cur_p / fn).stat()
                    except OSError:
                        continue
                    plan.append(PlanItem(rel, "nur_aktuell", st.st_size, st.st_mtime_ns))
        return plan

    def _restore_file(self, entry: dict, dest: Path):
        src = self.object_path(entry["h"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.dw-{uuid.uuid4().hex[:8]}.tmp")
        h = hashlib.sha256()
        try:
            with open(src, "rb") as fi, open(tmp, "wb") as fo:
                for block in iter(lambda: fi.read(CHUNK), b""):
                    h.update(block)
                    fo.write(block)
            if h.hexdigest() != entry["h"]:
                raise BackupError(f"Backup-Datei beschädigt: {src}")
            os.utime(tmp, ns=(entry["m"], entry["m"]))
            if dest.exists() and not os.access(dest, os.W_OK):
                os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def restore(self, job: Job, manifest: dict, target, selection=None,
                remove_extra: bool = False, progress=None) -> RestoreResult:
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        files = manifest["files"]
        plan = self.plan_restore(manifest, target, selection, job.excludes,
                                 with_extras=remove_extra)
        conflicts = [p.rel for p in plan if p.status == "konflikt"]
        if conflicts:
            raise BackupError("Am Ziel liegt ein Ordner, wo eine Datei hingehört: "
                              + ", ".join(conflicts[:5]))
        to_write = [p for p in plan if p.status in ("neu", "geändert")]
        extras = [p for p in plan if p.status == "nur_aktuell"] if remove_extra else []
        missing = [p.rel for p in to_write if not self.has_object(files[p.rel]["h"])]
        if missing:
            raise BackupError("Im Backup fehlen Inhalte für: " + ", ".join(missing[:5]))

        # 1) aktuellen Stand sichern, bevor irgendetwas ersetzt/entfernt wird
        at_risk = [p.rel for p in to_write if p.status == "geändert"] + [p.rel for p in extras]
        safety_id = safety_job = None
        if at_risk:
            if progress:
                progress("Sichere aktuellen Stand vor der Wiederherstellung …")
            if _same_path(target, job.source):
                safety_job = job.id
                res = self.snapshot(job.id, job.name, target, LABEL_SAFETY, job.excludes,
                                    only_if_changed=False)
            else:
                safety_job = SAFETY_JOB_ID
                res = self.snapshot(SAFETY_JOB_ID, "Sicherungen vor Wiederherstellung",
                                    target, f"{LABEL_SAFETY} ({job.name})", (),
                                    only_if_changed=False, only_paths=at_risk)
            saved = res.manifest["files"] if res.manifest else {}
            failed = [r for r in at_risk if r not in saved or saved[r].get("alt")]
            if failed:
                raise BackupError("Abgebrochen – der aktuelle Stand dieser Dateien konnte "
                                  "nicht gesichert werden (evtl. geöffnet/gesperrt):\n"
                                  + "\n".join(failed[:10]))
            safety_id = res.snapshot_id

        # 2) Ordner anlegen, Dateien schreiben
        for d in manifest.get("dirs", []):
            if _in_selection(d, selection):
                (target / d).mkdir(parents=True, exist_ok=True)
        written = []
        for i, p in enumerate(to_write, 1):
            if progress:
                progress(f"Stelle wieder her ({i}/{len(to_write)}): {p.rel}")
            self._restore_file(files[p.rel], target / p.rel)
            written.append(p.rel)

        # 3) überzählige Dateien nur in den Papierkorb
        trashed = []
        if extras:
            trash.move_to_trash([target / p.rel for p in extras])
            trashed = [p.rel for p in extras]
        return RestoreResult(safety_id, safety_job, written, trashed)

    def export_file(self, entry: dict, name: str) -> Path:
        """Legt eine Version in einen Temp-Ordner (zum Ansehen)."""
        d = Path(tempfile.mkdtemp(prefix="safekeep-"))
        dest = d / Path(name).name
        self._restore_file(entry, dest)
        return dest

    # --------------------------------------------------------------- Aufräumen
    def delete_snapshots(self, job_id: str, sids) -> int:
        paths = [self._job_dir(job_id) / f"{s}.json" for s in sids]
        paths = [p for p in paths if p.exists()]
        if paths:
            trash.move_to_trash(paths)
        for p in paths:
            self._cache.pop(str(p), None)
        return len(paths)

    def apply_retention(self, job: Job) -> int:
        if job.keep <= 0:
            return 0
        normal = [m for m in self.list_snapshots(job.id)
                  if not str(m.get("label", "")).startswith(LABEL_SAFETY)]
        old = [m["id"] for m in normal[job.keep:]]
        if not old:
            return 0
        n = self.delete_snapshots(job.id, old)
        self.collect_garbage()
        return n

    def referenced_hashes(self) -> set[str]:
        refs = set()
        for jid in self.job_ids():
            for m in self.list_snapshots(jid):
                refs.update(e["h"] for e in m["files"].values())
        return refs

    def collect_garbage(self) -> tuple[int, int]:
        """Nicht mehr benutzte Objekte in den Papierkorb. -> (anzahl, bytes)"""
        refs = self.referenced_hashes()
        orphans = []
        size = 0
        if self.objects.is_dir():
            for sub in self.objects.iterdir():
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    if f.name not in refs:
                        orphans.append(f)
                        size += f.stat().st_size
        if self.tmp.is_dir():
            orphans.extend(self.tmp.iterdir())
        if orphans:
            trash.move_to_trash(orphans)
        return len(orphans), size

    def storage_size(self) -> int:
        total = 0
        for cur, _d, fnames in os.walk(self.objects):
            for fn in fnames:
                try:
                    total += os.path.getsize(os.path.join(cur, fn))
                except OSError:
                    pass
        return total


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return f"{n:.1f} TB"
