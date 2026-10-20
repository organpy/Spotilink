from __future__ import annotations

import httpx
from conftest import (
    TRACK_ID,
    TRACK_TITLE,
    FakeProvider,
    build_spotify_page,
    spotify_transport,
)
from fastapi.testclient import TestClient

from spotify_resolver.config import Settings
from spotify_resolver.errors import ProviderTimeout
from spotify_resolver.main import create_app
from spotify_resolver.models import Candidate

RESPONSE_KEYS = {
    "ok",
    "spotify_id",
    "title",
    "artists",
    "album",
    "duration_ms",
    "duration_precision",
    "spotify_url",
    "match",
    "search_fallback",
    "cached",
    "metadata_cached",
    "attempts",
    "error",
}


def _candidate(
    platform: str = "youtube",
    *,
    url: str = "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    artist: str = "Rick Astley - Topic",
) -> Candidate:
    return Candidate(
        platform=platform,
        url=url,
        title=TRACK_TITLE,
        artist=artist,
        uploader=artist,
        duration_ms=213_573,
        is_topic=platform == "youtube",
    )


def _client(
    settings: Settings,
    *,
    youtube: FakeProvider | None = None,
    soundcloud: FakeProvider | None = None,
    main_status: int = 200,
    embed_status: int = 200,
) -> TestClient:
    providers = {
        "youtube": youtube or FakeProvider("youtube", configured=False),
        "soundcloud": soundcloud or FakeProvider("soundcloud", configured=False),
    }
    app = create_app(
        settings,
        transport=spotify_transport(
            main_html=build_spotify_page(),
            main_status=main_status,
            embed_status=embed_status,
        ),
        providers=providers,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_resolve_returns_consistent_schema_and_caches(settings, sample_track) -> None:
    youtube = FakeProvider("youtube", [_candidate()])
    with _client(settings, youtube=youtube) as client:
        response = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body) == RESPONSE_KEYS
        assert body["ok"] is True
        assert body["spotify_id"] == TRACK_ID
        assert body["title"] == TRACK_TITLE
        assert body["artists"] == ["Rick Astley"]
        assert body["album"] == "Whenever You Need Somebody"
        assert body["duration_ms"] == 213_573
        assert body["match"]["platform"] == "youtube"
        assert body["match"]["url"] == _candidate().url
        assert body["match"]["confidence"] >= 0.8
        assert body["match"]["score"]["total"] == body["match"]["confidence"]
        assert body["search_fallback"] is None
        assert body["cached"] is False
        assert body["metadata_cached"] is False
        assert body["attempts"][0]["status"] == "verified"
        assert body["error"] is None

        cached = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
        assert cached.status_code == 200
        cached_body = cached.json()
        assert set(cached_body) == RESPONSE_KEYS
        assert cached_body["cached"] is True
        assert cached_body["metadata_cached"] is True
    assert len(youtube.calls) == 1


def test_http_client_does_not_send_or_store_cookies(
    settings,
    sample_track,
) -> None:
    seen_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            text=build_spotify_page(),
            headers={"set-cookie": "sp_t=anonymous-session; Path=/"},
        )

    app = create_app(
        settings,
        transport=httpx.MockTransport(handler),
        providers={
            "youtube": FakeProvider("youtube", [_candidate()]),
            "soundcloud": FakeProvider("soundcloud", configured=False),
        },
    )
    with TestClient(app) as client:
        app.state.raw_http.cookies.set("sp_t", "pre-existing")
        response = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
        assert response.status_code == 200
        assert seen_cookies == [None]
        assert list(app.state.raw_http.cookies.items()) == []


def test_missing_url_returns_400(settings) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get("/v1/resolve")
    assert response.status_code == 400
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["ok"] is False
    assert body["error"]["code"] == "missing_url"
    assert body["match"] is None


def test_invalid_platform_returns_400(settings, sample_track) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get(
            "/v1/resolve",
            params={"url": sample_track.url, "platform": "tidal"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_platform"


def test_unsupported_url_returns_422(settings) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get("/v1/resolve?url=https://open.spotify.com/album/abc")
    assert response.status_code == 422
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["error"]["code"] == "unsupported_spotify_url"


def test_spotify_not_found_returns_404(settings, sample_track) -> None:
    with _client(
        settings,
        youtube=FakeProvider("youtube", [_candidate()]),
        main_status=404,
    ) as client:
        response = client.get("/v1/resolve", params={"url": sample_track.url})
    assert response.status_code == 404
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["error"]["code"] == "spotify_track_not_found"
    assert body["spotify_id"] is None


def test_metadata_unavailable_returns_502(settings, sample_track) -> None:
    with _client(
        settings,
        youtube=FakeProvider("youtube", [_candidate()]),
        main_status=403,
        embed_status=403,
    ) as client:
        response = client.get("/v1/resolve", params={"url": sample_track.url})
    assert response.status_code == 502
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["error"]["code"] == "spotify_metadata_unavailable"
    assert body["match"] is None


def test_no_match_returns_200_with_unverified_fallback(settings, sample_track) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [])) as client:
        response = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["match"] is None
    assert body["search_fallback"]["verified"] is False
    assert body["search_fallback"]["platform"] == "youtube"
    assert body["attempts"][0]["status"] == "no_match"


def test_provider_timeout_returns_504(settings, sample_track) -> None:
    provider = FakeProvider("youtube", error=ProviderTimeout("timeout"))
    with _client(settings, youtube=provider) as client:
        response = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
    assert response.status_code == 504
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["ok"] is False
    assert body["error"]["code"] == "providers_timeout"
    assert body["error"]["retryable"] is True
    assert body["search_fallback"]["verified"] is False


def test_cross_platform_fallback(settings, sample_track) -> None:
    youtube = FakeProvider("youtube", [])
    soundcloud = FakeProvider(
        "soundcloud",
        [
            _candidate(
                "soundcloud",
                url="https://soundcloud.com/rick-astley/never-gonna-give-you-up",
                artist="Rick Astley",
            )
        ],
    )
    with _client(settings, youtube=youtube, soundcloud=soundcloud) as client:
        response = client.get(
            "/v1/resolve",
            params={"url": sample_track.url, "platform": "youtube", "fallback": "true"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["match"]["platform"] == "soundcloud"
    assert [(a["platform"], a["status"]) for a in body["attempts"]] == [
        ("youtube", "no_match"),
        ("soundcloud", "verified"),
    ]


def test_no_provider_configured_returns_503(settings, sample_track) -> None:
    with _client(settings) as client:
        response = client.get("/v1/resolve", params={"url": sample_track.url})
    assert response.status_code == 503
    body = response.json()
    assert set(body) == RESPONSE_KEYS
    assert body["error"]["code"] == "providers_not_configured"
    assert body["match"] is None


def test_invalid_boolean_returns_400(settings, sample_track) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get(
            "/v1/resolve",
            params={"url": sample_track.url, "fallback": "not-a-bool"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_query_parameter"


def test_health_reports_configuration(settings) -> None:
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["providers"]["youtube"] is True
    assert body["providers"]["soundcloud"] is False


def test_health_is_degraded_without_providers(settings) -> None:
    with _client(settings) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_unknown_route_uses_error_envelope(settings) -> None:
    with _client(settings) as client:
        response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert set(response.json()) == RESPONSE_KEYS
    assert response.json()["error"]["code"] == "not_found"


def test_cors_is_applied(make_settings) -> None:
    settings = make_settings(cors_origins=("https://app.example",))
    with _client(settings, youtube=FakeProvider("youtube", [_candidate()])) as client:
        response = client.get(
            "/healthz",
            headers={"Origin": "https://app.example"},
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example"


def test_inbound_rate_limit(settings, sample_track, make_settings, tmp_path) -> None:
    limited_settings = make_settings(
        database_path=tmp_path / "rate-limit.db",
        api_rate_limit_per_minute=2,
    )
    youtube = FakeProvider("youtube", [_candidate()])
    with _client(limited_settings, youtube=youtube) as client:
        first = client.get("/v1/resolve", params={"url": sample_track.url})
        second = client.get("/v1/resolve", params={"url": sample_track.url})
        third = client.get("/v1/resolve", params={"url": sample_track.url})
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["retry-after"]
    assert third.json()["error"]["code"] == "rate_limited"


def test_resolution_timeout(make_settings, sample_track) -> None:
    settings = make_settings(resolve_timeout_seconds=0.05)
    provider = FakeProvider("youtube", [_candidate()], delay=0.2)
    with _client(settings, youtube=provider) as client:
        response = client.get(
            "/v1/resolve",
            params={
                "url": sample_track.url,
                "platform": "youtube",
                "fallback": "false",
            },
        )
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "resolution_timeout"
