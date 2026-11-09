from __future__ import annotations

import time
from dataclasses import replace

import pytest
from conftest import FakeProvider, build_spotify_page, spotify_transport, ytm_candidate
from fastapi.testclient import TestClient
from spotify_fakes import FakeSpotifyPlaylists, track_id, track_item

from spotify_resolver.errors import ProviderRateLimited
from spotify_resolver.main import create_app

SP_PLAYLIST = "https://open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U"
YT_PLAYLIST = "https://www.youtube.com/playlist?list=PL4fGSI1pDJn6O1LS0XSdF3RyO0Rq_LDeI"


def vid(n: int) -> str:
    return f"{n:011d}".replace("0", "v")


class SmartYTMusic:
    """Returns a matching candidate for 'Artist n Song n' unless n is special."""

    configured = True

    def __init__(self, unmatched=(), failing=()) -> None:
        self.unmatched, self.failing = set(unmatched), set(failing)
        self.calls: list[str] = []

    async def search(self, query, kind, limit):
        self.calls.append(query)
        n = int(query.rsplit(" ", 1)[1])
        if n in self.failing:
            raise ProviderRateLimited("throttled", upstream=True)
        if n in self.unmatched:
            return []
        if kind == "videos":
            return []
        return [ytm_candidate(f"Song {n}", f"Artist {n}", video_id=vid(n), duration_ms=180_000 + n)]


def make_client(settings, fake: FakeSpotifyPlaylists | None = None, ytm=None, yt_runner=None, **kw):
    fake = fake or FakeSpotifyPlaylists([track_item(i) for i in range(1, 6)])
    app = create_app(
        settings,
        transport=fake.transport(),
        providers={"youtube": FakeProvider("youtube", configured=False), "soundcloud": FakeProvider("soundcloud", configured=False)},
        ytmusic=ytm if ytm is not None else SmartYTMusic(),
        youtube_playlist_runner=yt_runner,
        **kw,
    )
    return TestClient(app, raise_server_exceptions=False), fake


def wait_final(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v2/jobs/{job_id}").json()
        if body["status"] in ("completed", "partial", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job did not finish: {body}")


@pytest.fixture
def v2settings(make_settings):
    return make_settings(yt_music_enabled=True, yt_music_concurrency=3, yt_music_retry_backoff_seconds=0.0)


def test_spotify_playlist_job_end_to_end(v2settings) -> None:
    with make_client(v2settings)[0] as client:
        response = client.post("/v2/resolve", json={"url": SP_PLAYLIST})
        assert response.status_code == 202
        created = response.json()
        assert created["type"] == "playlist" and created["source"] == "spotify"
        assert created["status_url"] == f"/v2/jobs/{created['job_id']}"
        assert response.headers["location"] == created["status_url"]

        job = wait_final(client, created["job_id"])
        assert job["status"] == "completed"
        assert job["playlist"]["reported_total"] == 5
        assert job["playlist"]["extraction_complete"] is True
        assert job["progress"] == {
            "processed": 5, "total": 5, "matched": 5, "uncertain": 0,
            "unmatched": 0, "unavailable": 0, "failed": 0, "pending": 0,
        }
        assert job["extractor"] == "spotify_downloader" and job["resolver"] == "ytmusicapi"

        page = client.get(f"/v2/jobs/{created['job_id']}/tracks").json()
        assert page["total"] == 5 and page["next_offset"] is None
        first = page["tracks"][0]
        assert first["position"] == 1 and first["spotify_id"] == track_id(1)
        assert first["youtube_url"] == f"https://www.youtube.com/watch?v={vid(1)}"
        assert first["youtube_id"] == vid(1)
        assert first["provider"] == "ytmusicapi" and first["status"] == "matched"
        assert first["confidence"] >= 0.75


def test_partial_matches_keep_order_duplicates_and_do_not_abort(v2settings) -> None:
    items = [track_item(1), track_item(2), track_item(1), track_item(3), track_item(4), track_item(5)]
    ytm = SmartYTMusic(unmatched={2}, failing={4})
    fake = FakeSpotifyPlaylists(items)
    with make_client(v2settings, fake, ytm)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        job = wait_final(client, job_id)
        assert job["status"] == "partial"
        assert job["playlist"]["extraction_complete"] is True  # extraction fine, matches not
        progress = job["progress"]
        # positions: 1,3 are the duplicate (matched), 2 unmatched, 4 -> Song 3, 5 -> Song 4 (fails), 6 -> Song 5
        assert (progress["matched"], progress["unmatched"], progress["failed"]) == (4, 1, 1)
        tracks = client.get(f"/v2/jobs/{job_id}/tracks").json()["tracks"]
        assert [t["position"] for t in tracks] == [1, 2, 3, 4, 5, 6]
        assert [t["spotify_id"] for t in tracks][:3] == [track_id(1), track_id(2), track_id(1)]
        assert tracks[0]["youtube_url"] == tracks[2]["youtube_url"]
        assert tracks[1]["status"] == "unmatched" and tracks[1]["youtube_url"] is None
        assert tracks[4]["status"] == "failed"
        # Duplicate track searched once.
        assert sum(q.endswith(" 1") for q in ytm.calls) == 1  # one search for both occurrences


def test_incomplete_extraction_is_reported_separately(v2settings) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 4)], total=10)
    with make_client(v2settings, fake)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        job = wait_final(client, job_id)
        assert job["status"] == "partial"
        assert job["playlist"]["extraction_complete"] is False
        assert job["playlist"]["reported_total"] == 10 and job["playlist"]["extracted_total"] == 3
        assert job["progress"]["matched"] == 3


def test_unavailable_tracks_are_listed(v2settings) -> None:
    removed = {"itemV2": {"data": {"__typename": "Track", "uri": f"spotify:track:{track_id(9)}", "artists": {"items": []}}}}
    fake = FakeSpotifyPlaylists([track_item(1), removed])
    with make_client(v2settings, fake)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        job = wait_final(client, job_id)
        tracks = client.get(f"/v2/jobs/{job_id}/tracks").json()["tracks"]
        assert tracks[1]["status"] == "unavailable" and tracks[1]["detail"] == "removed"
        assert job["progress"]["unavailable"] == 1


def test_pagination(v2settings) -> None:
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 26)])
    with make_client(v2settings, fake)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        wait_final(client, job_id)
        seen, offset = [], 0
        while offset is not None:
            page = client.get(f"/v2/jobs/{job_id}/tracks", params={"offset": offset, "limit": 10}).json()
            seen += [t["position"] for t in page["tracks"]]
            offset = page["next_offset"]
        assert seen == list(range(1, 26))
        assert client.get(f"/v2/jobs/{job_id}/tracks", params={"limit": 0}).status_code == 400
        assert client.get(f"/v2/jobs/{job_id}/tracks", params={"limit": 9999}).status_code == 400
        empty = client.get(f"/v2/jobs/{job_id}/tracks", params={"offset": 100}).json()
        assert empty["tracks"] == [] and empty["next_offset"] is None


def test_results_visible_while_processing(v2settings) -> None:
    import asyncio

    gate = {"open": False}

    class Slow(SmartYTMusic):
        async def search(self, query, kind, limit):
            n = int(query.rsplit(" ", 1)[1])
            if n >= 3:
                while not gate["open"]:
                    await asyncio.sleep(0.01)
            return await super().search(query, kind, limit)

    with make_client(v2settings, ytm=Slow())[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            progress = client.get(f"/v2/jobs/{job_id}").json()
            if progress["status"] == "matching" and progress["progress"]["matched"] >= 2:
                break
            time.sleep(0.05)
        assert progress["status"] == "matching"
        assert progress["progress"]["pending"] >= 1
        tracks = client.get(f"/v2/jobs/{job_id}/tracks").json()["tracks"]
        assert any(t["status"] == "matched" for t in tracks) and any(t["status"] == "pending" for t in tracks)
        gate["open"] = True
        assert wait_final(client, job_id)["status"] == "completed"


def test_cached_job_is_reused_and_tracks_cached(v2settings) -> None:
    ytm = SmartYTMusic()
    with make_client(v2settings, ytm=ytm)[0] as client:
        first = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()
        wait_final(client, first["job_id"])
        calls = len(ytm.calls)
        second = client.post("/v2/resolve", json={"url": SP_PLAYLIST + "?si=zzz"}).json()
        assert second["job_id"] == first["job_id"] and second["reused"] is True
        assert second["status"] == "completed"
        assert len(ytm.calls) == calls


def test_resolution_cache_shared_across_playlists(v2settings) -> None:
    ytm = SmartYTMusic()
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 4)])
    with make_client(v2settings, fake, ytm)[0] as client:
        a = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        wait_final(client, a)
        calls = len(ytm.calls)
        other = "https://open.spotify.com/playlist/37i9dQZF1DX4UtSsGT1Sbe"
        b = client.post("/v2/resolve", json={"url": other}).json()["job_id"]
        assert b != a
        job = wait_final(client, b)
        assert job["status"] == "completed" and len(ytm.calls) == calls


def test_private_playlist_fails_job_with_code(v2settings) -> None:
    fake = FakeSpotifyPlaylists([])
    fake.not_found = True
    with make_client(v2settings, fake)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        job = wait_final(client, job_id)
        assert job["status"] == "failed" and job["error"]["code"] == "playlist_not_found"


def test_youtube_playlist_job(v2settings) -> None:
    info = {
        "_type": "playlist", "title": "YT", "playlist_count": 3,
        "entries": [
            {"id": "aaaaaaaaaaa", "title": "One", "channel": "C", "duration": 100},
            {"id": "bbbbbbbbbbb", "title": "[Private video]"},
            {"id": "aaaaaaaaaaa", "title": "One", "channel": "C", "duration": 100},
        ],
    }
    seen = []

    def runner(url, options):
        seen.append(options)
        return info

    ytm = SmartYTMusic()
    with make_client(v2settings, yt_runner=runner, ytm=ytm)[0] as client:
        created = client.post("/v2/resolve", json={"url": YT_PLAYLIST}).json()
        assert created["source"] == "youtube"
        job = wait_final(client, created["job_id"])
        assert job["status"] == "completed" and job["extractor"] == "yt-dlp"
        assert job["resolver"] == "yt-dlp"
        tracks = client.get(created["tracks_url"]).json()["tracks"]
        assert [t["position"] for t in tracks] == [1, 2, 3]
        assert tracks[0]["youtube_url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        assert tracks[0]["youtube_url"] == tracks[2]["youtube_url"]
        assert tracks[1]["status"] == "unavailable" and tracks[1]["detail"] == "private"
        assert ytm.calls == []  # ytmusicapi is never used for YouTube playlists
        assert seen[0]["extract_flat"] == "in_playlist" and seen[0]["skip_download"] is True


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube_video_not_supported"),
        ("https://evil.example/open.spotify.com/playlist/37i9dQZF1DWXRqgorJj26U", "unsupported_input"),
        ("not a url", "invalid_url"),
    ],
)
def test_rejected_inputs_invoke_no_resolver(v2settings, url, code) -> None:
    ytm = SmartYTMusic()
    fake = FakeSpotifyPlaylists([track_item(1)])
    client, _ = make_client(v2settings, fake, ytm)
    with client:
        response = client.post("/v2/resolve", json={"url": url})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
        assert response.json()["ok"] is False
        assert ytm.calls == [] and fake.requests == []


def test_invalid_body_and_unknown_job(v2settings) -> None:
    with make_client(v2settings)[0] as client:
        assert client.post("/v2/resolve", json={}).status_code == 400
        assert client.post("/v2/resolve", json={"url": 5}).status_code == 400
        missing = client.get("/v2/jobs/job_doesnotexist")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "job_not_found"
        assert client.get("/v2/jobs/../etc/passwd").status_code == 404


def test_spotify_track_resolves_synchronously(v2settings) -> None:
    app = create_app(
        v2settings,
        transport=spotify_transport(main_html=build_spotify_page()),
        providers={"youtube": FakeProvider("youtube", configured=False), "soundcloud": FakeProvider("soundcloud", configured=False)},
        ytmusic=_TrackYTM(),
    )
    with TestClient(app) as client:
        response = client.post("/v2/resolve", json={"url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"})
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "track"
        resolution = body["resolution"]
        assert resolution["ok"] and resolution["match"]["url"].endswith("dQw4w9WgXcQ")


class _TrackYTM:
    configured = True

    async def search(self, query, kind, limit):
        return [ytm_candidate(duration_ms=213_573)] if kind == "songs" else []


def test_v1_endpoint_unchanged(v2settings) -> None:
    app = create_app(
        v2settings,
        transport=spotify_transport(main_html=build_spotify_page()),
        providers={"youtube": FakeProvider("youtube", configured=False), "soundcloud": FakeProvider("soundcloud", configured=False)},
        ytmusic=_TrackYTM(),
    )
    with TestClient(app) as client:
        body = client.get("/v1/resolve", params={"url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"}).json()
        assert set(body) == {
            "ok", "spotify_id", "title", "artists", "album", "duration_ms", "duration_precision",
            "spotify_url", "match", "search_fallback", "cached", "stale", "metadata_cached",
            "attempts", "error",
        }
        assert client.get("/healthz").status_code in (200, 503)


def test_health_exposes_no_secrets(v2settings) -> None:
    settings = replace(v2settings, youtube_api_key="SECRET-KEY-123")
    with make_client(settings)[0] as client:
        response = client.get("/v2/health")
        assert response.status_code == 200
        text = response.text
        assert "SECRET-KEY-123" not in text and "tok-" not in text
        body = response.json()
        assert body["providers"]["ytmusicapi"]["enabled"] is True
        assert body["providers"]["youtube_data_api"]["circuit"]["state"] == "closed"
        assert "spotify_downloader" in body["providers"]["spotify_playlist_extractors"]


def test_queue_full_and_rate_limit(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, playlist_max_pending_jobs=1, v2_rate_limit_per_minute=3)
    client, _ = make_client(settings, start_workers=False)
    with client:
        first = client.post("/v2/resolve", json={"url": SP_PLAYLIST})
        assert first.status_code == 202
        full = client.post("/v2/resolve", json={"url": "https://open.spotify.com/playlist/37i9dQZF1DX4UtSsGT1Sbe"})
        assert full.status_code == 503 and full.json()["error"]["code"] == "queue_full"
        assert "retry-after" in full.headers
        client.post("/v2/resolve", json={"url": SP_PLAYLIST})
        limited = client.post("/v2/resolve", json={"url": SP_PLAYLIST})
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"


def test_playlist_larger_than_limit_is_truncated(make_settings) -> None:
    settings = make_settings(yt_music_enabled=True, playlist_max_tracks=3)
    fake = FakeSpotifyPlaylists([track_item(i) for i in range(1, 8)])
    with make_client(settings, fake)[0] as client:
        job_id = client.post("/v2/resolve", json={"url": SP_PLAYLIST}).json()["job_id"]
        job = wait_final(client, job_id)
        assert job["status"] == "partial"
        assert job["playlist"]["truncated"] is True
        assert job["playlist"]["extraction_complete"] is False
        assert job["playlist"]["incomplete_reason"] == "max_tracks"
        assert job["progress"]["total"] == 3


def test_matching_disabled_rejects_spotify_playlists(make_settings) -> None:
    settings = make_settings(yt_music_enabled=False)
    app = create_app(
        settings,
        transport=FakeSpotifyPlaylists([track_item(1)]).transport(),
        providers={"youtube": FakeProvider("youtube", configured=False), "soundcloud": FakeProvider("soundcloud", configured=False)},
    )
    with TestClient(app) as client:
        response = client.post("/v2/resolve", json={"url": SP_PLAYLIST})
        assert response.status_code == 503 and response.json()["error"]["code"] == "matcher_unavailable"
