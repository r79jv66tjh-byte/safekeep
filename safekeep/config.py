"""Einstellungen in %APPDATA%\\SafeKeep\\config.json"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .backup import Job

CONFIG_DIR = Path(os.environ.get("SAFEKEEP_HOME")
                  or Path(os.environ.get("APPDATA", Path.home())) / "SafeKeep")
CONFIG_FILE = CONFIG_DIR / "config.json"


def default_config() -> dict:
    return {
        "backup_root": str(Path.home() / "SafeKeep-Backups"),
        "auto": True,
        "jobs": [],
        "music_folders": [],
        "music_tolerance": 3,
        "music_same_song": True,
    }


class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = Path(path)
        self.data = default_config()
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.jobs = [Job.from_dict(d) for d in self.data.get("jobs", [])]

    def save(self):
        self.data["jobs"] = [j.to_dict() for j in self.jobs]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        self.data[k] = v
