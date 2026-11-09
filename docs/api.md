# API reference

See the [README](../README.md) for setup and the [development notes](development.md) for matching, caching, provider limits, and configuration.

This page documents the v1 endpoint, which is unchanged. Playlists, jobs and the `ytmusicapi` fallback are documented in the [v2 guide](v2.md).

## `GET /v1/resolve`

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

## `GET /healthz`

Returns `200` when SQLite is reachable and at least one provider is configured. Otherwise returns `503` with `status: "degraded"`. It exposes provider names and configured booleans only; no credentials.

---

See also: [README](../README.md) · [Development notes](development.md) · [Architecture](architecture.md) · [Provider terms](provider-terms.md)
