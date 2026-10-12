# File: cogs/engagement/level.py
"""Leveling system for the engagement cog.

This module provides :class:`LevelMixin`, a plain mixin that is blended into the
existing :class:`~cogs.engagement.engagement.Engagement` cog. It owns the
``level`` command group, the XP-on-message listener and every level helper, and
deliberately does **not** create a new cog of its own.

The shared XP -> level / rank maths live in module-level functions so they can
be reused from anywhere (the giveaway code already relies on ``level_from_xp``).

Every helper and attribute belonging to this mixin is prefixed with ``_level_``
to avoid colliding with the (large) giveaway implementation in ``engagement.py``.
"""

from __future__ import annotations

import io
import logging
import math
import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import discord
from discord import Embed, Member, Message, Role, TextChannel
from discord.ext import commands
from discord.ext.commands import BucketType, cooldown, guild_only

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.commands import hybrid_group
from base.managers.predicates import has_permissions

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# XP maths (module level, shared)
# --------------------------------------------------------------------------- #

#: XP required to reach level 0. Every level's requirement grows quadratically:
#: ``xp_for_level(n) = 100 + 45n + 5n(n + 1)`` (i.e. 100, 155, 220, ...).
XP_BASE = 100


def xp_for_level(level: int) -> int:
    """Return the cumulative XP required to *be* at ``level``.

    Level 0 starts at :data:`XP_BASE` XP so a member's very first messages put
    them on the ladder. Negative levels clamp to ``0``.
    """
    level = max(0, int(level))
    return XP_BASE + 45 * level + 5 * level * (level + 1)


def level_from_xp(xp: int) -> int:
    """Return the level a member with ``xp`` total XP has reached.

    ``xp`` is clamped to ``0`` so a member can never fall below level ``0``.
    A member below :data:`XP_BASE` is always level ``0``.
    """
    xp = max(0, int(xp))
    if xp < XP_BASE:
        return 0
    # Largest integer n satisfying 5n^2 + 50n + (100 - xp) <= 0.
    return max(0, int((math.sqrt(25 + (xp - XP_BASE) / 5.0) - 5) // 1))


def progress_to_next(xp: int) -> Tuple[int, int, int, int]:
    """Return ``(level, current, needed, remaining)`` for ``xp``.

    ``current`` is the XP earned inside the current level, ``needed`` is the XP
    span of that level and ``remaining`` is how much is still required for the
    next one. Mirrors :func:`level_from_xp` exactly, including the clamp at 0.
    """
    xp = max(0, int(xp))
    level = level_from_xp(xp)
    floor = xp_for_level(level)
    ceiling = xp_for_level(level + 1)
    needed = ceiling - floor
    current = max(0, xp - floor)
    return level, current, needed, needed - current


def rank_from_rows(rows: Sequence[Dict[str, Any]], user_id: int) -> int:
    """Return the 1-based position of ``user_id`` within a ranked ``rows`` list.

    ``rows`` is expected to be ordered by ``xp`` descending (the order the
    leaderboard query returns). Returns ``0`` when the member is not present.
    """
    for index, row in enumerate(rows, start=1):
        if int(row["user_id"]) == int(user_id):
            return index
    return 0


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


class LevelStore:
    """Thin data-access layer for the leveling tables.

    Stateless: every call goes through ``bot.pool`` using parameterised queries
    scoped by ``guild_id``. Kept under this name because the existing giveaway
    code in ``engagement.py`` already imports and uses ``LevelStore``.
    """

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @property
    def pool(self) -> Any:
        return self.bot.pool

    async def get_xp(self, guild_id: int, user_id: int) -> int:
        """Return the total XP ``user_id`` has in ``guild_id`` (0 when unset)."""
        value = await self.pool.fetchval(
            "SELECT xp FROM levels WHERE guild_id = %s AND user_id = %s",
            guild_id,
            user_id,
        )
        return int(value or 0)

    async def set_xp(self, guild_id: int, user_id: int, xp: int) -> None:
        """Persist ``xp`` for a member, clamped at 0, creating the row if needed."""
        await self.pool.execute(
            "INSERT INTO levels (guild_id, user_id, xp, messages, updated_at) "
            "VALUES (%s, %s, %s, 0, %s) "
            "ON DUPLICATE KEY UPDATE xp = VALUES(xp), updated_at = VALUES(updated_at)",
            guild_id,
            user_id,
            max(0, int(xp)),
            int(time.time()),
        )

    async def add_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        """Atomically add ``amount`` XP, returning the new total (never below 0)."""
        await self.pool.execute(
            "INSERT INTO levels (guild_id, user_id, xp, messages, updated_at) "
            "VALUES (%s, %s, %s, 1, %s) "
            "ON DUPLICATE KEY UPDATE "
            "xp = GREATEST(0, xp + VALUES(xp)), "
            "messages = messages + 1, "
            "updated_at = VALUES(updated_at)",
            guild_id,
            user_id,
            max(0, int(amount)),
            int(time.time()),
        )
        return await self.get_xp(guild_id, user_id)


# --------------------------------------------------------------------------- #
# Level card renderer (Pillow, optional)
# --------------------------------------------------------------------------- #


class _LevelCardRenderer:
    """Renders a member's level card to a PNG using Pillow.

    Pillow is optional: when it is unavailable :attr:`available` is ``False``
    and :meth:`render` returns ``None`` so callers can fall back to an embed.
    """

    WIDTH = 900
    HEIGHT = 260

    def __init__(self) -> None:
        try:
            import PIL.Image as _Image
            import PIL.ImageDraw as _ImageDraw
            import PIL.ImageFont as _ImageFont
        except Exception:  # pragma: no cover - Pillow is optional
            self._Image = self._ImageDraw = self._ImageFont = None
            self.available = False
        else:
            self._Image = _Image
            self._ImageDraw = _ImageDraw
            self._ImageFont = _ImageFont
            self.available = True

    def _font(self, size: int) -> Any:
        for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"):
            try:
                return self._ImageFont.truetype(name, size)
            except Exception:
                continue
        return self._ImageFont.load_default()

    def render(
        self,
        *,
        display_name: str,
        avatar_bytes: Optional[bytes],
        level: int,
        rank: int,
        current: int,
        needed: int,
        total_xp: int,
        accent: Tuple[int, int, int] = (0x74, 0x7C, 0x8C),
    ) -> Optional[bytes]:
        """Render the card and return PNG bytes (or ``None`` when unavailable)."""
        if not self.available:
            return None

        canvas = self._Image.new("RGBA", (self.WIDTH, self.HEIGHT), (32, 34, 37, 255))
        draw = self._ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            (24, 24, 224, 224), radius=18, fill=(accent[0], accent[1], accent[2], 255)
        )

        if avatar_bytes:
            try:
                avatar = self._Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
                avatar = avatar.resize((176, 176))
                mask = self._Image.new("L", (176, 176), 0)
                self._ImageDraw.Draw(mask).ellipse((0, 0, 176, 176), fill=255)
                canvas.paste(avatar, (36, 36), mask)
            except Exception:  # pragma: no cover - avatar rendering is best effort
                pass

        draw.text((264, 48), display_name[:24], font=self._font(40), fill=(255, 255, 255, 255))
        draw.text(
            (264, 104),
            f"Level {level}  •  Rank #{rank}" if rank else f"Level {level}  •  Unranked",
            font=self._font(24),
            fill=(200, 202, 208, 255),
        )
        draw.text(
            (264, 148),
            f"{current:,} / {needed:,} XP",
            font=self._font(28),
            fill=(255, 255, 255, 255),
        )

        bar_x, bar_y, bar_w, bar_h = 264, 198, 588, 26
        draw.rounded_rectangle(
            (bar_x, bar_y, bar_x + bar_w, bar_y + bar_h),
            radius=bar_h // 2,
            fill=(54, 57, 63, 255),
        )
        ratio = 0.0 if needed <= 0 else max(0.0, min(1.0, current / needed))
        filled = int(bar_w * ratio)
        if filled > 0:
            draw.rounded_rectangle(
                (bar_x, bar_y, bar_x + filled, bar_y + bar_h),
                radius=bar_h // 2,
                fill=(accent[0], accent[1], accent[2], 255),
            )

        buffer = io.BytesIO()
        canvas.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Mixin
# --------------------------------------------------------------------------- #


class LevelMixin:
    """Leveling commands and the XP listener blended into the Engagement cog.

    Never defines ``__init__`` or ``setup``: per-instance state is created by
    :meth:`_init_level`, which the host cog calls from its own ``__init__`` (or
    lazily through :meth:`_level_ready`).
    """

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _init_level(self) -> None:
        """Initialise per-user XP cooldown state (idempotent)."""
        if getattr(self, "_level_ready_called", False):
            return
        self._level_store = LevelStore(self.bot)
        self._level_renderer = _LevelCardRenderer()
        #: user_id -> epoch seconds of their last XP grant (per-user cooldown).
        self._level_xp_cooldowns: Dict[int, float] = {}
        self._level_ready_called = True

    def _level_ready(self) -> None:
        """Ensure the mixin state exists before use (lazy fallback)."""
        if not getattr(self, "_level_ready_called", False):
            self._init_level()

    @commands.Cog.listener("on_message")
    async def _level_on_message(self, message: Message) -> None:
        """Grant XP for messages, honouring exemptions and the per-user cooldown."""
        try:
            await self._level_handle_message(message)
        except Exception as exc:  # pragma: no cover - a listener must never raise
            _log.warning("Level XP listener failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Group
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="level",
        aliases=["lvl", "rank"],
        invoke_without_command=True,
        description="Show a member's level, XP and server rank.",
        example="level @user",
    )
    @guild_only()
    async def level(self, ctx: Context, member: Optional[Member] = None) -> Message:
        """Show the level, current XP and server rank of ``member`` (or you)."""
        target = member or ctx.author
        if not isinstance(target, Member):
            return await ctx.warn("Please mention a member in this server.")
        return await self._level_show(ctx, target)

    @level.command(
        name="up",
        aliases=["levelup"],
        description="Preview the level-up card shown when a member levels up.",
        example="level up @user",
    )
    @guild_only()
    async def level_up(self, ctx: Context, member: Optional[Member] = None) -> Message:
        """Preview the level-up card for ``member`` (or you)."""
        target = member or ctx.author
        if not isinstance(target, Member):
            return await ctx.warn("Please mention a member in this server.")
        data = await self._level_fetch(ctx.guild.id, target.id)
        return await self._level_send_card(
            ctx,
            target,
            data,
            title=f"{EMOJIS.APPROVE} {target.display_name} levelled up!",
        )

    @level.command(
        name="add",
        description="Give a member XP, recalculating their level.",
        example="level add @user 1000",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 3, BucketType.guild)
    async def level_add(self, ctx: Context, member: Member, amount: int) -> Message:
        """Give ``member`` ``amount`` XP."""
        error = self._level_validate_amount(amount)
        if error:
            return await ctx.warn(error)
        if member.bot:
            return await ctx.warn("Bots don't earn XP.")

        before_xp = await self._level_store.get_xp(ctx.guild.id, member.id)
        before_level = level_from_xp(before_xp)
        after_xp = await self._level_store.add_xp(ctx.guild.id, member.id, amount)
        after_level = level_from_xp(after_xp)

        if after_level > before_level:
            await self._level_announce(ctx.channel, member, after_level)
        return await ctx.approve(
            f"Gave **{amount:,}** XP to {member.mention} — now level "
            f"**{after_level}** with **{after_xp:,}** XP."
        )

    @level.command(
        name="remove",
        description="Take XP away from a member, recalculating their level.",
        example="level remove @user 500",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 3, BucketType.guild)
    async def level_remove(self, ctx: Context, member: Member, amount: int) -> Message:
        """Take ``amount`` XP away from ``member`` (never below 0)."""
        error = self._level_validate_amount(amount)
        if error:
            return await ctx.warn(error)

        before_xp = await self._level_store.get_xp(ctx.guild.id, member.id)
        before_level = level_from_xp(before_xp)
        after_xp = max(0, before_xp - amount)
        await self._level_store.set_xp(ctx.guild.id, member.id, after_xp)
        after_level = level_from_xp(after_xp)

        if after_level > before_level:  # pragma: no cover - removal never raises a level
            await self._level_announce(ctx.channel, member, after_level)
        return await ctx.approve(
            f"Removed **{amount:,}** XP from {member.mention} — now level "
            f"**{after_level}** with **{after_xp:,}** XP."
        )

    @level.command(
        name="settings",
        description="Show the server's leveling settings.",
        example="level settings",
    )
    @guild_only()
    async def level_settings(self, ctx: Context) -> Message:
        """Show the server's leveling configuration."""
        settings = await self._level_get_settings(ctx.guild.id)
        embed = Embed(
            title="Leveling settings",
            color=COLORS.neutral,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Status",
            value="Enabled" if settings["enabled"] else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="XP per message",
            value=f"{settings['min_xp']}-{settings['max_xp']}",
            inline=True,
        )
        embed.add_field(
            name="XP cooldown", value=f"{settings['xp_cooldown']}s", inline=True
        )
        embed.add_field(
            name="Level-up announcements",
            value="On" if settings["announce_levelup"] else "Off",
            inline=True,
        )
        channel = None
        if settings["levelup_channel_id"]:
            channel = ctx.guild.get_channel(settings["levelup_channel_id"])
        embed.add_field(
            name="Level-up channel",
            value=channel.mention if channel else "Current channel",
            inline=True,
        )
        embed.set_footer(text="Use `level exempt` to exclude channels or roles.")
        return await ctx.send(embed=embed)

    @level.command(
        name="exempt",
        description="Exclude channels and/or roles from gaining XP.",
        example="level exempt #spam @NoXP",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 3, BucketType.guild)
    async def level_exempt(
        self,
        ctx: Context,
        channels: commands.Greedy[TextChannel] = None,  # type: ignore[assignment]
        roles: commands.Greedy[Role] = None,  # type: ignore[assignment]
    ) -> Message:
        """Exclude the given channels and/or roles from XP gain (toggles)."""
        channels = list(channels or [])
        roles = list(roles or [])
        if not channels and not roles:
            return await self._level_show_exempt(ctx)

        for channel in channels:
            await self._level_toggle_exempt(ctx.guild.id, channel.id, "channel")
        for role in roles:
            await self._level_toggle_exempt(ctx.guild.id, role.id, "role")

        parts: List[str] = []
        if channels:
            parts.append(", ".join(c.mention for c in channels))
        if roles:
            parts.append(", ".join(r.mention for r in roles))
        return await ctx.approve(f"Toggled XP exemption for {', '.join(parts)}.")

    @level.command(
        name="rank",
        aliases=["r"],
        description="Show a member's rank in the server's XP ordering.",
        example="level rank @user",
    )
    @guild_only()
    async def level_rank(self, ctx: Context, member: Optional[Member] = None) -> Message:
        """Show the position of ``member`` (or you) on the server leaderboard."""
        target = member or ctx.author
        if not isinstance(target, Member):
            return await ctx.warn("Please mention a member in this server.")
        data = await self._level_fetch(ctx.guild.id, target.id)
        if data["rank"] == 0:
            return await ctx.warn(f"{target.mention} isn't ranked yet.")
        return await ctx.approve(
            f"{target.mention} is **#{data['rank']}** on the server leaderboard "
            f"(level **{data['level']}**, **{data['xp']:,}** XP)."
        )

    @level.command(
        name="leaderboard",
        aliases=["lb", "top"],
        description="Show the server's paginated XP leaderboard.",
        example="level leaderboard 2",
    )
    @guild_only()
    async def level_leaderboard(self, ctx: Context, page: int = 1) -> Message:
        """Show the XP leaderboard starting at ``page`` (default 1)."""
        if page < 1:
            return await ctx.warn("The page must be **1** or greater.")

        rows = await self._level_store.pool.fetch(
            "SELECT user_id, xp FROM levels "
            "WHERE guild_id = %s AND xp > 0 "
            "ORDER BY xp DESC, user_id ASC",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.warn("Nobody has earned XP in this server yet.")

        per_page = 10
        total_pages = max(1, math.ceil(len(rows) / per_page))
        if page > total_pages:
            return await ctx.warn(
                f"That page doesn't exist — there are only **{total_pages}** page(s)."
            )

        pages: List[Embed] = []
        for page_index in range(total_pages):
            chunk = rows[page_index * per_page : (page_index + 1) * per_page]
            lines: List[str] = []
            for offset, row in enumerate(chunk):
                position = page_index * per_page + offset + 1
                user = ctx.guild.get_member(int(row["user_id"]))
                name = user.display_name if user else f"`{row['user_id']}`"
                lines.append(
                    f"**#{position}** {name} — level "
                    f"**{level_from_xp(int(row['xp']))}** ({int(row['xp']):,} XP)"
                )
            embed = Embed(
                title=f"{ctx.guild.name} leaderboard",
                description="\n".join(lines),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {page_index + 1}/{total_pages}")
            pages.append(embed)

        if page == 1:
            return await ctx.paginate(pages)
        # Re-order so the requested page shows first, keeping every page reachable.
        return await ctx.paginate(pages[page - 1 :] + pages[: page - 1])

    @level.command(
        name="card",
        description="Render and show the level card image for a member.",
        example="level card @user",
    )
    @guild_only()
    async def level_card(self, ctx: Context, member: Optional[Member] = None) -> Message:
        """Render the level card for ``member`` (or you)."""
        target = member or ctx.author
        if not isinstance(target, Member):
            return await ctx.warn("Please mention a member in this server.")
        data = await self._level_fetch(ctx.guild.id, target.id)
        return await self._level_send_card(
            ctx, target, data, title=f"{target.display_name}'s level card"
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _level_validate_amount(amount: Any) -> Optional[str]:
        """Return a user-facing error string when ``amount`` isn't positive."""
        try:
            value = int(amount)
        except (TypeError, ValueError):
            return "The amount must be a positive whole number."
        if value < 1:
            return "The amount must be a positive whole number."
        return None

    async def _level_get_settings(self, guild_id: int) -> Dict[str, Any]:
        """Return the guild's leveling settings, applying sensible defaults."""
        defaults: Dict[str, Any] = {
            "enabled": True,
            "min_xp": 15,
            "max_xp": 25,
            "xp_cooldown": 60,
            "announce_levelup": True,
            "levelup_channel_id": None,
            "levelup_message": None,
        }
        try:
            row = await self._level_store.pool.fetchrow(
                "SELECT * FROM level_config WHERE guild_id = %s", guild_id
            )
        except Exception as exc:
            _log.warning("Failed to load level settings for %s: %s", guild_id, exc)
            return defaults
        if not row:
            return defaults
        defaults.update(
            {
                "enabled": bool(row.get("enabled", 1)),
                "min_xp": int(row.get("min_xp") or 15),
                "max_xp": int(row.get("max_xp") or 25),
                "xp_cooldown": int(row.get("xp_cooldown") or 60),
                "announce_levelup": bool(row.get("announce_levelup", 1)),
                "levelup_channel_id": row.get("levelup_channel_id"),
                "levelup_message": row.get("levelup_message"),
            }
        )
        return defaults

    async def _level_is_exempt(
        self, guild_id: int, channel_id: int, role_ids: Sequence[int]
    ) -> bool:
        """Return ``True`` when the channel or any of ``role_ids`` is exempt."""
        try:
            rows = await self._level_store.pool.fetch(
                "SELECT object_id, mode FROM level_exempt WHERE guild_id = %s",
                guild_id,
            )
        except Exception as exc:
            _log.warning("Failed to load level exemptions for %s: %s", guild_id, exc)
            return False
        channel_exempt = {int(r["object_id"]) for r in rows if r["mode"] == "channel"}
        role_exempt = {int(r["object_id"]) for r in rows if r["mode"] == "role"}
        if channel_id in channel_exempt:
            return True
        return any(role_id in role_exempt for role_id in role_ids)

    async def _level_toggle_exempt(
        self, guild_id: int, object_id: int, mode: str
    ) -> bool:
        """Toggle an exemption row; returns ``True`` when it is now exempt."""
        existing = await self._level_store.pool.fetchval(
            "SELECT object_id FROM level_exempt "
            "WHERE guild_id = %s AND object_id = %s AND mode = %s",
            guild_id,
            object_id,
            mode,
        )
        if existing is not None:
            await self._level_store.pool.execute(
                "DELETE FROM level_exempt "
                "WHERE guild_id = %s AND object_id = %s AND mode = %s",
                guild_id,
                object_id,
                mode,
            )
            return False
        await self._level_store.pool.execute(
            "INSERT INTO level_exempt (guild_id, object_id, mode) VALUES (%s, %s, %s)",
            guild_id,
            object_id,
            mode,
        )
        return True

    async def _level_show_exempt(self, ctx: Context) -> Message:
        """Show the currently exempt channels and roles."""
        rows = await self._level_store.pool.fetch(
            "SELECT object_id, mode FROM level_exempt WHERE guild_id = %s",
            ctx.guild.id,
        )
        channels = [
            ctx.guild.get_channel(int(r["object_id"]))
            for r in rows
            if r["mode"] == "channel"
        ]
        roles = [
            ctx.guild.get_role(int(r["object_id"]))
            for r in rows
            if r["mode"] == "role"
        ]
        channels = [c for c in channels if c is not None]
        roles = [r for r in roles if r is not None]
        if not channels and not roles:
            return await ctx.warn(
                "No channels or roles are exempt — use `level exempt #channel @role`."
            )
        embed = Embed(title="XP exemptions", color=COLORS.neutral)
        if channels:
            embed.add_field(
                name="Channels", value=", ".join(c.mention for c in channels), inline=False
            )
        if roles:
            embed.add_field(
                name="Roles", value=", ".join(r.mention for r in roles), inline=False
            )
        return await ctx.send(embed=embed)

    async def _level_fetch(self, guild_id: int, user_id: int) -> Dict[str, Any]:
        """Collect the level, XP, rank and progress needed to display a member."""
        xp = await self._level_store.get_xp(guild_id, user_id)
        level, current, needed, _remaining = progress_to_next(xp)
        rank = await self._level_rank_position(guild_id, user_id, xp)
        progress = 0.0 if needed <= 0 else max(0.0, min(1.0, current / needed))
        return {
            "xp": xp,
            "level": level,
            "current": current,
            "needed": needed,
            "rank": rank,
            "progress": progress,
        }

    async def _level_rank_position(self, guild_id: int, user_id: int, xp: int) -> int:
        """Return the 1-based rank of ``user_id`` in the guild, or 0 when unranked."""
        if xp <= 0:
            return 0
        higher = await self._level_store.pool.fetchval(
            "SELECT COUNT(*) FROM levels WHERE guild_id = %s AND xp > %s",
            guild_id,
            xp,
        )
        # Ties are broken by user_id ASC, matching the leaderboard ordering.
        tied = await self._level_store.pool.fetchval(
            "SELECT COUNT(*) FROM levels "
            "WHERE guild_id = %s AND xp = %s AND user_id < %s",
            guild_id,
            xp,
            user_id,
        )
        return int(higher or 0) + int(tied or 0) + 1

    async def _level_show(self, ctx: Context, target: Member) -> Message:
        """Send a text summary of ``target``'s level, XP and rank."""
        data = await self._level_fetch(ctx.guild.id, target.id)
        rank = f"#{data['rank']}" if data["rank"] else "Unranked"
        embed = Embed(
            title=f"{target.display_name}'s level",
            color=COLORS.neutral,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=f"**{data['level']}**", inline=True)
        embed.add_field(name="XP", value=f"**{data['xp']:,}**", inline=True)
        embed.add_field(name="Rank", value=f"**{rank}**", inline=True)
        embed.add_field(
            name="Progress",
            value=f"{data['current']:,} / {data['needed']:,} XP",
            inline=False,
        )
        embed.set_footer(text="Use `level leaderboard` to see the top members.")
        return await ctx.send(embed=embed)

    async def _level_send_card(
        self, ctx: Any, target: Member, data: Dict[str, Any], *, title: str
    ) -> Message:
        """Render ``target``'s level card and send it, falling back to an embed."""
        avatar_bytes: Optional[bytes] = None
        try:
            avatar_bytes = await target.display_avatar.replace(
                size=256, format="png"
            ).read()
        except Exception:  # pragma: no cover - avatar fetch is best effort
            avatar_bytes = None

        accent = (0x74, 0x7C, 0x8C)
        png = None
        if getattr(self, "_level_renderer", None) is not None and self._level_renderer.available:
            png = self._level_renderer.render(
                display_name=target.display_name,
                avatar_bytes=avatar_bytes,
                level=data["level"],
                rank=data["rank"],
                current=data["current"],
                needed=data["needed"],
                total_xp=data["xp"],
                accent=accent,
            )

        if png is not None:
            file = discord.File(io.BytesIO(png), filename="level.png")
            embed = Embed(title=title, color=accent)
            embed.set_image(url="attachment://level.png")
            return await ctx.send(embed=embed, file=file)

        # Fallback when Pillow is unavailable or rendering failed.
        embed = Embed(title=title, color=accent)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=f"**{data['level']}**", inline=True)
        embed.add_field(name="XP", value=f"**{data['xp']:,}**", inline=True)
        embed.add_field(
            name="Progress",
            value=f"{data['current']:,} / {data['needed']:,} XP",
            inline=False,
        )
        return await ctx.send(embed=embed)

    async def _level_announce(self, channel: Any, member: Member, level: int) -> None:
        """Send the level-up card when announcements are enabled."""
        settings = await self._level_get_settings(member.guild.id)
        if not settings["announce_levelup"]:
            return

        target_channel = channel
        if settings["levelup_channel_id"]:
            resolved = member.guild.get_channel(settings["levelup_channel_id"])
            if resolved is not None:
                target_channel = resolved
        if target_channel is None:
            return

        data = await self._level_fetch(member.guild.id, member.id)
        try:
            await target_channel.send(f"{member.mention} reached level **{level}**!")
        except Exception as exc:  # pragma: no cover - announcements are best effort
            _log.warning("Failed to announce level-up for %s: %s", member.id, exc)

    async def _level_handle_message(self, message: Message) -> None:
        """Core XP-gain logic for a single message."""
        self._level_ready()
        if message.guild is None or not isinstance(message.author, Member):
            return
        if message.author.bot:
            return
        if not message.content and not message.attachments:
            return

        settings = await self._level_get_settings(message.guild.id)
        if not settings["enabled"]:
            return

        role_ids = [role.id for role in message.author.roles]
        if await self._level_is_exempt(message.guild.id, message.channel.id, role_ids):
            return

        now = time.time()
        cooldown = max(0, int(settings["xp_cooldown"]))
        last = self._level_xp_cooldowns.get(message.author.id, 0.0)
        if cooldown and now - last < cooldown:
            return
        self._level_xp_cooldowns[message.author.id] = now

        minimum = max(1, int(settings["min_xp"]))
        maximum = max(minimum, int(settings["max_xp"]))
        granted = random.randint(minimum, maximum)

        before_xp = await self._level_store.get_xp(message.guild.id, message.author.id)
        before_level = level_from_xp(before_xp)
        after_xp = await self._level_store.add_xp(
            message.guild.id, message.author.id, granted
        )
        after_level = level_from_xp(after_xp)

        if after_level > before_level:
            await self._level_announce(message.channel, message.author, after_level)