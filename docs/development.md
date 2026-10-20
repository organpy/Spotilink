# Development and operations

Developer notes for Spotilink. See the [README](../README.md) for setup and the [API reference](api.md) for endpoints and response examples.

## Provider setup and limits

### YouTube Data API v3

1. Create a Google Cloud project and enable **YouTube Data API v3**.
2. Create an API key and set `YOUTUBE_API_KEY`.
3. Keep the key server-side. Do not expose it to browsers.

Quota notes:

- `search.list` costs **100 quota units per call**.
- `videos.list` costs **1 unit per call**.
- The default project quota is **10,000 units/day**.
- The default `YOUTUBE_MAX_QUERIES=1` costs up to **101 units per uncached resolve** (one search plus one video-details call), or roughly **99 new resolutions per day** before cache hits.
- Set `YOUTUBE_MAX_QUERIES=2` for a second query variant. That costs up to **202 units** and roughly halves the daily new-resolution capacity.
- `YOUTUBE_RATE_LIMIT_PER_MINUTE` and `YOUTUBE_RATE_LIMIT_BURST` add a local token bucket on top of Google's quota.
- When quota is exhausted, the API returns HTTP `503` with `error.code: "providers_rate_limited"` and an unverified `search_fallback` URL. If an expired verified match is still within `STALE_MATCH_GRACE_SECONDS`, the API serves that direct link instead, with `stale: true`.

### SoundCloud official API

SoundCloud currently requires a **SoundCloud Artist Pro subscription** to register an API application and obtain a Client ID/Secret. See the official [API key instructions](https://developers.soundcloud.com/docs/api/register-app). If you do not have Artist Pro, skip SoundCloud and run YouTube-only.

1. Register a SoundCloud API app while signed in with an Artist Pro account.
2. Set `SOUNDCLOUD_CLIENT_ID` and `SOUNDCLOUD_CLIENT_SECRET`.
3. The resolver obtains an OAuth 2.1 client-credentials token from `https://secure.soundcloud.com/oauth/token` and refreshes it after expiry or a `401`.

Known platform limits to plan around:

- **50 client-credential tokens per 12 hours per app**.
- **30 token requests per hour per IP**.
- Access tokens are valid for about **1 hour**.
- The resolver caches the token in memory and only refreshes when needed. Multiple app processes each maintain their own token cache.
- SoundCloud API request limits can change; `SOUNDCLOUD_RATE_LIMIT_PER_MINUTE` and `SOUNDCLOUD_RATE_LIMIT_BURST` provide a conservative local limit. Review the current SoundCloud developer terms and rate limits for your app.

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

### Where the cache lives

SQLite is the only persistent store. The default path is:

```text
./data/resolver.db
```

relative to the API process's working directory, and can be changed with `DATABASE_PATH`. The database contains **no audio**; it stores Spotify metadata, provider URLs, scores, and timestamps. WAL mode adds `resolver.db-wal` and `resolver.db-shm` while the process is running.

On a container host or serverless platform, mount a persistent volume and point `DATABASE_PATH` at it. Otherwise the cache is lost whenever the container filesystem is replaced. For multiple API instances, use a shared volume or replace SQLite with a shared database; the in-process locks and rate limiters still require coordination.

SQLite stores:

- `spotify_metadata`: found/not-found metadata with fetched and expiry timestamps.
- `resolutions`: one row per `(spotify_id, platform)` with status (`matched`/`not_found`), canonical URL, confidence, score breakdown, metadata snapshot, timestamps, and expiry.

Default TTLs:

| Entry | TTL |
|---|---|
| Spotify metadata | 7 days |
| Verified match | 7 days |
| Negative provider result | 1 hour |
| Spotify track not found | 5 minutes |
| Stale match grace after expiry | 30 days |

Expired verified matches are retained for `STALE_MATCH_GRACE_SECONDS` (default 30 days) and may be served only after a provider failure. They are returned with `stale: true`; they are not treated as freshly verified. Set the grace period to `0` to disable stale matches. Expired negative results are pruned normally.

A verified match can be served after metadata expires because the resolution row includes a metadata snapshot. Metadata and resolution lookups use separate per-key locks so concurrent identical requests share one Spotify fetch and one provider search per process. Cached candidates are re-scored with the current matching configuration on every cache hit; a match that no longer clears the current threshold is searched again. Transient provider failures are not cached.

SQLite is shared across processes, but the in-process locks, provider token cache, and rate limiters are not. For multi-worker deployments, use conservative provider limits or route resolution through one worker/queue. The inbound limiter is also per process.

## Configuration reference

See [`../.env.example`](../.env.example) for every setting. Key groups:

| Variable | Purpose |
|---|---|
| `HOST`, `PORT` | Bind address used by `python -m spotify_resolver.main`; Uvicorn CLI flags take precedence when supplied. |
| `LOG_LEVEL` | Application/Uvicorn log level. |
| `DATABASE_PATH` | SQLite path. Default `./data/resolver.db`. |
| `HTTP_USER_AGENT` | Outbound User-Agent. Default `spotilink/0.1`. |
| `YOUTUBE_API_KEY` | Enables YouTube. |
| `YOUTUBE_MAX_QUERIES` | Query variants per YouTube search. Default `1`; `2` can improve matching but doubles search quota use. |
| `SOUNDCLOUD_CLIENT_ID`, `SOUNDCLOUD_CLIENT_SECRET` | Enables SoundCloud. Requires Artist Pro to register credentials. |
| `MATCH_THRESHOLD`, `WEIGHT_*`, `MIN_*_SCORE`, `*_MAX_DURATION_DIFF_MS`, `REQUIRE_DURATION` | Matching behavior. |
| `*_TTL_SECONDS` | Cache lifetimes. |
| `STALE_MATCH_GRACE_SECONDS` | Extra time an expired verified match may be served after a provider failure. Default 30 days; `0` disables. |
| `*_RATE_LIMIT_PER_MINUTE`, `*_RATE_LIMIT_BURST` | Outbound provider token buckets. |
| `API_RATE_LIMIT_PER_MINUTE` | Inbound per-client sliding window. `0` disables. |
| `RESOLVE_TIMEOUT_SECONDS` | Whole-request timeout. |
| `PROVIDER_TIMEOUT_SECONDS`, `SPOTIFY_TIMEOUT_SECONDS` | Per-upstream request timeouts. |
| `HTTP_MAX_RETRIES`, `HTTP_RETRY_BACKOFF_SECONDS`, `HTTP_RETRY_AFTER_CAP_SECONDS` | Retry policy for `429`/`5xx`; timeouts are not retried. |
| `*_MAX_BYTES` | Response-size caps. |
| `CORS_ORIGINS` | Comma-separated browser origins. Empty disables CORS middleware. |
| `TRUST_PROXY_HEADERS` | Trust the first `X-Forwarded-For` address for inbound limiting. Only enable behind a trusted proxy. |

## Project layout

```text
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

## Testing

The test suite mocks all external HTTP calls. No live provider credentials are required:

```bash
uv run pytest -q
```

## Operational notes

- Spotify can change its public page format or block automated requests. Extraction is layered to be resilient, but a blocked/redesigned page becomes `502`.
- This project deliberately does **not** use the official Spotify Web API, OAuth, or credentials.
- YouTube quota is the main scaling constraint. The default single query uses about half the quota of two variants; cache aggressively and request a quota increase if needed.
- When quota is exhausted, an expired verified match can be served stale for up to 30 days. Without a stale match and without SoundCloud, the API can return only an unverified search fallback.
- SoundCloud token issuance limits matter for multi-process or frequently restarted deployments.
- Provider search results and availability are outside this service's control. The fallback URL is a convenience, not a verification.
- Do not use this service to download or redistribute audio, bypass provider terms, or evade rate limits. See [provider terms](provider-terms.md).

See also:

- [API reference](api.md)
- [Architecture](architecture.md)
- [Provider terms](provider-terms.md)
- [README](../README.md)
