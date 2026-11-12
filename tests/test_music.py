import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from safekeep import music


def make_wav(path: Path, freq: float, seconds: float, rate=44100, bits=16,
             title=None, artist=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(rate * seconds)
    width = bits // 8
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / rate)))
                              for i in range(n))
        else:
            frames = bytes(128 + int(60 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n))
        w.writeframes(frames)
    if title and music.HAVE_MUTAGEN:
        from mutagen.id3 import TIT2, TPE1
        from mutagen.wave import WAVE
        f = WAVE(str(path))
        f.add_tags()
        f.tags.add(TIT2(encoding=3, text=title))
        f.tags.add(TPE1(encoding=3, text=artist or ""))
        f.save()


class NormalizeTests(unittest.TestCase):
    def test_normalize(self):
        n = music.normalize
        self.assertEqual(n("Bohemian Rhapsody (Remastered 2011)"), n("Bohemian Rhapsody"))
        self.assertEqual(n("Song [Official Video]"), n("song"))
        self.assertEqual(n("Café del Mar"), n("Cafe Del Mar"))
        self.assertEqual(n("Track feat. Somebody"), n("Track"))
        self.assertEqual(n("The Beatles"), n("Beatles"))
        self.assertEqual(n("Song - 2009 Remaster"), n("Song"))
        self.assertNotEqual(n("Song (Live)"), n("Song"))

    def test_parse_filename(self):
        self.assertEqual(music.parse_filename("03 - Queen - Bohemian Rhapsody (1)"),
                         ("Queen", "Bohemian Rhapsody"))
        self.assertEqual(music.parse_filename("50 Cent - In Da Club"), ("50 Cent", "In Da Club"))
        self.assertEqual(music.parse_filename("Nur ein Titel - Kopie"), ("", "Nur ein Titel"))


class DuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix="dw-musik-"))
        d = cls.d
        # Song A: gute Version (44,1 kHz/16 bit) + schlechtere (22 kHz/8 bit) mit Tags, anderer Name
        make_wav(d / "Album" / "01 Sonnenlied.wav", 440, 2.0, title="Sonnenlied", artist="Die Testband")
        make_wav(d / "Downloads" / "track_x_lowq.wav", 440, 2.0, rate=22050, bits=8,
                 title="Sonnenlied (Official Audio)", artist="Die Testband")
        # identische Kopie mit anderem Namen
        shutil.copy(d / "Album" / "01 Sonnenlied.wav", d / "Downloads" / "kopie irgendwas.wav")
        # Song B: nur Dateiname, zwei Varianten
        make_wav(d / "Album" / "Die Testband - Mondlied.wav", 330, 3.0)
        make_wav(d / "Alt" / "02 - Die Testband - Mondlied (1).wav", 330, 3.0, rate=22050)
        # anderer Song, gleicher Titel, deutlich andere Länge -> kein Duplikat
        make_wav(d / "Alt" / "Andere Band - Sonnenlied.wav", 500, 1.0)
        make_wav(d / "Album" / "Die Testband - Sternlied.wav", 550, 6.0, title="Sternlied",
                 artist="Die Testband")
        # Sternlied gleicher Tag, aber 3 s länger (Live) -> außerhalb Toleranz 2 s
        make_wav(d / "Alt" / "sternlied live.wav", 550, 9.5, title="Sternlied", artist="Die Testband")

    def test_groups(self):
        groups = music.find_duplicates([self.d], same_song=True, tolerance=2)
        by_label = {g.label: g for g in groups}
        names = {lbl: sorted(t.name for t in g.tracks) for lbl, g in by_label.items()}
        self.assertEqual(len(groups), 2, names)

        sonne = next(g for g in groups if "Sonnenlied" in g.label)
        self.assertEqual(sorted(t.name for t in sonne.tracks),
                         ["01 Sonnenlied.wav", "kopie irgendwas.wav", "track_x_lowq.wav"])
        self.assertNotEqual(sonne.best.name, "track_x_lowq.wav")
        self.assertEqual(sonne.best.sample_rate, 44100)

        mond = next(g for g in groups if "Mondlied" in g.label)
        self.assertEqual(mond.best.name, "Die Testband - Mondlied.wav")
        self.assertEqual(len(mond.tracks), 2)

    def test_identical_only(self):
        groups = music.find_duplicates([self.d], same_song=False)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].reason, "identische Dateien")
        self.assertEqual(sorted(t.name for t in groups[0].tracks),
                         ["01 Sonnenlied.wav", "kopie irgendwas.wav"])


if __name__ == "__main__":
    unittest.main()
