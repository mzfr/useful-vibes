import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "providers"


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def fake_tmdb(monkeypatch):
    """Deterministic TMDB stand-in so importer tests need no key or network."""
    from datetime import date

    from providers.base import EpisodeMetadata, Metadata, SeasonMetadata

    def _meta(media_type, media_id, **kw):
        base = dict(
            source="tmdb", media_type=media_type, media_id=str(media_id),
            title=f"{media_type}-{media_id}", image="", synopsis="", genres=[],
            release_date=date(2020, 1, 1), runtime=45,
            max_progress=1 if media_type in ("movie", "episode") else None,
            external_ids={}, raw={},
        )
        base.update(kw)
        return base

    class FakeTMDB:
        source = "tmdb"

        def details(self, media_type, media_id):
            return Metadata(**_meta(media_type, media_id))

        def search(self, media_type, query, page=1):
            from providers.base import SearchHit, SearchResults

            hit = SearchHit(
                source="tmdb", media_type=media_type, media_id="9001",
                title=f"{query} ({media_type})", image="", year=2030,
            )
            return SearchResults(
                page=1, total_pages=1, total_results=1, hits=[hit],
            )

        def season(self, media_id, season_number):
            episodes = [
                EpisodeMetadata(
                    **_meta("episode", media_id, title=f"E{n}"),
                    season_number=season_number, episode_number=n,
                )
                for n in range(1, 4)
            ]
            return SeasonMetadata(
                **_meta("season", media_id, title=f"S{season_number}",
                        runtime=None, max_progress=len(episodes)),
                season_number=season_number, episodes=episodes,
            )

    provider = FakeTMDB()
    monkeypatch.setattr("providers.registry.get", lambda source="tmdb": provider)
    return provider
