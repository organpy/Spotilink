# Spotify Resolver

A small server-side API that turns a **public Spotify track URL** into a **verified YouTube or SoundCloud track URL**. It reads structured metadata from the public Spotify track page without the Spotify Web API, searches provider APIs, scores candidates, and caches verified results in SQLite.

It never downloads, proxies, streams, or stores audio.

```
GET /v1/resolve?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC&platform=youtube
```

## What it does

- Extracts Spotify ID, title, artists, album (when exposed), and duration from:
  - JSON-LD (`MusicRecording`)
  - Open Graph and `music:*` meta tags
  - Spotify's base64 `initialState` JSON
  - The public embed page's `__NEXT_DATA__` as a fallback/enrichment
- Searches with official provider APIs:
  - **YouTube Data API v3** (`search.list`, then `videos.list`)
  - **SoundCloud official API** (OAuth 2.1 client credentials, `/tracks`)
- Matches on normalized title, artist/channel, and duration.
- Penalizes live, remix, instrumental, karaoke, cover, sped-up, slowed, nightcore, 8D, bass-boosted, extended, edit, and similar alternate versions.
- Returns a verified match only when it clears a configurable confidence threshold.
- Returns a clearly marked **unverified search fallback** when nothing clears the threshold.
- Caches metadata, verified matches, and short-lived negative results in SQLite.
- Deduplicates concurrent lookups in one process with per-key locks.
- Enforces request timeouts, response-size limits, outbound provider rate limits, inbound API rate limits, and bounded retries.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (or pip + a virtual environment)
- At least one provider:
  - A YouTube Data API v3 key
  - A SoundCloud app with client credentials

## Quick start

```bash
uv sync --all-groups
cp .env.example .env
# Edit .env and set YOUTUBE_API_KEY and/or SOUNDCLOUD_CLIENT_ID/SECRET
uv run uvicorn spotify_resolver.main:app --host 127.0.0.1 --port 8000
```

The app loads `.env` from the working directory. Environment variables always take precedence in normal deployment setups; provider credentials are never written to the database or logs.

- API docs: <http://127.0.0.1:8000/docs>
- Health/configuration: <http://127.0.0.1:8000/healthz>
- Resolve: <http://127.0.0.1:8000/v1/resolve?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC>

Run the tests:

```bash
uv run pytest -q
```

All HTTP calls in the test suite are mocked. No live credentials are required.

## Provider setup

### YouTube Data API v3

1. Create a Google Cloud project and enable **YouTube Data API v3**.
2. Create an API key and set `YOUTUBE_API_KEY`.
3. Keep the key server-side. Do not expose it to browsers.

Quota notes:

- `search.list` costs **100 quota units per call**.
- `videos.list` costs **1 unit per call**.
- The default project quota is **10,000 units/day**.
- With the default `YOUTUBE_MAX_QUERIES=2`, an uncached resolve can cost up to **202 units** (two searches plus up to two video-details calls). Default quota therefore allows roughly 49 worst-case uncached resolutions per day. Verified and negative cache entries reduce this substantially.
- Set `YOUTUBE_MAX_QUERIES=1` to halve search quota usage at the cost of fewer query variants.
- `YOUTUBE_RATE_LIMIT_PER_MINUTE` and `YOUTUBE_RATE_LIMIT_BURST` add a local token bucket on top of Google's quota.

### SoundCloud official API

1. Create a SoundCloud app at <https://soundcloud.com/you/apps>.
2. Set `SOUNDCLOUD_CLIENT_ID` and `SOUNDCLOUD_CLIENT_SECRET`.
3. The resolver obtains an OAuth 2.1 client-credentials token from `https://secure.soundcloud.com/oauth/token` and refreshes it after expiry or a `401`.

Known platform limits to plan around:

- **50 client-credential tokens per 12 hours per app**.
- **30 token requests per hour per IP**.
- Access tokens are valid for about **1 hour**.
- The resolver caches the token in memory and only refreshes when needed. Multiple app processes each maintain their own token cache.
- SoundCloud API request limits can change; `SOUNDCLOUD_RATE_LIMIT_PER_MINUTE` and `SOUNDCLOUD_RATE_LIMIT_BURST` provide a conservative local limit. Review the current SoundCloud developer terms and rate limits for your app.

## API

### `GET /v1/resolve`

| Parameter | Required | Default | Description |
|---|---|---|---|
| `url` | yes | — | A public `open.spotify.com/track/{22-char-id}` URL. `intl-*` paths and `http(s)` are accepted. |
| `platform` | no | `youtube` | Preferred platform: `youtube` or `soundcloud`. |
| `fallback` | no | `true` | Try the other platform when the preferred platform has no verified match or is not configured. |

Example verified response:

```json
{
  "ok": true,
  "spotify_id": "4uLU6hMCjMI75M1A2tKUQC",
  "title": "Never Gonna Give You Up",
  "artists": ["Rick Astley"],
  "album": "Whenever You Need Somebody",
  "duration_ms": 213573,
  "duration_precision": "ms",
  "spotify_url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
  "match": {
    "platform": "youtube",
    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "title": "Never Gonna Give You Up (Official Video)",
    "artist": "Rick Astley",
    "uploader": "Rick Astley",
    "duration_ms": 213000,
    "confidence": 1.0,
    "score": {
      "title": 1.0,
      "artist": 1.0,
      "duration": 1.0,
      "official_bonus": 0.0,
      "version_penalty": 0.0,
      "total": 1.0,
      "capped": false,
      "version_tags": [],
      "duration_diff_ms": 573
    }
  },
  "search_fallback": null,
  "cached": false,
  "metadata_cached": false,
  "attempts": [
    {
      "platform": "youtube",
      "status": "verified",
      "cached": false,
      "best_confidence": 1.0
    }
  ],
  "error": null
}
```

A no-match response is still HTTP `200` with `ok: true`, `match: null`, and an **unverified** search fallback. The fallback targets the requested platform; `attempts` shows which platform was actually searched:

```json
{
  "ok": true,
  "spotify_id": "4uLU6hMCjMI75M1A2tKUQC",
  "title": "Never Gonna Give You Up",
  "artists": ["Rick Astley"],
  "album": "Whenever You Need Somebody",
  "duration_ms": 213573,
  "duration_precision": "ms",
  "spotify_url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
  "match": null,
  "search_fallback": {
    "platform": "youtube",
    "url": "https://www.youtube.com/results?search_query=Rick+Astley+Never+Gonna+Give+You+Up",
    "verified": false,
    "reason": "no_verified_match"
  },
  "cached": false,
  "metadata_cached": false,
  "attempts": [
    {"platform": "youtube", "status": "no_match", "cached": false, "best_confidence": 0.73}
  ],
  "error": null
}
```

Errors use the same envelope with `ok: false` and `error: {code, message, retryable, retry_after_seconds}`. When Spotify metadata was already available, the envelope also includes the flat Spotify fields and a search fallback.

### Status codes

| Code | Meaning |
|---|---|
| `200` | Resolution completed. `match` may be `null`. |
| `400` | Missing/invalid parameter or invalid boolean. |
| `404` | Spotify reports that the track does not exist. |
| `422` | Unsupported Spotify URL (album, playlist, artist, malformed ID, etc.). |
| `429` | Inbound API rate limit exceeded. `Retry-After` is set. |
| `502` | Spotify page metadata is unavailable or unusable. |
| `503` | Provider unavailable/rate-limited/not configured, Spotify unavailable, or all providers failed. |
| `504` | Spotify, provider, or whole-resolution timeout. |

### `GET /healthz`

Returns `200` when SQLite is reachable and at least one provider is configured. Otherwise returns `503` with `status: "degraded"`. It exposes provider names and configured booleans only; no credentials.

## Matching

The default score is a weighted average plus bonuses/penalties:

- Title similarity: `0.50`
- Artist/channel similarity: `0.35`
- Duration similarity: `0.15`

Duration is omitted if either side lacks a duration; the title/artist weights are renormalized and the total is capped by `NO_DURATION_SCORE_CAP` (default `0.85`). Duration is required by default (`REQUIRE_DURATION=true`), so such a candidate is not verified. A match is eligible only when:

- If both durations are present, they are within `30s` for YouTube or `15s` for SoundCloud. Missing duration is rejected unless `REQUIRE_DURATION=false`.
- Title similarity is at least `MIN_TITLE_SCORE` (default `0.65`).
- Artist similarity is at least `MIN_ARTIST_SCORE` (default `0.50`).
- The final score is at least `MATCH_THRESHOLD` (default `0.80`).

Official-channel bonuses are small, not decisive: YouTube `- Topic` up to `0.06`, VEVO up to `0.04`, other official-looking channels up to `0.02`; verified SoundCloud uploaders up to `0.02`. Version mismatches apply meaningful penalties (roughly `0.02` for remaster through `0.45` for karaoke). The API returns the full score breakdown so callers can audit decisions.

If no candidate clears the threshold, the API never labels it verified. It returns a provider search URL instead.

## Caching and concurrency

SQLite in WAL mode stores:

- `spotify_metadata`: found/not-found metadata with fetched and expiry timestamps.
- `resolutions`: one row per `(spotify_id, platform)` with status (`matched`/`not_found`), canonical URL, confidence, score breakdown, metadata snapshot, timestamps, and expiry.

Default TTLs:

| Entry | TTL |
|---|---|
| Spotify metadata | 7 days |
| Verified match | 7 days |
| Negative provider result | 1 hour |
| Spotify track not found | 5 minutes |

A verified match can be served after metadata expires because the resolution row includes a metadata snapshot. Metadata and resolution lookups use separate per-key locks so concurrent identical requests share one Spotify fetch and one provider search per process. Cached candidates are re-scored with the current matching configuration on every cache hit; a match that no longer clears the current threshold is searched again. Transient provider failures are not cached.

SQLite is shared across processes, but the in-process locks, provider token cache, and rate limiters are not. For multi-worker deployments, use conservative provider limits or route resolution through one worker/queue. The inbound limiter is also per process.

## Configuration

See [`.env.example`](.env.example) for every setting. Key groups:

| Variable | Purpose |
|---|---|
| `HOST`, `PORT` | Bind address used by `python -m spotify_resolver.main`; Uvicorn CLI flags take precedence when supplied. |
| `LOG_LEVEL` | Application/Uvicorn log level. |
| `DATABASE_PATH` | SQLite path. Default `./data/resolver.db`. |
| `YOUTUBE_API_KEY` | Enables YouTube. |
| `SOUNDCLOUD_CLIENT_ID`, `SOUNDCLOUD_CLIENT_SECRET` | Enables SoundCloud. |
| `MATCH_THRESHOLD`, `WEIGHT_*`, `MIN_*_SCORE`, `*_MAX_DURATION_DIFF_MS`, `REQUIRE_DURATION` | Matching behavior. |
| `*_TTL_SECONDS` | Cache lifetimes. |
| `*_RATE_LIMIT_PER_MINUTE`, `*_RATE_LIMIT_BURST` | Outbound provider token buckets. |
| `API_RATE_LIMIT_PER_MINUTE` | Inbound per-client sliding window. `0` disables. |
| `RESOLVE_TIMEOUT_SECONDS` | Whole-request timeout. |
| `PROVIDER_TIMEOUT_SECONDS`, `SPOTIFY_TIMEOUT_SECONDS` | Per-upstream request timeouts. |
| `HTTP_MAX_RETRIES`, `HTTP_RETRY_BACKOFF_SECONDS`, `HTTP_RETRY_AFTER_CAP_SECONDS` | Retry policy for `429`/`5xx`; timeouts are not retried. |
| `*_MAX_BYTES` | Response-size caps. |
| `CORS_ORIGINS` | Comma-separated browser origins. Empty disables CORS middleware. |
| `TRUST_PROXY_HEADERS` | Trust the first `X-Forwarded-For` address for inbound limiting. Only enable behind a trusted proxy. |

## Project layout

```
src/spotify_resolver/
  api.py          FastAPI routes, error handlers, inbound limiter
  cache.py        SQLite metadata/resolution cache
  concurrency.py  Keyed locks, token bucket, sliding window limiter
  config.py       Environment-backed settings
  http.py         Bounded HTTP client with redirects/retries/size limits
  main.py         Application factory and lifespan
  matching.py     Normalization, version penalties, scoring, queries
  models.py       Domain models and cache serialization
  schemas.py      Public JSON schema
  service.py      Resolution orchestration and fallback
  spotify.py      Public-page URL validation and metadata extraction
  providers/      YouTube and SoundCloud official API clients
tests/            Fully mocked unit/integration tests
```

## Limitations and operational notes

- Spotify can change its public page format or block automated requests. Extraction is layered to be resilient, but a blocked/redesigned page becomes `502`.
- This project deliberately does **not** use the official Spotify Web API, OAuth, or credentials.
- YouTube quota is the main scaling constraint. Cache aggressively and reduce query variants if needed.
- SoundCloud token issuance limits matter for multi-process or frequently restarted deployments.
- Provider search results and availability are outside this service's control. The fallback URL is a convenience, not a verification.
- Do not use this service to download or redistribute audio, bypass provider terms, or evade rate limits. See [docs/provider-terms.md](docs/provider-terms.md).

## License / affiliation

No license has been chosen for this repository. This project is not affiliated with Spotify, YouTube, Google, or SoundCloud.
