"""Podcast distribution: RSS feed generation.

One feed per episode profile (the "show"). Only published episodes that have an
audio file appear in a feed. Feeds are RSS 2.0 with the iTunes namespace so they
can be submitted to Apple Podcasts / Spotify.

Absolute URLs (enclosures, cover art) are built from PUBLIC_BASE_URL so podcast
directories can fetch the audio from outside the container.
"""

import hashlib
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from open_notebook.config import PUBLIC_BASE_URL
from open_notebook.database.repository import parse_record_ids, repo_query
from open_notebook.podcasts.audio_paths import resolve_contained_audio_path

_ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM_NS = "http://www.w3.org/2005/Atom"
_CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

# Owner block (Apple requires an email at submission time). Configurable via env.
_OWNER_NAME = os.environ.get("PODCAST_OWNER_NAME", "").strip() or "Dīwān"
_OWNER_EMAIL = os.environ.get("PODCAST_OWNER_EMAIL", "").strip()
_DEFAULT_CATEGORY = "Education"


def _slugify(name: str) -> str:
    """Stable ASCII slug for a series name. Non-ASCII (e.g. Arabic) names fall
    back to a short hash so the slug stays URL-safe and deterministic."""
    norm = unicodedata.normalize("NFKD", name or "")
    ascii_only = norm.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    if not slug:
        digest = hashlib.md5((name or "").encode("utf-8")).hexdigest()[:10]
        slug = f"show-{digest}"
    return slug


def _audio_duration_seconds(path: Path) -> Optional[int]:
    """Audio duration in whole seconds via ffprobe. Best-effort (None on failure)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=20,
        )
        value = out.stdout.strip()
        if value:
            return int(float(value))
    except Exception as e:  # noqa: BLE001 - duration is optional metadata
        logger.warning(f"[feed] ffprobe failed for {path}: {e}")
    return None


def _fmt_duration(seconds: Optional[int]) -> Optional[str]:
    if not seconds or seconds <= 0:
        return None
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def _abs_url(path_or_url: Optional[str]) -> Optional[str]:
    if not path_or_url:
        return None
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return f"{PUBLIC_BASE_URL}/{path_or_url.lstrip('/')}"


def _episode_pub_dt(ep: Dict[str, Any]) -> datetime:
    raw = ep.get("published_at") or ep.get("created")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def _all_profiles() -> List[Dict[str, Any]]:
    rows = await repo_query("SELECT * FROM episode_profile")
    return parse_record_ids(rows) or []


async def _published_episodes(series_name: str) -> List[Dict[str, Any]]:
    rows = await repo_query(
        "SELECT * FROM episode "
        "WHERE published = true AND audio_file != NONE "
        "AND episode_profile.name = $name",
        {"name": series_name},
    )
    eps = parse_record_ids(rows) or []
    eps.sort(key=_episode_pub_dt, reverse=True)
    return eps


async def list_series() -> List[Dict[str, Any]]:
    """List shows (episode profiles) with their published-episode counts.

    `ready` means the feed is complete enough for directory submission
    (has a cover image and at least one published episode)."""
    profiles = await _all_profiles()
    series: List[Dict[str, Any]] = []
    for p in profiles:
        name = p.get("name")
        if not name:
            continue
        eps = await _published_episodes(name)
        slug = _slugify(name)
        series.append(
            {
                "slug": slug,
                "name": name,
                "description": p.get("description"),
                "image": p.get("show_image"),
                "author": p.get("show_author"),
                "category": p.get("show_category") or _DEFAULT_CATEGORY,
                "language": p.get("language"),
                "explicit": bool(p.get("show_explicit")),
                "episode_count": len(eps),
                "feed_url": f"{PUBLIC_BASE_URL}/api/podcasts/feed/{slug}.xml",
                "ready": bool(p.get("show_image")) and len(eps) > 0,
            }
        )
    return series


async def _find_profile_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    for p in await _all_profiles():
        if _slugify(p.get("name", "")) == slug:
            return p
    return None


def _el(tag: str, text: Optional[str]) -> str:
    if text is None or text == "":
        return ""
    return f"    <{tag}>{escape(str(text))}</{tag}>\n"


async def build_feed(slug: str) -> Optional[str]:
    """Build the RSS 2.0 (+iTunes) XML for a series slug, or None if unknown."""
    profile = await _find_profile_by_slug(slug)
    if profile is None:
        return None

    name = profile.get("name", "Podcast")
    episodes = await _published_episodes(name)
    language = (profile.get("language") or "ar").strip()
    description = profile.get("description") or name
    author = profile.get("show_author") or _OWNER_NAME
    category = profile.get("show_category") or _DEFAULT_CATEGORY
    explicit = "true" if profile.get("show_explicit") else "false"
    image = _abs_url(profile.get("show_image"))
    feed_url = f"{PUBLIC_BASE_URL}/api/podcasts/feed/{quote(slug)}.xml"

    parts: List[str] = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>\n')
    parts.append(
        f'<rss version="2.0" xmlns:itunes={quoteattr(_ITUNES_NS)} '
        f"xmlns:content={quoteattr(_CONTENT_NS)} "
        f"xmlns:atom={quoteattr(_ATOM_NS)}>\n"
    )
    parts.append("  <channel>\n")
    parts.append(_el("title", name))
    parts.append(f"    <link>{escape(PUBLIC_BASE_URL)}</link>\n")
    parts.append(_el("language", language))
    parts.append(_el("description", description))
    parts.append(
        f"    <atom:link href={quoteattr(feed_url)} rel=\"self\" "
        'type="application/rss+xml"/>\n'
    )
    parts.append(_el("itunes:author", author))
    parts.append(_el("itunes:summary", description))
    parts.append(f"    <itunes:explicit>{explicit}</itunes:explicit>\n")
    if image:
        parts.append(f"    <itunes:image href={quoteattr(image)}/>\n")
    parts.append(f"    <itunes:category text={quoteattr(category)}/>\n")
    owner = "    <itunes:owner>\n" + (
        f"      <itunes:name>{escape(_OWNER_NAME)}</itunes:name>\n"
    )
    if _OWNER_EMAIL:
        owner += f"      <itunes:email>{escape(_OWNER_EMAIL)}</itunes:email>\n"
    owner += "    </itunes:owner>\n"
    parts.append(owner)

    for ep in episodes:
        ep_id = str(ep.get("id"))
        title = ep.get("name") or "Episode"
        ep_desc = ep.get("description") or title
        # audio_file is stored RELATIVE to PODCASTS_FOLDER since migration 21;
        # the shared read-side helper joins it back and refuses anything that
        # escapes the root (legacy absolute / file:// rows return None).
        audio_path = resolve_contained_audio_path(ep.get("audio_file"))
        if audio_path is None:
            logger.warning(f"[feed] unresolvable audio path for {ep_id}, skipping")
            continue
        try:
            size = os.path.getsize(audio_path)
        except OSError:
            logger.warning(f"[feed] audio missing on disk for {ep_id}, skipping")
            continue
        enclosure = f"{PUBLIC_BASE_URL}/api/podcasts/episodes/{quote(ep_id)}/audio"
        duration = _fmt_duration(_audio_duration_seconds(audio_path))
        pub = format_datetime(_episode_pub_dt(ep))
        ep_image = _abs_url(ep.get("image_url")) or image

        parts.append("    <item>\n")
        parts.append(_el("title", title))
        parts.append(_el("description", ep_desc))
        parts.append(
            f"      <enclosure url={quoteattr(enclosure)} "
            f'length="{size}" type="audio/mpeg"/>\n'
        )
        parts.append(f'      <guid isPermaLink="false">{escape(ep_id)}</guid>\n')
        parts.append(f"      <pubDate>{escape(pub)}</pubDate>\n")
        if duration:
            parts.append(f"      <itunes:duration>{duration}</itunes:duration>\n")
        parts.append(f"      <itunes:explicit>{explicit}</itunes:explicit>\n")
        if ep_image:
            parts.append(f"      <itunes:image href={quoteattr(ep_image)}/>\n")
        parts.append("    </item>\n")

    parts.append("  </channel>\n")
    parts.append("</rss>\n")
    return "".join(parts)


async def set_published(
    episode_id: str,
    published: bool,
    description: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish/unpublish an episode using a DB-native timestamp (time::now()).

    When publishing, optional public description / cover art can be set in the
    same operation."""
    from open_notebook.database.repository import ensure_record_id

    rid = ensure_record_id(episode_id)
    params: Dict[str, Any] = {"id": rid}
    if published:
        sets = ["published = true", "published_at = time::now()"]
        if description is not None:
            sets.append("description = $description")
            params["description"] = description
        if image_url is not None:
            sets.append("image_url = $image_url")
            params["image_url"] = image_url
    else:
        sets = ["published = false", "published_at = NONE"]
    rows = await repo_query(f"UPDATE $id SET {', '.join(sets)}", params)
    result = parse_record_ids(rows) or []
    if not result:
        raise ValueError(f"Episode not found: {episode_id}")
    return result[0]
