---
tags:
  - claude-generated
Datum: 2026-09-27
---

# Prompt für Claude Code: Filmarchiv-Tool (Scan, Untertitel, Duplikate, Konvertierung)

> Diesen Abschnitt komplett in Claude Code einfügen.

---

## Kontext

Ich habe auf meinem NAS einen großen Filmordner, überwiegend alte DVD-Rips. Ich migriere gerade von **Emby zu Jellyfin** und möchte das Archiv vorher aufräumen und vereinheitlichen.

Struktur (ungefähr, bitte beim Scan verifizieren und nicht blind annehmen):

- Pro Film ein Ordner mit dem Filmnamen, z. B. `/filme/Der Pate/`
- Darin die Filmdatei(en). Mögliche Varianten:
  - komplette DVD-Struktur (`VIDEO_TS/` mit `VTS_01_1.VOB`, `VTS_01_2.VOB`, …, `.IFO`, `.BUP`)
  - ISO-Images
  - bereits konvertierte Dateien (`.mkv`, `.mp4`, `.avi`, `.m4v`)
  - **aufgesplittete Filme**: `CD1/CD2`, `part1/part2`, `teil1`, `-1of2`, fortlaufende VOBs usw.
- Teilweise externe Untertitel (`.srt`, `.ass`, `.sub/.idx`, `.sup`)
- Einige Filme liegen vermutlich **doppelt** vor (unterschiedliche Ordnernamen, Qualität oder Formate)

## Hardware & Laufumgebung

- **NAS:** TerraMaster TNAS F4-210 (ARM, wenig RAM) – nur Speicher, **nicht** zum Encodieren geeignet.
- **Ausführung:** Intel NUC8i5 (i5-8259U, Iris Plus 655, Linux), Filmordner per **NFS oder SMB** vom NAS gemountet.
- Laufzeit ist egal, das Tool darf tagelang laufen. **Qualität vor Geschwindigkeit.**
- Daraus folgt:
  - Standard: **Software-Encoding** (libx264 bzw. libx265) mit langsamem Preset und CRF, da DVD-Material (SD) auch so schnell genug ist.
  - Hardware-Encoding per Intel **QSV/VAAPI** (iHD-Treiber) nur optional per Flag.
  - Temporäre Dateien **lokal auf dem NUC** schreiben, erst die fertige, verifizierte Datei aufs NAS kopieren (schont Netzwerk und NAS, vermeidet halbe Dateien bei Abbruch).
  - Standardmäßig nur **1 paralleler Encode-Job** (`--workers` für den Scan separat einstellbar).
  - Das Tool muss Abbrüche (Neustart, Netzwerkausfall) sauber überstehen und beim nächsten Start weitermachen.
- Die erzeugten Dateien sollen in Jellyfin möglichst per **Direct Play** laufen (H.264 bzw. HEVC, Originalton AC3 behalten, optional zusätzliche AAC-Stereospur per Flag), damit später kein Transcoding nötig ist.

## Ziel

Ein **Python-CLI-Tool**, dem ich den Pfad zum Filmordner übergebe und das vier Aufgaben erledigt:

### 1. Scan / Inventar (`scan`)
- Rekursiv alle Filmordner durchgehen, Teile eines Films korrekt als **einen** Film gruppieren.
- Pro Datei per `ffprobe` (JSON-Output) auslesen:
  - Container, Dauer, Dateigröße, Video-Codec, Auflösung, Bitrate
  - Alle **Audiospuren**: Sprache, Codec, Kanäle
  - Alle **eingebetteten Untertitel**: Sprache, Codec, Typ (Text vs. Bitmap wie VobSub/PGS), Forced-Flag
  - Kapitel ja/nein
- Externe Untertiteldateien im Ordner erkennen und der Sprache zuordnen (Dateiname, ggf. Spracherkennung bei `.srt`).
- Ergebnisse in einem Cache speichern (JSON oder SQLite), damit Folgeläufe nur neue/geänderte Dateien prüfen (mtime + Größe).

### 2. Report & fehlende Untertitel (`report`)
- Übersicht als **CSV** und **Markdown** (optional HTML): Film, Format, Teile, Audiosprachen, Untertitelsprachen (intern/extern), Größe, Dauer.
- Konfigurierbare Pflichtsprachen (z. B. `--require-subs de,en`) → Liste aller Filme, bei denen Untertitel **fehlen**.
- Kennzeichnen, welche Untertitel Bitmap-basiert sind (wichtig für die Konvertierung).

### 3. Duplikate finden (`dupes`)
- Titel normalisieren (Jahr, Auflösungs-/Release-Tags, Klammern, Sonderzeichen, Umlaute, „Teil/CD“-Suffixe entfernen).
- Fuzzy-Matching (z. B. `rapidfuzz`) plus Abgleich der **Laufzeit** (Toleranz konfigurierbar, Standard ±3 Min).
- Optional: Hash-Vergleich für identische Dateien (Teil-Hash über Anfang/Mitte/Ende, nicht die ganze Datei, wegen NAS-I/O).
- Pro Duplikatgruppe eine Empfehlung, welche Version besser ist (Auflösung, Bitrate, Anzahl Audio-/Untertitelspuren).
- **Niemals automatisch löschen.** Nur Report; optional ein Verschieben in einen Quarantäne-Ordner per explizitem Flag.

### 4. Konvertierung (`convert`)
- Quelle: DVD-Struktur, ISO, VOB, AVI usw. → Zielcontainer konfigurierbar, **Standard MKV**, optional MP4.
- **Alle Audiospuren** und **alle Untertitelspuren** übernehmen, inklusive Sprach-Metadaten, Default-/Forced-Flags und Kapiteln.
- Aufgesplittete Teile zu **einer** Datei zusammenführen.
- Bei DVD-Struktur den richtigen Titel wählen (Haupttitel = längster Titel), Menüs/Extras ignorieren.
- Video: Remux, wenn der Codec schon passt; sonst Encoding nach H.264 oder HEVC (konfigurierbar, CRF-basiert). Deinterlacing automatisch bei interlaced Material.
- Optional Hardware-Encoding (VAAPI / Intel QSV) per Flag.
- **Untertitel-Problem MP4:** DVD-Untertitel sind Bitmaps (VobSub). MP4 unterstützt nur `mov_text`. Wenn MP4 gewählt ist, bitte sauber behandeln: als externe `.sub/.idx`- bzw. `.sup`-Datei daneben legen, optional OCR zu SRT (z. B. per Tesseract, nur mit Flag), und eine Warnung im Log ausgeben. In MKV einfach mitnehmen.
- Externe Untertitel entweder einbetten oder daneben lassen (Flag).
- Ausgabe in einen **separaten Zielordner**, Originale niemals überschreiben oder löschen.
- Benennung nach **Jellyfin-Konvention**: `Filmname (Jahr)/Filmname (Jahr).mkv`, externe Untertitel als `Filmname (Jahr).de.srt`, `Filmname (Jahr).en.forced.srt`.
- Nach der Konvertierung verifizieren: Dauer vergleichen, Anzahl Spuren prüfen, bei Abweichung als fehlerhaft markieren.
- Fortsetzbar: bereits erledigte Filme überspringen, Status im Cache speichern.

## Technische Anforderungen

- Python 3.10+, `ffmpeg`/`ffprobe` als externe Abhängigkeit (Prüfung beim Start mit klarer Fehlermeldung).
- CLI mit Subcommands (`scan`, `report`, `dupes`, `convert`), z. B. mit `typer` oder `argparse`.
- Wichtige Optionen: `--path`, `--output`, `--dry-run`, `--workers`, `--container mkv|mp4`, `--codec`, `--crf`, `--hwaccel`, `--require-subs`, `--only <Filmname>`.
- `--dry-run` zeigt die geplanten ffmpeg-Befehle, ohne etwas auszuführen.
- Robust gegen Leerzeichen, Umlaute, Sonderzeichen und langsame SMB/NFS-Freigaben.
- Sauberes Logging (Konsole + Logdatei), Fortschrittsanzeige (z. B. `rich` oder `tqdm`).
- Konfigurierbar über optionale `config.yaml`.
- Code modular aufbauen (Scanner, Grouping, Duplikate, Converter, Reports getrennt), mit Type Hints.
- Unit-Tests (pytest) für Titel-Normalisierung, Teile-Gruppierung und Duplikaterkennung mit Beispiel-Ordnerstrukturen (keine echten Videos nötig, ffprobe mocken).
- `README.md` mit Installation, Beispielaufrufen und empfohlenem Ablauf.
- `requirements.txt` bzw. `pyproject.toml`.

## Vorgehen

1. **Stelle mir zuerst Rückfragen**, bevor du Code schreibst, insbesondere zu:
   - NFS oder SMB, und läuft das Tool nativ oder in Docker auf dem NUC?
   - MKV oder MP4 als Standard?
   - Welche Pflicht-Untertitelsprachen?
   - Soll Video neu encodiert oder möglichst nur geremuxt werden (Qualität vs. Speicher vs. Zeit)?
2. Schlage eine Architektur und Dateistruktur vor und warte auf mein OK.
3. Implementiere in dieser Reihenfolge: Scan → Report → Duplikate → Konvertierung. Nach jedem Schritt kurz zeigen, wie ich ihn teste.
4. Bitte keine destruktiven Operationen ohne explizites Flag und Bestätigung.
