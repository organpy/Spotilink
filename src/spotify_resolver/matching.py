from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import quote_plus

from .config import Settings
from .models import Candidate, ScoreBreakdown, ScoredCandidate, SpotifyTrack

_DELIVERY_NOISE_SOURCE = (
    r"\b(?:"
    r"official\s+(?:music\s+)?video|"
    r"official\s+audio|"
    r"official\s+lyric\s+video|"
    r"official\s+visuali[sz]er|"
    r"official\s+video|"
    r"music\s+video|"
    r"lyric\s+video|"
    r"lyrics?|"
    r"visuali[sz]er|"
    r"audio|"
    r"m/v|"
    r"mv|"
    r"hd|"
    r"hq|"
    r"explicit|"
    r"clean(?:\s+version)?|"
    r"color\s+coded"
    r")\b"
)
_DELIVERY_NOISE_RE = re.compile(_DELIVERY_NOISE_SOURCE, re.IGNORECASE)
_BRACKETED_GROUP_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_TRAILING_DELIVERY_RE = re.compile(
    rf"(?<=\S)(?:\s*[-–—|:]\s*|\s+)(?:{_DELIVERY_NOISE_SOURCE})\s*$",
    re.IGNORECASE,
)

_BRACKETED_FEAT_RE = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\b[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_TRAILING_FEAT_RE = re.compile(
    r"\s+[-–—]?\s*(?:feat\.?|ft\.?|featuring)\s+.+$",
    re.IGNORECASE,
)
_TRAILING_SEPARATOR_RE = re.compile(r"\s*[-–—:]\s*$")

_TAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remix", re.compile(r"\b(?:re-?mix(?:ed)?|remix)\b", re.IGNORECASE)),
    ("live", re.compile(r"\blive\b", re.IGNORECASE)),
    ("instrumental", re.compile(r"\binstrumental\b", re.IGNORECASE)),
    ("karaoke", re.compile(r"\bkaraoke\b", re.IGNORECASE)),
    ("cover", re.compile(r"\bcover\b", re.IGNORECASE)),
    ("tribute", re.compile(r"\btribute\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\bacoustic\b", re.IGNORECASE)),
    ("unplugged", re.compile(r"\bunplugged\b", re.IGNORECASE)),
    ("demo", re.compile(r"\bdemo\b", re.IGNORECASE)),
    ("sped_up", re.compile(r"\b(?:sped\s*up|speed\s*up)\b", re.IGNORECASE)),
    ("slowed", re.compile(r"\bslowed\b", re.IGNORECASE)),
    ("nightcore", re.compile(r"\bnightcore\b", re.IGNORECASE)),
    ("8d", re.compile(r"\b8d\b", re.IGNORECASE)),
    ("bass_boost", re.compile(r"\bbass\s*boost(?:ed)?\b", re.IGNORECASE)),
    ("extended", re.compile(r"\bextended\b", re.IGNORECASE)),
    ("edit", re.compile(r"\b(?:radio\s+edit|club\s+edit|edit)\b", re.IGNORECASE)),
    ("remaster", re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE)),
)

_VERSION_PENALTIES: dict[str, float] = {
    "remix": 0.35,
    "live": 0.30,
    "instrumental": 0.40,
    "karaoke": 0.45,
    "cover": 0.40,
    "tribute": 0.40,
    "acoustic": 0.30,
    "unplugged": 0.30,
    "demo": 0.30,
    "sped_up": 0.35,
    "slowed": 0.35,
    "nightcore": 0.40,
    "8d": 0.35,
    "bass_boost": 0.35,
    "extended": 0.25,
    "edit": 0.25,
    "remaster": 0.02,
}

_MISSING_VERSION_PENALTIES: dict[str, float] = {
    "remix": 0.30,
    "live": 0.25,
    "instrumental": 0.30,
    "karaoke": 0.35,
    "cover": 0.30,
    "tribute": 0.30,
    "acoustic": 0.25,
    "unplugged": 0.25,
    "demo": 0.25,
    "sped_up": 0.25,
    "slowed": 0.25,
    "nightcore": 0.30,
    "8d": 0.25,
    "bass_boost": 0.25,
    "extended": 0.20,
    "edit": 0.20,
    "remaster": 0.02,
}

_CHANNEL_VERSION_TAGS = {
    "karaoke",
    "tribute",
    "cover",
    "nightcore",
    "instrumental",
    "8d",
    "bass_boost",
    "remix",
}

_ARTIST_SUFFIX_PATTERNS = (
    re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE),
    re.compile(r"\s*-?\s*vevo\s*$", re.IGNORECASE),
    re.compile(
        r"\s+official(?:\s+(?:channel|artist|music|audio|video))?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"\s+music\s*$", re.IGNORECASE),
    re.compile(r"\s+tv\s*$", re.IGNORECASE),
)

_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|\+|\bft\.?\b|\bfeat\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b)\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    threshold: float = 0.80
    weight_title: float = 0.50
    weight_artist: float = 0.35
    weight_duration: float = 0.15
    official_bonus: float = 0.06
    min_title_score: float = 0.65
    min_artist_score: float = 0.50
    no_duration_score_cap: float = 0.85
    require_duration: bool = True
    youtube_max_duration_diff_ms: int = 30_000
    soundcloud_max_duration_diff_ms: int = 15_000

    @classmethod
    def from_settings(cls, settings: Settings) -> ScoringConfig:
        return cls(
            threshold=settings.match_threshold,
            weight_title=settings.weight_title,
            weight_artist=settings.weight_artist,
            weight_duration=settings.weight_duration,
            official_bonus=settings.official_bonus,
            min_title_score=settings.min_title_score,
            min_artist_score=settings.min_artist_score,
            no_duration_score_cap=settings.no_duration_score_cap,
            require_duration=settings.require_duration,
            youtube_max_duration_diff_ms=settings.youtube_max_duration_diff_ms,
            soundcloud_max_duration_diff_ms=settings.soundcloud_max_duration_diff_ms,
        )

    def max_duration_diff_ms(self, platform: str) -> int:
        if platform == "soundcloud":
            return self.soundcloud_max_duration_diff_ms
        return self.youtube_max_duration_diff_ms


_EDITION_WORDS = frozenset(
    {
        "remaster", "remastered", "version", "single", "mono", "stereo",
        "album", "original", "digital", "deluxe", "edition",
    }
)
_EDITION_KEYWORDS = frozenset({"remaster", "remastered", "version", "mono", "stereo"})
_TRAILING_DASH_SEGMENT_RE = re.compile(r"\s+[-–—]\s+([^-–—()\[\]]+)$")


def _is_edition_noise(segment: str) -> bool:
    """True for pure edition annotations such as "Remastered 2010" or
    "Single Version;1990 Remastered Version". Anything containing another word
    (live, remix, acoustic, a venue...) is meaningful and kept."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", segment.casefold()) if t]
    if not tokens or not any(t in _EDITION_KEYWORDS for t in tokens):
        return False
    return all(t in _EDITION_WORDS or (t.isdigit() and len(t) == 4) for t in tokens)


def _strip_edition_noise(text: str) -> str:
    def bracket(match: re.Match[str]) -> str:
        return " " if _is_edition_noise(match.group(1)) else match.group(0)

    text = _BRACKETED_GROUP_RE.sub(bracket, text)
    match = _TRAILING_DASH_SEGMENT_RE.search(text)
    if match and _is_edition_noise(match.group(1)):
        text = text[: match.start()]
    return text


def clean_title(value: str) -> str:
    text = _strip_edition_noise(value)
    text = _BRACKETED_FEAT_RE.sub(" ", text)

    def strip_delivery_bracket(match: re.Match[str]) -> str:
        return " " if _DELIVERY_NOISE_RE.search(match.group(1)) else match.group(0)

    text = _BRACKETED_GROUP_RE.sub(strip_delivery_bracket, text)
    text = _TRAILING_DELIVERY_RE.sub("", text)
    text = _TRAILING_FEAT_RE.sub("", text)
    text = re.sub(r"[\(\[]\s*[\)\]]", " ", text)
    text = _TRAILING_SEPARATOR_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—:") or value.strip()


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold().replace("&", " and ")
    normalized = normalized.replace("’", "'").replace("`", "'")
    output: list[str] = []
    for char in normalized:
        output.append(char if char.isalnum() else " ")
    return " ".join("".join(output).split())


def compact_text(value: str) -> str:
    return "".join(char for char in normalize_text(value) if char.isalnum())


def title_similarity(left: str, right: str) -> float:
    left_clean = normalize_text(clean_title(left))
    right_clean = normalize_text(clean_title(right))
    if not left_clean or not right_clean:
        return 0.0
    if left_clean == right_clean:
        return 1.0
    compact_left = left_clean.replace(" ", "")
    compact_right = right_clean.replace(" ", "")
    if compact_left == compact_right:
        return 1.0
    shorter, longer = sorted((compact_left, compact_right), key=len)
    # Containment only counts when the contained title is a substantial part
    # of the other one; "Cafe" inside a long compilation title is no match.
    if len(shorter) >= 4 and shorter in longer and len(shorter) * 2 >= len(longer):
        return 0.95

    left_tokens = set(left_clean.split())
    right_tokens = set(right_clean.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, left_clean, right_clean).ratio()
    return _clamp(max(0.6 * jaccard + 0.4 * ratio, 0.95 * ratio))


def clean_artist_name(value: str) -> str:
    text = value.strip()
    for pattern in _ARTIST_SUFFIX_PATTERNS:
        text = pattern.sub("", text)
    text = text.strip(" -–—")
    return text or value.strip()


def artist_similarity(left: str, right: str) -> float:
    left_clean = clean_artist_name(left)
    right_clean = clean_artist_name(right)
    left_normalized = normalize_text(left_clean)
    right_normalized = normalize_text(right_clean)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    left_compact = left_normalized.replace(" ", "")
    right_compact = right_normalized.replace(" ", "")
    if left_compact == right_compact:
        return 1.0
    if min(len(left_compact), len(right_compact)) >= 4 and (
        left_compact in right_compact or right_compact in left_compact
    ):
        return 0.92

    left_tokens = set(left_normalized.split())
    right_tokens = set(right_normalized.split())
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    return _clamp(max(0.6 * jaccard + 0.4 * ratio, 0.95 * ratio))


def version_tags(value: str) -> tuple[str, ...]:
    found = [name for name, pattern in _TAG_PATTERNS if pattern.search(value)]
    return tuple(found)


def score_candidate(
    track: SpotifyTrack,
    candidate: Candidate,
    platform: str,
    config: ScoringConfig,
) -> ScoredCandidate:
    title_score = title_similarity(track.title, candidate.title)

    artist_names = _candidate_artist_variants(candidate)
    artist_score = 0.0
    for spotify_artist in track.artists:
        for candidate_artist in artist_names:
            artist_score = max(
                artist_score,
                artist_similarity(spotify_artist, candidate_artist),
            )

    duration_score, duration_diff, duration_rejected = _duration_score(
        track.duration_ms,
        candidate.duration_ms,
        config.max_duration_diff_ms(platform),
    )

    components: list[tuple[float, float]] = [
        (config.weight_title, title_score),
        (config.weight_artist, artist_score),
    ]
    if duration_score is not None:
        components.append((config.weight_duration, duration_score))
    denominator = sum(weight for weight, _ in components)
    base = sum(weight * value for weight, value in components) / denominator

    official_bonus = _official_bonus(candidate, platform, config)
    penalty, found_tags = _version_penalty(track.title, candidate)
    total = _clamp(base + official_bonus - penalty)
    capped = duration_score is None
    if capped:
        total = min(total, config.no_duration_score_cap)

    eligible = True
    rejection_reason: str | None = None
    if duration_score is None and config.require_duration:
        eligible = False
        rejection_reason = "duration_missing"
    elif duration_rejected:
        eligible = False
        rejection_reason = "duration_mismatch"
    elif title_score < config.min_title_score:
        eligible = False
        rejection_reason = "title_mismatch"
    elif artist_score < config.min_artist_score:
        eligible = False
        rejection_reason = "artist_mismatch"

    return ScoredCandidate(
        candidate=candidate,
        score=ScoreBreakdown(
            title=title_score,
            artist=artist_score,
            duration=duration_score,
            official_bonus=official_bonus,
            version_penalty=penalty,
            total=total,
            capped=capped,
            version_tags=found_tags,
            duration_diff_ms=duration_diff,
        ),
        eligible=eligible,
        rejection_reason=rejection_reason,
    )


def rank_candidates(
    track: SpotifyTrack,
    candidates: Sequence[Candidate],
    platform: str,
    config: ScoringConfig,
) -> list[ScoredCandidate]:
    scored: list[ScoredCandidate] = []
    seen_urls: set[str] = set()
    for candidate in candidates:
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        scored.append(score_candidate(track, candidate, platform, config))

    deduped: dict[tuple[str, str], ScoredCandidate] = {}
    for item in scored:
        candidate = item.candidate
        key = (
            normalize_text(clean_title(candidate.title)),
            compact_text(candidate.artist or candidate.uploader or ""),
        )
        existing = deduped.get(key)
        if existing is None or _candidate_sort_key(item) < _candidate_sort_key(
            existing
        ):
            deduped[key] = item

    return sorted(deduped.values(), key=_candidate_sort_key)


def best_accepted(
    ranked: Sequence[ScoredCandidate],
    config: ScoringConfig,
) -> ScoredCandidate | None:
    for item in ranked:
        if item.eligible and item.score.total >= config.threshold:
            return item
    return None


def build_queries(track: SpotifyTrack, platform: str, max_queries: int) -> list[str]:
    title = _query_value(clean_title(track.title))
    artist = _query_value(track.artists[0] if track.artists else "")
    if not title:
        return []
    if platform == "youtube":
        candidates = [
            f'"{artist}" "{title}" topic' if artist else f'"{title}" topic',
            f'"{artist}" "{title}"' if artist else f'"{title}"',
            f"{artist} {title} official audio".strip(),
        ]
    else:
        candidates = [
            f"{artist} {title}".strip(),
            title,
        ]
    result: list[str] = []
    for query in candidates:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in result:
            result.append(query[:200])
    return result[:max_queries]


def search_fallback_url(platform: str, track: SpotifyTrack) -> str:
    title = _query_value(clean_title(track.title))
    artist = _query_value(track.artists[0] if track.artists else "")
    query = quote_plus(" ".join(part for part in (artist, title) if part))
    if platform == "soundcloud":
        return f"https://soundcloud.com/search?q={query}"
    return f"https://www.youtube.com/results?search_query={query}"


def _query_value(value: str) -> str:
    return re.sub(r'["\r\n\t]+', " ", value).strip()


def _candidate_artist_variants(candidate: Candidate) -> list[str]:
    names: list[str] = []
    for value in (candidate.artist, candidate.uploader):
        if not value:
            continue
        names.append(value)
        names.extend(part for part in _ARTIST_SPLIT_RE.split(value) if part.strip())
    unique: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = clean_artist_name(name)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            unique.append(cleaned)
    return unique


def _duration_score(
    reference_ms: int | None,
    candidate_ms: int | None,
    max_diff_ms: int,
) -> tuple[float | None, int | None, bool]:
    if reference_ms is None or candidate_ms is None:
        return None, None, False
    diff = abs(reference_ms - candidate_ms)
    if diff > max_diff_ms:
        return 0.0, diff, True
    if diff <= 2_000:
        return 1.0, diff, False
    span = max(1, max_diff_ms - 2_000)
    score = 1.0 - 0.8 * (diff - 2_000) / span
    return _clamp(max(0.2, score)), diff, False


def _official_bonus(
    candidate: Candidate, platform: str, config: ScoringConfig
) -> float:
    if platform == "youtube":
        if candidate.is_topic:
            return config.official_bonus
        if candidate.is_vevo:
            return config.official_bonus * (2.0 / 3.0)
        if candidate.official_hint:
            return config.official_bonus / 3.0
        return 0.0
    if candidate.official_hint:
        return min(0.02, config.official_bonus / 3.0)
    return 0.0


def _version_penalty(
    spotify_title: str,
    candidate: Candidate,
) -> tuple[float, tuple[str, ...]]:
    expected = set(version_tags(spotify_title))
    found = set(version_tags(candidate.title))
    channel_tags = set(version_tags(candidate.artist or candidate.uploader or ""))
    found |= channel_tags & _CHANNEL_VERSION_TAGS

    penalty = 0.0
    for tag in found - expected:
        penalty += _VERSION_PENALTIES.get(tag, 0.2)
    for tag in expected - found:
        penalty += _MISSING_VERSION_PENALTIES.get(tag, 0.15)
    return min(0.70, penalty), tuple(sorted(found))


def _candidate_sort_key(item: ScoredCandidate) -> tuple[float, int, float, str]:
    candidate = item.candidate
    if candidate.is_topic:
        official_tier = 3
    elif candidate.is_vevo:
        official_tier = 2
    elif candidate.official_hint:
        official_tier = 1
    else:
        official_tier = 0
    duration_diff = item.score.duration_diff_ms
    return (
        -round(item.score.total, 6),
        -official_tier,
        duration_diff if duration_diff is not None else math.inf,
        candidate.url,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
