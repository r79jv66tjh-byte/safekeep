import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from safekeep import backup, trash
from safekeep.backup import BackupError, BackupStore, Job


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    # mtime sicher verändern (Dateisystem-Auflösung)
    t = time.time() + hash(text) % 1000
    os.utime(p, (t, t))


class FakeTrash:
    """Simuliert den Papierkorb: verschiebt in einen Ordner statt zu löschen."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.moved = []

    def __call__(self, paths, chunk=200):
        self.folder.mkdir(exist_ok=True)
        for p in paths:
            p = Path(p)
            dest = self.folder / f"{len(self.moved)}_{p.name}"
            os.rename(p, dest)
            self.moved.append(str(p))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dw-test-"))
        self.src = self.tmp / "Spielstand"
        self.store = BackupStore(self.tmp / "Backups")
        self.job = Job(name="Spiel", source=str(self.src), excludes=["*.log"])
        write(self.src / "save1.sav", "Level 1")
        write(self.src / "profil" / "settings.ini", "volume=5")
        write(self.src / "debug.log", "egal")
        self.fake = FakeTrash(self.tmp / "Papierkorb")
        self.patch = mock.patch.object(trash, "move_to_trash", self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_snapshot_dedup_and_unchanged(self):
        r1 = self.store.backup_job(self.job)
        self.assertTrue(r1.snapshot_id)
        self.assertEqual(set(r1.manifest["files"]), {"save1.sav", "profil/settings.ini"})
        r2 = self.store.backup_job(self.job, only_if_changed=True)
        self.assertIsNone(r2.snapshot_id, "ohne Änderung keine neue Version")
        write(self.src / "save1.sav", "Level 2")
        r3 = self.store.backup_job(self.job)
        self.assertTrue(r3.snapshot_id)
        self.assertEqual(len(self.store.list_snapshots(self.job.id)), 2)
        # unveränderte Datei wird nicht doppelt gespeichert
        objs = list((self.store.objects).rglob("*"))
        self.assertEqual(len([o for o in objs if o.is_file()]), 3)

    def test_restore_original_creates_safety_version(self):
        r1 = self.store.backup_job(self.job)
        write(self.src / "save1.sav", "Level 9 – kaputt")
        write(self.src / "neu.sav", "neu nach Backup")
        (self.src / "profil" / "settings.ini").unlink()
        m = self.store.load_manifest(self.job.id, r1.snapshot_id)

        plan = {p.rel: p.status for p in self.store.plan_restore(m, self.src, excludes=self.job.excludes)}
        self.assertEqual(plan["save1.sav"], "geändert")
        self.assertEqual(plan["profil/settings.ini"], "neu")
        self.assertEqual(plan["neu.sav"], "nur_aktuell")
        self.assertNotIn("debug.log", plan)

        res = self.store.restore(self.job, m, self.src, remove_extra=True)
        self.assertEqual((self.src / "save1.sav").read_text(encoding="utf-8"), "Level 1")
        self.assertEqual((self.src / "profil" / "settings.ini").read_text(encoding="utf-8"), "volume=5")
        self.assertFalse((self.src / "neu.sav").exists())
        self.assertIn(str(self.src / "neu.sav"), self.fake.moved)
        self.assertTrue((self.src / "debug.log").exists(), "ausgeschlossene Dateien bleiben")
        # vorheriger Stand ist als eigene Version erhalten
        self.assertTrue(res.safety_snapshot)
        safety = self.store.load_manifest(self.job.id, res.safety_snapshot)
        self.assertEqual(safety["label"], backup.LABEL_SAFETY)
        p = self.store.export_file(safety["files"]["save1.sav"], "save1.sav")
        self.assertEqual(p.read_text(encoding="utf-8"), "Level 9 – kaputt")
        self.assertIn("neu.sav", safety["files"])

    def test_restore_selection_only(self):
        r1 = self.store.backup_job(self.job)
        write(self.src / "save1.sav", "B")
        write(self.src / "profil" / "settings.ini", "B")
        m = self.store.load_manifest(self.job.id, r1.snapshot_id)
        self.store.restore(self.job, m, self.src, selection=["profil/settings.ini"])
        self.assertEqual((self.src / "save1.sav").read_text(encoding="utf-8"), "B")
        self.assertEqual((self.src / "profil" / "settings.ini").read_text(encoding="utf-8"), "volume=5")

    def test_restore_to_other_folder_backs_up_overwritten(self):
        r1 = self.store.backup_job(self.job)
        other = self.tmp / "Anderswo"
        write(other / "save1.sav", "fremd")
        m = self.store.load_manifest(self.job.id, r1.snapshot_id)
        res = self.store.restore(self.job, m, other)
        self.assertEqual((other / "save1.sav").read_text(encoding="utf-8"), "Level 1")
        self.assertEqual(res.safety_job, backup.SAFETY_JOB_ID)
        s = self.store.load_manifest(backup.SAFETY_JOB_ID, res.safety_snapshot)
        self.assertEqual(list(s["files"]), ["save1.sav"])

    def test_restore_aborts_if_current_state_unreadable(self):
        r1 = self.store.backup_job(self.job)
        write(self.src / "save1.sav", "gesperrt")
        m = self.store.load_manifest(self.job.id, r1.snapshot_id)
        real_ingest = self.store._ingest

        def failing(src, cancel=None):
            if Path(src).name == "save1.sav":
                raise PermissionError(13, "gesperrt")
            return real_ingest(src, cancel)

        with mock.patch.object(self.store, "_ingest", failing):
            with self.assertRaises(BackupError):
                self.store.restore(self.job, m, self.src)
        self.assertEqual((self.src / "save1.sav").read_text(encoding="utf-8"), "gesperrt",
                         "nichts überschrieben, wenn die Sicherung vorher scheitert")

    def test_manifests_and_objects_never_overwritten(self):
        self.store.backup_job(self.job)
        obj = next(p for p in self.store.objects.rglob("*") if p.is_file())
        mtime = obj.stat().st_mtime_ns
        write(self.src / "save1.sav", "Level 1")       # gleicher Inhalt, neue mtime
        self.store.backup_job(self.job, only_if_changed=False)
        self.assertEqual(obj.stat().st_mtime_ns, mtime)
        self.assertEqual(len(self.store.snapshot_ids(self.job.id)), 2)

    def test_retention_moves_old_versions_to_trash(self):
        self.job.keep = 2
        for i in range(4):
            write(self.src / "save1.sav", f"Stand {i}")
            self.store.backup_job(self.job)
        self.assertEqual(len(self.store.snapshot_ids(self.job.id)), 2)
        self.assertTrue(any(m.endswith(".json") for m in self.fake.moved))
        # die Inhalte der weggeräumten Versionen sind ebenfalls im Papierkorb
        refs = self.store.referenced_hashes()
        for o in self.store.objects.rglob("*"):
            if o.is_file():
                self.assertIn(o.name, refs)

    def test_backup_root_inside_source_is_skipped(self):
        store = BackupStore(self.src / "_backups")
        r = store.backup_job(self.job)
        self.assertFalse(any(k.startswith("_backups") for k in r.manifest["files"]))


class RealTrashTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "nur Windows")
    def test_real_recycle_bin(self):
        d = Path(tempfile.mkdtemp(prefix="dw-trash-"))
        f = d / "safekeep-papierkorb-test.txt"
        f.write_text("Test von SafeKeep – darf gelöscht werden", encoding="utf-8")
        trash.move_to_trash([f])
        self.assertFalse(f.exists())


if __name__ == "__main__":
    unittest.main()
