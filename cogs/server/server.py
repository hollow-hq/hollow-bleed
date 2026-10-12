from typing import Union, Optional

import requests
import io
import time
import aiohttp
import re
from re import compile
import asyncio

from base.context import Confirmation
from base.managers.types import CogMeta
from base.context import Context
from base.managers.paginator import *
from base.config import *
from random import random, choice
from humanize import naturaltime
from loguru import logger
import humanize
from datetime import datetime
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
    VoiceChannel,
    CategoryChannel,
    PermissionOverwrite,
    Attachment,
    ui,
    Interaction,
    NotFound,
    HTTPException,
    Forbidden,
)
from discord.ext import tasks, commands
from discord.app_commands import (
    allowed_installs,
    allowed_contexts,
)
from discord.ext.commands import (
    command,
    cooldown,
    BucketType,
    Author,
    Cog,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.ui import View, button
import discord
from discord.utils import format_dt, oauth_url
from base.managers.predicates import has_permissions

from psutil import Process
from difflib import get_close_matches

from PIL import Image
from colorthief import ColorThief
from base.managers.EmbedBuilder import *


def has_br_role():
    async def predicate(ctx: Context):
        check = await ctx.bot.pool.fetchrow(
            "SELECT * FROM booster_roles WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        )
        if not check:
            await ctx.warn(
                f"You do not have a booster role set\nPlease use `{ctx.clean_prefix}br create` to create a booster role"
            )
        return check is not None

    return commands.check(predicate)

def level2():
    async def predicate(ctx: Context):
        if ctx.guild is not None and ctx.guild.premium_tier >= 2:
            return True
        else:
            await ctx.warn("This guild doesn't have level 2 boosts.")
            return False

    return commands.check(predicate)

def br_enabled():
    async def predicate(ctx: Context):
        check = await ctx.bot.pool.fetchrow(
            "SELECT 1 FROM booster_module WHERE guild_id = %s", ctx.guild.id
        )

        if check:
            return True
        else:
            await ctx.warn("The booster role module is not enabled in this guild.")
            return False

    return commands.check(predicate)


class GuildBlacklistView(View):
    """Red 'Blacklist' button attached to customization logs."""

    def __init__(self, bot, guild_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id in self.bot.owner_ids:
            return True

        await interaction.response.send_message(
            "Only bot owners can blacklist a guild.", ephemeral=True
        )
        return False

    @button(label="Blacklist", style=ButtonStyle.red)
    async def blacklist_button(self, button: Button, interaction: Interaction):
        await interaction.response.defer()

        guild = self.bot.get_guild(self.guild_id)

        was_added = await self.bot.guild_blacklist(self.guild_id)

        if guild is not None:
            owner = guild.owner
            if owner:
                try:
                    await owner.send(
                        embed=Embed(
                            color=COLORS.deny,
                            description=(
                                f"Your server **{guild.name}** has been blacklisted "
                                f"from using **{self.bot.user.name}**."
                            ),
                        )
                    )
                except (HTTPException, Forbidden):
                    pass
            try:
                await guild.leave()
            except HTTPException:
                pass

        for child in self.children:
            child.disabled = True

        if was_added:
            message = f"Blacklisted the guild: `{self.guild_id}`"
        else:
            message = f"That guild was already blacklisted: `{self.guild_id}`"

        try:
            await interaction.message.edit(view=self)
        except HTTPException:
            pass

        await interaction.followup.send(message, ephemeral=True)


class Server(CogMeta):
    Sniped = {}
    editSnipe = {}
    reactSnipe = {}
    valid_perms = set(Permissions.VALID_FLAGS)

    def __init__(self, bot):
        super().__init__(bot)
        self.counter_presence_loop.start()

    def cog_unload(self):
        self.counter_presence_loop.cancel()

    @Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self.bot.pool.execute("DELETE FROM br_award WHERE role_id = %s", role.id)

    @Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if (
            not before.guild.premium_subscriber_role in before.roles
            and before.guild.premium_subscriber_role in after.roles
        ):
            if results := await self.bot.pool.fetchrow(
                "SELECT role_id FROM br_award WHERE guild_id = %s", before.guild.id
            ):
                roles = [
                    after.guild.get_role(result["role_id"])
                    for result in results
                    if after.guild.get_role(result["role_id"]).is_assignable()
                ]
                await asyncio.gather(
                    *[
                        after.add_roles(role, reason="Booster role created")
                        for role in roles
                    ]
                )
            elif (
                before.guild.premium_subscriber_role in before.roles
                and not after.guild.premium_subscriber_role in after.roles
            ):
                if results := await self.bot.pool.fetchrow(
                    "SELECT role_id FROM br_award WHERE guild_id = %s", before.guild.id
                ):
                    roles = [
                        after.guild.get_role(
                            result["role_id"]
                            for result in results
                            if after.guild.get_role(result["role_id"]).is_assignable()
                            and after.guild.get_role(result["role_id"]) in after.roles
                        )
                    ]

                    await asyncio.gather(
                        *[
                            after.remove_roles(
                                role, reason="Removed booster role from this member."
                            )
                            for role in roles
                        ]
                    )

    @hybrid_group(
        name="welcome",
        aliases=["welc", "welcomer", "wlc"],
        invoke_without_command=True,
        description="Configure the welcome module",
    )
    @has_permissions(manage_messages=True)
    async def welcome(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @welcome.command(
        name="add", aliases=["create", "set"], description="Add a welcome message",
        example="welcome add #channel Welcome to the server!",
    )
    @has_permissions(manage_messages=True)
    async def welcome_add(
        self, ctx: Context, channel: discord.TextChannel, *, message: str
    ):  # type: ignore

        processed_message = EmbedBuilder.embed_replacement(ctx.author, message)  # type: ignore
        content, embed, view = await EmbedBuilder.to_object(processed_message)

        check = await self.bot.pool.fetchrow(
            """
            SELECT * FROM welcome WHERE channel_id = %s
            """,
            channel.id,
        )
        if check:
            await self.bot.pool.execute(
                """
                UPDATE welcome
                SET message = %s
                WHERE channel_id = %s
                """,
                message,
                channel.id,
            )
            await ctx.approve(f"Edited {channel.mention}'s welcome message to:")
            if content or embed:
                return await ctx.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                return await ctx.send(content=processed_message)

        if ctx.guild:
            await self.bot.pool.execute(
                """
                INSERT INTO welcome
                VALUES (%s, %s, %s)
                """,
                ctx.guild.id,
                channel.id,
                message,
            )
            await ctx.approve(f"Added a welcome message in {channel.mention}.")
            if content or embed:
                return await ctx.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                return await ctx.send(content=processed_message)

    @welcome.command(
        name="remove", aliases=["delete", "del"], description="Remove a welcome message",
        example="welcome remove #channel",
    )
    @has_permissions(manage_messages=True)
    async def welcome_remove(self, ctx: Context, *, channel: discord.TextChannel):
        if ctx.guild:
            data = await self.bot.pool.fetchrow(
                "SELECT * FROM welcome WHERE guild_id = %s AND channel_id = %s",
                ctx.guild.id,
                channel.id,
            )

            if data:
                message = data["message"]

                await self.bot.pool.execute(
                    """
                    DELETE FROM welcome
                    WHERE guild_id = %s AND channel_id = %s
                    """,
                    ctx.guild.id,
                    channel.id,
                )
                await ctx.approve(
                    f"Removed the **welcome settings** from {channel.mention}!"
                )
            else:
                return await ctx.warn(
                    f"There are no **welcome settings** saved for {channel.mention}."
                )

    @welcome.command(
        name="view", aliases=["test"], description="Test a welcome message",
        example="welcome view #channel",
    )
    @has_permissions(manage_messages=True)
    async def welcome_test(self, ctx: Context, channel: discord.TextChannel):
        res = await self.bot.pool.fetchrow(
            "SELECT * from welcome WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,  # type: ignore
        )

        if res:
            channel_id = res["channel_id"]
            channel = ctx.guild.get_channel(channel_id)  # type: ignore

            if channel is None:
                return

            message = res["message"]
            processed_message = EmbedBuilder.embed_replacement(ctx.author, message)  # type: ignore
            content, embed, view = await EmbedBuilder.to_object(processed_message)

            if content or embed:
                await channel.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                await channel.send(content=processed_message)

        else:
            return

    @welcome.command(
        name="list", description="Show a list of channels with a welcome message."
    )
    @has_permissions(manage_messages=True)
    async def welcome_list(self, ctx: Context):
        if ctx.guild:
            res = await self.bot.pool.fetch(
                """
                SELECT channel_id, message FROM welcome WHERE guild_id = %s
                """,
                ctx.guild.id,
            )

            if not res:
                return await ctx.warn(
                    "There are no welcome messages set up in this guild."
                )

            entries = [
                f"`{i}` {self.bot.get_channel(entry['channel_id']).mention if self.bot.get_channel(entry['channel_id']) else 'Channel ID: ' + str(entry['channel_id'])} (`{entry['channel_id']}`)"  # type: ignore
                for i, entry in enumerate(res, start=1)
            ]

            embeds = []
            embed = discord.Embed(
                color=COLORS.neutral, title="Welcome channels", description=""
            )

            count = 0
            for entry in entries:
                embed.description += f"{entry}\n"  # type: ignore
                count += 1

                if count == 5:
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} (entries: {len(entries)})"
                    )
                    embed.set_author(
                        name=ctx.author.display_name,
                        icon_url=ctx.author.display_avatar.url,
                    )
                    embeds.append(embed)
                    embed = discord.Embed(
                        color=COLORS.neutral, title=f"Welcome channels", description=""
                    )
                    count = 0

            if count > 0:
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} ({len(entries)} entries)"
                )
                embed.set_author(
                    name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
                )
                embeds.append(embed)

            if len(embeds) > 1:
                await ctx.paginate(embeds)
            else:
                await ctx.send(embed=embeds[0])

    @hybrid_group(
        name="prefix",
        invoke_without_command=True,
        description="See the prefix in the server",
    )
    async def prefix(self, ctx: Context):
        prefix = await self.bot.pool.fetchval(
            "SELECT prefix FROM prefix WHERE guild_id = %s", ctx.message.guild.id
        ) or ","
        return await ctx.embed(
            description=f"{ctx.author.mention}: **Guild prefix:** `{prefix}`"
        )

    @prefix.command(name="add", aliases=["set"], description="Change the prefix", example="prefix add !")
    @has_permissions(manage_messages=True)
    async def prefix_add(self, ctx: Context, *, prefix: str):
        await self.bot.pool.execute(
            "INSERT INTO prefix VALUES (%s, %s) ON DUPLICATE KEY UPDATE prefix = VALUES(prefix)",
            ctx.guild.id,
            str(prefix),
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(f"**Server prefix** updated to: `{prefix}`")

    @prefix.command(
        name="remove", aliases=["reset"], description="Reset the prefix back to default"
    )
    @has_permissions(manage_messages=True)
    async def prefix_remove(self, ctx: Context):
        await self.bot.pool.execute(
            "DELETE FROM prefix WHERE guild_id = %s", ctx.guild.id
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(f"**Server prefix** updated to: `,`")

    @prefix.group(
        name="self",
        invoke_without_command=True,
        description="See your personal prefixes",
    )
    async def prefix_self(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            """
            SELECT prefix FROM self_prefixes
            WHERE guild_id = %s AND user_id = %s
            ORDER BY prefix ASC
            """,
            ctx.guild.id,
            ctx.author.id,
        )

        if not rows:
            return await ctx.warn(
                f"You don't have any **personal prefixes**.\n"
                f"Use `{ctx.clean_prefix}prefix self add <prefix>` to add one."
            )

        prefixes = " ".join(f"`{row['prefix']}`" for row in rows)
        return await ctx.embed(
            description=f"{ctx.author.mention}: **Your prefixes:** {prefixes}"
        )

    @prefix_self.command(
        name="add",
        description="Add a personal prefix",
        example="prefix self add !",
    )
    async def prefix_self_add(self, ctx: Context, *, prefix: str):
        prefix = prefix.strip()

        if not prefix:
            return await ctx.warn("Your **prefix** cannot be empty.")

        if " " in prefix:
            return await ctx.warn("Your **prefix** cannot contain **spaces**.")

        if len(prefix) > 7:
            return await ctx.warn(
                "Your **prefix** cannot be longer than **7** characters."
            )

        count = await self.bot.pool.fetchval(
            """
            SELECT COUNT(*) FROM self_prefixes
            WHERE guild_id = %s AND user_id = %s
            """,
            ctx.guild.id,
            ctx.author.id,
        )

        if (count or 0) >= 5:
            return await ctx.warn(
                f"You can only have **5** personal prefixes.\n"
                f"Remove one with `{ctx.clean_prefix}prefix self remove <prefix>`."
            )

        if await self.bot.pool.fetchval(
            """
            SELECT 1 FROM self_prefixes
            WHERE guild_id = %s AND user_id = %s AND prefix = %s
            """,
            ctx.guild.id,
            ctx.author.id,
            prefix,
        ):
            return await ctx.warn(f"`{prefix}` is already one of your **prefixes**.")

        await self.bot.pool.execute(
            """
            INSERT INTO self_prefixes (guild_id, user_id, prefix)
            VALUES (%s, %s, %s)
            """,
            ctx.guild.id,
            ctx.author.id,
            prefix,
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(f"Added **personal prefix**: `{prefix}`")

    @prefix_self.command(
        name="remove",
        aliases=["delete", "del"],
        description="Remove a personal prefix",
        example="prefix self remove !",
    )
    async def prefix_self_remove(self, ctx: Context, *, prefix: str):
        prefix = prefix.strip()

        if not await self.bot.pool.fetchval(
            """
            SELECT 1 FROM self_prefixes
            WHERE guild_id = %s AND user_id = %s AND prefix = %s
            """,
            ctx.guild.id,
            ctx.author.id,
            prefix,
        ):
            return await ctx.warn(f"`{prefix}` is not one of your **prefixes**.")

        await self.bot.pool.execute(
            """
            DELETE FROM self_prefixes
            WHERE guild_id = %s AND user_id = %s AND prefix = %s
            """,
            ctx.guild.id,
            ctx.author.id,
            prefix,
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(f"Removed **personal prefix**: `{prefix}`")

    @prefix.group(
        name="noprefix",
        invoke_without_command=True,
        description="Run commands without any prefix",
    )
    async def prefix_noprefix(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @prefix_noprefix.command(
        name="enable",
        description="Enable running commands without a prefix",
        example="prefix noprefix enable",
    )
    async def prefix_noprefix_enable(self, ctx: Context):
        if await self.bot.pool.fetchrow(
            "SELECT 1 FROM noprefix WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        ):
            return await ctx.warn("**Noprefix** is already enabled for you.")

        await self.bot.pool.execute(
            "INSERT INTO noprefix (guild_id, user_id) VALUES (%s, %s)",
            ctx.guild.id,
            ctx.author.id,
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(
            "**Noprefix** enabled: you can now run commands without a prefix."
        )

    @prefix_noprefix.command(
        name="disable",
        description="Disable running commands without a prefix",
        example="prefix noprefix disable",
    )
    async def prefix_noprefix_disable(self, ctx: Context):
        if not await self.bot.pool.fetchrow(
            "SELECT 1 FROM noprefix WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        ):
            return await ctx.warn("**Noprefix** is not enabled for you.")

        await self.bot.pool.execute(
            "DELETE FROM noprefix WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        )
        self.bot.reset_prefix_cache(ctx.guild.id)
        return await ctx.approve(
            "**Noprefix** disabled: you now need a prefix to run commands."
        )

    @hybrid_group(name="alias", invoke_without_command=True, description="Configure aliases")
    @has_permissions(manage_guild=True)
    async def alias(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @alias.command(name="add", description="Add a custom alias", example="alias add hello hi")
    @has_permissions(manage_guild=True)
    async def alias_add(self, ctx: Context, alias: str, *, command: str):
        alias = alias.lower().replace(" ", "")

        if self.bot.get_command(alias):
            return await ctx.warn(f"`{alias}` is a **registered command**!")

        _command = self.bot.get_command(
            compile(r"[a-zA-Z0-9 ]+").match(command).group()
        )
        if not _command:
            return await ctx.warn(f"`{_command}` is not a **command**.")

        if not await self.bot.pool.fetchval(
            """
            SELECT * FROM aliases WHERE guild_id = %s AND alias = %s
            """,
            ctx.guild.id,
            alias,
        ):
            await self.bot.pool.execute(
                """
                INSERT INTO aliases (guild_id, alias, command)
                VALUES (%s, %s, %s)
                """,
                ctx.guild.id,
                alias,
                _command.qualified_name,
            )
            return await ctx.approve(f"Added **{alias}** for `{_command}`.")

    @alias.command(name="remove", description="Remove a custom alias", example="alias remove hello")
    @has_permissions(manage_guild=True)
    async def alias_remove(self, ctx: Context, alias: str):
        alias = alias.lower().replace(" ", "")

        if not await self.bot.pool.fetchval(
            """
            SELECT * FROM aliases WHERE guild_id = %s AND alias = %s
            """,
            ctx.guild.id,
            alias,
        ):
            return await ctx.warn(f"`{alias}` is not an existing **alias**.")

        await self.bot.pool.execute(
            """
            DELETE FROM aliases WHERE guild_id = %s AND alias = %s
            """,
            ctx.guild.id,
            alias,
        )
        return await ctx.approve(f"Removed **alias** `{alias}`")

    @alias.command(name="list", description="Get a list of your guild's custom aliases")
    async def alias_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            """
        SELECT alias, command, invoke
        FROM aliases
        WHERE guild_id = %s
        """,
            ctx.guild.id,
        )

        if not rows:
            return await ctx.warn("There are no **aliases** set up.")

        embeds = []
        entries = []
        count = 0

        for i, row in enumerate(rows, start=1):
            alias = row["alias"]
            command = row["command"]

            entries.append(f"`{i}` **{alias}** executes `{command}`")

        total_pages = (len(entries) + 9) // 10

        if not entries:
            return await ctx.warn("There are no valid aliases to display.")

        embed = discord.Embed(color=COLORS.neutral, title=f"", description="")
        embed.set_author(name=ctx.author.name, icon_url=ctx.author.display_avatar.url)
        embed.set_footer(
            text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(embed)
                embed = discord.Embed(
                    color=COLORS.neutral,
                    description="",
                    title=f"Aliases",
                )
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
                )
                embed.set_author(
                    name=ctx.author.name, icon_url=ctx.author.display_avatar.url
                )
                count = 0

        if count > 0:
            embeds.append(embed)

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        elif embeds:
            await ctx.send(embed=embeds[0])

    @hybrid_group(
        name="autorole",
        invoke_without_command=True,
        description="Configure autorole",
    )
    @has_permissions(manage_roles=True)
    async def autorole(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @autorole.command(name="add", aliases=["create"], description="Add an autorole", example="autorole add @role")
    @has_permissions(manage_roles=True)
    async def autorole_add(self, ctx: Context, *, role: Role):
        if await self.bot.pool.fetchval(
            """
            SELECT *
            FROM autorole
            WHERE guild_id = %s AND role_id = %s
            """,
            ctx.guild.id,
            role.id,
        ):
            return await ctx.warn(f"{role.mention} is already an **auto role**.")

        await self.bot.pool.execute(
            """
            INSERT INTO autorole (guild_id, role_id)
            VALUES (%s, %s)
            """,
            ctx.guild.id,
            role.id,
        )
        return await ctx.approve(
            f"{role.mention} will now be assigned to **new members**."
        )

    @autorole.command(
        name="remove", aliases=["delete", "del"], description="Remove an autorole",
        example="autorole remove @role",
    )
    @has_permissions(manage_roles=True)
    async def autorole_remove(self, ctx: Context, *, role: Role):
        if not await self.bot.pool.fetchval(
            """
            SELECT *
            FROM autorole
            WHERE guild_id = %s AND role_id = %s
            """,
            ctx.guild.id,
            role.id,
        ):
            return await ctx.warn(f"{role.mention} is not an **auto role**.")

        await self.bot.pool.execute(
            """
            DELETE FROM autorole
            WHERE guild_id = %s AND role_id = %s
            """,
            ctx.guild.id,
            role.id,
        )
        return await ctx.approve(
            f"{role.mention} will no longer be assigned to **new members**."
        )

    @autorole.command(name="list", description="See a list of autoroles in the guild")
    @has_permissions(manage_roles=True)
    async def autorole_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            """
            SELECT role_id
            FROM autorole
            WHERE guild_id = %s
            """,
            ctx.guild.id,
        )

        role_ids = [row["role_id"] for row in rows]
        roles = [ctx.guild.get_role(role_id) for role_id in role_ids]

        embeds = []
        entries = []
        count = 0

        if not roles:
            return await ctx.warn("There are no **auto roles** set up.")

        for i, role in enumerate(roles, start=1):
            if role:
                entries.append(f"`{i}`  **{role.mention}**")

        if not entries:
            return await ctx.warn("There are no valid roles to display.")

        embed = discord.Embed(
            color=COLORS.neutral, title=f"Autoroles ({len(entries)})", description=""
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(embed)
                embed = discord.Embed(
                    color=COLORS.neutral,
                    description="",
                    title=f"Autoroles ({len(entries)})",
                )
                count = 0

        if count > 0:
            embeds.append(embed)

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        elif embeds:
            await ctx.send(embed=embeds[0])

    @Cog.listener("on_member_join")
    async def autorole_listener(self, member: Member):
        role_ids = await self.bot.pool.fetch(
            """
        SELECT role_id
        FROM autorole
        WHERE guild_id = %s
        """,
            member.guild.id,
        )

        if role_ids:
            for row in role_ids:
                role_id = row["role_id"]
                role = member.guild.get_role(role_id)

                if role and role not in member.roles:
                    try:
                        await member.add_roles(role)
                    except Exception:
                        pass

    @Cog.listener("on_member_join")
    async def welcome_listener(self, member: discord.Member):
        res = await self.bot.pool.fetch(
            "SELECT * FROM welcome WHERE guild_id = %s", member.guild.id
        )
        for result in res:
            channel = self.bot.get_channel(result["channel_id"])
            if channel:
                processed_message = EmbedBuilder.embed_replacement(
                    member, result["message"]
                )  # type: ignore
                content, embed, view = await EmbedBuilder.to_object(processed_message)
                if content or embed:
                    await channel.send(content=content, embed=embed, view=view)  # type: ignore
                else:
                    await channel.send(content=processed_message)  # type: ignore
                await asyncio.sleep(0.4)

    @hybrid_group(
        name="stickymessage",
        aliases=["sticky"],
        description="Set up a sticky message in one or multiple channels",
        invoke_without_command=True,
    )
    @has_permissions(manage_guild=True)
    async def stickymessage(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @stickymessage.command(name="add", description="Add a sticky message to a channel", example="stickymessage add #channel message")
    @has_permissions(manage_guild=True)
    async def stickymessage_add(
        self, ctx: Context, channel: TextChannel, *, message: str
    ):
        if await self.bot.pool.fetchval(
            """
            SELECT channel_id FROM sticky_messages WHERE guild_id = %s
            """,
            ctx.guild.id,
        ):
            return await ctx.warn(
                f" Theres already a **sticky message** for this channel, you can't have multiple for one channel. Remove the current **sticky message** then try again."
            )

        processed_message = EmbedBuilder.embed_replacement(ctx.author, message)  # type: ignore
        content, embed, view = await EmbedBuilder.to_object(processed_message)

        if content or embed:
            _message = await ctx.send(content=content, embed=embed, view=view)  # type: ignore
        else:
            _message = await ctx.send(content=processed_message)

        await self.bot.pool.execute(
            """
            INSERT INTO sticky_messages (guild_id, channel_id, message, message_id)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE message = VALUES(message)
            """,
            ctx.guild.id,
            channel.id,
            str(message),
            _message.id,
        )

    @stickymessage.command(
        name="remove", description="Remove a sticky message from a channel",
        example="stickymessage remove #channel",
    )
    @has_permissions(manage_guild=True)
    async def stickymessage_remove(self, ctx: Context, channel: TextChannel):
        message = await self.bot.pool.fetchval(
            """
        SELECT message FROM sticky_messages WHERE guild_id = %s AND channel_id = %s
        """,
            ctx.guild.id,
            channel.id,
        )

        if not message:
            return await ctx.warn(
                f"No **sticky messages** found for {channel.mention}."
            )

        await self.bot.pool.execute(
            """
            DELETE FROM sticky_messages
            WHERE guild_id = %s AND channel_id = %s
            """,
            ctx.guild.id,
            channel.id,
        )
        return await ctx.approve(
            f"Removed the **sticky message** for {channel.mention}"
        )

    @Cog.listener("on_message")
    async def dispatch_stickymessage(self, message: discord.Message):
        if message.author.bot:
            return
        data = await self.bot.pool.fetchrow(
            "SELECT * FROM sticky_messages WHERE guild_id = %s AND channel_id = %s",
            message.guild.id,
            message.channel.id,
        )

        if not data:
            return

        old = await message.channel.fetch_message(data["message_id"])
        await old.delete()

        if data["channel_id"] == message.channel.id:
            processed_message = EmbedBuilder.embed_replacement(message.author, data["message"])  # type: ignore
            content, embed, view = await EmbedBuilder.to_object(processed_message)

            if content or embed:
                new_message = await message.channel.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                new_message = await message.channel.send(content=processed_message)  # type: ignore

            await self.bot.pool.execute(
                "UPDATE sticky_messages SET message_id = %s WHERE guild_id = %s AND channel_id = %s",
                message.guild.id,
                message.channel.id,
                new_message.id,
            )

    @hybrid_group(
        name="boosts",
        aliases=["boost"],
        description="Set up boost messages in one or multiple channels",
        invoke_without_command=True,
    )
    @has_permissions(manage_messages=True)
    async def boosts(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @boosts.command(name="add", description="Add a boost message to a channel", example="boosts add #channel message")
    @has_permissions(manage_messages=True)
    async def boosts_add(self, ctx: Context, channel: TextChannel, *, message: str):
        processed_message = EmbedBuilder.embed_replacement(ctx.author, message)  # type: ignore
        content, embed, view = await EmbedBuilder.to_object(processed_message)

        check = await self.bot.pool.fetchrow(
            """
            SELECT * FROM boost WHERE channel_id = %s
            """,
            channel.id,
        )
        if check:
            await self.bot.pool.execute(
                """
                UPDATE boost
                SET message = %s
                WHERE channel_id = %s
                """,
                message,
                channel.id,
            )
            await ctx.approve(f"Edited {channel.mention}'s boost message to:")
            if content or embed:
                return await ctx.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                return await ctx.send(content=processed_message)

        if ctx.guild:
            await self.bot.pool.execute(
                """
                    INSERT INTO boost
                    VALUES (%s, %s, %s)
                    """,
                ctx.guild.id,
                channel.id,
                message,
            )
            await ctx.approve(f"Added a boost message in {channel.mention}.")
            if content or embed:
                return await ctx.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                return await ctx.send(content=processed_message)

    @boosts.command(
        name="remove",
        aliases=["delete", "del"],
        description="Remove a boost message from a channel",
        example="boosts remove #channel",
    )
    @has_permissions(manage_messages=True)
    async def boosts_remove(self, ctx: Context, *, channel: discord.TextChannel):
        if ctx.guild:
            data = await self.bot.pool.fetchrow(
                "SELECT * FROM boost WHERE guild_id = %s AND channel_id = %s",
                ctx.guild.id,
                channel.id,
            )

            if data:
                message = data["message"]

                await self.bot.pool.execute(
                    """
                    DELETE FROM boost
                    WHERE guild_id = %s AND channel_id = %s
                    """,
                    ctx.guild.id,
                    channel.id,
                )
                await ctx.approve(
                    f"Removed the **boost settings** from {channel.mention}!"
                )
            else:
                return await ctx.warn(
                    f"There are no **boost settings** saved for {channel.mention}."
                )

    @boosts.command(
        name="view", aliases=["test"], description="View a boost message for a channel",
        example="boosts view #channel",
    )
    @has_permissions(manage_messages=True)
    async def boosts_view(self, ctx: Context, channel: discord.TextChannel):
        res = await self.bot.pool.fetchrow(
            "SELECT * from boost WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,  # type: ignore
        )

        if res:
            channel_id = res["channel_id"]
            channel = ctx.guild.get_channel(channel_id)  # type: ignore

            if channel is None:
                return

            message = res["message"]
            processed_message = EmbedBuilder.embed_replacement(ctx.author, message)  # type: ignore
            content, embed, view = await EmbedBuilder.to_object(processed_message)

            if content or embed:
                await channel.send(content=content, embed=embed, view=view)  # type: ignore
            else:
                await channel.send(content=processed_message)
            await ctx.message.add_reaction("✅")

        else:
            return

    @boosts.command(
        name="list", description="Show a list of channels with a boost message."
    )
    @has_permissions(manage_messages=True)
    async def boosts_list(self, ctx: Context):
        if ctx.guild:
            res = await self.bot.pool.fetch(
                """
                SELECT channel_id, message FROM boost WHERE guild_id = %s
                """,
                ctx.guild.id,
            )

            if not res:
                return await ctx.warn(
                    "There are no **boost messages** set up in this guild."
                )

            entries = [
                f"`{i}` {self.bot.get_channel(entry['channel_id']).mention if self.bot.get_channel(entry['channel_id']) else 'Channel ID: ' + str(entry['channel_id'])} (`{entry['channel_id']}`)"  # type: ignore
                for i, entry in enumerate(res, start=1)
            ]

            embeds = []
            embed = discord.Embed(
                color=COLORS.neutral, title="Boost channels", description=""
            )

            count = 0
            for entry in entries:
                embed.description += f"{entry}\n"  # type: ignore
                count += 1

                if count == 5:
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} (entries: {len(entries)})"
                    )
                    embed.set_author(
                        name=ctx.author.display_name,
                        icon_url=ctx.author.display_avatar.url,
                    )
                    embeds.append(embed)
                    embed = discord.Embed(
                        color=COLORS.neutral, title=f"Boost channels", description=""
                    )
                    count = 0

            if count > 0:
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} ({len(entries)} entries)"
                )
                embed.set_author(
                    name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
                )
                embeds.append(embed)

            if len(embeds) > 1:
                await ctx.paginate(embeds)
            else:
                await ctx.send(embed=embeds[0])

    @Cog.listener("on_message")
    async def dispatch_boosts(self, message: Message):
        if "MessageType.premium_guild" in str(message.type):
            data = await self.bot.pool.fetchrow(
                """
                SELECT * FROM boost WHERE guild_id = %s
                """,
                message.guild.id,
            )
            for result in data:
                channel = self.bot.get_channel(result["channel_id"])
                if channel:
                    processed_message = EmbedBuilder.embed_replacement(
                        message.author, result["message"]
                    )  # type: ignore
                    content, embed, view = await EmbedBuilder.to_object(
                        processed_message
                    )
                    if content or embed:
                        await channel.send(content=content, embed=embed, view=view)  # type: ignore
                    else:
                        await channel.send(content=processed_message)  # type: ignore
                    await asyncio.sleep(0.4)

    @hybrid_group(name = "autoresponder", aliases = ["autorespond", "autoresponse", "ar"], description = "Set up automatic replies to messages matching a trigger", invoke_without_command = True)
    @has_permissions(manage_messages = True)
    async def autoresponder(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @autoresponder.command(name = "add", description = "Add an autoresponder", example="autoresponder add hello Hi there! --delete_trigger")
    @has_permissions(manage_messages = True)
    async def autoresponder_add(self, ctx: Context, *, args: str):
        split_args = args.split()

        if len(split_args) < 2:
            return await ctx.warn(
                "Invalid syntax! Use: `,autoresponder add <trigger> <response> --flags`"
            )

        trigger = split_args[0]
        response = split_args[1]
        flags = split_args[2:]

        not_strict = False
        self_destruct = False
        self_destruct_time = 0
        delete_trigger = False
        reply = False

        i = 0
        while i < len(flags):
            flag = flags[i]
            if flag == "--not_strict":
                not_strict = True
            elif flag == "--self_destruct":
                try:
                    self_destruct_time = int(flags[i + 1])
                    self_destruct = True
                    i += 1
                except (IndexError, ValueError):
                    return await ctx.warn("Invalid value for `--self_destruct`. Use: `--self_destruct <Seconds>`")
            elif flag == "--delete_trigger":
                delete_trigger = True
            elif flag == "--reply":
                reply = True
            else:
                return await ctx.warn(f"Unknown flag: `{flag}`. Use `--not_strict`, `--self_destruct`, `--delete_trigger`, or `--reply`.")
            i += 1

        res = await self.bot.pool.fetchrow(
            "SELECT * FROM autoresponders WHERE guild_id = %s AND `trigger` = %s",
            ctx.guild.id,
            trigger,
        )

        if res:
            return await ctx.warn(f"An autoresponder with the trigger `{trigger}` already exists.")

        await self.bot.pool.execute(
            "INSERT INTO autoresponders (guild_id, `trigger`, response, not_strict, self_destruct, self_destruct_time ,delete_trigger, reply) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            ctx.guild.id,
            trigger,
            response,
            not_strict,
            self_destruct,
            self_destruct_time,
            delete_trigger,
            reply,
        )

        return await ctx.approve(
            f"Added new autoresponder with trigger `{trigger}` and response `{response}`. {'(Not Strict)' if not_strict else '(Strict match)'} {'(Delete)' if delete_trigger else ''} {'(Reply)' if reply else ''}"
            f"{f' This **autoresponder** will self-destruct in **{self_destruct_time}** seconds' if self_destruct else ''}"
        )

    @Cog.listener("on_message")
    async def dispatch_autoresponder(self, message: Message):
        if message.author.bot:
            return

        res = await self.bot.pool.fetchrow(
            "SELECT * FROM autoresponders WHERE guild_id = %s AND `trigger` = %s",
            message.guild.id,
            message.content.split()[0],
        )

        if not res:
            return

        response = res["response"]
        not_strict = res["not_strict"]
        self_destruct = res["self_destruct"]
        destruct =res["self_destruct_time"] if res["self_destruct_time"] else 0
        delete_trigger = res["delete_trigger"]
        reply = res["reply"]

        if not not_strict and message.content != res["trigger"]:
            return

        sent_msg = None
        if reply:
            sent_msg = await message.reply(response)
        else:
            sent_msg = await message.channel.send(response)

        if delete_trigger:
            await message.delete()

        if self_destruct and sent_msg and destruct > 0:
            await asyncio.sleep(int(destruct * 1))
            await sent_msg.delete()

    @autoresponder.command(name = "delete", aliases = ["remove"], description = "Delete an autoresponder", example="autoresponder delete hello")
    @has_permissions(manage_permissions = True)
    async def autoresponder_delete(self, ctx: Context, *, trigger: str):
        res = await self.bot.pool.fetchrow(
        "SELECT * FROM autoresponders WHERE guild_id = %s AND `trigger` = %s",
        ctx.guild.id,
        trigger,
    )

        if not res:
            return await ctx.warn(f"No **autoresponder** found with the trigger `{trigger}`.")

        await self.bot.pool.execute(
            "DELETE FROM autoresponders WHERE guild_id = %s AND `trigger` = %s",
            ctx.guild.id,
            trigger,
        )

        await ctx.approve(f"Deleted the **autoresponder** with trigger `{trigger}`.")


    @autoresponder.command(name="list", description="Show a list of autoresponders in this server.")
    @has_permissions(manage_messages=True)
    async def autoresponder_list(self, ctx: Context):
        if ctx.guild:
            res = await self.bot.pool.fetch(
                """
                SELECT `trigger`, response, not_strict, self_destruct, self_destruct_time, delete_trigger, reply
                FROM autoresponders
                WHERE guild_id = %s
                """,
                ctx.guild.id,
            )

            if not res:
                return await ctx.warn("There are no autoresponders set up in this guild.")

            entries = [
                f"`{i}` **{entry['trigger']}** (sd: {entry['self_destruct_time'] if entry['self_destruct'] else 'None'}, "
                f"strict: {'yes' if entry['not_strict'] else 'no, '}"
                f"reply: {'yes' if entry['reply'] else 'no'})"
                for i, entry in enumerate(res, start=1)
            ]

            embeds = []
            embed = discord.Embed(
                color=COLORS.neutral, title="Autoresponders", description=""
            )

            count = 0
            for entry in entries:
                embed.description += f"{entry}\n"
                count += 1

                if count == 5:
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} (Entries: {len(entries)})"
                    )
                    embed.set_author(
                        name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
                    )
                    embeds.append(embed)
                    embed = discord.Embed(
                        color=COLORS.neutral, title="Autoresponders", description=""
                    )
                    count = 0

            if count > 0:
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{(len(entries) + 4) // 5} (Entries: {len(entries)})"
                )
                embed.set_author(
                    name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
                )
                embeds.append(embed)

            if len(embeds) > 1:
                await ctx.paginate(embeds)
            else:
                await ctx.send(embed=embeds[0])

    @hybrid_group(
        name="boosterrole",
        aliases=["br"],
        description="Configure boosterroles in your guild.",
        invoke_without_command=True,
    )
    async def boosterrole(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @boosterrole.command(
        name="setup",
        aliases=["enable"],
        description="Enable the booster role module in your guild.",
    )
    @commands.has_permissions(manage_guild=True)
    async def boosterrole_setup(self, ctx: Context):
        if await self.bot.pool.fetchrow(
            "SELECT * FROM booster_module WHERE guild_id = %s", ctx.guild.id
        ):
            return await ctx.warn(
                "The booster role module is already enabled in this guild."
            )

        premium_role = ctx.guild.premium_subscriber_role
        if premium_role is None:
            premium_role = ctx.guild.default_role

        await self.bot.pool.execute(
            "INSERT INTO booster_module (guild_id, base) VALUES (%s, %s)",
            ctx.guild.id,
            premium_role.id,
        )
        return await ctx.approve(f"The booster role module has been enabled.")

    @boosterrole.command(
        name="disable", description="Disable the booster role module in your guild."
    )
    @commands.has_permissions(manage_guild=True)
    async def boosterrole_disable(self, ctx: Context):
        await self.bot.pool.execute(
            "DELETE FROM booster_module WHERE guild_id = %s", ctx.guild.id
        )
        await self.bot.pool.execute(
            "DELETE FROM booster_roles WHERE guild_id = %s", ctx.guild.id
        )
        return await ctx.approve(f"Disabled the booster role module.")

    @boosterrole.command(name="base", description="Set the base for the booster role.", example="boosterrole base @role")
    @commands.has_permissions(manage_guild=True)
    async def boosterrole_base(self, ctx: Context, *, role: discord.Role = None):
        check = await self.bot.pool.fetchrow(
            "SELECT base FROM booster_module WHERE guild_id = %s", ctx.guild.id
        )
        if role is None:
            if check is None:
                return await ctx.warn(f"The booster role base role isn't set.")

            await self.bot.pool.execute(
                "UPDATE booster_module SET base = %s WHERE guild_id = %s",
                None,
                ctx.guild.id,
            )
            return await ctx.approve(f"Removed the booster role base.")

        await self.bot.pool.execute(
            "UPDATE booster_module SET base = %s WHERE guild_id = %s",
            role.id,
            ctx.guild.id,
        )
        return await ctx.approve(f"Set the booster role base to: {role.mention}")

    @boosterrole.command(name="create", description="Create your booster role.", example="boosterrole create Booster Role")
    @br_enabled()
    async def boosterrole_create(self, ctx: Context, *, name: str = None):
        if not ctx.author.premium_since:
            return await ctx.warn(
                f"You need to boost this server in order to use this command."
            )
        check = await self.bot.pool.fetchrow(
            "SELECT base FROM booster_module WHERE guild_id = %s", ctx.guild.id
        )

        if not name:
            name = f"{ctx.author.name}'s role"

        if await self.bot.pool.fetchrow(
            "SELECT * FROM booster_roles WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        ):
            return await ctx.warn("You already have a booster role.")

        base = ctx.guild.get_role(check)
        role = await ctx.guild.create_role(
            name=name, reason=f"{ctx.author} created their booster role."
        )
        await role.edit(position=base.position if base is not None else 1)
        await ctx.author.add_roles(role)
        await self.bot.pool.execute(
            "INSERT INTO booster_roles VALUES (%s,%s,%s)",
            ctx.guild.id,
            ctx.author.id,
            role.id,
        )
        return await ctx.approve("Booster role has been created.")

    @boosterrole.command(name="name", description="Rename your boosterrole.", example="boosterrole name New Name")
    @has_br_role()
    async def boosterrole_name(self, ctx: Context, *, name: str):

        if len(name) > 32:
            return await ctx.warn(
                "The booster role name can't be more than **32** characters long."
            )

        role = ctx.guild.get_role(
            await self.bot.pool.fetchval(
                "SELECT role_id FROM booster_roles WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                ctx.author.id,
            )
        )
        if not role:
            return await ctx.warn(
                f"You don't have a booster role setup. Use `{ctx.clean_prefix}br create` to create one."
            )

        await role.edit(
            name=name, reason=f"{ctx.author.name} edited their booster role."
        )
        return await ctx.approve(f"Renamed your booster role to **{name}**")

    @boosterrole.command(
        name="colour", aliases=["color"], description="Set your booster role's color.",
        example="boosterrole colour #ff0000",
    )
    @has_br_role()
    async def boosterrole_colour(self, ctx: Context, *, color: str):
        if color.startswith("#"):
            color = color[1:]

        try:
            discord_color = discord.Color(int(color, 16))
        except ValueError:
            return await ctx.deny(
                "You need to enter a valid hex code. Example: #47C1BC"
            )

        role = ctx.guild.get_role(
            await self.bot.pool.fetchval(
                "SELECT role_id FROM booster_roles WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                ctx.author.id,
            )
        )
        if not role:
            return await ctx.warn(
                f"You don't have a booster role setup. Use `{ctx.clean_prefix}br create` to create one."
            )

        await role.edit(
            color=discord_color, reason=f"{ctx.author} edited their booster role."
        )
        return await ctx.send(
            embed=discord.Embed(
                description=f"{EMOJIS.APPROVE} {ctx.author.mention}: Edited the role's color to {color}",
                color=discord_color,
            )
        )

    @boosterrole.command(name="icon", description="Set your booster role icon.", example="boosterrole icon https://example.com/icon.png")
    @has_br_role()
    @level2()
    async def boosterrole_icon(
        self, ctx: Context, *, icon: str
    ):
        role = ctx.guild.get_role(
            await self.bot.pool.fetchval(
                "SELECT role_id FROM booster_roles WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                ctx.author.id,
            )
        )
        if not role:
            return await ctx.warn(
                f"You don't have a booster role setup. Use `{ctx.clean_prefix}br create` to create one."
            )

        if icon.startswith("<") and icon.endswith(">"):
            icon = str(await discord.PartialEmoji.from_str(icon).read())

        await role.edit(
            display_icon=icon,
            reason=f"{ctx.author} edited their booster role",
        )
        return await ctx.approve(
            f"Booster role icon has been set to {icon.name if isinstance(icon, discord.PartialEmoji) else icon}"
        )

    @boosterrole.command(name="delete", description="Delete your booster role.")
    @has_br_role()
    async def boosterrole_delete(self, ctx: Context):
        role = ctx.guild.get_role(
            await self.bot.pool.fetchval(
                "SELECT role_id FROM booster_roles WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,
                ctx.author.id,
            )
        )
        if not role:
            return await ctx.warn(
                f"You don't have a booster role setup. Use `{ctx.clean_prefix}br create` to create one."
            )

        await role.delete(reason=f"{ctx.author} deleted their booster role.")
        await self.bot.pool.execute(
            "DELETE FROM booster_roles WHERE guild_id = %s AND user_id = %s",
            ctx.guild.id,
            ctx.author.id,
        )
        return await ctx.approve(f"Your booster role has been deleted.")

    @hybrid_group(
        name="leaves",
        aliases=["bye", "leave", "leaver", "goodbye"],
        description="Configure the leave messages.",
        invoke_without_command=True,
    )
    @has_permissions(manage_messages=True)
    async def leaves(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @leaves.command(name="add", aliases=["set"], description="""Add a leave message to a channel.""", example="leaves add #channel Goodbye!")
    @has_permissions(manage_messages=True)
    async def leaves_add(self, ctx: Context, channel: TextChannel, *, message: str):

        query = "SELECT 1 FROM `leave` WHERE channel_id = %s"
        action = "Edited" if await self.bot.pool.fetchrow(query, channel.id) else "Set"

        await self.bot.pool.execute(
            """
            INSERT INTO `leave` (guild_id, channel_id, message)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE message = VALUES(message)
            """,
            ctx.guild.id,
            channel.id,
            message,
        )

        await ctx.approve(f"**{action}** {channel.mention}'s leave message to: ")

        await send_embed(ctx.channel, message, ctx.author)

    @leaves.command(name="remove", aliases=["delete", "rm"], description= """Removes a channel's leave message""", example="leaves remove #channel")
    @has_permissions(manage_messages=True)
    async def leave_remove(self, ctx: Context, *, channel: TextChannel):
        if await self.bot.pool.fetchrow(
            "SELECT * FROM `leave` WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,
        ):
            await self.bot.pool.execute(
                "DELETE FROM `leave` WHERE guild_id = %s AND channel_id = %s",
                ctx.guild.id,
                channel.id,
            )
            return await ctx.approve(
                f"**Removed** the leave configuration from {channel.mention}."
            )
        else:
            return await ctx.warn(f"No **leave messages** found in that channel.")

    @leaves.command(name="test", aliases=["view"], description="""Test a channel's leave message.""", example="leaves test #channel")
    @has_permissions(manage_messages=True)
    async def leaves_view(self, ctx: Context, channel: TextChannel = None):  # type: ignore
        if channel is None:
            channel = ctx.channel

        data = await self.bot.pool.fetch(
            "SELECT * FROM `leave` WHERE guild_id = %s", ctx.guild.id
        )

        for result in data:
            channel = self.bot.get_channel(result["channel_id"])  # type: ignore
            if channel:
                await send_embed(channel, result["message"], ctx.author)
                await ctx.message.add_reaction(f"{EMOJIS.APPROVE}")
                await asyncio.sleep(0.5)

    @leaves.command(name="list")
    @has_permissions(manage_messages=True)
    async def leaves_list(self, ctx: Context):
        """List all leave messages set in the guild."""

        rows = await self.bot.pool.fetch(
            "SELECT channel_id, message FROM `leave` WHERE guild_id=%s",
            ctx.guild.id,
        )

        if not rows:
            return await ctx.warn("No leave messages are set in this guild.")

        embed = Embed(title="Leave Messages", color=COLORS.neutral)
        for index, row in enumerate(rows, start=1):
            channel = ctx.guild.get_channel(row["channel_id"])
            embed.add_field(
                name=f"`{index}.` {channel.mention if channel else 'Unknown Channel'} (`{channel.id}`)",  # type: ignore
                value="",
                inline=False,
            )

        await ctx.send(embed=embed)

    @Cog.listener("on_member_remove")
    async def dispatch_leaves(self, member: Member):
        """Dispatches the leave messages."""
        data = await self.bot.pool.fetch(
            "SELECT * FROM `leave` WHERE guild_id = %s", member.guild.id
        )

        for result in data:
            channel = self.bot.get_channel(result["channel_id"])
            if channel:
                await send_embed(channel, result["message"], member)
                await asyncio.sleep(0.5)

    @hybrid_group(name="fakeperms", aliases=["fp"], invoke_without_command=True)
    @has_permissions(administrator=True)
    async def fakeperms(self, ctx: Context):
        """Manage fakeperms in your server."""
        return await ctx.send_help(ctx.command)

    @fakeperms.command(name="add", aliases=["set"], example="fakeperms add @role send_messages")
    @has_permissions(administrator=True)
    async def fakeperms_add(self, ctx: Context, role: Role, *, permission: str):
        """Add a fakeperm to a role"""
        permission = permission.lower().replace(" ", "_")
        if permission not in self.valid_perms:
            return await ctx.warn(
                f"`{permission}` is not a valid **permission**. Do `{ctx.clean_prefix}fakeperms perms` to view all valid permissions."
            )

        updated_perms = ",".join(
            set(
                (
                    await self.bot.pool.fetchval(
                        "SELECT permission FROM fake_permissions WHERE guild_id = %s AND role_id = %s",
                        ctx.guild.id,
                        role.id,
                    )
                    or ""
                ).split(",")
            )
            | {permission}
        )

        await self.bot.pool.execute(
            """
            INSERT INTO fake_permissions (guild_id, role_id, permission)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE permission = VALUES(permission)
            """,
            ctx.guild.id,
            role.id,
            updated_perms,
        )

        return await ctx.approve(
            f"`{permission}` has been **assigned** to {role.mention}"
        )

    @fakeperms.command(name="list", example="fakeperms list @role")
    @has_permissions(administrator=True)
    async def fakeperms_list(self, ctx: Context, role: Role):
        """List all fake permissions for a role"""

        permissions = await self.bot.pool.fetchval(
            "SELECT permission FROM fake_permissions WHERE guild_id = %s AND role_id = %s",
            ctx.guild.id,
            role.id,
        )

        if not permissions:
            return await ctx.warn(f"No fake permissions found for {role.mention}.")

        permissions = permissions.split(",")
        pages = [permissions[i : i + 10] for i in range(0, len(permissions), 10)]

        embeds = [
            Embed(
                color=COLORS.neutral,
                title=f"Fake Permissions for {role.name}",
                description="\n".join(
                    [f"{i+1}. {perm}" for i, perm in enumerate(page)]
                ),
            )
            .set_author(
                name=ctx.author.name,
                icon_url=ctx.author.display_avatar.url,
            )
            .set_footer(text=f"Page {i + 1}/{len(pages)}")
            for i, page in enumerate(pages)
        ]

        await ctx.paginate(embeds)

    @fakeperms.command(name="remove", example="fakeperms remove @role send_messages")
    @has_permissions(administrator=True)
    async def fakeperms_remove(self, ctx: Context, role: Role, *, permission: str):
        permission = permission.lower().replace(" ", "_")
        if permission not in self.valid_perms:
            return await ctx.warn(
                f"`{permission}` is not a valid **permission**. Do `{ctx.clean_prefix}fakeperms perms` to view all valid permissions."
            )

        current_perms = await self.bot.pool.fetchval(
            "SELECT permission FROM fake_permissions WHERE guild_id = %s AND role_id = %s",
            ctx.guild.id,
            role.id,
        )

        if not current_perms or permission not in current_perms.split(","):
            return await ctx.warn(
                f"{role.mention} does not have `{permission}` permission."
            )

        updated_perms = ",".join(
            perm for perm in current_perms.split(",") if perm != permission
        )

        await self.bot.pool.execute(
            """
            INSERT INTO fake_permissions (guild_id, role_id, permission)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE permission = VALUES(permission)
            """,
            ctx.guild.id,
            role.id,
            updated_perms,
        )

        return await ctx.approve(
            f"`{permission}` has been **removed** from {role.mention}"
        )

    @fakeperms.command(name="perms")
    @has_permissions(administrator=True)
    async def fakeperms_perms(self, ctx: Context):
        permissions = list(self.valid_perms)
        pages = [permissions[i : i + 15] for i in range(0, len(permissions), 15)]

        embeds = []
        current_number = 1

        for i, page in enumerate(pages):
            embed = (
                Embed(
                    color=COLORS.neutral,
                    title="Valid Permissions",
                    description="\n".join(
                        [f"{current_number + j}. {perm}" for j, perm in enumerate(page)]
                    ),
                )
                .set_author(
                    name=ctx.author.name,
                    icon_url=ctx.author.display_avatar.url,
                )
                .set_footer(text=f"Page {i + 1}/{len(pages)}")
            )
            embeds.append(embed)
            current_number += len(page)

        await ctx.paginate(embeds)

    @Cog.listener("on_message_delete")
    async def snipe_listener(self, message: Message) -> None:
        if message.author.bot:
            return

        if message.channel.id not in self.Sniped:
            self.Sniped[message.channel.id] = []

        image_url = None
        if message.attachments:
            image_url = message.attachments[0].url

        self.Sniped[message.channel.id].append(
            {
                "author": str(message.author),
                "author_url": str(message.author.display_avatar.url),
                "content": message.content,
                "image_url": image_url,
                "timestamp": message.created_at,
                "deleted_at": datetime.utcnow(),
            }
        )

    @Cog.listener("on_raw_reaction_remove")
    async def reactionsnip_listener(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        message_link = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        emoji = str(payload.emoji)
        if payload.channel_id not in self.reactSnipe:
            self.reactSnipe[payload.channel_id] = []

        self.reactSnipe[payload.channel_id].append(
            {
                "author": str(payload.user_id),
                "emoji": emoji,
                "message_link": message_link,
                "message_id": payload.message_id,
                "timestamp": datetime.utcnow(),
            }
        )

    @Cog.listener("on_message_edit")
    async def editsnipe_listener(self, before: Message, after: Message) -> None:
        if before.guild and not before.author.bot:
            channel_id = before.channel.id

            if channel_id not in self.editSnipe:
                self.editSnipe[channel_id] = []

            self.editSnipe[channel_id].append(
                {
                    "before_content": before.content,
                    "after_content": after.content,
                    "author": str(before.author),
                    "author_url": str(before.author.display_avatar.url),
                    "timestamp": (before.edited_at),
                    "edited_at": datetime.utcnow(),
                }
            )

    @hybrid_command(
        name="reactionsnipe",
        description="See recently removed reactions.",
        aliases=["rs"],
        example="reactionsnipe 2",
    )
    async def reactionsnipe(self, ctx: Context, *, index: int = 1):
        snipes = self.reactSnipe.get(ctx.channel.id, [])
        if not snipes:
            return await ctx.embed(
                description=f"🔎 {ctx.author.mention}: No **reaction removals** found!",
            )

        index -= 1

        if index < 0 or index >= len(snipes):
            return await ctx.warn(f"Invalid index!")

        sniped = snipes[index]
        user_id = sniped.get("author", "N/A")
        emoji = sniped.get("emoji", "")
        timestamp = sniped.get("timestamp", datetime.utcnow())
        original_message_id = sniped.get("message_id")

        embed = Embed(
            description=f"**{await ctx.guild.fetch_member(user_id)}** reacted with *{emoji}* {format_dt(timestamp, 'R')}",
            color=COLORS.neutral,
        )

        channel = self.bot.get_channel(ctx.channel.id)
        original_message = await channel.fetch_message(original_message_id)
        await original_message.reply(embed=embed)

    @hybrid_command(name="editsnipe", aliases=["es"], description="See edited messages.", example="editsnipe 2")
    async def editsnipe(self, ctx: Context, *, index: int = 1):
        editsnipes = self.editSnipe.get(ctx.channel.id, [])
        if not editsnipes:
            return await ctx.embed(
                description=f"🔎 {ctx.author.mention}: No **edited messages** found!"
            )

        index -= 1

        if index < 0 or index >= len(editsnipes):
            return await ctx.warn(f"Invalid index!")

        editsniped = editsnipes[index]
        before = editsniped.get("before_content", "")
        after = editsniped.get("after_content", "")
        author = editsniped.get("author", "N/A")
        author_url = editsniped.get("author_url", "")
        edited = editsniped.get("edited_at", datetime.utcnow())

        time_after_edit = humanize.naturaltime(edited)

        embed = Embed(description=f"**Before:** {before} \n**After:** {after}")
        embed.set_footer(
            text=f"Edited {time_after_edit} ∙ {index + 1}/{len(editsnipes)}"
        )
        embed.set_author(name=author, icon_url=author_url)
        return await ctx.send(embed=embed)

    @hybrid_command(name="snipe", aliases=["s"], description="See deleted messages.", example="snipe 2")
    async def snipe(self, ctx: Context, *, index: int = 1):
        sniped_messages = self.Sniped.get(ctx.channel.id, [])
        if not sniped_messages:
            return await ctx.embed(
                description=f"🔎 {ctx.author.mention}: No **deleted messages** found!"
            )

        index -= 1

        if index < 0 or index >= len(sniped_messages):
            return await ctx.warn("Invalid index!")

        sniped_message = sniped_messages[index]
        content = sniped_message.get("content", "")
        author = sniped_message.get("author", "N/A")
        author_icon = sniped_message.get("author_url")
        deleted_at = sniped_message.get("deleted_at", datetime.utcnow())
        image_url = sniped_message.get("image_url")

        time_since_deletion = humanize.naturaltime(deleted_at)

        embed = Embed(description=content)
        embed.set_footer(
            text=f"Deleted {time_since_deletion} ∙ {index + 1}/{len(sniped_messages)}"
        )
        embed.set_author(name=author, icon_url=author_icon)

        if image_url:
            embed.set_image(url=image_url)

        await ctx.send(embed=embed)

    @hybrid_command(
        name="clearsnipes",
        aliases=["cs"],
        description="Clear all sniped messages in the guild.",
    )
    @has_permissions(manage_messages=True)
    async def clearsnipes(self, ctx: Context):
        cleared = False

        if ctx.message.channel.id in self.Sniped:
            del self.Sniped[ctx.message.channel.id]
            cleared = True

        if ctx.message.channel.id in self.editSnipe:
            del self.editSnipe[ctx.message.channel.id]
            cleared = True

        if ctx.message.channel.id in self.reactSnipe:
            del self.reactSnipe[ctx.message.channel.id]
            cleared = True

        if cleared:
            return await ctx.message.add_reaction("✅")  # type: ignore
        else:
            return await ctx.warn("There are no **sniped** messages in this guild.")

    # ------------------------------------------------------------------ #
    # Server Counters
    # ------------------------------------------------------------------ #

    VALID_COUNTER_TYPES = {
        "boost": "boost",
        "boosts": "boost",
        "online": "online",
        "offline": "offline",
        "bots": "bots",
        "bot": "bots",
        "members": "members",
        "member": "members",
        "humans": "members",
        "boostlevel": "boostlevel",
        "level": "boostlevel",
        "tier": "boostlevel",
    }

    def _strip_counter_suffix(self, name: str) -> str:
        clean = re.sub(r"[\s:\-_]+(?:\d+[\s\w]*)$", "", name).strip()
        return clean or name

    def _format_counter_name(self, channel: Union[TextChannel, VoiceChannel], base_name: str, count: int) -> str:
        sep = "-" if isinstance(channel, TextChannel) else ": "
        return f"{base_name}{sep}{count}"

    def _get_counter_value(self, guild: discord.Guild, counter_type: str) -> int:
        if counter_type == "members":
            return sum(1 for m in guild.members if not m.bot)
        elif counter_type == "bots":
            return sum(1 for m in guild.members if m.bot)
        elif counter_type == "online":
            return sum(1 for m in guild.members if m.status != Status.offline)
        elif counter_type == "offline":
            return sum(1 for m in guild.members if m.status == Status.offline)
        elif counter_type == "boost":
            return guild.premium_subscription_count or 0
        elif counter_type == "boostlevel":
            return guild.premium_tier
        return 0

    async def _update_counter_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        counter_type: str,
        base_name: str,
    ) -> None:
        channel = guild.get_channel(channel_id)
        if channel is None or not isinstance(channel, (VoiceChannel, TextChannel)):
            await self.bot.pool.execute(
                "DELETE FROM server_counters WHERE channel_id = %s",
                channel_id,
            )
            return

        value = self._get_counter_value(guild, counter_type)
        new_name = self._format_counter_name(channel, base_name, value)
        if channel.name != new_name:
            try:
                await channel.edit(name=new_name, reason=f"Counter update: {counter_type}")
            except (HTTPException, NotFound):
                pass

    @tasks.loop(minutes=1)
    async def counter_presence_loop(self) -> None:
        try:
            rows = await self.bot.pool.fetch(
                "SELECT guild_id, channel_id, counter_type, base_name FROM server_counters WHERE counter_type IN ('online', 'offline')"
            )
            for row in rows:
                guild = self.bot.get_guild(row["guild_id"])
                if guild is None:
                    continue
                await self._update_counter_channel(
                    guild,
                    row["channel_id"],
                    row["counter_type"],
                    row["base_name"],
                )
        except Exception as e:
            logger.error(f"Counter presence loop error: {e}")

    @counter_presence_loop.before_loop
    async def before_counter_presence_loop(self) -> None:
        await self.bot.wait_until_ready()

    @Cog.listener("on_member_join")
    async def counter_member_join_listener(self, member: Member) -> None:
        target_type = "bots" if member.bot else "members"
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, counter_type, base_name FROM server_counters WHERE guild_id = %s AND counter_type = %s",
            member.guild.id,
            target_type,
        )
        for row in rows:
            await self._update_counter_channel(
                member.guild,
                row["channel_id"],
                row["counter_type"],
                row["base_name"],
            )

    @Cog.listener("on_member_remove")
    async def counter_member_remove_listener(self, member: Member) -> None:
        target_type = "bots" if member.bot else "members"
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, counter_type, base_name FROM server_counters WHERE guild_id = %s AND counter_type = %s",
            member.guild.id,
            target_type,
        )
        for row in rows:
            await self._update_counter_channel(
                member.guild,
                row["channel_id"],
                row["counter_type"],
                row["base_name"],
            )

    @hybrid_group(
        name="counters",
        aliases=["statistics", "counter"],
        description="Configure server statistics counters",
        invoke_without_command=True,
    )
    @has_permissions(manage_channels=True)
    async def counters(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    @counters.command(
        name="setup",
        aliases=["quicksetup"],
        description="Quickly setup 4 locked voice counter channels",
        example="counters setup",
    )
    @has_permissions(manage_channels=True)
    async def counters_setup(self, ctx: Context):
        existing_types = await self.bot.pool.fetch(
            "SELECT counter_type FROM server_counters WHERE guild_id = %s",
            ctx.guild.id,
        )
        existing_set = {r["counter_type"] for r in existing_types}

        setup_specs = [
            ("members", "Members"),
            ("online", "Online"),
            ("offline", "Offline"),
            ("bots", "Bots"),
        ]

        needed_specs = [spec for spec in setup_specs if spec[0] not in existing_set]
        if not needed_specs:
            return await ctx.warn("All setup counters (`Members`, `Online`, `Offline`, `Bots`) are already configured.")

        category = None
        for cat in ctx.guild.categories:
            if cat.name.lower() in ("server stats", "server statistics", "stats", "counters"):
                category = cat
                break

        if category is None:
            try:
                category = await ctx.guild.create_category(
                    "Server Stats",
                    reason=f"{ctx.author}: Counter quicksetup category",
                )
            except HTTPException as e:
                return await ctx.deny(f"Failed to create category for counters: {e}")

        overwrites = {
            ctx.guild.default_role: PermissionOverwrite(connect=False, view_channel=True)
        }

        created = []
        for ctype, base_label in needed_specs:
            val = self._get_counter_value(ctx.guild, ctype)
            initial_name = f"{base_label}: {val}"
            try:
                channel = await ctx.guild.create_voice_channel(
                    name=initial_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"{ctx.author}: Counter quicksetup ({ctype})",
                )
            except HTTPException as e:
                return await ctx.deny(f"Failed to create voice channel `{initial_name}`: {e}")

            await self.bot.pool.execute(
                """
                INSERT INTO server_counters (guild_id, channel_id, counter_type, base_name)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE counter_type = VALUES(counter_type), base_name = VALUES(base_name)
                """,
                ctx.guild.id,
                channel.id,
                ctype,
                base_label,
            )
            created.append(f"{channel.mention} (`{ctype}`)")

        return await ctx.approve(f"Successfully configured counters:\n" + "\n".join(f"• {c}" for c in created))

    @counters.command(
        name="set",
        description="Set a channel as a counter channel",
        example="counters set #channel members",
    )
    @has_permissions(manage_channels=True)
    async def counters_set(
        self,
        ctx: Context,
        channel: Union[VoiceChannel, TextChannel],
        counter_type: str,
    ):
        ctype_normalized = counter_type.lower().strip()
        if ctype_normalized not in self.VALID_COUNTER_TYPES:
            valid_list = ", ".join(f"`{k}`" for k in ("boost", "online", "offline", "bots", "members", "boostlevel"))
            return await ctx.warn(f"`{counter_type}` is not a valid counter type. Choose from: {valid_list}")

        ctype = self.VALID_COUNTER_TYPES[ctype_normalized]
        raw_base = self._strip_counter_suffix(channel.name)
        value = self._get_counter_value(ctx.guild, ctype)
        new_name = self._format_counter_name(channel, raw_base, value)

        try:
            await channel.edit(name=new_name, reason=f"{ctx.author}: Set counter ({ctype})")
        except HTTPException as e:
            return await ctx.deny(f"Failed to update channel name: {e}")

        await self.bot.pool.execute(
            """
            INSERT INTO server_counters (guild_id, channel_id, counter_type, base_name)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE counter_type = VALUES(counter_type), base_name = VALUES(base_name)
            """,
            ctx.guild.id,
            channel.id,
            ctype,
            raw_base,
        )

        return await ctx.approve(f"Set {channel.mention} as a **{ctype}** counter (`{new_name}`).")

    @counters.command(
        name="edit",
        description="Edit an existing counter channel's type",
        example="counters edit #channel online",
    )
    @has_permissions(manage_channels=True)
    async def counters_edit(
        self,
        ctx: Context,
        channel: Union[VoiceChannel, TextChannel],
        counter_type: str,
    ):
        ctype_normalized = counter_type.lower().strip()
        if ctype_normalized not in self.VALID_COUNTER_TYPES:
            valid_list = ", ".join(f"`{k}`" for k in ("boost", "online", "offline", "bots", "members", "boostlevel"))
            return await ctx.warn(f"`{counter_type}` is not a valid counter type. Choose from: {valid_list}")

        ctype = self.VALID_COUNTER_TYPES[ctype_normalized]
        existing = await self.bot.pool.fetchrow(
            "SELECT base_name FROM server_counters WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,
        )
        if not existing:
            return await ctx.warn(f"{channel.mention} is not registered as a counter channel. Use `{ctx.clean_prefix}counters set` instead.")

        base_name = existing["base_name"] or self._strip_counter_suffix(channel.name)
        value = self._get_counter_value(ctx.guild, ctype)
        new_name = self._format_counter_name(channel, base_name, value)

        try:
            await channel.edit(name=new_name, reason=f"{ctx.author}: Edit counter ({ctype})")
        except HTTPException as e:
            return await ctx.deny(f"Failed to update channel name: {e}")

        await self.bot.pool.execute(
            "UPDATE server_counters SET counter_type = %s, base_name = %s WHERE guild_id = %s AND channel_id = %s",
            ctype,
            base_name,
            ctx.guild.id,
            channel.id,
        )

        return await ctx.approve(f"Updated {channel.mention} counter type to **{ctype}** (`{new_name}`).")

    @counters.command(
        name="remove",
        description="Remove a counter channel and restore its base name",
        example="counters remove #channel",
    )
    @has_permissions(manage_channels=True)
    async def counters_remove(
        self,
        ctx: Context,
        channel: Union[VoiceChannel, TextChannel],
    ):
        existing = await self.bot.pool.fetchrow(
            "SELECT base_name FROM server_counters WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,
        )
        if not existing:
            return await ctx.warn(f"{channel.mention} is not registered as a counter channel.")

        base_name = existing["base_name"] or self._strip_counter_suffix(channel.name)
        await self.bot.pool.execute(
            "DELETE FROM server_counters WHERE guild_id = %s AND channel_id = %s",
            ctx.guild.id,
            channel.id,
        )

        try:
            await channel.edit(name=base_name, reason=f"{ctx.author}: Removed counter")
        except HTTPException:
            pass

        return await ctx.approve(f"Removed counter from {channel.mention} and reverted name to `{base_name}`.")

    @counters.command(
        name="list",
        description="List all active counter channels in the server",
        example="counters list",
    )
    @has_permissions(manage_channels=True)
    async def counters_list(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, counter_type, base_name FROM server_counters WHERE guild_id = %s",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.warn("There are no counter channels configured in this server.")

        entries = []
        for i, row in enumerate(rows, start=1):
            ch = ctx.guild.get_channel(row["channel_id"])
            mention = ch.mention if ch else f"Deleted Channel (`{row['channel_id']}`)"
            entries.append(f"`{i}` {mention} — **Type:** `{row['counter_type']}` | **Base:** `{row['base_name']}`")

        total_pages = (len(entries) + 4) // 5
        embeds = []
        embed = discord.Embed(color=COLORS.neutral, title=f"Counter Channels ({len(entries)})", description="")
        count = 0

        for entry in entries:
            embed.description += f"{entry}\n"
            count += 1
            if count == 5:
                embed.set_footer(text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)")
                embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
                embeds.append(embed)
                embed = discord.Embed(color=COLORS.neutral, title=f"Counter Channels ({len(entries)})", description="")
                count = 0

        if count > 0:
            embed.set_footer(text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)")
            embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
            embeds.append(embed)

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0])

    @counters.command(
        name="reset",
        aliases=["disable"],
        description="Remove all counter channels in the server",
        example="counters reset",
    )
    @has_permissions(manage_channels=True)
    async def counters_reset(self, ctx: Context):
        rows = await self.bot.pool.fetch(
            "SELECT channel_id, base_name FROM server_counters WHERE guild_id = %s",
            ctx.guild.id,
        )
        if not rows:
            return await ctx.warn("No counter channels are currently set up in this server.")

        await self.bot.pool.execute(
            "DELETE FROM server_counters WHERE guild_id = %s",
            ctx.guild.id,
        )

        for row in rows:
            ch = ctx.guild.get_channel(row["channel_id"])
            if ch and isinstance(ch, (VoiceChannel, TextChannel)):
                base = row["base_name"] or self._strip_counter_suffix(ch.name)
                try:
                    await ch.edit(name=base, reason=f"{ctx.author}: Counter module reset")
                except HTTPException:
                    pass

        return await ctx.approve(f"Successfully removed **{len(rows)}** counter channel(s) and restored their names.")

    # ------------------------------------------------------------------ #
    # Bot customization (per server)
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="customize",
        aliases=["customization", "customise"],
        invoke_without_command=True,
        description="Change the bot's profile for this server.",
        example="customize avatar",
    )
    @has_permissions(manage_guild=True)
    async def customize(self, ctx: Context):
        return await ctx.send_help(ctx.command)

    async def _log_customization(
        self,
        ctx: Context,
        *,
        name: str,
        content: Optional[str] = None,
        image: Optional[bytes] = None,
    ) -> None:
        """Log a bot customization to the avatar changes channel for review."""

        channel = self.bot.get_channel(int(CHANNELS.AVATAR_CHANGES))
        if channel is None:
            return

        embed = (
            Embed(
                color=COLORS.neutral,
                title=name,
                timestamp=datetime.utcnow(),
            )
            .add_field(
                name="Server Name",
                value=ctx.guild.name,
                inline=True,
            )
            .add_field(
                name="Server ID",
                value=ctx.guild.id,
                inline=True,
            )
            .set_author(
                name=ctx.author.name, icon_url=ctx.author.display_avatar.url
            )
        )

        if content:
            embed.add_field(
                name="Content",
                value=content,
                inline=False,
            )

        if image is not None:
            embed.set_thumbnail(url="attachment://img.png")

        try:
            await channel.send(
                embed=embed,
                file=discord.File(io.BytesIO(image), filename="img.png")
                if image is not None
                else None,
                view=GuildBlacklistView(self.bot, ctx.guild.id),
            )
        except HTTPException as e:
            logger.error(f"Failed to log customization: {e}")

    async def _get_profile_image(
        self, ctx: Context, attachment: Optional[Attachment], url: Optional[str]
    ) -> Optional[bytes]:
        """Read the image bytes from an attachment, or download them from a url."""

        if attachment:
            if attachment.content_type and not attachment.content_type.startswith(
                "image/"
            ):
                await ctx.warn("The attachment you provided is not an image.")
                return None
            return await attachment.read()

        if not url:
            return None

        url = url.strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            await ctx.warn("Please provide a valid image url.")
            return None

        async with ctx.typing():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            await ctx.warn(
                                f"Failed to download that image (status `{response.status}`)."
                            )
                            return None
                        image = await response.read()
            except aiohttp.ClientError as e:
                logger.error(f"customize image download failed: {e}")
                await ctx.warn("Failed to download that image, please try again.")
                return None

        if not image:
            await ctx.warn("That url didn't return a valid image.")
            return None

        return image

    @customize.command(
        name="avatar",
        aliases=["profilepicture", "pfp"],
        description="Change the bot's avatar for this server.",
        example="customize avatar https://i.imgur.com/avatar.png",
    )
    @has_permissions(manage_guild=True)
    async def customize_avatar(
        self,
        ctx: Context,
        attachment: Optional[Attachment] = None,
        url: Optional[str] = None,
    ):
        if attachment is None and not url:
            return await ctx.warn(
                f"Please provide an image as an attachment or a url. (`{ctx.clean_prefix}customize avatar https://i.imgur.com/avatar.png`)"
            )

        image = await self._get_profile_image(ctx, attachment, url)
        if image is None:
            return

        try:
            await ctx.guild.me.edit(avatar=image)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to change the bot's avatar: `{e}`")

        await self._log_customization(ctx, name="Avatar", image=image)

        return await ctx.approve("Changed the bot's **avatar** for this server.")

    @customize.command(
        name="banner",
        description="Change the bot's banner for this server.",
        example="customize banner https://i.imgur.com/banner.png",
    )
    @has_permissions(manage_guild=True)
    async def customize_banner(
        self,
        ctx: Context,
        attachment: Optional[Attachment] = None,
        url: Optional[str] = None,
    ):
        if attachment is None and not url:
            return await ctx.warn(
                f"Please provide an image as an attachment or a url. (`{ctx.clean_prefix}customize banner https://i.imgur.com/banner.png`)"
            )

        image = await self._get_profile_image(ctx, attachment, url)
        if image is None:
            return

        try:
            await ctx.guild.me.edit(banner=image)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to change the bot's banner: `{e}`")

        await self._log_customization(ctx, name="Banner", image=image)

        return await ctx.approve("Changed the bot's **banner** for this server.")

    @customize.command(
        name="bio",
        aliases=["aboutme", "description"],
        description="Change the bot's bio for this server.",
        example="customize bio the best music bot around.",
    )
    @has_permissions(manage_guild=True)
    async def customize_bio(self, ctx: Context, *, bio: Optional[str]):
        if not bio:
            return await ctx.warn("Please provide a new bio for the bot.")

        bio = bio.strip()
        if len(bio) > 190:
            return await ctx.warn("The bio can't be longer than **190** characters.")

        try:
            await ctx.guild.me.edit(bio=bio)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to change the bot's bio: `{e}`")

        await self._log_customization(ctx, name="Bio", content=bio)

        return await ctx.approve("Changed the bot's **bio** for this server.")

    @customize.command(
        name="name",
        description="Change the bot's name for this server.",
        example="customize name hollow",
    )
    @has_permissions(manage_guild=True)
    async def customize_name(self, ctx: Context, *, name: Optional[str]):
        if not name:
            return await ctx.warn("Please provide a new name for the bot.")

        name = name.strip()

        try:
            await ctx.guild.me.edit(nick=name)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to change the bot's name: `{e}`")

        await self._log_customization(ctx, name="Name", content=name)

        return await ctx.approve("Changed the bot's **name** for this server.")

    @customize.command(
        name="remove",
        description="Remove a bot customization for this server.",
        example="customize remove avatar",
    )
    @has_permissions(manage_guild=True)
    async def customize_remove(self, ctx: Context, type: str):
        """Remove one of the bot's per server customizations."""

        option = type.strip().lower()

        if option not in ("avatar", "banner", "bio", "name"):
            valid = ", ".join(f"`{t}`" for t in ("avatar", "banner", "bio", "name"))
            return await ctx.warn(f"`{type}` is not a valid type. Choose from: {valid}")

        try:
            if option == "avatar":
                await ctx.guild.me.edit(avatar=None)
            elif option == "banner":
                await ctx.guild.me.edit(banner=None)
            elif option == "bio":
                await ctx.guild.me.edit(bio=None)
            else:
                await ctx.guild.me.edit(nick=None)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to remove the bot's {option}: `{e}`")

        await self._log_customization(ctx, name=f"Removed {option.capitalize()}")

        return await ctx.approve(f"Removed the bot's **{option}** for this server.")

    @customize.command(
        name="reset",
        description="Remove every bot customization for this server.",
        example="customize reset",
    )
    @has_permissions(manage_guild=True)
    async def customize_reset(self, ctx: Context):
        """Remove every one of the bot's per server customizations."""

        try:
            await ctx.guild.me.edit(avatar=None, banner=None, bio=None, nick=None)
        except (HTTPException, ValueError) as e:
            return await ctx.deny(f"Failed to reset the bot's customizations: `{e}`")

        await self._log_customization(ctx, name="Reset")

        return await ctx.approve("Removed every **customization** for this server.")
