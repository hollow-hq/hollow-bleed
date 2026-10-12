# File: cogs/engagement/highlight.py
"""Keyword highlight notifications for the engagement cog.

This module provides :class:`HighlightMixin`, a plain mixin that is blended
into the existing :class:`~cogs.engagement.engagement.Engagement` cog, exactly
the way ``LevelMixin`` owns the leveling half of that cog. It owns the
``highlight`` command group (with its ``ignore`` sub-group), the keyword
matching ``on_message`` listener and every helper, and deliberately does
**not** create a new cog of its own.

Highlights are per-user and per-guild: when a message contains one of a
member's keywords the member is DM'd with the keyword, the channel, the
author, an excerpt of the message and a jump link.

Every helper and attribute belonging to this mixin is prefixed with ``_hl_``
to avoid colliding with the (large) giveaway implementation in
``engagement.py`` or with the ``_level_`` names used by the leveling mixin.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import discord
from discord import Embed, Member, Message, Role, TextChannel
from discord.ext import commands
from discord.ext.commands import guild_only

from base.config import COLORS
from base.context import Context
from base.managers.commands import hybrid_group

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Shortest keyword a member may highlight (shorter words match too much).
HL_MIN_KEYWORD_LENGTH = 3

#: Longest keyword a member may highlight (matches the column width).
HL_MAX_KEYWORD_LENGTH = 100

#: Maximum number of keywords one member may highlight in a single guild.
HL_MAX_KEYWORDS = 25

#: Seconds a matched keyword stays quiet for one member, to prevent DM spam.
HL_NOTIFY_COOLDOWN = 60

#: How many keyword lines one embed page of ``,highlight list`` shows.
HL_LIST_PAGE_SIZE = 25

#: Bound on the in-memory notification cooldown map before expired entries
#: are pruned. The map is keyed by (guild_id, user_id, keyword).
HL_COOLDOWN_CACHE_LIMIT = 5000

#: Maximum characters of the original message quoted in a notification DM.
HL_EXCERPT_LIMIT = 300

#: Valid ``mode`` values for the ignore table (user / channel / role).
HL_IGNORE_MODES: Tuple[str, str, str] = ("user", "channel", "role")


# --------------------------------------------------------------------------- #
# Keyword matching helpers
# --------------------------------------------------------------------------- #


def _hl_normalise_keyword(raw: Any) -> str:
    """Return the canonical (lowercased, whitespace-collapsed) keyword form."""
    return " ".join(str(raw or "").split()).lower()


def _hl_build_pattern(keyword: str) -> re.Pattern[str]:
    """Compile a whole-word/phrase matcher for a normalised ``keyword``.

    The pattern is anchored with non-word-character lookarounds instead of
    ``\\b`` so it also behaves when a keyword starts or ends with punctuation,
    and it is intended to be searched against lowercased message content.
    """
    return re.compile(r"(?<![0-9a-z_])" + re.escape(keyword) + r"(?![0-9a-z_])")


def _hl_is_duplicate_error(exc: Exception) -> bool:
    """Return ``True`` when a DB error is a duplicate-key violation."""
    if "duplicate" in str(exc).lower():
        return True
    args = getattr(exc, "args", None) or ()
    return any(code == 1062 for code in args)


@dataclass
class _HLKeywordEntry:
    """One keyword of a guild plus the members highlighting it.

    ``user_ids`` are every member in the guild who highlighted ``keyword``
    and ``pattern`` is the compiled matcher used by the listener.
    """

    keyword: str
    user_ids: Set[int]
    pattern: re.Pattern[str]


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


class HighlightStore:
    """Thin data-access layer for the highlight tables.

    Stateless: every call goes through ``bot.pool`` using parameterised
    queries scoped by ``guild_id``, mirroring :class:`LevelStore` and
    :class:`~cogs.engagement.engagement.GiveawayStore`.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @property
    def pool(self) -> Any:
        return self.bot.pool

    # -- keywords -------------------------------------------------------- #

    async def keywords_for_guild(self, guild_id: int) -> List[dict]:
        """Return every ``(user_id, keyword)`` row of the guild."""
        return await self.pool.fetch(
            "SELECT user_id, keyword FROM highlight_keywords WHERE guild_id = %s",
            guild_id,
        )

    async def keywords_for_user(self, guild_id: int, user_id: int) -> List[str]:
        """Return the member's keywords in the guild, alphabetically."""
        rows = await self.pool.fetch(
            "SELECT keyword FROM highlight_keywords "
            "WHERE guild_id = %s AND user_id = %s ORDER BY keyword ASC",
            guild_id,
            user_id,
        )
        return [str(row["keyword"]) for row in rows]

    async def keyword_exists(self, guild_id: int, user_id: int, keyword: str) -> bool:
        """Return ``True`` when the member already highlights ``keyword``."""
        row = await self.pool.fetchval(
            "SELECT 1 FROM highlight_keywords "
            "WHERE guild_id = %s AND user_id = %s AND keyword = %s",
            guild_id,
            user_id,
            keyword,
        )
        return row is not None

    async def count_keywords(self, guild_id: int, user_id: int) -> int:
        """Return how many keywords the member has in the guild."""
        value = await self.pool.fetchval(
            "SELECT COUNT(*) FROM highlight_keywords "
            "WHERE guild_id = %s AND user_id = %s",
            guild_id,
            user_id,
        )
        return int(value or 0)

    async def add_keyword(self, guild_id: int, user_id: int, keyword: str) -> None:
        """Insert a keyword; a unique-key violation is treated as a duplicate."""
        await self.pool.execute(
            "INSERT INTO highlight_keywords "
            "(guild_id, user_id, keyword, created_at) VALUES (%s, %s, %s, %s)",
            guild_id,
            user_id,
            keyword,
            int(time.time()),
        )

    async def remove_keyword(self, guild_id: int, user_id: int, keyword: str) -> bool:
        """Delete one keyword; returns ``False`` when it was not present."""
        existing = await self.keyword_exists(guild_id, user_id, keyword)
        if not existing:
            return False
        await self.pool.execute(
            "DELETE FROM highlight_keywords "
            "WHERE guild_id = %s AND user_id = %s AND keyword = %s",
            guild_id,
            user_id,
            keyword,
        )
        return True

    async def reset_keywords(self, guild_id: int, user_id: int) -> int:
        """Delete every keyword of the member, returning how many were removed."""
        count = await self.count_keywords(guild_id, user_id)
        if count:
            await self.pool.execute(
                "DELETE FROM highlight_keywords "
                "WHERE guild_id = %s AND user_id = %s",
                guild_id,
                user_id,
            )
        return count

    # -- ignores --------------------------------------------------------- #

    async def ignores_for_guild(self, guild_id: int) -> List[dict]:
        """Return every ``(user_id, object_id, mode)`` ignore row of the guild."""
        return await self.pool.fetch(
            "SELECT user_id, object_id, mode FROM highlight_ignores "
            "WHERE guild_id = %s",
            guild_id,
        )

    async def ignores_for_user(self, guild_id: int, user_id: int) -> List[dict]:
        """Return the member's ignore rows in the guild."""
        return await self.pool.fetch(
            "SELECT object_id, mode FROM highlight_ignores "
            "WHERE guild_id = %s AND user_id = %s ORDER BY mode, object_id",
            guild_id,
            user_id,
        )

    async def toggle_ignore(
        self, guild_id: int, user_id: int, object_id: int, mode: str
    ) -> bool:
        """Toggle an ignore row; returns ``True`` when it is now ignored."""
        existing = await self.pool.fetchval(
            "SELECT object_id FROM highlight_ignores "
            "WHERE guild_id = %s AND user_id = %s AND object_id = %s AND mode = %s",
            guild_id,
            user_id,
            object_id,
            mode,
        )
        if existing is not None:
            await self.pool.execute(
                "DELETE FROM highlight_ignores "
                "WHERE guild_id = %s AND user_id = %s AND object_id = %s AND mode = %s",
                guild_id,
                user_id,
                object_id,
                mode,
            )
            return False
        await self.pool.execute(
            "INSERT INTO highlight_ignores (guild_id, user_id, object_id, mode) "
            "VALUES (%s, %s, %s, %s)",
            guild_id,
            user_id,
            object_id,
            mode,
        )
        return True


# --------------------------------------------------------------------------- #
# Mixin
# --------------------------------------------------------------------------- #


class HighlightMixin:
    """Highlight commands and the keyword listener blended into Engagement.

    Never defines ``__init__`` or ``setup``: per-instance state is created by
    :meth:`_init_highlight`, which the host cog calls from its own
    ``__init__`` (or lazily through :meth:`_hl_ready`).
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _init_highlight(self) -> None:
        """Initialise the highlight caches and cooldowns (idempotent)."""
        if getattr(self, "_hl_ready_called", False):
            return
        self._hl_store = HighlightStore(self.bot)
        #: guild_id -> keyword entries (rebuilt from the DB on invalidation).
        self._hl_cache: Dict[int, List[_HLKeywordEntry]] = {}
        #: guild_id -> user_id -> {"channel"|"user"|"role": set(object_id)}.
        self._hl_ignores: Dict[int, Dict[int, Dict[str, Set[int]]]] = {}
        #: (guild_id, user_id, keyword) -> epoch seconds of the last DM.
        self._hl_cooldowns: Dict[Tuple[int, int, str], float] = {}
        self._hl_ready_called = True

    def _hl_ready(self) -> None:
        """Ensure the mixin state exists before use (lazy fallback)."""
        if not getattr(self, "_hl_ready_called", False):
            self._init_highlight()

    def _hl_invalidate(self, guild_id: int) -> None:
        """Drop the cached keyword/ignore state of one guild."""
        self._hl_cache.pop(guild_id, None)
        self._hl_ignores.pop(guild_id, None)

    # ------------------------------------------------------------------ #
    # Listener
    # ------------------------------------------------------------------ #

    @commands.Cog.listener("on_message")
    async def _hl_on_message(self, message: Message) -> None:
        """Match keywords and DM the members who highlighted them."""
        try:
            await self._hl_handle_message(message)
        except Exception as exc:  # pragma: no cover - a listener must never raise
            _log.warning("Highlight listener failed: %s", exc)

    async def _hl_handle_message(self, message: Message) -> None:
        """Core keyword matching and notification logic for one message."""
        self._hl_ready()
        if message.guild is None or not isinstance(message.author, Member):
            return
        if message.author.bot:
            return
        if not message.content:
            return

        guild_id = message.guild.id
        index = self._hl_cache.get(guild_id)
        if index is None:
            index = await self._hl_load_guild(guild_id)
        if not index:
            return

        content = message.content.lower()
        matches: Dict[int, List[str]] = {}
        for entry in index:
            if entry.pattern.search(content) is None:
                continue
            for user_id in entry.user_ids:
                if user_id == message.author.id:
                    continue
                matches.setdefault(user_id, []).append(entry.keyword)
        if not matches:
            return

        ignores = await self._hl_load_ignores(guild_id)
        now = time.time()
        for user_id, keywords in matches.items():
            member = message.guild.get_member(user_id)
            if member is None or member.bot:
                continue
            if self._hl_is_ignored(message, member, ignores.get(user_id, {})):
                continue
            if not self._hl_can_view(message.channel, member):
                continue
            fresh = [
                keyword
                for keyword in keywords
                if self._hl_off_cooldown(guild_id, user_id, keyword, now)
            ]
            if not fresh:
                continue
            await self._hl_notify(member, message, fresh)

    async def _hl_load_guild(self, guild_id: int) -> List[_HLKeywordEntry]:
        """Load (and cache) every keyword entry of a guild from the database."""
        try:
            rows = await self._hl_store.keywords_for_guild(guild_id)
        except Exception as exc:
            # Cache an empty index so a DB outage cannot turn the listener
            # into a hot loop; the next add/remove/reset re-arms the cache.
            _log.warning("Failed to load highlights for guild %s: %s", guild_id, exc)
            self._hl_cache[guild_id] = []
            return []

        merged: Dict[str, Set[int]] = {}
        for row in rows:
            keyword = str(row["keyword"])
            merged.setdefault(keyword, set()).add(int(row["user_id"]))
        index = [
            _HLKeywordEntry(
                keyword=keyword,
                user_ids=user_ids,
                pattern=_hl_build_pattern(keyword),
            )
            for keyword, user_ids in merged.items()
        ]
        self._hl_cache[guild_id] = index
        return index

    async def _hl_load_ignores(
        self, guild_id: int
    ) -> Dict[int, Dict[str, Set[int]]]:
        """Load (and cache) the ignore lists of a guild from the database."""
        cached = self._hl_ignores.get(guild_id)
        if cached is not None:
            return cached
        mapping: Dict[int, Dict[str, Set[int]]] = {}
        try:
            rows = await self._hl_store.ignores_for_guild(guild_id)
        except Exception as exc:
            _log.warning("Failed to load highlight ignores for %s: %s", guild_id, exc)
        else:
            for row in rows:
                user_id = int(row["user_id"])
                mode = str(row["mode"])
                mapping.setdefault(user_id, {}).setdefault(mode, set()).add(
                    int(row["object_id"])
                )
        self._hl_ignores[guild_id] = mapping
        return mapping

    def _hl_is_ignored(
        self, message: Message, member: Member, blocked: Dict[str, Set[int]]
    ) -> bool:
        """Return ``True`` when the channel, author or an author role is ignored."""
        if not blocked:
            return False
        if message.channel.id in blocked.get("channel", set()):
            return True
        if message.author.id in blocked.get("user", set()):
            return True
        role_ids = {role.id for role in message.author.roles}
        return bool(role_ids & blocked.get("role", set()))

    @staticmethod
    def _hl_can_view(channel: Any, member: Member) -> bool:
        """Return ``True`` when ``member`` can actually read ``channel``."""
        perms = getattr(channel, "permissions_for", None)
        if perms is None:
            return False
        try:
            return bool(perms(member).read_messages)
        except Exception:  # pragma: no cover - defensive
            return False

    def _hl_off_cooldown(
        self, guild_id: int, user_id: int, keyword: str, now: float
    ) -> bool:
        """Return ``True`` when the notification cooldown has elapsed."""
        if len(self._hl_cooldowns) > HL_COOLDOWN_CACHE_LIMIT:
            self._hl_cooldowns = {
                key: last
                for key, last in self._hl_cooldowns.items()
                if now - last < HL_NOTIFY_COOLDOWN
            }
        key = (guild_id, user_id, keyword)
        last = self._hl_cooldowns.get(key, 0.0)
        if now - last < HL_NOTIFY_COOLDOWN:
            return False
        self._hl_cooldowns[key] = now
        return True

    async def _hl_notify(
        self, member: Member, message: Message, keywords: Sequence[str]
    ) -> None:
        """DM ``member`` about ``keywords`` being mentioned, best effort."""
        keyword_text = self._hl_join_keywords(keywords)
        channel_name = getattr(message.channel, "name", "unknown")
        excerpt = (message.content or "").strip() or "*(attachment or embed)*"
        if len(excerpt) > HL_EXCERPT_LIMIT:
            excerpt = excerpt[:HL_EXCERPT_LIMIT].rstrip() + "…"

        embed = Embed(
            title="🔔 Highlight",
            description=(
                f"Your highlighted keyword{'s' if len(keywords) > 1 else ''} "
                f"{keyword_text} was mentioned in **{message.guild.name}**."
            ),
            color=COLORS.neutral,
            timestamp=message.created_at,
        )
        embed.add_field(name="Channel", value=f"#{channel_name}", inline=True)
        embed.add_field(name="Author", value=str(message.author), inline=True)
        embed.add_field(
            name="Message",
            value=discord.utils.escape_markdown(excerpt),
            inline=False,
        )
        embed.add_field(
            name="Jump", value=f"[Jump to message]({message.jump_url})", inline=False
        )
        embed.set_footer(text="Manage your keywords with `highlight list`.")

        try:
            await member.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=False
                ),
            )
        except discord.Forbidden:
            # Closed DMs are expected and must fail silently.
            pass
        except discord.HTTPException as exc:  # pragma: no cover - best effort
            _log.warning("Failed to DM highlight to %s: %s", member.id, exc)

    @staticmethod
    def _hl_join_keywords(keywords: Sequence[str]) -> str:
        """Render a keyword list, truncating very long ones for a description."""
        rendered = [f"**`{keyword}`**" for keyword in keywords]
        text = ", ".join(rendered)
        if len(text) > 800:
            text = text[:800].rsplit(",", 1)[0] + ", …"
        return text

    # ------------------------------------------------------------------ #
    # Group
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="highlight",
        aliases=["hl"],
        invoke_without_command=True,
        description="Manage keyword notifications so you are alerted when a keyword is said.",
        example="highlight add antinuke",
    )
    @guild_only()
    async def highlight(self, ctx: Context) -> Message:
        """Show the highlight command group's help."""
        return await ctx.send_help(ctx.command)

    # ------------------------------------------------------------------ #
    # add
    # ------------------------------------------------------------------ #

    @highlight.command(
        name="add",
        description="Add a keyword to your highlights in this server.",
        example="highlight add hello",
    )
    @guild_only()
    async def highlight_add(self, ctx: Context, *, keyword: str) -> Message:
        """Add ``keyword`` to your highlights in this server."""
        self._hl_ready()
        keyword, error = self._hl_validate_keyword(keyword)
        if error:
            return await ctx.warn(error)

        try:
            if await self._hl_store.keyword_exists(ctx.guild.id, ctx.author.id, keyword):
                return await ctx.warn(
                    f"**`{keyword}`** is already one of your highlighted keywords."
                )
            count = await self._hl_store.count_keywords(ctx.guild.id, ctx.author.id)
            if count >= HL_MAX_KEYWORDS:
                return await ctx.warn(
                    f"You can highlight up to **{HL_MAX_KEYWORDS}** keywords per "
                    "server — remove one first with `highlight remove`."
                )
            await self._hl_store.add_keyword(ctx.guild.id, ctx.author.id, keyword)
        except Exception as exc:
            if _hl_is_duplicate_error(exc):
                return await ctx.warn(
                    f"**`{keyword}`** is already one of your highlighted keywords."
                )
            _log.exception(
                "Failed to add highlight %r for %s in %s",
                keyword,
                ctx.author.id,
                ctx.guild.id,
            )
            return await ctx.warn(
                "I couldn't save that keyword right now — please try again."
            )

        self._hl_invalidate(ctx.guild.id)
        return await ctx.approve(
            f"You'll now be DM'd when someone says **`{keyword}`** in "
            f"**{ctx.guild.name}**."
        )

    # ------------------------------------------------------------------ #
    # remove
    # ------------------------------------------------------------------ #

    @highlight.command(
        name="remove",
        aliases=["delete", "rm"],
        description="Remove one of your highlighted keywords.",
        example="highlight remove hello",
    )
    @guild_only()
    async def highlight_remove(self, ctx: Context, *, keyword: str) -> Message:
        """Remove ``keyword`` from your highlights in this server."""
        self._hl_ready()
        keyword = _hl_normalise_keyword(keyword)
        try:
            removed = await self._hl_store.remove_keyword(
                ctx.guild.id, ctx.author.id, keyword
            )
        except Exception as exc:  # pragma: no cover - DB failure is defensive
            _log.exception(
                "Failed to remove highlight %r for %s in %s",
                keyword,
                ctx.author.id,
                ctx.guild.id,
            )
            return await ctx.warn(
                "I couldn't remove that keyword right now — please try again."
            )

        if not removed:
            return await ctx.warn(
                f"You don't have **`{keyword}`** in your highlighted keywords here."
            )
        self._hl_invalidate(ctx.guild.id)
        return await ctx.approve(
            f"Removed **`{keyword}`** from your highlighted keywords."
        )

    # ------------------------------------------------------------------ #
    # list
    # ------------------------------------------------------------------ #

    @highlight.command(
        name="list",
        description="List all keywords you have set in this server.",
        example="highlight list",
    )
    @guild_only()
    async def highlight_list(self, ctx: Context) -> Message:
        """List every keyword you highlight in this server."""
        self._hl_ready()
        try:
            keywords = await self._hl_store.keywords_for_user(
                ctx.guild.id, ctx.author.id
            )
        except Exception as exc:  # pragma: no cover - DB failure is defensive
            _log.exception(
                "Failed to list highlights for %s in %s", ctx.author.id, ctx.guild.id
            )
            return await ctx.warn(
                "I couldn't load your highlighted keywords right now — please try again."
            )

        if not keywords:
            return await ctx.warn(
                "You don't have any highlighted keywords in this server yet — "
                "add one with `highlight add <keyword>`."
            )

        chunks = [
            keywords[i : i + HL_LIST_PAGE_SIZE]
            for i in range(0, len(keywords), HL_LIST_PAGE_SIZE)
        ]
        embeds: List[Embed] = []
        for index, chunk in enumerate(chunks, 1):
            lines = [
                f"{position}. `{keyword}`"
                for position, keyword in enumerate(chunk, 1)
            ]
            embed = Embed(
                title=f"Highlighted keywords in {ctx.guild.name}",
                description="\n".join(lines),
                color=COLORS.neutral,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(
                text=(
                    f"{len(keywords)} keyword(s) • Page {index}/{len(chunks)} • "
                    "Remove one with `highlight remove <keyword>`"
                )
            )
            embeds.append(embed)
        return await ctx.paginate(embeds)

    # ------------------------------------------------------------------ #
    # reset
    # ------------------------------------------------------------------ #

    @highlight.command(
        name="reset",
        description="Clear all of your highlighted keywords in this server.",
        example="highlight reset",
    )
    @guild_only()
    async def highlight_reset(self, ctx: Context) -> Message:
        """Clear every keyword you highlight in this server."""
        self._hl_ready()
        try:
            removed = await self._hl_store.reset_keywords(
                ctx.guild.id, ctx.author.id
            )
        except Exception as exc:  # pragma: no cover - DB failure is defensive
            _log.exception(
                "Failed to reset highlights for %s in %s", ctx.author.id, ctx.guild.id
            )
            return await ctx.warn(
                "I couldn't clear your keywords right now — please try again."
            )

        if not removed:
            return await ctx.warn("You don't have any highlighted keywords here.")
        self._hl_invalidate(ctx.guild.id)
        return await ctx.approve(
            f"Cleared **{removed}** highlighted keyword(s) in **{ctx.guild.name}**."
        )

    # ------------------------------------------------------------------ #
    # ignore sub-group
    # ------------------------------------------------------------------ #

    @highlight.group(
        name="ignore",
        invoke_without_command=True,
        description="Ignore notifications from a specific channel, user or role.",
        example="highlight ignore user @adler",
    )
    @guild_only()
    async def highlight_ignore(self, ctx: Context) -> Message:
        """Show the ``highlight ignore`` sub-group's help."""
        return await ctx.send_help(ctx.command)

    @highlight_ignore.command(
        name="channel",
        description="Ignore highlights triggered in a specific channel.",
        example="highlight ignore channel #general",
    )
    @guild_only()
    async def highlight_ignore_channel(
        self, ctx: Context, channel: TextChannel
    ) -> Message:
        """Toggle ignoring highlights triggered in ``channel``."""
        return await self._hl_toggle_ignore(ctx, channel, "channel")

    @highlight_ignore.command(
        name="user",
        aliases=["member"],
        description="Ignore highlights triggered by a specific user.",
        example="highlight ignore user @adler",
    )
    @guild_only()
    async def highlight_ignore_user(self, ctx: Context, member: Member) -> Message:
        """Toggle ignoring highlights triggered by ``member``."""
        return await self._hl_toggle_ignore(ctx, member, "user")

    @highlight_ignore.command(
        name="role",
        description="Ignore highlights triggered by members with a specific role.",
        example="highlight ignore role @staff",
    )
    @guild_only()
    async def highlight_ignore_role(self, ctx: Context, role: Role) -> Message:
        """Toggle ignoring highlights triggered by members with ``role``."""
        return await self._hl_toggle_ignore(ctx, role, "role")

    @highlight_ignore.command(
        name="list",
        description="List all of your ignored members, channels and roles.",
        example="highlight ignore list",
    )
    @guild_only()
    async def highlight_ignore_list(self, ctx: Context) -> Message:
        """List everything you ignore highlights from in this server."""
        self._hl_ready()
        try:
            rows = await self._hl_store.ignores_for_user(ctx.guild.id, ctx.author.id)
        except Exception as exc:  # pragma: no cover - DB failure is defensive
            _log.exception(
                "Failed to list highlight ignores for %s in %s",
                ctx.author.id,
                ctx.guild.id,
            )
            return await ctx.warn(
                "I couldn't load your ignores right now — please try again."
            )

        groups: Dict[str, List[int]] = {mode: [] for mode in HL_IGNORE_MODES}
        for row in rows:
            mode = str(row["mode"])
            if mode in groups:
                groups[mode].append(int(row["object_id"]))

        if not any(groups.values()):
            return await ctx.warn(
                "You aren't ignoring anything here — use "
                "`highlight ignore user @user`, `highlight ignore channel #channel` "
                "or `highlight ignore role @role`."
            )

        labels = {
            "user": ("Users", self._hl_mention_member),
            "channel": ("Channels", self._hl_mention_channel),
            "role": ("Roles", self._hl_mention_role),
        }
        embed = Embed(
            title=f"Highlight ignores in {ctx.guild.name}",
            color=COLORS.neutral,
            timestamp=discord.utils.utcnow(),
        )
        for mode in HL_IGNORE_MODES:
            entries = groups[mode]
            if not entries:
                continue
            label, describe = labels[mode]
            embed.add_field(
                name=f"{label} ({len(entries)})",
                value=", ".join(describe(ctx.guild, object_id) for object_id in entries),
                inline=False,
            )
        return await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _hl_validate_keyword(raw: str) -> Tuple[str, Optional[str]]:
        """Return the normalised keyword and an error message (or ``None``)."""
        keyword = _hl_normalise_keyword(raw)
        if len(keyword) < HL_MIN_KEYWORD_LENGTH:
            return keyword, (
                f"A highlighted keyword must be at least "
                f"**{HL_MIN_KEYWORD_LENGTH}** characters long."
            )
        if len(keyword) > HL_MAX_KEYWORD_LENGTH:
            return keyword, (
                f"A highlighted keyword can't be longer than "
                f"**{HL_MAX_KEYWORD_LENGTH}** characters."
            )
        return keyword, None

    async def _hl_toggle_ignore(
        self,
        ctx: Context,
        target: Union[TextChannel, Member, Role],
        mode: str,
    ) -> Message:
        """Toggle one ignore row and report the new state to the user."""
        self._hl_ready()
        try:
            ignored = await self._hl_store.toggle_ignore(
                ctx.guild.id, ctx.author.id, target.id, mode
            )
        except Exception as exc:  # pragma: no cover - DB failure is defensive
            _log.exception(
                "Failed to toggle highlight ignore (%s %s) for %s in %s",
                mode,
                target.id,
                ctx.author.id,
                ctx.guild.id,
            )
            return await ctx.warn(
                "I couldn't update your ignores right now — please try again."
            )

        self._hl_invalidate(ctx.guild.id)
        if ignored:
            return await ctx.approve(
                f"You'll no longer be notified about highlights from "
                f"{self._hl_mention(ctx.guild, target, mode)}."
            )
        return await ctx.approve(
            f"You'll be notified about highlights from "
            f"{self._hl_mention(ctx.guild, target, mode)} again."
        )

    @staticmethod
    def _hl_mention(guild: discord.Guild, target: Any, mode: str) -> str:
        """Render ``target`` as a mention, falling back to its raw ID."""
        mention = getattr(target, "mention", None)
        if mention:
            return str(mention)
        identifier = getattr(target, "id", None)
        return f"`{identifier}`" if identifier is not None else str(target)

    @staticmethod
    def _hl_mention_channel(guild: discord.Guild, channel_id: int) -> str:
        channel = guild.get_channel(channel_id)
        return channel.mention if channel is not None else f"`{channel_id}`"

    @staticmethod
    def _hl_mention_member(guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.mention if member is not None else f"`{user_id}`"

    @staticmethod
    def _hl_mention_role(guild: discord.Guild, role_id: int) -> str:
        role = guild.get_role(role_id)
        return role.mention if role is not None else f"`{role_id}`"
