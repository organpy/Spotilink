"""In-memory fake of the Spotify endpoints used by playlist extraction."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx

from spotify_resolver.playlists.spotify_downloader_adapter import FETCH_PLAYLIST_SHA256

WEBPLAYER_HASH = "b" * 64
BUNDLE_URL = "https://open.spotifycdn.com/cdn/build/mobile-web-player/mobile-web-player.abc123ef.js"


def track_id(n: int) -> str:
    return f"{n:022d}".replace("0", "A")


def track_item(n: int, *, title: str | None = None, artists: tuple[str, ...] | None = None) -> dict:
    return {
        "itemV2": {
            "__typename": "TrackResponseWrapper",
            "data": {
                "__typename": "Track",
                "uri": f"spotify:track:{track_id(n)}",
                "name": title if title is not None else f"Song {n}",
                "artists": {
                    "items": [
                        {"profile": {"name": a}} for a in (artists if artists is not None else (f"Artist {n}",))
                    ]
                },
                "albumOfTrack": {"name": f"Album {n}"},
                "trackDuration": {"totalMilliseconds": 180_000 + n},
            },
        }
    }


def next_data_page(*, token: str = "tok-1", expires_ms: int = 4_102_444_800_000, entity: Any = None, status: int | None = None) -> str:
    props: dict[str, Any] = {
        "state": {
            "settings": {
                "session": {
                    "accessToken": token,
                    "accessTokenExpirationTimestampMs": expires_ms,
                    "isAnonymous": True,
                }
            },
            "data": {"entity": entity} if entity is not None else {},
        }
    }
    if status is not None:
        props = {"status": status}
    doc = {"props": {"pageProps": props}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(doc)}</script></html>'


class FakeSpotifyPlaylists:
    """Serves embed pages, the web-player shell/bundle and both pathfinder APIs."""

    def __init__(self, items: list[dict], *, name: str = "Test Playlist", total: int | None = None) -> None:
        self.items = items
        self.name = name
        self.total = len(items) if total is None else total
        self.requests: list[str] = []
        self.valid_tokens = {"tok-1", "tok-2"}
        self.token_counter = 0
        self.accepted_hashes = {FETCH_PLAYLIST_SHA256, WEBPLAYER_HASH}
        self.bundle_hash = WEBPLAYER_HASH
        self.not_found = False
        self.generic_error: str | None = None
        self.fail_at_offset: dict[int, int] = {}  # offset -> HTTP status
        self.graphql_errors = False
        self.embed_items: list[dict] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _union(self, offset: int, limit: int) -> dict:
        if self.not_found:
            return {"data": {"playlistV2": {"__typename": "NotFound", "message": "nope"}}}
        if self.generic_error:
            return {"data": {"playlistV2": {"__typename": "GenericError", "message": self.generic_error}}}
        if self.graphql_errors:
            return {"errors": [{"message": "Cannot query field"}], "data": None}
        return {
            "data": {
                "playlistV2": {
                    "__typename": "Playlist",
                    "name": self.name,
                    "description": "A <b>test</b> &amp; playlist",
                    "ownerV2": {"data": {"__typename": "User", "name": "Owner"}},
                    "content": {
                        "totalCount": self.total,
                        "items": self.items[offset : offset + limit],
                    },
                }
            }
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.requests.append(f"{request.method} {url.host}{url.path}")
        if url.host == "open.spotify.com":
            if url.path.startswith("/embed/"):
                if self.not_found and url.path.startswith("/embed/playlist/"):
                    return httpx.Response(200, text=next_data_page(status=404))
                self.token_counter += 1
                token = "tok-1" if self.token_counter == 1 else "tok-2"
                entity = None
                if url.path.startswith("/embed/playlist/"):
                    entity = {
                        "name": self.name,
                        "subtitle": "Owner",
                        "trackList": self.embed_items
                        if self.embed_items is not None
                        else [_embed_track(i) for i in self.items[:100]],
                    }
                return httpx.Response(200, text=next_data_page(token=token, entity=entity))
            if url.path == "/":
                return httpx.Response(200, text=f'<html><script src="{BUNDLE_URL}"></script></html>')
        if url.host == "open.spotifycdn.com":
            return httpx.Response(
                200, text=f'x=new a.l("queryPlaylist","query","{self.bundle_hash}",null);'
            )
        if url.host == "api-partner.spotify.com":
            auth = request.headers.get("authorization", "")
            if auth.removeprefix("Bearer ") not in self.valid_tokens:
                return httpx.Response(401, json={"error": {"status": 401}})
            if request.method == "GET":
                params = parse_qs(url.query.decode())
                variables = json.loads(params["variables"][0])
                sha = json.loads(params["extensions"][0])["persistedQuery"]["sha256Hash"]
            else:
                body = json.loads(request.content)
                variables = body["variables"]
                sha = body["extensions"]["persistedQuery"]["sha256Hash"]
            if sha not in self.accepted_hashes:
                return httpx.Response(412, text='"Invalid query hash"')
            offset, limit = variables["offset"], variables["limit"]
            if offset in self.fail_at_offset:
                return httpx.Response(self.fail_at_offset[offset], text="err")
            return httpx.Response(200, json=self._union(offset, limit))
        return httpx.Response(404)


def _embed_track(item: dict) -> dict:
    data = ((item or {}).get("itemV2") or {}).get("data") or {}
    return {
        "uri": data.get("uri"),
        "title": data.get("name"),
        "subtitle": ", ".join(
            a["profile"]["name"] for a in (data.get("artists") or {}).get("items", [])
        ),
        "duration": (data.get("trackDuration") or {}).get("totalMilliseconds"),
    }
