"""Pure, dependency-free helpers for the information cog.

Everything in here is stdlib-only so it can be unit tested without a
Discord connection or a database. Network fetching, Pillow work and
Discord converters live in ``information.py``; this module only holds
validation, parsing and safety logic.
"""

from __future__ import annotations

import ipaddress
import re
from difflib import get_close_matches
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from zoneinfo import available_timezones
except ImportError:  # Python < 3.9 fallback, kept for safety
    def available_timezones():  # type: ignore
        return set()


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

#: Maximum bytes the bot will download for an image/sticker URL.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: Maximum decoded pixels (width * height * frames counted as one frame).
MAX_IMAGE_PIXELS = 25_000_000

#: Seconds for a single outbound HTTP request.
FETCH_TIMEOUT = 10

#: How many manual redirects are followed before giving up.
MAX_REDIRECTS = 3

#: Ports allowed for fetched URLs (anything else is rejected as SSRF-prone).
ALLOWED_PORTS = {80, 443}

#: Image formats accepted for rotate/compress/invert.
ALLOWED_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "JPG", "WEBP", "GIF", "BMP"})

#: Discord sticker upload ceiling (PNG/APNG/Lottie).
MAX_STICKER_BYTES = 500 * 1024


# --------------------------------------------------------------------------- #
# SSRF protection
# --------------------------------------------------------------------------- #

def is_public_ip(value: str) -> bool:
    """Return ``True`` when ``value`` is a publicly routable unicast address.

    Uses an explicit denylist instead of :attr:`ipaddress.IPv4Address.is_global`
    because ``is_global`` treats multicast as global on some Python versions.
    """
    try:
        ip = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def sockaddrs_are_public(sockaddrs: Sequence[tuple]) -> bool:
    """Return ``True`` when every sockaddr from getaddrinfo is public.

    ``sockaddrs`` are the raw 5-tuples returned by ``socket.getaddrinfo``.
    Strict rule: a single non-public address vetoes the whole host, because
    an attacker-controlled domain can rotate between public and private
    answers (DNS rebinding).
    """
    if not sockaddrs:
        return False
    for family, _, _, _, sockaddr in sockaddrs:
        host = sockaddr[0]
        if not is_public_ip(host):
            return False
    return True


def validate_fetch_url(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate a user-supplied URL for server-side fetching.

    Returns ``(clean_url, None)`` on success or ``(None, error)`` with a
    user-facing reason. This checks shape only (scheme, credentials, port,
    hostname); DNS resolution is checked separately with
    :func:`sockaddrs_are_public` so every redirect hop can be re-validated.
    """
    if not raw or not raw.strip():
        return None, "Provide an image URL."
    text = raw.strip()
    if len(text) > 2000:
        return None, "That URL is too long."
    try:
        parts = urlparse(text)
    except ValueError:
        return None, "That isn't a valid URL."
    if parts.scheme not in ("http", "https"):
        return None, "Only `http://` and `https://` URLs are supported."
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return None, "URLs with credentials are not allowed."
    host = (parts.hostname or "").strip().strip(".")
    if not host:
        return None, "That URL has no host."
    if host.lower() == "localhost":
        return None, "That destination is not allowed."
    try:
        port = parts.port
    except ValueError:
        return None, "That URL has an invalid port."
    if port is not None and port not in ALLOWED_PORTS:
        return None, "Only ports 80 and 443 are allowed."
    # A literal IP in the URL can be rejected without a DNS lookup.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        if not ip.is_global:
            return None, "That destination is not allowed."
    return text, None


# --------------------------------------------------------------------------- #
# Image argument parsing
# --------------------------------------------------------------------------- #

def parse_degrees(raw: object) -> Tuple[Optional[int], Optional[str]]:
    """Parse a rotation amount in degrees; returns ``(degrees, None)`` or
    ``(None, error)``. Any integer is accepted and normalised ``% 360``;
    a no-op rotation is an error so the user gets feedback."""
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None, "Degrees must be a whole number (1-359)."
    normalised = value % 360
    if normalised == 0:
        return None, "Degrees must not be a multiple of 360."
    return normalised, None


def parse_quality(raw: object) -> Tuple[Optional[int], Optional[str]]:
    """Parse a JPEG quality value in the 1-100 range."""
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None, "Quality must be a whole number (1-100)."
    if not 1 <= value <= 100:
        return None, "Quality must be between 1 and 100."
    return value, None


# --------------------------------------------------------------------------- #
# Emoji / sticker validation
# --------------------------------------------------------------------------- #

_EMOJI_NAME_RE = re.compile(r"^[a-z0-9_]{2,32}$")


def validate_emoji_name(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """Validate a custom-emoji name per Discord's rules (2-32 chars,
    lowercase alphanumerics and underscores)."""
    name = str(raw or "").strip()
    if not _EMOJI_NAME_RE.match(name):
        return None, (
            "Emoji names must be 2-32 characters of lowercase "
            "letters, numbers or underscores."
        )
    return name, None


def validate_sticker_name(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """Validate a sticker name (2-30 characters, Discord requirement)."""
    name = " ".join(str(raw or "").split())
    if not 2 <= len(name) <= 30:
        return None, "Sticker names must be 2-30 characters."
    return name, None


def validate_sticker_description(raw: object, fallback: str) -> Tuple[str, Optional[str]]:
    """Validate a sticker description, falling back to ``fallback``."""
    text = " ".join(str(raw or "").split())
    if not text:
        text = " ".join(str(fallback or "").split()) or "sticker"
    if len(text) < 2 or len(text) > 200:
        return "", "Sticker descriptions must be 2-200 characters."
    return text, None


# --------------------------------------------------------------------------- #
# Time zones
# --------------------------------------------------------------------------- #

_TZ_CACHE: Optional[List[str]] = None


def _all_timezones() -> List[str]:
    global _TZ_CACHE
    if _TZ_CACHE is None:
        try:
            _TZ_CACHE = sorted(available_timezones())
        except Exception:
            _TZ_CACHE = []
    return _TZ_CACHE


COMMON_TIMEZONES = (
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Moscow",
    "Africa/Cairo",
    "Asia/Dubai",
    "Asia/Karachi",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Pacific/Auckland",
)


def validate_timezone(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """Validate an IANA time-zone identifier.

    Returns ``(canonical_name, None)`` or ``(None, error)`` where the error
    includes a closest-match suggestion when one exists.
    """
    text = " ".join(str(raw or "").split())
    if not text:
        return None, "Provide a time zone like `UTC` or `Europe/Berlin`."
    if len(text) > 64:
        return None, "That time zone is too long."
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        suggestion = ""
        matches = get_close_matches(text, _all_timezones(), n=1, cutoff=0.6)
        if matches:
            suggestion = f" Did you mean `{matches[0]}`?"
        return None, f"Unknown time zone `{text}`.{suggestion}"
    except Exception:
        return None, f"Unknown time zone `{text}`."
    return text, None


# --------------------------------------------------------------------------- #
# External-lookup parsing
# --------------------------------------------------------------------------- #

_GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$")
_GITHUB_REPO_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,100}$")


def parse_github_target(raw: object) -> Tuple[Optional[tuple], Optional[str]]:
    """Parse ``user`` or ``owner/repo`` for the GitHub lookup.

    Returns ``((kind, owner, repo_or_None), None)`` or ``(None, error)``.
    """
    text = str(raw or "").strip().strip("/")
    if not text or len(text) > 200:
        return None, "Provide a GitHub username or `owner/repo`."
    if text.count("/") > 1:
        return None, "Use `username` or `owner/repo`."
    if "/" in text:
        owner, _, repo = text.partition("/")
        owner, repo = owner.strip(), repo.strip()
        if not owner or not repo:
            return None, "Use `username` or `owner/repo`."
        if not _GITHUB_USER_RE.match(owner) or not _GITHUB_REPO_RE.match(repo):
            return None, "That doesn't look like a valid `owner/repo`."
        return ("repo", owner, repo), None
    if not _GITHUB_USER_RE.match(text):
        return None, "That doesn't look like a valid GitHub username."
    return ("user", text, None), None


_MC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def parse_minecraft_identifier(raw: object) -> Tuple[Optional[tuple], Optional[str]]:
    """Parse a Minecraft username or UUID.

    Returns ``((kind, value), None)`` where kind is ``"uuid"`` (dashed,
    lowercase) or ``"username"``, or ``(None, error)``.
    """
    text = str(raw or "").strip()
    if not text or len(text) > 36:
        return None, "Provide a Minecraft username or UUID."
    dashed = text.lower()
    undashed = dashed.replace("-", "")
    if re.fullmatch(r"[0-9a-f]{32}", undashed):
        canonical = (
            f"{undashed[0:8]}-{undashed[8:12]}-{undashed[12:16]}-"
            f"{undashed[16:20]}-{undashed[20:32]}"
        )
        return ("uuid", canonical), None
    if _MC_USERNAME_RE.match(text):
        return ("username", text), None
    return None, "That isn't a valid Minecraft username or UUID."


# --------------------------------------------------------------------------- #
# Weather codes (Open-Meteo WMO table)
# --------------------------------------------------------------------------- #

WMO_WEATHER = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def wmo_description(code: object) -> str:
    """Return a human description for a WMO weather code."""
    try:
        return WMO_WEATHER.get(int(code), "Unknown conditions")  # type: ignore
    except (ValueError, TypeError):
        return "Unknown conditions"


# --------------------------------------------------------------------------- #
# Pagination helper (pure)
# --------------------------------------------------------------------------- #

def chunk_lines(lines: Sequence[str], per_page: int) -> List[List[str]]:
    """Split ``lines`` into pages of at most ``per_page`` entries."""
    per_page = max(1, int(per_page or 10))
    return [list(lines[i:i + per_page]) for i in range(0, len(lines), per_page)]
