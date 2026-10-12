from typing import Union
import requests
import io
import time
import aiohttp
import asyncio
import re

from collections import defaultdict
from base.context import Confirmation
from base.managers.types import CogMeta
from base.context import Context
from base.managers.paginator import *
from base.config import *
from random import random, choice
from datetime import timedelta
from humanize import naturaltime
from humanfriendly import format_timespan
import humanize
from datetime import datetime
import humanfriendly
import time

from discord import (
    Embed,
    User,
    Member,
    Message,
    Spotify,
    ActivityType,
    Permissions,
    Status,
    Invite,
    Role,
    Button,
    ButtonStyle,
    TextChannel,
    ui,
    Interaction,
    VoiceChannel,
)
from discord.app_commands import (
    allowed_installs,
    allowed_contexts,
)
from discord.ext.commands import (
    cooldown,
    BucketType,
    Author,
    Cog,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.ui import View, button
import discord
from discord.utils import format_dt, oauth_url, utcnow
from base.managers.predicates import has_permissions

from psutil import Process
from difflib import get_close_matches

from PIL import Image
from colorthief import ColorThief


class Moderation(CogMeta):
    role_lock = defaultdict(asyncio.Lock)

    @hybrid_command(
        name="ban",
        aliases=["evict", "deport"],
        description="Ban a member from the server",
        example="ban @voby spamming",
    )
    @has_permissions(ban_members=True)
    async def ban(
        self,
        ctx: Context,
        user: Union[Member, User],
        *,
        reason: str = "No reason provided.",
    ):
        reason += f" | Executed by {ctx.author}"

        if isinstance(user, Member):
            if user == ctx.guild.owner:  # type: ignore
                return await ctx.warn("You're unable to ban the **server owner.**")
            if user == ctx.author:
                return await ctx.warn("You're unable to ban **yourself**.")
            if ctx.author.top_role.position <= user.top_role.position:  # type: ignore
                return await ctx.warn(
                    "You're unable to ban a user with a **higher role** than **yourself.**"
                )

            if user.premium_since:
                confirmation_view = Confirmation(
                    ctx, user=user, reason=reason, action="ban"
                )
                await confirmation_view.send_confirmation()
            else:
                await user.ban(reason=reason, delete_message_days=7)
                return await ctx.approve(f"{user.mention} has been **banned**.")
        else:
            await ctx.guild.ban(user, reason=reason)  # type: ignore
            return await ctx.approve(f"{user.mention} has been **banned**.")

    @hybrid_command(name="kick", description="Kicks a user from the server", example="kick @voby spamming")
    @has_permissions(kick_members=True)
    async def kick(
        self, ctx: Context, user: Member, *, reason: str = "No reason provided."
    ):
        reason += f" | Executed by {ctx.author}"

        if isinstance(user, Member):
            if user == ctx.guild.owner:  # type: ignore
                return await ctx.warn("You're unable to kick the **server owner.**")
            if user == ctx.author:
                return await ctx.warn("You're unable to kick **yourself")
            if ctx.author.top_role.position <= user.top_role.position:  # type: ignore
                return await ctx.warn(
                    "You're unable to kick a user with a **higher role** than **yourself.**"
                )

            if user.premium_since:
                confirmation_view = Confirmation(
                    ctx, user=user, reason=reason, action="kick"
                )
                await confirmation_view.send_confirmation()
            else:
                await user.kick(reason=reason)
                return await ctx.approve(f"{user.mention} has been **kicked**.")

    @hybrid_command(name="nuke", description="Nukes the channel", example="nuke #channel")
    @has_permissions(manage_guild=True)
    async def nuke(self, ctx: Context, *, channel: TextChannel = None):  # type: ignore
        if channel is None:
            channel = ctx.channel

        confirmation = NukeConfirm(ctx, channel=channel)
        await confirmation.send_confirmation()

    @hybrid_command(
        name="forcenickname",
        aliases=["fn"],
        description="force a nickname upon a user.",
        example="forcenickname @voby New Name",
    )
    @has_permissions(moderate_members=True)
    async def forcenickname(
        self,
        ctx: Context,
        user: discord.Member,
        *,
        name: str = None,  # type: ignore
    ):
        if name is None:
            check = await self.bot.pool.fetchrow(
                "SELECT name FROM forcenick WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                user.id,
            )
            if check and check["name"]:
                await self.bot.pool.execute(
                    "DELETE FROM forcenick WHERE guild_id = %s AND user_id = %s",
                    ctx.guild.id,
                    user.id,
                )
                await user.edit(nick=None)
                return await ctx.approve(
                    f"Removed **forced nickname** for {user.mention}!"
                )
            else:
                return await ctx.deny(f"No forced nickname found for {user.mention}.")
        else:
            check = await self.bot.pool.fetchrow(
                "SELECT * FROM forcenick WHERE user_id = %s AND guild_id = %s",
                user.id,
                ctx.guild.id,  # type: ignore
            )
            if check is None:
                await self.bot.pool.execute(
                    "INSERT INTO forcenick VALUES (%s,%s,%s)",
                    ctx.guild.id,  # type: ignore
                    user.id,
                    name,
                )
            else:
                await self.bot.pool.execute(
                    "UPDATE forcenick SET name = %s WHERE user_id = %s AND guild_id = %s",
                    name,
                    user.id,
                    ctx.guild.id,  # type: ignore
                )
            await user.edit(nick=name)
            return await ctx.approve(
                f"Now **forcing nickname** for **{user.name}** to `{name}`"
            )

    @Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if str(before.nick) != str(after.nick):
            check = await self.bot.pool.fetchrow(
                "SELECT name FROM forcenick WHERE user_id = %s AND guild_id = %s",
                before.id,
                before.guild.id,
            )
            if check:
                return await before.edit(nick=check["name"])

    @hybrid_group(
        name="lockdown",
        aliases=["lock"],
        invoke_without_command=True,
        description="Locks the channel",
        example="lockdown #channel raid",
    )
    @has_permissions(manage_channels=True)
    async def lockdown(
        self,
        ctx: Context,
        channel: TextChannel = None,
        *,
        reason: str = "Locked channel",
    ):  # type: ignore
        if channel is None:
            channel = ctx.channel

        lockdown_role_id = await self.bot.pool.fetchval(
            """
            SELECT role_id
            FROM lockdown
            WHERE guild_id = %s
            """,
            ctx.guild.id,
        )

        if lockdown_role_id:
            lockdown_role = ctx.guild.get_role(lockdown_role_id)
        else:
            lockdown_role = ctx.guild.default_role

        overwrite = channel.overwrites_for(lockdown_role)
        overwrite.send_messages = False
        await channel.set_permissions(lockdown_role, overwrite=overwrite, reason=reason)

        await ctx.message.add_reaction("🔒")

    @lockdown.command(name="all", description="Locks all the channels.")
    @has_permissions(manage_channels=True)
    async def lockdown_all(self, ctx: Context):
        lockdown_role_id = await self.bot.pool.fetchval(
            """
            SELECT role_id
            FROM lockdown
            WHERE guild_id = %s
            """,
            ctx.guild.id,
        )

        if lockdown_role_id:
            lockdown_role = ctx.guild.get_role(lockdown_role_id)
        else:
            lockdown_role = ctx.guild.default_role
        message = await ctx.reply(
            embed=Embed(
                description=f"{EMOJIS.LOADING} Locking all channels...",
                color=COLORS.neutral,
            ),
        )
        ignored_channels = await self.bot.pool.fetch(
            "SELECT channel_id FROM lockdown WHERE guild_id = %s", ctx.guild.id
        )  # type: ignore
        ignored_channel_ids = {record["channel_id"] for record in ignored_channels}

        locked_channels = 0
        for channel in ctx.guild.channels:  # type: ignore
            if channel.id in ignored_channel_ids:
                continue

            overwrite = channel.overwrites_for(lockdown_role)  # type: ignore
            overwrite.send_messages = False
            await channel.set_permissions(
                lockdown_role,
                overwrite=overwrite,
                reason=f"{ctx.author} locked down all channels.",
            )  # type: ignore
            locked_channels += 1

        await message.delete()
        return await ctx.approve(f"**{locked_channels} channels** have been locked.")

    @lockdown.command(name="ignore", description="Add a channel to the ignored list.", example="lockdown ignore #channel")
    @has_permissions(manage_channels=True)
    async def lockdown_ignore(self, ctx: Context, *, channel: TextChannel):
        result = await self.bot.pool.fetch(
            "SELECT channel_id FROM lockdown WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,  # type: ignore
            channel.id,
        )
        if result:
            await self.bot.pool.execute(
                "DELETE FROM lockdown WHERE guild_id = %s AND channel_id = %s",
                ctx.guild.id,  # type: ignore
                channel.id,
            )
            await ctx.approve(
                f"I will stop **ignoring** {channel.mention} from **lockdown** commands."
            )
        else:
            await self.bot.pool.execute(
                "INSERT INTO lockdown (guild_id, channel_id) VALUES (%s, %s)",
                ctx.guild.id,  # type: ignore
                channel.id,
            )
            await ctx.approve(
                f"I will now **ignore** {channel.mention} from **lockdown** commands."
            )

    @lockdown.command(name="ignored", description="See a list of ignored channels.")
    @has_permissions(manage_channels=True)
    async def lockdown_ignored(self, ctx: Context):
        ignored_channels = await self.bot.pool.fetch(
            "SELECT channel_id FROM lockdown WHERE guild_id = %s", ctx.guild.id
        )  # type: ignore

        if not ignored_channels:
            return await ctx.warn(
                "No channels are currently ignored in lockdown commands."
            )

        entries = []
        count = 0

        for i, record in enumerate(ignored_channels, start=1):
            channel = ctx.guild.get_channel(record["channel_id"])  # type: ignore
            channel_name = (
                channel.mention
                if channel
                else f"Unknown Channel (ID: {record['channel_id']})"
            )
            entries.append(f"`{i}`  {channel_name}")

        embeds = []
        embed = discord.Embed(
            color=COLORS.neutral, title=f"Ignored Channels", description=""
        )
        embed.set_footer(text=f"Page 1/1 (entries: {len(entries)})")

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(embed)
                embed = discord.Embed(
                    color=COLORS.neutral, description="", title=f"Ignored Channels"
                )
                count = 0

        if count > 0:
            embeds.append(embed)

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0])

    @lockdown.command(name="role", description="Set the default lock role.", example="lockdown role @Locked")
    @has_permissions(manage_channels=True)
    async def lockdown_role(self, ctx: Context, *, role: Role):
        if await self.bot.pool.fetchval(
            """
            SELECT role_id
            FROM lockdown
            WHERE guild_id = %s AND role_id = %s
            """,
            ctx.guild.id,
            role.id,
        ):
            return await ctx.warn(f"{role.mention} is already the **lockdown role**.")

        await self.bot.pool.execute(
            """
            INSERT INTO lockdown (guild_id, role_id)
            VALUES (%s, %s)
            """,
            ctx.guild.id,
            role.id,
        )
        return await ctx.approve(
            f"{role.mention} is now set as the **default lock role**."
        )

    @hybrid_group(name="unlock", invoke_without_command=True, description="Unlocks a channel.", example="unlock #channel raid over")
    @has_permissions(manage_channels=True)
    async def unlock(
        self,
        ctx: Context,
        channel: TextChannel = None,
        *,
        reason: str = "Unlocked channel",
    ):  # type: ignore
        if channel is None:
            channel = ctx.channel
        lockdown_role_id = await self.bot.pool.fetchval(
            """
            SELECT role_id
            FROM lockdown
            WHERE guild_id = %s
            """,
            ctx.guild.id,
        )

        if lockdown_role_id:
            lockdown_role = ctx.guild.get_role(lockdown_role_id)
        else:
            lockdown_role = ctx.guild.default_role

        overwrite = channel.overwrites_for(lockdown_role)
        overwrite.send_messages = True
        await channel.set_permissions(lockdown_role, overwrite=overwrite, reason=reason)

        await ctx.message.add_reaction("🔓")

    @unlock.command(name="all", description="Unlocks all the channels")
    @has_permissions(manage_channels=True)
    async def unlock_all(self, ctx: Context):
        lockdown_role_id = await self.bot.pool.fetchval(
            """
            SELECT role_id
            FROM lockdown
            WHERE guild_id = %s
            """,
            ctx.guild.id,
        )

        if lockdown_role_id:
            lockdown_role = ctx.guild.get_role(lockdown_role_id)
        else:
            lockdown_role = ctx.guild.default_role
        message = await ctx.reply(
            embed=Embed(
                description=f"{EMOJIS.LOADING} Unlocking all channels... ",
                color=COLORS.neutral,
            )
        )
        ignored_channels = await self.bot.pool.fetch(
            "SELECT channel_id FROM lockdown WHERE guild_id = %s", ctx.guild.id
        )  # type: ignore
        ignored_channel_ids = {record["channel_id"] for record in ignored_channels}

        unlocked_channels = 0
        for channel in ctx.guild.channels:  # type: ignore
            if channel.id in ignored_channel_ids:
                continue

            overwrite = channel.overwrites_for(lockdown_role)  # type: ignore
            overwrite.send_messages = True
            await channel.set_permissions(
                lockdown_role,
                overwrite=overwrite,
                reason=f"{ctx.author} unlocked all channels.",
            )  # type: ignore
            unlocked_channels += 1

        await message.delete()
        return await ctx.approve(
            f"**{unlocked_channels} channels** have been unlocked."
        )

    @hybrid_group(
        name="purge",
        description="Purge messages.",
        aliases=["c"],
        invoke_without_command=True,
        example="purge 50",
    )
    @has_permissions(manage_messages=True)
    async def purge(self, ctx: Context, *, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            check=lambda m: not m.pinned,
            reason=f"Purged by {ctx.author.name}",
        )

    @purge.command(
        name="user",
        description="Purge messages sent by a certain user.",
        aliases=["member"],
        example="purge user @voby 50",
    )
    @has_permissions(manage_messages=True)
    async def purge_user(self, ctx: Context, user: discord.Member, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: m.author == user and not m.pinned,
        )

    @purge.command(name="bots", description="Purge messages sent by bots", example="purge bots 50")
    @has_permissions(manage_messages=True)
    async def purge_bots(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: m.author.bot and not m.pinned,
        )

    @purge.command(name="links", description="Purges messages containing links.", example="purge links 50")
    @has_permissions(manage_messages=True)
    async def purge_links(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()

        def links(message: discord.Message):
            match = re.search(
                r"(http|ftp|https):\/\/([\w_-]+(?:(?:\.[\w_-]+)+))([\w.,@?^=%&:\/~+#-]*[\w@?^=%&\/~+#-])",
                message.content,
            )

            return message.embeds or match

        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=links,  # type: ignore
        )

    @purge.command(
        name="attachments",
        description="Purge messages containing attachments.",
        aliases=["images", "pictures", "files"],
        example="purge attachments 50",
    )
    @has_permissions(manage_messages=True)
    async def purge_attachments(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: m.attachments,  # type: ignore
        )

    @purge.command(
        name="humans",
        description="Purges messages sent by humans.",
        aliases=["members"],
        example="purge humans 50",
    )
    @has_permissions(manage_messages=True)
    async def purge_humans(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: not m.author.bot,
        )

    @purge.command(
        name="stickers",
        description="Purges messages containing stickers",
        aliases=["sticker"],
        example="purge stickers 50",
    )
    @has_permissions(manage_messages=True)
    async def purge_stickers(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: m.stickers,  # type: ignore
        )

    @purge.command(name="mentions", description="Purges messages containing mentions.", example="purge mentions 50")
    @has_permissions(manage_messages=True)
    async def purge_mentions(self, ctx: Context, amount: int = 15):
        await ctx.message.delete()
        await ctx.channel.purge(  # type: ignore
            limit=amount,
            bulk=True,
            reason=f"Purged by {ctx.author.name}",
            check=lambda m: m.mentions,  # type: ignore
        )

    @hybrid_command(
        name="stripstaff",
        aliases=["strip"],
        description="Strip dangerous roles from a user",
        example="stripstaff @member",
    )
    @has_permissions(administrator=True)
    async def stripstaff(self, ctx: Context, *, member: Member):
        try:
            dangerous_roles_found = False
            stripped_roles = []
            message = await ctx.send(
                embed=Embed(
                    description=f"{EMOJIS.LOADING} {ctx.author.mention}: Stripping roles from {member.mention} now..."
                )
            )

            for role in member.roles:
                if ctx.is_dangerous(role):
                    await member.remove_roles(
                        role, reason=f"{ctx.author} stripped dangerous permissions."
                    )
                    dangerous_roles_found = True
                    stripped_roles.append(role)

            if not dangerous_roles_found:
                return await ctx.warn(f"There are no **dangerous** roles to strip.")

            await message.delete()

            stripped_roles_mentions = (
                ", ".join([role.mention for role in stripped_roles])
                if stripped_roles
                else "None"
            )
            return await ctx.approve(
                f"Removed {member.mention} from: {stripped_roles_mentions}."
            )
        except Exception as E:
            return await ctx.warn(f"An error occurred: \n```{E}```")

    @hybrid_command(name="imute", description="Remove a members image permissions", example="imute @member")
    @has_permissions(moderate_members=True)
    async def imute(self, ctx: Context, *, member: Member):
        await ctx.channel.set_permissions(member, attach_files=False, embed_links=False)  # type: ignore
        return await ctx.embed(
            description=f"{ctx.author.mention}: Removed **attach files & embed links** from **{member.name}**",
            color=0xE60000,
        )

    @hybrid_command(name="iunmute", description="Restore a members image permissions", example="iunmute @member")
    @has_permissions(moderate_members=True)
    async def iunmute(self, ctx: Context, *, member: Member):
        await ctx.channel.set_permissions(member, attach_files=True, embed_links=True)  # type: ignore
        return await ctx.embed(
            description=f"{ctx.author.mention}: Restored **attach files & embed links** to **{member.name}**",
            color=0xE60000,
        )

    @hybrid_command(
        name="reactionmute",
        aliases=["rmute"],
        description="Remove a members reaction permissions",
        example="reactionmute @member",
    )
    @has_permissions(moderate_members=True)
    async def reactionmute(self, ctx: Context, *, member: Member):
        await ctx.channel.set_permissions(
            member, add_reactions=False, use_external_emojis=False
        )  # type: ignore
        return await ctx.embed(
            description=f"Removed {member.mention}'s permissions to **react** and use **external emotes**",
            color=COLORS.red,
        )

    @hybrid_command(
        name="reactionunmute",
        aliases=["runmute"],
        description="Restore a members reaction permissions",
        example="reactionunmute @member",
    )
    @has_permissions(moderate_members=True)
    async def reactionunmute(self, ctx: Context, *, member: Member):
        await ctx.channel.set_permissions(
            member, add_reactions=True, use_external_emojis=True
        )  # type: ignore
        return await ctx.embed(
            description=f"Restored {member.mention}'s permissions to **react** and use **external emotes**",
            color=COLORS.red,
        )

    @hybrid_group(
        name="role",
        aliases=["r"],
        invoke_without_command=True,
        description="Role a user.",
        example="role @voby @role",
    )
    @has_permissions(manage_roles=True)
    async def role(self, ctx: Context, user: Member, role: Union[Role, str]):
        if isinstance(role, str):
            role = ctx.guild.get_role(role)

        if role in user.roles:
            await user.remove_roles(role)
            await ctx.embed(
                description=f"{EMOJIS.REMOVE} {ctx.author.mention}: Removed {role.mention} from {user.mention}",
                color=0x38A9E1,
            )
        else:
            await user.add_roles(role)
            await ctx.embed(
                description=f"{EMOJIS.ADD} {ctx.author.mention}: Added {role.mention} to {user.mention}",
                color=0x38A9E1,
            )

    @role.command(name="create", aliases=["add"], description="Create a role", example="role create New Role")
    @has_permissions(manage_roles=True)
    async def role_create(self, ctx: Context, *, name: str):
        role = await ctx.guild.create_role(name=name)
        return await ctx.approve(f"Created **role** {role.mention}")

    @role.command(name="delete", aliases=["del"], description="Delete a role", example="role delete @role")
    @has_permissions(manage_roles=True)
    async def role_delete(self, ctx: Context, *, role: Role):
        await role.delete()
        return await ctx.approve(f"Deleted **role** `{role.name}`")

    @role.command(name="all", description="Give everyone a role.", example="role all @role")
    @has_permissions(manage_roles=True)
    async def role_all(self, ctx: Context, role: Role):
        async with self.role_lock[ctx.guild.id]:
            tasks = [
                m.add_roles(role, reason=f"Role all invoked by {ctx.author}")
                for m in ctx.guild.members
                if not role in m.roles
            ]

        if len(tasks) == 0:
            return await ctx.warn(f"Everyone has this role!")

        message = await ctx.cooldown(
            f"Giving {role.mention} to **{len(tasks)}** members. This may take around **{format_timespan(0.3 * len(tasks))}**"
        )

        await asyncio.gather(*tasks)
        await message.delete()
        return await ctx.embed(
            description=f"{EMOJIS.ADD} {ctx.author.mention}: Added {role.mention} to **{len(tasks)}** members.",
            color=0x38A9E1,
        )

    @role.command(name="color", aliases=["colour"], description="Edit a role's colour", example="role color #ff0000 @role")
    @has_permissions(manage_roles=True)
    async def role_color(self, ctx: Context, hex: str, *, role: Role):
        if not re.match(r"^#[0-9A-Fa-f]{6}$", hex):
            return await ctx.warn(f"Invalid **hex code**.")

        if role.position >= ctx.me.top_role.position:
            return await ctx.warn(
                "I cannot modify this role, as it has a **higher** role than me."
            )

        color = discord.Color(int(hex.lstrip("#"), 16))

        await role.edit(color=color)
        return await ctx.approve(f"Role {role.mention}'s **color** changed to `{hex}`")

    @role.command(name="humans", description="Give all humans a role.", example="role humans @role")
    @has_permissions(manage_roles=True)
    async def role_humans(self, ctx: Context, *, role: Role):
        async with self.role_lock[ctx.guild.id]:
            tasks = [
                m.add_roles(role, reason=f"Role added by {ctx.author}")
                for m in ctx.guild.members
                if not m.bot and role not in m.roles
            ]

        if len(tasks) == 0:
            return await ctx.warn(f"All **humans** have this role.")

        message = await ctx.cooldown(
            f"Giving {role.mention} to **{len(tasks)}** members. This may take around **{format_timespan(0.3 * len(tasks))}**"
        )

        await asyncio.gather(*tasks)
        await message.delete()

        return await ctx.embed(
            description=f"{EMOJIS.ADD} {ctx.author.mention}: Added {role.mention} to **{len(tasks)}** human members.",
            color=0x38A9E1,
        )

    @role.command(name="hoist", description="Hoist / unhoist a role", example="role hoist @role")
    @has_permissions(manage_roles=True)
    async def role_hoist(self, ctx: Context, *, role: Role):
        if role.hoist == False:
            await role.edit(hoist=True)
            return await ctx.message.add_reaction("✅")
        else:
            await role.edit(hoist=False)
            return await ctx.message.add_reaction("✅")

    @role.command(name="mentionable", description="Edit a roles mentionability.", example="role mentionable @role")
    @has_permissions(manage_roles=True)
    async def role_mentionable(self, ctx: Context, *, role: Role):
        if role.hoist == False:
            await role.edit(mentionable=True)
            return await ctx.message.add_reaction("✅")
        else:
            await role.edit(mentionable=False)
            return await ctx.message.add_reaction("✅")

    @hybrid_command(name="unban", description="Unbans a user", example="unban 123456789012345678")
    @has_permissions(moderate_members=True)
    async def unban(self, ctx: Context, *, user: User):
        await ctx.guild.unban(user)
        return await ctx.approve(f"{user.mention} has been **unbanned.**")

    @hybrid_command(name="mute", description="mute a member", example="mute @member 10m")
    @has_permissions(moderate_members=True)
    async def mute(
        self, ctx: Context, member: Member, *, time: str = "60s"
    ):
        if member.id == self.bot.user.id:
            return await ctx.deny("I cannot **mute** myself.")

        if member.id == ctx.author.id:
            return await ctx.deny("You cannot **mute** yourself.")

        if ctx.author.id != ctx.guild.owner_id:
            if member.top_role.position >= ctx.guild.me.top_role.position:
                return await ctx.warn(
                    "You cannot **mute** a member with a higher role than me."
                )
            if member.top_role.position >= ctx.author.top_role.position:
                return await ctx.warn(
                    "You cannot **mute** a member with a higher role than you."
                )
        else:
            pass

        time = humanfriendly.parse_timespan(time)
        try:
            await member.timeout(
                utcnow() + timedelta(seconds=time),
                reason=f"User timed out by {ctx.author}",
            )
            return await ctx.approve(f"**{member.name}** is now muted for {humanfriendly.format_timespan(time)}")
        except Exception as E:
            return await ctx.warn(f"I'm **unable** to mute this user.")

    @hybrid_command(name="unmute", description="Unmute a member", example="unmute @member")
    @has_permissions(moderate_members=True)
    async def unmute(self, ctx: Context, *, member: Member):
        if member.id == self.bot.user.id:
            return await ctx.deny("I cannot **unmute** myself.")

        if member.id == ctx.author.id:
            return await ctx.deny("You cannot **unmute** yourself.")

        if ctx.author.id != ctx.guild.owner_id:
            if member.top_role.position >= ctx.guild.me.top_role.position:
                return await ctx.warn(
                    "You cannot **unmute** a member with a higher role than me."
                )
            if member.top_role.position >= ctx.author.top_role.position:
                return await ctx.warn(
                    "You cannot **unmute** a member with a higher role than you."
                )
        else:
            pass

        await member.timeout(None)
        return await ctx.embed(
            description=f"{ctx.author.mention}: **{member.name}** is now unmuted.",
            color=COLORS.red,
        )

    @hybrid_group(name="channel", invoke_without_command=True, description="Edit channels.")
    @has_permissions(manage_channels=True)
    async def channel(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @channel.command(
        name="create", aliases=["add", "make"], description="Create a channel",
        example="channel create new-channel",
    )
    @has_permissions(manage_channels=True)
    async def channel_create(self, ctx: Context, *, name: str):
        await ctx.guild.create_text_channel(name=name)
        await ctx.message.add_reaction("✅")

    @channel.command(name="delete", description="Deletes a channel", example="channel delete #channel")
    @has_permissions(manage_channels=True)
    async def channel_delete(
        self, ctx: Context, *, channel: Union[TextChannel, VoiceChannel]
    ):
        try:
            await channel.delete(reason=f"Deleted by {ctx.author}")
            await ctx.message.add_reaction("✅")
        except Exception as E:
            return await ctx.warn(f"I cannot delete {channel.mention}")

    @channel.command(name="rename", description="Rename a channel", example="channel rename #channel new-name")
    @has_permissions(manage_channels=True)
    async def channel_rename(self, ctx: Context, channel: TextChannel, *, new: str):
        await channel.edit(name=new)
        return await ctx.message.add_reaction("✅")

    @channel.command(name="nsfw", description="Mark a channel as NSFW / SFW", example="channel nsfw #channel")
    @has_permissions(manage_channels=True)
    async def channel_nsfw(self, ctx: Context, *, channel: TextChannel):
        await channel.edit(
            nsfw=not channel.is_nsfw(),
        )
        return await ctx.approve(
            f"{channel.mention} has been marked as **{'NSFW' if channel.is_nsfw() else 'SFW'}**."
        )

    @hybrid_command(name="reveal", description="Reveal a channel that's been hidden.", example="reveal #channel")
    @has_permissions(manage_channels=True)
    async def reveal(self, ctx: Context, *, channel: TextChannel = None):
        if channel is None:
            channel = ctx.channel

        await channel.set_permissions(ctx.guild.default_role, view_channel=True)
        return await ctx.approve(f"**{channel.name}** has now been revealed.")

    @hybrid_command(name="hide", description="Hide a channel..", example="hide #channel")
    @has_permissions(manage_channels=True)
    async def hide(self, ctx: Context, *, channel: TextChannel = None):
        if channel is None:
            channel = ctx.channel

        await channel.set_permissions(ctx.guild.default_role, view_channel=False)
        return await ctx.approve(f"**{channel.name}** has now been hidden.")


class NukeConfirm(View):
    def __init__(self, ctx: Context, channel: TextChannel):
        super().__init__()
        self.ctx = ctx
        self.channel = channel
        self.message = None

    async def send_confirmation(self):
        embed = Embed(
            title="",
            description=f"Are you sure you want to nuke {self.channel.mention}?",
            color=COLORS.neutral,
        )
        self.message = await self.ctx.send(embed=embed, view=self)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message(
                "You cannot confirm this action.", ephemeral=True
            )
            return

        nukedchannel = await self.channel.clone()

        await nukedchannel.edit(
            position=self.channel.position,
            topic=self.channel.topic,  # type: ignore
            overwrites=self.channel.overwrites,
        )
        q = [
        "UPDATE welcome SET channel_id = %s WHERE channel_id = %s",
        "UPDATE sticky_messages SET channel_id = %s WHERE channel_id = %s",
        ]
        for query in q:
            await self.ctx.bot.pool.execute(query, nukedchannel.id, self.channel.id)
        await self.channel.delete()
        await nukedchannel.send(f"First!")

        if self.message:
            await self.message.delete()
        self.stop()

    @button(label="No", style=discord.ButtonStyle.red)  # type: ignore
    async def no_button(self, interaction: Interaction, button: Button):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message(
                "You cannot cancel this action.", ephemeral=True
            )
            return
        await self.ctx.approve(f"Nuke action has been **cancelled**.")
        if self.message:
            await self.message.delete()
        self.stop()
