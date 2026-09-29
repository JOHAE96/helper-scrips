#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8", "typer>=0.12", "rich>=13"]
# ///
"""Tests für filmarchiv.py. Aufruf: ./test_filmarchiv.py  (keine echten Videos nötig)."""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("filmarchiv", Path(__file__).with_name("filmarchiv.py"))
fa = importlib.util.module_from_spec(_spec)
sys.modules["filmarchiv"] = fa
_spec.loader.exec_module(fa)


@pytest.fixture(autouse=True)
def no_size_limit(monkeypatch):
    monkeypatch.setattr(fa, "EXTRA_MAX_SIZE", 0)


def touch(root: Path, *rels: str) -> None:
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def fake_probe(duration: float, audio=("ger",), subs=()) -> dict:
    streams = [{"index": 0, "codec_type": "video", "codec_name": "mpeg2video", "width": 720,
                "height": 576, "field_order": "tt"}]
    for lang in audio:
        streams.append({"index": len(streams), "codec_type": "audio", "codec_name": "ac3",
                        "channels": 6, "tags": {"language": lang}})
    for lang, codec, forced in subs:
        streams.append({"index": len(streams), "codec_type": "subtitle", "codec_name": codec,
                        "tags": {"language": lang}, "disposition": {"forced": int(forced)}})
    return {"format": {"format_name": "mpeg", "duration": str(duration), "size": "1000"},
            "streams": streams, "chapters": []}


# ------------------------------------------------------------------ Titel

@pytest.mark.parametrize("name, expected", [
    ("Der Pate (1972)", ("Der Pate", 1972)),
    ("Der.Pate.1972.German.DVDRip.XviD", ("Der Pate", 1972)),
    ("Matrix_1999_German", ("Matrix", 1999)),
    ("2001 - Odyssee im Weltraum (1968)", ("2001 - Odyssee im Weltraum", 1968)),
    ("Blade Runner 2049 (2017)", ("Blade Runner 2049", 2017)),
    ("Blade Runner 2049", ("Blade Runner 2049", None)),
    ("1917", ("1917", None)),
    ("Heat German DVDRip", ("Heat", None)),
    ("Uncut Gems", ("Uncut Gems", None)),
    ("Die Hard [1988]", ("Die Hard", 1988)),
])
def test_parse_title(name, expected):
    assert fa.parse_title(name) == expected


@pytest.mark.parametrize("name, part", [
    ("Film.CD1", 1), ("Film cd2 German", 2), ("Film - Part 1", 1), ("Film.part02", 2),
    ("Film Teil 2 von 2", 2), ("Film-1of2", 1), ("Film [Disc 2]", 2),
    ("Film", None), ("Scream", None), ("Abcd1", None), ("Jurassic Park", None),
])
def test_split_part(name, part):
    assert fa.split_part(name)[1] == part


def test_split_part_same_base_for_all_parts():
    assert fa.key_of(fa.split_part("Film.CD1.German")[0]) == fa.key_of(fa.split_part("Film.CD2.German")[0])


# ------------------------------------------------------------------ Gruppierung

def movies_by_folder(root: Path) -> dict[str, "fa.Movie"]:
    return {m.folder: m for m in fa.discover(root)}


def names(src) -> list[str]:
    return [Path(p).name for p in src.parts]


def test_cd_suffix_files_are_one_movie(tmp_path):
    touch(tmp_path, "Film (2000)/film.cd1.avi", "Film (2000)/film.cd2.avi")
    m = movies_by_folder(tmp_path)["Film (2000)"]
    assert len(m.sources) == 1
    assert names(m.sources[0]) == ["film.cd1.avi", "film.cd2.avi"]


def test_cd_subfolders_are_one_movie(tmp_path):
    touch(tmp_path, "Film/CD2/b.avi", "Film/CD1/a.avi")
    m = movies_by_folder(tmp_path)["Film"]
    assert len(m.sources) == 1
    assert names(m.sources[0]) == ["a.avi", "b.avi"]


def test_trailing_number_parts(tmp_path):
    touch(tmp_path, "Film/Film 1.avi", "Film/Film 2.avi")
    assert names(movies_by_folder(tmp_path)["Film"].sources[0]) == ["Film 1.avi", "Film 2.avi"]


def test_single_trailing_number_is_no_part(tmp_path):
    touch(tmp_path, "Rocky 2/Rocky 2.avi")
    m = movies_by_folder(tmp_path)["Rocky 2"]
    assert len(m.sources) == 1 and len(m.sources[0].parts) == 1


def test_different_formats_are_separate_versions(tmp_path):
    touch(tmp_path, "Film/film.cd1.avi", "Film/film.cd2.avi", "Film/film.mkv")
    m = movies_by_folder(tmp_path)["Film"]
    assert sorted(len(s.parts) for s in m.sources) == [1, 2]
    assert any("Versionen" in w for w in m.warnings)


def test_same_basename_different_extension_not_merged(tmp_path):
    touch(tmp_path, "Film/film.cd1.avi", "Film/film.cd2.avi", "Film/film.cd1.mkv", "Film/film.cd2.mkv")
    m = movies_by_folder(tmp_path)["Film"]
    assert len(m.sources) == 2 and all(len(s.parts) == 2 for s in m.sources)


def test_missing_part_warns(tmp_path):
    touch(tmp_path, "Film/film.cd1.avi", "Film/film.cd3.avi")
    assert any("unvollständig" in w for w in movies_by_folder(tmp_path)["Film"].warnings)


def test_video_ts_is_dvd_and_vobs_are_not_files(tmp_path):
    touch(tmp_path, "Film/VIDEO_TS/VIDEO_TS.IFO", "Film/VIDEO_TS/VTS_01_1.VOB", "Film/VIDEO_TS/VTS_01_2.VOB")
    m = movies_by_folder(tmp_path)["Film"]
    assert [(s.kind, Path(s.parts[0]).name) for s in m.sources] == [("dvd", "VIDEO_TS")]


def test_two_disc_dvd(tmp_path):
    touch(tmp_path, "Film/Disc 1/VIDEO_TS/VIDEO_TS.IFO", "Film/Disc 2/VIDEO_TS/VIDEO_TS.IFO")
    m = movies_by_folder(tmp_path)["Film"]
    assert len(m.sources) == 1 and m.sources[0].kind == "dvd" and len(m.sources[0].parts) == 2


def test_loose_vobs_grouped_menu_skipped(tmp_path):
    touch(tmp_path, "Film/VTS_01_0.VOB", "Film/VTS_01_1.VOB", "Film/VTS_01_2.VOB")
    assert names(movies_by_folder(tmp_path)["Film"].sources[0]) == ["VTS_01_1.VOB", "VTS_01_2.VOB"]


def test_iso(tmp_path):
    touch(tmp_path, "Film/film.iso")
    assert movies_by_folder(tmp_path)["Film"].sources[0].kind == "iso"


def test_extras_and_samples_ignored(tmp_path):
    touch(tmp_path, "Film/film.mkv", "Film/film-sample.mkv", "Film/Extras/making of.avi", "Film/trailer.mp4")
    m = movies_by_folder(tmp_path)["Film"]
    assert len(m.sources) == 1 and len(m.extras) == 3


def test_small_files_are_extras(tmp_path, monkeypatch):
    monkeypatch.setattr(fa, "EXTRA_MAX_SIZE", 10)
    touch(tmp_path, "Film/film.mkv")
    (tmp_path / "Film/big.mkv").write_bytes(b"x" * 100)
    m = movies_by_folder(tmp_path)["Film"]
    assert names(m.sources[0]) == ["big.mkv"]


def test_collection_folder_is_split(tmp_path):
    touch(tmp_path, "Star Wars/Episode IV (1977)/a.mkv", "Star Wars/Episode V (1980)/b.mkv")
    assert set(movies_by_folder(tmp_path)) == {"Star Wars/Episode IV (1977)", "Star Wars/Episode V (1980)"}


def test_nas_system_dirs_ignored(tmp_path):
    touch(tmp_path, "Film/film.mkv", "Film/@eaDir/film.mkv/thumb.mkv", "@eaDir/x.mkv", "#recycle/Alt/alt.mkv")
    movies = movies_by_folder(tmp_path)
    assert set(movies) == {"Film"} and len(movies["Film"].sources) == 1


def test_loose_files_in_root(tmp_path):
    touch(tmp_path, "Alien.1979.mkv", "Heat.cd1.avi", "Heat.cd2.avi")
    movies = {m.title: m for m in fa.discover(tmp_path)}
    assert movies["Alien"].year == 1979
    assert len(movies["Heat"].sources[0].parts) == 2


def test_empty_folder_warns(tmp_path):
    touch(tmp_path, "Leer/info.nfo")
    assert "keine Filmdatei gefunden" in movies_by_folder(tmp_path)["Leer"].warnings


def test_umlauts_and_special_chars(tmp_path):
    touch(tmp_path, "Die Brücke am Fluß (1995)/Die Brücke & das Café – Teil 1.mkv",
          "Die Brücke am Fluß (1995)/Die Brücke & das Café – Teil 2.mkv")
    m = movies_by_folder(tmp_path)["Die Brücke am Fluß (1995)"]
    assert m.title == "Die Brücke am Fluß" and len(m.sources[0].parts) == 2


def test_only_filter(tmp_path):
    touch(tmp_path, "Der Pate/a.mkv", "Heat/b.mkv")
    assert [m.folder for m in fa.discover(tmp_path, only="pate")] == ["Der Pate"]


# ------------------------------------------------------------------ Untertitel

@pytest.mark.parametrize("stem, expected", [
    ("Film.de", ("de", False)), ("Film.German.forced", ("de", True)), ("Film.eng", ("en", False)),
    ("It", (None, False)), ("It.en", ("en", False)), ("Film", (None, False)),
])
def test_lang_from_filename(stem, expected):
    assert fa.lang_from_filename(stem) == expected


def test_srt_language_from_content(tmp_path):
    de = tmp_path / "a.srt"
    de.write_text("1\n00:00:01,000 --> 00:00:02,000\nIch weiß nicht, was du hier machst. Das ist nicht gut, "
                  "wir haben jetzt keine Zeit und du kannst auch nicht mit mir reden.\n" * 5, encoding="cp1252")
    en = tmp_path / "b.srt"
    en.write_text("1\n00:00:01,000 --> 00:00:02,000\nI don't know what you are doing here, it is not the way "
                  "we can do this now and you have to go.\n" * 5)
    assert fa.inspect_subtitle(de)[0].lang == "de"
    assert fa.inspect_subtitle(en)[0].lang == "en"


def test_idx_lists_all_languages(tmp_path):
    idx = tmp_path / "film.idx"
    idx.write_text("# VobSub index file\nid: de, index: 0\ntimestamp: 00:00:01:000\nid: en, index: 1\n")
    subs = fa.inspect_subtitle(idx)
    assert [(s.lang, s.bitmap, s.format) for s in subs] == [("de", True, "vobsub"), ("en", True, "vobsub")]


def test_sub_next_to_idx_not_listed_twice(tmp_path):
    touch(tmp_path, "Film/film.mkv", "Film/film.sub")
    (tmp_path / "Film/film.idx").write_text("id: de, index: 0\n")
    m = movies_by_folder(tmp_path)["Film"]
    assert [s.format for s in m.subtitles] == ["vobsub"]


# ------------------------------------------------------------------ ffprobe (gemockt)

def test_summarize_probe_marks_bitmap_and_forced():
    s = fa.summarize_probe(fake_probe(5400, audio=("ger", "eng"),
                                      subs=[("ger", "dvd_subtitle", True), ("eng", "subrip", False)]))
    assert [a["lang"] for a in s["audio"]] == ["de", "en"]
    assert [(x["lang"], x["bitmap"], x["forced"]) for x in s["subs"]] == [("de", True, True), ("en", False, False)]
    assert s["video"]["field_order"] == "tt" and s["duration"] == 5400


def test_probe_uses_cache(tmp_path, monkeypatch):
    f = tmp_path / "film.mkv"
    f.write_bytes(b"x")
    calls = []
    monkeypatch.setattr(fa, "run_ffprobe", lambda path, pre=(): calls.append(path) or fake_probe(100))
    cache = fa.ProbeCache(tmp_path / "cache.json")
    for _ in range(2):
        src = fa.Source(kind="file", parts=[str(f)])
        fa.probe_source(cache, src)
        assert src.duration == 100
    assert len(calls) == 1
    cache.save()
    f.write_bytes(b"xx")  # Größe geändert -> neu proben
    fa.probe_source(fa.ProbeCache(tmp_path / "cache.json"), fa.Source(kind="file", parts=[str(f)]))
    assert len(calls) == 2


def test_probe_error_is_recorded(tmp_path, monkeypatch):
    f = tmp_path / "kaputt.avi"
    f.write_bytes(b"x")

    def boom(path, pre=()):
        raise fa.ProbeError("Invalid data")
    monkeypatch.setattr(fa, "run_ffprobe", boom)
    src = fa.Source(kind="file", parts=[str(f)])
    fa.probe_source(fa.ProbeCache(tmp_path / "c.json"), src)
    assert src.error == "Invalid data"


def test_standalone_parts_are_split(tmp_path):
    touch(tmp_path, "HP/HP Teil 1.mkv", "HP/HP Teil 2.mkv")
    m = movies_by_folder(tmp_path)["HP"]
    m.sources[0].probe = [fa.summarize_probe(fake_probe(146 * 60)), fa.summarize_probe(fake_probe(130 * 60))]
    fa.split_standalone_parts(m)
    assert len(m.sources) == 2 and any("getrennte Filme" in w for w in m.warnings)


def test_real_split_parts_stay_together(tmp_path):
    touch(tmp_path, "F/f.cd1.avi", "F/f.cd2.avi")
    m = movies_by_folder(tmp_path)["F"]
    m.sources[0].probe = [fa.summarize_probe(fake_probe(55 * 60)), fa.summarize_probe(fake_probe(50 * 60))]
    fa.split_standalone_parts(m)
    assert len(m.sources) == 1


# ------------------------------------------------------------------ DVD

def make_vmg_ifo(titles: int) -> bytes:
    data = bytearray(3 * 2048)
    data[:12] = b"DVDVIDEO-VMG"
    data[0xC4:0xC8] = struct.pack(">I", 1)       # TT_SRPT in Sektor 1
    data[2048:2050] = struct.pack(">H", titles)
    return bytes(data)


def test_dvd_title_count(tmp_path):
    (tmp_path / "video_ts.ifo").write_bytes(make_vmg_ifo(7))  # Kleinschreibung kommt vor
    assert fa.dvd_title_count("dvd", tmp_path) == 7


def test_dvd_picks_longest_title(tmp_path, monkeypatch):
    (tmp_path / "VIDEO_TS.IFO").write_bytes(make_vmg_ifo(3))
    durations = {"1": 120, "2": 6300, "3": 900}
    monkeypatch.setattr(fa, "run_ffprobe", lambda path, pre=(): fake_probe(durations[pre[-1]]))
    src = fa.Source(kind="dvd", parts=[str(tmp_path)])
    fa.probe_source(fa.ProbeCache(tmp_path / "c.json"), src)
    assert src.error is None and src.dvd_title == [2] and src.duration == 6300


def test_dvd_without_ifo_table_probes_until_failure(tmp_path, monkeypatch):
    def probe(path, pre=()):
        if int(pre[-1]) > 2:
            raise fa.ProbeError("no such title")
        return fake_probe(int(pre[-1]) * 1000)
    monkeypatch.setattr(fa, "run_ffprobe", probe)
    src = fa.Source(kind="dvd", parts=[str(tmp_path)])
    fa.probe_source(fa.ProbeCache(tmp_path / "c.json"), src)
    assert src.dvd_title == [2]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
