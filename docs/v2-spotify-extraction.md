# Spotify playlist extraction: investigation and results

Everything below was measured against the live services on **2026-11-09** with
`scripts/spotify_playlist_diag.py` (raw data:
[`results/spotify-extraction-2026-11-09.json`](results/spotify-extraction-2026-11-09.json)).
Nothing here is taken from a README. Re-run the script to refresh it; Spotify's
internal API can change at any time.

## What was found

Spotify's web player reads playlists through an internal GraphQL gateway
(`api-partner.spotify.com/pathfinder`) with **persisted queries** (a SHA-256 of
the query, no query text) and an **anonymous bearer token** that Spotify itself
embeds in its public embed pages (`__NEXT_DATA__` →
`props.pageProps.state.settings.session`, with an expiry timestamp). No account,
cookie or official API credential is used.

| Aspect | Observed behaviour |
|---|---|
| Token | ~45 min lifetime. Invalid/expired → HTTP 401. Reused until 60 s before expiry, renewed once on 401. |
| Hash rotation | Unknown hash → HTTP **412** `"Invalid query hash"`. Hashes are baked into the JS bundles. |
| Page size | `limit` above 100 is honoured (limit 343 returned 200 items, limit 1000 returned all 594), **but** with 500-item pages one track of a 500-track playlist came back with degraded metadata and a removed item was counted differently. Default stays at 100. |
| Pagination | `offset`/`limit`; `content.totalCount` is the reported size. Advance `offset` by the raw item count. |
| Not found / private | HTTP 200 with `playlistV2.__typename: "NotFound"`. |
| Malformed ID | HTTP 200 with `GenericError` ("… 400 BAD_REQUEST"). |
| Removed tracks | Item stays with its `spotify:track:` URI but no name/artists → recorded as `unavailable` (`removed`). |
| Duplicates | Preserved by the API and by SpotiLink. |
| Embed page | Server-renders only the **first 100** tracks; artists are one joined string; **no total count**. |

## The two implementations

| | A: `spotify_downloader` adaptation | B: `webplayer` (independent) |
|---|---|---|
| Source | Adapted from [TousK74/spotify_downloader](https://github.com/TousK74/spotify_downloader) (MIT; attribution kept in the module docstring) | Written from scratch |
| Token source | fixed track embed page | the requested playlist's own embed page |
| Endpoint | `GET pathfinder/v1/query` | `POST pathfinder/v2/query` |
| Operation / hash | `fetchPlaylist`, hash hardcoded from the original (`a65e121…`) | `queryPlaylist`, hash **discovered at runtime** from the web-player bundle, cached 6 h, rediscovered on HTTP 412, or pinned with `SPOTIFY_WEBPLAYER_QUERY_HASH` |
| Page size | 100 (fixed) | `SPOTIFY_PLAYLIST_PAGE_SIZE` (default 100) |

Only the metadata path of spotify_downloader was used. Its YouTube search,
yt-dlp downloading, FFmpeg conversion and CLI are not part of SpotiLink, and the
project is not installed as a dependency (it is a single script, not a library).
Both implementations share response validation/normalisation
(`playlists/spotify_pathfinder.py`). They are **alternatives against the same
upstream**, not independent providers.

## Results

Complete = `extracted == reported` (verified from the data, not assumed).

| Playlist (reported) | Method | Extracted | Complete | Requests | Time |
|---|---|---|---|---|---|
| 10 | A | 10 | ✅ | 2 | 1.3 s |
| 10 | B | 10 | ✅ | 4¹ | 1.3 s |
| 50 | A / B | 50 / 50 | ✅ / ✅ | 1 / 1 | 0.46 / 0.48 s |
| 100 | A / B | 100 / 100 | ✅ / ✅ | 1 / 1 | 0.49 / 0.29 s |
| 200 | A / B | 200 / 200 | ✅ / ✅ | 2 / 2 | 1.27 / 0.58 s |
| 500 | A / B | 500 / 500 | ✅ / ✅ | 5–6 / 5–8 | 2.5 / 1.3 s |
| 594 | A / B | 594 / 594 | ✅ / ✅ | 6 / 6 | 3.4 / 1.9 s |
| 200 or 594 | embed | **100** | ❌ (by design) | 1 | 0.2–0.4 s |
| nonexistent | A / B / embed | `playlist_not_found` ×3 | n/a | 1 / 1 / 1 | <0.3 s |
| malformed ID | A / B | `playlist_not_found` | n/a | 1 / 1 | <0.2 s |

¹ includes one-time token fetch and hash discovery (two extra downloads).

* Both GraphQL implementations extracted **every** item, including beyond 100
  (200, 500 and 594 items verified against the reported total). Pagination is
  reliable. The 500-track playlist contains one removed track (reported as
  `unavailable: removed`, so 499 usable).
* No duplicates occurred in the test playlists; duplicate handling is covered by
  the mocked tests (positions and repeated IDs preserved).
* B is faster (fewer round trips after warm-up; the discovered hash is cached).
  A is simpler but its hash is hardcoded: when Spotify retires it, A fails with
  `upstream_schema_changed` while B rediscovers automatically. B is therefore
  the more maintainable one; A stays primary because it is the one adapted from
  the reference and needs no bundle download. `SPOTIFY_PLAYLIST_EXTRACTOR=webplayer`
  flips the order.
* Not tested live: private playlists beyond the `NotFound` shape, playlists
  >1000 tracks (the job cap is 1000 by default), 429 throttling (simulated in
  tests only; the live service did not throttle at this volume).

## Failure behaviour

* Schema/hash/GraphQL changes **fail loudly** (`upstream_schema_changed`): an
  empty or unexpected response is never reported as a successful empty playlist.
* A count mismatch is reported with `extraction_complete: false`
  (`incomplete_reason: "count_mismatch"`), never silently as complete.
* HTTP 429/403 marks the upstream as *blocked*. The second GraphQL
  implementation is **skipped** (same endpoint), nothing is retried endlessly,
  and only the embed partial data (≤100 tracks, `extraction_complete: false`) is
  returned, or the job fails with `upstream_blocked`.

## Maintenance notes

If Spotify rotates the hash used by A, set `SPOTIFY_PLAYLIST_EXTRACTOR=webplayer`
(or just rely on `auto`, which falls back to B). If the bundle layout changes
and B's discovery fails, pin a hash with `SPOTIFY_WEBPLAYER_QUERY_HASH` after
finding `"queryPlaylist","query","<sha256>"` in the `mobile-web-player` bundle
linked from `https://open.spotify.com/` (fetched with a non-browser User-Agent).
Both modules are small and isolated to make such repairs quick.
