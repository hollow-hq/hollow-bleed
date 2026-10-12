"""Moderation systems mixin for the Moderation cog.

This module provides :class:`ModSysMixin`, a plain mixin blended into
``cogs.moderation.moderation.Moderation``. It owns the case/warn/note
subsystems, temporary moderation actions, reminders, threads, slowmode,
channel utilities and every other moderation feature that did not already
exist in Hollow-Phi.

It deliberately does **not** create a second cog: all commands here are
registered on the existing ``Moderation`` cog so the command category,
permission helpers and response style stay identical, and no duplicate
command names, aliases or database workflows are introduced.

Conventions (matching the existing cog):
- ``has_permissions`` from ``base.managers.predicates`` for auth.
- ``ctx.approve / ctx.warn / ctx.deny / ctx.paginate`` for responses.
- ``self.bot.pool`` (asyncmy, ``%s`` placeholders) for persistence.
- Background expiry loops are restart-safe: every temporary action is a
  row in ``temp_actions`` / ``reminders`` and the loops act only on due
  rows with idempotent ``active = 0`` transitions.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union

import discord
from discord import Embed, Member, Role, TextChannel, Thread, User, VoiceChannel
from discord.ext import tasks
from discord.ext.commands import Cog

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.commands import hybrid_command, hybrid_group
from base.managers.predicates import has_permissions


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no Discord I/O)
# ---------------------------------------------------------------------------

_TIME_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "year": 31536000, "years": 31536000,
}

_MAX_DURATION = 365 * 24 * 3600
_MAX_TIMEOUT = 28 * 24 * 3600


def parse_duration(raw: str, maximum: int = _MAX_DURATION) -> int:
    """Parse a human duration into seconds.

    Accepts ``10m``, ``1h30m``, ``2 days``, ``30`` (bare seconds) and
    ``1:30`` / ``1:02:03`` clock notation. Raises ``ValueError`` with a
    user-facing message on bad input.
    """
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("Provide a duration, e.g. `10m`, `1h30m` or `2d`.")
    if "-" in text:
        raise ValueError("That duration is not valid. Try `10m` or `1:30`.")
    if ":" in text:
        parts = text.split(":")
        if not all(p.strip().isdigit() for p in parts) or len(parts) > 3:
            raise ValueError("That duration is not valid. Try `10m` or `1:30`.")
        numbers = [int(p) for p in parts]
        total = numbers[0] * 60 + numbers[1] if len(numbers) == 2 else (
            numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        )
        return _clamp(total, maximum)
    if text.isdigit():
        return _clamp(int(text), maximum)
    total_f = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text):
        key = unit.rstrip(".")
        if key not in _TIME_UNITS:
            raise ValueError(f"`{unit}` is not a unit I know. Try `s`, `m`, `h`, `d`, `w`.")
        total_f += float(value) * _TIME_UNITS[key]
        matched = True
    if not matched:
        raise ValueError("That duration is not valid. Try `10m`, `1h30m` or `2d`.")
    return _clamp(int(total_f), maximum)


def _clamp(seconds: int, maximum: int) -> int:
    if seconds < 1:
        raise ValueError("The duration must be at least **1 second**.")
    if seconds > maximum:
        raise ValueError("That duration is too long for this action.")
    return seconds


def humanize_seconds(seconds: Optional[int]) -> str:
    if seconds is None:
        return "N/A"
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    chunks: List[str] = []
    for suffix, size in (("d", 86400), ("h", 3600), ("m", 60)):
        value, total = divmod(total, size)
        if value:
            chunks.append(f"{value}{suffix}")
    return " ".join(chunks[:2]) or "0s"


def split_attachments(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def join_attachments(items: List[str]) -> str:
    seen: List[str] = []
    for item in items:
        item = (item or "").strip()
        if item and item not in seen:
            seen.append(item)
    return ",".join(seen)


class ModSysMixin:
    """Mixin blended into ``Moderation``. Never instantiated on its own."""

    # -- runtime state (per-cog instance) ----------------------------------
    _unbanall_tasks: dict = {}
    _loops_started: bool = False

    # ------------------------------------------------------------------
    # Internal helpers (DB + Discord)
    # ------------------------------------------------------------------

    async def _mod_next_case_id(self, guild_id: int) -> int:
        current = await self.bot.pool.fetchval(
            "SELECT COALESCE(MAX(case_id), 0) FROM mod_cases WHERE guild_id = %s",
            guild_id,
        )
        return int(current or 0) + 1

    async def _mod_log_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
        expires_at: Optional[int] = None,
    ) -> int:
        case_id = await self._mod_next_case_id(guild_id)
        await self.bot.pool.execute(
            "INSERT INTO mod_cases "
            "(guild_id, case_id, user_id, moderator_id, action, reason, created_at, expires_at, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)",
            guild_id, case_id, user_id, moderator_id, action, reason,
            int(time.time()), expires_at,
        )
        return case_id

    def _mod_hierarchy_error(self, ctx: Context, target: Union[Member, User]) -> Optional[str]:
        try:
            guild = ctx.guild
            if guild is None:
                return "This command can only be used in a server."
            # Duck-typed so unit tests (and future converters) work without
            # requiring an exact discord.Member instance; User-like targets
            # without roles are not hierarchy-checked.
            if not hasattr(target, "top_role"):
                return None
            if isinstance(target, Member) or hasattr(target, "top_role"):
                if target.id == guild.owner_id:  # type: ignore
                    return "You cannot moderate the **server owner**."
                if target.id == ctx.author.id:
                    return "You cannot moderate **yourself**."
                if target.id == self.bot.user.id:  # type: ignore
                    return "I cannot moderate **myself**."
                if ctx.author.id != guild.owner_id:
                    if target.top_role.position >= guild.me.top_role.position:  # type: ignore
                        return "That member is above me in the role hierarchy."
                    if target.top_role.position >= ctx.author.top_role.position:  # type: ignore
                        return "That member has a **higher or equal role** than you."
            return None
        except Exception:
            return "Hierarchy check failed."

    async def _mod_case_embed(self, ctx: Context, row: dict) -> Embed:
        user = self.bot.get_user(row["user_id"]) or ctx.guild.get_member(row["user_id"])  # type: ignore
        mod = self.bot.get_user(row["moderator_id"])
        embed = Embed(color=COLORS.neutral, title=f"Case #{row['case_id']} • {row['action']}")
        embed.add_field(name="Member", value=f"{user.mention if user else row['user_id']} (`{row['user_id']}`)", inline=True)
        embed.add_field(name="Moderator", value=f"{mod.mention if mod else row['moderator_id']} (`{row['moderator_id']}`)", inline=True)
        embed.add_field(name="Reason", value=(row["reason"] or "No reason provided.")[:1000], inline=False)
        if row.get("proof"):
            embed.add_field(name="Proof", value=str(row["proof"])[:1000], inline=False)
        attachments = split_attachments(row.get("attachments"))
        if attachments:
            embed.add_field(name="Attachments", value="\n".join(attachments[:5]), inline=False)
        embed.set_footer(text=f"Created <t:{int(row['created_at'])}:R>")
        return embed

    async def _mod_list_entries(self, ctx: Context, rows: List[dict]) -> List[str]:
        entries = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"]) if ctx.guild else None  # type: ignore
            label = member.mention if member else f"`{row['user_id']}`"
            entries.append(
                f"`#{row['case_id']}` **{row['action']}** {label} — {(row['reason'] or 'No reason')[:80]}"
            )
        return entries

    async def _send_paginated(self, ctx: Context, title: str, entries: List[str], empty: str):
        if not entries:
            return await ctx.warn(empty)
        embeds: List[Embed] = []
        embed = Embed(color=COLORS.neutral, title=title, description="")
        count = 0
        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1
            if count == 10:
                embeds.append(embed)
                embed = Embed(color=COLORS.neutral, title=title, description="")
                count = 0
        if count > 0:
            embeds.append(embed)
        if len(embeds) > 1:
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0])

    async def _ensure_temp_table(self) -> None:
        # Defensive: the schema file creates these, but an old database may
        # predate the migration. Best-effort creation keeps commands working.
        statements = [
            """CREATE TABLE IF NOT EXISTS mod_cases (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, guild_id BIGINT NOT NULL,
                case_id INT NOT NULL, user_id BIGINT NOT NULL, moderator_id BIGINT NOT NULL,
                action VARCHAR(32) NOT NULL, reason TEXT NULL, proof TEXT NULL,
                attachments TEXT NULL, created_at BIGINT NOT NULL, expires_at BIGINT NULL,
                active TINYINT(1) NOT NULL DEFAULT 1,
                UNIQUE KEY uniq_mod_case (guild_id, case_id))""",
            """CREATE TABLE IF NOT EXISTS mod_notes (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, moderator_id BIGINT NOT NULL,
                note TEXT NOT NULL, created_at BIGINT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS reminders (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, channel_id BIGINT NULL, content TEXT NOT NULL,
                remind_at BIGINT NOT NULL, created_at BIGINT NOT NULL,
                done TINYINT(1) NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS temp_actions (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL, action VARCHAR(16) NOT NULL,
                role_id BIGINT NULL, data TEXT NULL, expires_at BIGINT NOT NULL,
                active TINYINT(1) NOT NULL DEFAULT 1, created_at BIGINT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS jail_config (
                guild_id BIGINT PRIMARY KEY, role_id BIGINT NULL)""",
            """CREATE TABLE IF NOT EXISTS jail_state (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
                role_ids TEXT NULL, expires_at BIGINT NULL,
                PRIMARY KEY (guild_id, user_id))""",
            """CREATE TABLE IF NOT EXISTS sticky_roles (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, role_id BIGINT NOT NULL,
                PRIMARY KEY (guild_id, user_id, role_id))""",
            """CREATE TABLE IF NOT EXISTS hardbans (
                guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, reason TEXT NULL,
                moderator_id BIGINT NULL, created_at BIGINT NOT NULL,
                PRIMARY KEY (guild_id, user_id))""",
            """CREATE TABLE IF NOT EXISTS restrict_commands (
                guild_id BIGINT NOT NULL, command VARCHAR(64) NOT NULL, role_id BIGINT NOT NULL,
                PRIMARY KEY (guild_id, command, role_id))""",
            """CREATE TABLE IF NOT EXISTS mute_config (
                guild_id BIGINT PRIMARY KEY, role_id BIGINT NULL)""",
        ]
        for statement in statements:
            try:
                await self.bot.pool.execute(statement)
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Lifecycle: restart-safe expiry loops
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:  # type: ignore
        await self._ensure_temp_table()
        # Register the global command-restriction check once.
        try:
            self.bot.add_check(self._restrict_global_check)  # type: ignore
        except Exception:
            pass
        if not self._loops_started:
            self._mod_expiry_loop.start()
            self._reminder_loop.start()
            self._loops_started = True

    def cog_unload(self) -> None:  # type: ignore
        try:
            self._mod_expiry_loop.cancel()
        except Exception:
            pass
        try:
            self._reminder_loop.cancel()
        except Exception:
            pass

    async def _restrict_global_check(self, ctx) -> bool:  # type: ignore
        # Global precondition: if the invoked command is restricted in this
        # guild, the author must hold one of the allowed roles (or own the
        # guild / bot). Restrictions only ever ADD a requirement; they never
        # bypass the command's own permission checks.
        try:
            if ctx.guild is None or ctx.command is None:
                return True
            rows = await self.bot.pool.fetch(
                "SELECT role_id FROM restrict_commands WHERE guild_id = %s AND command = %s",
                ctx.guild.id, ctx.command.qualified_name,
            )
            if not rows:
                return True
            if ctx.author.id in (getattr(self.bot, "owner_ids", set()) or set()):
                return True
            if ctx.author.id == ctx.guild.owner_id:
                return True
            allowed = {int(r["role_id"]) for r in rows}
            held = {r.id for r in getattr(ctx.author, "roles", [])}
            return bool(allowed & held)
        except Exception:
            return True

    @tasks.loop(seconds=30)
    async def _mod_expiry_loop(self):
        try:
            now = int(time.time())
            rows = await self.bot.pool.fetch(
                "SELECT * FROM temp_actions WHERE active = 1 AND expires_at <= %s LIMIT 50",
                now,
            )
        except Exception:
            return
        for row in rows:
            try:
                claimed = False
                try:
                    # Claim the row so two loops / restarts cannot double-apply.
                    await self.bot.pool.execute(
                        "UPDATE temp_actions SET active = 0 WHERE id = %s AND active = 1",
                        row["id"],
                    )
                    claimed = True
                except Exception:
                    continue
                if not claimed:
                    continue
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                action = row["action"]
                if action == "tempban":
                    try:
                        user = discord.Object(id=row["user_id"])
                        await guild.unban(user, reason="Temporary ban expired")
                    except Exception:
                        pass
                    try:
                        await self.bot.pool.execute(
                            "UPDATE mod_cases SET active = 0 WHERE guild_id = %s AND user_id = %s AND action = 'tempban' AND active = 1",
                            row["guild_id"], row["user_id"],
                        )
                    except Exception:
                        pass
                elif action == "jail":
                    member = guild.get_member(row["user_id"])
                    state = None
                    try:
                        state = await self.bot.pool.fetchrow(
                            "SELECT role_ids FROM jail_state WHERE guild_id = %s AND user_id = %s",
                            row["guild_id"], row["user_id"],
                        )
                    except Exception:
                        state = None
                    if member is not None:
                        try:
                            jail_role_id = await self.bot.pool.fetchval(
                                "SELECT role_id FROM jail_config WHERE guild_id = %s",
                                row["guild_id"],
                            )
                            jail_role = guild.get_role(jail_role_id) if jail_role_id else None
                            if jail_role is not None and jail_role in member.roles:
                                await member.remove_roles(jail_role, reason="Jail expired")
                            restore_ids = []
                            if state and state.get("role_ids"):
                                try:
                                    restore_ids = [int(x) for x in json.loads(state["role_ids"])]
                                except Exception:
                                    restore_ids = []
                            for rid in restore_ids:
                                role = guild.get_role(rid)
                                if role is not None and role not in member.roles:
                                    try:
                                        if role.position < guild.me.top_role.position:
                                            await member.add_roles(role, reason="Jail expired")
                                    except Exception:
                                        continue
                        except Exception:
                            pass
                    try:
                        await self.bot.pool.execute(
                            "DELETE FROM jail_state WHERE guild_id = %s AND user_id = %s",
                            row["guild_id"], row["user_id"],
                        )
                    except Exception:
                        pass
                elif action == "temprole":
                    member = guild.get_member(row["user_id"])
                    role = guild.get_role(row["role_id"]) if row.get("role_id") else None
                    if member is not None and role is not None and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Temporary role expired")
                        except Exception:
                            pass
            except Exception:
                continue

    @_mod_expiry_loop.before_loop
    async def _mod_expiry_before(self):
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

    @tasks.loop(seconds=20)
    async def _reminder_loop(self):
        try:
            now = int(time.time())
            rows = await self.bot.pool.fetch(
                "SELECT * FROM reminders WHERE done = 0 AND remind_at <= %s LIMIT 20",
                now,
            )
        except Exception:
            return
        for row in rows:
            try:
                await self.bot.pool.execute(
                    "UPDATE reminders SET done = 1 WHERE id = %s AND done = 0", row["id"]
                )
            except Exception:
                continue
            try:
                user = self.bot.get_user(row["user_id"]) or await self.bot.fetch_user(row["user_id"])
            except Exception:
                continue
            text = f"Reminder: {row['content']}"
            sent = False
            if row.get("channel_id"):
                channel = self.bot.get_channel(row["channel_id"])
                if channel is not None:
                    try:
                        await channel.send(f"{user.mention} {text}")  # type: ignore
                        sent = True
                    except Exception:
                        sent = False
            if not sent:
                try:
                    await user.send(text)  # type: ignore
                except Exception:
                    continue

    @_reminder_loop.before_loop
    async def _reminder_before(self):
        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

    @Cog.listener()
    async def on_member_join(self, member: Member):
        # Sticky roles + hardban re-enforcement. Best effort; never blocks join.
        try:
            rows = await self.bot.pool.fetch(
                "SELECT role_id FROM sticky_roles WHERE guild_id = %s AND user_id = %s",
                member.guild.id, member.id,
            )
            for row in rows or []:
                role = member.guild.get_role(row["role_id"])
                if role is None or role in member.roles:
                    continue
                try:
                    if role.position < member.guild.me.top_role.position:
                        await member.add_roles(role, reason="Sticky role reapplied")
                except Exception:
                    continue
        except Exception:
            pass
        try:
            hard = await self.bot.pool.fetchrow(
                "SELECT reason FROM hardbans WHERE guild_id = %s AND user_id = %s",
                member.guild.id, member.id,
            )
            if hard:
                try:
                    await member.ban(reason=f"Hardban re-enforced: {hard.get('reason') or 'No reason'}")
                except Exception:
                    pass
        except Exception:
            pass

    @Cog.listener()
    async def on_member_remove(self, member: Member):
        # Leave backup used by `role restore`. Overwrites previous backup.
        try:
            role_ids = [r.id for r in member.roles if not r.is_default()]
            if not role_ids:
                return
            await self.bot.pool.execute(
                "INSERT INTO temp_actions (guild_id, user_id, action, role_id, data, expires_at, active, created_at) "
                "VALUES (%s, %s, 'rolebackup', NULL, %s, %s, 0, %s)",
                member.guild.id, member.id, json.dumps(role_ids),
                int(time.time()) + 90 * 86400, int(time.time()),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Warnings
    # ------------------------------------------------------------------

    @hybrid_command(name="warn", description="Warn a member.", example="warn @voby spamming")
    @has_permissions(manage_messages=True)
    async def warn(self, ctx: Context, member: Member, *, reason: str = "No reason provided."):
        err = self._mod_hierarchy_error(ctx, member)
        if err:
            return await ctx.warn(err)
        case_id = await self._mod_log_case(
            ctx.guild.id, member.id, ctx.author.id, "warn", reason  # type: ignore
        )
        try:
            await self.bot.pool.execute(
                "INSERT INTO warnings (guild_id, user_id, warns) VALUES (%s, %s, 1) "
                "ON DUPLICATE KEY UPDATE warns = warns + 1",
                ctx.guild.id, member.id,  # type: ignore
            )
        except Exception:
            pass
        try:
            await member.send(f"You were warned in **{ctx.guild.name}** for: {reason}")  # type: ignore
        except Exception:
            pass
        return await ctx.approve(f"{member.mention} warned (case `#{case_id}`).")

    @hybrid_command(name="warnings", aliases=["warns"], description="View warnings for a member.", example="warnings @voby")
    @has_permissions(manage_messages=True)
    async def warnings(self, ctx: Context, member: Member = None):  # type: ignore
        target = member or ctx.author
        rows = await self.bot.pool.fetch(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND user_id = %s AND action = 'warn' AND active = 1 ORDER BY case_id DESC LIMIT 50",
            ctx.guild.id, target.id,  # type: ignore
        )
        entries = await self._mod_list_entries(ctx, rows or [])
        await self._send_paginated(ctx, f"Warnings for {target}", entries, f"No warnings for {target.mention}.")

    # ------------------------------------------------------------------
    # History / cases / reasons / stats
    # ------------------------------------------------------------------

    @hybrid_group(name="history", invoke_without_command=True, description="View moderation history.", example="history @voby")
    @has_permissions(manage_messages=True)
    async def history(self, ctx: Context, member: Member = None, *, action: str = None):  # type: ignore
        if member is None:
            return await ctx.send_help(ctx.command)
        if action:
            rows = await self.bot.pool.fetch(
                "SELECT * FROM mod_cases WHERE guild_id = %s AND user_id = %s AND action = %s ORDER BY case_id DESC LIMIT 50",
                ctx.guild.id, member.id, action.lower(),  # type: ignore
            )
        else:
            rows = await self.bot.pool.fetch(
                "SELECT * FROM mod_cases WHERE guild_id = %s AND user_id = %s ORDER BY case_id DESC LIMIT 50",
                ctx.guild.id, member.id,  # type: ignore
            )
        entries = await self._mod_list_entries(ctx, rows or [])
        await self._send_paginated(ctx, f"History for {member}", entries, f"No history for {member.mention}.")

    @history.command(name="view", description="View a single case.", example="history view 3")
    @has_permissions(manage_messages=True)
    async def history_view(self, ctx: Context, case_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        return await ctx.send(embed=await self._mod_case_embed(ctx, row))

    @history.command(name="remove", aliases=["delete", "del"], description="Remove a single case.", example="history remove @voby 3")
    @has_permissions(manage_messages=True)
    async def history_remove(self, ctx: Context, member: Member, case_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s AND user_id = %s",
            ctx.guild.id, case_id, member.id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found for {member.mention}.")
        await self.bot.pool.execute(
            "DELETE FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if row.get("action") == "warn":
            try:
                await self.bot.pool.execute(
                    "UPDATE warnings SET warns = GREATEST(warns - 1, 0) WHERE guild_id = %s AND user_id = %s",
                    ctx.guild.id, member.id,  # type: ignore
                )
            except Exception:
                pass
        return await ctx.approve(f"Removed case `#{case_id}` for {member.mention}.")

    @history.command(name="removeall", aliases=["deleteall", "delall"], description="Remove all cases for a member.", example="history removeall @voby")
    @has_permissions(administrator=True)
    async def history_removeall(self, ctx: Context, member: Member):
        await self.bot.pool.execute(
            "DELETE FROM mod_cases WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id, member.id,  # type: ignore
        )
        try:
            await self.bot.pool.execute(
                "DELETE FROM warnings WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id, member.id,  # type: ignore
            )
        except Exception:
            pass
        return await ctx.approve(f"Cleared all history for {member.mention}.")

    @hybrid_command(name="case", aliases=["caselog"], description="View a case log.", example="case 3")
    @has_permissions(manage_messages=True)
    async def case(self, ctx: Context, case_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        return await ctx.send(embed=await self._mod_case_embed(ctx, row))

    @hybrid_command(name="reason", description="Update the reason on a case.", example="reason 3 spamming")
    @has_permissions(manage_messages=True)
    async def reason(self, ctx: Context, case_id: int, *, reason: str):
        row = await self.bot.pool.fetchrow(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        await self.bot.pool.execute(
            "UPDATE mod_cases SET reason = %s WHERE guild_id = %s AND case_id = %s",
            reason, ctx.guild.id, case_id,  # type: ignore
        )
        return await ctx.approve(f"Updated reason for case `#{case_id}`.")

    @hybrid_command(name="modstats", description="View punishment stats for a moderator.", example="modstats @voby")
    @has_permissions(manage_messages=True)
    async def modstats(self, ctx: Context, member: Member = None):  # type: ignore
        target = member or ctx.author
        rows = await self.bot.pool.fetch(
            "SELECT action, COUNT(*) AS total FROM mod_cases WHERE guild_id = %s AND moderator_id = %s GROUP BY action",
            ctx.guild.id, target.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn(f"No moderation stats for {target.mention}.")
        lines = [f"**{r['action']}**: {r['total']}" for r in rows]
        total = sum(int(r["total"]) for r in rows)
        embed = Embed(color=COLORS.neutral, title=f"Mod stats for {target}", description="\n".join(lines))
        embed.set_footer(text=f"Total actions: {total}")
        return await ctx.send(embed=embed)

    @hybrid_command(name="modhistory", aliases=["moderationhistory", "mhistory"], description="View actions performed by a moderator.", example="modhistory @voby")
    @has_permissions(manage_messages=True)
    async def modhistory(self, ctx: Context, member: Member = None, *, action: str = None):  # type: ignore
        target = member or ctx.author
        if action:
            rows = await self.bot.pool.fetch(
                "SELECT * FROM mod_cases WHERE guild_id = %s AND moderator_id = %s AND action = %s ORDER BY case_id DESC LIMIT 50",
                ctx.guild.id, target.id, action.lower(),  # type: ignore
            )
        else:
            rows = await self.bot.pool.fetch(
                "SELECT * FROM mod_cases WHERE guild_id = %s AND moderator_id = %s ORDER BY case_id DESC LIMIT 50",
                ctx.guild.id, target.id,  # type: ignore
            )
        entries = await self._mod_list_entries(ctx, rows or [])
        await self._send_paginated(ctx, f"Actions by {target}", entries, f"No recorded actions by {target.mention}.")

    # ------------------------------------------------------------------
    # Proof / evidence
    # ------------------------------------------------------------------

    @hybrid_group(name="proof", aliases=["evidence"], invoke_without_command=True, description="Manage case proof.", example="proof view 3")
    @has_permissions(manage_messages=True)
    async def proof(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @proof.command(name="view", description="View proof for a case.", example="proof view 3")
    @has_permissions(manage_messages=True)
    async def proof_view(self, ctx: Context, case_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        attachments = split_attachments(row.get("attachments"))
        desc = (row.get("proof") or "No proof recorded.")[:1500]
        if attachments:
            desc += "\n\n**Attachments:**\n" + "\n".join(attachments[:5])
        return await ctx.send(embed=Embed(color=COLORS.neutral, title=f"Proof for case #{case_id}", description=desc))

    @proof.command(name="list", description="List attachments for a case.", example="proof list 3")
    @has_permissions(manage_messages=True)
    async def proof_list(self, ctx: Context, case_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT attachments FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        attachments = split_attachments(row.get("attachments"))
        if not attachments:
            return await ctx.warn(f"No attachments for case `#{case_id}`.")
        await self._send_paginated(ctx, f"Attachments for case #{case_id}", [f"`{i}` {u}" for i, u in enumerate(attachments, 1)], "No attachments.")

    @proof.command(name="set", description="Set proof text for a case.", example="proof set 3 rule violation")
    @has_permissions(manage_messages=True)
    async def proof_set(self, ctx: Context, case_id: int, *, explanation: str):
        row = await self.bot.pool.fetchrow(
            "SELECT id FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        extra = [a.url for a in ctx.message.attachments]
        current = await self.bot.pool.fetchval(
            "SELECT attachments FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        merged = join_attachments(split_attachments(current) + extra)
        await self.bot.pool.execute(
            "UPDATE mod_cases SET proof = %s, attachments = %s WHERE guild_id = %s AND case_id = %s",
            explanation, merged or None, ctx.guild.id, case_id,  # type: ignore
        )
        return await ctx.approve(f"Set proof for case `#{case_id}`.")

    @proof.command(name="add", description="Add attachment(s) to a case.", example="proof add 3 https://...")
    @has_permissions(manage_messages=True)
    async def proof_add(self, ctx: Context, case_id: int, *, media_url: str = None):  # type: ignore
        row = await self.bot.pool.fetchrow(
            "SELECT attachments FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        urls = [a.url for a in ctx.message.attachments]
        if media_url:
            urls.extend([u.strip() for u in media_url.split() if u.strip().startswith("http")])
        if not urls:
            return await ctx.warn("Provide an image URL or attach a file.")
        merged = join_attachments(split_attachments(row.get("attachments")) + urls)
        await self.bot.pool.execute(
            "UPDATE mod_cases SET attachments = %s WHERE guild_id = %s AND case_id = %s",
            merged, ctx.guild.id, case_id,  # type: ignore
        )
        return await ctx.approve(f"Added **{len(urls)}** attachment(s) to case `#{case_id}`.")

    @proof.command(name="remove", description="Remove an attachment from a case.", example="proof remove 3 1")
    @has_permissions(manage_messages=True)
    async def proof_remove(self, ctx: Context, case_id: int, index: int):
        row = await self.bot.pool.fetchrow(
            "SELECT attachments FROM mod_cases WHERE guild_id = %s AND case_id = %s",
            ctx.guild.id, case_id,  # type: ignore
        )
        if not row:
            return await ctx.warn(f"Case `#{case_id}` not found.")
        attachments = split_attachments(row.get("attachments"))
        if not attachments or index < 1 or index > len(attachments):
            return await ctx.warn("Invalid attachment index.")
        removed = attachments.pop(index - 1)
        await self.bot.pool.execute(
            "UPDATE mod_cases SET attachments = %s WHERE guild_id = %s AND case_id = %s",
            join_attachments(attachments) or None, ctx.guild.id, case_id,  # type: ignore
        )
        return await ctx.approve(f"Removed attachment `{removed[:60]}` from case `#{case_id}`.")

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    @hybrid_group(name="notes", aliases=["note"], invoke_without_command=True, description="View staff notes on a member.", example="notes @voby")
    @has_permissions(manage_messages=True)
    async def notes(self, ctx: Context, member: Member = None):  # type: ignore
        if member is None:
            return await ctx.send_help(ctx.command)
        rows = await self.bot.pool.fetch(
            "SELECT * FROM mod_notes WHERE guild_id = %s AND user_id = %s ORDER BY id DESC LIMIT 50",
            ctx.guild.id, member.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn(f"No notes for {member.mention}.")
        entries = [f"`{r['id']}` <t:{int(r['created_at'])}:R> — {(r['note'] or '')[:120]}" for r in rows]
        await self._send_paginated(ctx, f"Notes for {member}", entries, "No notes.")

    @notes.command(name="add", description="Add a staff note.", example="notes add @voby helpful member")
    @has_permissions(manage_messages=True)
    async def notes_add(self, ctx: Context, member: Member, *, note: str):
        note = (note or "").strip()
        if not note or len(note) > 1000:
            return await ctx.warn("Notes must be 1–1000 characters.")
        await self.bot.pool.execute(
            "INSERT INTO mod_notes (guild_id, user_id, moderator_id, note, created_at) VALUES (%s, %s, %s, %s, %s)",
            ctx.guild.id, member.id, ctx.author.id, note, int(time.time()),  # type: ignore
        )
        return await ctx.approve(f"Added a note for {member.mention}.")

    @notes.command(name="remove", aliases=["delete", "del"], description="Remove a staff note.", example="notes remove @voby 12")
    @has_permissions(manage_messages=True)
    async def notes_remove(self, ctx: Context, member: Member, note_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT id FROM mod_notes WHERE guild_id = %s AND user_id = %s AND id = %s",
            ctx.guild.id, member.id, note_id,  # type: ignore
        )
        if not row:
            return await ctx.warn("Note not found for that member.")
        await self.bot.pool.execute("DELETE FROM mod_notes WHERE id = %s", note_id)
        return await ctx.approve(f"Removed note `{note_id}` for {member.mention}.")

    @notes.command(name="clear", aliases=["cl"], description="Clear all notes for a member.", example="notes clear @voby")
    @has_permissions(manage_messages=True)
    async def notes_clear(self, ctx: Context, member: Member):
        await self.bot.pool.execute(
            "DELETE FROM mod_notes WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id, member.id,  # type: ignore
        )
        return await ctx.approve(f"Cleared all notes for {member.mention}.")

    # ------------------------------------------------------------------
    # Temporary bans / softban / hardban / recent / unbanall
    # ------------------------------------------------------------------

    @hybrid_command(name="tempban", description="Temporarily ban a member.", example="tempban @voby 1d spamming")
    @has_permissions(ban_members=True)
    async def tempban(self, ctx: Context, member: Union[Member, User], duration: str, *, reason: str = "No reason provided."):
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        if isinstance(member, Member):
            err = self._mod_hierarchy_error(ctx, member)
            if err:
                return await ctx.warn(err)
        full_reason = f"{reason} | Tempbanned by {ctx.author} for {humanize_seconds(seconds)}"
        try:
            await ctx.guild.ban(member, reason=full_reason, delete_message_days=1)  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot ban: `{exc}`")
        expires_at = int(time.time()) + seconds
        case_id = await self._mod_log_case(
            ctx.guild.id, member.id, ctx.author.id, "tempban", reason, expires_at  # type: ignore
        )
        await self.bot.pool.execute(
            "INSERT INTO temp_actions (guild_id, user_id, action, role_id, data, expires_at, active, created_at) "
            "VALUES (%s, %s, 'tempban', NULL, %s, %s, 1, %s)",
            ctx.guild.id, member.id, f"case:{case_id}", expires_at, int(time.time()),  # type: ignore
        )
        return await ctx.approve(f"{member.mention} tempbanned for **{humanize_seconds(seconds)}** (case `#{case_id}`).")

    @hybrid_command(name="softban", description="Ban and immediately unban (clears messages).", example="softban @voby spamming")
    @has_permissions(ban_members=True)
    async def softban(self, ctx: Context, member: Member, *, reason: str = "No reason provided."):
        err = self._mod_hierarchy_error(ctx, member)
        if err:
            return await ctx.warn(err)
        full_reason = f"{reason} | Softbanned by {ctx.author}"
        try:
            await ctx.guild.ban(member, reason=full_reason, delete_message_days=1)  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot softban: `{exc}`")
        try:
            await ctx.guild.unban(member, reason=f"Softban release by {ctx.author}")  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Banned, but unban failed: `{exc}`")
        await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, "softban", reason)  # type: ignore
        return await ctx.approve(f"{member.mention} softbanned (messages cleared).")

    @hybrid_group(name="hardban", aliases=["hb"], invoke_without_command=True, description="Keep a member banned (reban on join).", example="hardban @voby raiding")
    @has_permissions(administrator=True)
    async def hardban(self, ctx: Context, member: Union[Member, User], *, reason: str = "No reason provided."):
        full_reason = f"{reason} | Hardbanned by {ctx.author}"
        try:
            await ctx.guild.ban(member, reason=full_reason, delete_message_days=1)  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot hardban: `{exc}`")
        await self.bot.pool.execute(
            "INSERT INTO hardbans (guild_id, user_id, reason, moderator_id, created_at) VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE reason = VALUES(reason), moderator_id = VALUES(moderator_id), created_at = VALUES(created_at)",
            ctx.guild.id, member.id, reason, ctx.author.id, int(time.time()),  # type: ignore
        )
        await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, "hardban", reason)  # type: ignore
        return await ctx.approve(f"{member.mention} hardbanned.")

    @hardban.command(name="list", description="List hardbanned members.", example="hardban list")
    @has_permissions(administrator=True)
    async def hardban_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT user_id, reason FROM hardbans WHERE guild_id = %s ORDER BY created_at DESC LIMIT 100",
            ctx.guild.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("No hardbanned members.")
        entries = [f"`{r['user_id']}` — {(r['reason'] or 'No reason')[:100]}" for r in rows]
        await self._send_paginated(ctx, "Hardbanned members", entries, "No hardbanned members.")

    @hardban.command(name="remove", description="Remove a hardban (does not unban).", example="hardban remove 123")
    @has_permissions(administrator=True)
    async def hardban_remove(self, ctx: Context, user_id: int):
        exists = await self.bot.pool.fetchval(
            "SELECT user_id FROM hardbans WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id, user_id,  # type: ignore
        )
        if not exists:
            return await ctx.warn("That user is not hardbanned.")
        await self.bot.pool.execute(
            "DELETE FROM hardbans WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id, user_id,  # type: ignore
        )
        return await ctx.approve(f"Removed hardban for `{user_id}` (use `unban` to allow rejoin).")

    @hybrid_command(name="recentban", aliases=["chunkban"], description="Ban recently joined members.", example="recentban 5 raiding")
    @has_permissions(ban_members=True)
    async def recentban(self, ctx: Context, count: int, *, reason: str = "No reason provided."):
        count = max(1, min(int(count or 1), 50))
        members = sorted(
            [m for m in ctx.guild.members if m.joined_at is not None],  # type: ignore
            key=lambda m: m.joined_at,  # type: ignore
            reverse=True,
        )[:count]
        banned = 0
        for member in members:
            if self._mod_hierarchy_error(ctx, member):
                continue
            try:
                await member.ban(reason=f"{reason} | Recent-ban by {ctx.author}", delete_message_days=1)
                await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, "ban", reason)  # type: ignore
                banned += 1
            except Exception:
                continue
            await asyncio.sleep(0.5)
        if not banned:
            return await ctx.warn("No members could be banned (hierarchy or permissions).")
        return await ctx.approve(f"Banned **{banned}** recently joined member(s).")

    @hybrid_group(name="unbanall", aliases=["massunban"], invoke_without_command=True, description="Unban every banned member.", example="unbanall")
    @has_permissions(administrator=True)
    async def unbanall(self, ctx: Context):
        if ctx.author.id != ctx.guild.owner_id and ctx.author.id not in (getattr(self.bot, "owner_ids", set()) or set()):  # type: ignore
            return await ctx.deny("Only the **server owner** can unban everyone.")
        try:
            bans = [entry async for entry in ctx.guild.bans(limit=1000)]  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot list bans: `{exc}`")
        if not bans:
            return await ctx.warn("No banned users.")
        key = ctx.guild.id  # type: ignore
        if key in self._unbanall_tasks and not self._unbanall_tasks[key].done():
            return await ctx.warn("An unban-all task is already running.")

        async def _run():
            done = 0
            for entry in bans:
                try:
                    await ctx.guild.unban(entry.user, reason=f"Unban-all by {ctx.author}")  # type: ignore
                    done += 1
                except Exception:
                    continue
                await asyncio.sleep(0.5)
            return done

        task = asyncio.create_task(_run())
        self._unbanall_tasks[key] = task
        progress = await ctx.cooldown(f"Unbanning **{len(bans)}** user(s)... Use `unbanall cancel` to stop.")
        try:
            done = await task
        except asyncio.CancelledError:
            try:
                await progress.delete()
            except Exception:
                pass
            return await ctx.warn("Unban-all **cancelled**.")
        finally:
            self._unbanall_tasks.pop(key, None)
        try:
            await progress.delete()
        except Exception:
            pass
        return await ctx.approve(f"Unbanned **{done}/{len(bans)}** user(s).")

    @unbanall.command(name="cancel", aliases=["kill"], description="Cancel a running unban-all.", example="unbanall cancel")
    @has_permissions(administrator=True)
    async def unbanall_cancel(self, ctx: Context):
        key = ctx.guild.id  # type: ignore
        task = self._unbanall_tasks.get(key)
        if not task or task.done():
            return await ctx.warn("No unban-all task is running.")
        task.cancel()
        return await ctx.approve("Cancelling unban-all...")

    # ------------------------------------------------------------------
    # Timeout
    # ------------------------------------------------------------------

    @hybrid_group(name="timeout", invoke_without_command=True, description="Timeout a member.", example="timeout @voby 10m spamming")
    @has_permissions(moderate_members=True)
    async def timeout(self, ctx: Context, member: Member = None, duration: str = None, *, reason: str = "No reason provided."):  # type: ignore
        if member is None or duration is None:
            return await ctx.send_help(ctx.command)
        try:
            seconds = parse_duration(duration, _MAX_TIMEOUT)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        err = self._mod_hierarchy_error(ctx, member)
        if err:
            return await ctx.warn(err)
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=f"{reason} | By {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot timeout: `{exc}`")
        await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, "timeout", reason)  # type: ignore
        return await ctx.approve(f"{member.mention} timed out for **{humanize_seconds(seconds)}**.")

    @timeout.command(name="list", description="List timed-out members.", example="timeout list")
    @has_permissions(moderate_members=True)
    async def timeout_list(self, ctx: Context):
        now = discord.utils.utcnow()
        timed = [m for m in ctx.guild.members if getattr(m, "timed_out_until", None) and m.timed_out_until > now]  # type: ignore
        if not timed:
            return await ctx.warn("No timed-out members.")
        entries = [f"{m.mention} until <t:{int(m.timed_out_until.timestamp())}:R>" for m in timed]  # type: ignore
        await self._send_paginated(ctx, "Timed-out members", entries, "No timed-out members.")

    @hybrid_command(name="untimeout", description="Remove a timeout.", example="untimeout @voby")
    @has_permissions(moderate_members=True)
    async def untimeout(self, ctx: Context, member: Member, *, reason: str = "No reason provided."):
        try:
            await member.timeout(None, reason=f"{reason} | By {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot remove timeout: `{exc}`")
        return await ctx.approve(f"{member.mention} timeout removed.")

    # ------------------------------------------------------------------
    # Jail
    # ------------------------------------------------------------------

    async def _jail_role(self, guild: discord.Guild) -> Optional[Role]:
        role_id = None
        try:
            role_id = await self.bot.pool.fetchval(
                "SELECT role_id FROM jail_config WHERE guild_id = %s", guild.id
            )
        except Exception:
            role_id = None
        role = guild.get_role(role_id) if role_id else None
        if role is not None:
            return role
        role = discord.utils.get(guild.roles, name="Jailed")
        if role is None:
            try:
                role = await guild.create_role(name="Jailed", reason="Jail system setup")
            except Exception:
                return None
            for channel in guild.channels:
                try:
                    await channel.set_permissions(role, send_messages=False, speak=False, connect=False, add_reactions=False)
                except Exception:
                    continue
        try:
            await self.bot.pool.execute(
                "INSERT INTO jail_config (guild_id, role_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE role_id = VALUES(role_id)",
                guild.id, role.id,
            )
        except Exception:
            pass
        return role

    @hybrid_command(name="jail", description="Jail a member.", example="jail @voby 1h raiding")
    @has_permissions(manage_messages=True)
    async def jail(self, ctx: Context, member: Member, duration: str = None, *, reason: str = "No reason provided."):  # type: ignore
        err = self._mod_hierarchy_error(ctx, member)
        if err:
            return await ctx.warn(err)
        seconds = None
        if duration:
            try:
                seconds = parse_duration(duration)
            except ValueError:
                reason = f"{duration} {reason}".strip()
                seconds = None
        role = await self._jail_role(ctx.guild)  # type: ignore
        if role is None:
            return await ctx.warn("Cannot set up the jail role.")
        keep = [r.id for r in member.roles if not r.is_default() and r != role]
        try:
            await self.bot.pool.execute(
                "INSERT INTO jail_state (guild_id, user_id, role_ids, expires_at) VALUES (%s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE role_ids = VALUES(role_ids), expires_at = VALUES(expires_at)",
                ctx.guild.id, member.id, json.dumps(keep),  # type: ignore
                int(time.time()) + seconds if seconds else None,
            )
        except Exception:
            pass
        try:
            await member.remove_roles(*[r for r in member.roles if not r.is_default() and r != role], reason=f"Jailed by {ctx.author}")
            await member.add_roles(role, reason=f"Jailed by {ctx.author}: {reason}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot jail: `{exc}`")
        case_id = await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, "jail", reason)  # type: ignore
        if seconds:
            try:
                await self.bot.pool.execute(
                    "INSERT INTO temp_actions (guild_id, user_id, action, role_id, data, expires_at, active, created_at) "
                    "VALUES (%s, %s, 'jail', NULL, %s, %s, 1, %s)",
                    ctx.guild.id, member.id, f"case:{case_id}", int(time.time()) + seconds, int(time.time()),  # type: ignore
                )
            except Exception:
                pass
            return await ctx.approve(f"{member.mention} jailed for **{humanize_seconds(seconds)}** (case `#{case_id}`).")
        return await ctx.approve(f"{member.mention} jailed (case `#{case_id}`).")

    @hybrid_command(name="unjail", description="Unjail a member.", example="unjail @voby")
    @has_permissions(manage_messages=True)
    async def unjail(self, ctx: Context, member: Member, *, reason: str = "No reason provided."):
        state = None
        try:
            state = await self.bot.pool.fetchrow(
                "SELECT role_ids FROM jail_state WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id, member.id,  # type: ignore
            )
        except Exception:
            state = None
        role_id = None
        try:
            role_id = await self.bot.pool.fetchval(
                "SELECT role_id FROM jail_config WHERE guild_id = %s", ctx.guild.id  # type: ignore
            )
        except Exception:
            role_id = None
        jail_role = ctx.guild.get_role(role_id) if role_id else discord.utils.get(ctx.guild.roles, name="Jailed")  # type: ignore
        try:
            if jail_role is not None and jail_role in member.roles:
                await member.remove_roles(jail_role, reason=f"Unjailed by {ctx.author}: {reason}")
            restore_ids: List[int] = []
            if state and state.get("role_ids"):
                try:
                    restore_ids = [int(x) for x in json.loads(state["role_ids"])]
                except Exception:
                    restore_ids = []
            for rid in restore_ids:
                role = ctx.guild.get_role(rid)  # type: ignore
                if role is not None and role not in member.roles and role.position < ctx.guild.me.top_role.position:  # type: ignore
                    try:
                        await member.add_roles(role, reason=f"Unjailed by {ctx.author}")
                    except Exception:
                        continue
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot unjail: `{exc}`")
        try:
            await self.bot.pool.execute(
                "DELETE FROM jail_state WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id, member.id,  # type: ignore
            )
            await self.bot.pool.execute(
                "UPDATE temp_actions SET active = 0 WHERE guild_id = %s AND user_id = %s AND action = 'jail' AND active = 1",
                ctx.guild.id, member.id,  # type: ignore
            )
        except Exception:
            pass
        return await ctx.approve(f"{member.mention} unjailed.")

    @hybrid_command(name="jaillist", aliases=["currentlyjailed", "jl"], description="List jailed members.", example="jaillist")
    @has_permissions(manage_messages=True)
    async def jaillist(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT user_id, expires_at FROM jail_state WHERE guild_id = %s LIMIT 100",
            ctx.guild.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("No jailed members.")
        entries = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])  # type: ignore
            label = member.mention if member else f"`{row['user_id']}`"
            suffix = f" until <t:{int(row['expires_at'])}:R>" if row.get("expires_at") else ""
            entries.append(f"{label}{suffix}")
        await self._send_paginated(ctx, "Jailed members", entries, "No jailed members.")

    # ------------------------------------------------------------------
    # Temporary roles
    # ------------------------------------------------------------------

    @hybrid_group(name="temprole", invoke_without_command=True, description="Give a temporary role.", example="temprole @voby @role 1h")
    @has_permissions(manage_roles=True)
    async def temprole(self, ctx: Context, member: Member = None, role: Role = None, duration: str = None):  # type: ignore
        if member is None or role is None or duration is None:
            return await ctx.send_help(ctx.command)
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        if role.is_default() or role.position >= ctx.guild.me.top_role.position:  # type: ignore
            return await ctx.warn("I cannot manage that role.")
        if ctx.author.id != ctx.guild.owner_id and role.position >= ctx.author.top_role.position:  # type: ignore
            return await ctx.warn("That role is above you.")
        try:
            await member.add_roles(role, reason=f"Temporary role by {ctx.author} for {humanize_seconds(seconds)}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot add role: `{exc}`")
        await self.bot.pool.execute(
            "INSERT INTO temp_actions (guild_id, user_id, action, role_id, data, expires_at, active, created_at) "
            "VALUES (%s, %s, 'temprole', %s, %s, %s, 1, %s)",
            ctx.guild.id, member.id, role.id, f"by:{ctx.author.id}",  # type: ignore
            int(time.time()) + seconds, int(time.time()),
        )
        return await ctx.approve(f"{member.mention} given {role.mention} for **{humanize_seconds(seconds)}**.")

    @temprole.command(name="list", description="List active temporary roles.", example="temprole list")
    @has_permissions(manage_roles=True)
    async def temprole_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT user_id, role_id, expires_at FROM temp_actions WHERE guild_id = %s AND action = 'temprole' AND active = 1 ORDER BY expires_at ASC LIMIT 50",
            ctx.guild.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("No active temporary roles.")
        entries = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])  # type: ignore
            role = ctx.guild.get_role(row["role_id"])  # type: ignore
            mlabel = member.mention if member else str(row["user_id"])
            rlabel = role.mention if role else str(row["role_id"])
            exp = int(row["expires_at"])
            entries.append(f"{mlabel} -> {rlabel} until <t:{exp}:R>")
        await self._send_paginated(ctx, "Temporary roles", entries, "No active temporary roles.")

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    @hybrid_group(name="remind", aliases=["reminder"], invoke_without_command=True, description="Set a reminder.", example="remind 10m take a break")
    async def remind(self, ctx: Context, duration: str = None, *, text: str = None):  # type: ignore
        if not duration or not text:
            return await ctx.send_help(ctx.command)
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        text = text.strip()
        if not text or len(text) > 1000:
            return await ctx.warn("Reminder text must be 1–1000 characters.")
        remind_at = int(time.time()) + seconds
        await self.bot.pool.execute(
            "INSERT INTO reminders (guild_id, user_id, channel_id, content, remind_at, created_at, done) "
            "VALUES (%s, %s, %s, %s, %s, %s, 0)",
            ctx.guild.id if ctx.guild else 0, ctx.author.id, ctx.channel.id,  # type: ignore
            text, remind_at, int(time.time()),
        )
        return await ctx.approve(f"I will remind you in **{humanize_seconds(seconds)}**.")

    @remind.command(name="list", description="List your reminders.", example="remind list")
    async def remind_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT id, content, remind_at FROM reminders WHERE guild_id = %s AND user_id = %s AND done = 0 ORDER BY remind_at ASC LIMIT 25",
            ctx.guild.id if ctx.guild else 0, ctx.author.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("You have no active reminders.")
        entries = [f"`{r['id']}` <t:{int(r['remind_at'])}:R> — {(r['content'] or '')[:100]}" for r in rows]
        await self._send_paginated(ctx, "Your reminders", entries, "No reminders.")

    @remind.command(name="remove", aliases=["delete", "del"], description="Remove a reminder.", example="remind remove 3")
    async def remind_remove(self, ctx: Context, reminder_id: int):
        row = await self.bot.pool.fetchrow(
            "SELECT id FROM reminders WHERE id = %s AND user_id = %s AND done = 0",
            reminder_id, ctx.author.id,
        )
        if not row:
            return await ctx.warn("Reminder not found.")
        await self.bot.pool.execute("UPDATE reminders SET done = 1 WHERE id = %s", reminder_id)
        return await ctx.approve(f"Removed reminder `{reminder_id}`.")

    @hybrid_command(name="reminders", description="List your reminders.", example="reminders")
    async def reminders(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT id, content, remind_at FROM reminders WHERE guild_id = %s AND user_id = %s AND done = 0 ORDER BY remind_at ASC LIMIT 25",
            ctx.guild.id if ctx.guild else 0, ctx.author.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("You have no active reminders.")
        entries = [f"`{r['id']}` <t:{int(r['remind_at'])}:R> — {(r['content'] or '')[:100]}" for r in rows]
        await self._send_paginated(ctx, "Your reminders", entries, "No reminders.")

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    @hybrid_group(name="thread", invoke_without_command=True, description="Manage threads.", example="thread lock")
    async def thread(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    def _resolve_thread(self, ctx: Context, thread: Optional[Thread]) -> Optional[Thread]:
        if thread is not None:
            return thread
        if isinstance(ctx.channel, Thread):
            return ctx.channel
        return None

    @thread.command(name="rename", description="Rename a thread.", example="thread rename new-name")
    @has_permissions(manage_threads=True)
    async def thread_rename(self, ctx: Context, new_name: str, thread: Thread = None):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        new_name = new_name.strip()
        if not new_name or len(new_name) > 100:
            return await ctx.warn("Thread names must be 1–100 characters.")
        try:
            await target.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot rename: `{exc}`")
        return await ctx.approve(f"Thread renamed to `{new_name}`.")

    @thread.command(name="lock", aliases=["close"], description="Lock a thread.", example="thread lock")
    @has_permissions(manage_threads=True)
    async def thread_lock(self, ctx: Context, thread: Thread = None, *, reason: str = "Locked"):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        try:
            await target.edit(locked=True, reason=f"{reason} | By {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot lock: `{exc}`")
        return await ctx.approve(f"Locked {target.mention}.")

    @thread.command(name="unlock", aliases=["reopen"], description="Unlock a thread.", example="thread unlock")
    @has_permissions(manage_threads=True)
    async def thread_unlock(self, ctx: Context, thread: Thread = None, *, reason: str = "Unlocked"):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        try:
            await target.edit(locked=False, archived=False, reason=f"{reason} | By {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot unlock: `{exc}`")
        return await ctx.approve(f"Unlocked {target.mention}.")

    @thread.command(name="add", description="Add a member to a thread.", example="thread add @voby")
    @has_permissions(manage_threads=True)
    async def thread_add(self, ctx: Context, member: Member, thread: Thread = None):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        try:
            await target.add_user(member)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot add: `{exc}`")
        return await ctx.approve(f"Added {member.mention} to {target.mention}.")

    @thread.command(name="remove", description="Remove a member from a thread.", example="thread remove @voby")
    @has_permissions(manage_threads=True)
    async def thread_remove(self, ctx: Context, member: Member, thread: Thread = None):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        try:
            await target.remove_user(member)
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot remove: `{exc}`")
        return await ctx.approve(f"Removed {member.mention} from {target.mention}.")

    # NOTE: `thread` is nested under /modtools for slash, so Discord allows
    # only flat subcommands beneath it (no sub-sub-groups). `watch` is a
    # plain subcommand and the thread listing is `thread list` (not
    # `thread watch list`) so slash registration stays valid.
    @thread.command(name="watch", description="Watch a thread (prevent archival).", example="thread watch")
    @has_permissions(manage_channels=True)
    async def thread_watch(self, ctx: Context, thread: Thread = None):  # type: ignore
        target = self._resolve_thread(ctx, thread)
        if target is None:
            return await ctx.warn("Run this inside a thread or provide one.")
        try:
            await target.edit(auto_archive_duration=60, reason=f"Watched by {ctx.author}")
            await target.join()
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot watch: `{exc}`")
        return await ctx.approve(f"Watching {target.mention} (set to 1h archival).")

    @thread.command(name="list", description="List active threads in this server.", example="thread list")
    @has_permissions(manage_channels=True)
    async def thread_list(self, ctx: Context):
        threads = list(getattr(ctx.guild, "threads", []) or [])  # type: ignore
        if not threads:
            return await ctx.warn("No active threads.")
        entries = [f"{t.mention} (`{t.id}`)" for t in threads[:50]]
        await self._send_paginated(ctx, "Active threads", entries, "No active threads.")

    # ------------------------------------------------------------------
    # Slowmode / revokefiles / topic / talk / unhide
    # ------------------------------------------------------------------

    @hybrid_group(name="slowmode", invoke_without_command=True, description="Manage slowmode.", example="slowmode on #channel 10s")
    @has_permissions(manage_channels=True)
    async def slowmode(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @slowmode.command(name="on", description="Enable slowmode.", example="slowmode on #channel 10s")
    @has_permissions(manage_channels=True)
    async def slowmode_on(self, ctx: Context, channel: TextChannel = None, delay: str = "10s"):  # type: ignore
        target = channel or ctx.channel
        try:
            seconds = parse_duration(delay, 6 * 3600)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        try:
            await target.edit(slowmode_delay=min(int(seconds), 21600), reason=f"Slowmode by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot set slowmode: `{exc}`")
        return await ctx.approve(f"Slowmode set to **{humanize_seconds(int(seconds))}** in {target.mention}.")

    @slowmode.command(name="off", description="Disable slowmode.", example="slowmode off #channel")
    @has_permissions(manage_channels=True)
    async def slowmode_off(self, ctx: Context, channel: TextChannel = None):  # type: ignore
        target = channel or ctx.channel
        try:
            await target.edit(slowmode_delay=0, reason=f"Slowmode off by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot disable slowmode: `{exc}`")
        return await ctx.approve(f"Slowmode disabled in {target.mention}.")

    @hybrid_group(name="revokefiles", invoke_without_command=True, description="Toggle file/embed permissions.", example="revokefiles off")
    @has_permissions(manage_channels=True)
    async def revokefiles(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @revokefiles.command(name="on", description="Allow files and embeds.", example="revokefiles on #channel")
    @has_permissions(manage_channels=True)
    async def revokefiles_on(self, ctx: Context, channel: TextChannel = None):  # type: ignore
        target = channel or ctx.channel
        try:
            await target.set_permissions(ctx.guild.default_role, attach_files=True, embed_links=True, reason=f"By {ctx.author}")  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot update: `{exc}`")
        return await ctx.approve(f"Allowed files and embeds in {target.mention}.")

    @revokefiles.command(name="off", description="Revoke files and embeds.", example="revokefiles off #channel")
    @has_permissions(manage_channels=True)
    async def revokefiles_off(self, ctx: Context, channel: TextChannel = None):  # type: ignore
        target = channel or ctx.channel
        try:
            await target.set_permissions(ctx.guild.default_role, attach_files=False, embed_links=False, reason=f"By {ctx.author}")  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot update: `{exc}`")
        return await ctx.approve(f"Revoked files and embeds in {target.mention}.")

    @hybrid_command(name="topic", description="Change the channel topic.", example="topic new topic here")
    @has_permissions(manage_channels=True)
    async def topic(self, ctx: Context, *, text: str):
        text = (text or "").strip()
        if len(text) > 1024:
            return await ctx.warn("Topics must be under 1024 characters.")
        try:
            await ctx.channel.edit(topic=text or None, reason=f"Topic by {ctx.author}")  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot set topic: `{exc}`")
        return await ctx.approve("Channel topic updated.")

    @hybrid_command(name="talk", description="Toggle send permission for a role.", example="talk #channel @role")
    @has_permissions(manage_channels=True)
    async def talk(self, ctx: Context, channel: TextChannel = None, role: Role = None):  # type: ignore
        target = channel or ctx.channel
        if role is None:
            return await ctx.send_help(ctx.command)
        overwrite = target.overwrites_for(role)
        new_value = not (overwrite.send_messages is True)
        try:
            await target.set_permissions(role, send_messages=new_value, reason=f"Talk toggle by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot update: `{exc}`")
        state = "can now talk" if new_value else "can no longer talk"
        return await ctx.approve(f"{role.mention} {state} in {target.mention}.")

    @hybrid_command(name="unhide", description="Unhide a channel for a role or member.", example="unhide #channel @role")
    @has_permissions(manage_channels=True)
    async def unhide(self, ctx: Context, channel: TextChannel = None, role: Role = None, member: Member = None):  # type: ignore
        target_channel = channel or ctx.channel
        overwrite_target = member or role or ctx.guild.default_role  # type: ignore
        try:
            await target_channel.set_permissions(overwrite_target, view_channel=True, reason=f"Unhidden by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot unhide: `{exc}`")
        return await ctx.approve(f"{target_channel.mention} unhidden for {overwrite_target.mention}.")  # type: ignore

    # ------------------------------------------------------------------
    # Voice / invites / members / nicknames / permissions
    # ------------------------------------------------------------------

    @hybrid_command(name="moveall", description="Move everyone in your VC to another channel.", example="moveall <voice channel>")
    @has_permissions(administrator=True)
    async def moveall(self, ctx: Context, *, channel: VoiceChannel):
        author_voice = getattr(ctx.author, "voice", None)
        if not author_voice or not author_voice.channel:
            return await ctx.warn("Join a voice channel first.")
        moved = 0
        for member in list(author_voice.channel.members):
            try:
                await member.move_to(channel, reason=f"Move-all by {ctx.author}")
                moved += 1
            except Exception:
                continue
            await asyncio.sleep(0.25)
        if not moved:
            return await ctx.warn("No members could be moved.")
        return await ctx.approve(f"Moved **{moved}** member(s) to {channel.mention}.")

    @hybrid_command(name="clearinvites", description="Delete all invites in this guild.", example="clearinvites")
    @has_permissions(manage_guild=True)
    async def clearinvites(self, ctx: Context):
        try:
            invites = await ctx.guild.invites()  # type: ignore
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot list invites: `{exc}`")
        deleted = 0
        for invite in invites:
            try:
                await invite.delete(reason=f"Cleared by {ctx.author}")
                deleted += 1
            except Exception:
                continue
            await asyncio.sleep(0.25)
        return await ctx.approve(f"Deleted **{deleted}/{len(invites)}** invite(s).")

    @hybrid_command(name="newmembers", aliases=["newusers"], description="List recently joined members.", example="newmembers 5")
    async def newmembers(self, ctx: Context, count: int = 5):
        count = max(1, min(int(count or 5), 25))
        members = sorted(
            [m for m in ctx.guild.members if m.joined_at is not None],  # type: ignore
            key=lambda m: m.joined_at,  # type: ignore
            reverse=True,
        )[:count]
        entries = [f"{m.mention} joined <t:{int(m.joined_at.timestamp())}:R>" for m in members]  # type: ignore
        await self._send_paginated(ctx, "New members", entries, "No members found.")

    @hybrid_command(name="rename", aliases=["nick", "nickname"], description="Change a member's nickname.", example="rename @voby New Name")
    @has_permissions(manage_nicknames=True)
    async def rename(self, ctx: Context, member: Member, *, newnick: str = None):  # type: ignore
        if ctx.author.id != ctx.guild.owner_id:  # type: ignore
            if member.top_role.position >= ctx.author.top_role.position and ctx.author.id != ctx.guild.owner_id:  # type: ignore
                return await ctx.warn("You cannot rename someone above you.")
        try:
            await member.edit(nick=newnick, reason=f"Renamed by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot rename: `{exc}`")
        if newnick:
            return await ctx.approve(f"{member.mention} renamed to `{newnick}`.")
        return await ctx.approve(f"Cleared nickname for {member.mention}.")

    @hybrid_command(name="permissions", aliases=["perms"], description="Show a member's channel permissions.", example="permissions @voby")
    @has_permissions(manage_roles=True, manage_channels=True)
    async def permissions(self, ctx: Context, member: Member = None, channel: TextChannel = None):  # type: ignore
        target = member or ctx.author
        where = channel or ctx.channel
        perms = where.permissions_for(target)  # type: ignore
        allowed = [name.replace("_", " ").title() for name, value in perms if value]
        embed = Embed(
            color=COLORS.neutral,
            title=f"Permissions for {target} in #{where.name}",  # type: ignore
            description=", ".join(allowed[:60]) or "None",
        )
        return await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Setup / raid / restrictions / sticky roles / naughty
    # ------------------------------------------------------------------

    @hybrid_command(name="setupmute", description="Create a Muted role and block it everywhere.", example="setupmute")
    @has_permissions(manage_guild=True, manage_channels=True)
    async def setupmute(self, ctx: Context):
        role = discord.utils.get(ctx.guild.roles, name="Muted")  # type: ignore
        if role is None:
            try:
                role = await ctx.guild.create_role(name="Muted", reason=f"Setupmute by {ctx.author}")  # type: ignore
            except (discord.Forbidden, discord.HTTPException) as exc:
                return await ctx.warn(f"Cannot create Muted role: `{exc}`")
        try:
            await self.bot.pool.execute(
                "INSERT INTO mute_config (guild_id, role_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE role_id = VALUES(role_id)",
                ctx.guild.id, role.id,  # type: ignore
            )
        except Exception:
            pass
        updated = 0
        for channel in ctx.guild.channels:  # type: ignore
            try:
                await channel.set_permissions(role, send_messages=False, speak=False, add_reactions=False, reason="Setupmute")
                updated += 1
            except Exception:
                continue
        return await ctx.approve(f"Muted role {role.mention} configured in **{updated}** channel(s).")

    @hybrid_command(name="setup", aliases=["setme"], description="Set up jail and mute roles.", example="setup")
    @has_permissions(administrator=True)
    async def setup(self, ctx: Context):
        jail_role = await self._jail_role(ctx.guild)  # type: ignore
        muted = discord.utils.get(ctx.guild.roles, name="Muted")  # type: ignore
        if muted is None:
            try:
                muted = await ctx.guild.create_role(name="Muted", reason=f"Setup by {ctx.author}")  # type: ignore
            except Exception:
                muted = None
        return await ctx.approve(
            f"Setup complete. Jail: {jail_role.mention if jail_role else 'failed'}, "
            f"Muted: {muted.mention if muted else 'failed'}."
        )

    @hybrid_command(name="raid", description="Remove members who joined recently.", example="raid 10m ban raiding")
    @has_permissions(ban_members=True)
    async def raid(self, ctx: Context, window: str, action: str = "ban", *, reason: str = "Raid protection"):
        try:
            seconds = parse_duration(window, 30 * 86400)
        except ValueError as exc:
            return await ctx.warn(str(exc))
        action = (action or "ban").lower()
        if action not in ("ban", "kick"):
            return await ctx.warn("Action must be `ban` or `kick`.")
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        candidates = [m for m in ctx.guild.members if m.joined_at and m.joined_at > cutoff and not m.bot]  # type: ignore
        if not candidates:
            return await ctx.warn("No members joined in that window.")
        if len(candidates) > 100:
            return await ctx.warn(f"Safety limit: {len(candidates)} matches (max 100). Narrow the window.")
        removed = 0
        for member in candidates[:100]:
            if self._mod_hierarchy_error(ctx, member):
                continue
            try:
                if action == "ban":
                    await member.ban(reason=f"{reason} | Raid action by {ctx.author}", delete_message_days=1)
                else:
                    await member.kick(reason=f"{reason} | Raid action by {ctx.author}")
                await self._mod_log_case(ctx.guild.id, member.id, ctx.author.id, action, reason)  # type: ignore
                removed += 1
            except Exception:
                continue
            await asyncio.sleep(0.5)
        return await ctx.approve(f"Raid action complete: **{removed}/{len(candidates[:100])}** removed.")

    @hybrid_group(name="restrictcommand", aliases=["restrictcmd", "restrict", "rc"], invoke_without_command=True, description="Restrict commands to roles.", example="restrictcommand add ban @mod")
    @has_permissions(manage_guild=True)
    async def restrictcommand(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @restrictcommand.command(name="add", description="Restrict a command to a role.", example="restrictcommand add ban @mod")
    @has_permissions(manage_guild=True)
    async def restrictcommand_add(self, ctx: Context, command: str, role: Role):
        name = command.strip().lower()
        if self.bot.get_command(name) is None:
            return await ctx.warn(f"`{name}` is not a command.")
        if name in ("restrictcommand", "restrict", "rc", "restrictcmd"):
            return await ctx.warn("That command cannot be restricted (it would lock out admins).")
        await self.bot.pool.execute(
            "INSERT IGNORE INTO restrict_commands (guild_id, command, role_id) VALUES (%s, %s, %s)",
            ctx.guild.id, name, role.id,  # type: ignore
        )
        return await ctx.approve(f"`{name}` now requires {role.mention} (plus its normal permissions).")

    @restrictcommand.command(name="remove", aliases=["del", "delete"], description="Remove a command restriction.", example="restrictcommand remove ban @mod")
    @has_permissions(manage_guild=True)
    async def restrictcommand_remove(self, ctx: Context, command: str, role: Role):
        name = command.strip().lower()
        await self.bot.pool.execute(
            "DELETE FROM restrict_commands WHERE guild_id = %s AND command = %s AND role_id = %s",
            ctx.guild.id, name, role.id,  # type: ignore
        )
        return await ctx.approve(f"Removed {role.mention} restriction from `{name}`.")

    @restrictcommand.command(name="list", description="List command restrictions.", example="restrictcommand list")
    @has_permissions(manage_guild=True)
    async def restrictcommand_list(self, ctx: Context, filter: str = None):  # type: ignore
        if filter:
            rows = await self.bot.pool.fetch(
                "SELECT command, role_id FROM restrict_commands WHERE guild_id = %s AND (command = %s OR role_id = %s)",
                ctx.guild.id, filter.lower(), int(filter) if filter.isdigit() else -1,  # type: ignore
            )
        else:
            rows = await self.bot.pool.fetch(
                "SELECT command, role_id FROM restrict_commands WHERE guild_id = %s LIMIT 100",
                ctx.guild.id,  # type: ignore
            )
        if not rows:
            return await ctx.warn("No command restrictions.")
        entries = []
        for row in rows:
            role = ctx.guild.get_role(row["role_id"])  # type: ignore
            rlabel = role.mention if role else str(row["role_id"])
            entries.append("`" + str(row["command"]) + "` -> " + rlabel)
        await self._send_paginated(ctx, "Command restrictions", entries, "No restrictions.")

    @restrictcommand.command(name="reset", aliases=["clear"], description="Clear all command restrictions.", example="restrictcommand reset")
    @has_permissions(manage_guild=True)
    async def restrictcommand_reset(self, ctx: Context):
        await self.bot.pool.execute(
            "DELETE FROM restrict_commands WHERE guild_id = %s", ctx.guild.id,  # type: ignore
        )
        return await ctx.approve("Cleared all command restrictions.")

    @hybrid_group(name="stickyrole", aliases=["sr"], invoke_without_command=True, description="Reapply roles on rejoin.", example="stickyrole add @voby @role")
    @has_permissions(administrator=True)
    async def stickyrole(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    def _sticky_owner_ok(self, ctx: Context) -> bool:
        return ctx.author.id == ctx.guild.owner_id or ctx.author.id in (getattr(self.bot, "owner_ids", set()) or set())  # type: ignore

    @stickyrole.command(name="add", description="Add a sticky role.", example="stickyrole add @voby @role")
    @has_permissions(administrator=True)
    async def stickyrole_add(self, ctx: Context, member: Member, *, role: Role):
        if not self._sticky_owner_ok(ctx):
            return await ctx.deny("Only the **server owner** can manage sticky roles.")
        await self.bot.pool.execute(
            "INSERT IGNORE INTO sticky_roles (guild_id, user_id, role_id) VALUES (%s, %s, %s)",
            ctx.guild.id, member.id, role.id,  # type: ignore
        )
        return await ctx.approve(f"{role.mention} will be reapplied to {member.mention}.")

    @stickyrole.command(name="remove", aliases=["del", "clear"], description="Remove a sticky role.", example="stickyrole remove @voby @role")
    @has_permissions(administrator=True)
    async def stickyrole_remove(self, ctx: Context, member: Member, *, role: Role):
        if not self._sticky_owner_ok(ctx):
            return await ctx.deny("Only the **server owner** can manage sticky roles.")
        await self.bot.pool.execute(
            "DELETE FROM sticky_roles WHERE guild_id = %s AND user_id = %s AND role_id = %s",
            ctx.guild.id, member.id, role.id,  # type: ignore
        )
        return await ctx.approve(f"Removed sticky {role.mention} from {member.mention}.")

    @stickyrole.command(name="list", description="List sticky roles.", example="stickyrole list")
    @has_permissions(administrator=True)
    async def stickyrole_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT user_id, role_id FROM sticky_roles WHERE guild_id = %s LIMIT 100",
            ctx.guild.id,  # type: ignore
        )
        if not rows:
            return await ctx.warn("No sticky roles.")
        entries = []
        for i, row in enumerate(rows, start=1):
            member = ctx.guild.get_member(row["user_id"])  # type: ignore
            role = ctx.guild.get_role(row["role_id"])  # type: ignore
            mlabel = member.mention if member else str(row["user_id"])
            rlabel = role.mention if role else str(row["role_id"])
            entries.append(f"`{i}` {mlabel} -> {rlabel}")
        await self._send_paginated(ctx, "Sticky roles", entries, "No sticky roles.")

    @hybrid_command(name="naughty", description="Make a channel NSFW for 30 seconds.", example="naughty #channel")
    @has_permissions(manage_channels=True)
    async def naughty(self, ctx: Context, channel: TextChannel = None):  # type: ignore
        target = channel or ctx.channel
        was_nsfw = target.is_nsfw()
        try:
            await target.edit(nsfw=True, reason=f"Naughty by {ctx.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            return await ctx.warn(f"Cannot edit channel: `{exc}`")
        await ctx.approve(f"{target.mention} is NSFW for **30 seconds**.")
        await asyncio.sleep(30)
        try:
            if not was_nsfw:
                await target.edit(nsfw=False, reason="Naughty expired")
        except Exception:
            pass
