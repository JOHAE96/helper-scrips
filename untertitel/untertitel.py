#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "typer>=0.12", "rich>=13"]
# ///
"""Fehlende Untertitel für Filme und Serien von opensubtitles.com laden.

  ./untertitel.py missing /mnt/nas/public/media/Filme            # nur anzeigen, ohne API
  ./untertitel.py fetch   /mnt/nas/public/media/Filme --dry-run  # suchen, nichts laden
  ./untertitel.py fetch   /mnt/nas/public/media/Serien
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

console = Console(stderr=True)
log = logging.getLogger("untertitel")

APP_VERSION = "0.1"
API_URL = "https://api.opensubtitles.com/api/v1"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "untertitel" / "config.toml"
DEFAULT_STATE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "untertitel"
STATE_VERSION = 1
TRANSPORT: Optional[httpx.BaseTransport] = None  # Tests setzen hier eine Fake-API ein

VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".wmv", ".mov",
             ".divx", ".ogm", ".webm", ".flv"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt"}
BITMAP_SUB_CODECS = {"dvd_subtitle", "hdmv_pgs_subtitle", "dvb_subtitle", "xsub"}
SKIP_DIRS = {"@eadir", "#recycle", "@recycle", ".@__thumb", ".appledouble", "$recycle.bin",
             "system volume information", "lost+found", "video_ts", "audio_ts", "bdmv",
             # Jellyfin-Extras-Ordner
             "extras", "featurettes", "behind the scenes", "deleted scenes", "interviews",
             "scenes", "shorts", "trailers", "samples", "sample", "other", "backdrops"}
EXTRA_FILE_RE = re.compile(r"(?:^|[\s._-])(sample|trailer|featurette|behindthescenes|deleted|interview)"
                           r"(?:$|[\s._-])", re.I)
EPISODE_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[\s._-]?e(\d{1,3})(?!\d)|(?<![0-9])(\d{1,2})x(\d{2,3})(?!\d)", re.I)
PART_DIR_RE = re.compile(r"^(?:cd|dis[ck]|dvd|part|teil)[\s._-]*\d{1,2}$", re.I)
SEASON_DIR_RE = re.compile(r"^(?:season|staffel|s)[\s._-]*\d{1,2}$|^specials$", re.I)
YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d\d)(?![0-9])")
RELEASE_TAG_RE = re.compile(
    r"(?<![a-z0-9])(german|deutsch|dl|ac3|dts|dvd-?rip|dvdr|dvd|bd-?rip|br-?rip|blu-?ray|hdtv|"
    r"web-?dl|web-?rip|xvid|divx|x264|x265|h\.?264|h\.?265|hevc|480p|576p|720p|1080p|2160p|"
    r"pal|ntsc|remux|uncut|extended|unrated|proper|repack|multi)(?![a-z0-9])", re.I)
IMDB_NFO_RE = re.compile(r"<(?:imdbid|imdb_id|id)>\s*(tt\d{7,9})\s*<|<uniqueid[^>]*type=\"imdb\"[^>]*>\s*(tt\d{7,9})", re.I)

LANG_MAP = {
    "de": "de", "ger": "de", "deu": "de", "german": "de", "deutsch": "de",
    "en": "en", "eng": "en", "english": "en", "englisch": "en",
    "fr": "fr", "fre": "fr", "fra": "fr", "french": "fr",
    "es": "es", "spa": "es", "spanish": "es", "it": "it", "ita": "it", "italian": "it",
    "nl": "nl", "dut": "nl", "nld": "nl", "tr": "tr", "tur": "tr", "pl": "pl", "pol": "pl",
    "ru": "ru", "rus": "ru", "ja": "ja", "jpn": "ja", "da": "da", "dan": "da",
    "sv": "sv", "swe": "sv", "no": "no", "nor": "no", "fi": "fi", "fin": "fi",
    "pt": "pt", "por": "pt", "cs": "cs", "cze": "cs", "ces": "cs", "hu": "hu", "hun": "hu",
}
STOPWORDS = {
    "de": set("der die das und ist nicht ich du sie wir ein eine zu mit auf was wie ja nein "
              "mir dich mich hier jetzt sind haben wird kann schon noch auch".split()),
    "en": set("the and is not you we a to with on what how yes no it that i'm don't "
              "this here now are have will can just your".split()),
    "fr": set("le la les et est pas je tu vous nous un une que qui ce ça oui non".split()),
    "es": set("el la los y es no yo tú que un una por qué sí está".split()),
}


# --------------------------------------------------------------------------- Datenmodell

@dataclass
class Video:
    path: Path
    kind: str                           # movie | episode
    title: str
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb_id: Optional[int] = None       # Film oder Episode
    parent_imdb_id: Optional[int] = None  # Serie
    have: set[str] = field(default_factory=set)   # vorhandene vollständige Untertitelsprachen

    @property
    def label(self) -> str:
        if self.kind == "episode":
            return f"{self.title} S{self.season or 0:02d}E{self.episode or 0:02d}"
        return f"{self.title} ({self.year})" if self.year else self.title


# --------------------------------------------------------------------------- Hilfsfunktionen

def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIRS


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


def opensubtitles_hash(path: Path) -> Optional[str]:
    """OpenSubtitles-Hash: Dateigröße + Summe der 64-bit-Wörter der ersten und letzten 64 KiB."""
    size = path.stat().st_size
    chunk = 65536
    if size < chunk * 2:
        return None
    with open(path, "rb") as fh:
        head = fh.read(chunk)
        fh.seek(size - chunk)
        tail = fh.read(chunk)
    value = size
    for buf in (head, tail):
        value += sum(struct.unpack(f"<{chunk // 8}Q", buf))
    return f"{value & 0xFFFFFFFFFFFFFFFF:016x}"


def imdb_from_nfo(nfo: Path) -> Optional[int]:
    try:
        text = nfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = IMDB_NFO_RE.search(text) or re.search(r"(tt\d{7,9})", text)
    if not m:
        return None
    return int(next(g for g in m.groups() if g)[2:])


def read_text(path: Path, limit: int = 65536) -> str:
    raw = path.open("rb").read(limit)
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def detect_text_lang(text: str) -> Optional[str]:
    words = re.findall(r"[a-zäöüßéèàùâêîôûçñ']+", text.lower())
    scores = sorted(((sum(w in sw for w in words), lang) for lang, sw in STOPWORDS.items()), reverse=True)
    (best, lang), (second, _) = scores[0], scores[1]
    return lang if best >= 15 and best >= 1.5 * max(second, 1) else None


# --------------------------------------------------------------------------- Vorhandene Untertitel

def external_sub_langs(video: Path) -> set[str]:
    """Sprachen vollständiger externer Untertitel nach Jellyfin-Schema: <stem>.<lang>[.forced].srt."""
    langs: set[str] = set()
    stem = video.stem
    try:
        siblings = list(video.parent.iterdir())
    except OSError:
        return langs
    idx_stems = {p.stem for p in siblings if p.suffix.lower() == ".idx"}
    for sub in siblings:
        ext = sub.suffix.lower()
        if ext not in SUB_EXT or not sub.name.startswith(stem + ".") and sub.stem != stem:
            continue
        if ext == ".sub" and sub.stem in idx_stems:
            continue
        tokens = [t.lower() for t in sub.stem[len(stem):].split(".") if t]
        if any(t in ("forced", "foreign") for t in tokens):
            continue  # Forced-Untertitel ersetzen keine vollständigen
        tok_langs = [LANG_MAP[t] for t in tokens if t in LANG_MAP]
        if tok_langs:
            langs.update(tok_langs)
        elif ext == ".idx":
            langs.update(LANG_MAP.get(l, l) for l in re.findall(r"^id:\s*([a-z]{2,3})", read_text(sub, 1 << 20), re.M))
        elif ext in (".srt", ".ass", ".ssa", ".vtt", ".sub"):
            try:
                if lang := detect_text_lang(read_text(sub)):
                    langs.add(lang)
            except OSError:
                pass
    return langs


def embedded_sub_langs(path: Path, ignore_bitmap: bool) -> tuple[set[str], set[str]]:
    """(text-, bitmap-)Sprachen der eingebetteten, nicht erzwungenen Untertitel."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
           "-select_streams", "s", "-i", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-300:])
    text, bitmap = set(), set()
    for s in json.loads(res.stdout).get("streams", []):
        if s.get("disposition", {}).get("forced"):
            continue
        lang = LANG_MAP.get(str((s.get("tags") or {}).get("language", "")).lower())
        if lang:
            (bitmap if s.get("codec_name") in BITMAP_SUB_CODECS else text).add(lang)
    return text, bitmap


# --------------------------------------------------------------------------- Zustand

class State:
    """ffprobe-Ergebnisse und bisherige Suchversuche, damit Folgeläufe schnell sind und kein Kontingent verschwenden."""

    def __init__(self, path: Path):
        self.path = path
        self.probe: dict[str, dict] = {}
        self.tried: dict[str, dict] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") == STATE_VERSION:
                    self.probe, self.tried = data.get("probe", {}), data.get("tried", {})
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Zustand %s unlesbar (%s), starte leer", path, exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": STATE_VERSION, "probe": self.probe, "tried": self.tried}
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=1).encode())

    def embedded(self, path: Path, ignore_bitmap: bool) -> set[str]:
        st = path.stat()
        sig = [st.st_size, st.st_mtime_ns]
        entry = self.probe.get(str(path))
        if not entry or entry.get("sig") != sig:
            text, bitmap = embedded_sub_langs(path, ignore_bitmap)
            entry = {"sig": sig, "text": sorted(text), "bitmap": sorted(bitmap)}
            self.probe[str(path)] = entry
        return set(entry["text"]) | (set() if ignore_bitmap else set(entry["bitmap"]))

    def recently_tried(self, path: Path, lang: str, retry_days: int) -> bool:
        entry = self.tried.get(f"{path}|{lang}")
        if not entry:
            return False
        return datetime.fromisoformat(entry["at"]) > datetime.now() - timedelta(days=retry_days)

    def mark(self, path: Path, lang: str, result: str) -> None:
        self.tried[f"{path}|{lang}"] = {"at": datetime.now().isoformat(timespec="seconds"), "result": result}


# --------------------------------------------------------------------------- Bibliothek

def classify(path: Path, root: Path) -> Video:
    """Film oder Episode erkennen, Titel/Jahr/IMDb-ID aus Ordnern und .nfo holen."""
    m = EPISODE_RE.search(path.stem)
    if m:
        season, episode = (int(m[1]), int(m[2])) if m[1] else (int(m[3]), int(m[4]))
        show_dir = path.parent.parent if SEASON_DIR_RE.match(path.parent.name) else path.parent
        if show_dir == root or root not in show_dir.parents:
            name = re.sub(r"[\s._-]+$", "", path.stem[:m.start()]) or path.stem
            name = re.sub(r"[._]+", " ", name) if " " not in name else name
            show_dir = None
        else:
            name = show_dir.name
        title, year = parse_title(name)
        video = Video(path, "episode", title, year, season, episode)
        video.imdb_id = imdb_from_nfo(path.with_suffix(".nfo")) if path.with_suffix(".nfo").exists() else None
        if show_dir and (show_dir / "tvshow.nfo").exists():
            video.parent_imdb_id = imdb_from_nfo(show_dir / "tvshow.nfo")
        if video.imdb_id and video.imdb_id == video.parent_imdb_id:
            video.imdb_id = None  # Episoden-NFO enthielt nur die Serien-ID
        return video
    folder = path.parent
    if PART_DIR_RE.match(folder.name) and folder != root:
        folder = folder.parent  # Film/CD1/film.avi
    name = folder.name if folder != root else path.stem
    title, year = parse_title(name)
    video = Video(path, "movie", title, year)
    for nfo in (path.with_suffix(".nfo"), path.parent / "movie.nfo", folder / "movie.nfo"):
        if nfo.exists() and (imdb := imdb_from_nfo(nfo)):
            video.imdb_id = imdb
            break
    return video


def find_videos(root: Path, only: Optional[str], min_size: int) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(d))
        for fname in sorted(filenames):
            p = Path(dirpath) / fname
            if p.suffix.lower() not in VIDEO_EXT or fname.startswith(".") or EXTRA_FILE_RE.search(p.stem):
                continue
            if only and only.lower() not in str(p.relative_to(root)).lower():
                continue
            try:
                if p.stat().st_size < min_size:
                    continue
            except OSError:
                continue
            found.append(p)
    return found


def collect(root: Path, langs: list[str], state: State, only: Optional[str], min_size: int,
            ignore_bitmap: bool, probe: bool) -> list[tuple[Video, list[str]]]:
    """Alle Videos mit den Sprachen, die ihnen fehlen."""
    result = []
    paths = find_videos(root, only, min_size)
    with console.status("") as status:
        for i, path in enumerate(paths, 1):
            status.update(f"prüfe {i}/{len(paths)}: {path.name[:70]}")
            video = classify(path, root)
            video.have = external_sub_langs(path)
            if probe and not set(langs) <= video.have:
                try:
                    video.have |= state.embedded(path, ignore_bitmap)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    log.warning("ffprobe fehlgeschlagen für %s: %s", path.name, exc)
            if i % 50 == 0:
                state.save()
            missing = [l for l in langs if l not in video.have]
            if missing:
                result.append((video, missing))
    state.save()
    return result


# --------------------------------------------------------------------------- API

class QuotaExhausted(RuntimeError):
    pass


class ApiError(RuntimeError):
    pass


class OpenSubtitles:
    def __init__(self, api_key: str, user_agent: str, transport: Optional[httpx.BaseTransport] = None):
        self.client = httpx.Client(base_url=API_URL, timeout=30, follow_redirects=True, transport=transport,
                                   headers={"Api-Key": api_key, "User-Agent": user_agent,
                                            "Accept": "application/json"})
        self._last = 0.0
        self.remaining: Optional[int] = None

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(4):
            wait = 0.25 - (time.monotonic() - self._last)  # API-Limit: max. 5 Anfragen/Sekunde
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise ApiError(f"Netzwerkfehler: {exc}") from exc
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                delay = float(resp.headers.get("Retry-After") or 10 * (attempt + 1))
                log.info("API %s, warte %.0fs …", resp.status_code, delay)
                time.sleep(min(delay, 120))
                continue
            return resp
        raise ApiError(f"{method} {url}: {resp.status_code} nach mehreren Versuchen")

    def login(self, username: str, password: str) -> None:
        resp = self._request("POST", "/login", json={"username": username, "password": password})
        if resp.status_code in (400, 401):
            raise ApiError("Login fehlgeschlagen: Benutzername (nicht E-Mail) und Passwort prüfen")
        resp.raise_for_status()
        data = resp.json()
        self.client.headers["Authorization"] = f"Bearer {data['token']}"
        if base := data.get("base_url"):  # VIP-Nutzer bekommen einen eigenen Host
            self.client.base_url = f"https://{base}/api/v1"
        info = self._request("GET", "/infos/user")
        if info.is_success:
            self.remaining = info.json().get("data", {}).get("remaining_downloads")

    def search(self, params: dict[str, Any]) -> list[dict]:
        # Parameter sortiert und kleingeschrieben, sonst leitet die API um
        clean = {k: str(v).lower() for k, v in sorted(params.items()) if v not in (None, "")}
        resp = self._request("GET", "/subtitles", params=clean)
        if not resp.is_success:
            raise ApiError(f"Suche fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
        return resp.json().get("data", [])

    def download(self, file_id: int) -> bytes:
        resp = self._request("POST", "/download", json={"file_id": file_id, "sub_format": "srt"})
        if resp.status_code == 406:
            raise QuotaExhausted(resp.json().get("message", "Tageskontingent erschöpft"))
        if not resp.is_success:
            raise ApiError(f"Download fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self.remaining = data.get("remaining", self.remaining)
        link = data.get("link")
        if not link:
            raise ApiError("Download-Antwort ohne Link")
        file_resp = self._request("GET", link)
        file_resp.raise_for_status()
        return file_resp.content


def search_params(video: Video, langs: list[str], moviehash: Optional[str], allow_ai: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "languages": ",".join(sorted(langs)),
        "moviehash": moviehash,
        "machine_translated": "exclude",
        "ai_translated": "include" if allow_ai else "exclude",
        "foreign_parts_only": "exclude",
    }
    if video.kind == "episode":
        params["type"] = "episode"
        if video.parent_imdb_id:
            params.update(parent_imdb_id=video.parent_imdb_id, season_number=video.season,
                          episode_number=video.episode)
        elif video.imdb_id:
            params["imdb_id"] = video.imdb_id
        else:
            params.update(query=video.title, season_number=video.season, episode_number=video.episode)
    else:
        params["type"] = "movie"
        if video.imdb_id:
            params["imdb_id"] = video.imdb_id
        else:
            params.update(query=video.title, year=video.year)
    return params


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


def pick_best(results: list[dict], video: Video, lang: str, hash_only: bool, allow_hi: bool) -> Optional[dict]:
    """Bestes Ergebnis für eine Sprache: Hash-Treffer > passender Release-Name > Downloads."""
    file_tokens = _tokens(video.path.stem)
    best, best_score = None, -math.inf
    for item in results:
        a = item.get("attributes", {})
        if a.get("language") != lang or not a.get("files") or a.get("foreign_parts_only"):
            continue
        if hash_only and not a.get("moviehash_match"):
            continue
        feat = a.get("feature_details") or {}
        if video.kind == "episode" and feat.get("episode_number") is not None:
            if (feat.get("season_number"), feat.get("episode_number")) != (video.season, video.episode):
                continue
        if (video.kind == "movie" and not video.imdb_id and video.year and feat.get("year")
                and abs(int(feat["year"]) - video.year) > 1):
            continue
        score = (1000 if a.get("moviehash_match") else 0)
        score += 20 * len(file_tokens & _tokens(a.get("release") or ""))
        score += 30 if a.get("from_trusted") else 0
        score += 5 * math.log10(1 + (a.get("download_count") or 0))
        score -= 0 if allow_hi or not a.get("hearing_impaired") else 40
        if len(a["files"]) > 1:
            score -= 200  # mehrteiliger Untertitel (CD1/CD2) passt nicht zu einer Datei
        if score > best_score:
            best, best_score = item, score
    return best


def to_utf8(data: bytes) -> bytes:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc).encode("utf-8")
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1").encode("utf-8")


# --------------------------------------------------------------------------- CLI

def load_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        cfg = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key in ("api_key", "username", "password"):
        if env := os.environ.get(f"OPENSUBTITLES_{key.upper()}"):
            cfg[key] = env
    return cfg


def setup_logging(state_dir: Path, verbose: bool) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    ch = RichHandler(console=console, show_path=False, show_time=False)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(state_dir / "untertitel.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(ch)
    log.addHandler(fh)


def parse_langs(value: str) -> list[str]:
    langs = [LANG_MAP.get(l.strip().lower(), l.strip().lower()) for l in value.split(",") if l.strip()]
    if not langs:
        raise typer.BadParameter("mindestens eine Sprache, z. B. de,en")
    return langs


app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Fehlende Untertitel für Filme und Serien von opensubtitles.com laden.")


@app.callback()
def main() -> None:
    """Subcommands: missing, fetch."""


PathArg = typer.Argument(..., exists=True, file_okay=False, resolve_path=True, help="Film- oder Serienordner")
LangsOpt = typer.Option("de,en", "--langs", "-l", help="gewünschte Sprachen")
OnlyOpt = typer.Option(None, "--only", help="nur Pfade, die das enthalten")
MinSizeOpt = typer.Option(50, "--min-size", min=0, help="kleinere Videos (MB) ignorieren")
IgnoreBitmapOpt = typer.Option(False, "--ignore-bitmap",
                               help="eingebettete Bild-Untertitel (VobSub/PGS) nicht als vorhanden zählen")
NoProbeOpt = typer.Option(False, "--no-probe", help="eingebettete Untertitel nicht per ffprobe prüfen")
StateDirOpt = typer.Option(DEFAULT_STATE_DIR, "--state-dir")


@app.command()
def missing(path: Path = PathArg, langs: str = LangsOpt, only: Optional[str] = OnlyOpt,
            min_size: int = MinSizeOpt, ignore_bitmap: bool = IgnoreBitmapOpt, no_probe: bool = NoProbeOpt,
            state_dir: Path = StateDirOpt, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Nur auflisten, welche Untertitel fehlen (ohne API)."""
    if not no_probe and not shutil.which("ffprobe"):
        console.print("[red]ffprobe fehlt[/] (oder --no-probe verwenden)")
        raise typer.Exit(2)
    setup_logging(state_dir, verbose)
    todo = collect(path, parse_langs(langs), State(state_dir / "state.json"), only, min_size * 2**20,
                   ignore_bitmap, not no_probe)
    table = Table(title=f"Fehlende Untertitel ({len(todo)} Videos)")
    table.add_column("Video", overflow="fold")
    table.add_column("vorhanden")
    table.add_column("fehlt", style="red")
    table.add_column("IMDb")
    for video, miss in todo:
        imdb = video.imdb_id or video.parent_imdb_id
        table.add_row(str(video.path.relative_to(path)), ",".join(sorted(video.have)) or "-",
                      ",".join(miss), f"tt{imdb:07d}" if imdb else "-")
    console.print(table)


@app.command()
def fetch(path: Path = PathArg, langs: str = LangsOpt, only: Optional[str] = OnlyOpt,
          min_size: int = MinSizeOpt, ignore_bitmap: bool = IgnoreBitmapOpt, no_probe: bool = NoProbeOpt,
          dry_run: bool = typer.Option(False, "--dry-run", help="suchen, aber nichts laden/schreiben"),
          limit: Optional[int] = typer.Option(None, "--limit", min=1, help="max. Downloads in diesem Lauf"),
          hash_only: bool = typer.Option(False, "--hash-only",
                                         help="nur Untertitel, deren Hash exakt zur Datei passt (sicher synchron)"),
          hi: bool = typer.Option(False, "--hi", help="Hörgeschädigten-Untertitel (SDH) nicht abwerten"),
          allow_ai: bool = typer.Option(False, "--allow-ai", help="auch KI-übersetzte Untertitel"),
          retry_days: int = typer.Option(7, "--retry-days", help="nicht Gefundenes erst nach X Tagen erneut suchen"),
          state_dir: Path = StateDirOpt, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Fehlende Untertitel suchen und als <Video>.<sprache>.srt daneben speichern."""
    cfg = load_config()
    if not cfg.get("api_key"):
        console.print(f"[red]Kein API-Key.[/] In {CONFIG_PATH} `api_key = \"…\"` eintragen "
                      "oder OPENSUBTITLES_API_KEY setzen (opensubtitles.com → Profil → API consumers).")
        raise typer.Exit(2)
    if not no_probe and not shutil.which("ffprobe"):
        console.print("[red]ffprobe fehlt[/] (oder --no-probe verwenden)")
        raise typer.Exit(2)
    setup_logging(state_dir, verbose)
    wanted = parse_langs(langs)
    state = State(state_dir / "state.json")
    todo = collect(path, wanted, state, only, min_size * 2**20, ignore_bitmap, not no_probe)
    log.info("%d Videos mit fehlenden Untertiteln", len(todo))
    if not todo:
        return

    api = OpenSubtitles(cfg["api_key"], cfg.get("user_agent", f"untertitel v{APP_VERSION}"), TRANSPORT)
    if cfg.get("username") and cfg.get("password"):
        api.login(cfg["username"], cfg["password"])
        log.info("Angemeldet, verbleibende Downloads heute: %s", api.remaining)
    else:
        log.warning("Ohne Login sind nur ca. 5 Downloads pro Tag möglich (username/password in %s)", CONFIG_PATH)

    stats = {"geladen": 0, "nicht gefunden": 0, "übersprungen": 0, "Fehler": 0}
    downloads = 0
    try:
        for video, miss in todo:
            miss = [l for l in miss if not state.recently_tried(video.path, l, retry_days)]
            if not miss:
                stats["übersprungen"] += 1
                continue
            try:
                results = api.search(search_params(video, miss, opensubtitles_hash(video.path), allow_ai))
            except (ApiError, OSError) as exc:
                log.error("%s: %s", video.label, exc)
                stats["Fehler"] += 1
                continue
            for lang in miss:
                best = pick_best(results, video, lang, hash_only, hi)
                if not best:
                    log.info("[yellow]–[/] %s [%s]: nichts gefunden", video.label, lang, extra={"markup": True})
                    stats["nicht gefunden"] += 1
                    if not dry_run:
                        state.mark(video.path, lang, "notfound")
                    continue
                a = best["attributes"]
                target = video.path.with_name(f"{video.path.stem}.{lang}.srt")
                sync = "Hash" if a.get("moviehash_match") else "Name"
                if dry_run:
                    log.info("[cyan]→[/] %s [%s] %s (%s)", video.label, lang, a.get("release"), sync,
                             extra={"markup": True})
                    continue
                if limit is not None and downloads >= limit:
                    raise QuotaExhausted(f"--limit {limit} erreicht")
                data = to_utf8(api.download(a["files"][0]["file_id"]))
                downloads += 1
                try:
                    atomic_write(target, data)
                except OSError as exc:
                    log.error("Kann %s nicht schreiben: %s (Freigabe read-only gemountet?)", target, exc)
                    stats["Fehler"] += 1
                    continue
                state.mark(video.path, lang, "downloaded")
                stats["geladen"] += 1
                log.info("[green]✓[/] %s [%s] %s (%s)", video.label, lang, a.get("release"), sync,
                         extra={"markup": True})
    except QuotaExhausted as exc:
        log.warning("Stopp: %s", exc)
    finally:
        state.save()
    table = Table(title="Ergebnis", show_header=False)
    for k, v in stats.items():
        table.add_row(k, str(v))
    if api.remaining is not None:
        table.add_row("Downloads übrig heute", str(api.remaining))
    console.print(table)


if __name__ == "__main__":
    app()
