<a href="https://destro.aetherics.dev/">
  <p align="center">
    <img src="spotilink.svg" alt="Spotilink banner">
  </p>
</a>
<h2 align="center">Spotilink</h2>
<p align="center">A server-side API that turns public Spotify track URLs into verified YouTube or SoundCloud track links.</p>
<br/>

It was made for [Destro](https://destro.aetherics.dev/), live music rooms for listening together.

It never downloads, proxies, streams, or stores audio.

```text
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
- Serves an expired verified match as **stale** when a provider is rate-limited or otherwise failing, rather than losing the link entirely.
- Caches metadata, verified matches, and short-lived negative results in SQLite.
- Deduplicates concurrent lookups in one process with per-key locks.
- Enforces request timeouts, response-size limits, outbound provider rate limits, inbound API rate limits, and bounded retries.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (or pip + a virtual environment)
- At least one provider:
  - A YouTube Data API v3 key
  - A SoundCloud app with client credentials. SoundCloud currently requires an **Artist Pro** subscription to register an API app; leave SoundCloud unset for a YouTube-only deployment.

## Quick start

```bash
uv sync --all-groups
cp .env.example .env
# Edit .env and set YOUTUBE_API_KEY and/or SOUNDCLOUD_CLIENT_ID/SECRET
uv run uvicorn spotify_resolver.main:app --host 127.0.0.1 --port 8000
```

The app loads `.env` from the working directory. Environment variables always take precedence in normal deployment setups; provider credentials are never written to the database or logs.

The project/distribution is named **Spotilink**; the Python package remains `spotify_resolver` so existing imports and deployment commands keep working.

- Web UI: <http://127.0.0.1:8000/>
- API docs: <http://127.0.0.1:8000/docs>
- Health/configuration: <http://127.0.0.1:8000/healthz>
- Resolve: <http://127.0.0.1:8000/v1/resolve?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC>

The local page at `/` is a minimal raw-JSON viewer. Paste a public Spotify track URL and submit the request without using the command line. The API response body is displayed as returned, including error responses. It uses no external assets or CDNs.

## Provider setup

### YouTube Data API v3

1. Create a Google Cloud project and enable **YouTube Data API v3**.
2. Create an API key and set `YOUTUBE_API_KEY`.
3. Keep the key server-side. Do not expose it to browsers.

Quick quota facts:

- `search.list` costs **100 quota units per call**.
- `videos.list` costs **1 unit per call**.
- The default project quota is **10,000 units/day**.
- The default `YOUTUBE_MAX_QUERIES=1` costs up to **101 units per uncached resolve**, or roughly **99 new resolutions per day** before cache hits.
- See [provider limits and failover](docs/development.md#provider-limits-and-quota) for the full quota math, stale-match behavior, and `YOUTUBE_MAX_QUERIES=2` tradeoff.

### SoundCloud official API

SoundCloud currently requires a **SoundCloud Artist Pro subscription** to register an API application and obtain a Client ID/Secret. See the official [API key instructions](https://developers.soundcloud.com/docs/api/register-app). If you do not have Artist Pro, skip SoundCloud and run YouTube-only.

1. Register a SoundCloud API app while signed in with an Artist Pro account.
2. Set `SOUNDCLOUD_CLIENT_ID` and `SOUNDCLOUD_CLIENT_SECRET`.
3. The resolver obtains an OAuth 2.1 client-credentials token from `https://secure.soundcloud.com/oauth/token` and refreshes it after expiry or a `401`.

See [SoundCloud limits](docs/development.md#soundcloud-official-api) for token issuance and rate-limit details.

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
  "stale": false,
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
  "stale": false,
  "metadata_cached": false,
  "attempts": [
    {"platform": "youtube", "status": "no_match", "cached": false, "best_confidence": 0.73}
  ],
  "error": null
}
```

Errors use the same envelope with `ok: false` and `error: {code, message, retryable, retry_after_seconds}`. When Spotify metadata was already available, the envelope also includes the flat Spotify fields and a search fallback. A provider-failure fallback uses `reason: "provider_unavailable"` and remains unverified.

If a provider is rate-limited, times out, rejects its credentials, or fails upstream, the resolver checks for an expired verified match within `STALE_MATCH_GRACE_SECONDS`. If one exists and still passes the current matching threshold, it returns HTTP `200` with the direct URL, `cached: true`, `stale: true`, and an attempt status of `stale`. That URL is not rechecked against the provider and may no longer be playable.

### Status codes

| Code | Meaning |
|---|---|
| `200` | Resolution completed. `match` may be `null`; a match with `stale: true` came from the expired cache. |
| `400` | Missing/invalid parameter or invalid boolean. |
| `404` | Spotify reports that the track does not exist. |
| `422` | Unsupported Spotify URL (album, playlist, artist, malformed ID, etc.). |
| `429` | Inbound API rate limit exceeded. `Retry-After` is set. |
| `502` | Spotify page metadata is unavailable or unusable. |
| `503` | Provider unavailable/rate-limited/not configured, Spotify unavailable, or all providers failed. The body may include an unverified search fallback. |
| `504` | Spotify, provider, or whole-resolution timeout. |

### `GET /healthz`

Returns `200` when SQLite is reachable and at least one provider is configured. Otherwise returns `503` with `status: "degraded"`. It exposes provider names and configured booleans only; no credentials.

## Development

Implementation details live in [`docs/development.md`](docs/development.md):

- Provider quota and rate-limit notes
- Matching and scoring rules
- SQLite schema, cache TTLs, stale-if-error behavior, and concurrency
- Full configuration reference
- Project layout and testing
- Operational limitations

Architecture and provider terms remain in separate documents:

- [Architecture](docs/architecture.md)
- [Provider terms](docs/provider-terms.md)

## License / affiliation

MIT License. See [LICENSE](LICENSE). This project is not affiliated with Spotify, YouTube, Google, or SoundCloud.
