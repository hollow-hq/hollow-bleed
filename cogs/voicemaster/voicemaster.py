"""
VoiceMaster cog — join-to-create private voice channels.

Users join the "Join To Create" channel to spawn their own voice channel,
then manage it through a persistent button/select interface or the
`voicemaster` hybrid commands.
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import discord
from discord import (
    ButtonStyle,
    Embed,
    Member,
    Message,
    PermissionOverwrite,
    SelectOption,
    TextStyle,
)
from discord.ext.commands import (
    BucketType,
    Cog,
    cooldown,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.ui import Button, Modal, Select, TextInput, View, button

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.predicates import has_permissions
from base.managers.types import CogMeta

_log = logging.getLogger(__name__)

DEFAULT_BITRATE = 64
EMPTY_DELETE_DELAY = 10

BITRATES = (32, 64, 96, 128, 192, 256, 384)
LIMITS = (0, 2, 5, 10, 15, 20, 25, 30, 50, 99)

REGION_LABELS = {
    "automatic": "Automatic",
    "brazil": "Brazil",
    "eu-central": "EU Central",
    "eu-west": "EU West",
    "frankfurt": "Frankfurt",
    "hongkong": "Hong Kong",
    "india": "India",
    "japan": "Japan",
    "russia": "Russia",
    "singapore": "Singapore",
    "southafrica": "South Africa",
    "southkorea": "South Korea",
    "sydney": "Sydney",
    "us-central": "US Central",
    "us-east": "US East",
    "us-west": "US West",
    "us-south": "US South",
}

OWNER_PERMISSIONS = dict(
    view_channel=True,
    connect=True,
    manage_channels=True,
    move_members=True,
    mute_members=True,
    deafen_members=True,
)


def _match_region(value: str) -> Optional[str]:
    normalized = "".join(value.lower().split())
    for key, label in REGION_LABELS.items():
        if key == normalized or "".join(label.lower().split()) == normalized:
            return key
    return None


class InterfaceButton(Button):
    def __init__(
        self, label: str, emoji: str, style: ButtonStyle, action: str, channel_id: int
    ) -> None:
        super().__init__(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"voicemaster:{action}:{channel_id}",
        )
        self.action = action
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, InterfaceView):
            return
        await view.cog.handle_interface_action(interaction, self.action, self.channel_id)


class InterfaceSelect(Select):
    def __init__(
        self,
        placeholder: str,
        options: List[SelectOption],
        action: str,
        channel_id: int,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            options=options,
            custom_id=f"voicemaster:{action}:{channel_id}",
            min_values=1,
            max_values=1,
        )
        self.action = action
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, InterfaceView):
            return
        await view.cog.handle_interface_action(
            interaction, self.action, self.channel_id, values=self.values
        )


class InterfaceView(View):
    def __init__(
        self,
        cog: "VoiceMaster",
        channel_id: int,
        *,
        locked: bool = False,
        hidden: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id

        self.add_item(
            InterfaceButton(
                "Unlock" if locked else "Lock",
                EMOJIS.UNLOCK if locked else EMOJIS.LOCK,
                ButtonStyle.secondary,
                "lock",
                channel_id,
            )
        )
        self.add_item(
            InterfaceButton("Rename", "✏️", ButtonStyle.secondary, "rename", channel_id)
        )
        self.add_item(
            InterfaceButton("Delete", "🗑️", ButtonStyle.danger, "delete", channel_id)
        )
        self.add_item(
            InterfaceSelect(
                "Change region",
                [
                    SelectOption(label=label, value=value)
                    for value, label in REGION_LABELS.items()
                ],
                "region",
                channel_id,
            )
        )
        self.add_item(
            InterfaceSelect(
                "Change bitrate",
                [
                    SelectOption(label=f"{bitrate} kbps", value=str(bitrate))
                    for bitrate in BITRATES
                ],
                "bitrate",
                channel_id,
            )
        )
        self.add_item(
            InterfaceSelect(
                "Change user limit",
                [
                    SelectOption(label="No limit" if limit == 0 else str(limit), value=str(limit))
                    for limit in LIMITS
                ],
                "limit",
                channel_id,
            )
        )


class DeleteConfirmView(View):
    def __init__(self, cog: "VoiceMaster", channel_id: int) -> None:
        super().__init__(timeout=30)
        self.cog = cog
        self.channel_id = channel_id

    @button(label="Delete", style=ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, button: Button, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id) if guild else None
        if channel is None:
            await interaction.response.edit_message(
                content="That voice channel no longer exists.", view=None
            )
        else:
            await self.cog.delete_channel(
                channel, reason=f"VoiceMaster: deleted by {interaction.user}"
            )
            await interaction.response.edit_message(
                content=f"**{channel.name}** has been deleted.", view=None
            )
        self.stop()

    @button(label="Cancel", style=ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, button: Button, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)
        self.stop()


class RenameModal(Modal):
    def __init__(self, cog: "VoiceMaster", channel_id: int) -> None:
        super().__init__(title="Rename voice channel")
        self.cog = cog
        self.channel_id = channel_id
        self.channel_name = TextInput(
            label="Channel name",
            style=TextStyle.short,
            placeholder="Enter a new name for your channel...",
            min_length=1,
            max_length=99,
            required=True,
        )
        self.add_item(self.channel_name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id) if guild else None
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message(
                "That voice channel no longer exists.", ephemeral=True
            )
        name = self.channel_name.value.strip()[:99]
        try:
            await channel.edit(name=name, reason="VoiceMaster: renamed")
        except discord.HTTPException as error:
            return await interaction.response.send_message(
                f"Failed to rename the channel: {error}", ephemeral=True
            )
        await interaction.response.send_message(
            f"Renamed your channel to **{name}**.", ephemeral=True
        )
        await self.cog._refresh_interface(channel)


class VoiceMaster(CogMeta):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._create_locks: Dict[int, asyncio.Lock] = {}
        self._cleanup_tasks: Dict[int, asyncio.Task] = {}

    async def cog_load(self) -> None:
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, locked, hidden FROM voicemaster_channels"
        )
        for row in rows:
            self.bot.add_view(
                InterfaceView(
                    self,
                    row["channel_id"],
                    locked=bool(row["locked"]),
                    hidden=bool(row["hidden"]),
                )
            )
        if rows:
            _log.info(f"Registered {len(rows)} persistent VoiceMaster interfaces")

    def cog_unload(self) -> None:
        for task in self._cleanup_tasks.values():
            task.cancel()
        self._cleanup_tasks.clear()

    async def _get_config(self, guild_id: int) -> Optional[dict]:
        return await self.bot.pool.fetchrow(
            "SELECT category_id, interface_id, create_channel_id FROM voicemaster WHERE guild_id = %s",
            guild_id,
        )

    async def _get_channel_data(self, guild_id: int, channel_id: int) -> Optional[dict]:
        return await self.bot.pool.fetchrow(
            "SELECT owner_id, locked, hidden FROM voicemaster_channels WHERE guild_id = %s AND channel_id = %s",
            guild_id,
            channel_id,
        )

    async def _get_member_channel(self, guild_id: int, member_id: int) -> Optional[dict]:
        return await self.bot.pool.fetchrow(
            "SELECT channel_id FROM voicemaster_channels WHERE guild_id = %s AND owner_id = %s",
            guild_id,
            member_id,
        )

    async def _resolve_owner_channel(
        self, ctx: Context
    ) -> Optional[Tuple[discord.VoiceChannel, dict]]:
        if ctx.guild is None:
            await ctx.deny("This command can't be used in private messages.")
            return None
        config = await self._get_config(ctx.guild.id)
        if config is None:
            await ctx.deny(
                "VoiceMaster is not set up in this server. An admin needs to run `voicemaster setup` first."
            )
            return None
        voice = ctx.author.voice
        if voice is None or voice.channel is None or not isinstance(voice.channel, discord.VoiceChannel):
            await ctx.deny("You need to join your voice channel first.")
            return None
        channel = voice.channel
        data = await self._get_channel_data(ctx.guild.id, channel.id)
        if data is None:
            await ctx.deny("This is not a VoiceMaster channel. Join **Join To Create** to create one.")
            return None
        if data["owner_id"] != ctx.author.id:
            await ctx.deny("You don't own this voice channel.")
            return None
        return channel, data

    async def _apply_everyone_overwrite(
        self, channel: discord.VoiceChannel, *, locked: bool, hidden: bool
    ) -> None:
        await channel.set_permissions(
            channel.guild.default_role,
            view_channel=not hidden,
            connect=not hidden and not locked,
            reason="VoiceMaster: permissions updated",
        )

    async def _set_channel_flags(
        self,
        guild_id: int,
        channel_id: int,
        *,
        locked: Optional[bool] = None,
        hidden: Optional[bool] = None,
    ) -> None:
        if locked is not None:
            await self.bot.pool.execute(
                "UPDATE voicemaster_channels SET locked = %s WHERE channel_id = %s",
                int(locked),
                channel_id,
            )
        if hidden is not None:
            await self.bot.pool.execute(
                "UPDATE voicemaster_channels SET hidden = %s WHERE channel_id = %s",
                int(hidden),
                channel_id,
            )

    async def _build_interface_embed(
        self, owner: Optional[Member], channel: discord.VoiceChannel
    ) -> Embed:
        data = await self._get_channel_data(channel.guild.id, channel.id) or {}
        locked = bool(data.get("locked"))
        hidden = bool(data.get("hidden"))
        region = channel.rtc_region
        members = channel.members
        status = "👻 Hidden" if hidden else ("🔒 Locked" if locked else "🔓 Unlocked")
        embed = Embed(
            color=COLORS.neutral,
            title=f"🎙️ {channel.name}",
            description=f"Owned by {owner.mention}" if owner else "Owned by an unknown member",
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(
            name="Region",
            value=REGION_LABELS.get(region, "Automatic") if region else "Automatic",
            inline=True,
        )
        embed.add_field(name="Bitrate", value=f"{max(channel.bitrate // 1000, 1)} kbps", inline=True)
        embed.add_field(
            name="User limit",
            value=str(channel.user_limit) if channel.user_limit else "No limit",
            inline=True,
        )
        embed.add_field(name="Members", value=str(len(members)), inline=True)
        if members:
            embed.add_field(
                name="In channel",
                value=", ".join(member.display_name for member in members[:25]),
                inline=False,
            )
        embed.set_footer(text="VoiceMaster — manage your channel with the controls below")
        return embed

    async def _send_interface(self, member: Member, channel: discord.VoiceChannel) -> None:
        guild = channel.guild
        config = await self._get_config(guild.id)
        if config is None:
            return
        interface_channel = guild.get_channel(config["interface_id"])
        if interface_channel is None or not isinstance(interface_channel, discord.TextChannel):
            return

        previous = await self.bot.pool.fetchrow(
            "SELECT message_id FROM voicemaster_interfaces WHERE channel_id = %s",
            channel.id,
        )
        if previous is not None:
            try:
                old_message = await interface_channel.fetch_message(previous["message_id"])
                await old_message.delete()
            except discord.HTTPException:
                pass

        embed = await self._build_interface_embed(member, channel)
        data = await self._get_channel_data(guild.id, channel.id) or {}
        view = InterfaceView(
            self, channel.id, locked=bool(data.get("locked")), hidden=bool(data.get("hidden"))
        )
        try:
            message = await interface_channel.send(embed=embed, view=view)
        except discord.HTTPException:
            return

        await self.bot.pool.execute(
            """
            INSERT INTO voicemaster_interfaces (guild_id, channel_id, message_id)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE message_id = VALUES(message_id)
            """,
            guild.id,
            channel.id,
            message.id,
        )

    async def _refresh_interface(self, channel: discord.VoiceChannel) -> None:
        guild = channel.guild
        row = await self.bot.pool.fetchrow(
            "SELECT message_id FROM voicemaster_interfaces WHERE channel_id = %s",
            channel.id,
        )
        if row is None:
            return
        config = await self._get_config(guild.id)
        if config is None:
            return
        interface_channel = guild.get_channel(config["interface_id"])
        if interface_channel is None or not isinstance(interface_channel, discord.TextChannel):
            return
        data = await self._get_channel_data(guild.id, channel.id)
        if data is None:
            return
        owner = guild.get_member(data["owner_id"])
        embed = await self._build_interface_embed(owner, channel)
        view = InterfaceView(
            self, channel.id, locked=bool(data["locked"]), hidden=bool(data["hidden"])
        )
        try:
            message = await interface_channel.fetch_message(row["message_id"])
            await message.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def _handle_join_create(
        self, member: Member, guild: discord.Guild, config: dict
    ) -> None:
        lock = self._create_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            data = await self._get_member_channel(guild.id, member.id)
            if data is not None:
                channel = guild.get_channel(data["channel_id"])
                if channel is not None and isinstance(channel, discord.VoiceChannel):
                    try:
                        await member.move_to(channel, reason="VoiceMaster: rejoined your channel")
                    except discord.HTTPException:
                        pass
                    return
                await self._cleanup_channel_row(guild.id, data["channel_id"])

            category = guild.get_channel(config["category_id"])
            if category is None or not isinstance(category, discord.CategoryChannel):
                return

            create_channel = guild.get_channel(config["create_channel_id"])
            if isinstance(create_channel, discord.VoiceChannel):
                bitrate = create_channel.bitrate
                user_limit = create_channel.user_limit
                rtc_region = create_channel.rtc_region
            else:
                bitrate = DEFAULT_BITRATE * 1000
                user_limit = 0
                rtc_region = None

            overwrites = {
                guild.default_role: PermissionOverwrite(view_channel=True, connect=True),
                member: PermissionOverwrite(**OWNER_PERMISSIONS),
            }
            options = dict(
                name=f"{member.display_name}'s channel",
                bitrate=bitrate,
                user_limit=user_limit,
                overwrites=overwrites,
                reason=f"VoiceMaster: channel for {member}",
            )
            if rtc_region is not None:
                options["rtc_region"] = rtc_region

            try:
                channel = await category.create_voice_channel(**options)
            except discord.HTTPException as error:
                _log.warning(f"VoiceMaster failed to create a channel for {member}: {error}")
                return

            await self.bot.pool.execute(
                """
                INSERT INTO voicemaster_channels (guild_id, channel_id, owner_id, locked, hidden, created_at)
                VALUES (%s, %s, %s, 0, 0, %s)
                """,
                guild.id,
                channel.id,
                member.id,
                int(time.time()),
            )

            try:
                await member.move_to(channel, reason="VoiceMaster: joined to create")
            except discord.HTTPException as error:
                _log.warning(f"VoiceMaster failed to move {member} into their channel: {error}")
                await self._cleanup_channel_row(guild.id, channel.id)
                try:
                    await channel.delete(reason="VoiceMaster: failed to move member")
                except discord.HTTPException:
                    pass
                return

            await self._send_interface(member, channel)

    def _schedule_cleanup(self, channel: discord.VoiceChannel) -> None:
        if channel.id in self._cleanup_tasks:
            return
        task = asyncio.create_task(self._cleanup_after_delay(channel.guild.id, channel.id))
        self._cleanup_tasks[channel.id] = task

    async def _cleanup_after_delay(self, guild_id: int, channel_id: int) -> None:
        try:
            await asyncio.sleep(EMPTY_DELETE_DELAY)
        except asyncio.CancelledError:
            return
        finally:
            self._cleanup_tasks.pop(channel_id, None)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            await self._cleanup_channel_row(guild_id, channel_id)
            return
        if any(not member.bot for member in channel.members):
            return
        await self.delete_channel(channel)

    async def _cleanup_channel_row(self, guild_id: int, channel_id: int) -> None:
        await self.bot.pool.execute(
            "DELETE FROM voicemaster_channels WHERE guild_id = %s AND channel_id = %s",
            guild_id,
            channel_id,
        )
        await self.bot.pool.execute(
            "DELETE FROM voicemaster_interfaces WHERE channel_id = %s",
            channel_id,
        )

    async def delete_channel(
        self, channel: discord.VoiceChannel, *, reason: str = "VoiceMaster: channel deleted"
    ) -> None:
        guild = channel.guild
        task = self._cleanup_tasks.pop(channel.id, None)
        if task is not None:
            task.cancel()

        row = await self.bot.pool.fetchrow(
            "SELECT message_id FROM voicemaster_interfaces WHERE channel_id = %s",
            channel.id,
        )
        if row is not None:
            config = await self._get_config(guild.id)
            if config is not None:
                interface_channel = guild.get_channel(config["interface_id"])
                if interface_channel is not None and isinstance(
                    interface_channel, discord.TextChannel
                ):
                    try:
                        message = await interface_channel.fetch_message(row["message_id"])
                        await message.delete()
                    except discord.HTTPException:
                        pass

        try:
            await channel.delete(reason=reason)
        except discord.HTTPException:
            pass

        await self._cleanup_channel_row(guild.id, channel.id)

    async def handle_interface_action(
        self,
        interaction: discord.Interaction,
        action: str,
        channel_id: int,
        values: Optional[List[str]] = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        data = await self._get_channel_data(guild.id, channel_id)
        if data is None:
            return await interaction.response.send_message(
                "This voice channel no longer exists.", ephemeral=True
            )
        if data["owner_id"] != interaction.user.id:
            return await interaction.response.send_message(
                "Only the owner of this channel can use these controls.", ephemeral=True
            )
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, discord.VoiceChannel):
            return await interaction.response.send_message(
                "This voice channel no longer exists.", ephemeral=True
            )

        if action == "lock":
            locked = not data["locked"]
            try:
                await self._apply_everyone_overwrite(
                    channel, locked=locked, hidden=bool(data["hidden"])
                )
                await self._set_channel_flags(guild.id, channel_id, locked=locked)
                await self._refresh_interface(channel)
            except discord.HTTPException as error:
                return await interaction.response.send_message(
                    f"Failed to update the channel: {error}", ephemeral=True
                )
            return await interaction.response.send_message(
                f"Your channel is now **{'locked' if locked else 'unlocked'}**.", ephemeral=True
            )

        if action == "rename":
            return await interaction.response.send_modal(RenameModal(self, channel_id))

        if action == "delete":
            view = DeleteConfirmView(self, channel_id)
            return await interaction.response.send_message(
                f"Are you sure you want to delete **{channel.name}**? This cannot be undone.",
                ephemeral=True,
                view=view,
            )

        if action == "region" and values:
            key = values[0]
            rtc_region = None if key == "automatic" else key
            try:
                await channel.edit(rtc_region=rtc_region, reason="VoiceMaster: region changed")
                await self._refresh_interface(channel)
            except discord.HTTPException as error:
                return await interaction.response.send_message(
                    f"Failed to update the region: {error}", ephemeral=True
                )
            return await interaction.response.send_message(
                f"Region set to **{REGION_LABELS.get(key, key)}**.", ephemeral=True
            )

        if action == "bitrate" and values:
            bitrate = int(values[0])
            try:
                await channel.edit(bitrate=bitrate * 1000, reason="VoiceMaster: bitrate changed")
                await self._refresh_interface(channel)
            except discord.HTTPException as error:
                return await interaction.response.send_message(
                    f"Failed to update the bitrate: {error}", ephemeral=True
                )
            return await interaction.response.send_message(
                f"Bitrate set to **{bitrate} kbps**.", ephemeral=True
            )

        if action == "limit" and values:
            limit = int(values[0])
            try:
                await channel.edit(user_limit=limit, reason="VoiceMaster: user limit changed")
                await self._refresh_interface(channel)
            except discord.HTTPException as error:
                return await interaction.response.send_message(
                    f"Failed to update the user limit: {error}", ephemeral=True
                )
            return await interaction.response.send_message(
                f"User limit set to **{limit}**." if limit else "The user limit was removed.",
                ephemeral=True,
            )

        await interaction.response.send_message("Unknown action.", ephemeral=True)

    @Cog.listener("on_voice_state_update")
    async def on_voice_state_update(
        self, member: Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if member.bot:
            return
        guild = member.guild
        if guild is None:
            return
        try:
            config = await self._get_config(guild.id)
            if config is None:
                return

            if after.channel is not None and after.channel.id == config["create_channel_id"]:
                await self._handle_join_create(member, guild, config)
                return

            if after.channel is not None:
                task = self._cleanup_tasks.pop(after.channel.id, None)
                if task is not None:
                    task.cancel()

            if before.channel is not None:
                data = await self._get_channel_data(guild.id, before.channel.id)
                if data is None:
                    return
                channel = guild.get_channel(before.channel.id)
                if channel is None or not isinstance(channel, discord.VoiceChannel):
                    await self._cleanup_channel_row(guild.id, before.channel.id)
                    return
                if not any(not voice_member.bot for voice_member in channel.members):
                    self._schedule_cleanup(channel)
        except Exception:
            _log.exception("VoiceMaster voice state handler failed")

    @hybrid_group(
        name="voicemaster",
        aliases=["join2create", "j2c", "vm"],
        invoke_without_command=True,
        description="Create and manage your own voice channel",
    )
    async def voicemaster(self, ctx: Context) -> None:
        return await ctx.send_help(ctx.command)

    @voicemaster.command(
        name="setup",
        description="Set up the VoiceMaster module in this server",
        example="voicemaster setup",
    )
    @has_permissions(manage_guild=True)
    @cooldown(1, 30, BucketType.guild)
    async def setup(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")
        guild = ctx.guild
        if not guild.me.guild_permissions.manage_channels:
            return await ctx.deny("I need the `Manage Channels` permission to set up VoiceMaster.")

        category = discord.utils.get(guild.categories, name="VoiceMaster")
        if category is None:
            try:
                category = await guild.create_category("VoiceMaster", reason="VoiceMaster setup")
            except discord.HTTPException as error:
                return await ctx.deny(f"Failed to create the category: {error}")

        interface = discord.utils.get(category.text_channels, name="interface")
        if interface is None:
            try:
                interface = await category.create_text_channel("interface", reason="VoiceMaster setup")
            except discord.HTTPException as error:
                return await ctx.deny(f"Failed to create the interface channel: {error}")

        create_channel = discord.utils.get(category.voice_channels, name="Join To Create")
        if create_channel is None:
            try:
                create_channel = await category.create_voice_channel(
                    "Join To Create", reason="VoiceMaster setup"
                )
            except discord.HTTPException as error:
                return await ctx.deny(f"Failed to create the join channel: {error}")

        await self.bot.pool.execute(
            """
            INSERT INTO voicemaster (guild_id, category_id, interface_id, create_channel_id)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                category_id = VALUES(category_id),
                interface_id = VALUES(interface_id),
                create_channel_id = VALUES(create_channel_id)
            """,
            guild.id,
            category.id,
            interface.id,
            create_channel.id,
        )

        embed = Embed(
            color=COLORS.neutral,
            title="VoiceMaster",
            description=(
                "Join **Join To Create** to spawn your own private voice channel.\n"
                "Manage it with the interface message sent here or with the "
                f"`{ctx.clean_prefix}voicemaster` commands."
            ),
        )
        embed.set_footer(
            text="New channels inherit the bitrate, user limit and region of Join To Create"
        )
        try:
            await interface.send(embed=embed)
        except discord.HTTPException:
            pass

        return await ctx.approve(
            f"VoiceMaster is set up.\n"
            f"**Category:** {category.mention}\n"
            f"**Interface:** {interface.mention}\n"
            f"**Join To Create:** {create_channel.mention}"
        )

    @voicemaster.command(
        name="lock",
        description="Lock your voice channel so only invited members can join",
        example="voicemaster lock",
    )
    async def lock(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, data = resolved
        if data["locked"]:
            return await ctx.warn("Your channel is already locked.")
        try:
            await self._apply_everyone_overwrite(channel, locked=True, hidden=bool(data["hidden"]))
            await self._set_channel_flags(ctx.guild.id, channel.id, locked=True)
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to lock the channel: {error}")
        return await ctx.approve(f"**{channel.name}** is now locked. Only invited members can join.")

    @voicemaster.command(
        name="unlock",
        description="Unlock your voice channel so everyone can join",
        example="voicemaster unlock",
    )
    async def unlock(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, data = resolved
        if not data["locked"]:
            return await ctx.warn("Your channel is already unlocked.")
        try:
            await self._apply_everyone_overwrite(channel, locked=False, hidden=bool(data["hidden"]))
            await self._set_channel_flags(ctx.guild.id, channel.id, locked=False)
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to unlock the channel: {error}")
        return await ctx.approve(f"**{channel.name}** is now unlocked.")

    @voicemaster.command(
        name="region",
        description="Set the region of your voice channel, or list all regions",
        example="voicemaster region japan",
        usage="region [region]",
    )
    async def region(self, ctx: Context, *, region: Optional[str] = None) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if region is None:
            current = channel.rtc_region or "automatic"
            embed = Embed(
                color=COLORS.neutral,
                title="Voice regions",
                description="\n".join(
                    f"{'➤' if key == current else '•'} **{label}**"
                    for key, label in REGION_LABELS.items()
                ),
            )
            embed.set_footer(text=f"Current region: {REGION_LABELS.get(current, 'Automatic')}")
            return await ctx.send(embed=embed)
        key = _match_region(region)
        if key is None:
            return await ctx.deny(
                f"Unknown region **{region}**. Run `{ctx.clean_prefix}voicemaster region` to list every region."
            )
        rtc_region = None if key == "automatic" else key
        try:
            await channel.edit(rtc_region=rtc_region, reason="VoiceMaster: region changed")
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to update the region: {error}")
        return await ctx.approve(f"Region set to **{REGION_LABELS[key]}**.")

    @voicemaster.command(
        name="bitrate",
        description="Set the bitrate of your voice channel (32-384 kbps)",
        example="voicemaster bitrate 128",
        usage="bitrate <32-384>",
    )
    async def bitrate(self, ctx: Context, bitrate: int) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if not 32 <= bitrate <= 384:
            return await ctx.deny("The bitrate must be between **32** and **384** kbps.")
        try:
            await channel.edit(bitrate=bitrate * 1000, reason="VoiceMaster: bitrate changed")
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to update the bitrate: {error}")
        return await ctx.approve(f"Bitrate set to **{bitrate} kbps**.")

    @voicemaster.command(
        name="limit",
        aliases=["userlimit"],
        description="Set the user limit of your voice channel (0-99)",
        example="voicemaster limit 10",
        usage="limit <0-99>",
    )
    async def limit(self, ctx: Context, limit: int) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if not 0 <= limit <= 99:
            return await ctx.deny("The user limit must be between **0** and **99** (`0` disables the limit).")
        try:
            await channel.edit(user_limit=limit, reason="VoiceMaster: user limit changed")
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to update the user limit: {error}")
        return await ctx.approve(
            f"User limit set to **{limit}**." if limit else "The user limit was removed."
        )

    @voicemaster.command(
        name="name",
        aliases=["rename"],
        description="Rename your voice channel",
        example="voicemaster name Gaming Room",
        usage="name <name>",
    )
    async def name(self, ctx: Context, *, name: str) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        name = name.strip()[:99]
        if not name:
            return await ctx.deny("The channel name can't be empty.")
        try:
            await channel.edit(name=name, reason="VoiceMaster: renamed")
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to rename the channel: {error}")
        return await ctx.approve(f"Renamed your channel to **{name}**.")

    @voicemaster.command(
        name="claim",
        description="Claim ownership of a voice channel whose owner is gone",
        example="voicemaster claim",
    )
    async def claim(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")
        config = await self._get_config(ctx.guild.id)
        if config is None:
            return await ctx.deny("VoiceMaster is not set up in this server.")
        voice = ctx.author.voice
        if voice is None or voice.channel is None or not isinstance(voice.channel, discord.VoiceChannel):
            return await ctx.deny("You need to join a voice channel first.")
        channel = voice.channel
        data = await self._get_channel_data(ctx.guild.id, channel.id)
        if data is None:
            return await ctx.deny("This is not a VoiceMaster channel.")
        if data["owner_id"] == ctx.author.id:
            return await ctx.deny("You already own this channel.")
        owner = ctx.guild.get_member(data["owner_id"])
        if owner is not None and any(voice_member.id == owner.id for voice_member in channel.members):
            return await ctx.deny(f"This channel is still owned by {owner.mention}.")
        try:
            if owner is not None:
                await channel.set_permissions(
                    owner, overwrite=None, reason="VoiceMaster: ownership claimed"
                )
            await channel.set_permissions(
                ctx.author, **OWNER_PERMISSIONS, reason="VoiceMaster: ownership claimed"
            )
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to claim the channel: {error}")
        await self.bot.pool.execute(
            "UPDATE voicemaster_channels SET owner_id = %s WHERE channel_id = %s",
            ctx.author.id,
            channel.id,
        )
        await self._refresh_interface(channel)
        return await ctx.approve(f"You claimed **{channel.name}**.")

    @voicemaster.command(
        name="interface",
        description="Send the interface for your voice channel again",
        example="voicemaster interface",
    )
    @cooldown(1, 10, BucketType.user)
    async def interface(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        await self._send_interface(ctx.author, channel)
        return await ctx.approve(
            f"Sent the interface for **{channel.name}** in the interface channel."
        )

    @voicemaster.command(
        name="drag",
        description="Drag a member into your voice channel",
        example="voicemaster drag @member",
        usage="drag <member>",
    )
    @cooldown(1, 3, BucketType.user)
    async def drag(self, ctx: Context, member: Member) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, data = resolved
        if member.id == ctx.author.id:
            return await ctx.deny("You can't drag yourself.")
        if member.voice is None or member.voice.channel is None:
            return await ctx.deny(f"{member.mention} is not in a voice channel.")
        if member.voice.channel.id == channel.id:
            return await ctx.deny(f"{member.mention} is already in your channel.")
        permissions = channel.permissions_for(ctx.me)
        source_permissions = member.voice.channel.permissions_for(ctx.me)
        if not source_permissions.move_members or not permissions.connect:
            return await ctx.deny("I need the `Move Members` permission to drag members.")
        try:
            if data["locked"] and not channel.permissions_for(member).connect:
                await channel.set_permissions(
                    member, view_channel=True, connect=True, reason="VoiceMaster: dragged in"
                )
            await member.move_to(channel, reason=f"VoiceMaster: dragged by {ctx.author}")
        except discord.Forbidden:
            return await ctx.deny(
                f"I can't move {member.mention} (they may have a higher role than me)."
            )
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to drag {member.mention}: {error}")
        return await ctx.approve(f"Dragged {member.mention} into **{channel.name}**.")

    @voicemaster.command(
        name="kick",
        description="Kick a member from your voice channel",
        example="voicemaster kick @member",
        usage="kick <member>",
    )
    @cooldown(1, 3, BucketType.user)
    async def kick(self, ctx: Context, member: Member) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if member.id == ctx.author.id:
            return await ctx.deny("You can't kick yourself.")
        if (
            member.voice is None
            or member.voice.channel is None
            or member.voice.channel.id != channel.id
        ):
            return await ctx.deny(f"{member.mention} is not in your voice channel.")
        if not channel.permissions_for(ctx.me).move_members:
            return await ctx.deny(
                "I need the `Move Members` permission to kick members from voice channels."
            )
        try:
            await member.move_to(None, reason=f"VoiceMaster: kicked by {ctx.author}")
        except discord.Forbidden:
            return await ctx.deny(
                f"I can't kick {member.mention} (they may have a higher role than me)."
            )
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to kick {member.mention}: {error}")
        return await ctx.approve(f"Kicked {member.mention} from **{channel.name}**.")

    @voicemaster.command(
        name="invite",
        aliases=["allow"],
        description="Allow a member to join your locked or hidden channel",
        example="voicemaster invite @member",
        usage="invite <member>",
    )
    @cooldown(1, 3, BucketType.user)
    async def invite(self, ctx: Context, member: Member) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if member.id == ctx.author.id:
            return await ctx.deny("You don't need to invite yourself.")
        try:
            await channel.set_permissions(
                member, view_channel=True, connect=True, reason=f"VoiceMaster: invited by {ctx.author}"
            )
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to invite {member.mention}: {error}")
        return await ctx.approve(f"Invited {member.mention} to **{channel.name}**.")

    @voicemaster.command(
        name="uninvite",
        aliases=["revoke"],
        description="Revoke a member's access to your channel",
        example="voicemaster uninvite @member",
        usage="uninvite <member>",
    )
    @cooldown(1, 3, BucketType.user)
    async def uninvite(self, ctx: Context, member: Member) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if member.id == ctx.author.id:
            return await ctx.deny("You can't uninvite yourself.")
        try:
            await channel.set_permissions(
                member, overwrite=None, reason=f"VoiceMaster: uninvited by {ctx.author}"
            )
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to revoke {member.mention}'s access: {error}")
        return await ctx.approve(f"Revoked {member.mention}'s access to **{channel.name}**.")

    @voicemaster.command(
        name="transfer",
        aliases=["give"],
        description="Transfer ownership of your channel to a member inside it",
        example="voicemaster transfer @member",
        usage="transfer <member>",
    )
    async def transfer(self, ctx: Context, member: Member) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        if member.id == ctx.author.id:
            return await ctx.deny("You already own this channel.")
        if (
            member.voice is None
            or member.voice.channel is None
            or member.voice.channel.id != channel.id
        ):
            return await ctx.deny(f"{member.mention} is not in your voice channel.")
        overwrites = {
            ctx.guild.default_role: channel.overwrites_for(ctx.guild.default_role),
            member: PermissionOverwrite(**OWNER_PERMISSIONS),
        }
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, Member) and target.id not in (ctx.author.id, member.id):
                overwrites[target] = overwrite
        try:
            await channel.edit(overwrites=overwrites, reason="VoiceMaster: ownership transferred")
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to transfer the channel: {error}")
        await self.bot.pool.execute(
            "UPDATE voicemaster_channels SET owner_id = %s WHERE channel_id = %s",
            member.id,
            channel.id,
        )
        await self._refresh_interface(channel)
        return await ctx.approve(f"Transferred **{channel.name}** to {member.mention}.")

    @voicemaster.command(
        name="hide",
        description="Hide your channel from everyone except invited members",
        example="voicemaster hide",
    )
    async def hide(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, data = resolved
        if data["hidden"]:
            return await ctx.warn("Your channel is already hidden.")
        try:
            await self._apply_everyone_overwrite(channel, locked=bool(data["locked"]), hidden=True)
            await self._set_channel_flags(ctx.guild.id, channel.id, hidden=True)
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to hide the channel: {error}")
        return await ctx.approve(f"**{channel.name}** is now hidden from everyone else.")

    @voicemaster.command(
        name="unhide",
        description="Make your hidden channel visible to everyone again",
        example="voicemaster unhide",
    )
    async def unhide(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, data = resolved
        if not data["hidden"]:
            return await ctx.warn("Your channel is already visible.")
        try:
            await self._apply_everyone_overwrite(channel, locked=bool(data["locked"]), hidden=False)
            await self._set_channel_flags(ctx.guild.id, channel.id, hidden=False)
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to unhide the channel: {error}")
        return await ctx.approve(f"**{channel.name}** is now visible to everyone.")

    @voicemaster.command(
        name="info",
        aliases=["stats"],
        description="Show information about the VoiceMaster channel you're in",
        example="voicemaster info",
    )
    async def info(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")
        config = await self._get_config(ctx.guild.id)
        if config is None:
            return await ctx.deny("VoiceMaster is not set up in this server.")
        voice = ctx.author.voice
        if voice is None or voice.channel is None:
            return await ctx.deny("You need to join a voice channel first.")
        channel = voice.channel
        data = await self._get_channel_data(ctx.guild.id, channel.id)
        if data is None:
            return await ctx.deny("This is not a VoiceMaster channel.")
        owner = ctx.guild.get_member(data["owner_id"])
        region = channel.rtc_region
        embed = Embed(color=COLORS.neutral, title=f"🎙️ {channel.name}")
        embed.add_field(
            name="Owner",
            value=owner.mention if owner else f"Unknown ({data['owner_id']})",
            inline=True,
        )
        embed.add_field(
            name="Status",
            value="👻 Hidden" if data["hidden"] else ("🔒 Locked" if data["locked"] else "🔓 Unlocked"),
            inline=True,
        )
        embed.add_field(
            name="Region",
            value=REGION_LABELS.get(region, "Automatic") if region else "Automatic",
            inline=True,
        )
        embed.add_field(name="Bitrate", value=f"{max(channel.bitrate // 1000, 1)} kbps", inline=True)
        embed.add_field(
            name="User limit",
            value=str(channel.user_limit) if channel.user_limit else "No limit",
            inline=True,
        )
        embed.add_field(name="Members", value=str(len(channel.members)), inline=True)
        if channel.members:
            embed.add_field(
                name="In channel",
                value=", ".join(voice_member.display_name for voice_member in channel.members[:25]),
                inline=False,
            )
        embed.set_footer(text=f"Channel ID: {channel.id}")
        return await ctx.send(embed=embed)

    @voicemaster.command(
        name="settings",
        description="Show the VoiceMaster configuration for this server",
        example="voicemaster settings",
    )
    async def settings(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")
        config = await self._get_config(ctx.guild.id)
        if config is None:
            return await ctx.deny(
                "VoiceMaster is not set up in this server. An admin needs to run `voicemaster setup` first."
            )
        category = ctx.guild.get_channel(config["category_id"])
        interface = ctx.guild.get_channel(config["interface_id"])
        create_channel = ctx.guild.get_channel(config["create_channel_id"])
        active = await self.bot.pool.fetchval(
            "SELECT COUNT(*) FROM voicemaster_channels WHERE guild_id = %s",
            ctx.guild.id,
        )
        embed = Embed(color=COLORS.neutral, title="VoiceMaster settings")
        embed.add_field(name="Category", value=category.mention if category else "Unknown", inline=True)
        embed.add_field(name="Interface", value=interface.mention if interface else "Unknown", inline=True)
        embed.add_field(
            name="Join To Create",
            value=create_channel.mention if create_channel else "Unknown",
            inline=True,
        )
        if isinstance(create_channel, discord.VoiceChannel):
            region = create_channel.rtc_region
            embed.add_field(
                name="Template bitrate",
                value=f"{max(create_channel.bitrate // 1000, 1)} kbps",
                inline=True,
            )
            embed.add_field(
                name="Template user limit",
                value=str(create_channel.user_limit) if create_channel.user_limit else "No limit",
                inline=True,
            )
            embed.add_field(
                name="Template region",
                value=REGION_LABELS.get(region, "Automatic") if region else "Automatic",
                inline=True,
            )
        embed.add_field(name="Active channels", value=str(active or 0), inline=True)
        embed.set_footer(
            text="New channels inherit the bitrate, user limit and region of Join To Create"
        )
        return await ctx.send(embed=embed)

    @voicemaster.command(
        name="reset",
        description="Reset your channel to the default settings",
        example="voicemaster reset",
    )
    async def reset(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        config = await self._get_config(ctx.guild.id)
        create_channel = ctx.guild.get_channel(config["create_channel_id"]) if config else None
        if isinstance(create_channel, discord.VoiceChannel):
            bitrate = create_channel.bitrate
            user_limit = create_channel.user_limit
            rtc_region = create_channel.rtc_region
        else:
            bitrate = DEFAULT_BITRATE * 1000
            user_limit = 0
            rtc_region = None
        overwrites = {
            ctx.guild.default_role: PermissionOverwrite(view_channel=True, connect=True),
            ctx.author: PermissionOverwrite(**OWNER_PERMISSIONS),
        }
        options = dict(
            bitrate=bitrate,
            user_limit=user_limit,
            rtc_region=rtc_region,
            overwrites=overwrites,
            reason="VoiceMaster: channel reset",
        )
        try:
            await channel.edit(**options)
            await self._set_channel_flags(ctx.guild.id, channel.id, locked=False, hidden=False)
            await self._refresh_interface(channel)
        except discord.HTTPException as error:
            return await ctx.deny(f"Failed to reset the channel: {error}")
        return await ctx.approve(f"Reset **{channel.name}** to the default settings.")

    @voicemaster.command(
        name="delete",
        aliases=["remove", "close"],
        description="Delete your voice channel",
        example="voicemaster delete",
    )
    async def delete(self, ctx: Context) -> Message:
        resolved = await self._resolve_owner_channel(ctx)
        if resolved is None:
            return
        channel, _ = resolved
        await self.delete_channel(channel, reason=f"VoiceMaster: deleted by {ctx.author}")
        return await ctx.approve(f"Deleted **{channel.name}**.")

    @voicemaster.command(
        name="list",
        aliases=["channels"],
        description="List every active VoiceMaster channel in this server",
        example="voicemaster list",
    )
    async def list_channels(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")
        config = await self._get_config(ctx.guild.id)
        if config is None:
            return await ctx.deny("VoiceMaster is not set up in this server.")
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, owner_id FROM voicemaster_channels WHERE guild_id = %s ORDER BY created_at DESC",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.warn("There are no active VoiceMaster channels.")
        embeds: List[Embed] = []
        page_size = 10
        for index in range(0, len(rows), page_size):
            chunk = rows[index : index + page_size]
            lines = []
            for row in chunk:
                channel = ctx.guild.get_channel(row["channel_id"])
                owner = ctx.guild.get_member(row["owner_id"])
                name = channel.mention if channel else "Unknown channel"
                owner_name = owner.mention if owner else "Unknown"
                lines.append(f"{name} — owned by {owner_name}")
            embed = Embed(
                color=COLORS.neutral,
                title="VoiceMaster channels",
                description="\n".join(lines),
            )
            embed.set_footer(text=f"Page {index // page_size + 1}/{(len(rows) + page_size - 1) // page_size}")
            embeds.append(embed)
        return await ctx.paginate(embeds)
