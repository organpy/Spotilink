# Architecture

## Request flow

1. `GET /v1/resolve` validates the query parameters and applies the inbound per-client rate limit.
2. The service validates the Spotify URL and checks that at least one provider is configured.
3. It checks SQLite in this order:
   - Spotify metadata cache (`ok` or `not_found`).
   - Preferred-platform resolution cache when metadata is absent.
   - Fallback-platform resolution cache when the preferred platform is unconfigured or has a cached negative result.
4. On a metadata miss, the service takes a per-track metadata lock and rechecks the cache. One request fetches `https://open.spotify.com/track/{id}`; concurrent requests wait and then reuse the result.
5. Spotify extraction parses the main page. If title/artist metadata is missing, or duration is absent, it fetches the public embed page and parses `__NEXT_DATA__`.
6. For each platform in preferred-then-fallback order:
   - Skip cached negative results.
   - Recheck the resolution cache under a per-`(track, platform)` lock.
   - Build a small number of query variants.
   - Call the official provider API.
   - Normalize, score, rank, and optionally run another query variant if no strong match was found.
   - Cache a verified match or a short-lived negative result.
7. If a verified match exists, return it. Otherwise return HTTP `200` with `match: null` and an unverified search fallback. If no provider completed a search, return the appropriate `503`/`504` error envelope.

## Spotify extraction sources

The HTML parser captures only relevant scripts and meta tags:

| Source | Fields | Notes |
|---|---|---|
| JSON-LD `MusicRecording` | title, `byArtist`, `inAlbum`, ISO duration | Preferred title source; supplies missing artists/album; duration is used when no exact-ms source is available. |
| Open Graph / `music:*` | `og:title`, `music:musician_description`, `music:duration`, description segments | Duration is seconds precision. |
| `initialState` base64 JSON | title, artists, album, exact ms duration | Preferred for artists/album; entity must match the requested track ID. |
| `__NEXT_DATA__` embed JSON | title, artists, exact ms duration | Used as fallback and duration enrichment. |

Title and at least one artist are required. Missing fields are left `null`; nothing is invented. Anonymous tokens in embedded state are ignored.

## Scoring

```text
base = (w_title * title_sim + w_artist * artist_sim + w_duration * duration_sim)
       / (present weights)
total = clamp(base + official_bonus - version_penalty)
```

- Title/artist similarity uses Unicode-aware normalization, token Jaccard, sequence ratio, and conservative containment handling.
- Duration similarity is exact within 2 seconds, then decreases linearly to a floor at the platform's maximum allowed difference.
- If either duration is missing, duration is omitted from the weighted average, the total is capped, and the candidate is rejected by default (`REQUIRE_DURATION=true`).
- Version tags are detected in candidate titles and selected channel names. Expected tags in the Spotify title (for example an official live version) avoid the corresponding penalty.
- Hard guardrails reject mismatched durations and low title/artist similarity before threshold comparison.

## Cache schema

SQLite runs in WAL mode with a single connection per process and a write/read lock.

```text
spotify_metadata
  spotify_id PK, status, title, artists_json, album,
  duration_ms, duration_precision, fetched_at, expires_at

resolutions
  (spotify_id, platform) PK, status, url, confidence,
  best_confidence, candidate_json, title, artists_json, album,
  duration_ms, duration_precision, resolved_at, expires_at
```

`candidate_json` contains the candidate URL/title/artist/duration and the full score breakdown. Resolution rows include a metadata snapshot so a valid cached match can be served after the metadata cache expires. Cached candidates are re-scored with the active matching configuration; stale entries that no longer clear the threshold are ignored and searched again. Expired rows are filtered on read and pruned at startup.

## Failure isolation

- Spotify transport/parse failures do not create negative provider cache entries.
- Provider timeouts, rate limits, authentication errors, and upstream failures are reported in `attempts` but are not cached.
- A completed search with no acceptable candidate is a normal `no_match` and is negatively cached.
- Redirects are allowed only to pre-approved hosts. Provider redirects are disabled.
- Response bodies are streamed with byte caps; retries apply only to `429` and `5xx`, with capped `Retry-After` waits.
