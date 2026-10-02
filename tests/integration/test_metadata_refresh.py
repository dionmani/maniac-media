"""Scheduled metadata refresh: thin (IMDb-only) matches get upgraded once a richer provider is configured."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.domain.types import CastMember, MediaMetadata


@pytest.fixture
def movies(database):
    from backend.db import models
    from backend.db.base import utcnow
    from backend.db.session import session_scope

    three_days_ago = utcnow() - timedelta(days=3)
    with session_scope() as db:
        thin = models.Movie(title="Refresh Thin", year=1968, imdb_id="tt7700001", metadata_source="imdb", overview=None,
                            cast_members=[{"name": "Keir Dullea"}], last_metadata_refresh=three_days_ago)
        full = models.Movie(title="Refresh Full", year=1968, imdb_id="tt7700002", metadata_source="tmdb", overview="A plot.",
                            last_metadata_refresh=three_days_ago)
        db.add_all([thin, full])
        db.flush()
        ids = {"thin": thin.id, "full": full.id}
    yield ids
    with session_scope() as db:
        for movie_id in ids.values():
            db.delete(db.get(models.Movie, movie_id))


def _run(monkeypatch, provider_keys):
    from backend.db.session import db_manager
    from backend.services import metadata_service

    asked = []

    async def fake_fetch(**kw):
        asked.append(kw.get("imdb_id"))
        if kw.get("imdb_id") == "tt7700001":
            return MediaMetadata(media_type="movie", provider="tmdb", title="Refresh Thin", year=1968, imdb_id="tt7700001", tmdb_id=7700001,
                                 overview="The full plot.", genres=["Sci-Fi"], cast=[CastMember("Keir Dullea"), CastMember("Gary Lockwood"),
                                                                                       CastMember("William Sylvester")])
        return None

    monkeypatch.setattr(metadata_service, "providers", lambda media_type: [SimpleNamespace(key=k) for k in provider_keys])
    monkeypatch.setattr(metadata_service, "fetch_movie", fake_fetch)
    asyncio.run(metadata_service.refresh_library(db_manager.session, media_type="movie"))
    return asked


def test_thin_matches_are_upgraded_when_a_richer_provider_exists(monkeypatch, movies):
    from backend.db import models
    from backend.db.session import session_scope

    # IMDb is the only provider: re-asking it can't add anything, so the thin match isn't due yet
    assert "tt7700001" not in _run(monkeypatch, ["imdb"])

    asked = _run(monkeypatch, ["tmdb", "imdb"])
    assert "tt7700001" in asked
    assert "tt7700002" not in asked  # a full match refreshed 3 days ago keeps the normal 7-day cycle
    with session_scope() as db:
        thin = db.get(models.Movie, movies["thin"])
        assert thin.overview == "The full plot." and thin.genres == "Sci-Fi" and thin.metadata_source == "tmdb"
        assert len(thin.cast_members) == 3

        # still thin after a refresh (e.g. TMDB doesn't know the title): retried daily, not on every run
        thin.overview, thin.metadata_source = None, "imdb"
    assert "tt7700001" not in _run(monkeypatch, ["tmdb", "imdb"])
