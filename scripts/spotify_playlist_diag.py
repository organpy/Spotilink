"""Run each Spotify playlist extraction method independently and report results.

Usage:
    uv run python scripts/spotify_playlist_diag.py [--json] [--methods a,b,embed]
        [PLAYLIST_ID_OR_URL ...]

Without playlist arguments a default set of public playlists of different
sizes is used. Nothing is cached and no YouTube matching is performed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter

import httpx

from spotify_resolver.config import Settings
from spotify_resolver.http import LimitedHttpClient
from spotify_resolver.playlists.models import PlaylistError, PlaylistExtraction
from spotify_resolver.playlists.spotify_downloader_adapter import SpotifyDownloaderExtractor
from spotify_resolver.playlists.spotify_embed import SpotifyEmbedExtractor
from spotify_resolver.playlists.spotify_webplayer import SpotifyWebPlayerExtractor
from spotify_resolver.urls import InputKind, UnsupportedInput, classify_url

DEFAULT_PLAYLISTS = {
    "0iPQ4lGrvXDAoipBLBYDc5": "~10 tracks (user)",
    "37i9dQZF1DXcBWIGoYBM5M": "~50 tracks (editorial)",
    "5ABHKGoOzxkaa28ttQV9sE": "~100 tracks (user)",
    "37i9dQZF1DWXRqgorJj26U": "~200 tracks (editorial)",
    "6G9mBCSozMx0sOSXhSzZRY": "~500 tracks (user)",
    "7EAqBCOVkDZcbccjxZmgjp": "~594 tracks (user)",
    "37i9dQZF1E4zxHpwG9Mq0S": "not found",
    "AAAAAAAAAAAAAAAAAAAAAA": "invalid id",
}


def _playlist_id(value: str) -> str:
    if len(value) == 22 and value.isalnum():
        return value
    try:
        classified = classify_url(value)
    except UnsupportedInput as exc:
        raise SystemExit(f"{value}: {exc.message}") from exc
    if classified.kind is not InputKind.SPOTIFY_PLAYLIST:
        raise SystemExit(f"{value}: not a Spotify playlist URL")
    return classified.identifier


def _summary(method: str, result: PlaylistExtraction) -> dict:
    available = [e for e in result.entries if e.available]
    reasons = Counter(e.unavailable_reason for e in result.entries if not e.available)
    ids = [e.source_id for e in result.entries if e.source_id]
    positions = [e.position for e in result.entries]
    return {
        "method": method,
        "ok": True,
        "title": result.title,
        "reported_total": result.reported_total,
        "extracted_total": result.extracted_total,
        "available": len(available),
        "unavailable": dict(reasons),
        "duplicate_track_ids": len(ids) - len(set(ids)),
        "positions_contiguous": positions == list(range(1, len(positions) + 1)),
        "missing_duration": sum(1 for e in available if e.duration_ms is None),
        "with_isrc": sum(1 for e in available if e.isrc),
        "extraction_complete": result.extraction_complete,
        "incomplete_reason": result.incomplete_reason,
        "requests": result.stats.requests,
        "pages": result.stats.pages,
        "seconds": result.stats.elapsed_seconds,
    }


async def run(playlists: list[str], methods: list[str], as_json: bool) -> int:
    settings = Settings.from_env()
    raw = httpx.AsyncClient(
        headers={"User-Agent": settings.http_user_agent}, timeout=None, follow_redirects=False
    )
    http = LimitedHttpClient(
        raw,
        max_retries=settings.http_max_retries,
        retry_backoff_seconds=settings.http_retry_backoff_seconds,
        retry_after_cap_seconds=settings.http_retry_after_cap_seconds,
        connect_timeout_seconds=settings.http_connect_timeout_seconds,
    )
    extractors = {
        "a": SpotifyDownloaderExtractor(http, settings),
        "b": SpotifyWebPlayerExtractor(http, settings),
        "embed": SpotifyEmbedExtractor(http, settings),
    }
    rows: list[dict] = []
    try:
        for playlist_id in playlists:
            for key in methods:
                extractor = extractors[key]
                started = time.monotonic()
                try:
                    result = await extractor.extract(playlist_id, 10_000)
                    row = _summary(extractor.name, result)
                except PlaylistError as exc:
                    row = {
                        "method": extractor.name,
                        "ok": False,
                        "error_code": exc.code,
                        "error": exc.message,
                        "requests": exc.requests,
                        "partial_extracted": exc.partial.extracted_total if exc.partial else 0,
                        "seconds": round(time.monotonic() - started, 3),
                    }
                row = {"playlist_id": playlist_id, "label": DEFAULT_PLAYLISTS.get(playlist_id, ""), **row}
                rows.append(row)
                if not as_json:
                    _print_row(row)
    finally:
        await http.aclose()
    if as_json:
        json.dump(rows, sys.stdout, indent=2, ensure_ascii=False)
        print()
    return 0


def _print_row(row: dict) -> None:
    head = f"{row['playlist_id']} [{row['label']}] {row['method']:>18}:"
    if not row["ok"]:
        print(
            f"{head} FAILED {row['error_code']} - {row['error']} "
            f"(requests={row['requests']}, partial={row['partial_extracted']}, {row['seconds']}s)"
        )
        return
    print(
        f"{head} reported={row['reported_total']} extracted={row['extracted_total']} "
        f"available={row['available']} unavailable={row['unavailable']} "
        f"dupes={row['duplicate_track_ids']} contiguous={row['positions_contiguous']} "
        f"complete={row['extraction_complete']} requests={row['requests']} "
        f"pages={row['pages']} {row['seconds']}s"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("playlists", nargs="*")
    parser.add_argument("--methods", default="a,b,embed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    playlists = [_playlist_id(p) for p in args.playlists] or list(DEFAULT_PLAYLISTS)
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    return asyncio.run(run(playlists, methods, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
