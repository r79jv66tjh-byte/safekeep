"""Start:  python -m safekeep            -> Oberfläche
        python -m safekeep --sichern  -> alle aktiven Sicherungen ohne Oberfläche
                                           (z. B. für die Windows-Aufgabenplanung)
"""
import sys


def cli_backup() -> int:
    from .backup import BackupStore, human_size
    from .config import Config

    cfg = Config()
    store = BackupStore(cfg["backup_root"])
    rc = 0
    for job in cfg.jobs:
        if not job.enabled:
            continue
        try:
            r = store.backup_job(job, "Automatisch", only_if_changed=True)
        except Exception as e:      # noqa: BLE001
            print(f"{job.name}: FEHLER – {e}")
            rc = 1
            continue
        if r.snapshot_id:
            print(f"{job.name}: gesichert ({r.file_count} Dateien, {human_size(r.new_bytes)} neu, "
                  f"{len(r.errors)} Fehler)")
        else:
            print(f"{job.name}: keine Änderungen")
    cfg.save()
    return rc


def main():
    if "--sichern" in sys.argv[1:]:
        sys.exit(cli_backup())
    from .gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()
