
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import discord
from discord import (
    AllowedMentions,
    ButtonStyle,
    Colour,
    Embed,
    Interaction,
    Member,
    Message,
    Role,
    TextChannel,
    TextStyle,
    User,
)
from discord.ext.commands import (
    BucketType,
    MemberConverter,
    RoleConverter,
    cooldown,
    guild_only,
)
from discord.ui import Button, Modal, TextInput, View, button
from discord.ext import tasks as dpy_tasks

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.EmbedBuilder import EmbedBuilder
from base.managers.commands import hybrid_command, hybrid_group
from base.managers.predicates import has_permissions
from base.managers.types import CogMeta

from cogs.engagement.highlight import HighlightMixin
from cogs.engagement.level import LevelMixin, LevelStore, level_from_xp

_log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

GIVEAWAY_LINK = re.compile(
    r"channels/(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)

MAX_DURATION = 365 * 24 * 3600  # one year
MAX_WINNERS = 100
MAX_ENTRIES_CAP = 100_000
BUTTON_TIMEOUT = 300.0
LIST_PAGE_SIZE = 8

STATE_ICON = "🎉"
LOCK_ICON = "🔒"
SHIELD_ICON = "🛡️"

#: Unit -> seconds used by the duration parser.
_TIME_UNITS: Dict[str, int] = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "year": 31536000, "years": 31536000,
}

# --------------------------------------------------------------------------- #
# Formatting / parsing helpers
# --------------------------------------------------------------------------- #


def humanize_delta(seconds: Optional[int]) -> str:
    """Return a compact relative string such as ``2h 30m`` for a second count."""
    if seconds is None:
        return "N/A"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "N/A"
    total = max(0, total)
    if total < 60:
        return f"{total}s"
    chunks: List[str] = []
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60)):
        value, total = divmod(total, size)
        if value:
            chunks.append(f"{value}{suffix}")
    return " ".join(chunks[:2]) or "0s"


def parse_duration(raw: str) -> int:
    """Parse a human duration into seconds.

    Accepts ``10m``, ``1h30m``, ``2 days``, ``1 week``, ``30`` (bare seconds),
    ``1:30`` (mm:ss) and ``1:02:03`` (hh:mm:ss).

    Raises :class:`ValueError` with a user-facing message on bad input.
    """
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("Please provide a duration, e.g. `10m`, `1h30m` or `2d`.")

    # Colon clock notation
    if ":" in text:
        parts = text.split(":")
        if not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
            raise ValueError("That duration isn't valid. Try `10m` or `1:30`.")
        numbers = [int(p) for p in parts]
        if len(numbers) == 2:
            minutes, seconds = numbers
            total = minutes * 60 + seconds
        else:
            hours, minutes, seconds = numbers
            total = hours * 3600 + minutes * 60 + seconds
        return _clamp_duration(total)

    # Bare number -> seconds
    if text.isdigit():
        return _clamp_duration(int(text))

    # ``1h30m`` style
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text):
        key = unit.rstrip(".")
        if key not in _TIME_UNITS:
            raise ValueError(f"`{unit}` isn't a unit I know. Try `s`, `m`, `h`, `d`, `w`.")
        total += float(value) * _TIME_UNITS[key]
        matched = True

    if not matched:
        raise ValueError("That duration isn't valid. Try `10m`, `1h30m` or `2d`.")
    return _clamp_duration(int(total))


def _clamp_duration(seconds: int) -> int:
    if seconds < 1:
        raise ValueError("The duration must be at least **1 second**.")
    if seconds > MAX_DURATION:
        raise ValueError("The duration can't be longer than **1 year**.")
    return seconds


def parse_entries_cap(raw: str) -> int:
    """Parse the maximum-entries value. ``0`` (or blank) means unlimited."""
    text = (raw or "").strip().lower()
    if text in ("", "0", "none", "unlimited", "infinite", "inf", "∞"):
        return 0
    try:
        value = int(text.replace(",", "").replace("_", ""))
    except ValueError:
        raise ValueError("Entries must be a whole number, or `0` for unlimited.")
    if value < 0:
        raise ValueError("Entries can't be negative — use `0` for unlimited.")
    if value > MAX_ENTRIES_CAP:
        raise ValueError(f"That's too many entries. The cap is **{MAX_ENTRIES_CAP:,}**.")
    return value


def parse_colour(raw: str) -> int:
    """Parse a colour from ``#rrggbb``, ``rrggbb`` or a colour name."""
    text = (raw or "").strip().lstrip("#")
    if not text:
        raise ValueError("Please provide a colour, e.g. `#ff79c6` or `blurple`.")

    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return int(text, 16)
    if re.fullmatch(r"[0-9a-fA-F]{3}", text):
        return int("".join(c * 2 for c in text), 16)

    try:
        colour = discord.Colour.from_str(text) if text.startswith("#") else None
    except ValueError:
        colour = None
    named = getattr(Colour, text.lower(), None)
    if callable(named):
        try:
            return named().value
        except Exception:  # pragma: no cover - defensive
            pass
    if colour is not None:
        return colour.value
    raise ValueError("That colour isn't valid. Use a hex code like `#ff79c6`.")


def sort_winners(entries: Sequence[int], count: int) -> List[int]:
    """Pick up to ``count`` distinct winners from ``entries`` at random."""
    pool = list(dict.fromkeys(entries))
    if not pool or count <= 0:
        return []
    if count >= len(pool):
        random.shuffle(pool)
        return pool
    return random.sample(pool, count)


def mentions_from(ids: Optional[str]) -> str:
    """Turn a stored comma-separated id string into a readable mention list."""
    if not ids:
        return "None"
    parts = [p for p in str(ids).split(",") if p.strip().isdigit()]
    if not parts:
        return "None"
    return ", ".join(f"<@{p.strip()}>" for p in parts) or "None"


def parse_message_ref(value: str) -> Optional[int]:
    """Extract a message id from a raw id, a jump link or a ``guild/channel/message`` triple."""
    if value is None:
        return None
    text = str(value).strip().strip("<>")
    link = GIVEAWAY_LINK.search(text)
    if link:
        return int(link.group("message"))
    if text.isdigit():
        return int(text)
    return None


def format_dt_rel(timestamp: Optional[int]) -> str:
    """Render a unix timestamp as a relative Discord timestamp."""
    if not timestamp:
        return "N/A"
    return f"<t:{int(timestamp)}:R>"


def _join_age_days(member: Member) -> int:
    if member.joined_at is None:
        return 0
    delta = datetime.now(timezone.utc) - member.joined_at
    return max(0, delta.days)


def _link(guild_id: int, channel_id: Optional[int], message_id: Optional[int]) -> str:
    """Build a jump link to a giveaway message (or an em dash when unusable)."""
    if channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return "—"


class _FakeCtx:
    """Minimal context object accepted by discord.py's built-in converters.

    ``RoleConverter`` / ``MemberConverter`` / ``UserConverter`` only ever touch
    ``.guild`` and ``.bot``, so a tiny stand-in is enough to reuse them for
    modal input resolution.
    """

    def __init__(self, guild: discord.Guild) -> None:
        self.guild = guild
        self.bot = getattr(guild, "_state", None)

    @property
    def message(self):
        return None


# --------------------------------------------------------------------------- #
# Entry requirement evaluation
# --------------------------------------------------------------------------- #


async def requirement_failure(
    member: Member,
    giveaway: dict,
    blacklisted_roles: Iterable[int],
) -> Optional[str]:
    """Return a human reason why ``member`` can't enter, or ``None`` when allowed."""
    role_id = giveaway.get("required_role_id")
    if role_id and not any(role.id == role_id for role in member.roles):
        role = member.guild.get_role(role_id)
        name = role.mention if role else f"`{role_id}`"
        return f"You need the {name} role to enter this giveaway."

    required_level = giveaway.get("required_level") or 0
    required_stay = giveaway.get("required_age") or 0
    if required_stay:
        joined = _join_age_days(member)
        if joined < required_stay:
            remaining = required_stay - joined
            return (
                f"You need to be in this server for **{required_stay} more day(s)** "
                f"(you still need {remaining})."
            )

    blocked = set(blacklisted_roles or ())
    if blocked:
        for role in member.roles:
            if role.id in blocked:
                return "You have a role that is **blacklisted** from entering giveaways here."
    return None


# --------------------------------------------------------------------------- #
# Database layer
# --------------------------------------------------------------------------- #


class GiveawayStore:
    """Thin async wrapper around the giveaway tables.

    Every call goes through ``bot.pool`` so the pooling / cursor lifecycle is
    handled centrally in :mod:`base.database`.
    """

    def __init__(self, bot) -> None:
        self.bot = bot

    @property
    def pool(self):
        return self.bot.pool

    # -- giveaways -------------------------------------------------------- #

    async def create_draft(
        self,
        *,
        giveaway_id: int,
        guild_id: int,
        host_id: int,
        panel_message_id: int,
    ) -> None:
        await self.pool.execute(
            "INSERT INTO giveaways "
            "(id, guild_id, host_id, status, winners, panel_message_id, created_at) "
            "VALUES (%s, %s, %s, 'draft', 1, %s, %s)",
            giveaway_id,
            guild_id,
            host_id,
            panel_message_id,
            int(time.time()),
        )

    async def get(self, giveaway_id: int) -> Optional[dict]:
        return await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE id = %s", giveaway_id
        )

    async def get_by_message(
        self, guild_id: int, message_id: int
    ) -> Optional[dict]:
        row = await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE guild_id = %s AND message_id = %s",
            guild_id,
            message_id,
        )
        if row:
            return row
        return await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE guild_id = %s AND panel_message_id = %s",
            guild_id,
            message_id,
        )

    async def get_panel(self, message_id: int) -> Optional[dict]:
        return await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE panel_message_id = %s", message_id
        )

    async def update(self, giveaway_id: int, **fields) -> None:
        allowed = {
            "channel_id", "prize", "duration", "ends_at", "required_role_id",
            "required_age", "required_level", "max_entries", "winners",
            "embed_script", "status", "message_id", "winner_ids", "reroll_count",
            "started_at", "ended_at", "host_id",
        }
        columns = [k for k in fields if k in allowed]
        if not columns:
            return
        assignments = ", ".join(f"{col} = %s" for col in columns)
        values = [fields[col] for col in columns]
        values.append(giveaway_id)
        await self.pool.execute(
            f"UPDATE giveaways SET {assignments} WHERE id = %s", *values
        )

    async def delete(self, giveaway_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id = %s", giveaway_id
        )
        await self.pool.execute("DELETE FROM giveaways WHERE id = %s", giveaway_id)

    async def for_guild(
        self, guild_id: int, status: Optional[str] = None
    ) -> List[dict]:
        if status is None:
            return await self.pool.fetch(
                "SELECT * FROM giveaways WHERE guild_id = %s "
                "ORDER BY created_at DESC",
                guild_id,
            )
        return await self.pool.fetch(
            "SELECT * FROM giveaways WHERE guild_id = %s AND status = %s "
            "ORDER BY created_at DESC",
            guild_id,
            status,
        )

    async def due(self, now: int) -> List[dict]:
        return await self.pool.fetch(
            "SELECT * FROM giveaways WHERE status = 'running' "
            "AND ends_at IS NOT NULL AND ends_at <= %s",
            now,
        )

    # -- entries ---------------------------------------------------------- #

    async def add_entry(self, giveaway_id: int, user_id: int) -> bool:
        """Insert an entry; returns False when the user already entered."""
        existing = await self.pool.fetchval(
            "SELECT user_id FROM giveaway_entries "
            "WHERE giveaway_id = %s AND user_id = %s",
            giveaway_id,
            user_id,
        )
        if existing:
            return False
        await self.pool.execute(
            "INSERT INTO giveaway_entries (giveaway_id, user_id, entered_at) "
            "VALUES (%s, %s, %s)",
            giveaway_id,
            user_id,
            int(time.time()),
        )
        return True

    async def remove_entry(self, giveaway_id: int, user_id: int) -> bool:
        existing = await self.pool.fetchval(
            "SELECT user_id FROM giveaway_entries "
            "WHERE giveaway_id = %s AND user_id = %s",
            giveaway_id,
            user_id,
        )
        if not existing:
            return False
        await self.pool.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id = %s AND user_id = %s",
            giveaway_id,
            user_id,
        )
        return True

    async def count_entries(self, giveaway_id: int) -> int:
        value = await self.pool.fetchval(
            "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id = %s",
            giveaway_id,
        )
        return int(value or 0)

    async def entries(self, giveaway_id: int) -> List[int]:
        rows = await self.pool.fetch(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = %s "
            "ORDER BY entered_at ASC",
            giveaway_id,
        )
        return [int(r["user_id"]) for r in rows]

    # -- blacklist -------------------------------------------------------- #

    async def blacklist_add(
        self, guild_id: int, object_id: int, mode: str
    ) -> bool:
        existing = await self.pool.fetchval(
            "SELECT object_id FROM giveaway_blacklist "
            "WHERE guild_id = %s AND object_id = %s AND mode = %s",
            guild_id,
            object_id,
            mode,
        )
        if existing:
            return False
        await self.pool.execute(
            "INSERT INTO giveaway_blacklist (guild_id, object_id, mode) "
            "VALUES (%s, %s, %s)",
            guild_id,
            object_id,
            mode,
        )
        return True

    async def blacklist_remove(
        self, guild_id: int, object_id: int, mode: str
    ) -> bool:
        existing = await self.pool.fetchval(
            "SELECT object_id FROM giveaway_blacklist "
            "WHERE guild_id = %s AND object_id = %s AND mode = %s",
            guild_id,
            object_id,
            mode,
        )
        if not existing:
            return False
        await self.pool.execute(
            "DELETE FROM giveaway_blacklist "
            "WHERE guild_id = %s AND object_id = %s AND mode = %s",
            guild_id,
            object_id,
            mode,
        )
        return True

    async def blacklist_rows(self, guild_id: int) -> List[dict]:
        return await self.pool.fetch(
            "SELECT object_id, mode FROM giveaway_blacklist WHERE guild_id = %s",
            guild_id,
        )

    async def blacklist_ids(self, guild_id: int, mode: str) -> set:
        rows = await self.pool.fetch(
            "SELECT object_id FROM giveaway_blacklist WHERE guild_id = %s AND mode = %s",
            guild_id,
            mode,
        )
        return {int(r["object_id"]) for r in rows}

    # -- config ----------------------------------------------------------- #

    async def get_channel(self, guild_id: int) -> Optional[int]:
        value = await self.pool.fetchval(
            "SELECT channel_id FROM giveaway_config WHERE guild_id = %s", guild_id
        )
        return int(value) if value else None

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        await self.pool.execute(
            "INSERT INTO giveaway_config (guild_id, channel_id) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE channel_id = VALUES(channel_id)",
            guild_id,
            channel_id,
        )

    async def clear_channel(self, guild_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM giveaway_config WHERE guild_id = %s", guild_id
        )


# --------------------------------------------------------------------------- #
# UI — modals
# --------------------------------------------------------------------------- #


class GiveawayModal(Modal):
    """Base modal that funnels *all* of its inputs into a single coroutine.

    Subclasses override :meth:`submit`, which receives the stripped values in
    declaration order.
    """

    def __init__(self, cog: "Engagement", giveaway_id: int, title: str) -> None:
        super().__init__(title=title, timeout=BUTTON_TIMEOUT)
        self.cog = cog
        self.giveaway_id = giveaway_id
        self._inputs: List[TextInput] = []

    def add_input(self, item: TextInput) -> None:
        self._inputs.append(item)
        self.add_item(item)

    async def on_submit(self, interaction: Interaction) -> None:  # pragma: no cover
        raise NotImplementedError

    async def on_error(  # pragma: no cover - safety net
        self, interaction: Interaction, error: Exception
    ) -> None:
        _log.exception("Giveaway modal failed", exc_info=error)
        try:
            await interaction.response.send_message(
                "Something went wrong while saving that. Please try again.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


class PrizeModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Set the prize")
        self.prize = TextInput(
            label="Prize",
            style=TextStyle.short,
            placeholder="e.g. Nitro Classic",
            max_length=256,
            required=True,
        )
        self.add_input(self.prize)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_prize(interaction, self.giveaway_id, self.prize.value)


class DurationModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Set the duration")
        self.duration = TextInput(
            label="Duration",
            style=TextStyle.short,
            placeholder="e.g. 10m, 1h30m, 2 days",
            max_length=32,
            required=True,
        )
        self.add_input(self.duration)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_duration(interaction, self.giveaway_id, self.duration.value)


class RequiredRoleModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Set the required role")
        self.role = TextInput(
            label="Required role",
            style=TextStyle.short,
            placeholder="Role name, @role or ID (empty to clear)",
            max_length=100,
            required=False,
        )
        self.add_input(self.role)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_required_role(interaction, self.giveaway_id, self.role.value)


class RequiredAgeModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Set the required server age")
        self.days = TextInput(
            label="Minimum days in server",
            style=TextStyle.short,
            placeholder="e.g. 7 (empty or 0 to clear)",
            max_length=4,
            required=False,
        )
        self.add_input(self.days)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_required_age(interaction, self.giveaway_id, self.days.value)


class RequiredLevelModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Set the required level")
        self.level = TextInput(
            label="Minimum level",
            style=TextStyle.short,
            placeholder="e.g. 10 (empty or 0 to clear)",
            max_length=4,
            required=False,
        )
        self.add_input(self.level)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_required_level(interaction, self.giveaway_id, self.level.value)


class EmbedModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Custom embed script")
        self.script = TextInput(
            label="Embed script",
            style=TextStyle.paragraph,
            placeholder="{embed}$v{title: ...}$v{description: ...}  (empty = default)",
            max_length=4000,
            required=False,
        )
        self.add_input(self.script)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_embed(interaction, self.giveaway_id, self.script.value)


class EntriesModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Max entries")
        self.entries = TextInput(
            label="Maximum entries",
            style=TextStyle.short,
            placeholder="e.g. 100 (empty or 0 = unlimited)",
            max_length=6,
            required=False,
        )
        self.add_input(self.entries)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_entries(interaction, self.giveaway_id, self.entries.value)


class WinnersModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Number of winners")
        self.winners = TextInput(
            label="Winners",
            style=TextStyle.short,
            placeholder="e.g. 1",
            max_length=3,
            required=True,
        )
        self.add_input(self.winners)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_winners(interaction, self.giveaway_id, self.winners.value)


class ColourModal(GiveawayModal):
    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(cog, giveaway_id, "Change the embed colour")
        self.colour = TextInput(
            label="Colour",
            style=TextStyle.short,
            placeholder="e.g. #ff79c6",
            max_length=16,
            required=True,
        )
        self.add_input(self.colour)

    async def on_submit(self, interaction: Interaction) -> None:
        await self.cog.save_colour(interaction, self.giveaway_id, self.colour.value)


class EditPickModal(Modal, title="Edit a giveaway"):
    """Opens a giveaway by message id/link and then shows the edit picker."""

    def __init__(self, cog: "Engagement") -> None:
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.cog = cog
        self.reference = TextInput(
            label="Message ID or link",
            style=TextStyle.short,
            placeholder="1234567890123456789 or the message link",
            max_length=200,
            required=True,
        )
        self.add_item(self.reference)

    async def on_submit(self, interaction: Interaction) -> None:
        giveaway = await self.cog.resolve_giveaway(interaction, self.reference.value)
        if giveaway is None:
            return
        await self.cog.open_edit_picker(interaction, giveaway)


# --------------------------------------------------------------------------- #
# UI — entry button (persistent) and setup panel
# --------------------------------------------------------------------------- #


class EnterButton(Button):
    def __init__(self, giveaway_id: int) -> None:
        super().__init__(
            label="Enter",
            emoji=STATE_ICON,
            style=ButtonStyle.secondary,
            custom_id=f"giveaway:enter:{giveaway_id}",
        )
        self.giveaway_id = giveaway_id

    async def callback(self, interaction: Interaction) -> None:
        client = interaction.client
        cog = client.get_cog("Engagement")
        if cog is not None:
            await cog.handle_enter(interaction, self.giveaway_id)


class GiveawayView(View):
    """Persistent view holding the single Enter button of a running giveaway."""

    def __init__(self, giveaway_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(EnterButton(giveaway_id))


class SetupView(View):
    """Ephemeral setup panel used by ``,giveaway start``."""

    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.cog = cog
        self.giveaway_id = giveaway_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        giveaway = await self.cog.store.get(self.giveaway_id)
        if giveaway is None or giveaway.get("host_id") != interaction.user.id:
            await interaction.response.send_message(
                "This setup panel isn't yours. Run `giveaway start` to make your own.",
                ephemeral=True,
            )
            return False
        return True

    @button(label="Start", style=ButtonStyle.primary, row=0)
    async def start_button(self, interaction: Interaction, _button: Button) -> None:
        await self.cog.start_giveaway(interaction)

    @button(label="Prize", style=ButtonStyle.primary, row=0)
    async def prize_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(PrizeModal(self.cog, self.giveaway_id))

    @button(label="Duration", style=ButtonStyle.primary, row=0)
    async def duration_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(DurationModal(self.cog, self.giveaway_id))

    @button(label="Required Role", style=ButtonStyle.secondary, row=1)
    async def role_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredRoleModal(self.cog, self.giveaway_id))

    @button(label="Required Age", style=ButtonStyle.secondary, row=1)
    async def age_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredAgeModal(self.cog, self.giveaway_id))

    @button(label="Required Level", style=ButtonStyle.secondary, row=2)
    async def level_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredLevelModal(self.cog, self.giveaway_id))

    @button(label="Embed", style=ButtonStyle.secondary, row=2)
    async def embed_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(EmbedModal(self.cog, self.giveaway_id))

    @button(label="Entries", style=ButtonStyle.secondary, row=3)
    async def entries_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(EntriesModal(self.cog, self.giveaway_id))

    @button(label="Winners", style=ButtonStyle.primary, row=3)
    async def winners_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(WinnersModal(self.cog, self.giveaway_id))

    @button(label="Cancel", style=ButtonStyle.danger, row=3, emoji=EMOJIS.CANCEL)
    async def cancel_button(self, interaction: Interaction, _button: Button) -> None:
        await self.cog.cancel_draft(interaction)

    async def on_timeout(self) -> None:
        await self.cog.expire_draft(self.giveaway_id)


class EditView(View):
    """Edit picker shown for a running giveaway — one button per setup option."""

    def __init__(self, cog: "Engagement", giveaway_id: int) -> None:
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.cog = cog
        self.giveaway_id = giveaway_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        return True

    @button(label="Prize", style=ButtonStyle.primary, row=0)
    async def prize_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(PrizeModal(self.cog, self.giveaway_id))

    @button(label="Duration", style=ButtonStyle.primary, row=0)
    async def duration_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(DurationModal(self.cog, self.giveaway_id))

    @button(label="Required Role", style=ButtonStyle.secondary, row=0)
    async def role_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredRoleModal(self.cog, self.giveaway_id))

    @button(label="Required Age", style=ButtonStyle.secondary, row=1)
    async def age_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredAgeModal(self.cog, self.giveaway_id))

    @button(label="Required Level", style=ButtonStyle.secondary, row=1)
    async def level_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(RequiredLevelModal(self.cog, self.giveaway_id))

    @button(label="Embed", style=ButtonStyle.secondary, row=2)
    async def embed_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(EmbedModal(self.cog, self.giveaway_id))

    @button(label="Entries", style=ButtonStyle.secondary, row=2)
    async def entries_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(EntriesModal(self.cog, self.giveaway_id))

    @button(label="Winners", style=ButtonStyle.primary, row=2)
    async def winners_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(WinnersModal(self.cog, self.giveaway_id))

    @button(label="Colour", style=ButtonStyle.secondary, row=3)
    async def colour_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(ColourModal(self.cog, self.giveaway_id))


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


class Engagement(LevelMixin, HighlightMixin, CogMeta):
    """Giveaway command group, leveling commands and lifecycle manager."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.store = GiveawayStore(bot)
        self.level_store = LevelStore(bot)
        self._end_locks: Dict[int, asyncio.Lock] = {}
        self._init_level()
        self._init_highlight()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def cog_load(self) -> None:
        running = await self.bot.pool.fetch(
            "SELECT id FROM giveaways WHERE status = 'running'"
        )
        for row in running:
            self.bot.add_view(GiveawayView(int(row["id"])))
        _log.info("Registered %d persistent giveaway views", len(running))
        self.giveaway_scheduler.start()

    async def cog_unload(self) -> None:
        self.giveaway_scheduler.cancel()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    async def build_embed(self, giveaway: dict, *, entry_count: int) -> Embed:
        """Build the live/variant embed for a giveaway."""
        custom = await self._build_custom_embed(giveaway, entry_count)
        if custom is not None:
            return custom

        winners = int(giveaway.get("winners") or 1)
        status = giveaway.get("status") or "running"
        fields: List[Tuple[str, str, bool]] = [
            ("Winners", f"**{winners}**", True),
            (
                "Entries",
                f"**{entry_count}**"
                + (
                    f" / {giveaway['max_entries']}"
                    if giveaway.get("max_entries")
                    else ""
                ),
                True,
            ),
        ]

        if status == "running":
            fields.append(("Ends", format_dt_rel(giveaway.get("ends_at")), True))
        elif status == "ended":
            fields.append(("Ended", format_dt_rel(giveaway.get("ended_at")), True))

        requirements: List[str] = []
        role_id = giveaway.get("required_role_id")
        if role_id:
            role = None
            guild = self.bot.get_guild(giveaway["guild_id"])
            if guild is not None:
                role = guild.get_role(role_id)
            requirements.append(
                f"{LOCK_ICON} Role: {role.mention if role else f'`{role_id}`'}"
            )
        required_stay = giveaway.get("required_age") or 0
        if required_stay:
            requirements.append(f"⏳ Stay: **{required_stay}** day(s)")
        required_level = giveaway.get("required_level") or 0
        if required_level:
            requirements.append(f"📈 Level: **{required_level}**+")
        if requirements:
            fields.append(("Requirements", "\n".join(requirements), False))

        if status == "ended":
            winners_text = mentions_from(giveaway.get("winner_ids"))
            fields.append(("🥇 Winners", winners_text, False))

        description = f"React with the button below to enter for **{giveaway.get('prize') or 'the prize'}**."
        if status == "ended":
            description = f"The giveaway for **{giveaway.get('prize') or 'the prize'}** has ended."

        embed = Embed(
            title=f"{STATE_ICON} {giveaway.get('prize') or 'Giveaway'}",
            description=description,
            color=COLORS.neutral,
            timestamp=datetime.now(timezone.utc),
        )
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
        embed.set_footer(text=f"Hosted by the giveaway system • ID {giveaway['id']}")
        return embed

    async def _build_custom_embed(
        self, giveaway: dict, entry_count: int
    ) -> Optional[Embed]:
        script = giveaway.get("embed_script")
        if not script:
            return None
        try:
            content, embed, _view = await EmbedBuilder.to_object(
                EmbedBuilder.embed_replacement(
                    self.bot.user,  # type: ignore[arg-type]
                    script,
                )
            )
        except Exception as exc:  # pragma: no cover - rendering must never break
            _log.warning("Bad giveaway embed script (%s): %s", giveaway["id"], exc)
            return None
        if embed is None and not content:
            return None
        if embed is None:
            from discord import Embed as _Embed

            embed = _Embed(description=str(content), color=COLORS.neutral)
        return embed

    def build_panel_embed(self, giveaway: dict) -> Embed:
        """Build the setup panel embed showing the current configuration."""
        prize = giveaway.get("prize")
        duration = giveaway.get("duration")
        winners = int(giveaway.get("winners") or 1)
        ends_at = int(time.time()) + duration if duration else None

        lines = [
            f"{STATE_ICON} **Prize:** {prize or '*not set*'}",
            f"⏳ **Duration:** {humanize_delta(duration) if duration else '*not set*'}"
            + (f" (ends {format_dt_rel(ends_at)})" if ends_at else ""),
            f"🏆 **Winners:** {winners}",
        ]

        role_id = giveaway.get("required_role_id")
        if role_id:
            guild = self.bot.get_guild(giveaway["guild_id"])
            role = guild.get_role(role_id) if guild else None
            lines.append(f"{LOCK_ICON} **Required role:** {role.mention if role else f'`{role_id}`'}")
        else:
            lines.append(f"{LOCK_ICON} **Required role:** *none*")

        required_stay = giveaway.get("required_age") or 0
        lines.append(f"⏳ **Required stay:** {required_stay} day(s)" if required_stay else "⏳ **Required stay:** *none*")

        required_level = giveaway.get("required_level") or 0
        lines.append(
            f"📈 **Required level:** {required_level}" if required_level else "📈 **Required level:** *none*"
        )

        max_entries = giveaway.get("max_entries") or 0
        lines.append(
            f"🎟️ **Max entries:** {max_entries}" if max_entries else "🎟️ **Max entries:** *unlimited*"
        )
        lines.append(
            "🖼️ **Custom embed:** set" if giveaway.get("embed_script") else "🖼️ **Custom embed:** default"
        )

        ready = bool(prize and duration)
        embed = Embed(
            title="Giveaway setup",
            description="\n".join(lines)
            + (
                "\n\nPress **Start** to launch the giveaway."
                if ready
                else "\n\nSet a **prize** and a **duration** before starting."
            ),
            color=COLORS.approve if ready else COLORS.warn,
        )
        embed.set_footer(text="Buttons: Prize · Duration · Required Role · Required Age · Required Level · Embed · Entries · Winners")
        return embed

    # ------------------------------------------------------------------ #
    # Panel / draft flow
    # ------------------------------------------------------------------ #

    async def post_panel(self, ctx: Context) -> Message:
        giveaway_id = discord.utils.time_snowflake(datetime.now(timezone.utc))
        panel = await ctx.send(
            embed=self.build_panel_embed(
                {
                    "prize": None,
                    "duration": None,
                    "winners": 1,
                    "required_role_id": None,
                    "required_age": 0,
                    "required_level": 0,
                    "max_entries": 0,
                    "embed_script": None,
                    "guild_id": ctx.guild.id,
                    "id": giveaway_id,
                }
            ),
            view=SetupView(self, giveaway_id),
        )
        await self.store.create_draft(
            giveaway_id=giveaway_id,
            guild_id=ctx.guild.id,
            host_id=ctx.author.id,
            panel_message_id=panel.id,
        )
        return panel

    async def _refresh_panel(self, interaction: Interaction, giveaway_id: int) -> None:
        giveaway = await self.store.get(giveaway_id)
        if giveaway is None:
            return
        try:
            await interaction.response.edit_message(embed=self.build_panel_embed(giveaway))
        except discord.HTTPException:
            pass

    async def _current_giveaway(
        self, interaction: Interaction, giveaway_id: int
    ) -> Optional[dict]:
        giveaway = await self.store.get(giveaway_id)
        if giveaway is None:
            await interaction.response.send_message(
                "This setup panel is no longer valid.", ephemeral=True
            )
            return None
        if giveaway.get("status") != "draft":
            await interaction.response.send_message(
                "This giveaway has already been started.", ephemeral=True
            )
            return None
        return giveaway

    # -- individual save handlers (called from the modals) -------------- #

    async def save_prize(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        prize = value.strip()[:256]
        if not prize:
            await interaction.response.send_message("The prize can't be empty.", ephemeral=True)
            return
        await self.store.update(giveaway_id, prize=prize)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_duration(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        try:
            duration = parse_duration(value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.store.update(giveaway_id, duration=duration)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_required_role(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        text = value.strip()
        if not text or text.lower() in ("none", "clear", "0"):
            await self.store.update(giveaway_id, required_role_id=None)
            await self._refresh_panel(interaction, giveaway_id)
            return
        role = await self._resolve_role(interaction.guild, text)
        if role is None:
            await interaction.response.send_message(
                "I couldn't find that role. Try a mention, the role name or its ID.",
                ephemeral=True,
            )
            return
        await self.store.update(giveaway_id, required_role_id=role.id)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_required_age(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        text = value.strip()
        if not text:
            days = 0
        else:
            try:
                days = int(text)
            except ValueError:
                await interaction.response.send_message(
                    "Please enter a whole number of days.", ephemeral=True
                )
                return
        if days < 0:
            await interaction.response.send_message(
                "Days can't be negative.", ephemeral=True
            )
            return
        if days > 3650:
            await interaction.response.send_message(
                "That's over 10 years — try something smaller.", ephemeral=True
            )
            return
        await self.store.update(giveaway_id, required_age=days or None)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_required_level(
        self, interaction: Interaction, giveaway_id: int, value: str
    ) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        text = value.strip()
        if not text:
            level = 0
        else:
            try:
                level = int(text)
            except ValueError:
                await interaction.response.send_message(
                    "Please enter a whole level number.", ephemeral=True
                )
                return
        if level < 0:
            await interaction.response.send_message(
                "A level can't be negative.", ephemeral=True
            )
            return
        await self.store.update(giveaway_id, required_level=level or None)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_embed(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        script = value.strip()
        if not script:
            await self.store.update(giveaway_id, embed_script=None)
            await self._refresh_panel(interaction, giveaway_id)
            return

        embed = await self._build_custom_embed({"id": giveaway_id, "embed_script": script}, 0)
        if embed is None:
            await interaction.response.send_message(
                "That embed script didn't produce anything. Double-check the syntax.",
                ephemeral=True,
            )
            return
        await self.store.update(giveaway_id, embed_script=script)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_entries(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        try:
            cap = parse_entries_cap(value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self.store.update(giveaway_id, max_entries=cap)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_winners(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self._current_giveaway(interaction, giveaway_id)
        if giveaway is None:
            return
        try:
            winners = int(value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Please enter a whole number of winners.", ephemeral=True
            )
            return
        if winners < 1 or winners > MAX_WINNERS:
            await interaction.response.send_message(
                f"Winners must be between **1** and **{MAX_WINNERS}**.", ephemeral=True
            )
            return
        await self.store.update(giveaway_id, winners=winners)
        await self._refresh_panel(interaction, giveaway_id)

    async def save_colour(self, interaction: Interaction, giveaway_id: int, value: str) -> None:
        giveaway = await self.store.get(giveaway_id)
        if giveaway is None:
            await interaction.response.send_message("This giveaway no longer exists.", ephemeral=True)
            return
        try:
            colour = parse_colour(value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        base = giveaway.get("embed_script")
        script = self._recolour_script(base, colour)
        await self.store.update(giveaway_id, embed_script=script)
        await self.cog_refresh(giveaway)
        await interaction.response.send_message(
            f"Embed colour updated to `#{colour:06x}`.", ephemeral=True
        )

    @staticmethod
    def _recolour_script(script: Optional[str], colour: int) -> str:
        """Force a fixed ``color:`` onto an embed script (creating one if needed)."""
        hexed = f"{colour:06x}"
        if not script:
            return f"{{embed}}$v{{color: {hexed}}}"
        if "{color:" in script:
            return re.sub(r"\{color:[^}]*\}", f"{{color: {hexed}}}", script, count=1)
        if "{embed}" in script:
            return script.replace("{embed}", f"{{embed}}$v{{color: {hexed}}}", 1)
        return f"{{embed}}$v{{color: {hexed}}}$v" + script

    # ------------------------------------------------------------------ #
    # Starting / cancelling
    # ------------------------------------------------------------------ #

    async def start_giveaway(self, interaction: Interaction) -> None:
        if interaction.message is None:
            await interaction.response.send_message(
                "I couldn't find the setup panel.", ephemeral=True
            )
            return

        panel = await self.store.get_panel(interaction.message.id)
        if panel is None:
            await interaction.response.send_message(
                "This setup panel is no longer valid.", ephemeral=True
            )
            return

        giveaway = await self._current_giveaway(interaction, panel["id"])
        if giveaway is None:
            return
        if not giveaway.get("prize"):
            await interaction.response.send_message(
                "You need to set a **prize** before starting.", ephemeral=True
            )
            return
        if not giveaway.get("duration"):
            await interaction.response.send_message(
                "You need to set a **duration** before starting.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Giveaways can only run in a server.", ephemeral=True
            )
            return

        channel = await self._resolve_target_channel(interaction)
        if channel is None:
            return

        now = int(time.time())
        ends_at = now + int(giveaway["duration"])
        embed = await self.build_embed(
            {**giveaway, "status": "running", "ends_at": ends_at}, entry_count=0
        )

        try:
            message = await channel.send(embed=embed, view=GiveawayView(giveaway["id"]))
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to post in {channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"Failed to post the giveaway: {exc}", ephemeral=True
            )
            return

        await self.store.update(
            giveaway["id"],
            status="running",
            channel_id=channel.id,
            message_id=message.id,
            ends_at=ends_at,
            started_at=now,
        )

        try:
            await interaction.response.edit_message(
                content=f"{EMOJIS.APPROVE} Giveaway started in {channel.mention} — it ends {format_dt_rel(ends_at)}.",
                embed=None,
                view=None,
            )
        except discord.HTTPException:
            pass

    async def _resolve_target_channel(self, interaction: Interaction) -> Optional[TextChannel]:
        guild = interaction.guild
        configured = await self.store.get_channel(guild.id)
        if configured:
            channel = guild.get_channel(configured)
            if isinstance(channel, TextChannel):
                return channel
        if isinstance(interaction.channel, TextChannel):
            return interaction.channel
        return None

    async def cancel_draft(self, interaction: Interaction) -> None:
        panel = await self.store.get_panel(interaction.message.id)
        if panel:
            await self.store.delete(panel["id"])
        try:
            await interaction.response.edit_message(
                content=f"{EMOJIS.DENY} Giveaway setup cancelled.", embed=None, view=None
            )
        except discord.HTTPException:
            pass

    async def expire_draft(self, giveaway_id: int) -> None:
        giveaway = await self.store.get(giveaway_id)
        if giveaway and giveaway.get("status") == "draft":
            await self.store.delete(giveaway_id)

    # ------------------------------------------------------------------ #
    # Entering
    # ------------------------------------------------------------------ #

    async def handle_enter(self, interaction: Interaction, giveaway_id: int) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Giveaways can only be entered in a server.", ephemeral=True
            )
            return

        giveaway = await self.store.get(giveaway_id)
        if giveaway is None:
            await interaction.response.send_message(
                "That giveaway no longer exists.", ephemeral=True
            )
            return
        if giveaway.get("status") != "running":
            await interaction.response.send_message(
                "That giveaway has already ended.", ephemeral=True
            )
            return
        if giveaway.get("ends_at") and int(giveaway["ends_at"]) <= int(time.time()):
            await interaction.response.send_message(
                "That giveaway has just ended.", ephemeral=True
            )
            return

        member = interaction.user
        if not isinstance(member, Member):
            await interaction.response.send_message(
                "I couldn't resolve you as a server member.", ephemeral=True
            )
            return

        # Global (bot-level) blacklist
        if await self.bot.is_blacklisted(member.id):
            await interaction.response.send_message(
                "You're blacklisted from using the bot.", ephemeral=True
            )
            return

        # Giveaway-specific blacklist: users are an explicit block, roles are
        # evaluated together with the other requirements.
        blacklisted_users = await self.store.blacklist_ids(giveaway["guild_id"], "user")
        if member.id in blacklisted_users:
            await interaction.response.send_message(
                "You're blacklisted from entering giveaways here.", ephemeral=True
            )
            return

        blacklisted_roles = await self.store.blacklist_ids(giveaway["guild_id"], "role")
        reason = await requirement_failure(
            member, giveaway, blacklisted_roles, self.level_store
        )
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        # Max entries cap
        cap = giveaway.get("max_entries") or 0
        if cap:
            count = await self.store.count_entries(giveaway_id)
            if count >= cap:
                await interaction.response.send_message(
                    "This giveaway is full — no more entries.", ephemeral=True
                )
                return

        entered = await self.store.add_entry(giveaway_id, member.id)
        if not entered:
            await interaction.response.send_message(
                "You're already entered in this giveaway.", ephemeral=True
            )
            return

        count = await self.store.count_entries(giveaway_id)
        await interaction.response.send_message(
            f"{EMOJIS.APPROVE} You entered the giveaway for **{giveaway.get('prize')}**! "
            f"Good luck 🍀",
            ephemeral=True,
        )
        await self.refresh_giveaway_message(giveaway, entry_count=count)

    # ------------------------------------------------------------------ #
    # Ending / reroll
    # ------------------------------------------------------------------ #

    def _end_lock(self, giveaway_id: int) -> asyncio.Lock:
        lock = self._end_locks.get(giveaway_id)
        if lock is None:
            lock = asyncio.Lock()
            self._end_locks[giveaway_id] = lock
        return lock

    async def finish_giveaway(self, giveaway: dict, *, reason: str = "time") -> Optional[dict]:
        """End a running giveaway, pick winners and announce them."""
        giveaway_id = giveaway["id"]
        async with self._end_lock(giveaway_id):
            fresh = await self.store.get(giveaway_id)
            if fresh is None or fresh.get("status") != "running":
                return None

            entries = await self.store.entries(giveaway_id)
            winners = int(fresh.get("winners") or 1)
            winner_ids = sort_winners(entries, winners)
            winner_str = ",".join(str(w) for w in winner_ids) or None
            now = int(time.time())

            await self.store.update(
                giveaway_id,
                status="ended",
                ended_at=now,
                winner_ids=winner_str,
            )
            fresh = await self.store.get(giveaway_id)

        embed = await self.build_embed(fresh, entry_count=len(entries))
        await self._render_message(
            fresh,
            embed=embed,
            view=None,
        )

        await self._announce_winners(fresh, winner_ids, entries, reroll=False)
        _log.info(
            "Giveaway %s ended (%s) with %d entries -> %s",
            giveaway_id,
            reason,
            len(entries),
            winner_ids,
        )
        return fresh

    async def reroll_giveaway(self, giveaway: dict) -> Tuple[List[int], int, int]:
        """Pick fresh winners for an ended giveaway.

        Returns ``(winners, entry_count, reroll_count)``.
        """
        giveaway_id = giveaway["id"]
        async with self._end_lock(giveaway_id):
            fresh = await self.store.get(giveaway_id)
            if fresh is None or fresh.get("status") != "ended":
                return [], 0, int(giveaway.get("reroll_count") or 0)

            entries = await self.store.entries(giveaway_id)
            winners = int(fresh.get("winners") or 1)
            winner_ids = sort_winners(entries, winners)
            reroll_count = int(fresh.get("reroll_count") or 0) + 1

            await self.store.update(
                giveaway_id,
                winner_ids=",".join(str(w) for w in winner_ids) or None,
                reroll_count=reroll_count,
            )
            fresh = await self.store.get(giveaway_id)

        embed = await self.build_embed(fresh, entry_count=len(entries))
        await self._render_message(fresh, embed=embed, view=None)
        await self._announce_winners(fresh, winner_ids, entries, reroll=True)
        return winner_ids, len(entries), reroll_count

    async def _announce_winners(
        self,
        giveaway: dict,
        winner_ids: List[int],
        entries: Sequence[int],
        *,
        reroll: bool,
    ) -> None:
        """Post a winners announcement in the giveaway channel."""
        channel = await self._giveaway_channel(giveaway)
        if channel is None:
            return

        prize = giveaway.get("prize") or "the prize"
        if winner_ids:
            winner_text = ", ".join(f"<@{w}>" for w in winner_ids)
            headline = "🔄 **Reroll**" if reroll else f"{STATE_ICON} **Giveaway ended**"
            embed = Embed(
                title="🔁 Reroll" if reroll else f"{STATE_ICON} Winners",
                description=(
                    f"{headline} — **{prize}**\n\n"
                    f"🏆 Winners: {winner_text}\n"
                    f"🎟️ Entries: **{len(entries)}**\n"
                    f"👥 Total unique entrants: **{len(set(entries))}**"
                ),
                color=COLORS.approve,
                timestamp=datetime.now(timezone.utc),
            )
        else:
            embed = Embed(
                title=f"{EMOJIS.WARN} No winners",
                description=(
                    f"The giveaway for **{prize}** ended with **no entries**, so there "
                    "was nobody to pick."
                ),
                color=COLORS.warn,
                timestamp=datetime.now(timezone.utc),
            )

        try:
            await channel.send(
                embed=embed, allowed_mentions=AllowedMentions(everyone=False, roles=False)
            )
        except discord.HTTPException as exc:
            _log.warning("Could not announce winners for %s: %s", giveaway["id"], exc)

    async def _render_message(
        self, giveaway: dict, *, embed: Embed, view: Optional[View]
    ) -> None:
        """Edit the original giveaway message in place, if we still can."""
        channel = await self._giveaway_channel(giveaway)
        if channel is None:
            return
        message_id = giveaway.get("message_id")
        if not message_id:
            return
        try:
            message = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            if view is None:
                await message.edit(embed=embed, view=None)
            else:
                await message.edit(embed=embed, view=view)
        except discord.HTTPException as exc:
            _log.warning("Could not edit giveaway message %s: %s", message_id, exc)

    async def refresh_giveaway_message(
        self, giveaway: dict, *, entry_count: Optional[int] = None
    ) -> None:
        if entry_count is None:
            entry_count = await self.store.count_entries(giveaway["id"])
        embed = await self.build_embed(giveaway, entry_count=entry_count)
        await self._render_message(giveaway, embed=embed, view=None)

    async def cog_refresh(self, giveaway: dict) -> None:
        """Alias kept for the colour editor: repaint the live message."""
        await self.refresh_giveaway_message(giveaway)

    async def _giveaway_channel(self, giveaway: dict) -> Optional[TextChannel]:
        channel_id = giveaway.get("channel_id")
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.HTTPException:
                return None
        return channel if isinstance(channel, TextChannel) else None

    # ------------------------------------------------------------------ #
    # Resolvers
    # ------------------------------------------------------------------ #

    async def resolve_giveaway(
        self, ctx_or_interaction: Union[Context, Interaction], reference: str
    ) -> Optional[dict]:
        """Resolve a giveaway from an id/link, honouring replies for prefix use."""
        guild = ctx_or_interaction.guild
        if guild is None:
            await self._respond(ctx_or_interaction, "This command only works in a server.", ok=False)
            return None

        message_id: Optional[int] = None

        if isinstance(ctx_or_interaction, Context):
            # Reply shortcut: borrow the referenced message's id.
            reference_message = getattr(ctx_or_interaction.message, "reference", None)
            if reference_message is not None and reference_message.message_id:
                message_id = reference_message.message_id

        if message_id is None and reference:
            message_id = parse_message_ref(reference)

        if message_id is None:
            await self._respond(
                ctx_or_interaction,
                "No giveaway given. Pass a **message ID or link**, or reply to the "
                "giveaway message.",
                ok=False,
            )
            return None

        giveaway = await self.store.get_by_message(guild.id, message_id)
        if giveaway is None and message_id != reference:
            giveaway = await self.store.get_by_message(guild.id, int(reference) if str(reference).isdigit() else 0)
        if giveaway is None:
            await self._respond(
                ctx_or_interaction,
                "I couldn't find a giveaway for that message in this server.",
                ok=False,
            )
            return None
        return giveaway

    async def _respond(
        self, ctx_or_interaction: Union[Context, Interaction], message: str, *, ok: bool
    ) -> None:
        if isinstance(ctx_or_interaction, Interaction):
            await ctx_or_interaction.response.send_message(message, ephemeral=True)
        else:
            if ok:
                await ctx_or_interaction.approve(message)
            else:
                await ctx_or_interaction.warn(message)

    @staticmethod
    async def _resolve_role(guild: discord.Guild, value: str) -> Optional[Role]:
        try:
            role = await RoleConverter().convert(_FakeCtx(guild), value)
        except Exception:
            role = None
        if role is not None:
            return role
        lowered = value.strip().lower()
        for candidate in guild.roles:
            if candidate.name.lower() == lowered:
                return candidate
        return None

    @staticmethod
    async def _resolve_member(guild: discord.Guild, value: str) -> Optional[Member]:
        try:
            member = await MemberConverter().convert(_FakeCtx(guild), value)
        except Exception:
            member = None
        if member is not None:
            return member
        lowered = value.strip().lower().lstrip("@")
        for candidate in guild.members:
            if candidate.name.lower() == lowered or candidate.display_name.lower() == lowered:
                return candidate
        return None

    # ------------------------------------------------------------------ #
    # Edit picker
    # ------------------------------------------------------------------ #

    async def open_edit_picker(
        self, interaction: Interaction, giveaway: dict
    ) -> None:
        if giveaway.get("status") == "draft":
            await interaction.response.send_message(
                "That giveaway is still a setup panel — edit it from its own buttons.",
                ephemeral=True,
            )
            return
        embed = await self.build_embed(
            giveaway, entry_count=await self.store.count_entries(giveaway["id"])
        )
        await interaction.response.send_message(
            content="Pick a button below to edit the giveaway.",
            embed=embed,
            view=EditView(self, giveaway["id"]),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #

    @dpy_tasks.loop(seconds=15)
    async def giveaway_scheduler(self) -> None:
        now = int(time.time())
        due = await self.store.due(now)
        for giveaway in due:
            try:
                await self.finish_giveaway(giveaway, reason="timer")
            except Exception:  # pragma: no cover - a bad row must not kill the loop
                _log.exception("Failed to end giveaway %s", giveaway.get("id"))

    @giveaway_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Group
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="giveaway",
        aliases=["gw"],
        invoke_without_command=True,
        description="Manage giveaways.",
        example="giveaway start",
    )
    @guild_only()
    async def giveaway(self, ctx: Context) -> Message:
        return await ctx.send_help(ctx.command)

    # ------------------------------------------------------------------ #
    # start
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="start",
        aliases=["gstart", "gwstart"],
        description="Start a new giveaway interactively (buttons collect the inputs).",
        example="giveaway start",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_start(self, ctx: Context) -> Message:
        return await self.post_panel(ctx)

    # ------------------------------------------------------------------ #
    # create (convenience)
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="create",
        aliases=["gcreate", "gwcreate"],
        description="Create a giveaway directly: prize, winners, duration.",
        example="giveaway create Nitro 1 1h",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_create(
        self,
        ctx: Context,
        prize: str,
        winners: int = 1,
        duration: str = "1h",
        channel: Optional[TextChannel] = None,
    ) -> Message:
        try:
            parsed_duration = parse_duration(duration)
        except ValueError as exc:
            return await ctx.warn(str(exc))

        if winners < 1 or winners > MAX_WINNERS:
            return await ctx.warn(
                f"Winners must be between **1** and **{MAX_WINNERS}**."
            )

        target = channel or ctx.channel
        if not isinstance(target, TextChannel):
            return await ctx.warn("I can't post a giveaway in this channel.")

        giveaway_id = discord.utils.time_snowflake(datetime.now(timezone.utc))
        now = int(time.time())
        ends_at = now + parsed_duration
        giveaway = {
            "id": giveaway_id,
            "guild_id": ctx.guild.id,
            "host_id": ctx.author.id,
            "prize": prize[:256],
            "duration": parsed_duration,
            "winners": winners,
            "max_entries": 0,
            "required_role_id": None,
            "required_age": 0,
            "required_level": 0,
            "embed_script": None,
            "status": "running",
            "ends_at": ends_at,
        }

        embed = await self.build_embed(giveaway, entry_count=0)
        try:
            message = await target.send(embed=embed, view=GiveawayView(giveaway_id))
        except discord.Forbidden:
            return await ctx.warn(f"I don't have permission to post in {target.mention}.")

        await self.bot.pool.execute(
            "INSERT INTO giveaways "
            "(id, guild_id, channel_id, host_id, status, prize, duration, ends_at, "
            " winners, created_at, started_at) "
            "VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s)",
            giveaway_id,
            ctx.guild.id,
            target.id,
            ctx.author.id,
            prize[:256],
            parsed_duration,
            ends_at,
            winners,
            now,
            now,
        )
        return await ctx.approve(
            f"Giveaway for **{prize[:256]}** started in {target.mention} — "
            f"ends {format_dt_rel(ends_at)}."
        )

    # ------------------------------------------------------------------ #
    # end
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="end",
        aliases=["gend", "gwend"],
        description="End a giveaway early and pick winners.",
        example="giveaway end 1234567890123456789",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_end(
        self, ctx: Context, *, message_id_or_link: str = None
    ) -> Message:
        giveaway = await self.resolve_giveaway(ctx, message_id_or_link)
        if giveaway is None:
            return

        if giveaway.get("status") == "ended":
            return await ctx.warn("That giveaway has already ended.")
        if giveaway.get("status") != "running":
            return await ctx.warn("That giveaway hasn't been started yet.")

        result = await self.finish_giveaway(giveaway, reason="manual")
        if result is None:
            return await ctx.warn("That giveaway is no longer running.")
        return await ctx.approve(f"Ended the giveaway for **{result.get('prize')}**.")

    # ------------------------------------------------------------------ #
    # cancel (bot command; not part of the primary spec)
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="cancel",
        aliases=["gcancel", "gwcancel"],
        description="Cancel an active giveaway (no winners are picked).",
        example="giveaway cancel 1234567890123456789",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_cancel(
        self, ctx: Context, *, message_id_or_link: str = None
    ) -> Message:
        giveaway = await self.resolve_giveaway(ctx, message_id_or_link)
        if giveaway is None:
            return
        if giveaway.get("status") != "running":
            return await ctx.warn("Only a running giveaway can be cancelled.")

        await self.store.delete(giveaway["id"])
        channel = await self._giveaway_channel(giveaway)
        if channel and giveaway.get("message_id"):
            try:
                message = await channel.fetch_message(int(giveaway["message_id"]))
                await message.edit(
                    embed=Embed(
                        title=f"{EMOJIS.DENY} Giveaway cancelled",
                        description=f"The giveaway for **{giveaway.get('prize')}** was cancelled.",
                        color=COLORS.deny,
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass
        return await ctx.approve(f"Cancelled the giveaway for **{giveaway.get('prize')}**.")

    # ------------------------------------------------------------------ #
    # reroll
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="reroll",
        aliases=["greroll", "gwreroll"],
        description="Reroll the winners of an ended giveaway.",
        example="giveaway reroll 1234567890123456789",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_reroll(
        self, ctx: Context, *, message_id_or_link: str = None
    ) -> Message:
        giveaway = await self.resolve_giveaway(ctx, message_id_or_link)
        if giveaway is None:
            return
        if giveaway.get("status") != "ended":
            return await ctx.warn("You can only reroll a giveaway that has ended.")

        winners, entry_count, reroll_count = await self.reroll_giveaway(giveaway)
        if not winners:
            return await ctx.warn("There were no entries to pick new winners from.")

        winner_text = ", ".join(f"<@{w}>" for w in winners)
        return await ctx.approve(
            f"Rerolled **{len(winners)}** winner(s) from **{entry_count}** entries "
            f"(reroll #{reroll_count}): {winner_text}"
        )

    # ------------------------------------------------------------------ #
    # entries
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="entries",
        aliases=["gentries", "gwentries", "entires", "gentires"],
        description="View the entries of a giveaway.",
        example="giveaway entries 1234567890123456789",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_entries(
        self, ctx: Context, *, message_id_or_link: str = None
    ) -> Message:
        giveaway = await self.resolve_giveaway(ctx, message_id_or_link)
        if giveaway is None:
            return

        entries = await self.store.entries(giveaway["id"])
        if not entries:
            return await ctx.warn("Nobody entered that giveaway.")

        winner_ids = {
            int(w) for w in (giveaway.get("winner_ids") or "").split(",") if w.strip().isdigit()
        }

        if entries and all(ctx.guild.get_member(uid) is None for uid in entries):
            lines = [f"`{uid}`" for uid in entries]
        else:
            lines = []
            for uid in entries:
                member = ctx.guild.get_member(uid)
                tag = "🏆 " if uid in winner_ids else ""
                lines.append(f"{tag}{member.mention if member else f'`{uid}`'}")

        pages = [
            lines[i : i + 20] for i in range(0, len(lines), 20)
        ] or [[]]
        embeds: List[Embed] = []
        for index, chunk in enumerate(pages, 1):
            embed = Embed(
                title=f"{STATE_ICON} Entries — {giveaway.get('prize')}",
                description="\n".join(chunk),
                color=COLORS.neutral,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(
                text=f"{giveaway['id']} • Page {index}/{len(pages)} • {len(entries)} entries"
            )
            embeds.append(embed)
        return await ctx.paginate(embeds)

    # ------------------------------------------------------------------ #
    # list
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="list",
        aliases=["glist", "gwlist"],
        description="List every giveaway in this guild (active, then historical).",
        example="giveaway list",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_list(self, ctx: Context) -> Message:
        rows = await self.store.for_guild(ctx.guild.id)
        if not rows:
            return await ctx.warn("There are no giveaways in this server yet.")

        running = [r for r in rows if r["status"] == "running"]
        ended = [r for r in rows if r["status"] == "ended"]
        drafts = [r for r in rows if r["status"] == "draft"]

        entries: List[str] = []
        for row in running + drafts + ended:
            status = row["status"]
            icon = {"running": "🟢", "ended": "⚪", "draft": "🟡"}.get(status, "⚪")
            prize = row.get("prize") or "Untitled"
            link = _link(ctx.guild.id, row.get("channel_id"), row.get("message_id"))
            when = ""
            if status == "running":
                when = f" • ends {format_dt_rel(row.get('ends_at'))}"
            elif status == "ended":
                when = f" • ended {format_dt_rel(row.get('ended_at'))}"
            entries.append(f"{icon} **{prize}** — [{row['id']}]({link}){when}")

        pages = [entries[i : i + LIST_PAGE_SIZE] for i in range(0, len(entries), LIST_PAGE_SIZE)]
        embeds: List[Embed] = []
        for index, chunk in enumerate(pages, 1):
            embed = Embed(
                title=f"Giveaways in {ctx.guild.name}",
                description="\n".join(chunk),
                color=COLORS.neutral,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(
                text=(
                    f"{len(running)} active • {len(drafts)} draft • {len(ended)} ended "
                    f"• Page {index}/{len(pages)}"
                )
            )
            embeds.append(embed)
        return await ctx.paginate(embeds)

    # ------------------------------------------------------------------ #
    # channel
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="channel",
        aliases=["gchannel", "gwchannel"],
        description="Set the default channel where giveaways will be posted.",
        example="giveaway channel #giveaways",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 5, BucketType.guild)
    async def giveaway_channel(
        self, ctx: Context, *, channel: Optional[TextChannel] = None
    ) -> Message:
        if channel is None:
            await self.store.clear_channel(ctx.guild.id)
            return await ctx.approve(
                "Giveaways will now be posted in the channel the command is run in."
            )
        await self.store.set_channel(ctx.guild.id, channel.id)
        return await ctx.approve(f"Giveaways will now be posted in {channel.mention}.")

    # ------------------------------------------------------------------ #
    # clear (bot command; not part of the primary spec)
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="clear",
        aliases=["gclear", "gwclear"],
        description="Clear ended giveaways from the database.",
        example="giveaway clear",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    @cooldown(1, 30, BucketType.guild)
    async def giveaway_clear(self, ctx: Context) -> Message:
        rows = await self.store.for_guild(ctx.guild.id, status="ended")
        for row in rows:
            await self.store.delete(row["id"])
        return await ctx.approve(f"Cleared **{len(rows)}** ended giveaway(s) from the database.")

    # ------------------------------------------------------------------ #
    # edit group — one subcommand per setup button (6 of them)
    # ------------------------------------------------------------------ #

    @giveaway.group(
        name="edit",
        invoke_without_command=True,
        description="Edit an active giveaway's configuration.",
        example="giveaway edit 1234567890123456789",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def giveaway_edit(
        self, ctx: Context, *, message_id_or_link: str = None
    ) -> Message:
        if not message_id_or_link and not getattr(ctx.message.reference, "message_id", None):
            # No reference to resolve — offer the ephemeral button picker instead.
            return await ctx.send(
                "Reply to a giveaway message or pass its ID/link, then pick what to edit "
                "with the buttons on that message.",
                view=EditLauncher(self),
            )
        giveaway = await self.resolve_giveaway(ctx, message_id_or_link)
        if giveaway is None:
            return
        if giveaway.get("status") == "draft":
            return await ctx.warn(
                "That giveaway is still a setup panel — edit it from its own buttons."
            )
        embed = await self.build_embed(
            giveaway, entry_count=await self.store.count_entries(giveaway["id"])
        )
        return await ctx.send(
            content="Pick a button below to edit the giveaway.",
            embed=embed,
            view=EditView(self, giveaway["id"]),
        )

    @giveaway_edit.command(
        name="prize",
        description="Change the prize of an active giveaway.",
        example="giveaway edit prize 1234567890123456789 Nitro",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_prize(self, ctx: Context, message_id: str, *, prize: str) -> Message:
        return await self._edit_field(ctx, message_id, "prize", prize)

    @giveaway_edit.command(
        name="winners",
        description="Change how many winners an active giveaway will pick.",
        example="giveaway edit winners 1234567890123456789 3",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_winners(self, ctx: Context, message_id: str, winners: int) -> Message:
        if winners < 1 or winners > MAX_WINNERS:
            return await ctx.warn(f"Winners must be between **1** and **{MAX_WINNERS}**.")
        return await self._edit_field(ctx, message_id, "winners", winners)

    @giveaway_edit.command(
        name="duration",
        description="Change how long an active giveaway lasts.",
        example="giveaway edit duration 1234567890123456789 2h",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_duration(self, ctx: Context, message_id: str, *, duration: str) -> Message:
        try:
            parsed = parse_duration(duration)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        giveaway = await self._get_editable(ctx, message_id)
        if giveaway is None:
            return
        ends_at = int(time.time()) + parsed
        await self.store.update(giveaway["id"], duration=parsed, ends_at=ends_at)
        await self._repaint(ctx, giveaway["id"])
        return await ctx.approve(
            f"Duration updated to **{humanize_delta(parsed)}** — now ends {format_dt_rel(ends_at)}."
        )

    @giveaway_edit.command(
        name="stay",
        description="Set the minimum server stay required to enter a giveaway (days).",
        example="giveaway edit stay 1234567890123456789 7",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_stay(self, ctx: Context, message_id: str, days: int) -> Message:
        if days < 0:
            return await ctx.warn("Days can't be negative.")
        return await self._edit_field(ctx, message_id, "required_age", days or None)

    @giveaway_edit.command(
        name="level",
        aliases=["requiredlevel"],
        description="Set the minimum level required to enter a giveaway.",
        example="giveaway edit level 1234567890123456789 10",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_level(self, ctx: Context, message_id: str, level: int) -> Message:
        if level < 0:
            return await ctx.warn("A level can't be negative.")
        return await self._edit_field(ctx, message_id, "required_level", level or None)

    @giveaway_edit.command(
        name="age",
        description="Set the minimum account age required to enter a giveaway (days).",
        example="giveaway edit age 1234567890123456789 30",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_age(self, ctx: Context, message_id: str, days: int) -> Message:
        if days < 0:
            return await ctx.warn("Days can't be negative.")
        return await self._edit_field(ctx, message_id, "account_age", days or None)

    @giveaway_edit.command(
        name="host",
        description="Transfer the host of an active giveaway to another member.",
        example="giveaway edit host 1234567890123456789 @voby",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_host(self, ctx: Context, message_id: str, *, member: Member) -> Message:
        return await self._edit_field(ctx, message_id, "host_id", member.id)

    @giveaway_edit.command(
        name="requiredroles",
        aliases=["requiredrole"],
        description="Replace the role required to enter a giveaway.",
        example="giveaway edit requiredroles 1234567890123456789 @Member",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_requiredroles(
        self, ctx: Context, message_id: str, *, role: Optional[str] = None
    ) -> Message:
        if role and role.strip().lower() not in ("none", "clear", "0"):
            resolved = await self._resolve_role(ctx.guild, role)
            if resolved is None:
                return await ctx.warn("I couldn't find that role.")
            value = resolved.id
        else:
            value = None
        return await self._edit_field(ctx, message_id, "required_role_id", value)

    @giveaway_edit.command(
        name="messages",
        description="Update the custom embed shown for an active giveaway.",
        example="giveaway edit messages 1234567890123456789 {embed}$v{description: hi}",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_messages(self, ctx: Context, message_id: str, *, text: str = None) -> Message:
        value = (text or "").strip() or None
        return await self._edit_field(ctx, message_id, "embed_script", value)

    @giveaway_edit.command(
        name="entries",
        aliases=["maxentries", "entires"],
        description="Set the maximum number of entries a giveaway can hold.",
        example="giveaway edit entries 1234567890123456789 100",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_entries(self, ctx: Context, message_id: str, entries: int) -> Message:
        if entries < 0:
            return await ctx.warn("Entries can't be negative — use `0` for unlimited.")
        return await self._edit_field(ctx, message_id, "max_entries", entries)

    @giveaway_edit.command(
        name="colour",
        aliases=["color"],
        description="Change the embed colour of an active giveaway.",
        example="giveaway edit colour 1234567890123456789 #ff79c6",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_colour(self, ctx: Context, message_id: str, *, colour: str) -> Message:
        try:
            parsed = parse_colour(colour)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        giveaway = await self._get_editable(ctx, message_id)
        if giveaway is None:
            return
        script = self._recolour_script(giveaway.get("embed_script"), parsed)
        await self.store.update(giveaway["id"], embed_script=script)
        await self._repaint(ctx, giveaway["id"])
        return await ctx.approve(f"Embed colour updated to `#{parsed:06x}`.")

    @giveaway_edit.command(
        name="image",
        description="Set the larger image shown on an active giveaway embed.",
        example="giveaway edit image 1234567890123456789 https://example.com/a.png",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_image(self, ctx: Context, message_id: str, url: str = None) -> Message:
        return await self._edit_media(ctx, message_id, "image", url)

    @giveaway_edit.command(
        name="thumbnail",
        aliases=["thumb"],
        description="Set the small image shown on an active giveaway embed.",
        example="giveaway edit thumbnail 1234567890123456789 https://example.com/a.png",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def edit_thumbnail(self, ctx: Context, message_id: str, url: str = None) -> Message:
        return await self._edit_media(ctx, message_id, "thumbnail", url)

    # ------------------------------------------------------------------ #
    # blacklist group
    # ------------------------------------------------------------------ #

    @giveaway.group(
        name="blacklist",
        aliases=["bl"],
        invoke_without_command=True,
        description="Manage the giveaway entry block-list.",
        example="giveaway blacklist list",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def giveaway_blacklist(self, ctx: Context) -> Message:
        return await ctx.send_help(ctx.command)

    @giveaway_blacklist.command(
        name="add",
        description="Block a role or user from entering giveaways in this server.",
        example="giveaway blacklist add @Member",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def blacklist_add(self, ctx: Context, *, target: str) -> Message:
        role = await self._resolve_role(ctx.guild, target)
        if role is not None:
            created = await self.store.blacklist_add(ctx.guild.id, role.id, "role")
            if not created:
                return await ctx.warn(f"{role.mention} is already blacklisted.")
            return await ctx.approve(f"Blacklisted the {role.mention} role from giveaways.")

        member = await self._resolve_member(ctx.guild, target)
        if member is not None:
            created = await self.store.blacklist_add(ctx.guild.id, member.id, "user")
            if not created:
                return await ctx.warn(f"{member.mention} is already blacklisted.")
            return await ctx.approve(f"Blacklisted {member.mention} from giveaways.")

        return await ctx.warn("I couldn't find that role or user.")

    @giveaway_blacklist.command(
        name="remove",
        aliases=["delete", "del"],
        description="Remove a role or user from the giveaway blacklist.",
        example="giveaway blacklist remove @Member",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def blacklist_remove(self, ctx: Context, *, target: str) -> Message:
        role = await self._resolve_role(ctx.guild, target)
        if role is not None:
            removed = await self.store.blacklist_remove(ctx.guild.id, role.id, "role")
            if not removed:
                return await ctx.warn(f"{role.mention} isn't blacklisted.")
            return await ctx.approve(f"Unblacklisted the {role.mention} role.")

        member = await self._resolve_member(ctx.guild, target)
        if member is not None:
            removed = await self.store.blacklist_remove(ctx.guild.id, member.id, "user")
            if not removed:
                return await ctx.warn(f"{member.mention} isn't blacklisted.")
            return await ctx.approve(f"Unblacklisted {member.mention}.")

        return await ctx.warn("I couldn't find that role or user.")

    @giveaway_blacklist.command(
        name="list",
        description="Show every role and user blacklisted from giveaways.",
        example="giveaway blacklist list",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def blacklist_list(self, ctx: Context) -> Message:
        rows = await self.store.blacklist_rows(ctx.guild.id)
        if not rows:
            return await ctx.warn("Nothing is blacklisted in this server.")

        users = [int(r["object_id"]) for r in rows if r["mode"] == "user"]
        roles = [int(r["object_id"]) for r in rows if r["mode"] == "role"]

        lines: List[str] = []
        if roles:
            lines.append("**Roles**")
            for rid in roles:
                role = ctx.guild.get_role(rid)
                lines.append(f"• {role.mention if role else f'`{rid}`'}")
        if users:
            if lines:
                lines.append("")
            lines.append("**Users**")
            for uid in users:
                member = ctx.guild.get_member(uid)
                lines.append(f"• {member.mention if member else f'`{uid}`'}")

        embed = Embed(
            title=f"{SHIELD_ICON} Giveaway blacklist",
            description="\n".join(lines),
            color=COLORS.neutral,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{len(roles)} role(s) • {len(users)} user(s)")
        return await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    # extraentries (bot command; not part of the primary spec)
    # ------------------------------------------------------------------ #

    @giveaway.command(
        name="extraentries",
        aliases=["gextraentries", "gwextraentries"],
        description="Show every role that grants extra giveaway entries.",
        example="giveaway extraentries",
    )
    @guild_only()
    @has_permissions(manage_guild=True)
    async def giveaway_extraentries(self, ctx: Context) -> Message:
        return await ctx.warn(
            "Extra role entries aren't part of this giveaway build — the entry button "
            "counts one entry per member."
        )

    # ------------------------------------------------------------------ #
    # Edit helpers
    # ------------------------------------------------------------------ #

    async def _get_editable(self, ctx: Context, message_id: str) -> Optional[dict]:
        giveaway = await self.resolve_giveaway(ctx, message_id)
        if giveaway is None:
            return None
        if giveaway.get("status") == "draft":
            await ctx.warn("That giveaway is still a setup panel — edit it from its own buttons.")
            return None
        return giveaway

    async def _edit_field(
        self, ctx: Context, message_id: str, field: str, value
    ) -> Message:
        giveaway = await self._get_editable(ctx, message_id)
        if giveaway is None:
            return
        if field not in {
            "prize", "winners", "required_age", "account_age", "required_role_id",
            "required_level", "embed_script", "max_entries", "host_id",
        }:
            return await ctx.warn("That field can't be edited with this command.")

        label = {
            "prize": "Prize",
            "winners": "Winners",
            "required_age": "Required stay",
            "account_age": "Required account age",
            "required_role_id": "Required role",
            "required_level": "Required level",
            "embed_script": "Embed",
            "max_entries": "Max entries",
            "host_id": "Host",
        }[field]

        if field == "embed_script" and value:
            preview = await self._build_custom_embed(
                {"id": giveaway["id"], "embed_script": value}, 0
            )
            if preview is None:
                return await ctx.warn("That embed script didn't produce anything.")

        column = "required_age" if field in ("required_age", "account_age") else field
        await self.store.update(giveaway["id"], **{column: value})
        await self._repaint(ctx, giveaway["id"])
        return await ctx.approve(f"Updated the giveaway **{label}**.")

    async def _edit_media(
        self, ctx: Context, message_id: str, kind: str, url: Optional[str]
    ) -> Message:
        giveaway = await self._get_editable(ctx, message_id)
        if giveaway is None:
            return
        script = giveaway.get("embed_script") or "{embed}"
        # Drop any previous image/thumbnail of the same kind, then append.
        script = re.sub(r"\{image:[^}]*\}", "", script)
        script = re.sub(r"\{thumbnail:[^}]*\}", "", script)
        if url:
            if "://" not in url:
                return await ctx.warn("Please provide a full image URL.")
            script = script.rstrip("$v ") + f"$v{{{kind}: {url}}}"
        await self.store.update(giveaway["id"], embed_script=script)
        await self._repaint(ctx, giveaway["id"])
        if url:
            return await ctx.approve(f"Updated the giveaway **{kind}**.")
        return await ctx.approve(f"Cleared the giveaway **{kind}**.")

    async def _repaint(self, ctx: Context, giveaway_id: int) -> None:
        giveaway = await self.store.get(giveaway_id)
        if giveaway is None:
            return
        await self.refresh_giveaway_message(giveaway)


class EditLauncher(View):
    """Fallback launcher used by ``,giveaway edit`` with no reference given."""

    def __init__(self, cog: Engagement) -> None:
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.cog = cog

    @button(label="Edit a giveaway", style=ButtonStyle.primary)
    async def edit_button(self, interaction: Interaction, _button: Button) -> None:
        await interaction.response.send_modal(EditPickModal(self.cog))
