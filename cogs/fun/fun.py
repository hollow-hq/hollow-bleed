import aiohttp
import asyncio
import base64
import binascii
import calendar
import hashlib
import html
import io
import ipaddress
import json
import random
import re
import socket
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import discord
from aiohttp.abc import AbstractResolver
from discord import AllowedMentions, Embed, Member, Message, Role, TextChannel
from discord.ext import tasks
from discord.ext.commands import (
    BucketType,
    Cog,
    cooldown,
    guild_only,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.utils import format_dt
from humanize import ordinal
from loguru import logger

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.predicates import has_permissions
from base.managers.types import CogMeta

# Your own wrappers (kept out of this repo's requirements on purpose).
try:
    from fishr import DeepAI
except Exception:  # pragma: no cover
    DeepAI = None
try:
    from fishr import MusicMake
except Exception:  # pragma: no cover
    MusicMake = None

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DIH_HEAD = "8"
DIH_TIP = "D"
DIH_REAL_INCHES = 67

MAX_PING_ROLES = 5
MAX_DIARY_ENTRIES = 50
DIARY_MAX_LENGTH = 2000
PER_PAGE = 10
BASE64_MAX_INPUT = 2500
MIN_BIRTH_AGE = 13  # Discord's minimum age

MONTHS: Dict[str, int] = {}
for _i in range(1, 13):
    MONTHS[calendar.month_name[_i].lower()] = _i
    MONTHS[calendar.month_abbr[_i].lower()] = _i
MONTHS["sept"] = 9

EIGHT_BALL: List[Tuple[str, str]] = [
    ("It is certain.", "yes"),
    ("It is decidedly so.", "yes"),
    ("Without a doubt.", "yes"),
    ("Yes, definitely.", "yes"),
    ("You may rely on it.", "yes"),
    ("As I see it, yes.", "yes"),
    ("Most likely.", "yes"),
    ("Outlook good.", "yes"),
    ("Yes.", "yes"),
    ("Signs point to yes.", "yes"),
    ("Reply hazy, try again.", "maybe"),
    ("Ask again later.", "maybe"),
    ("Better not tell you now.", "maybe"),
    ("Cannot predict now.", "maybe"),
    ("Concentrate and ask again.", "maybe"),
    ("Don't count on it.", "no"),
    ("My reply is no.", "no"),
    ("My sources say no.", "no"),
    ("Outlook not so good.", "no"),
    ("Very doubtful.", "no"),
]
EIGHT_BALL_COLORS = {
    "yes": COLORS.approve,
    "maybe": COLORS.warn,
    "no": COLORS.deny,
}

SHIP_VERDICTS: List[Tuple[int, str]] = [
    (10, "Absolutely not. Run."),
    (30, "Not a great match."),
    (50, "There's a spark, maybe."),
    (70, "Pretty compatible!"),
    (90, "Seriously good together."),
    (100, "Soulmates. Book the venue."),
]

BITCHES_TIERS: List[Tuple[int, str]] = [
    (0, "None. Go touch some grass."),
    (5, "Barely a few. Keep trying."),
    (20, "Respectable numbers."),
    (50, "Certified player."),
    (100, "Legendary. Someone stop them."),
]

KILO_URL = "https://api.kilo.ai/api/gateway/chat/completions"
KILO_MODELS = ["kilo-auto/free"]  # tried in order
KILO_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "origin": "https://api.kilo.ai",
    "referer": "https://api.kilo.ai/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    ),
}
ROAST_MAX_LENGTH = 900

ROAST_SYSTEM_PROMPT = (
    "You write silly, harmless, rapid-fire comedy roasts for a Discord bot. "
    "Never reference race, skin color, ethnicity, religion, gender, sexuality, disability, "
    "mental health, bodies, age, nationality, family, death or tragedies. "
    "No slurs, no sexual content, no threats. Output only the roast, nothing else."
)

ROAST_USER_PROMPT = """Generate a packgod-style roast similar to this example but with different words and references:

"SHUT YO CHICKEN BONE GOOGLE CHROME NO HOME FLIP PHONE DISOWNED ICE CREAM CONE GARDEN GNOME METRONOME DIMMADOME GENOME FULL BLOWN MONOCHROME STUDENT LOAN INDIANA JONES OVERGROWN FLINTSTONE FRIEND ZONE SYLVESTER STALLONE SIERRA LEONE AUTOZONE PROFESSIONALLY SEEN SILVER PATRONE"

Create a similar roast with:
- ALL CAPS format
- Same rapid-fire style
- Random object/reference combinations
- Rhyming words when possible
- Keep it silly and creative, not actually offensive
- Start with "SHUT YO"
- Use creative ending
- Make it about the same length
- Include 💀 and 🔥 emojis where appropriate or other emojis!

Generate ONE unique variation in ALL CAPS:"""

BIRTHDAY_MESSAGES = [
    "Wishing {member} the happiest of birthdays!",
    "It's {member}'s special day. Happy birthday!",
    "Everybody wish {member} a happy birthday!",
    "Another trip around the sun for {member}. Happy birthday!",
]

NO_MENTIONS = AllowedMentions.none()

MAX_PROMPT_LENGTH = 1500
MAX_IMAGINE_LENGTH = 500
MAX_TTS_LENGTH = 300
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_CHAT_CHANNELS = 5

HISTORY_MESSAGES = 12  # user + assistant messages kept per conversation
HISTORY_TTL = 30 * 60  # seconds
MAX_TOOL_ROUNDS = 3
TOOL_RESULT_MAX = 6000
USER_REPLY_INTERVAL = 3.0  # seconds between auto-replies per user
CONCURRENT_CHATS = 4
CONCURRENT_MEDIA = 2

# Public voice ids of the MusicMake wrapper (upstream names stay inside the wrapper).
TTS_VOICES = (
    "aura", "breeze", "cypress", "drift", "echo", "flare", "gem", "hazel", "ivy",
    "jazz", "kite", "lumen", "mist", "saffron", "solstice", "pearl", "quartz",
    "ripple", "cobalt", "tide", "vale", "wren", "ash", "brook", "cedar", "dawn",
    "fern", "glen", "harbor", "indigo", "juniper", "lotus", "maple", "nettle",
    "opal", "pine", "river", "slate", "willow",
)
DEFAULT_VOICE = "aura"

SYSTEM_PROMPT = "\n".join(
    (
        "you are a discord bot named {bot}",
        "you are inside a discord server",
        "you only answer in the least amount of words as possible",
        "you only answer in all lower case",
        "current date is {date}",
    )
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def bar(pct: int, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def truncate(text: str, length: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1] + "…"


def truncate_block(text: str, length: int) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"


def split_message(text: str, limit: int = 1900, max_parts: int = 3) -> List[str]:
    parts: List[str] = []
    while text and len(parts) < max_parts:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    else:
        if text and parts:
            parts[-1] = parts[-1][: limit - 1] + "…"
    return parts


def neutralize(text: str) -> str:
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def image_ext(content_type: str, data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"GIF8"):
        return "gif"
    return "png"


def audio_ext(data: bytes) -> str:
    if data.startswith(b"RIFF"):
        return "wav"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if data.startswith(b"OggS"):
        return "ogg"
    return "wav"


def codeblock(text: str) -> str:
    return "```\n" + text.replace("```", "`\u200b``") + "\n```"


def month_day(month: int, day: int) -> str:
    return f"{calendar.month_name[month]} {ordinal(day)}"


def effective_date(month: int, day: int, year: int) -> date:
    """Feb 29 birthdays are celebrated on Feb 28 in non-leap years."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_occurrence(month: int, day: int, today: date) -> date:
    for year in (today.year, today.year + 1, today.year + 2):
        candidate = effective_date(month, day, year)
        if candidate >= today:
            return candidate
    return effective_date(month, day, today.year + 1)  # unreachable


def as_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def paged(title: str, lines: List[str], noun: str, per_page: int = PER_PAGE) -> List[Embed]:
    pages = [lines[i : i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    embeds: List[Embed] = []
    for index, chunk in enumerate(pages, 1):
        embed = Embed(title=title, description="\n".join(chunk), color=COLORS.neutral)
        embed.set_footer(text=f"Page {index}/{len(pages)} ({len(lines)} {noun})")
        embeds.append(embed)
    return embeds


def parse_birthday(raw: str) -> Tuple[int, int, Optional[int]]:
    """
    Accepts:  june 5 | 5 june | jun 5th 2000 | 2000-06-05 | 06/05 | 06/05/2000
    Numeric month-first (MM/DD) unless the first number is > 12 (then DD/MM).
    Returns (month, day, year|None). Raises ValueError with a user-facing message.
    """
    text = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", raw.lower())
    tokens = [t for t in re.split(r"[\s,/.\-]+", text) if t and t != "of"]
    bad_format = ValueError(
        "I couldn't understand that date. Try `june 5`, `5 june 2000` or `06/05`."
    )

    if not 2 <= len(tokens) <= 3:
        raise bad_format
    words = [t for t in tokens if t.isalpha()]
    nums = [t for t in tokens if t.isdigit()]
    if len(words) + len(nums) != len(tokens):
        raise bad_format

    month = day = year = None
    if words:
        if len(words) != 1 or words[0] not in MONTHS:
            raise bad_format
        month = MONTHS[words[0]]
        if len(nums) == 1:
            day = int(nums[0])
        elif len(nums) == 2:
            first, second = nums
            if len(first) == 4:
                year, day = int(first), int(second)
            elif len(second) == 4:
                day, year = int(first), int(second)
            else:
                raise bad_format
        else:
            raise bad_format
    else:
        if len(nums) == 3 and len(nums[0]) == 4:
            year, month, day = (int(n) for n in nums)
        elif len(nums) == 3 and len(nums[2]) == 4:
            first, second = int(nums[0]), int(nums[1])
            year = int(nums[2])
            month, day = (second, first) if first > 12 else (first, second)
        elif len(nums) == 2:
            first, second = int(nums[0]), int(nums[1])
            month, day = (second, first) if first > 12 else (first, second)
        else:
            raise bad_format

    if not 1 <= month <= 12:
        raise ValueError("That month doesn't exist.")
    reference_year = year if year is not None else 2000  # 2000 is a leap year
    if not 1 <= day <= calendar.monthrange(reference_year, month)[1]:
        raise ValueError("That day doesn't exist in that month.")
    if year is not None:
        newest = utc_today().year - MIN_BIRTH_AGE
        if not 1900 <= year <= newest:
            raise ValueError(
                f"The year must be between 1900 and {newest} (Discord requires {MIN_BIRTH_AGE}+). "
                "You can also leave the year out."
            )
    return month, day, year


# --------------------------------------------------------------------------- #
# Web tools (search + fetch, SSRF-safe)
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
MAX_PAGE_BYTES = 1_500_000
MAX_REDIRECTS = 4
BLOCKED_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Returns titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch a web page and return its readable text.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Full http(s) URL"}},
                "required": ["url"],
            },
        },
    },
]


class WebError(Exception):
    pass


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_global and not ip.is_multicast


class PublicOnlyResolver(AbstractResolver):
    """DNS resolver that refuses private/loopback/link-local targets (SSRF guard)."""

    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        infos = await self._inner.resolve(host, port, family)
        safe = [i for i in infos if is_public_ip(i["host"])]
        if not safe:
            raise OSError(f"Blocked non-public address for {host}")
        return safe

    async def close(self) -> None:
        await self._inner.close()


def validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebError("Only full http(s) URLs are allowed.")
    if parsed.username or parsed.password:
        raise WebError("URLs with credentials are not allowed.")
    if parsed.port not in (None, 80, 443):
        raise WebError("Only ports 80 and 443 are allowed.")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise WebError("That host is not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a hostname; the resolver checks what it resolves to
    else:
        if not is_public_ip(host):
            raise WebError("That address is not allowed.")
    return parsed.geturl()


def html_to_text(raw: str) -> str:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""
    raw = re.sub(r"<(script|style|noscript|svg|head)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return f"Title: {title}\n\n{text}" if title else text


def _strip_tags(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())


class WebTools:
    def __init__(self) -> None:
        connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver(), limit=10, ttl_dns_cache=60)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": USER_AGENT},
        )

    async def close(self) -> None:
        await self.session.close()

    async def _get(self, url: str, max_bytes: int) -> Tuple[bytes, str]:
        for _ in range(MAX_REDIRECTS + 1):
            url = validate_url(url)
            try:
                async with self.session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            raise WebError("Redirect without a location.")
                        url = urljoin(url, location)
                        continue
                    if resp.status >= 400:
                        raise WebError(f"HTTP {resp.status}")
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise WebError("The response is too large.")
                    return bytes(body), resp.headers.get("Content-Type", "")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                raise WebError(f"Request failed: {type(e).__name__}")
        raise WebError("Too many redirects.")

    async def fetch_bytes(self, url: str, max_bytes: int) -> Tuple[bytes, str]:
        return await self._get(url, max_bytes)

    async def fetch_text(self, url: str) -> str:
        body, ctype = await self._get(url, MAX_PAGE_BYTES)
        if not any(t in ctype.lower() for t in ("text/", "json", "xml")):
            raise WebError(f"Unsupported content type: {ctype or 'unknown'}")
        raw = body.decode("utf-8", errors="replace")
        if "html" in ctype.lower() or raw.lstrip().startswith("<"):
            return html_to_text(raw)
        return raw.strip()

    async def search(self, query: str) -> str:
        try:
            async with self.session.post(
                "https://html.duckduckgo.com/html/", data={"q": query}
            ) as resp:
                if resp.status != 200:
                    raise WebError(f"Search failed (HTTP {resp.status})")
                page = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise WebError(f"Search failed: {type(e).__name__}")

        results = []
        pattern = r'(<a\b[^>]*class="[^"]*result__a[^"]*"[^>]*>)(.*?)</a>'
        for match in re.finditer(pattern, page, re.S):
            href_match = re.search(r'href="([^"]+)"', match.group(1))
            if not href_match:
                continue
            href = html.unescape(href_match.group(1))
            if href.startswith("//"):
                href = "https:" + href
            target = parse_qs(urlparse(href).query).get("uddg")
            url = target[0] if target else href
            snippet_match = re.search(
                r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
                page[match.end() : match.end() + 3000],
                re.S,
            )
            snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
            results.append(f"{len(results) + 1}. {_strip_tags(match.group(2))}\n{url}\n{snippet}")
            if len(results) >= 5:
                break
        if not results:
            raise WebError("No results found.")
        return "\n\n".join(results)

    async def run(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "web_search":
                return await self.search(str(args.get("query", ""))[:200])
            if name == "fetch_url":
                return await self.fetch_text(str(args.get("url", "")))
            return f"Error: unknown tool {name}"
        except WebError as e:
            return f"Error: {e}"


# --------------------------------------------------------------------------- #
# Kilo gateway client (KILO_URL / KILO_MODELS / KILO_HEADERS live in Constants)
# --------------------------------------------------------------------------- #


class KiloError(RuntimeError):
    pass


def _text(content: Any) -> str:
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL)
    return content.strip()


class KiloClient:
    """Minimal async client for the keyless Kilo gateway (OpenAI-compatible)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> Dict[str, Any]:
        """Returns {"content": str, "tool_calls": list}. Raises KiloError."""
        last_error = "no models available"
        for model in KILO_MODELS:
            payload: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "reasoning": {"enabled": False},
            }
            if tools:
                payload["tools"] = tools

            data = None
            for _ in range(3):
                try:
                    async with self.session.post(
                        KILO_URL, json=payload, headers=KILO_HEADERS
                    ) as resp:
                        if resp.status == 400:
                            # Drop optional features the model/gateway rejected.
                            if "reasoning" in payload:
                                payload.pop("reasoning")
                                continue
                            if "tools" in payload:
                                payload.pop("tools")
                                continue
                        if resp.status != 200:
                            body = (await resp.text())[:200]
                            last_error = f"{model}: HTTP {resp.status}: {body}"
                            break
                        data = await resp.json(content_type=None)
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_error = f"{model}: {type(e).__name__}: {e}"
                    break
            if data is None:
                continue

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = _text(message.get("content"))
            tool_calls = message.get("tool_calls") or []
            if content or tool_calls:
                return {"content": content, "tool_calls": tool_calls}
            last_error = f"{model}: empty response (finish_reason={choice.get('finish_reason')})"

        raise KiloError(last_error)


class GuildChat:
    __slots__ = ("enabled", "channels")

    def __init__(self, enabled: bool, channels: Set[int]) -> None:
        self.enabled = enabled
        self.channels = channels


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


class Fun(CogMeta):
    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        self.birthday_loop.start()
        self.kilo = KiloClient(self.session)
        self.web = WebTools()
        self.deepai = DeepAI() if DeepAI else None
        self.musicmake = MusicMake() if MusicMake else None
        if not self.deepai or not self.musicmake:
            logger.warning("fishr DeepAI/MusicMake not importable; imagine/tts are disabled.")

        self._configs: Dict[int, GuildChat] = {}
        self._history: Dict[Any, Tuple[float, List[Dict[str, str]]]] = {}
        self._last_reply: Dict[int, float] = {}
        self._chat_sem = asyncio.Semaphore(CONCURRENT_CHATS)
        self._media_sem = asyncio.Semaphore(CONCURRENT_MEDIA)

    async def cog_unload(self) -> None:
        self.birthday_loop.cancel()
        await self.session.close()
        await self.web.close()

    # ------------------------------------------------------------------ #
    # Percentage commands
    # ------------------------------------------------------------------ #

    async def _meter(
        self, ctx: Context, member: Optional[Member], trait: str, emoji: str
    ) -> Message:
        member = member or ctx.author
        pct = random.randint(0, 100)
        return await ctx.embed(
            description=f"{emoji} {member.mention} is **{pct}%** {trait}\n{bar(pct)}",
            footer={
                "text": f"Requested by {ctx.author.display_name}",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    @hybrid_command(
        name="howgay",
        aliases=["gay", "gayrate", "gaymeter"],
        description="See how gay someone is.",
        example="howgay @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def howgay(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._meter(ctx, member, "gay", "🏳️‍🌈")

    @hybrid_command(
        name="howlesbian",
        aliases=["lesbian", "lesbianrate", "lesbianmeter"],
        description="See how lesbian someone is.",
        example="howlesbian @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def howlesbian(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._meter(ctx, member, "lesbian", "💗")

    @hybrid_command(
        name="howautism",
        aliases=["autism", "autistic", "autismrate", "howautistic"],
        description="See how autistic someone is.",
        example="howautism @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def howautism(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._meter(ctx, member, "autistic", "🧩")

    @hybrid_command(
        name="howsimp",
        aliases=["simp", "simprate", "simpmeter"],
        description="See how much of a simp someone is.",
        example="howsimp @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def howsimp(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._meter(ctx, member, "a simp", "🥺")

    @hybrid_command(
        name="bitches",
        aliases=["bitchcount", "hoes", "rizz"],
        description="See how many bitches someone has.",
        example="bitches @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def bitches(self, ctx: Context, member: Optional[Member] = None) -> Message:
        member = member or ctx.author
        count = random.randint(0, 100)
        verdict = next(text for limit, text in BITCHES_TIERS if count <= limit)
        return await ctx.embed(
            description=f"💅 {member.mention} has **{count}** bitches\n{verdict}",
            footer={
                "text": f"Requested by {ctx.author.display_name}",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    # ------------------------------------------------------------------ #
    # Dih
    # ------------------------------------------------------------------ #

    async def _send_dih(
        self, ctx: Context, member: Member, inches: float, real: bool
    ) -> Message:
        visual = DIH_HEAD + "=" * max(1, round(inches)) + DIH_TIP
        shown = f"{inches:g}"
        label = "REAL dih 🥀" if real else "Dih 🥀"
        return await ctx.embed(
            author={
                "name": f"{member.display_name}'s {label.lower()}",
                "icon_url": member.display_avatar.url,
            },
            description=f"{visual}\n**{shown}** inches",
            footer={
                "text": f"Checked by {ctx.author.display_name}",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    @hybrid_command(
        name="dih",
        aliases=["dick", "pp", "penis"],
        description="Measure someone's dih.",
        example="dih @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def dih(self, ctx: Context, member: Optional[Member] = None) -> Message:
        member = member or ctx.author
        inches = round(min(max(random.gauss(5.5, 1.8), 1.0), 12.0), 1)
        return await self._send_dih(ctx, member, inches, real=False)

    @hybrid_command(
        name="dihr",
        aliases=["dickr", "ppr", "realdih", "realdick"],
        description="Measure someone's REAL dih.",
        example="dihr @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def dihr(self, ctx: Context, member: Optional[Member] = None) -> Message:
        member = member or ctx.author
        return await self._send_dih(ctx, member, DIH_REAL_INCHES, real=True)

    # ------------------------------------------------------------------ #
    # 8ball
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="8ball",
        aliases=["eightball", "8b", "magic8ball"],
        description="Ask the magic 8-ball a question.",
        example="8ball will I win?",
    )
    @cooldown(1, 3, BucketType.user)
    async def eightball(self, ctx: Context, *, question: str) -> Message:
        answer, kind = random.choice(EIGHT_BALL)
        return await ctx.embed(
            color=EIGHT_BALL_COLORS[kind],
            description=f"🎱 **{truncate(question, 250)}**\n{answer}",
            footer={
                "text": f"Asked by {ctx.author.display_name}",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    # ------------------------------------------------------------------ #
    # Roast
    # ------------------------------------------------------------------ #

    async def _generate_roast(self) -> str:
        messages = [
            {"role": "system", "content": ROAST_SYSTEM_PROMPT},
            {"role": "user", "content": ROAST_USER_PROMPT},
        ]

        text = ""
        for model in KILO_MODELS:
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "temperature": 1.0,
                "max_tokens": 1024,
                "reasoning": {"enabled": False},
            }
            data = None
            for _ in range(2):
                try:
                    async with self.session.post(
                        KILO_URL, json=payload, headers=KILO_HEADERS
                    ) as resp:
                        if resp.status == 400 and "reasoning" in payload:
                            # Model/gateway rejected the reasoning flag, retry without it.
                            payload.pop("reasoning")
                            continue
                        if resp.status != 200:
                            body = (await resp.text())[:300]
                            logger.warning(f"Kilo {model} returned HTTP {resp.status}: {body}")
                            break
                        data = await resp.json(content_type=None)
                        break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(f"Kilo {model} request failed: {e}")
                    break
            if data is None:
                continue

            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL)
            text = " ".join(text.split()).strip(" \"'")
            if text:
                break
            logger.warning(
                f"Kilo {model} gave no content (finish_reason={choice.get('finish_reason')}, "
                f"has_reasoning={bool(message.get('reasoning') or message.get('reasoning_content'))}): "
                f"{str(data)[:400]}"
            )

        if not text:
            raise RuntimeError("Empty response from the AI")
        text = text.replace("@", "@\u200b").upper()
        return truncate(text, ROAST_MAX_LENGTH)

    @hybrid_command(
        name="roast",
        aliases=["flame", "burn", "packgod"],
        description="Get roasted by AI (just for fun).",
        example="roast @voby",
    )
    @cooldown(1, 10, BucketType.user)
    async def roast(self, ctx: Context, member: Optional[Member] = None) -> Message:
        member = member or ctx.author
        async with ctx.typing():
            try:
                roast = await self._generate_roast()
            except Exception as e:
                logger.error(f"Roast failed: {e}")
                return await ctx.warn("The roast machine is down right now. Try again in a bit.")

        return await ctx.embed(
            description=f"{member.mention} {roast}",
            footer={
                "text": f"Roasted by AI ∙ requested by {ctx.author.display_name} ∙ just for fun",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    # ------------------------------------------------------------------ #
    # Ship
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="ship",
        aliases=["compatibility", "lovecalc", "lovemeter"],
        description="Check two people's compatibility.",
        example="ship @voby @kay",
    )
    @cooldown(1, 3, BucketType.user)
    async def ship(
        self, ctx: Context, member1: Member, member2: Optional[Member] = None
    ) -> Message:
        if member2 is None:
            member1, member2 = ctx.author, member1

        if member1.id == member2.id:
            pct, verdict = 100, "Self-love is the best love."
        else:
            low, high = sorted((member1.id, member2.id))
            pct = int(hashlib.sha256(f"{low}:{high}".encode()).hexdigest(), 16) % 101
            verdict = next(text for limit, text in SHIP_VERDICTS if pct <= limit)

        first, second = member1.display_name, member2.display_name
        ship_name = first[: (len(first) + 1) // 2] + second[len(second) // 2 :]
        return await ctx.embed(
            description=(
                f"💞 {member1.mention} × {member2.mention}\n"
                f"**Ship name:** {ship_name}\n\n"
                f"{bar(pct)} **{pct}%**\n{verdict}"
            ),
            footer={
                "text": f"Requested by {ctx.author.display_name}",
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    # ------------------------------------------------------------------ #
    # Base64
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="base64",
        aliases=["b64"],
        invoke_without_command=True,
        description="Encode or decode base64.",
        example="base64 encode hello",
    )
    async def base64(self, ctx: Context) -> Message:
        return await ctx.send_help(ctx.command)

    @base64.command(
        name="encode",
        aliases=["enc", "e"],
        description="Encode text to base64.",
        example="base64 encode hello world",
    )
    @cooldown(1, 2, BucketType.user)
    async def base64_encode(self, ctx: Context, *, text: str) -> Message:
        if len(text) > BASE64_MAX_INPUT:
            return await ctx.warn(f"That's too long. The limit is **{BASE64_MAX_INPUT}** characters.")
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return await ctx.embed(title="Base64 encoded", description=codeblock(encoded))

    @base64.command(
        name="decode",
        aliases=["dec", "d"],
        description="Decode base64 to text.",
        example="base64 decode aGVsbG8=",
    )
    @cooldown(1, 2, BucketType.user)
    async def base64_decode(self, ctx: Context, *, text: str) -> Message:
        cleaned = "".join(text.split())
        cleaned += "=" * (-len(cleaned) % 4)
        try:
            raw = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError):
            try:
                raw = base64.urlsafe_b64decode(cleaned)
            except (binascii.Error, ValueError):
                return await ctx.warn("That isn't valid base64.")
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return await ctx.warn("That decodes to binary data, not text.")
        if not decoded.strip():
            return await ctx.warn("That decodes to an empty message.")
        return await ctx.embed(
            title="Base64 decoded", description=codeblock(truncate_block(decoded, 3800))
        )

    # ------------------------------------------------------------------ #
    # Birthday: viewing
    # ------------------------------------------------------------------ #

    async def _show_birthday(self, ctx: Context, member: Member) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT month, day, year, locked FROM birthdays WHERE user_id = %s", member.id
        )
        if not row:
            who = "You haven't" if member.id == ctx.author.id else f"**{member.display_name}** hasn't"
            return await ctx.warn(
                f"{who} set a birthday yet. Use `{ctx.clean_prefix}birthday set <date>`."
            )

        today = utc_today()
        upcoming = next_occurrence(row["month"], row["day"], today)
        lines = [f"🎂 {member.mention}'s birthday is on **{month_day(row['month'], row['day'])}**"]
        if upcoming == today:
            lines.append("🎉 It's their birthday **today**!")
        else:
            lines.append(f"Next birthday {format_dt(as_dt(upcoming), 'R')}")
        if row["year"]:
            lines.append(f"Turning **{upcoming.year - row['year']}**")

        return await ctx.embed(
            description="\n".join(lines),
            footer={"text": "Locked" if row["locked"] else "Use birthday set to change yours"},
        )

    @hybrid_group(
        name="birthday",
        aliases=["bday", "brithday", "bd"],
        invoke_without_command=True,
        description="View a user's birthday.",
        example="birthday @voby",
    )
    async def birthday(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._show_birthday(ctx, member or ctx.author)

    @birthday.command(
        name="view",
        aliases=["show", "check"],
        description="View a user's birthday.",
        example="birthday view @voby",
    )
    async def birthday_view(self, ctx: Context, member: Optional[Member] = None) -> Message:
        return await self._show_birthday(ctx, member or ctx.author)

    # ------------------------------------------------------------------ #
    # Birthday: personal settings
    # ------------------------------------------------------------------ #

    @birthday.command(
        name="set",
        aliases=["add", "edit"],
        description="Set your birthday.",
        example="birthday set june 5",
    )
    @cooldown(1, 5, BucketType.user)
    async def birthday_set(self, ctx: Context, *, birthday: str) -> Message:
        existing = await self.bot.pool.fetchrow(
            "SELECT locked FROM birthdays WHERE user_id = %s", ctx.author.id
        )
        if existing and existing["locked"]:
            return await ctx.warn(
                f"Your birthday is locked. Use `{ctx.clean_prefix}birthday unlock` first."
            )
        try:
            month, day, year = parse_birthday(birthday)
        except ValueError as e:
            return await ctx.warn(str(e))

        await self.bot.pool.execute(
            "INSERT INTO birthdays (user_id, month, day, year) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE month = VALUES(month), day = VALUES(day), year = VALUES(year)",
            ctx.author.id, month, day, year,
        )
        return await ctx.approve(f"Your birthday is now set to **{month_day(month, day)}**")

    @birthday.command(
        name="reset",
        aliases=["clear", "remove", "delete"],
        description="Clear your birthday.",
        example="birthday reset",
    )
    async def birthday_reset(self, ctx: Context) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT locked FROM birthdays WHERE user_id = %s", ctx.author.id
        )
        if not row:
            return await ctx.warn("You don't have a birthday set.")
        if row["locked"]:
            return await ctx.warn(
                f"Your birthday is locked. Use `{ctx.clean_prefix}birthday unlock` first."
            )
        await self.bot.pool.execute("DELETE FROM birthdays WHERE user_id = %s", ctx.author.id)
        return await ctx.approve("Your birthday has been cleared.")

    @birthday.command(
        name="lock",
        description="Lock your birthday so it can't be changed.",
        example="birthday lock",
    )
    async def birthday_lock(self, ctx: Context) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT locked FROM birthdays WHERE user_id = %s", ctx.author.id
        )
        if not row:
            return await ctx.warn("You don't have a birthday set.")
        if row["locked"]:
            return await ctx.warn("Your birthday is already locked.")
        await self.bot.pool.execute("UPDATE birthdays SET locked = 1 WHERE user_id = %s", ctx.author.id)
        return await ctx.approve("Your birthday is now **locked**.")

    @birthday.command(
        name="unlock",
        description="Unlock your birthday so it can be changed.",
        example="birthday unlock",
    )
    async def birthday_unlock(self, ctx: Context) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT locked FROM birthdays WHERE user_id = %s", ctx.author.id
        )
        if not row:
            return await ctx.warn("You don't have a birthday set.")
        if not row["locked"]:
            return await ctx.warn("Your birthday isn't locked.")
        await self.bot.pool.execute("UPDATE birthdays SET locked = 0 WHERE user_id = %s", ctx.author.id)
        return await ctx.approve("Your birthday is now **unlocked**.")

    # ------------------------------------------------------------------ #
    # Birthday: server lists
    # ------------------------------------------------------------------ #

    @birthday.command(
        name="list",
        aliases=["all"],
        description="List every birthday in this server.",
        example="birthday list",
    )
    @guild_only()
    @cooldown(1, 5, BucketType.guild)
    async def birthday_list(self, ctx: Context) -> Message:
        rows = await self.bot.pool.fetch("SELECT user_id, month, day FROM birthdays")
        entries = [
            (r["month"], r["day"], m)
            for r in rows
            if (m := ctx.guild.get_member(r["user_id"]))
        ]
        if not entries:
            return await ctx.warn("Nobody in this server has set a birthday.")
        entries.sort(key=lambda e: (e[0], e[1], e[2].display_name.lower()))
        lines = [f"**{month_day(mo, d)}** — {m.mention}" for mo, d, m in entries]
        return await ctx.paginate(paged(f"Birthdays in {ctx.guild.name}", lines, "birthdays"))

    @birthday.command(
        name="next",
        aliases=["upcoming"],
        description="Show the next birthdays in this server.",
        example="birthday next",
    )
    @guild_only()
    @cooldown(1, 5, BucketType.guild)
    async def birthday_next(self, ctx: Context) -> Message:
        rows = await self.bot.pool.fetch("SELECT user_id, month, day FROM birthdays")
        today = utc_today()
        entries = [
            (next_occurrence(r["month"], r["day"], today), r, m)
            for r in rows
            if (m := ctx.guild.get_member(r["user_id"]))
        ]
        if not entries:
            return await ctx.warn("Nobody in this server has set a birthday.")
        entries.sort(key=lambda e: (e[0], e[2].display_name.lower()))

        first = entries[0][0]
        selected = [e for e in entries if (e[0].year, e[0].month) == (first.year, first.month)]
        lines = []
        for when, r, m in selected:
            suffix = " 🎂 **today**" if when == today else ""
            lines.append(f"**{month_day(r['month'], r['day'])}** — {m.mention}{suffix}")
        title = f"Upcoming birthdays — {calendar.month_name[first.month]} {first.year}"
        return await ctx.paginate(paged(title, lines, "birthdays"))

    # ------------------------------------------------------------------ #
    # Birthday: server configuration
    # ------------------------------------------------------------------ #

    @birthday.command(
        name="channel",
        aliases=["ch"],
        description="Set the channel birthdays are announced in.",
        example="birthday channel #birthdays",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def birthday_channel(
        self, ctx: Context, channel: Optional[TextChannel] = None
    ) -> Message:
        if channel is None:
            cfg = await self.bot.pool.fetchrow(
                "SELECT channel_id FROM birthday_config WHERE guild_id = %s", ctx.guild.id
            )
            current = ctx.guild.get_channel(cfg["channel_id"]) if cfg and cfg["channel_id"] else None
            if not current:
                return await ctx.warn(
                    f"No birthday channel is set. Use `{ctx.clean_prefix}birthday channel #channel`."
                )
            return await ctx.embed(description=f"{EMOJIS.INFORMATION} Birthdays are announced in {current.mention}")

        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await ctx.warn(f"I need **Send Messages** and **Embed Links** in {channel.mention}.")

        await self.bot.pool.execute(
            "INSERT INTO birthday_config (guild_id, channel_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id)",
            ctx.guild.id, channel.id,
        )
        return await ctx.approve(f"Birthdays will now be announced in {channel.mention}")

    @birthday.command(
        name="role",
        description="Set the role members get on their birthday.",
        example="birthday role @Birthday",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def birthday_role(self, ctx: Context, role: Optional[Role] = None) -> Message:
        if role is None:
            cfg = await self.bot.pool.fetchrow(
                "SELECT role_id FROM birthday_config WHERE guild_id = %s", ctx.guild.id
            )
            current = ctx.guild.get_role(cfg["role_id"]) if cfg and cfg["role_id"] else None
            if not current:
                return await ctx.warn(
                    f"No birthday role is set. Use `{ctx.clean_prefix}birthday role @role`."
                )
            return await ctx.embed(description=f"{EMOJIS.INFORMATION} The birthday role is {current.mention}")

        me = ctx.guild.me
        if role.is_default() or role.managed:
            return await ctx.warn("That role can't be used as a birthday role.")
        if not me.guild_permissions.manage_roles:
            return await ctx.warn("I need the **Manage Roles** permission.")
        if role >= me.top_role:
            return await ctx.warn("That role is higher than or equal to my top role.")
        if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
            return await ctx.warn("That role is higher than or equal to your top role.")
        if ctx.is_dangerous(role):
            return await ctx.warn("That role has dangerous permissions and can't be handed out automatically.")

        await self.bot.pool.execute(
            "INSERT INTO birthday_config (guild_id, role_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE role_id = VALUES(role_id)",
            ctx.guild.id, role.id,
        )
        return await ctx.approve(f"Members will now receive {role.mention} on their birthday")

    @birthday.command(
        name="disable",
        aliases=["off"],
        description="Disable birthday announcements and roles here.",
        example="birthday disable",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def birthday_disable(self, ctx: Context) -> Message:
        cfg = await self.bot.pool.fetchrow(
            "SELECT guild_id FROM birthday_config WHERE guild_id = %s", ctx.guild.id
        )
        if not cfg:
            return await ctx.warn("Birthdays aren't set up in this server.")
        await self.bot.pool.execute("DELETE FROM birthday_config WHERE guild_id = %s", ctx.guild.id)
        await self.bot.pool.execute("DELETE FROM birthday_roles WHERE guild_id = %s", ctx.guild.id)
        return await ctx.approve("Birthday announcements and the birthday role were disabled.")

    @birthday.group(
        name="celebrate",
        invoke_without_command=True,
        description="List roles pinged in birthday announcements.",
        example="birthday celebrate",
    )
    @guild_only()
    async def birthday_celebrate(self, ctx: Context) -> Message:
        rows = await self.bot.pool.fetch(
            "SELECT role_id FROM birthday_roles WHERE guild_id = %s", ctx.guild.id
        )
        roles = [r for row in rows if (r := ctx.guild.get_role(row["role_id"]))]
        if not roles:
            return await ctx.warn(
                f"No celebration roles set. Use `{ctx.clean_prefix}birthday celebrate add @role`."
            )
        return await ctx.embed(
            title="Celebration roles",
            description="\n".join(role.mention for role in roles),
            footer={"text": "These roles are pinged in birthday announcements"},
        )

    @birthday_celebrate.command(
        name="add",
        aliases=["create"],
        description="Add a celebration role.",
        example="birthday celebrate add @Birthday Squad",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def birthday_celebrate_add(self, ctx: Context, role: Role) -> Message:
        if role.is_default():
            return await ctx.warn("You can't use @everyone as a celebration role.")
        rows = await self.bot.pool.fetch(
            "SELECT role_id FROM birthday_roles WHERE guild_id = %s", ctx.guild.id
        )
        if any(row["role_id"] == role.id for row in rows):
            return await ctx.warn(f"{role.mention} is already a celebration role.")
        if len(rows) >= MAX_PING_ROLES:
            return await ctx.warn(f"You can only have **{MAX_PING_ROLES}** celebration roles.")
        await self.bot.pool.execute(
            "INSERT INTO birthday_roles (guild_id, role_id) VALUES (%s, %s)",
            ctx.guild.id, role.id,
        )
        return await ctx.approve(f"Added {role.mention} as a celebration role")

    @birthday_celebrate.command(
        name="remove",
        aliases=["delete", "del", "rm"],
        description="Remove a celebration role.",
        example="birthday celebrate remove @Birthday Squad",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def birthday_celebrate_remove(self, ctx: Context, role: Role) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT role_id FROM birthday_roles WHERE guild_id = %s AND role_id = %s",
            ctx.guild.id, role.id,
        )
        if not row:
            return await ctx.warn(f"{role.mention} isn't a celebration role.")
        await self.bot.pool.execute(
            "DELETE FROM birthday_roles WHERE guild_id = %s AND role_id = %s",
            ctx.guild.id, role.id,
        )
        return await ctx.approve(f"Removed {role.mention} from the celebration roles")

    # ------------------------------------------------------------------ #
    # Birthday: background scheduler
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=10)
    async def birthday_loop(self) -> None:
        try:
            await self._run_birthdays()
        except Exception as e:
            logger.error(f"Birthday loop failed: {e}")

    @birthday_loop.before_loop
    async def before_birthday_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_birthdays(self) -> None:
        pool = self.bot.pool
        today = utc_today()

        query = "SELECT user_id FROM birthdays WHERE month = %s AND day = %s"
        if today.month == 2 and today.day == 28 and not calendar.isleap(today.year):
            query += " OR (month = 2 AND day = 29)"
        today_ids: Set[int] = {r["user_id"] for r in await pool.fetch(query, today.month, today.day)}

        configs = await pool.fetch("SELECT guild_id, channel_id, role_id FROM birthday_config")
        ping_rows = await pool.fetch("SELECT guild_id, role_id FROM birthday_roles")
        ping_map: Dict[int, List[int]] = {}
        for row in ping_rows:
            ping_map.setdefault(row["guild_id"], []).append(row["role_id"])

        for cfg in configs:
            guild = self.bot.get_guild(cfg["guild_id"])
            if guild is None:
                continue
            channel = guild.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
            role = guild.get_role(cfg["role_id"]) if cfg["role_id"] else None
            ping_roles = [r for rid in ping_map.get(guild.id, []) if (r := guild.get_role(rid))]

            for user_id in today_ids:
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    continue

                logged = await pool.fetchval(
                    "SELECT 1 FROM birthday_log WHERE guild_id = %s AND user_id = %s AND year = %s",
                    guild.id, user_id, today.year,
                )
                if not logged:
                    await pool.execute(
                        "INSERT INTO birthday_log (guild_id, user_id, year) VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE year = VALUES(year)",
                        guild.id, user_id, today.year,
                    )
                    if isinstance(channel, TextChannel):
                        await self._announce(guild, channel, member, ping_roles)

                if role and role not in member.roles:
                    try:
                        await member.add_roles(role, reason="Birthday")
                    except discord.HTTPException:
                        pass

            if role:
                for member in list(role.members):
                    if member.id not in today_ids:
                        try:
                            await member.remove_roles(role, reason="Birthday is over")
                        except discord.HTTPException:
                            pass
            await asyncio.sleep(0)

    async def _announce(
        self, guild: discord.Guild, channel: TextChannel, member: Member, ping_roles: List[Role]
    ) -> None:
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return
        embed = Embed(
            title="🎂 Happy Birthday!",
            description=random.choice(BIRTHDAY_MESSAGES).format(member=member.mention),
            color=COLORS.neutral,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        content = " ".join([member.mention] + [r.mention for r in ping_roles])
        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=AllowedMentions(users=[member], roles=ping_roles),
            )
        except discord.HTTPException as e:
            logger.warning(f"Could not send birthday message in {guild.id}: {e}")

    # ------------------------------------------------------------------ #
    # Diary
    # ------------------------------------------------------------------ #

    async def _send_diary_list(self, ctx: Context, member: Member, rows: List[dict]) -> Message:
        own = member.id == ctx.author.id
        if not rows:
            who = "You don't" if own else f"**{member.display_name}** doesn't"
            return await ctx.warn(f"{who} have any diary entries.")
        lines = []
        for row in rows:
            lock = f"{EMOJIS.LOCK} " if row["is_private"] else ""
            lines.append(
                f"{lock}**#{row['entry_id']}** · <t:{row['created_at']}:d> — {truncate(row['content'], 80)}"
            )
        embeds = paged(f"{member.display_name}'s diary", lines, "entries", per_page=8)
        return await ctx.paginate(embeds)

    @hybrid_group(
        name="diary",
        aliases=["journal"],
        invoke_without_command=True,
        description="List your diary entries.",
        example="diary",
    )
    async def diary(self, ctx: Context) -> Message:
        rows = await self.bot.pool.fetch(
            "SELECT entry_id, content, created_at, is_private FROM diary "
            "WHERE user_id = %s ORDER BY entry_id",
            ctx.author.id,
        )
        return await self._send_diary_list(ctx, ctx.author, rows)

    @diary.command(
        name="add",
        aliases=["write", "new", "create"],
        description="Add a diary entry.",
        example="diary add today was a good day",
    )
    @cooldown(1, 5, BucketType.user)
    async def diary_add(self, ctx: Context, *, entry: str) -> Message:
        entry = entry.strip()
        if len(entry) > DIARY_MAX_LENGTH:
            return await ctx.warn(f"Entries can be at most **{DIARY_MAX_LENGTH}** characters.")
        count = await self.bot.pool.fetchval(
            "SELECT COUNT(*) FROM diary WHERE user_id = %s", ctx.author.id
        )
        if count >= MAX_DIARY_ENTRIES:
            return await ctx.warn(f"You've reached the limit of **{MAX_DIARY_ENTRIES}** entries.")

        entry_id = await self.bot.pool.fetchval(
            "SELECT COALESCE(MAX(entry_id), 0) + 1 FROM diary WHERE user_id = %s", ctx.author.id
        )
        await self.bot.pool.execute(
            "INSERT INTO diary (user_id, entry_id, content, created_at) VALUES (%s, %s, %s, %s)",
            ctx.author.id, entry_id, entry, int(datetime.now(timezone.utc).timestamp()),
        )
        return await ctx.approve(
            f"Saved as entry **#{entry_id}**. Entries are public unless you run "
            f"`{ctx.clean_prefix}diary privacy {entry_id}`."
        )

    @diary.command(
        name="remove",
        aliases=["delete", "del", "rm"],
        description="Remove one of your diary entries.",
        example="diary remove 1",
    )
    async def diary_remove(self, ctx: Context, entry_id: int) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT entry_id FROM diary WHERE user_id = %s AND entry_id = %s",
            ctx.author.id, entry_id,
        )
        if not row:
            return await ctx.warn(f"You don't have an entry **#{entry_id}**.")
        await self.bot.pool.execute(
            "DELETE FROM diary WHERE user_id = %s AND entry_id = %s", ctx.author.id, entry_id
        )
        return await ctx.approve(f"Removed entry **#{entry_id}**")

    @diary.command(
        name="privacy",
        aliases=["private", "public", "toggle"],
        description="Toggle whether an entry is private.",
        example="diary privacy 1",
    )
    async def diary_privacy(self, ctx: Context, entry_id: int) -> Message:
        row = await self.bot.pool.fetchrow(
            "SELECT is_private FROM diary WHERE user_id = %s AND entry_id = %s",
            ctx.author.id, entry_id,
        )
        if not row:
            return await ctx.warn(f"You don't have an entry **#{entry_id}**.")
        new_value = 0 if row["is_private"] else 1
        await self.bot.pool.execute(
            "UPDATE diary SET is_private = %s WHERE user_id = %s AND entry_id = %s",
            new_value, ctx.author.id, entry_id,
        )
        state = "**private**" if new_value else "**public**"
        return await ctx.approve(f"Entry **#{entry_id}** is now {state}")

    @diary.command(
        name="view",
        aliases=["read", "show"],
        description="View a diary entry (yours or someone else's).",
        example="diary view @voby 1",
    )
    @cooldown(1, 3, BucketType.user)
    async def diary_view(
        self,
        ctx: Context,
        member: Optional[Member] = None,
        entry_id: Optional[int] = None,
    ) -> Message:
        member = member or ctx.author
        own = member.id == ctx.author.id

        if entry_id is None:
            query = (
                "SELECT entry_id, content, created_at, is_private FROM diary WHERE user_id = %s"
                + ("" if own else " AND is_private = 0")
                + " ORDER BY entry_id"
            )
            rows = await self.bot.pool.fetch(query, member.id)
            return await self._send_diary_list(ctx, member, rows)

        row = await self.bot.pool.fetchrow(
            "SELECT content, created_at, is_private FROM diary WHERE user_id = %s AND entry_id = %s",
            member.id, entry_id,
        )
        if not row or (row["is_private"] and not own):
            return await ctx.warn(f"No public entry **#{entry_id}** found for **{member.display_name}**.")

        written = datetime.fromtimestamp(row["created_at"], timezone.utc)
        return await ctx.embed(
            author={"name": f"{member.display_name}'s diary", "icon_url": member.display_avatar.url},
            description=row["content"],
            footer={"text": f"Entry #{entry_id} ∙ {written:%b %d, %Y}"},
        )

    # ------------------------------------------------------------------ #
    # Conversation core
    # ------------------------------------------------------------------ #

    def _history_get(self, key: Any) -> List[Dict[str, str]]:
        entry = self._history.get(key)
        if not entry:
            return []
        stamp, messages = entry
        if time.monotonic() - stamp > HISTORY_TTL:
            self._history.pop(key, None)
            return []
        return list(messages)

    def _history_add(self, key: Any, user_content: str, reply: str) -> None:
        messages = self._history_get(key)
        messages += [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": reply},
        ]
        self._history[key] = (time.monotonic(), messages[-HISTORY_MESSAGES:])
        if len(self._history) > 500:
            now = time.monotonic()
            for k in [k for k, (t, _) in self._history.items() if now - t > HISTORY_TTL]:
                self._history.pop(k, None)

    async def _exec_tool(self, call: Dict[str, Any]) -> str:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        result = await self.web.run(fn.get("name", ""), args)
        return result[:TOOL_RESULT_MAX]

    async def _complete(self, messages: List[Dict[str, Any]]) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await self.kilo.chat(messages, tools=TOOLS)
            calls = msg["tool_calls"]
            if not calls:
                if msg["content"]:
                    return msg["content"]
                raise KiloError("empty response")
            messages.append(
                {"role": "assistant", "content": msg["content"] or "", "tool_calls": calls}
            )
            for call in calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": await self._exec_tool(call),
                    }
                )
        final = await self.kilo.chat(messages)  # tools off: force a final answer
        if not final["content"]:
            raise KiloError("empty response")
        return final["content"]

    async def _converse(self, key: Any, display_name: str, text: str, named: bool) -> str:
        user_content = f"{display_name}: {text}" if named else text
        system = SYSTEM_PROMPT.format(
            bot=self.bot.user.display_name if self.bot.user else "Hollow",
            date=datetime.now(timezone.utc).strftime("%A, %B %d, %Y"),
        )
        messages: List[Dict[str, Any]] = (
            [{"role": "system", "content": system}]
            + self._history_get(key)
            + [{"role": "user", "content": user_content}]
        )
        async with self._chat_sem:
            reply = await self._complete(messages)
        reply = neutralize(reply)
        self._history_add(key, user_content, reply)
        return reply

    # ------------------------------------------------------------------ #
    # chatgpt
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="chatgpt",
        aliases=["ask", "ai", "gpt"],
        description="Talk to the AI chatbot.",
        example="chatgpt what's the weather like on Mars?",
    )
    @cooldown(1, 5, BucketType.user)
    async def chatgpt(self, ctx: Context, *, prompt: str) -> Optional[Message]:
        prompt = prompt.strip()
        if len(prompt) > MAX_PROMPT_LENGTH:
            return await ctx.warn(f"That's too long. The limit is **{MAX_PROMPT_LENGTH}** characters.")
        async with ctx.typing():
            try:
                reply = await self._converse(("u", ctx.author.id), ctx.author.display_name, prompt, named=False)
            except Exception as e:
                logger.error(f"chatgpt failed: {e}")
                return await ctx.warn("The AI is unavailable right now. Try again in a bit.")

        parts = split_message(reply)
        message = await ctx.reply(parts[0], allowed_mentions=NO_MENTIONS)
        for part in parts[1:]:
            await ctx.channel.send(part, allowed_mentions=NO_MENTIONS)
        return message

    # ------------------------------------------------------------------ #
    # Chatbot (mentions, replies, autorespond channels)
    # ------------------------------------------------------------------ #

    async def _config(self, guild_id: int) -> GuildChat:
        cfg = self._configs.get(guild_id)
        if cfg is None:
            row = await self.bot.pool.fetchrow(
                "SELECT enabled FROM chatbot_config WHERE guild_id = %s", guild_id
            )
            rows = await self.bot.pool.fetch(
                "SELECT channel_id FROM chatbot_channels WHERE guild_id = %s", guild_id
            )
            cfg = GuildChat(bool(row and row["enabled"]), {r["channel_id"] for r in rows})
            self._configs[guild_id] = cfg
        return cfg

    async def _is_reply_to_bot(self, message: Message) -> bool:
        ref = message.reference
        if not ref or not ref.message_id:
            return False
        resolved = ref.resolved if isinstance(ref.resolved, Message) else ref.cached_message
        if resolved is None:
            try:
                resolved = await message.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return False
        return resolved.author.id == self.bot.user.id

    @Cog.listener("on_message")
    async def chatbot_listener(self, message: Message) -> None:
        if message.author.bot or message.guild is None or message.webhook_id:
            return
        try:
            await self._handle_message(message)
        except Exception as e:
            logger.error(f"Chatbot listener failed: {e}")

    async def _handle_message(self, message: Message) -> None:
        cfg = await self._config(message.guild.id)
        if not cfg.enabled:
            return

        me = message.guild.me
        in_channel = message.channel.id in cfg.channels
        mentioned = me in message.mentions
        replied = False if (in_channel or mentioned) else await self._is_reply_to_bot(message)
        if not (in_channel or mentioned or replied):
            return
        if not message.channel.permissions_for(me).send_messages:
            return

        text = re.sub(rf"<@!?{me.id}>", "", message.content).strip()
        if not text:
            if in_channel or not mentioned:
                return
            text = "hi"
        text = text[:MAX_PROMPT_LENGTH]

        # Cheap checks first, then anything that touches the database.
        now = time.monotonic()
        if now - self._last_reply.get(message.author.id, 0) < USER_REPLY_INTERVAL:
            return
        if await self.bot.pool.fetchval("SELECT 1 FROM blacklist WHERE user_id = %s", message.author.id):
            return
        ctx = await self.bot.get_context(message)
        if ctx.valid:  # it's a command, not a chat message
            return
        self._last_reply[message.author.id] = now
        if len(self._last_reply) > 2000:
            self._last_reply = {u: t for u, t in self._last_reply.items() if now - t < 60}

        async with message.channel.typing():
            try:
                reply = await self._converse(
                    ("c", message.channel.id), message.author.display_name, text, named=True
                )
            except Exception as e:
                logger.error(f"Chatbot reply failed: {e}")
                return

        parts = split_message(reply)
        try:
            await message.reply(parts[0], mention_author=False, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            await message.channel.send(parts[0], allowed_mentions=NO_MENTIONS)
        for part in parts[1:]:
            await message.channel.send(part, allowed_mentions=NO_MENTIONS)

    @hybrid_group(
        name="chatbot",
        aliases=["cb", "aibot"],
        invoke_without_command=True,
        description="Manage the AI chatbot.",
        example="chatbot enable",
    )
    @guild_only()
    async def chatbot(self, ctx: Context) -> Message:
        return await ctx.send_help(ctx.command)

    @chatbot.command(
        name="enable",
        aliases=["on"],
        description="Enable the AI chatbot in this server.",
        example="chatbot enable",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def chatbot_enable(self, ctx: Context) -> Message:
        cfg = await self._config(ctx.guild.id)
        if cfg.enabled:
            return await ctx.warn("The chatbot is already enabled.")
        await self.bot.pool.execute(
            "INSERT INTO chatbot_config (guild_id, enabled) VALUES (%s, 1) "
            "ON DUPLICATE KEY UPDATE enabled = 1",
            ctx.guild.id,
        )
        self._configs.pop(ctx.guild.id, None)
        return await ctx.approve(
            f"The chatbot is now **enabled**. Members can mention {self.bot.user.mention} or reply "
            "to its messages. Messages sent to it are processed by a third-party AI service."
        )

    @chatbot.command(
        name="disable",
        aliases=["off"],
        description="Disable the AI chatbot in this server.",
        example="chatbot disable",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def chatbot_disable(self, ctx: Context) -> Message:
        cfg = await self._config(ctx.guild.id)
        if not cfg.enabled:
            return await ctx.warn("The chatbot isn't enabled.")
        await self.bot.pool.execute(
            "UPDATE chatbot_config SET enabled = 0 WHERE guild_id = %s", ctx.guild.id
        )
        self._configs.pop(ctx.guild.id, None)
        return await ctx.approve("The chatbot is now **disabled**.")

    @chatbot.command(
        name="status",
        aliases=["settings", "config"],
        description="Show the chatbot settings.",
        example="chatbot status",
    )
    @guild_only()
    async def chatbot_status(self, ctx: Context) -> Message:
        cfg = await self._config(ctx.guild.id)
        channels = [c.mention for cid in cfg.channels if (c := ctx.guild.get_channel(cid))]
        return await ctx.embed(
            title="Chatbot",
            description=(
                f"**Status:** {'enabled' if cfg.enabled else 'disabled'}\n"
                f"**Autorespond channels:** {', '.join(channels) if channels else 'none'}"
            ),
        )

    @chatbot.group(
        name="channel",
        aliases=["channels", "ch"],
        invoke_without_command=True,
        description="List the chatbot autorespond channels.",
        example="chatbot channel",
    )
    @guild_only()
    async def chatbot_channel(self, ctx: Context) -> Message:
        cfg = await self._config(ctx.guild.id)
        channels = [c.mention for cid in cfg.channels if (c := ctx.guild.get_channel(cid))]
        if not channels:
            return await ctx.warn(
                f"No autorespond channels. Use `{ctx.clean_prefix}chatbot channel set #channel`."
            )
        return await ctx.embed(title="Chatbot channels", description="\n".join(channels))

    @chatbot_channel.command(
        name="set",
        aliases=["add"],
        description="Make the chatbot answer every message in a channel.",
        example="chatbot channel set #chat",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def chatbot_channel_set(self, ctx: Context, channel: TextChannel) -> Message:
        cfg = await self._config(ctx.guild.id)
        if channel.id in cfg.channels:
            return await ctx.warn(f"{channel.mention} is already a chatbot channel.")
        if len(cfg.channels) >= MAX_CHAT_CHANNELS:
            return await ctx.warn(f"You can have at most **{MAX_CHAT_CHANNELS}** chatbot channels.")
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await ctx.warn(f"I need to see and send messages in {channel.mention}.")

        await self.bot.pool.execute(
            "INSERT IGNORE INTO chatbot_channels (guild_id, channel_id) VALUES (%s, %s)",
            ctx.guild.id, channel.id,
        )
        self._configs.pop(ctx.guild.id, None)
        note = "" if cfg.enabled else f" Run `{ctx.clean_prefix}chatbot enable` to turn it on."
        return await ctx.approve(f"The chatbot will now answer every message in {channel.mention}.{note}")

    @chatbot_channel.command(
        name="reset",
        aliases=["remove", "clear", "delete"],
        description="Remove one or all chatbot channels.",
        example="chatbot channel reset #chat",
    )
    @has_permissions(manage_guild=True)
    @guild_only()
    async def chatbot_channel_reset(
        self, ctx: Context, channel: Optional[TextChannel] = None
    ) -> Message:
        cfg = await self._config(ctx.guild.id)
        if not cfg.channels:
            return await ctx.warn("There are no chatbot channels.")
        if channel is None:
            await self.bot.pool.execute("DELETE FROM chatbot_channels WHERE guild_id = %s", ctx.guild.id)
            message = "Removed all chatbot channels."
        else:
            if channel.id not in cfg.channels:
                return await ctx.warn(f"{channel.mention} isn't a chatbot channel.")
            await self.bot.pool.execute(
                "DELETE FROM chatbot_channels WHERE guild_id = %s AND channel_id = %s",
                ctx.guild.id, channel.id,
            )
            message = f"{channel.mention} is no longer a chatbot channel."
        self._configs.pop(ctx.guild.id, None)
        return await ctx.approve(message)

    # ------------------------------------------------------------------ #
    # imagine (DeepAI)
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="imagine",
        aliases=["draw", "dream", "aiimage"],
        description="Generate an image from a prompt.",
        example="imagine a cat riding a skateboard",
    )
    @cooldown(1, 30, BucketType.user)
    async def imagine(self, ctx: Context, *, prompt: str) -> Optional[Message]:
        if self.deepai is None:
            return await ctx.warn("Image generation isn't available right now.")
        prompt = prompt.strip()
        if len(prompt) > MAX_IMAGINE_LENGTH:
            return await ctx.warn(f"That's too long. The limit is **{MAX_IMAGINE_LENGTH}** characters.")

        async with ctx.typing():
            try:
                async with self._media_sem:
                    result = await self.deepai.ask_async(prompt, model="deepai/image")
                url = getattr(result, "image_url", "") or ""
                if not url:
                    return await ctx.warn("I couldn't generate that image. Try a different prompt.")
                data, content_type = await self.web.fetch_bytes(url, MAX_IMAGE_BYTES)
            except Exception as e:
                logger.error(f"imagine failed: {e}")
                return await ctx.warn("Image generation failed. Try again in a bit.")

        filename = f"imagine.{image_ext(content_type, data)}"
        embed = Embed(color=COLORS.neutral, description=f"**{truncate(prompt, 200)}**")
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text="Generated with DeepAI")
        return await ctx.send(embed=embed, file=discord.File(io.BytesIO(data), filename=filename))

    # ------------------------------------------------------------------ #
    # tts (MusicMake)
    # ------------------------------------------------------------------ #

    async def _speak(self, ctx: Context, text: str, voice: str) -> Optional[Message]:
        if self.musicmake is None:
            return await ctx.warn("Text to speech isn't available right now.")
        text = re.sub(r"<(?:@[!&]?|#|a?:\w+:)\d*>?", "", text)  # mentions / custom emoji
        text = " ".join(text.split())
        if not text:
            return await ctx.warn("There's nothing to say.")
        if len(text) > MAX_TTS_LENGTH:
            return await ctx.warn(f"That's too long. The limit is **{MAX_TTS_LENGTH}** characters.")

        async with ctx.typing():
            try:
                async with self._media_sem:
                    result = await self.musicmake.speak_async(text, voice=voice)
                audio = getattr(result, "audio", b"") or b""
            except Exception as e:
                logger.error(f"tts failed: {e}")
                return await ctx.warn("Text to speech failed. Try again in a bit.")

        if not audio:
            return await ctx.warn("I couldn't generate that audio. Try again in a bit.")
        limit = ctx.guild.filesize_limit if ctx.guild else 8 * 1024 * 1024
        if len(audio) > limit:
            return await ctx.warn("The generated audio is too large to upload here.")

        embed = Embed(color=COLORS.neutral, description=truncate(text, 200))
        embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"Voice: {voice}")
        filename = f"tts.{audio_ext(audio)}"
        return await ctx.send(embed=embed, file=discord.File(io.BytesIO(audio), filename=filename))

    @hybrid_group(
        name="tts",
        fallback="say",
        invoke_without_command=True,
        description="Turn text into speech.",
        example="tts hello world",
    )
    @cooldown(1, 15, BucketType.user)
    async def tts(self, ctx: Context, *, text: str) -> Optional[Message]:
        return await self._speak(ctx, text, DEFAULT_VOICE)

    @tts.command(
        name="voice",
        aliases=["as"],
        description="Turn text into speech with a specific voice.",
        example="tts voice breeze hello world",
    )
    @cooldown(1, 15, BucketType.user)
    async def tts_voice(self, ctx: Context, voice: str, *, text: str) -> Optional[Message]:
        voice = voice.lower()
        if voice not in TTS_VOICES:
            return await ctx.warn(
                f"Unknown voice. See `{ctx.clean_prefix}tts voices` for the list."
            )
        return await self._speak(ctx, text, voice)

    @tts.command(
        name="voices",
        aliases=["list"],
        description="List the available TTS voices.",
        example="tts voices",
    )
    async def tts_voices(self, ctx: Context) -> Message:
        return await ctx.embed(
            title="TTS voices",
            description=", ".join(f"`{v}`" for v in TTS_VOICES),
            footer={"text": f"Default: {DEFAULT_VOICE}"},
        )
