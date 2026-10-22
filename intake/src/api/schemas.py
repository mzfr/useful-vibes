"""Pydantic schemas for the public API."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from ninja import Field, Schema


class SearchHitOut(Schema):
    source: str
    media_type: str
    media_id: str
    title: str
    image: str = ""
    year: int | None = None
    synopsis: str = ""


class SearchOut(Schema):
    hits: list[SearchHitOut]
    page: int
    total_pages: int
    total_results: int


class MetadataOut(Schema):
    source: str
    media_type: str
    media_id: str
    title: str
    image: str = ""
    synopsis: str = ""
    genres: list[str] = []
    release_date: date | None = None
    runtime: int | None = None
    max_progress: int | None = None
    external_ids: dict[str, str] = {}


class TrackIn(Schema):
    source: str
    media_id: str
    season_number: int | None = Field(default=None, ge=0)
    episode_number: int | None = Field(default=None, ge=1)
    status: Literal["completed", "in_progress", "planning", "paused", "dropped"] | None = None
    score: int | None = Field(default=None, ge=1, le=10)
    progress: int | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None


class TrackOut(Schema):
    id: int
    media_type: str
    item_id: int
    status: str
    score: int | None
    progress: int
    repeats: int
    start_date: date | None
    end_date: date | None
    progressed_at: datetime | None


class HistoryRangeIn(Schema):
    from_: datetime = Field(alias="from")
    to: datetime


class ProgressIn(Schema):
    progress: int = Field(ge=0)


class BulkIn(Schema):
    """Bulk season ops: action ∈ all / range."""
    action: str
    start: int | None = None
    end: int | None = None


class UpNextRowOut(Schema):
    show_id: int
    show_title: str
    next_episode_id: int
    next_episode_label: str
    air_date: date | None


class StatsOut(Schema):
    year: int | None
    movies_completed: int
    shows_completed: int
    episodes_watched: int
    books_finished: int
    minutes_watched: int
    pages_read: int
    top_genres: list[tuple[str, int]] = []
    decades: list[tuple[int, int]] = []


class ImportJobOut(Schema):
    id: int
    kind: str
    state: str
    processed: int
    total: int
    percent: int
    error: str
    needs_review_count: int


class TokenMintIn(Schema):
    ttl: str = "30d"
    label: str = ""


class TokenMintOut(Schema):
    token: str
    expires_at: datetime | None
