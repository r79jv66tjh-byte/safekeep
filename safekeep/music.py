"""Duplikat-Finder für Musik.

Zwei Arten von Duplikaten:
* identische Dateien  – gleicher Inhalt (SHA-256), egal wie sie heißen
* gleicher Song       – gleicher Interpret + Titel (normalisiert, aus Tags oder
                         Dateiname) und ähnliche Länge, aber andere Datei/Qualität

Pro Gruppe wird die beste Version vorgeschlagen (verlustfrei > hohe Bitrate …).
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    import mutagen
    HAVE_MUTAGEN = True
except ImportError:          # funktioniert auch ohne, dann nur Dateinamen
    mutagen = None
    HAVE_MUTAGEN = False

AUDIO_EXT = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus",
             ".wma", ".aif", ".aiff", ".ape", ".wv", ".mpc", ".alac"}
LOSSLESS_EXT = {".flac", ".wav", ".aif", ".aiff", ".ape", ".wv", ".alac"}
# Vorliebe bei sonst gleicher Qualität (höher = besser)
FORMAT_PREF = {".flac": 5, ".alac": 4, ".wv": 4, ".ape": 3, ".aiff": 2, ".aif": 2,
               ".wav": 1, ".m4a": 3, ".opus": 3, ".ogg": 2, ".mp3": 2, ".aac": 2}

TAG_KEYS = {
    "title": ["TIT2", "title", "TITLE", "\xa9nam", "Title"],
    "artist": ["TPE1", "artist", "ARTIST", "\xa9ART", "Author", "TPE2", "albumartist", "aART"],
}

NOISE = re.compile(
    r"official|offiziell|video|audio|lyrics?|visuali[sz]er|\bhq\b|\bhd\b|\b4k\b|"
    r"remaster|explicit|\bclean\b|\bmp3\b|kbps|full song|bonus track")
FEAT = re.compile(r"\b(feat|ft|featuring)\b")


@dataclass
class Track:
    path: str
    size: int
    ext: str
    title: str = ""
    artist: str = ""
    duration: float | None = None
    bitrate: int = 0            # kbit/s
    sample_rate: int = 0
    bits: int = 0
    channels: int = 0
    lossless: bool = False
    codec: str = ""
    from_tags: bool = False
    hash: str | None = None
    key: tuple = field(default=())

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def effective_kbps(self) -> int:
        if self.lossless:
            return int((self.sample_rate or 44100) * (self.bits or 16) * (self.channels or 2) / 1000)
        return self.bitrate

    def score(self) -> tuple:
        return (1 if self.lossless else 0,
                self.effective_kbps(),
                self.sample_rate,
                self.bits,
                1 if self.from_tags else 0,
                FORMAT_PREF.get(self.ext, 0),
                -len(self.path))

    def quality_text(self) -> str:
        fmt = (self.codec or self.ext.lstrip(".")).upper()
        if self.lossless:
            parts = [fmt, "verlustfrei"]
            if self.sample_rate:
                parts.append(f"{self.sample_rate / 1000:g} kHz".replace(".", ",")
                             + (f"/{self.bits} bit" if self.bits else ""))
            return " · ".join(parts)
        return f"{fmt} · {self.bitrate} kbit/s" if self.bitrate else fmt


@dataclass
class DupGroup:
    tracks: list
    reason: str
    best: Track

    @property
    def label(self) -> str:
        b = self.best
        if b.artist and b.title:
            return f"{b.artist} – {b.title}"
        return b.title or b.name


# ------------------------------------------------------------------ Hilfen
def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")

    def bracket(m):
        inner = m.group(1)
        if NOISE.search(inner) or FEAT.search(inner):
            return " "
        return " " + inner + " "

    s = re.sub(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]", bracket, s)
    s = re.sub(r"\s[-–]\s[^-–]*remaster[^-–]*$", "", s)   # "Song - 2011 Remaster"
    s = FEAT.split(s)[0]
    s = re.sub(r"[\W_]+", " ", s)
    s = " ".join(s.split())
    if s.startswith("the "):
        s = s[4:]
    return s


def parse_filename(stem: str) -> tuple[str, str]:
    """'01 - Interpret - Titel (1)' -> ('Interpret', 'Titel')"""
    s = re.sub(r"\s*(\(\d+\)|-\s*(kopie|copy)(\s*\(\d+\))?)\s*$", "", stem, flags=re.I)
    s = re.sub(r"^\s*(\d{1,3}|[a-dA-D]\d{1,2})\s*[-._)]\s*", "", s)
    s = s.replace("_", " ")
    parts = re.split(r"\s+[-–—]\s+", s, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", s.strip()


def _first_tag(tags, keys) -> str:
    if not tags:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            continue
        if v is None:
            continue
        if hasattr(v, "text"):
            v = v.text
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v
    return ""


def read_track(path: str) -> Track:
    p = Path(path)
    ext = p.suffix.lower()
    t = Track(path=str(p), size=p.stat().st_size, ext=ext, lossless=ext in LOSSLESS_EXT)
    if HAVE_MUTAGEN:
        try:
            f = mutagen.File(str(p))
        except Exception:
            f = None
        if f is not None:
            info = getattr(f, "info", None)
            if info is not None:
                t.duration = float(getattr(info, "length", 0) or 0) or None
                t.bitrate = int((getattr(info, "bitrate", 0) or 0) / 1000)
                t.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
                t.bits = int(getattr(info, "bits_per_sample", 0) or 0)
                t.channels = int(getattr(info, "channels", 0) or 0)
                codec = str(getattr(info, "codec", "") or "").lower()
                if codec.startswith("alac"):
                    t.lossless, t.codec = True, "ALAC"
                elif ext == ".m4a":
                    t.codec = "AAC"
            tags = getattr(f, "tags", None)
            t.title = _first_tag(tags, TAG_KEYS["title"])
            t.artist = _first_tag(tags, TAG_KEYS["artist"])
            t.from_tags = bool(t.title)
    fa, ft = parse_filename(p.stem)
    if not t.title:
        t.title = ft
    if not t.artist:
        t.artist = fa
    t.key = (normalize(t.artist), normalize(t.title))
    return t


def sha256(path: str, cancel=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            if cancel and cancel():
                raise InterruptedError
            h.update(block)
    return h.hexdigest()


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def scan_files(folders) -> list[str]:
    out, seen = [], set()
    for folder in folders:
        for cur, _d, fnames in os.walk(folder):
            for fn in fnames:
                if os.path.splitext(fn)[1].lower() in AUDIO_EXT:
                    full = os.path.join(cur, fn)
                    k = os.path.normcase(os.path.abspath(full))
                    if k not in seen:
                        seen.add(k)
                        out.append(full)
    return out


def find_duplicates(folders, same_song: bool = True, tolerance: float = 3.0,
                    progress=None, cancel=None) -> list[DupGroup]:
    paths = scan_files(folders)
    tracks: list[Track] = []
    for i, path in enumerate(paths, 1):
        if cancel and cancel():
            raise InterruptedError
        try:
            tracks.append(read_track(path))
        except OSError:
            continue
        if progress:
            progress(f"Lese Tags {i}/{len(paths)}: {os.path.basename(path)}")

    uf = _UF(len(tracks))

    # 1) identische Inhalte: erst nach Größe vorsortieren, dann hashen
    by_size = defaultdict(list)
    for i, t in enumerate(tracks):
        by_size[t.size].append(i)
    candidates = [i for g in by_size.values() if len(g) > 1 for i in g]
    by_hash = defaultdict(list)
    for n, i in enumerate(candidates, 1):
        if progress:
            progress(f"Vergleiche Inhalte {n}/{len(candidates)}: {tracks[i].name}")
        try:
            tracks[i].hash = sha256(tracks[i].path, cancel)
        except OSError:
            continue
        by_hash[tracks[i].hash].append(i)
    for g in by_hash.values():
        for i in g[1:]:
            uf.union(g[0], i)

    # 2) gleicher Song (Interpret + Titel, ähnliche Dauer)
    if same_song:
        by_key = defaultdict(list)
        for i, t in enumerate(tracks):
            if t.key[1]:
                by_key[t.key].append(i)
        for idx in by_key.values():
            if len(idx) < 2:
                continue
            idx.sort(key=lambda i: tracks[i].duration or 0)
            for a, b in zip(idx, idx[1:]):
                da, db = tracks[a].duration, tracks[b].duration
                if da is None or db is None or abs(da - db) <= tolerance:
                    uf.union(a, b)

    comps = defaultdict(list)
    for i in range(len(tracks)):
        comps[uf.find(i)].append(tracks[i])
    groups = []
    for members in comps.values():
        if len(members) < 2:
            continue
        hashes = {t.hash for t in members}
        reason = ("identische Dateien" if len(hashes) == 1 and None not in hashes
                  else "gleicher Song")
        best = max(members, key=lambda t: t.score())
        members.sort(key=lambda t: t.score(), reverse=True)
        groups.append(DupGroup(members, reason, best))
    groups.sort(key=lambda g: g.label.lower())
    return groups
