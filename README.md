# SafeKeep

Windows-Tool (Python + tkinter, deutsche Oberfläche) mit zwei Funktionen:

1. **Backups – Zeitmaschine für Ordner.** Gewählte Ordner (z. B. Spielstände) werden
   automatisch oder per Klick gesichert. Jede Sicherung ist eine eigene Version im
   Verlauf; Wiederherstellen mit Vorschau, ganz oder nur einzelne Dateien.
2. **Musik-Duplikate.** Findet gleiche Songs trotz anderer Dateinamen oder Qualität,
   schlägt pro Gruppe die beste Version vor und verschiebt den Rest in den Papierkorb.

## Start

```bash
python -m pip install -r requirements.txt
```

```bash
python -m safekeep
```

Oder `SafeKeep.pyw` doppelklicken (ohne Konsolenfenster).
Ohne `mutagen` funktioniert die Musiksuche nur über Dateinamen und identische Dateien.

## Sicherheitsregeln

- **Nichts wird überschrieben, ohne dass eine ältere Version erhalten bleibt.**
  Gesicherte Inhalte und Versions-Manifeste werden nie überschrieben (jede Sicherung
  ist eine neue Datei). Bevor eine Wiederherstellung eine vorhandene Datei ersetzt
  oder entfernt, wird der aktuelle Stand automatisch als Version
  „Vor Wiederherstellung“ gesichert. Lässt sich eine betroffene Datei dabei nicht
  lesen (z. B. weil das Spiel sie gesperrt hat), wird die Wiederherstellung abgebrochen.
- **Wiederherstellen nur mit Vorschau:** Der Dialog zeigt für jede Datei, ob sie neu
  erstellt, ersetzt oder nicht angefasst wird; Doppelklick öffnet die gesicherte
  Version zum Ansehen.
- **Gelöscht wird nur in den Windows-Papierkorb** – alte Versionen, aufgeräumter
  Speicher, Musik-Duplikate, Dateien, die beim Wiederherstellen entfernt werden.
  Hat ein Laufwerk keinen Papierkorb, fragt Windows vorher nach.
- In jeder Duplikat-Gruppe bleibt immer mindestens eine Datei übrig.

## Backups im Detail

- **Neu …** → Ordner wählen (Schnellauswahl für *Gespeicherte Spiele*, *My Games*,
  *AppData* …), Intervall in Minuten (0 = nur manuell), Anzahl Versionen
  (0 = alle behalten), Ausschluss-Muster wie `*.tmp, cache, logs/*`.
- Automatische Sicherungen legen nur dann eine neue Version an, wenn sich etwas
  geändert hat. „Jetzt sichern“ legt immer eine an.
- Der Verlauf zeigt pro Version Art, Dateien, Größe und Änderungen gegenüber der
  vorherigen (neu / geändert / weg).
- Gleiche Dateiinhalte werden nur einmal gespeichert (Deduplizierung über SHA-256),
  viele Versionen kosten daher wenig Platz.
- Gesperrte Dateien werden übersprungen und unter „Fehler anzeigen“ gelistet; in der
  Version steckt dann ihr letzter bekannter Stand.
- Ohne Oberfläche, z. B. für die Windows-Aufgabenplanung:
  `python -m safekeep --sichern`

Aufbau des Backup-Speichers (Standard: `%USERPROFILE%\SafeKeep-Backups`):

```
objects/ab/abcdef…           Dateiinhalte, benannt nach SHA-256
snapshots/<job-id>/<zeit>.json  eine Datei pro Version
snapshots/_wiederherstellung/  Sicherungen vor Wiederherstellung in fremde Ordner
```

Einstellungen: `%APPDATA%\SafeKeep\config.json`.

## Musik-Duplikate im Detail

- **Identische Dateien:** gleicher Inhalt (SHA-256), egal wie sie heißen.
- **Gleicher Song:** gleicher Interpret und Titel – aus den Tags oder aus dem
  Dateinamen (`01 - Interpret - Titel (1).mp3`) – bei ähnlicher Länge
  (Toleranz einstellbar). Zusätze wie „(Official Video)“, „Remastered 2011“,
  „feat. …“, Akzente und Groß-/Kleinschreibung werden ignoriert; „(Live)“ oder
  „(Remix)“ zählen als eigene Version.
- **Beste Version:** verlustfrei (FLAC, WAV, ALAC …) vor verlustbehaftet, dann
  höhere Bitrate, Abtastrate, Bittiefe, vorhandene Tags, Format (FLAC vor WAV).
- Markierung per Doppelklick/Leertaste umschalten oder „Als beste Version behalten“.

## Claude-Schnittstelle

`cli.py` macht SafeKeep für Claude (über den MCP-Server ToolHub) bedienbar.
`toolhub.json` beschreibt alle Befehle für ToolHub.

Jeder Befehl gibt **ein JSON-Objekt auf stdout** aus (`"ok": true, …`). Bei einem Fehler
steht die Meldung auf stderr, auf stdout kommt `{"ok": false, "error": "…"}`, und der
Exit-Code ist 1 (2 bei falschen Argumenten).

| Befehl | ändert etwas? | Zweck |
|---|---|---|
| `status` | nein | Speicherort, belegter Platz, Anzahl Sicherungen |
| `jobs` | nein | gesicherte Ordner mit ID, Pfad, Versionen |
| `versions --job X [--limit N]` | nein | Versionen, neueste zuerst, mit Änderungen |
| `files --job X [--version V] [--pattern *.sav]` | nein | Dateien einer Version |
| `music-dupes [--folder A,B] [--identical-only] [--tolerance S]` | nein | Musik-Duplikate + beste Version |
| `backup --job X \| --all [--force]` | ja (legt nur neue Version an) | jetzt sichern |
| `restore --job X [--version V] [--target D] [--files a,b] [--remove-extra] [--dry-run]` | ja | wiederherstellen |
| `delete-version --job X --version V [--dry-run]` | ja | Version in den Papierkorb |
| `add-job --folder D [--name N] [--interval M] [--keep K] [--exclude …] [--dry-run]` | ja (Einstellungen) | Ordner hinzufügen |
| `music-clean [--folder …] [--dry-run]` | ja | Duplikate außer der besten in den Papierkorb |

`--job` nimmt die ID oder den Namen. `--version` nimmt die ID, einen eindeutigen Anfang
davon oder `latest`. Mit `--dry-run` zeigen die verändernden Befehle nur die Vorschau,
z. B. bei `restore` welche Dateien erstellt, ersetzt oder in den Papierkorb verschoben
würden. Auch über die Schnittstelle gelten die Sicherheitsregeln: Vor dem Ersetzen wird
automatisch gesichert, und gelöscht wird nur in den Papierkorb.

```bash
python cli.py restore --job Spielstand --version latest --dry-run
```

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

Die Tests legen Testordner im Temp-Verzeichnis an, sichern, verändern und stellen
wieder her, prüfen die Sicherung vor dem Überschreiben, den Abbruch bei gesperrten
Dateien und die Duplikat-Erkennung an erzeugten WAV-Dateien. Ein Test verschiebt
eine kleine Datei `safekeep-papierkorb-test.txt` in den echten Papierkorb.
`tests/test_cli.py` führt jeden Befehl aus `toolhub.json` an Testordnern aus (Papierkorb dort simuliert).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
