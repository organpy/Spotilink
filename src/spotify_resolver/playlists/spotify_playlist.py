"""Spotify playlist extraction orchestrator.

Order (SPOTIFY_PLAYLIST_EXTRACTOR=auto):
  1. spotify_downloader (Method A)
  2. webplayer (Method B), when SPOTIFY_WEBPLAYER_FALLBACK=true and Method A
     failed recoverably. Skipped when Method A was blocked (429/403), because
     both methods hit the same api-partner endpoint.
  3. spotify_embed partial data, when SPOTIFY_EMBED_PARTIAL_FALLBACK=true.
     Always reported as incomplete.

A not-found/private playlist is terminal and never triggers fallbacks. When
every step fails, the most informative error is raised; the best partial
extraction seen is attached to it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from ..config import Settings
from .models import (
    PlaylistEmpty,
    PlaylistError,
    PlaylistExtraction,
    PlaylistNotFound,
)
from .spotify_embed import SpotifyEmbedExtractor

logger = logging.getLogger(__name__)


class PlaylistExtractor(Protocol):
    name: str

    async def extract(self, playlist_id: str, max_tracks: int) -> PlaylistExtraction: ...


class SpotifyPlaylistService:
    def __init__(
        self,
        settings: Settings,
        *,
        primary: PlaylistExtractor,
        secondary: PlaylistExtractor | None,
        embed: SpotifyEmbedExtractor | None,
    ) -> None:
        self._settings = settings
        mode = settings.spotify_playlist_extractor
        if mode == "webplayer" and secondary is not None:
            chain: list[PlaylistExtractor] = [secondary]
            if settings.spotify_webplayer_fallback:
                chain.append(primary)
        else:
            chain = [primary]
            if mode == "auto" and secondary is not None and settings.spotify_webplayer_fallback:
                chain.append(secondary)
        self._chain = chain
        self._embed = embed if settings.spotify_embed_partial_fallback else None

    @property
    def extractor_names(self) -> list[str]:
        names = [extractor.name for extractor in self._chain]
        if self._embed is not None:
            names.append(self._embed.name)
        return names

    async def extract(self, playlist_id: str, max_tracks: int) -> PlaylistExtraction:
        errors: list[str] = []
        last_error: PlaylistError | None = None
        best_partial: PlaylistExtraction | None = None
        reported_total: int | None = None
        blocked = False

        for extractor in self._chain:
            if blocked:
                errors.append(f"{extractor.name}: skipped (shared endpoint blocked)")
                continue
            try:
                result = await extractor.extract(playlist_id, max_tracks)
            except (PlaylistNotFound, PlaylistEmpty):
                raise
            except PlaylistError as exc:
                logger.warning(
                    "Spotify playlist extractor %s failed for %s: %s",
                    extractor.name,
                    playlist_id,
                    exc.message,
                )
                errors.append(f"{extractor.name}: {exc.code}: {exc.message}")
                last_error = exc
                best_partial = _better(best_partial, exc.partial)
                if exc.partial is not None and exc.partial.reported_total is not None:
                    reported_total = exc.partial.reported_total
                if exc.blocked:
                    blocked = True
                if not exc.recoverable:
                    break
                continue
            if not result.entries:
                raise PlaylistEmpty()
            if result.extraction_complete or result.truncated:
                return _with_errors(result, errors)
            # Count mismatch: keep it, but let the next implementation try.
            errors.append(
                f"{extractor.name}: extracted {result.extracted_total} of "
                f"{result.reported_total} reported items"
            )
            best_partial = _better(best_partial, result)
            reported_total = result.reported_total

        if self._embed is not None:
            try:
                embedded = await self._embed.extract(
                    playlist_id, max_tracks, reported_total=reported_total
                )
            except (PlaylistNotFound, PlaylistEmpty):
                if best_partial is None:
                    raise
            except PlaylistError as exc:
                errors.append(f"{self._embed.name}: {exc.code}: {exc.message}")
                last_error = last_error or exc
            else:
                best_partial = _better(best_partial, embedded)

        if best_partial is not None:
            return _with_errors(best_partial, errors)
        assert last_error is not None
        last_error.message = "; ".join(errors) or last_error.message
        raise last_error


def _better(
    current: PlaylistExtraction | None, candidate: PlaylistExtraction | None
) -> PlaylistExtraction | None:
    if candidate is None:
        return current
    if current is None or candidate.extracted_total > current.extracted_total:
        return candidate
    return current


def _with_errors(result: PlaylistExtraction, errors: list[str]) -> PlaylistExtraction:
    if not errors:
        return result
    return replace(result, fallback_errors=tuple(errors))
