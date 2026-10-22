# Intake

Self-hosted single-user tracker for movies, TV shows, and books.
Runs locally or in Docker with a SQLite database.

## Features

- Movies, TV shows (with per-episode tracking), and books
- TMDB + Open Library providers
- Per-item rewatch / reread counts, ratings (1–10), custom lists
- Episode grid with keyboard nav: `←→↑↓`, `x` to toggle, `r N` to rate
- Bulk season ops: mark-all-aired, mark-range, mark-whole, rewatch-season
- "Up Next" home page (stale-first ordering)
- Releases page — what's already out and unwatched (24h cached)
- Trakt export ZIP importer + CSV imports (Goodreads / Storygraph / Trakt-format)
- CSV + full JSON exports
- Public read-only sharing via access links
- Stats page (totals, top-rated, score histogram, genres, rewatch leaders)
- REST API (django-ninja, OpenAPI docs at `/api/v1/docs`)
- Light + dark theme toggle, single-user auth

## Quick start (dev)

```bash
uv sync
cp .env.example .env
bash scripts/fetch_vendor.sh
bash scripts/build_css.sh
# edit .env — at minimum set INTAKE_INITIAL_PASSWORD on the first run.
# TMDB_API_KEY is needed for movie/TV search to work.

uv run python src/manage.py migrate
uv run python src/manage.py runserver
```

Open <http://localhost:8000>, log in with `admin` and the password from `.env`.

## Docker

```bash
cp .env.example .env
# set INTAKE_SECRET_KEY, INTAKE_INITIAL_PASSWORD, TMDB_API_KEY at minimum.
# If you serve through an HTTPS reverse proxy, also set INTAKE_BEHIND_TLS=1
# to enable secure cookies + HSTS. Leave it off for plain-HTTP LAN use.
docker compose up -d
```

The SQLite DB lives on the `intake_data` named volume — back up with
`docker run --rm -v intake_data:/data alpine tar c -C /data . > backup.tar`.

## Optional deployment ideas

You could run Intake with Podman or publish generated read-only history pages
through Cloudflare Pages. Deployment configuration, publishing scripts, and
setup instructions for these options are not included.

Create and revoke history links under **Settings → History sharing**.

## Automatic episode downloads

Use **Auto ↓** on an Up Next show to send its current and later aired,
unwatched episodes to the configured download client. Intake picks the source
with the most seeders among known single-episode files between 1 and 10 GiB,
rejecting sub-720p results. Automatic downloads use the client's default save
path with a shared `Show/Season NN/` folder, regardless of source provider. Disabling the option stops future checks; already submitted
episodes are not submitted again.

Run a check manually with:

```bash
uv run python src/manage.py process_automatic_downloads
```

Enabling **Auto ↓** also runs an immediate check. Recurring checks require an
external scheduler; no scheduler configuration is included.

## Trakt import

Import a Trakt data export ZIP; no Trakt API credentials or OAuth setup are required.

1. Go to <https://trakt.tv/settings/data> and request your data export.
2. Download the ZIP when Trakt emails you.
3. In the UI, open **Import** → upload the ZIP. Imports run inline; progress
   polls via HTMX. Large histories take a few minutes.

This imports watch history (with rewatch dates), ratings, watchlist, custom
lists, favourites, hidden shows, and your profile for the Trakt Legacy page.

Or run from the CLI:

```bash
uv run python src/manage.py import_trakt path/to/trakt-export.zip
```

`TMDB_API_KEY` is still required — Trakt entries are resolved to metadata via
their TMDB IDs.

## Tests

```bash
uv run pytest
```

## License

MIT.
