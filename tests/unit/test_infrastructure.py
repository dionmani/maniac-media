import hashlib
import os
import time
from pathlib import Path

import pytest

from backend.core.ratelimit import KeyedLimiter
from backend.domain.quality import QualityProfileSpec
from backend.domain.types import ReleaseInfo
from backend.plugins import registry
from backend.plugins.base import PluginKind
from backend.plugins.download_clients._common import magnet_info_hash, map_remote_path, torrent_info_hash
from backend.plugins.indexers.newznab import parse_feed
from backend.plugins.base import PluginError
from backend.services import fileops
from backend.services.decision_engine import Target, evaluate, rank

TORZNAB = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.H.265-GRP</title><guid>abc</guid>
<link>http://idx/dl/1?apikey=K</link><size>12884901888</size><pubDate>Mon, 01 Apr 2024 10:00:00 +0000</pubDate>
<torznab:attr name="seeders" value="120"/><torznab:attr name="peers" value="150"/><torznab:attr name="imdbid" value="15239678"/>
<torznab:attr name="category" value="2045"/><torznab:attr name="infohash" value="deadbeef"/></item>
<item><title>Dune.Part.Two.2024.720p.HDTV.x264-GRP</title><guid>def</guid><link>http://idx/dl/2</link>
<torznab:attr name="seeders" value="0"/><torznab:attr name="peers" value="0"/></item>
</channel></rss>"""


def test_plugins_are_discovered_dynamically():
    keys = {k: set(v) for k, v in ((kind, registry.available(kind)) for kind in PluginKind)}
    assert {"tmdb", "tvdb", "tvmaze", "imdb"} <= keys[PluginKind.METADATA]
    assert {"torznab", "newznab", "rss"} <= keys[PluginKind.INDEXER]
    assert {"qbittorrent", "transmission", "deluge", "sabnzbd", "nzbget"} <= keys[PluginKind.DOWNLOAD_CLIENT]
    assert {"email", "discord", "telegram", "webhook"} <= keys[PluginKind.NOTIFICATION]


def test_external_plugin_directory(tmp_path, monkeypatch):
    from backend.core import paths

    (tmp_path / "my_notifier.py").write_text(
        "from backend.plugins.base import NotificationPlugin\n"
        "from backend.plugins.registry import register\n"
        "@register\nclass Pigeon(NotificationPlugin):\n    key='pigeon'\n    name='Pigeon'\n"
        "    async def send(self, message):\n        pass\n"
    )
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')")
    monkeypatch.setattr(paths, "PLUGIN_DIR", tmp_path)
    registry.discover(force=True)
    assert "pigeon" in registry.available(PluginKind.NOTIFICATION)
    assert not registry.get(PluginKind.NOTIFICATION, "pigeon").builtin
    assert any("broken" in e["source"] for e in registry.catalogue()["errors"])


def test_torznab_feed_parsing():
    releases = parse_feed(TORZNAB, "Idx", "torrent", 1)
    assert len(releases) == 2
    r = releases[0]
    assert r.size_bytes == 12884901888 and r.seeders == 120 and r.leechers == 30
    assert r.imdb_id == "tt15239678" and r.info_hash == "deadbeef" and r.categories == [2045]


def test_indexer_error_response():
    with pytest.raises(PluginError, match="Incorrect"):
        parse_feed('<error code="100" description="Incorrect user credentials"/>', "Idx", "usenet")


def test_torrent_hashes():
    info = b"d6:lengthi5e4:name3:bare"
    torrent = b"d8:announce3:foo4:info" + info + b"e"
    assert torrent_info_hash(torrent) == hashlib.sha1(info).hexdigest()
    assert magnet_info_hash("magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01") == "abcdef0123456789abcdef0123456789abcdef01"


def test_remote_path_mapping():
    settings = {"remote_path": "/downloads", "local_path": r"M:\downloads"}
    assert map_remote_path("/downloads/complete/Movie", settings) == r"M:\downloads\complete\Movie"
    assert map_remote_path("/other/x", settings) == "/other/x"


def test_decision_engine_filters_and_ranks():
    releases = parse_feed(TORZNAB, "Idx", "torrent", 1)
    profile = QualityProfileSpec("Any", ["WEBDL-2160p", "WEBDL-1080p", "HDTV-720p"], cutoff="WEBDL-2160p")
    target = Target("movie", 1, "Dune: Part Two", 2024, profile, minimum_seeders={1: 1})
    decisions = rank([evaluate(r, target) for r in releases])
    assert decisions[0].approved and decisions[0].quality.quality.name == "WEBDL-2160p"
    assert not decisions[1].approved and any("seeders" in x for x in decisions[1].rejections)


def test_decision_rejects_wrong_title_and_blocklist():
    profile = QualityProfileSpec("Any", ["WEBDL-1080p"])
    target = Target("movie", 1, "Arrival", 2016, profile)
    wrong = ReleaseInfo("g", "Departure.2016.1080p.WEB-DL", "u", "i", "usenet")
    blocked = ReleaseInfo("h", "Arrival.2016.1080p.WEB-DL", "u", "i", "usenet")
    assert not evaluate(wrong, target).approved
    assert "Release is blocklisted" in evaluate(blocked, target, {"arrival.2016.1080p.web-dl"}).rejections


def test_rate_limiter():
    limiter = KeyedLimiter()
    results = [limiter.hit("ip", 5)[0] for _ in range(7)]
    assert results.count(True) == 5 and results[-1] is False


# ------------------------------------------------------------------ filesystem
def test_video_validation(tmp_path, make_video):
    good = make_video(tmp_path / "a.mkv")
    bad = make_video(tmp_path / "b.mkv", b"not a matroska file at all")
    empty = make_video(tmp_path / "c.mp4", b"")
    part = make_video(tmp_path / "d.mkv.part", b"x")
    assert fileops.check_video_file(good).ok
    assert fileops.check_video_file(bad).state == "corrupt"
    assert fileops.check_video_file(empty).state == "corrupt"
    assert fileops.check_video_file(part).state == "partial"


def test_transfer_modes(tmp_path, make_video):
    src = make_video(tmp_path / "src" / "movie.mkv")
    assert fileops.transfer(src, tmp_path / "copy" / "movie.mkv", "copy") == "copy"
    assert src.exists()
    used = fileops.transfer(src, tmp_path / "link" / "movie.mkv", "hardlink")
    assert used in ("hardlink", "copy") and src.exists()
    fileops.transfer(src, tmp_path / "moved" / "movie.mkv", "move")
    assert not src.exists() and (tmp_path / "moved" / "movie.mkv").exists()
    with pytest.raises(FileExistsError):
        fileops.transfer(tmp_path / "copy" / "movie.mkv", tmp_path / "moved" / "movie.mkv", "copy")


def test_file_transaction_rolls_back(tmp_path, make_video):
    src = make_video(tmp_path / "dl" / "new.mkv")
    existing = make_video(tmp_path / "lib" / "Movie (2000)" / "old.mkv")
    tx = fileops.FileTransaction()
    tx.recycle(existing, tmp_path / "recycle")
    tx.place(src, tmp_path / "lib" / "Movie (2000)" / "Movie (2000).mkv", "move")
    assert not src.exists() and not existing.exists()
    assert tx.rollback() == []
    assert src.exists() and existing.exists()
    assert not (tmp_path / "lib" / "Movie (2000)" / "Movie (2000).mkv").exists()


def test_sabnzbd_reports_only_the_active_job_as_downloading(monkeypatch):
    import asyncio

    from backend.plugins.download_clients.sabnzbd import SabnzbdClient

    responses = {
        "queue": {"queue": {"kbpersec": "2048.00", "slots": [
            # SABnzbd calls every unpaused job "Downloading" and gives waiting ones a 0:00:00 timeleft
            {"nzo_id": "SABnzbd_nzo_a", "filename": "A", "status": "Downloading", "mb": "100", "mbleft": "40", "percentage": "60", "timeleft": "0:00:30"},
            {"nzo_id": "SABnzbd_nzo_b", "filename": "B", "status": "Downloading", "mb": "100", "mbleft": "100", "percentage": "0", "timeleft": "0:00:00"},
        ]}},
        "history": {"history": {"slots": [
            {"nzo_id": "SABnzbd_nzo_c", "name": "C", "status": "Extracting", "bytes": 10},
            {"nzo_id": "SABnzbd_nzo_d", "name": "D", "status": "Failed", "bytes": 10, "fail_message": "Repair failed"},
        ]}},
    }
    calls = []

    async def fake_api(self, mode, **params):
        calls.append((mode, params))
        return responses[mode]

    monkeypatch.setattr(SabnzbdClient, "_api", fake_api)
    sab = SabnzbdClient({"host": "sab", "api_key": "k"})
    items = {i.id: i for i in asyncio.run(sab.get_items("tv"))}
    a, b, c, d = (items[f"SABnzbd_nzo_{k}"] for k in "abcd")
    assert (a.status, a.speed_bps, a.eta_seconds) == ("downloading", 2048 * 1024, 30)
    assert (a.position, b.position, c.position) == (0, 1, None)
    assert (b.status, b.speed_bps, b.eta_seconds) == ("queued", 0, None)
    assert (c.status, c.progress) == ("downloading", 100.0)
    assert (d.status, d.message) == ("failed", "Repair failed")

    calls.clear()
    found = asyncio.run(sab.find_items(["SABnzbd_nzo_B", "SABnzbd_nzo_gone"]))
    assert [i.id for i in found] == ["SABnzbd_nzo_b"]
    assert all("category" not in params and params["nzo_ids"] == "SABnzbd_nzo_B,SABnzbd_nzo_gone" for _, params in calls)


def test_sabnzbd_test_flags_each_missing_category(monkeypatch):
    import asyncio

    from backend.plugins.download_clients.sabnzbd import SabnzbdClient

    async def fake_api(self, mode, **params):
        return {"version": {"version": "4.3.0"}, "get_cats": {"categories": ["*", "movies"]}}.get(mode, {})

    monkeypatch.setattr(SabnzbdClient, "_api", fake_api)
    ok, message = asyncio.run(SabnzbdClient({"host": "sab", "api_key": "k", "category": "movies", "tv_category": "tv"}).test())
    assert ok and "category 'tv' does not exist" in message
    ok, message = asyncio.run(SabnzbdClient({"host": "sab", "api_key": "k", "category": "Movies", "tv_category": "movies"}).test())
    assert ok and message == "Connected to SABnzbd 4.3.0."


def test_recycle_stays_on_the_files_volume(tmp_path, make_video, monkeypatch):
    """A bin on another volume would turn recycling into a full copy of the file (minutes for a remux)."""
    volume = tmp_path / "data"
    video = make_video(volume / "media" / "movies" / "Movie (2000)" / "movie.mkv")
    config_bin = tmp_path / "config" / "recycle"
    copies = []
    monkeypatch.setattr(fileops, "same_volume", lambda a, b: False)
    monkeypatch.setattr(fileops, "_mount_root", lambda p: volume)
    monkeypatch.setattr(fileops, "_copy_atomic", lambda src, dst: copies.append(src))

    target = fileops.recycle(video, config_bin)
    assert target.parent.parent == volume / fileops.VOLUME_RECYCLE_DIR and target.exists() and not video.exists()
    assert copies == [] and not config_bin.exists()

    tx = fileops.FileTransaction()  # and it still rolls back
    tx.recycle(make_video(video), config_bin)
    assert not video.exists()
    tx.rollback()
    assert video.exists()


def test_recycle_never_uses_a_filesystem_root(tmp_path, make_video, monkeypatch):
    video = make_video(tmp_path / "movie.mkv")
    config_bin = tmp_path / "recycle"
    monkeypatch.setattr(fileops, "same_volume", lambda a, b: False)
    monkeypatch.setattr(fileops, "_mount_root", lambda p: Path(Path(p).anchor))
    assert fileops.recycle_root_for(video, config_bin) == config_bin


def test_prune_empty_dirs_removes_only_old_empty_trees(tmp_path, make_video):
    root = tmp_path / "downloads"
    nested = root / "Show.S01.1080p" / "Subs"  # a folder holding only empty folders goes in the same pass
    nested.mkdir(parents=True)
    fresh, kept, skipped = root / "Just.Started", root / "movies", root / "_UNPACK_x"
    hidden = root / ".maniac-recycle" / "2026-01-01"
    for d in (fresh, kept, skipped, hidden):
        d.mkdir(parents=True)
    video = make_video(root / "Movie.2024" / "movie.mkv")
    then = time.time() - 7200
    for d in (nested, nested.parent, kept, skipped, hidden, hidden.parent, video.parent):
        os.utime(d, (then, then))

    removed = fileops.prune_empty_dirs(root, min_age_seconds=3600, skip=lambda p: p.name.startswith("_UNPACK_"),
                                       keep=lambda p: p.name == "movies")
    assert removed == 2 and not nested.parent.exists()
    assert all(p.exists() for p in (root, fresh, kept, skipped, hidden, video))
