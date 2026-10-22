"""History grouping and the distinct month/week presentation contracts."""
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Item
from tracking.models import CompletionEvent

pytestmark = pytest.mark.django_db


def add_episode(user, show, season, number, when):
    parent, _ = Item.objects.get_or_create(
        source="tmdb", media_type="season", media_id=show.media_id,
        season_number=season, parent=show,
        defaults={"title": f"Season {season}"},
    )
    item, _ = Item.objects.get_or_create(
        source="tmdb", media_type="episode", media_id=show.media_id,
        season_number=season, episode_number=number, parent=parent,
        defaults={"title": f"Episode {number}", "runtime": 20},
    )
    return CompletionEvent.objects.create(user=user, item=item, completed_at=when)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="history_test")


@pytest.fixture
def show():
    return Item.objects.create(
        source="tmdb", media_type="tv", media_id="123", title="Rick and Morty",
    )


@pytest.mark.parametrize("view", ["month", "week"])
@pytest.mark.parametrize("count", [1, 3, 20])
def test_history_month_opens_all_cards_and_week_never_folds(client, user, show, view, count):
    when = timezone.now()
    for number in range(1, count + 1):
        add_episode(user, show, 1, number, when)
    client.force_login(user)
    response = client.get("/history/", {"view": view})
    assert response.status_code == 200
    day = next(day for day in response.context["days"] if day["watches"])
    assert day["completion_count"] == count
    assert len(day["watches"]) == 1
    assert day["watches"][0]["completion_count"] == count
    assert response.context["episode_count"] == count
    html = response.content.decode()
    if view == "month":
        before, folded = html.split('<details class="k-history-more" open>')
        inside, after = folded.split("</details>")
        assert 'class="k-history-card"' in inside
        assert 'class="k-history-card"' not in before + after
        assert '<details class="k-history-more" open>' in html
    else:
        assert 'class="k-history-card"' in html
        assert "<details" not in html
        assert "k-history-overflow" not in html


def test_history_groups_by_show_season_and_day_preserving_gaps_and_repeats(client, user, show):
    when = timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)
    for number in [1, 2, 3, 7, 8, 8]:
        add_episode(user, show, 1, number, when)
    for number in [1, 2]:
        add_episode(user, show, 2, number, when + timedelta(minutes=5))
    other_show = Item.objects.create(
        source="tmdb", media_type="tv", media_id="456", title="Another show",
    )
    add_episode(user, other_show, 1, 1, when + timedelta(minutes=10))
    add_episode(user, show, 1, 4, when - timedelta(days=1))
    client.force_login(user)
    response = client.get("/history/", {"view": "month"})
    today = next(day for day in response.context["days"] if day["date"] == when.date())
    assert today["completion_count"] == 9
    cards = today["watches"]
    assert len(cards) == 3
    assert cards[0]["show"] == other_show
    assert cards[1]["episode_code"] == "S2 · E1–2"
    assert cards[2]["episode_code"] == "S1 · E1–3, E7–8"
    assert cards[2]["completion_count"] == 6
    assert cards[2]["repeat_count"] == 1


def test_history_does_not_merge_movies_or_books(client, user):
    for media_type in ["movie", "book"]:
        item = Item.objects.create(
            source="manual", media_type=media_type, media_id=media_type, title=media_type,
        )
        for _ in range(2):
            CompletionEvent.objects.create(user=user, item=item, completed_at=timezone.now())
    client.force_login(user)
    response = client.get("/history/", {"view": "week"})
    day = next(day for day in response.context["days"] if day["watches"])
    assert len(day["watches"]) == day["completion_count"] == 4
