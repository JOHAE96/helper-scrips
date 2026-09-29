#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["typer>=0.12", "rich>=13"]
# ///
"""Filmarchiv: Filmordner scannen, Untertitel prüfen, Duplikate finden, nach MKV konvertieren.

Aufruf:  ./filmarchiv.py scan /mnt/nas/public/media/Filme
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console(stderr=True)
log = logging.getLogger("filmarchiv")

DEFAULT_STATE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "filmarchiv"
CACHE_VERSION = 1

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv", ".mov",
             ".divx", ".ogm", ".vob", ".flv", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
BITMAP_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
# Systemordner von NAS/Betriebssystemen, die nie Filme enthalten
SKIP_DIRS = {"@eadir", "#recycle", "@recycle", ".@__thumb", ".appledouble", "$recycle.bin",
             "system volume information", "lost+found", "audio_ts"}
EXTRA_RE = re.compile(r"(?<![a-z])(sample|trailer|featurettes?|making[\s._-]?of|bonus|extras?|"
                      r"deleted[\s._-]scenes?|behind[\s._-]the[\s._-]scenes)(?![a-z])", re.I)
EXTRA_MAX_SIZE = 30 * 1024 * 1024  # kleinere Videodateien sind Samples/Trailer

PART_RE = re.compile(r"[\s._-]*[\[(]?(?<![a-z])(?:cd|disc|disk|part|pt|teil)[\s._-]*(\d{1,2})(?!\d)"
                     r"(?:[\s._-]*(?:of|von)[\s._-]*\d{1,2})?[\])]?", re.I)
OF_RE = re.compile(r"[\s._-]*[\[(]?(?<!\d)(\d{1,2})[\s._-]*(?:of|von)[\s._-]*\d{1,2}(?!\d)[\])]?", re.I)
DIRPART_RE = re.compile(r"^(?:cd|dis[ck]|dvd|part|teil)[\s._-]*(\d{1,2})$", re.I)
VOB_RE = re.compile(r"^(VTS_\d\d)_(\d)$", re.I)
TRAILING_RE = re.compile(r"^(.*?)[\s._-]+([1-9]|[ab])$", re.I)
MULTIPART_MIN_STANDALONE = 70 * 60  # sind alle "Teile" länger, sind es eher eigenständige Filme

LANG_MAP = {
    "de": "de", "ger": "de", "deu": "de", "german": "de", "deutsch": "de",
    "en": "en", "eng": "en", "english": "en", "englisch": "en",
    "fr": "fr", "fre": "fr", "fra": "fr", "french": "fr", "französisch": "fr",
    "es": "es", "spa": "es", "spanish": "es", "spanisch": "es",
    "it": "it", "ita": "it", "italian": "it", "italienisch": "it",
    "nl": "nl", "dut": "nl", "nld": "nl", "dutch": "nl",
    "tr": "tr", "tur": "tr", "turkish": "tr",
    "pl": "pl", "pol": "pl", "polish": "pl",
    "ru": "ru", "rus": "ru", "russian": "ru",
    "ja": "ja", "jpn": "ja", "japanese": "ja",
    "da": "da", "dan": "da", "sv": "sv", "swe": "sv", "no": "no", "nor": "no",
    "fi": "fi", "fin": "fi", "pt": "pt", "por": "pt", "cs": "cs", "cze": "cs", "ces": "cs",
    "hu": "hu", "hun": "hu", "el": "el", "gre": "el", "ell": "el",
}
STOPWORDS = {
    "de": set("der die das und ist nicht ich du sie wir ein eine zu mit auf was wie ja nein "
              "mir dich mich hier jetzt sind haben wird kann schon noch auch".split()),
    "en": set("the and is not you we a to with on what how yes no it that i'm don't "
              "this here now are have will can just your".split()),
    "fr": set("le la les et est pas je tu vous nous un une que qui ce ça oui non".split()),
    "es": set("el la los y es no yo tú que un una por qué sí está".split()),
}
RELEASE_TAG_RE = re.compile(
    r"(?<![a-z0-9])(german|deutsch|dl|ac3|dts|dd5\.?1|dvd-?rip|dvdr|dvd|bd-?rip|br-?rip|blu-?ray|hdtv|"
    r"web-?dl|web-?rip|xvid|divx|x264|x265|h\.?264|h\.?265|hevc|avc|480p|576p|720p|1080p|2160p|"
    r"pal|ntsc|remux|uncut|extended|directors?[\s._-]cut|unrated|proper|repack|internal|"
    r"dubbed|multi|ws|fs|mpeg-?2)(?![a-z0-9])", re.I)
YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d\d)(?![0-9])")


# --------------------------------------------------------------------------- Datenmodell

@dataclass
class Source:
    """Eine Version eines Films: eine Datei, mehrere Teile, eine DVD-Struktur oder ein ISO."""
    kind: str                       # file | dvd | iso
    parts: list[str]                # absolute Pfade (bei dvd: Ordner mit VIDEO_TS.IFO)
    size: int = 0
    dvd_title: list[int] = field(default_factory=list)          # gewählter Titel je Teil
    dvd_titles: list[dict[str, float]] = field(default_factory=list)  # je Teil: Titel -> Dauer
    probe: list[dict] = field(default_factory=list)              # je Teil ein Probe-Summary
    error: Optional[str] = None

    @property
    def duration(self) -> float:
        return sum(p.get("duration") or 0 for p in self.probe)


@dataclass
class ExtSub:
    path: str
    format: str                     # srt, ass, vobsub, pgs, ...
    lang: Optional[str]
    lang_source: Optional[str]      # filename | content | idx
    forced: bool
    bitmap: bool


@dataclass
class Movie:
    folder: str                     # relativ zum Wurzelordner
    title: str
    year: Optional[int]
    sources: list[Source] = field(default_factory=list)
    subtitles: list[ExtSub] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Movie":
        d = dict(d)
        d["sources"] = [Source(**s) for s in d.get("sources", [])]
        d["subtitles"] = [ExtSub(**s) for s in d.get("subtitles", [])]
        return cls(**d)


# --------------------------------------------------------------------------- Hilfsfunktionen

def norm_lang(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = value.strip().lower()
    if v in ("und", "unknown", "mis", "zxx", ""):
        return None
    return LANG_MAP.get(v, v)


def parse_title(name: str) -> tuple[str, Optional[int]]:
    """'Der.Pate.1972.German.DVDRip' -> ('Der Pate', 1972)."""
    s = name.strip()
    if " " not in s and (s.count(".") >= 2 or "_" in s):
        s = re.sub(r"[._]+", " ", s)
    s = s.replace("_", " ")
    max_year = date.today().year + 1

    year: Optional[int] = None
    cut: Optional[int] = None
    bracketed = re.search(r"[(\[]((?:19|20)\d\d)[)\]]", s)
    if bracketed and bracketed.start() > 0 and int(bracketed[1]) <= max_year:
        year, cut = int(bracketed[1]), bracketed.start()
    else:
        for m in YEAR_RE.finditer(s):
            if m.start() > 0 and s[:m.start()].strip(" -.([") and int(m[1]) <= max_year:
                year, cut = int(m[1]), m.start()
    title = s[:cut] if cut is not None else s
    tag = RELEASE_TAG_RE.search(title)
    if tag and title[:tag.start()].strip(" -.([_"):
        title = title[:tag.start()]
    title = re.sub(r"[\s(\[{-]+$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" -.")
    return (title or name.strip()), year


def split_part(name: str) -> tuple[str, Optional[int]]:
    """'Film.CD2.German' -> ('Film German', 2); ohne Teilkennung -> (name, None)."""
    m = PART_RE.search(name) or OF_RE.search(name)
    if not m:
        return name, None
    base = f"{name[:m.start()]} {name[m.end():]}"
    return base, int(m[1])


def key_of(text: str) -> str:
    return re.sub(r"[\s._-]+", " ", text).strip().lower()


def is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIRS


def find_ci(directory: Path, filename: str) -> Optional[Path]:
    """Datei case-insensitive in einem Ordner finden (DVD-Rips haben oft video_ts.ifo)."""
    try:
        for entry in directory.iterdir():
            if entry.name.lower() == filename.lower():
                return entry
    except OSError:
        pass
    return None


def dir_signature(directory: Path) -> list[int]:
    size, mtime = 0, 0
    for entry in directory.iterdir():
        if entry.is_file():
            st = entry.stat()
            size += st.st_size
            mtime = max(mtime, st.st_mtime_ns)
    return [size, mtime]


def file_signature(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def check_tools(*tools: str) -> None:
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        console.print(f"[red]Fehlt: {', '.join(missing)}.[/] Bitte installieren (z. B. `sudo pacman -S ffmpeg`).")
        raise typer.Exit(2)


# --------------------------------------------------------------------------- ffprobe

class ProbeError(RuntimeError):
    pass


def run_ffprobe(target: Path, pre_args: tuple[str, ...] = (), timeout: int = 900) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-show_chapters", *pre_args, "-i", str(target)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe Timeout nach {timeout}s") from exc
    if res.returncode != 0:
        raise ProbeError(res.stderr.strip()[-400:] or f"ffprobe Exit {res.returncode}")
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe lieferte kein JSON: {exc}") from exc


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize_probe(data: dict) -> dict:
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    summary: dict[str, Any] = {
        "container": fmt.get("format_name"),
        "duration": _float(fmt.get("duration")),
        "size": _int(fmt.get("size")),
        "bitrate": _int(fmt.get("bit_rate")),
        "video": None,
        "audio": [],
        "subs": [],
        "chapters": len(data.get("chapters", [])),
    }
    if video:
        summary["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "dar": video.get("display_aspect_ratio"),
            "fps": video.get("avg_frame_rate") or video.get("r_frame_rate"),
            "field_order": video.get("field_order"),
            "bitrate": _int(video.get("bit_rate")),
        }
    for s in streams:
        tags = {k.lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition", {})
        if s.get("codec_type") == "audio":
            summary["audio"].append({
                "index": s.get("index"), "lang": norm_lang(tags.get("language")),
                "codec": s.get("codec_name"), "channels": s.get("channels"),
                "layout": s.get("channel_layout"), "title": tags.get("title"),
                "default": bool(disp.get("default")),
            })
        elif s.get("codec_type") == "subtitle":
            summary["subs"].append({
                "index": s.get("index"), "lang": norm_lang(tags.get("language")),
                "codec": s.get("codec_name"), "bitmap": s.get("codec_name") in BITMAP_SUB_CODECS,
                "forced": bool(disp.get("forced")), "default": bool(disp.get("default")),
                "title": tags.get("title"),
            })
    return summary


class ProbeCache:
    """ffprobe-Ergebnisse als JSON, gültig solange Größe + mtime gleich bleiben."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.dirty = 0
        self.entries: dict[str, dict] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") == CACHE_VERSION:
                    self.entries = data.get("entries", {})
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Cache %s unlesbar (%s), starte leer", path, exc)

    def get(self, key: str, sig: list[int]) -> Optional[dict]:
        with self.lock:
            entry = self.entries.get(key)
        return entry["data"] if entry and entry.get("sig") == sig else None

    def put(self, key: str, sig: list[int], data: dict) -> None:
        with self.lock:
            self.entries[key] = {"sig": sig, "data": data}
            self.dirty += 1
            if self.dirty >= 25:
                self._save()

    def save(self) -> None:
        with self.lock:
            self._save()

    def _save(self) -> None:
        atomic_write_json(self.path, {"version": CACHE_VERSION, "entries": self.entries})
        self.dirty = 0


def probe_cached(cache: ProbeCache, target: Path, sig: list[int], pre_args: tuple[str, ...] = (),
                 key_suffix: str = "") -> dict:
    key = f"{target}{key_suffix}"
    hit = cache.get(key, sig)
    if hit is not None:
        return hit
    summary = summarize_probe(run_ffprobe(target, pre_args))
    cache.put(key, sig, summary)
    return summary


# --------------------------------------------------------------------------- DVD

def _iso_find(iso: Path, components: list[str]) -> Optional[bytes]:
    """Minimaler ISO9660-Leser: Datei (z. B. VIDEO_TS/VIDEO_TS.IFO) aus einem ISO lesen."""
    with open(iso, "rb") as fh:
        fh.seek(16 * 2048)
        pvd = fh.read(2048)
        if pvd[1:6] != b"CD001":
            return None
        root = pvd[156:190]
        extent, size = struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0]
        for comp in components:
            fh.seek(extent * 2048)
            data = fh.read(size)
            pos, found = 0, None
            while pos < len(data):
                length = data[pos]
                if length == 0:  # Rest des Sektors ist leer
                    pos = (pos // 2048 + 1) * 2048
                    continue
                rec = data[pos:pos + length]
                ident = rec[33:33 + rec[32]].decode("ascii", "replace").split(";")[0].rstrip(".")
                if ident.upper() == comp.upper():
                    found = struct.unpack("<I", rec[2:6])[0], struct.unpack("<I", rec[10:14])[0]
                    break
                pos += length
            if not found:
                return None
            extent, size = found
        fh.seek(extent * 2048)
        return fh.read(size)


def dvd_title_count(kind: str, path: Path) -> Optional[int]:
    """Anzahl der Titel aus der VMG-Titeltabelle (TT_SRPT) in VIDEO_TS.IFO."""
    try:
        if kind == "iso":
            data = _iso_find(path, ["VIDEO_TS", "VIDEO_TS.IFO"])
        else:
            ifo = find_ci(path, "VIDEO_TS.IFO")
            data = ifo.read_bytes() if ifo else None
    except OSError:
        return None
    if not data or not data.startswith(b"DVDVIDEO-VMG"):
        return None
    offset = struct.unpack(">I", data[0xC4:0xC8])[0] * 2048
    if offset + 2 > len(data):
        return None
    return struct.unpack(">H", data[offset:offset + 2])[0] or None


def probe_dvd(cache: ProbeCache, kind: str, path: Path) -> tuple[int, dict[str, float], dict]:
    """Alle Titel einer DVD/ISO proben; liefert (haupttitel, {titel: dauer}, probe des haupttitels)."""
    sig = file_signature(path) if kind == "iso" else dir_signature(path)
    count = dvd_title_count(kind, path)
    limit = count or 99  # ohne lesbare Titeltabelle: durchprobieren bis zum ersten Fehler
    results: dict[int, dict] = {}
    last_error = None
    for n in range(1, limit + 1):
        try:
            results[n] = probe_cached(cache, path, sig, ("-f", "dvdvideo", "-title", str(n)),
                                      key_suffix=f"#title={n}")
        except ProbeError as exc:
            last_error = str(exc)
            if count is None:
                break
            log.debug("%s Titel %d: %s", path, n, exc)
    if not results:
        raise ProbeError(f"kein lesbarer DVD-Titel ({last_error})")
    best = max(results, key=lambda n: results[n].get("duration") or 0)
    return best, {str(n): r.get("duration") or 0 for n, r in results.items()}, results[best]


# --------------------------------------------------------------------------- Untertitel

def _read_text(path: Path, limit: int = 65536) -> str:
    raw = path.open("rb").read(limit)
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def detect_text_lang(text: str) -> Optional[str]:
    words = re.findall(r"[a-zäöüßéèàùâêîôûçñ']+", text.lower())
    scores = {lang: sum(w in sw for w in words) for lang, sw in STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, second = ranked[0], ranked[1]
    if best[1] >= 15 and best[1] >= 1.5 * max(second[1], 1):
        return best[0]
    return None


def lang_from_filename(stem: str) -> tuple[Optional[str], bool]:
    tokens = [t for t in re.split(r"[\s._\-\[\]()]+", stem.lower()) if t]
    forced = any(t in ("forced", "erzwungen") for t in tokens)
    for tok in reversed(tokens[1:]):  # erstes Token ist meist der Filmtitel ("It", "Up")
        if tok in LANG_MAP:
            return LANG_MAP[tok], forced
    return None, forced


def inspect_subtitle(path: Path) -> list[ExtSub]:
    ext = path.suffix.lower()
    lang, forced = lang_from_filename(path.stem)
    lang_source = "filename" if lang else None
    if ext == ".idx":
        langs = re.findall(r"^id:\s*([a-z]{2,3})", _read_text(path, 1 << 20), re.M)
        if langs:
            return [ExtSub(str(path), "vobsub", norm_lang(l), "idx", forced, True) for l in langs]
        return [ExtSub(str(path), "vobsub", lang, lang_source, forced, True)]
    if ext == ".sup":
        return [ExtSub(str(path), "pgs", lang, lang_source, forced, True)]
    fmt = {".srt": "srt", ".ass": "ass", ".ssa": "ssa", ".vtt": "vtt", ".sub": "microdvd"}[ext]
    if not lang:
        try:
            lang = detect_text_lang(_read_text(path))
            lang_source = "content" if lang else None
        except OSError:
            pass
    return [ExtSub(str(path), fmt, lang, lang_source, forced, False)]


# --------------------------------------------------------------------------- Gruppierung

def part_info(kind: str, path: Path) -> tuple[Optional[tuple], Optional[int]]:
    """Gruppenschlüssel und Teilnummer, falls die Quelle Teil eines gesplitteten Films ist."""
    if kind == "dvd":
        name, parent = ("", path.parent) if path.name.lower() == "video_ts" else (path.name, path.parent)
        ext = ""
    else:
        name, parent, ext = path.stem, path.parent, path.suffix.lower()
    # Teil über Ordnernamen: .../CD1/film.avi oder .../Disc 2/VIDEO_TS
    m = DIRPART_RE.match(parent.name)
    if m:
        return (kind, str(parent.parent), "<dir>", ext), int(m[1])
    if kind == "file":
        vm = VOB_RE.match(name)
        if vm:
            return (kind, str(parent), vm[1].lower(), ext), int(vm[2])
    base, num = split_part(name)
    if num is not None:
        return (kind, str(parent), key_of(base), ext), num
    return None, None


def group_sources(cands: list[tuple[str, Path]]) -> tuple[list[Source], list[str]]:
    """Kandidaten (kind, pfad) zu Quellen zusammenfassen. Liefert (quellen, warnungen)."""
    groups: dict[tuple, dict[int, Path]] = defaultdict(dict)
    singles: list[tuple[str, Path]] = []
    warnings: list[str] = []
    for kind, path in cands:
        key, num = part_info(kind, path)
        if key is None:
            singles.append((kind, path))
        elif num in groups[key]:
            warnings.append(f"Teil {num} doppelt: {groups[key][num].name} / {path.name}")
            singles.append((kind, path))
        else:
            groups[key][num] = path

    sources: list[Source] = []
    for key, parts in groups.items():
        if len(parts) == 1:
            singles.append((key[0], next(iter(parts.values()))))
            continue
        nums = sorted(parts)
        if nums != list(range(nums[0], nums[0] + len(nums))) or nums[0] > 1:
            warnings.append(f"Teile unvollständig ({', '.join(map(str, nums))}): {parts[nums[0]].name}")
        sources.append(Source(kind=key[0], parts=[str(parts[n]) for n in nums]))

    # Rückfall: "Film 1.avi" + "Film 2.avi" / "Film a.avi" + "Film b.avi"
    trailing: dict[tuple, dict[int, Path]] = defaultdict(dict)
    rest: list[tuple[str, Path]] = []
    for kind, path in singles:
        m = TRAILING_RE.match(path.stem) if kind == "file" else None
        if m:
            tok = m[2].lower()
            num = ord(tok) - ord("a") + 1 if tok.isalpha() else int(tok)
            key = (str(path.parent), key_of(m[1]), path.suffix.lower())
            if num not in trailing[key]:
                trailing[key][num] = path
                continue
        rest.append((kind, path))
    for key, parts in trailing.items():
        nums = sorted(parts)
        if len(nums) >= 2 and nums == list(range(1, len(nums) + 1)):
            sources.append(Source(kind="file", parts=[str(parts[n]) for n in nums]))
        else:
            rest.extend(("file", p) for p in parts.values())

    sources.extend(Source(kind=k, parts=[str(p)]) for k, p in rest)
    sources.sort(key=lambda s: s.parts[0])
    return sources, warnings


def collect_folder(movie_dir: Path) -> tuple[list[tuple[str, Path]], list[Path], list[str], list[str]]:
    """Ordner rekursiv einsammeln: (kandidaten, untertitel, extras, warnungen)."""
    cands: list[tuple[str, Path]] = []
    subs: list[Path] = []
    extras: list[str] = []
    warnings: list[str] = []

    def onerror(exc: OSError) -> None:
        warnings.append(f"Lesefehler: {exc}")

    for dirpath, dirnames, filenames in os.walk(movie_dir, onerror=onerror):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(d))
        lower = {f.lower() for f in filenames}
        in_extra_dir = here != movie_dir and EXTRA_RE.search(str(here.relative_to(movie_dir)))
        if "video_ts.ifo" in lower:
            (extras.append(str(here)) if in_extra_dir else cands.append(("dvd", here)))
            dirnames[:] = []
            subs.extend(here / f for f in filenames if Path(f).suffix.lower() in SUB_EXT)
            continue
        for fname in sorted(filenames):
            path = here / fname
            ext = path.suffix.lower()
            if fname.startswith("."):
                continue
            if ext in SUB_EXT:
                subs.append(path)
            elif ext == ".iso":
                (extras.append(str(path)) if in_extra_dir else cands.append(("iso", path)))
            elif ext in VIDEO_EXT:
                if ext == ".vob" and VOB_RE.match(path.stem) and path.stem.endswith("_0"):
                    continue  # Menü-VOB
                try:
                    small = path.stat().st_size < EXTRA_MAX_SIZE
                except OSError as exc:
                    warnings.append(f"Lesefehler: {exc}")
                    continue
                if in_extra_dir or EXTRA_RE.search(path.stem) or small:
                    extras.append(str(path))
                else:
                    cands.append(("file", path))
    return cands, subs, extras, warnings


def has_media(directory: Path) -> bool:
    for _dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d)]
        if any(Path(f).suffix.lower() in VIDEO_EXT | {".iso", ".ifo"} for f in filenames):
            return True
    return False


def build_movie(root: Path, movie_dir: Path) -> Movie:
    cands, sub_paths, extras, warnings = collect_folder(movie_dir)
    sources, group_warnings = group_sources(cands)
    title, year = parse_title(movie_dir.name)
    subs: list[ExtSub] = []
    idx_stems = {p.with_suffix("").as_posix().lower() for p in sub_paths if p.suffix.lower() == ".idx"}
    for sp in sub_paths:
        if sp.suffix.lower() == ".sub" and sp.with_suffix("").as_posix().lower() in idx_stems:
            continue  # gehört zur .idx
        try:
            subs.extend(inspect_subtitle(sp))
        except OSError as exc:
            warnings.append(f"Untertitel unlesbar: {sp.name} ({exc})")
    movie = Movie(folder=str(movie_dir.relative_to(root)), title=title, year=year,
                  sources=sources, subtitles=subs, extras=sorted(extras),
                  warnings=warnings + group_warnings)
    if not sources:
        movie.warnings.append("keine Filmdatei gefunden")
    elif len(sources) > 1:
        movie.warnings.append(f"{len(sources)} Versionen im Ordner")
    return movie


def discover(root: Path, only: Optional[str] = None) -> list[Movie]:
    """Wurzelordner in Filme zerlegen. Sammelordner ohne eigene Videos werden aufgelöst."""
    movies: list[Movie] = []
    loose: list[tuple[str, Path]] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            log.warning("Ordner unlesbar: %s (%s)", directory, exc)
            return
        subdirs = [e for e in entries if e.is_dir() and not is_skipped_dir(e.name)]
        direct_media = any(e.is_file() and e.suffix.lower() in VIDEO_EXT | {".iso", ".ifo"} for e in entries)
        structural = [d for d in subdirs if DIRPART_RE.match(d.name) or d.name.lower() == "video_ts"
                      or EXTRA_RE.search(d.name)]
        media_subdirs = [d for d in subdirs if has_media(d)]
        # Sammelordner, z. B. "Star Wars/Episode I/", "Star Wars/Episode II/"
        if directory != root and not direct_media and not structural and len(media_subdirs) >= 2:
            for d in media_subdirs:
                visit(d)
            return
        if directory == root:
            for e in entries:
                if e.is_file() and e.suffix.lower() in VIDEO_EXT:
                    loose.append(("file", e))
                elif e.is_file() and e.suffix.lower() == ".iso":
                    loose.append(("iso", e))
            for d in subdirs:
                visit(d)
            return
        movies.append(build_movie(root, directory))

    visit(root)
    for src in group_sources(loose)[0]:
        first = Path(src.parts[0])
        stem = split_part(first.stem)[0] if len(src.parts) > 1 else first.stem
        title, year = parse_title(stem)
        movies.append(Movie(folder=first.name, title=title, year=year, sources=[src]))

    if only:
        needle = only.lower()
        movies = [m for m in movies if needle in m.folder.lower() or needle in m.title.lower()]
    movies.sort(key=lambda m: m.folder.lower())
    return movies


# --------------------------------------------------------------------------- Scan

def probe_source(cache: ProbeCache, src: Source) -> None:
    src.size, src.probe, src.dvd_title, src.dvd_titles = 0, [], [], []
    try:
        for part in src.parts:
            p = Path(part)
            sig = dir_signature(p) if src.kind == "dvd" else file_signature(p)
            src.size += sig[0]
            if src.kind in ("dvd", "iso"):
                title, titles, probe = probe_dvd(cache, src.kind, p)
                src.dvd_title.append(title)
                src.dvd_titles.append(titles)
                src.probe.append(probe)
                continue
            # MPEG-PS/VOB: DVD-Untertitel tauchen oft erst spät im Stream auf
            pre = ("-analyzeduration", "200M", "-probesize", "200M") \
                if p.suffix.lower() in {".vob", ".mpg", ".mpeg", ".ts"} else ()
            src.probe.append(probe_cached(cache, p, sig, pre))
    except (ProbeError, OSError) as exc:
        src.error = str(exc)


def split_standalone_parts(movie: Movie) -> None:
    """'Teil 1' + 'Teil 2', die beide Spielfilmlänge haben, sind zwei Filme, keine Split-Teile."""
    new_sources = []
    for src in movie.sources:
        durations = [p.get("duration") or 0 for p in src.probe]
        if (src.kind == "file" and len(src.parts) > 1 and len(durations) == len(src.parts)
                and min(durations) >= MULTIPART_MIN_STANDALONE):
            movie.warnings.append("Teile haben je Spielfilmlänge, als getrennte Filme behandelt: "
                                  + ", ".join(Path(p).name for p in src.parts))
            for part, probe in zip(src.parts, src.probe):
                new_sources.append(Source(kind="file", parts=[part], size=Path(part).stat().st_size,
                                          probe=[probe]))
        else:
            new_sources.append(src)
    movie.sources = new_sources


def dvd_warnings(movie: Movie) -> None:
    for src in movie.sources:
        for part, titles, chosen in zip(src.parts, src.dvd_titles, src.dvd_title):
            durs = sorted(titles.values(), reverse=True)
            if len(durs) > 1 and durs[0] > 0 and durs[1] >= durs[0] * 0.97:
                movie.warnings.append(f"{Path(part).name}: mehrere fast gleich lange DVD-Titel "
                                      f"(Kopierschutz?), gewählt: Titel {chosen}")


def setup_logging(state_dir: Path, verbose: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    ch = RichHandler(console=console, show_path=False, show_time=False)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(state_dir / "filmarchiv.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(ch)
    log.addHandler(fh)


def load_inventory(state_dir: Path) -> tuple[Path, list[Movie]]:
    path = state_dir / "inventory.json"
    if not path.exists():
        console.print(f"[red]Kein Inventar in {state_dir}.[/] Erst `scan` ausführen.")
        raise typer.Exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    return Path(data["root"]), [Movie.from_dict(m) for m in data["movies"]]


app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Filmarchiv scannen, Untertitel prüfen, Duplikate finden, nach MKV konvertieren.")



@app.callback()
def main() -> None:
    """Subcommands: scan, report, dupes, convert."""


StateDirOpt = typer.Option(DEFAULT_STATE_DIR, "--state-dir", help="Cache, Inventar und Log")


@app.command()
def scan(
    path: Path = typer.Argument(..., exists=True, file_okay=False, resolve_path=True,
                                help="Filmordner, z. B. /mnt/nas/public/media/Filme"),
    workers: int = typer.Option(2, "--workers", "-w", min=1, help="parallele ffprobe-Aufrufe"),
    only: Optional[str] = typer.Option(None, "--only", help="nur Filme, deren Ordner/Titel das enthält"),
    min_size: int = typer.Option(30, "--min-size", min=0, help="kleinere Videos (MB) gelten als Sample/Extra"),
    state_dir: Path = StateDirOpt,
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Filmordner inventarisieren: Teile gruppieren, ffprobe, externe Untertitel."""
    global EXTRA_MAX_SIZE
    EXTRA_MAX_SIZE = min_size * 1024 * 1024
    check_tools("ffprobe")
    setup_logging(state_dir, verbose)
    log.info("Scanne Ordnerstruktur in %s …", path)
    movies = discover(path, only)
    sources = [(m, s) for m in movies for s in m.sources]
    log.info("%d Filme, %d Quellen gefunden", len(movies), len(sources))

    cache = ProbeCache(state_dir / "probe-cache.json")
    try:
        with Progress(TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(),
                      TimeElapsedColumn(), TextColumn("[dim]{task.fields[current]}"),
                      console=console) as progress:
            task = progress.add_task("ffprobe", total=len(sources), current="")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(probe_source, cache, s): m for m, s in sources}
                for fut in as_completed(futures):
                    movie = futures[fut]
                    fut.result()
                    progress.update(task, advance=1, current=movie.folder[:60])
    finally:
        cache.save()

    for movie in movies:
        split_standalone_parts(movie)
        dvd_warnings(movie)
        for src in movie.sources:
            if src.error:
                movie.warnings.append(f"ffprobe-Fehler bei {Path(src.parts[0]).name}: {src.error}")

    inv_path = state_dir / "inventory.json"
    if only and inv_path.exists():
        # Teilscan: nur die betroffenen Filme im bestehenden Inventar ersetzen
        old = json.loads(inv_path.read_text(encoding="utf-8"))
        if old.get("root") == str(path):
            fresh = {m.folder for m in movies}
            merged = [Movie.from_dict(m) for m in old["movies"] if m["folder"] not in fresh] + movies
            movies = sorted(merged, key=lambda m: m.folder.lower())
    atomic_write_json(inv_path, {"root": str(path), "movies": [asdict(m) for m in movies]})
    print_scan_summary(movies)
    log.info("Inventar geschrieben: %s", inv_path)


def print_scan_summary(movies: list[Movie]) -> None:
    kinds: dict[str, int] = defaultdict(int)
    multipart = errors = 0
    for m in movies:
        for s in m.sources:
            kinds[s.kind] += 1
            multipart += len(s.parts) > 1
            errors += bool(s.error)
    table = Table(title="Scan-Ergebnis", show_header=False)
    table.add_row("Filme", str(len(movies)))
    for kind, label in (("file", "Videodateien"), ("dvd", "DVD-Ordner"), ("iso", "ISO-Images")):
        table.add_row(label, str(kinds.get(kind, 0)))
    table.add_row("davon aufgeteilt", str(multipart))
    table.add_row("Probe-Fehler", str(errors))
    table.add_row("Filme mit Hinweisen", str(sum(bool(m.warnings) for m in movies)))
    console.print(table)
    warned = [m for m in movies if m.warnings]
    if warned:
        wt = Table(title="Hinweise", show_lines=False)
        wt.add_column("Ordner", overflow="fold")
        wt.add_column("Hinweis", overflow="fold")
        for m in warned:
            for w in m.warnings:
                wt.add_row(m.folder, w)
        console.print(wt)


if __name__ == "__main__":
    app()
