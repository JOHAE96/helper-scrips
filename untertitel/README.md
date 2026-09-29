# untertitel

Lädt fehlende Untertitel für Filme und Serien von [opensubtitles.com](https://www.opensubtitles.com) und legt sie
nach Jellyfin-Schema neben das Video: `Film (1999).de.srt`. Ein einzelnes Script, Abhängigkeiten holt `uv`.

(opensubtitles.org hat seine alte API für Drittanbieter abgeschaltet, deshalb nur .com.)

## Einrichtung

1. Account auf opensubtitles.com anlegen, unter **Profil → API consumers** eine App anlegen (Name z. B. `untertitel`)
   und den API-Key kopieren.
2. `~/.config/untertitel/config.toml`:

   ```toml
   api_key  = "…"
   username = "…"          # Benutzername, nicht die E-Mail
   password = "…"
   # user_agent = "untertitel v0.1"
   ```

   Alternativ Umgebungsvariablen `OPENSUBTITLES_API_KEY`, `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD`.
   Datei mit `chmod 600` schützen.
3. Der Film-/Serienordner muss **beschreibbar** gemountet sein (NFS ohne `ro`).

Kontingent: ohne Login 5 Downloads/Tag, mit Account mehr (das Script zeigt nach dem Login die verbleibenden an).
Suchen kosten kein Kontingent.

## Nutzung

```sh
./untertitel.py missing /mnt/nas/public/media/Filme               # nur anzeigen, ohne API
./untertitel.py fetch   /mnt/nas/public/media/Filme --dry-run     # suchen und Treffer zeigen, nichts laden
./untertitel.py fetch   /mnt/nas/public/media/Filme               # laden (Standard: de,en)
./untertitel.py fetch   /mnt/nas/public/media/Serien --langs de --only "Breaking Bad"
```

- **Vorhanden** zählen externe Untertitel `<Video>.<sprache>[.sdh].srt/.ass/.idx/.sup` und eingebettete Spuren
  (ffprobe, gecacht). Forced-Untertitel zählen nicht. `--ignore-bitmap` zählt eingebettete VobSub/PGS nicht mit,
  falls dein Jellyfin-Client die sonst ins Bild brennt.
- **Suche** per IMDb-ID aus Emby/Jellyfin-`.nfo` (`movie.nfo`, `tvshow.nfo`, `<Video>.nfo`), sonst Titel + Jahr
  bzw. Serie + SxxEyy. Der OpenSubtitles-Hash der Datei wird immer mitgeschickt.
- **Auswahl**: Hash-Treffer (garantiert synchron) > passender Release-Name > Downloadzahl. Maschinell/KI-übersetzte
  und Forced-only-Untertitel werden ausgeschlossen, SDH abgewertet (`--hi` erlaubt sie gleichwertig).
  `--hash-only` nimmt ausschließlich Hash-Treffer.
- Nicht Gefundenes wird erst nach `--retry-days` (7) erneut gesucht, damit tägliche Läufe schnell sind.
  Ist das Tageskontingent aufgebraucht, stoppt das Script; der nächste Lauf macht weiter.
- Zustand, ffprobe-Cache und Log: `~/.cache/untertitel/`.

Aufgeteilte Filme (CD1/CD2) am besten erst mit `../filmarchiv` zu einer Datei zusammenführen: Untertitel gibt es
meist nur für die ganze Länge.

## Tests

```sh
./test_untertitel.py      # simulierte API, kein Account nötig
```
