"""Testet jeden Befehl aus toolhub.json an Testordnern (Papierkorb simuliert)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from safekeep import trash  # noqa: E402
from tests.test_backup import FakeTrash, write  # noqa: E402
from tests.test_music import make_wav  # noqa: E402

MANIFEST = json.loads((ROOT / "toolhub.json").read_text(encoding="utf-8"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sk-cli-"))
        self.home = self.tmp / "cfg"
        self.home.mkdir()
        self.src = self.tmp / "Spielstand"
        write(self.src / "save1.sav", "Level 1")
        write(self.src / "profil" / "opt.ini", "a=1")
        self.music = self.tmp / "Musik"
        make_wav(self.music / "Band - Lied.wav", 440, 1)
        make_wav(self.music / "Band - Lied (1).wav", 440, 1, rate=22050)
        (self.home / "config.json").write_text(json.dumps({
            "backup_root": str(self.tmp / "Backups"), "jobs": [],
            "music_folders": [str(self.music)]}), encoding="utf-8")
        self.fake = FakeTrash(self.tmp / "Papierkorb")
        self.patches = [mock.patch.dict(os.environ, {"SAFEKEEP_HOME": str(self.home)}),
                        mock.patch.object(trash, "move_to_trash", self.fake)]
        for p in self.patches:
            p.start()
        import safekeep.config as c
        self.cfgpatch = mock.patch.object(c.Config.__init__, "__defaults__", (self.home / "config.json",))
        self.cfgpatch.start()

    def tearDown(self):
        self.cfgpatch.stop()
        for p in reversed(self.patches):
            p.stop()

    def run_cli(self, *args, expect=0):
        import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as e:
                rc = e.code
        data = json.loads(out.getvalue())          # muss gültiges JSON sein
        self.assertEqual(rc, expect, (args, out.getvalue(), err.getvalue()))
        if expect:
            self.assertFalse(data["ok"])
            self.assertTrue(err.getvalue().strip())
        else:
            self.assertTrue(data["ok"])
        return data

    def cmd(self, name):
        return next(c for c in MANIFEST["commands"] if c["name"] == name)

    def run_tool(self, cmd_name, expect=0, **kw):
        """Baut die Kommandozeile so, wie ToolHub es aus toolhub.json tut."""
        c = self.cmd(cmd_name)
        argv = list(c["run"][2:])
        for a in c["args"]:
            if a["name"] in kw:
                v = kw[a["name"]]
                if a["type"] == "boolean":
                    if v:
                        argv.append(a["flag"])
                else:
                    argv += [a["flag"], str(v)]
        missing = [a["name"] for a in c["args"] if a["required"] and a["name"] not in kw]
        self.assertFalse(missing, f"Pflichtargumente fehlen: {missing}")
        return self.run_cli(*argv, expect=expect)

    def test_manifest_format(self):
        self.assertEqual(MANIFEST["name"], "SafeKeep")
        for c in MANIFEST["commands"]:
            self.assertEqual(c["run"][:2], ["python", "cli.py"])
            self.assertIsInstance(c["readonly"], bool)
            for a in c["args"]:
                self.assertEqual(set(a), {"name", "flag", "type", "required", "description"})
            if not c["readonly"] and c["name"] != "backup":
                self.assertIn("--dry-run", [a["flag"] for a in c["args"]], c["name"])

    def test_all_commands(self):
        covered = set()

        def tool(cmd_name, **kw):
            covered.add(cmd_name)
            return self.run_tool(cmd_name, **kw)

        self.assertEqual(tool("status")["jobs"], 0)
        prev = tool("add-job", folder=str(self.src), name="Spiel", dry_run=True)
        self.assertTrue(prev["dry_run"])
        self.assertEqual(self.run_tool("jobs")["jobs"], [])
        job = tool("add-job", folder=str(self.src), name="Spiel", interval=10, exclude="*.log")["added"]
        self.run_tool("add-job", folder=str(self.src), expect=1)        # doppelt

        jobs = tool("jobs")["jobs"]
        self.assertEqual(jobs[0]["name"], "Spiel")

        b1 = tool("backup", job="Spiel")["results"][0]
        self.assertTrue(b1["created_version"])
        self.assertIsNone(self.run_tool("backup", all=True)["results"][0]["created_version"])
        write(self.src / "save1.sav", "Level 2 kaputt")
        b2 = self.run_tool("backup", job=job["id"], force=True)["results"][0]
        self.assertTrue(b2["created_version"])

        vers = tool("versions", job="Spiel", limit=5)
        self.assertEqual(vers["total"], 2)
        self.assertEqual(vers["versions"][0]["changes_vs_previous"]["changed"], 1)

        files = tool("files", job="Spiel", version=b1["created_version"], pattern="*.sav")
        self.assertEqual([f["path"] for f in files["files"]], ["save1.sav"])

        # Wiederherstellen: erst Vorschau, nichts verändert
        dry = tool("restore", job="Spiel", version=b1["created_version"], dry_run=True)
        self.assertEqual(dry["summary"]["overwrite"], 1)
        self.assertTrue(dry["safety_backup_before"])
        self.assertEqual((self.src / "save1.sav").read_text(encoding="utf-8"), "Level 2 kaputt")
        res = self.run_tool("restore", job="Spiel", version=b1["created_version"])
        self.assertEqual(res["written"], 1)
        self.assertTrue(res["safety_version"])
        self.assertEqual((self.src / "save1.sav").read_text(encoding="utf-8"), "Level 1")

        # in anderen Ordner, nur eine Datei
        other = self.tmp / "Ziel"
        r2 = self.run_tool("restore", job="Spiel", version=b1["created_version"],
                           target=str(other), files="profil/opt.ini")
        self.assertEqual(r2["summary"]["create"], 1)
        self.assertTrue((other / "profil" / "opt.ini").exists())
        self.assertFalse((other / "save1.sav").exists())

        # Version löschen
        d = tool("delete-version", job="Spiel", version=b2["created_version"], dry_run=True)
        self.assertTrue(d["dry_run"])
        self.assertEqual(self.run_tool("versions", job="Spiel")["total"], 3)
        self.run_tool("delete-version", job="Spiel", version=b2["created_version"])
        self.assertEqual(self.run_tool("versions", job="Spiel")["total"], 2)
        self.assertTrue(any(m.endswith(".json") for m in self.fake.moved))

        # Musik
        md = tool("music-dupes", folder=str(self.music), tolerance=2)
        self.assertEqual(md["groups"], 1)
        self.assertEqual(Path(md["results"][0]["keep"]["path"]).name, "Band - Lied.wav")
        mc = tool("music-clean", folder=str(self.music), dry_run=True)
        self.assertEqual(len(mc["would_move_to_recycle_bin"]["files"]), 1)
        self.assertTrue((self.music / "Band - Lied (1).wav").exists())
        self.run_tool("music-clean", identical_only=False)
        self.assertFalse((self.music / "Band - Lied (1).wav").exists())
        self.assertTrue((self.music / "Band - Lied.wav").exists())

        self.assertEqual(covered, {c["name"] for c in MANIFEST["commands"]})

    def test_errors_are_json(self):
        self.run_cli("versions", "--job", "gibtsnicht", expect=1)
        self.run_cli("restore", "--job", "x", "--dry-run", expect=1)
        self.run_cli("versions", expect=2)               # Pflichtargument fehlt
        self.run_cli("backup", expect=2)

    def test_subprocess_run_line(self):
        c = self.cmd("status")
        env = dict(os.environ, SAFEKEEP_HOME=str(self.home))
        p = subprocess.run([sys.executable] + c["run"][1:], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["backup_root"], str(self.tmp / "Backups"))


if __name__ == "__main__":
    unittest.main()
