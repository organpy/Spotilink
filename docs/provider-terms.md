# Provider terms and responsible operation

This document is an operational checklist, not legal advice. Provider terms change; review the current terms for each platform before deploying a public service.

## Spotify

- The resolver performs ordinary unauthenticated `GET` requests for **public** track and embed pages on `open.spotify.com`.
- It does not use the Spotify Web API, OAuth, user accounts, cookies, or credentials.
- It does not bypass authentication, paywalls, geographic restrictions, or bot protections.
- Extracted anonymous session/access tokens embedded in Spotify page state are ignored and never persisted or logged.
- No audio is downloaded, proxied, cached, or streamed.
- Use a descriptive `HTTP_USER_AGENT`, keep request volumes modest, and cache metadata. If Spotify blocks or changes its pages, stop retrying and treat it as metadata unavailable (`502`).
- Do not present extracted Spotify metadata as an official Spotify API response.

## YouTube / Google

- Search and video details use the official **YouTube Data API v3** with your API key.
- No scraping of YouTube pages, no `yt-dlp`, and no audio/video downloading.
- API keys must remain server-side. Restrict keys in Google Cloud where possible.
- Respect the daily quota. `search.list` is much more expensive than `videos.list`; use caching and the query-variant limit.
- Follow the YouTube API Services Terms of Service and developer policies, including attribution and data-retention requirements. This project stores only resolution metadata, not YouTube content.
- The `- Topic`, VEVO, and official-channel bonuses are heuristics. A high score is not a claim of rights ownership.

## SoundCloud

- Search uses the official SoundCloud API with OAuth 2.1 client credentials.
- Registering an API application currently requires a SoundCloud Artist Pro subscription. Without it, leave SoundCloud unconfigured and operate YouTube-only.
- No scraping of SoundCloud pages and no audio downloading or proxying.
- Keep client credentials server-side.
- Respect token issuance limits (historically 50 client-credential tokens per 12 hours per app and 30 per hour per IP) and all current API rate limits.
- Use `SOUNDCLOUD_RATE_LIMIT_PER_MINUTE` and `SOUNDCLOUD_RATE_LIMIT_BURST` conservatively. Multi-process deployments each hold their own token and limiter.
- Follow SoundCloud's developer terms, including attribution, caching, and deletion requirements. Delete cached resolution data when required or when it expires.

## Verification and fallbacks

- A result in `match` cleared the configured score threshold and is the service's **verified** match. If `stale` is `true`, it came from an expired cache entry after a provider failure and has not been rechecked; present it as stale and possibly unavailable, not as freshly verified.
- A result in `search_fallback` is an **unverified search URL** and must be presented as such.
- Never describe a fallback as verified or official.
- Provider search results may be incomplete, region-specific, or removed later. The resolver cannot guarantee that a previously cached URL remains playable.
- When YouTube quota is exhausted, reuse cached verified matches where they are still within `STALE_MATCH_GRACE_SECONDS`, or return the unverified search fallback. Do not rotate projects or keys to evade quota.

## Data and operations

- SQLite stores Spotify metadata, provider URLs, confidence scores, and timestamps. It does not store credentials or audio. The default database path is `./data/resolver.db`, configurable with `DATABASE_PATH`.
- Default TTLs are 7 days for metadata and verified matches, 1 hour for negative provider results, and 5 minutes for Spotify "not found" responses. Expired verified matches may be served stale for up to 30 days after provider failures (`STALE_MATCH_GRACE_SECONDS`).
- Logs should not contain API keys, client secrets, OAuth tokens, or raw Spotify page state.
- Use TLS at the edge if the API is exposed beyond localhost. Inbound rate limiting is intentionally simple and per process; add a gateway or shared limiter for multi-instance deployments.
- This project is not affiliated with Spotify, YouTube, Google, or SoundCloud.
