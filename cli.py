"""SafeKeep-Kommandozeile für Claude/ToolHub.

Jeder Befehl gibt genau ein JSON-Objekt auf stdout aus.
Bei Fehlern: Meldung auf stderr, {"ok": false, "error": ...} auf stdout, Exit-Code 1
(2 bei falschen Argumenten).

Befehle, die Dateien verändern (restore, delete-version, music-clean, add-job),
unterstützen --dry-run: dann wird nur die Vorschau ausgegeben.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from safekeep import music, trash  # noqa: E402
from safekeep.backup import (SAFETY_JOB_ID, BackupError, BackupStore, Job,  # noqa: E402
                             human_size)
from safekeep.config import Config  # noqa: E402

MAX_ITEMS = 500


class CliError(Exception):
    pass


# ---------------------------------------------------------------- Helfer
def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


def job_info(store: BackupStore, j: Job) -> dict:
    ids = store.snapshot_ids(j.id)
    return {"id": j.id, "name": j.name, "folder": j.source, "interval_min": j.interval_min,
            "keep": j.keep, "excludes": j.excludes, "enabled": j.enabled,
            "last_check": j.last_run, "versions": len(ids),
            "latest_version": ids[0] if ids else None,
            "folder_exists": os.path.isdir(j.source)}


def find_job(cfg: Config, key: str | None) -> Job:
    if not key:
        raise CliError("--job fehlt (ID oder Name, siehe Befehl 'jobs')")
    if key == SAFETY_JOB_ID:
        return Job(name="Sicherungen vor Wiederherstellung", source="", id=SAFETY_JOB_ID)
    for j in cfg.jobs:
        if j.id == key:
            return j
    matches = [j for j in cfg.jobs if j.name.lower() == key.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CliError(f"Name '{key}' ist mehrdeutig – bitte die ID angeben")
    raise CliError(f"Keine Sicherung '{key}' gefunden")


def find_version(store: BackupStore, job: Job, key: str | None) -> dict:
    ids = store.snapshot_ids(job.id)
    if not ids:
        raise CliError(f"'{job.name}' hat noch keine Versionen")
    if not key or key == "latest":
        return store.load_manifest(job.id, ids[0])
    if key in ids:
        return store.load_manifest(job.id, key)
    pref = [i for i in ids if i.startswith(key)]
    if len(pref) == 1:
        return store.load_manifest(job.id, pref[0])
    if len(pref) > 1:
        raise CliError(f"Version '{key}' ist mehrdeutig ({len(pref)} Treffer)")
    raise CliError(f"Version '{key}' nicht gefunden (siehe Befehl 'versions')")


def version_info(m: dict, older: dict | None = None) -> dict:
    d = {"id": m["id"], "created": m.get("created"), "label": m.get("label"),
         "files": len(m["files"]), "total_size": m.get("total_size", 0),
         "total_size_text": human_size(m.get("total_size", 0)),
         "partial": bool(m.get("partial")), "errors": len(m.get("errors") or [])}
    if older is not None or not m.get("partial"):
        a, c, r = BackupStore.diff_manifests(older, m)
        d["changes_vs_previous"] = {"added": a, "changed": c, "removed": r}
    return d


# --------------------------------------------------------------- Befehle
def cmd_status(cfg, store, a):
    return {"backup_root": str(store.root), "backup_root_exists": store.root.is_dir(),
            "storage_bytes": store.storage_size() if store.objects.is_dir() else 0,
            "jobs": len(cfg.jobs), "auto_backup": cfg["auto"],
            "config_file": str(cfg.path), "music_tags_available": music.HAVE_MUTAGEN}


def cmd_jobs(cfg, store, a):
    jobs = [job_info(store, j) for j in cfg.jobs]
    if store.snapshot_ids(SAFETY_JOB_ID):
        jobs.append({"id": SAFETY_JOB_ID, "name": "Sicherungen vor Wiederherstellung (in andere Ordner)",
                     "versions": len(store.snapshot_ids(SAFETY_JOB_ID))})
    return {"jobs": jobs}


def cmd_versions(cfg, store, a):
    job = find_job(cfg, a.job)
    ms = store.list_snapshots(job.id)
    limit = a.limit or len(ms)
    out = []
    for i, m in enumerate(ms[:limit]):
        older = ms[i + 1] if i + 1 < len(ms) else None
        out.append(version_info(m, older))
    return {"job": job.id, "name": job.name, "total": len(ms), "versions": out}


def cmd_files(cfg, store, a):
    job = find_job(cfg, a.job)
    m = find_version(store, job, a.version)
    import fnmatch
    pat = (a.pattern or "*").lower()
    files = [{"path": rel, "size": e["s"], "stale": bool(e.get("alt"))}
             for rel, e in sorted(m["files"].items())
             if fnmatch.fnmatch(rel.lower(), pat) or fnmatch.fnmatch(os.path.basename(rel).lower(), pat)]
    return {"job": job.id, "version": m["id"], "source": m.get("source"), "count": len(files),
            "truncated": len(files) > MAX_ITEMS, "files": files[:MAX_ITEMS],
            "errors": m.get("errors") or []}


def cmd_backup(cfg, store, a):
    if a.all:
        jobs = [j for j in cfg.jobs if j.enabled]
    else:
        jobs = [find_job(cfg, a.job)]
        if jobs[0].id == SAFETY_JOB_ID:
            raise CliError("Diese Sammlung kann nicht direkt gesichert werden")
    results = []
    for j in jobs:
        try:
            r = store.backup_job(j, "Manuell", only_if_changed=not a.force)
            results.append({"job": j.id, "name": j.name, "created_version": r.snapshot_id,
                            "changed": r.changed, "files": r.file_count,
                            "new_bytes": r.new_bytes, "unreadable": r.errors})
        except BackupError as e:
            results.append({"job": j.id, "name": j.name, "error": str(e)})
    cfg.save()
    failed = [r for r in results if "error" in r]
    if failed and len(failed) == len(results):
        raise CliError("; ".join(f"{r['name']}: {r['error']}" for r in failed))
    return {"results": results}


def cmd_restore(cfg, store, a):
    job = find_job(cfg, a.job)
    m = find_version(store, job, a.version)
    if job.id == SAFETY_JOB_ID:
        job = Job(name="Sicherung vor Wiederherstellung", source=m.get("source", ""), id=SAFETY_JOB_ID)
    target = a.target or job.source
    if not target:
        raise CliError("--target fehlt")
    selection = split_list(a.files) or None
    plan = store.plan_restore(m, target, selection, job.excludes, with_extras=a.remove_extra)
    counts = {}
    for p in plan:
        counts[p.status] = counts.get(p.status, 0) + 1
    actions = {"neu": "create", "geändert": "overwrite", "gleich": "unchanged",
               "nur_aktuell": "move_to_recycle_bin" if a.remove_extra else "keep",
               "konflikt": "conflict"}
    items = [{"path": p.rel, "action": actions[p.status], "size": p.size}
             for p in plan if p.status != "gleich"]
    summary = {"create": counts.get("neu", 0), "overwrite": counts.get("geändert", 0),
               "unchanged": counts.get("gleich", 0),
               "to_recycle_bin": counts.get("nur_aktuell", 0) if a.remove_extra else 0,
               "conflicts": counts.get("konflikt", 0)}
    base = {"job": job.id, "version": m["id"], "version_created": m.get("created"),
            "target": str(target), "summary": summary,
            "safety_backup_before": bool(summary["overwrite"] or summary["to_recycle_bin"]),
            "truncated": len(items) > MAX_ITEMS, "items": items[:MAX_ITEMS]}
    if a.dry_run:
        return {"dry_run": True, **base}
    res = store.restore(job, m, target, selection, a.remove_extra)
    return {"dry_run": False, **base, "written": len(res.written), "moved_to_recycle_bin": len(res.trashed),
            "safety_version": res.safety_snapshot, "safety_job": res.safety_job}


def cmd_delete_version(cfg, store, a):
    job = find_job(cfg, a.job)
    m = find_version(store, job, a.version)
    if a.version in (None, "", "latest") and not a.dry_run:
        raise CliError("Zum Löschen bitte die Versions-ID ausdrücklich angeben")
    info = version_info(m)
    if a.dry_run:
        return {"dry_run": True, "job": job.id, "would_move_to_recycle_bin": info}
    store.delete_snapshots(job.id, [m["id"]])
    cnt, size = store.collect_garbage()
    return {"dry_run": False, "job": job.id, "moved_to_recycle_bin": info,
            "unused_contents_moved": cnt, "unused_bytes": size}


def cmd_add_job(cfg, store, a):
    folder = os.path.abspath(a.folder)
    if not os.path.isdir(folder):
        raise CliError(f"Ordner nicht gefunden: {folder}")
    for j in cfg.jobs:
        if os.path.normcase(os.path.abspath(j.source)) == os.path.normcase(folder):
            raise CliError(f"Ordner wird schon gesichert als '{j.name}' ({j.id})")
    job = Job(name=a.name or os.path.basename(folder), source=folder,
              interval_min=max(0, a.interval or 0), keep=max(0, a.keep or 0),
              excludes=split_list(a.exclude))
    if a.dry_run:
        return {"dry_run": True, "would_add": job.to_dict()}
    cfg.jobs.append(job)
    cfg.save()
    return {"dry_run": False, "added": job.to_dict()}


def _dupes(cfg, a):
    folders = split_list(a.folder) or cfg["music_folders"]
    if not folders:
        raise CliError("--folder fehlt (ein oder mehrere Musik-Ordner, kommagetrennt)")
    for f in folders:
        if not os.path.isdir(f):
            raise CliError(f"Ordner nicht gefunden: {f}")
    tol = a.tolerance if a.tolerance is not None else cfg["music_tolerance"]
    return folders, music.find_duplicates(folders, not a.identical_only, tol)


def _track(t: music.Track) -> dict:
    return {"path": t.path, "artist": t.artist, "title": t.title,
            "duration": round(t.duration, 1) if t.duration else None,
            "quality": t.quality_text(), "lossless": t.lossless, "bitrate_kbps": t.bitrate,
            "size": t.size}


def cmd_music_dupes(cfg, store, a):
    folders, groups = _dupes(cfg, a)
    out = [{"label": g.label, "reason": g.reason, "keep": _track(g.best),
            "duplicates": [_track(t) for t in g.tracks if t is not g.best]} for g in groups]
    reclaim = sum(t.size for g in groups for t in g.tracks if t is not g.best)
    return {"folders": folders, "groups": len(out), "duplicate_files": sum(len(g["duplicates"]) for g in out),
            "reclaimable_bytes": reclaim, "reclaimable_text": human_size(reclaim),
            "truncated": len(out) > MAX_ITEMS, "results": out[:MAX_ITEMS]}


def cmd_music_clean(cfg, store, a):
    folders, groups = _dupes(cfg, a)
    victims = [t for g in groups for t in g.tracks if t is not g.best]
    keeps = [g.best.path for g in groups]
    data = {"folders": folders, "groups": len(groups), "keep": keeps,
            "files": [t.path for t in victims], "bytes": sum(t.size for t in victims)}
    if a.dry_run:
        return {"dry_run": True, "would_move_to_recycle_bin": data}
    trash.move_to_trash([t.path for t in victims])
    return {"dry_run": False, "moved_to_recycle_bin": data}


# ---------------------------------------------------------------- Parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="SafeKeep – JSON-Schnittstelle")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="(immer JSON; nur für Kompatibilität)")
        sp.set_defaults(fn=fn)
        return sp

    add("status", cmd_status, "Speicherort, Größe, Anzahl Sicherungen")
    add("jobs", cmd_jobs, "gesicherte Ordner auflisten")
    sp = add("versions", cmd_versions, "Versionen einer Sicherung")
    sp.add_argument("--job", required=True)
    sp.add_argument("--limit", type=int)
    sp = add("files", cmd_files, "Dateien einer Version")
    sp.add_argument("--job", required=True)
    sp.add_argument("--version")
    sp.add_argument("--pattern")
    sp = add("backup", cmd_backup, "jetzt sichern")
    sp.add_argument("--job")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp = add("restore", cmd_restore, "Version wiederherstellen")
    sp.add_argument("--job", required=True)
    sp.add_argument("--version")
    sp.add_argument("--target")
    sp.add_argument("--files")
    sp.add_argument("--remove-extra", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("delete-version", cmd_delete_version, "Version in den Papierkorb")
    sp.add_argument("--job", required=True)
    sp.add_argument("--version")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("add-job", cmd_add_job, "Ordner zur Sicherung hinzufügen")
    sp.add_argument("--folder", required=True)
    sp.add_argument("--name")
    sp.add_argument("--interval", type=int)
    sp.add_argument("--keep", type=int)
    sp.add_argument("--exclude")
    sp.add_argument("--dry-run", action="store_true")
    for name, fn, h in (("music-dupes", cmd_music_dupes, "Musik-Duplikate finden"),
                        ("music-clean", cmd_music_clean, "Musik-Duplikate in den Papierkorb")):
        sp = add(name, fn, h)
        sp.add_argument("--folder")
        sp.add_argument("--identical-only", action="store_true")
        sp.add_argument("--tolerance", type=float)
        if name == "music-clean":
            sp.add_argument("--dry-run", action="store_true")
    return p


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True, indent=2) + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    parser = build_parser()

    def _error(message):
        sys.stderr.write(f"Fehler: {message}\n")
        emit({"ok": False, "error": message})
        sys.exit(2)

    parser.error = _error
    for sp in parser._subparsers._group_actions[0].choices.values():
        sp.error = _error
    a = parser.parse_args(argv)
    if a.cmd == "backup" and not a.all and not a.job:
        _error("--job oder --all angeben")
    try:
        cfg = Config()
        store = BackupStore(cfg["backup_root"])
        result = a.fn(cfg, store, a)
    except (CliError, BackupError, trash.TrashError, OSError) as e:
        sys.stderr.write(f"Fehler: {e}\n")
        emit({"ok": False, "error": str(e)})
        return 1
    emit({"ok": True, "command": a.cmd, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
