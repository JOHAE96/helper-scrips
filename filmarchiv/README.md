# filmarchiv

Filmordner auf dem NAS aufräumen, bevor er in Jellyfin landet: inventarisieren, fehlende Untertitel finden,
Duplikate erkennen, alte DVD-Rips nach MKV (H.265) konvertieren. Ein einzelnes Script, Abhängigkeiten holt `uv`.

Voraussetzungen: `uv`, `ffmpeg` (mit `dvdvideo`-Demuxer, ab ffmpeg 7). Die Filme per NFS mounten, z. B. in `/etc/fstab`:

```
tnas-johannes.local:/mnt/md0/public  /mnt/nas/public  nfs  ro,noatime,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30,hard,timeo=600  0 0
```

## Scan

```sh
./filmarchiv.py scan /mnt/nas/public/media/Filme            # alles
./filmarchiv.py scan /mnt/nas/public/media/Filme --only pate # nur passende Ordner neu scannen
./filmarchiv.py scan --help
```

- Jeder Unterordner ist ein Film. Sammelordner ohne eigene Videos (`Star Wars/Episode IV/…`) werden aufgelöst,
  lose Dateien im Wurzelordner werden zu eigenen Filmen.
- Teile (`CD1/CD2`, `part1`, `Teil 1 von 2`, `1of2`, `Film 1/Film 2`, `CD1/`-Unterordner, `VTS_01_1.VOB …`)
  werden zu einer Quelle gruppiert. Zwei „Teile“ mit je Spielfilmlänge gelten als eigene Filme.
- `VIDEO_TS` und ISO: alle Titel werden geprobt, der längste gilt als Hauptfilm.
- Videos unter 30 MB (`--min-size`), `sample`, `trailer` und `Extras/` werden als Extras ignoriert.
- Ergebnisse landen in `~/.cache/filmarchiv/` (`--state-dir`):
  `inventory.json`, `probe-cache.json` (erneute Scans proben nur neue/geänderte Dateien) und `filmarchiv.log`.

## Tests

```sh
./test_filmarchiv.py
```
