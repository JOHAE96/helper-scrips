#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "httpx>=0.27", "typer>=0.12", "rich>=13"]
# ///
"""Tests für untertitel.py mit simulierter opensubtitles.com-API. Aufruf: ./test_untertitel.py"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

_spec = importlib.util.spec_from_file_location("untertitel", Path(__file__).with_name("untertitel.py"))
ut = importlib.util.module_from_spec(_spec)
sys.modules["untertitel"] = ut
_spec.loader.exec_module(ut)


def video(root: Path, rel: str, size: int = 200_000) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    return p


def touch(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def result(lang: str, file_id: int, release: str = "", hash_match: bool = False, downloads: int = 10,
           hi: bool = False, season=None, episode=None, year=None, files: int = 1) -> dict:
    return {"attributes": {
        "language": lang, "release": release, "moviehash_match": hash_match, "download_count": downloads,
        "hearing_impaired": hi, "from_trusted": False, "foreign_parts_only": False,
        "feature_details": {"season_number": season, "episode_number": episode, "year": year},
        "files": [{"file_id": file_id + i} for i in range(files)],
    }}


# ------------------------------------------------------------------ Hash

def test_opensubtitles_hash_matches_reference(tmp_path):
    p = video(tmp_path, "a.bin", 300_000)
    data = p.read_bytes()
    ref = len(data)
    for buf in (data[:65536], data[-65536:]):
        ref += sum(int.from_bytes(buf[i:i + 8], "little") for i in range(0, 65536, 8))
    assert ut.opensubtitles_hash(p) == f"{ref % 2**64:016x}"


def test_hash_small_file_is_none(tmp_path):
    assert ut.opensubtitles_hash(video(tmp_path, "a.bin", 1000)) is None


# ------------------------------------------------------------------ Erkennung

def test_movie_from_folder(tmp_path):
    v = ut.classify(video(tmp_path, "Der Pate (1972)/Der Pate (1972).mkv"), tmp_path)
    assert (v.kind, v.title, v.year) == ("movie", "Der Pate", 1972)


def test_movie_loose_in_root(tmp_path):
    v = ut.classify(video(tmp_path, "Alien.1979.German.DVDRip.mkv"), tmp_path)
    assert (v.title, v.year) == ("Alien", 1979)


@pytest.mark.parametrize("rel, show, s, e", [
    ("Breaking Bad (2008)/Season 01/Breaking Bad S01E02.mkv", "Breaking Bad", 1, 2),
    ("Tatort/Staffel 3/tatort.s03e10.german.mkv", "Tatort", 3, 10),
    ("Dark/Dark - 2x05 - Title.mkv", "Dark", 2, 5),
    ("The.Office.S02E01.720p.mkv", "The Office", 2, 1),
])
def test_episode_detection(tmp_path, rel, show, s, e):
    v = ut.classify(video(tmp_path, rel), tmp_path)
    assert (v.kind, v.title, v.season, v.episode) == ("episode", show, s, e)


def test_movie_in_cd_subfolder(tmp_path):
    v = ut.classify(video(tmp_path, "Matrix.1999.German.DVDRip/CD1/matrix-cd1.avi"), tmp_path)
    assert (v.title, v.year) == ("Matrix", 1999)


def test_imdb_from_nfo(tmp_path):
    touch(tmp_path, "Film/movie.nfo", "<movie><uniqueid type=\"imdb\" default=\"true\">tt0068646</uniqueid></movie>")
    assert ut.classify(video(tmp_path, "Film/film.mkv"), tmp_path).imdb_id == 68646


def test_series_imdb_from_tvshow_nfo(tmp_path):
    touch(tmp_path, "Show/tvshow.nfo", "<tvshow><imdb_id>tt0903747</imdb_id></tvshow>")
    v = ut.classify(video(tmp_path, "Show/Season 1/Show S01E01.mkv"), tmp_path)
    assert v.parent_imdb_id == 903747 and v.imdb_id is None


def test_extras_and_small_files_skipped(tmp_path):
    video(tmp_path, "Film/film.mkv", 2_000_000)
    video(tmp_path, "Film/film-trailer.mkv", 2_000_000)
    video(tmp_path, "Film/Extras/making of.mkv", 2_000_000)
    video(tmp_path, "Film/klein.mkv", 1000)
    assert [p.name for p in ut.find_videos(tmp_path, None, 1_000_000)] == ["film.mkv"]


# ------------------------------------------------------------------ Vorhandene Untertitel

def test_external_subs_jellyfin_naming(tmp_path):
    v = video(tmp_path, "Film/Film.mkv")
    touch(tmp_path, "Film/Film.de.srt")
    touch(tmp_path, "Film/Film.en.forced.srt")      # forced zählt nicht
    touch(tmp_path, "Film/Film.eng.sdh.srt")
    touch(tmp_path, "Film/Anderer Film.fr.srt")     # gehört nicht zu diesem Video
    assert ut.external_sub_langs(v) == {"de", "en"}


def test_external_sub_without_lang_uses_content(tmp_path):
    v = video(tmp_path, "Film/Film.mkv")
    touch(tmp_path, "Film/Film.srt", "1\n00:00:01,000 --> 00:00:02,000\nIch weiß nicht, was du hier machst. "
          "Das ist nicht gut, wir haben jetzt keine Zeit und du kannst auch nicht mit mir reden.\n" * 5)
    assert ut.external_sub_langs(v) == {"de"}


def test_idx_languages(tmp_path):
    v = video(tmp_path, "Film/Film.mkv")
    touch(tmp_path, "Film/Film.idx", "id: de, index: 0\nid: en, index: 1\n")
    touch(tmp_path, "Film/Film.sub")
    assert ut.external_sub_langs(v) == {"de", "en"}


def test_embedded_probe_cached_and_bitmap_option(tmp_path, monkeypatch):
    v = video(tmp_path, "Film/Film.mkv")
    calls = []
    monkeypatch.setattr(ut, "embedded_sub_langs", lambda p, ib: calls.append(p) or ({"en"}, {"de"}))
    state = ut.State(tmp_path / "state.json")
    assert state.embedded(v, ignore_bitmap=False) == {"de", "en"}
    assert state.embedded(v, ignore_bitmap=True) == {"en"}
    assert len(calls) == 1


def test_state_retry(tmp_path):
    state = ut.State(tmp_path / "s.json")
    p = tmp_path / "x.mkv"
    state.mark(p, "de", "notfound")
    state.save()
    state = ut.State(tmp_path / "s.json")
    assert state.recently_tried(p, "de", 7)
    assert not state.recently_tried(p, "de", 0)
    assert not state.recently_tried(p, "en", 7)


# ------------------------------------------------------------------ Suche & Auswahl

def test_search_params_prefer_ids(tmp_path):
    ep = ut.Video(tmp_path / "x.mkv", "episode", "Show", season=1, episode=2, parent_imdb_id=903747)
    p = ut.search_params(ep, ["en", "de"], "abc", False)
    assert p["parent_imdb_id"] == 903747 and "query" not in p and p["languages"] == "de,en"
    mv = ut.Video(tmp_path / "x.mkv", "movie", "Der Pate", 1972)
    p = ut.search_params(mv, ["de"], None, False)
    assert (p["query"], p["year"], p["type"], p["ai_translated"]) == ("Der Pate", 1972, "movie", "exclude")


def test_pick_best_prefers_hash_then_release(tmp_path):
    v = ut.Video(tmp_path / "Film.2000.German.DVDRip.XviD-GRP.avi", "movie", "Film", 2000)
    results = [result("de", 1, "Film.2000.BluRay.x264", downloads=90000),
               result("de", 2, "Film.2000.German.DVDRip.XviD-GRP", downloads=100),
               result("en", 3, "whatever", hash_match=True)]
    assert ut.pick_best(results, v, "de", False, False)["attributes"]["files"][0]["file_id"] == 2
    results.append(result("de", 4, "other", hash_match=True))
    assert ut.pick_best(results, v, "de", False, False)["attributes"]["files"][0]["file_id"] == 4
    assert ut.pick_best(results[:2], v, "de", hash_only=True, allow_hi=False) is None


def test_pick_best_filters_wrong_episode_and_year(tmp_path):
    ep = ut.Video(tmp_path / "x.mkv", "episode", "Show", season=1, episode=2)
    assert ut.pick_best([result("de", 1, season=1, episode=3)], ep, "de", False, False) is None
    mv = ut.Video(tmp_path / "x.mkv", "movie", "King Kong", 1933)
    assert ut.pick_best([result("de", 1, year=2005)], mv, "de", False, False) is None


def test_pick_best_avoids_hi_and_multi_cd(tmp_path):
    v = ut.Video(tmp_path / "x.mkv", "movie", "F")
    results = [result("de", 1, hi=True, downloads=5000), result("de", 10, files=2, downloads=5000),
               result("de", 20, downloads=10)]
    assert ut.pick_best(results, v, "de", False, False)["attributes"]["files"][0]["file_id"] == 20


def test_to_utf8():
    assert ut.to_utf8("Größe".encode("cp1252")) == "Größe".encode()
    assert ut.to_utf8(b"\xef\xbb\xbfabc") == b"abc"


# ------------------------------------------------------------------ Ende-zu-Ende mit Fake-API

class FakeApi:
    def __init__(self, results: list[dict], remaining: int = 20):
        self.results, self.remaining = results, remaining
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        assert request.headers["Api-Key"] == "KEY"
        if path.endswith("/login"):
            return httpx.Response(200, json={"token": "TOK", "base_url": "api.opensubtitles.com"})
        if path.endswith("/infos/user"):
            return httpx.Response(200, json={"data": {"remaining_downloads": self.remaining}})
        if path.endswith("/subtitles"):
            return httpx.Response(200, json={"data": self.results})
        if path.endswith("/download"):
            assert request.headers["Authorization"] == "Bearer TOK"
            if self.remaining == 0:
                return httpx.Response(406, json={"message": "You have downloaded your allowed 20 subtitles"})
            self.remaining -= 1
            fid = json.loads(request.content)["file_id"]
            return httpx.Response(200, json={"link": f"https://dl.example/{fid}.srt", "remaining": self.remaining})
        if request.url.host == "dl.example":
            return httpx.Response(200, content="1\n00:00:01,000 --> 00:00:02,000\nGrüße\n".encode("cp1252"))
        return httpx.Response(404)

    def calls(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "KEY")
    monkeypatch.setenv("OPENSUBTITLES_USERNAME", "user")
    monkeypatch.setenv("OPENSUBTITLES_PASSWORD", "pw")
    monkeypatch.setattr(ut, "CONFIG_PATH", tmp_path / "nope.toml")
    monkeypatch.setattr(ut.time, "sleep", lambda s: None)
    lib = tmp_path / "lib"
    video(lib, "Der Pate (1972)/Der Pate (1972).mkv")
    touch(lib, "Der Pate (1972)/Der Pate (1972).en.srt")
    return lib, tmp_path / "state"


def run(lib: Path, state: Path, *args: str):
    res = CliRunner().invoke(ut.app, ["fetch", str(lib), "--no-probe", "--min-size", "0",
                                      "--state-dir", str(state), *args])
    assert res.exit_code == 0, res.output + repr(res.exception)
    return res


def test_fetch_downloads_only_missing_language(env, monkeypatch):
    lib, state = env
    api = FakeApi([result("de", 42, "Der.Pate.1972"), result("en", 7)])
    monkeypatch.setattr(ut, "TRANSPORT", httpx.MockTransport(api))
    run(lib, state)
    target = lib / "Der Pate (1972)/Der Pate (1972).de.srt"
    assert target.read_text(encoding="utf-8").endswith("Grüße\n")
    assert len(api.calls("/download")) == 1
    search = api.calls("/subtitles")[0].url.params
    assert search["languages"] == "de" and search["query"] == "der pate" and search["year"] == "1972"
    assert "moviehash" in search
    # zweiter Lauf: nichts mehr zu tun, keine API-Anfragen
    api.requests.clear()
    run(lib, state)
    assert api.requests == []


def test_fetch_dry_run_writes_nothing(env, monkeypatch):
    lib, state = env
    api = FakeApi([result("de", 42)])
    monkeypatch.setattr(ut, "TRANSPORT", httpx.MockTransport(api))
    run(lib, state, "--dry-run")
    assert not (lib / "Der Pate (1972)/Der Pate (1972).de.srt").exists()
    assert api.calls("/download") == []


def test_fetch_not_found_is_remembered(env, monkeypatch):
    lib, state = env
    api = FakeApi([])
    monkeypatch.setattr(ut, "TRANSPORT", httpx.MockTransport(api))
    run(lib, state)
    run(lib, state)
    assert len(api.calls("/subtitles")) == 1  # zweiter Lauf sucht erst nach --retry-days wieder


def test_fetch_stops_when_quota_exhausted(env, monkeypatch):
    lib, state = env
    video(lib, "Heat (1995)/Heat (1995).mkv")
    api = FakeApi([result("de", 42)], remaining=0)
    monkeypatch.setattr(ut, "TRANSPORT", httpx.MockTransport(api))
    res = run(lib, state)
    assert len(api.calls("/download")) == 1 and "Stopp" in res.output
    assert not list(lib.rglob("*.de.srt"))


def test_fetch_respects_limit(env, monkeypatch):
    lib, state = env
    video(lib, "Heat (1995)/Heat (1995).mkv")
    api = FakeApi([result("de", 42), result("en", 43)])
    monkeypatch.setattr(ut, "TRANSPORT", httpx.MockTransport(api))
    run(lib, state, "--limit", "1")
    assert len(api.calls("/download")) == 1


def test_fetch_without_api_key_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    monkeypatch.setattr(ut, "CONFIG_PATH", tmp_path / "nope.toml")
    res = CliRunner().invoke(ut.app, ["fetch", str(tmp_path), "--state-dir", str(tmp_path / "s")])
    assert res.exit_code == 2 and "API-Key" in res.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
