<div align="center">
  <a href="https://destro.aetherics.dev/">
    <img src="spotilink.svg" alt="Spotilink banner" width="500">
  </a>
  <h1></h1>
  <p>A server-side API for <a href="https://github.com/AymennX/Destro">Destro</a>. It reads public Spotify track metadata, searches official YouTube and SoundCloud APIs, verifies candidates, and caches the results in SQLite. It never downloads, proxies, streams, or stores audio.</p>
  <a href="https://destro.aetherics.dev/">
    <img src="trydestro.png" alt="Try Destro" width="200">
  </a>
</div>

## Quick start

You need Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and at least one provider API key.

```bash
uv sync --all-groups
cp .env.example .env
# Set YOUTUBE_API_KEY and/or SOUNDCLOUD_CLIENT_ID + SOUNDCLOUD_CLIENT_SECRET.
uv run uvicorn spotify_resolver.main:app --host 127.0.0.1 --port 8000
```

SoundCloud requires an Artist Pro account to register API credentials. YouTube-only works without SoundCloud.

- Showcase: <http://127.0.0.1:8000/>
- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/healthz>
- Example resolve: <http://127.0.0.1:8000/v1/resolve?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC>

## API

```http
GET /v1/resolve?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC&platform=youtube
```

A successful lookup returns a verified `match`; when nothing clears the confidence threshold, Spotilink returns an unverified `search_fallback` instead. Errors use the same JSON envelope.

Parameters, response examples, status codes, and stale-cache behavior are in the **[API reference](docs/api.md)**.

## How it works

Spotilink fetches the public Spotify track page, searches official provider APIs, and verifies title, artist, and duration before caching a result.

Want to know how it works in depth? Read the **[development notes](docs/development.md)** for matching rules, quota limits, stale-if-error caching, configuration, project layout, and operational notes.

## Documentation

- [API reference](docs/api.md)
- [Development notes](docs/development.md)
- [Architecture](docs/architecture.md)
- [Provider terms](docs/provider-terms.md)

## License

MIT. See [LICENSE](LICENSE). Spotilink is not affiliated with Spotify, YouTube, Google, or SoundCloud.
