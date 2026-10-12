from typing import Optional, Union
import asyncio
import socket
import requests
import io
import aiohttp
import re
from datetime import timezone as dt_timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from discord.ext.commands import Cog, Command, Group, command

from base.managers.types import CogMeta
from base.context import Context, SITE_URL
from base.managers.paginator import *
from base.config import *
from cogs.information import helpers as info_helpers
from random import random, choice
from humanize import naturaltime
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
    PartialEmoji,
    Emoji,
)
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
    guild_only,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.ui import View
import discord
from discord.utils import format_dt, oauth_url

from psutil import Process
from difflib import get_close_matches
from base.managers.predicates import has_permissions

from PIL import Image, ImageOps
from colorthief import ColorThief


class Information(CogMeta):
    process: Process

    @hybrid_command(
        name="inviteinfo",
        aliases=["ii"],
        description="Get information about a guild using their invite code.",
        example="inviteinfo https://discord.gg/breeds",
    )
    async def inviteinfo(self, ctx: Context, *, invite: Invite = None) -> None:  # type: ignore
        if invite is None:
            return await ctx.warn(f"An `invite code` is missing.")

        return await ctx.embed(
            title=f"Invite code: {invite.code}",
            author={"name": ctx.author.name, "icon_url": ctx.author.display_avatar.url},
            thumbnail=(
                invite.guild.icon.url if invite.guild.icon else "https://none.none"
            ),  # type: ignore
            fields=[
                {
                    "name": "Channel & Invite",
                    "value": f"**Name:** {invite.channel.name} (`text`) \n**ID:** `{invite.channel.id}` \n**Created**: {format_dt(invite.created_at, 'F') if invite.created_at else 'Unkown'} ({format_dt(invite.created_at, 'R') if invite.created_at else ''}) \n**Invite Expiration:** {format_dt(invite.expires_at) if invite.expires_at else 'Never'} \n**Inviter:** {invite.inviter if invite.inviter else 'Vanity URL'} \n **Temporary:** {invite.temporary if invite.temporary else 'N/A'} \n**Usage:** {invite.uses}",  # type: ignore
                    "inline": True,
                },
                {
                    "name": "Guild",
                    "value": f"**Name:** {invite.guild.name} \n**ID:** `{invite.guild.id}` \n**Created:** {format_dt(invite.guild.created_at, 'F')} ({format_dt(invite.guild.created_at, 'R')}) \n**Members:** {invite.approximate_member_count: ,} \n**Online Members:** {invite.approximate_presence_count: ,} \n**Verification Level:** {invite.guild.verification_level}",  # type: ignore
                    "inline": True,
                },
            ],
            buttons=[
                {
                    "label": "Icon",
                    "emoji": "🖼️",
                    "url": (
                        invite.guild.icon.url
                        if invite.guild.icon
                        else "https://none.none"
                    ),  # type: ignore
                },
                {
                    "label": "Splash",
                    "emoji": "🎨",
                    "url": (
                        invite.guild.splash.url
                        if invite.guild.splash
                        else "https://none.none"
                    ),  # type: ignore
                },
                {
                    "label": "Banner",
                    "emoji": "🏳️",
                    "url": (
                        invite.guild.banner.url
                        if invite.guild.banner
                        else "https://none.none"
                    ),  # type: ignore
                },
            ],
        )

    @hybrid_command(
        name="userinfo",
        aliases=["ui", "whois"],
        description="Get information about a user.",
        example="userinfo v0b2",
    )
    async def userinfo(
        self, ctx: Context, *, user: Union[User, Member] = Author
    ) -> Message:
        user = user or ctx.author
        member = ctx.guild.get_member(user.id) if ctx.guild else None

        flags = user.public_flags
        emojis = []

        if flags.staff: emojis.append(EMOJIS.STAFF)
        if flags.partner: emojis.append(EMOJIS.PARTNER)
        if flags.active_developer: emojis.append(EMOJIS.ACTIVE_DEVELOPER)
        if flags.bug_hunter: emojis.append(EMOJIS.BUG_HUNTER)
        if flags.early_supporter: emojis.append(EMOJIS.EARLY_SUPPORTER)
        if flags.hypesquad_balance: emojis.append(EMOJIS.HYPESQUAD_BALANCE)
        if flags.hypesquad_bravery: emojis.append(EMOJIS.HYPESQUAD_BRAVERY)
        if flags.hypesquad_brilliance: emojis.append(EMOJIS.HYPESQUAD_BRILLIANCE)

        if user.display_avatar.is_animated() or (member and member.premium_since):
            emojis.append(EMOJIS.NITRO)
        if member and member.premium_since:
            emojis.append(EMOJIS.BOOSTER)

        if isinstance(user, Member):
            if user.activities:
                for activity in user.activities:
                    if isinstance(activity, discord.Spotify):
                        emojis.append(
                            f"\n {EMOJIS.SPOTIFY} Listening to **[{activity.title}]({activity.track_url})** by **`{activity.artists[0]}`**"
                        )

        emoji_string = "".join(emojis) if emojis else ""

        fields = [
            {
                "name": "Dates",
                "value": (
                    f"**Created:** {format_dt(user.created_at, 'd')}{format_dt(user.created_at, 't')} ({format_dt(user.created_at, 'R')})"
                    + (
                        f"\n**Joined:** {format_dt(member.joined_at, 'd')}{format_dt(member.joined_at, 't')} ({format_dt(member.joined_at, 'R')})"
                        if member and member.joined_at
                        else ""
                    )
                ),
            }
        ]

        footer = f"{len(user.mutual_guilds)} mutual guild(s)"
        if member:
            roles = [role.mention for role in member.roles if role.id != ctx.guild.id]  # type: ignore
            role_list = ", ".join(roles) if roles else "None"
            fields.append({"name": f"Roles ({len(roles)})", "value": role_list})

            members = sorted(ctx.guild.members, key=lambda m: m.joined_at)  # type: ignore
            position = members.index(member) + 1
            footer += f" • join position: {position}"

        return await ctx.embed(
            author={"name": f"{user.name} ({user.id})"},
            description=f"{emoji_string}",
            thumbnail=(
                user.display_avatar.url if user.display_avatar else "https://none.none"
            ),
            fields=fields,  # type: ignore
            footer={"text": footer},
        )

    @hybrid_command(
        name="hex", aliases=["dominant"], description="Get a hex code from an image.",
        example="hex @voby",
    )
    async def hex(self, ctx: Context, *, user: User = None):  # type: ignore
        user = user or ctx.author
        image_url = None

        if ctx.message.attachments:
            image_url = ctx.message.attachments[0].url
        elif user.display_avatar:
            image_url = user.display_avatar.url
        else:
            image_url = ctx.author.display_avatar.url

        async with ctx.typing():
            try:
                response = requests.get(image_url)  # type: ignore
                image_bytes = io.BytesIO(response.content)
                image = Image.open(image_bytes)
                color_thief = ColorThief(image_bytes)
                dominant = color_thief.get_color(quality=1)

                hex_discord = int("0x{:02x}{:02x}{:02x}".format(*dominant), 16)
                hex = "#{:02x}{:02x}{:02x}".format(*dominant)

                return await ctx.embed(
                    title=f"Showing hex code: {hex}",
                    color=hex_discord,  # type: ignore
                    fields=[{"name": "RGB Value", "value": f"{dominant}"}],
                )
            except Exception as e:
                return await ctx.warn(f"An error occurred: `{e}`")

    @hybrid_command(name="avatar", aliases=["av"], description="Get a user's avatar.", example="avatar @voby")
    async def avatar(
        self, ctx: Context, *, user: Union[User, Member] = Author
    ) -> Message:
        user = user or ctx.author
        member = ctx.guild.get_member(user.id) if ctx.guild else None

        return await ctx.embed(
            title=f"{user}'s avatar",
            url=user.display_avatar.url,
            image=user.display_avatar.url,
        )

    @hybrid_command(name="banner", description="Get a user's banner.", example="banner @voby")
    async def banner(
        self, ctx: Context, *, user: Union[User, Member] = Author
    ) -> Message:  # type: ignore
        user = user or ctx.author
        member = ctx.guild.get_member(user.id) if ctx.guild else None

        user = await self.bot.fetch_user(user.id)

        if user.banner:
            embed = discord.Embed(
                title=f"{user.name}'s banner", color=COLORS.neutral, url=user.banner.url
            )
            embed.set_image(url=user.banner.url)
            await ctx.send(embed=embed)
        else:
            await ctx.warn(f"This user does not have a banner set.")

    @hybrid_command(
        name="serverinfo",
        aliases=["si"],
        description="Get information about the server",
        example="serverinfo",
    )
    async def serverinfo(self, ctx: Context):
        premium_tier = ctx.guild.premium_tier  # type: ignore

        if premium_tier == 0:
            role_limit = 250
        elif premium_tier == 1:
            role_limit = 250
        elif premium_tier == 2:
            role_limit = 500
        elif premium_tier == 3:
            role_limit = 2500

        return await ctx.embed(
            title=f"{ctx.guild.name}",  # type: ignore
            description=f"Server created on {format_dt(ctx.guild.created_at, 'D')} ({format_dt(ctx.guild.created_at, 'R')})",  # type: ignore
            fields=[
                {
                    "name": "Owner",
                    "value": f"{ctx.guild.owner.mention} \n({ctx.guild.owner.id})",  # type: ignore
                    "inline": True,
                },
                {
                    "name": "Members",
                    "value": f"**Total:** {ctx.guild.member_count} \n**Humans:** {sum(1 for member in ctx.guild.members if not member.bot)} \n**Bots:** {sum(1 for member in ctx.guild.members if member.bot)}",  # type: ignore
                    "inline": True,
                },
                {
                    "name": "Information",
                    "value": f"**Verification:** {ctx.guild.verification_level} \n**Boosts:** {ctx.guild.premium_subscription_count}",  # type: ignore
                    "inline": True,
                },
                {
                    "name": "Design",
                    "value": f"**Splash:** [click here]({ctx.guild.splash.url if ctx.guild.splash else 'https://none.none'}) \n**Banner:** [click here]({ctx.guild.banner.url if ctx.guild.banner else 'https://none.none'}) \n**Icon:** [click here]({ctx.guild.icon.url if ctx.guild.icon else 'https://none.none'})",  # type: ignore
                    "inline": True,
                },
                {
                    "name": f"Channels ({len(ctx.guild.channels)})",  # type: ignore
                    "value": f"**Text:** {len(ctx.guild.text_channels)} \n**Voice:** {len(ctx.guild.voice_channels)} \n**Categories:** {len(ctx.guild.categories)}",  # type: ignore
                    "inline": True,
                },
                {
                    "name": "Counts",
                    "value": f"**Roles:** {len(ctx.guild.roles)}/{role_limit} \n**Emojis:** {len(ctx.guild.emojis)}/{ctx.guild.emoji_limit} \n**Boosters:** {len(ctx.guild.premium_subscribers)}",  # type: ignore
                    "inline": True,
                },
            ],
            thumbnail=ctx.guild.icon.url if ctx.guild.icon else "https://none.none",  # type: ignore
            author={
                "name": ctx.author.display_name,
                "icon_url": ctx.author.display_avatar.url,
            },
        )

    @hybrid_command(
        name="ping",
        aliases=["latency", "websocket"],
        description="Get the latency of the bot",
        example="ping",
    )
    async def ping(self, ctx: Context):
        pings = Path(__file__).resolve().parents[2] / "base" / "data" / "pings.txt"
        with open(pings, "r") as f:
            lines = f.readlines()
            randomping = choice(lines).strip()
        start = time.time()
        latency_ms = self.bot.ping
        message = await ctx.send(content="ping...")
        finished = time.time() - start
        edit_ms = round(finished * 1000, 1)
        return await message.edit(
            content=f"it took `{latency_ms}ms` to ping **{randomping}** (edit: `{edit_ms}ms`)"
        )

    @hybrid_group(
        name="boosters",
        invoke_without_command=True,
        description="See a list of boosters in the guild",
        example="boosters",
    )
    async def boosters(self, ctx: Context):
        boosters = ctx.guild.premium_subscribers  # type: ignore
        if not boosters:
            return await ctx.warn(f"There are no **boosters** in this guild.")

        entries = []
        for i, member in enumerate(boosters, start=1):
            boosted_at = member.premium_since
            boosted_ago = naturaltime(boosted_at) if boosted_at else "Unknown"
            entries.append(f"`{i}`  **{member.name}** - Boosted: **{boosted_ago}**")

        total_pages = (len(entries) + 9) // 10  # Calculates total pages, rounding up.
        embeds = []
        count = 0
        embed = discord.Embed(
            color=COLORS.neutral, title=f"Boosters ({len(entries)})", description=""
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
                    )
                )
                embed = discord.Embed(
                    color=COLORS.neutral,
                    title=f"Boosters ({len(entries)})",
                    description="",
                )
                count = 0

        if count > 0:
            embeds.append(
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
                )
            )

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0])

    @Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Check if the boost status has been removed."""
        if before.premium_since and not after.premium_since:
            await self.bot.log_lost_boost(before)

    @boosters.command(
        name="lost", description="Check how many boosters the guild has lost.",
        example="boosters lost",
    )
    async def boosters_lost(self, ctx: Context):
        guild_id = ctx.guild.id  # type: ignore
        query = """
        SELECT user_id, username, discriminator, lost_at
        FROM lost_boosters
        WHERE guild_id = %s
        ORDER BY lost_at DESC
        """
        lost_boosters = await self.bot.pool.fetch(query, guild_id)

        if not lost_boosters:
            return await ctx.warn("No boosters have lost their boost in this guild.")

        embeds = []
        count = 0
        entries = []

        for i, row in enumerate(lost_boosters, start=1):
            user_id = row["user_id"]
            username = row["username"]
            discriminator = row["discriminator"]
            lost_at = row["lost_at"]
            lost_ago = naturaltime(lost_at)

            entries.append(
                f"`{i}`  **{username}#{discriminator}** - Lost Boost: **{lost_ago}**"
            )

        embed = discord.Embed(
            color=COLORS.neutral,
            title=f"Lost Boosters ({len(entries)})",
            description="",
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(embed)
                embed = discord.Embed(
                    color=COLORS.neutral,
                    description="",
                    title=f"Lost Boosters ({len(entries)})",
                )
                count = 0

        if count > 0:
            embeds.append(embed)

        await ctx.paginate(embeds)

    @hybrid_command(
        name="membercount",
        aliases=["mc"],
        description="Get the member count of the guild.",
        example="membercount",
    )
    async def membercount(self, ctx: Context):
        total_members = ctx.guild.member_count  # type: ignore
        bot_count = sum(1 for member in ctx.guild.members if member.bot)  # type: ignore
        user_count = total_members - bot_count  # type: ignore

        bot_percentage = (bot_count / total_members) * 100 if total_members > 0 else 0  # type: ignore
        user_percentage = (user_count / total_members) * 100 if total_members > 0 else 0  # type: ignore
        return await ctx.embed(
            title=f"",
            description=f"**Members:** `{ctx.guild.member_count}` \n**Users:** `{sum(1 for member in ctx.guild.members if not member.bot)}` ({user_percentage:.2f}%)\n**Bots:** `{sum(1 for member in ctx.guild.members if member.bot)}` ({bot_percentage:.2f}%)",  # type: ignore
        )

    @hybrid_command(
        name="roleinfo",
        aliases=["rinfo", "ri"],
        description="Get information about a role",
        example="roleinfo @role",
    )
    async def roleinfo(self, ctx: Context, role: Role):
        return await ctx.embed(
            title=role.name,
            fields=[
                {"name": "Role ID", "value": f"`{role.id}`", "inline": True},
                {
                    "name": "Guild",
                    "value": f"{role.guild.name} (`{role.guild.id}`)",
                    "inline": True,
                },
                {"name": "Color", "value": f"{role.color}", "inline": True},
                {
                    "name": "Creation Date",
                    "value": f"{format_dt(role.created_at, 'D')} (**{format_dt(role.created_at, 'R')}**)",
                },
                {
                    "name": f"{len(role.members)} Member(s)",
                    "value": ", ".join([member.name for member in role.members]),
                },
            ],
            author={
                "name": ctx.author.display_name,
                "icon_url": ctx.author.display_avatar.url,
            },
            thumbnail=role.icon.url if role.icon else "https://none.none",
        )

    @hybrid_command(
        name="serveravatar",
        aliases=["serverav", "sav"],
        description="Get a user's server avatar",
        example="serveravatar @voby",
    )
    async def serveravatar(
        self, ctx: Context, *, user: Union[User, Member] = Author
    ) -> Message:
        user = user or ctx.author
        member = ctx.guild.get_member(user.id) if ctx.guild else None

        if not user.guild_avatar:  # type: ignore
            return await ctx.warn(f"{user} doesn't have a **server avatar** set.")

        return await ctx.embed(
            title=f"{user}'s server avatar",
            image=user.guild_avatar.url,  # type: ignore
            url=user.guild_avatar.url,  # type: ignore
        )

    @hybrid_command(
        name="serverbanner",
        aliases=["sbanner"],
        description="Get a user's server banner.",
        example="serverbanner @voby",
    )
    async def serverbanner(
        self, ctx: Context, *, user: Union[User, Member] = Author
    ) -> Message:
        user = user or ctx.author
        member = ctx.guild.get_member(user.id) if ctx.guild else None

        if not user.guild_banner:  # type: ignore
            return await ctx.warn(f"{user} doesn't have a **server banner** set.")

        return await ctx.embed(
            title=f"{user}'s server banner",
            url=user.guild_banner.url,  # type: ignore
            image=user.guild_banner.url,  # type: ignore
        )

    @hybrid_command(name="bots", description="Get a list of bots.", example="bots")
    async def bots(self, ctx: Context):
        bots = [b for b in ctx.guild.members if b.bot]  # type: ignore
        count = 0
        embeds = []

        entries = [f"`{i}`  **{m.name}**" for i, m in enumerate(bots, start=1)]
        embed = discord.Embed(
            color=COLORS.neutral, title=f"List of bots ({len(entries)})", description=""
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            count += 1

            if count == 10:
                embeds.append(embed)
                embed = discord.Embed(
                    color=COLORS.neutral,
                    description="",
                    title=f"List of bots ({len(entries)})",
                )
                count = 0

        if count > 0:
            embeds.append(embed)

        await ctx.paginate(embeds)

    @hybrid_command(
        name="botinfo",
        aliases=["about", "bi"],
        description="Get information about the bot.",
        example="botinfo",
    )
    async def botinfo(self, ctx: Context):
        return await ctx.embed(
            author={
                "name": ctx.author.display_name,
                "icon_url": (
                    ctx.author.display_avatar.url
                    if ctx.author.display_avatar
                    else "https://none.none"
                ),
            },
            fields=[
                {
                    "name": "Bot",
                    "value": f"Users: **{len(self.bot.users):,}** \nServers: **{len(self.bot.guilds):,}** \nPing: **{self.bot.ping}ms**",
                    "inline": True,
                },
                {
                    "name": "Info",
                    "value": f"RAM: **{self.bot.format_size(Process().memory_info().rss)}** \nCPU: **{Process().cpu_percent()}%** \nBooted: **{self.bot.booted}**",
                    "inline": True,
                },
            ],
            thumbnail=(
                self.bot.user.display_avatar.url
                if self.bot.user.display_avatar
                else "https://none.none"
            ),  # type: ignore
            footer={
                "text": f"Commands: {len([cmd for cmd in self.bot.walk_commands() if cmd.cog_name != 'Jishaku'])} | lines: {self.bot.linecount: ,}"
            },
        )

    @hybrid_command(name="bans", description="Get a list of banned users.", example="bans")
    async def bans(self, ctx: Context):
        bans = [entry async for entry in ctx.guild.bans()]  # type: ignore
        count = 0
        embeds = []

        entries = [
            f"`{i}`  **{ban.user.name}** (`{ban.user.id}`) - Reason: {ban.reason if ban.reason else 'No reason provided'}"
            for i, ban in enumerate(bans, start=1)
        ]

        total_pages = (len(entries) + 9) // 10  # Calculates total pages, rounding up.
        embed = discord.Embed(
            color=COLORS.neutral,
            title=f"List of Banned Users ({len(entries)})",
            description="",
        )

        for entry in entries:
            embed.description += f"{entry}\n"  # type: ignore
            embed.set_author(
                name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url
            )
            count += 1

            if count == 10:
                embeds.append(
                    embed.set_footer(
                        text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
                    )
                )
                embed = discord.Embed(
                    color=COLORS.neutral,
                    title=f"List of Banned Users ({len(entries)})",
                    description="",
                )
                count = 0

        if count > 0:
            embeds.append(
                embed.set_footer(
                    text=f"Page {len(embeds) + 1}/{total_pages} ({len(entries)} entries)"
                )
            )

        if len(embeds) > 1:
            await ctx.paginate(embeds)
        else:
            await ctx.send(embed=embeds[0])

    @hybrid_command(name="uptime", aliases=["up", "ut"], description="Get the bot's uptime", example="uptime")
    async def uptime(self, ctx: Context):
        return await ctx.embed(
            description=f"⏰ **{self.bot.user.display_name}** has been up for: {self.bot.humanize_time(self.bot._uptime)}"  # type: ignore
        )

    @hybrid_command(name="invite", aliases=["inv"], description="Get the bots invite.", example="invite")
    async def invite(self, ctx: Context):
        oauth_url = f"https://discord.com/oauth2/authorize?client_id={self.bot.user.id}&permissions={Permissions.all().value}&scope=bot"  # type: ignore
        button = discord.ui.Button(
            label="Invite",
            style=ButtonStyle.url,
            url=oauth_url,
        )

        view = discord.ui.View()
        view.add_item(button)

        await ctx.reply(view=view)

    @hybrid_command(name="banreason", description="Get a user's ban reason.", example="banreason @voby")
    @has_permissions(ban_members=True)
    async def banreason(self, ctx: Context, *, user: User):
        bans = [entry async for entry in ctx.guild.bans()]  # type: ignore
        entry = next((b for b in bans if b.user.id == user.id), None)

        if not entry:
            return await ctx.warn("This member is **not** banned.")

        return await ctx.embed(
            description=f"**{user.name}** was banned for **{entry.reason}**"
        )

    # @hybrid_command(name="status", description="Get a link to the status page")
    # async def status(self, ctx: Context):
    #     return await ctx.send(
    #         f"{ctx.author.mention}: Experiencing issues? Check your shards status on https://hollow.best/status"
    #     )

    @hybrid_group(
        name="emoji",
        description="Returns a large emoji or server emote",
        invoke_without_command=True,
        example="emoji :name:",
    )
    async def emoji(self, ctx: Context, *, emoji: Union[PartialEmoji, Emoji]):
        return await ctx.send(
            file=await emoji.to_file(
                filename=f"{emoji.name}{'.gif' if emoji.animated else '.png'}"
            )
        )

    @emoji.command(
        name="steal",
        aliases=["copy", "add"],
        description="Downloads emote and adds to server",
        example="emoji steal :name: newname",
    )
    @has_permissions(manage_expressions=True)
    async def emoji_steal(
        self,
        ctx: Context,
        *,
        emoji: PartialEmoji,
        name: str = None,
    ):
        if not name:
            name = emoji.name

        try:
            emoji = await ctx.guild.create_custom_emoji(
                image=await emoji.read(), name=name
            )
            return await ctx.approve(f"Added **emote** {emoji}")
        except Exception as E:
            return await ctx.warn(f"{E}")

    @emoji.command(
        name="remove",
        aliases=["delete"],
        description="Remove an emoji from the server.",
        example="emoji remove :name:",
    )
    @has_permissions(manage_expressions=True)
    async def emoji_remove(self, ctx: Context, *, emoji: PartialEmoji):
        target = await self._resolve_guild_emoji(ctx, emoji)
        if target is None:
            return await ctx.warn(
                "That emoji isn't in this server. I can only remove emojis from this server."
            )
        if not ctx.guild.me.guild_permissions.manage_expressions:  # type: ignore
            return await ctx.warn(
                "I need the **Manage Emojis and Stickers** permission to do that."
            )
        try:
            name = target.name
            await target.delete(reason=f"Removed by {ctx.author}")
            return await ctx.approve(f"Removed **emote** `{name}`")
        except discord.Forbidden:
            return await ctx.warn("I don't have permission to remove that emoji.")
        except discord.HTTPException as e:
            return await ctx.warn(f"Couldn't remove that emoji: `{e.text or e}`")

    @emoji.command(
        name="rename",
        aliases=["edit"],
        description="Rename a server emoji.",
        example="emoji rename :name: newname",
    )
    @has_permissions(manage_expressions=True)
    async def emoji_rename(
        self, ctx: Context, emoji: PartialEmoji, *, name: str
    ):
        clean, error = info_helpers.validate_emoji_name(name)
        if error:
            return await ctx.warn(error)
        target = await self._resolve_guild_emoji(ctx, emoji)
        if target is None:
            return await ctx.warn(
                "That emoji isn't in this server. I can only rename emojis from this server."
            )
        if not ctx.guild.me.guild_permissions.manage_expressions:  # type: ignore
            return await ctx.warn(
                "I need the **Manage Emojis and Stickers** permission to do that."
            )
        try:
            await target.edit(name=clean, reason=f"Renamed by {ctx.author}")
            return await ctx.approve(f"Renamed **emote** to `{clean}` {target}")
        except discord.Forbidden:
            return await ctx.warn("I don't have permission to rename that emoji.")
        except discord.HTTPException as e:
            return await ctx.warn(f"Couldn't rename that emoji: `{e.text or e}`")

    @emoji.command(
        name="info",
        aliases=["information", "show", "view"],
        description="Show details about a server emoji.",
        example="emoji info :name:",
    )
    async def emoji_info(self, ctx: Context, *, emoji: PartialEmoji):
        target = await self._resolve_guild_emoji(ctx, emoji)
        if target is None:
            # Fall back to whatever was passed (e.g. an emoji from another
            # server): still show its basic identity instead of failing.
            name = getattr(emoji, "name", "unknown")
            url = getattr(emoji, "url", None)
            return await ctx.embed(
                title=f":{name}:",
                description=(
                    f"**ID:** `{getattr(emoji, 'id', 'N/A')}`\n"
                    f"**Animated:** {getattr(emoji, 'animated', False)}\n"
                    "Not from this server — add it with "
                    f"`{ctx.clean_prefix}emoji steal`."
                ),
                image=url,
            )
        author = target.user
        return await ctx.embed(
            title=f":{target.name}:",
            thumbnail=target.url,
            fields=[
                {"name": "ID", "value": f"`{target.id}`", "inline": True},
                {"name": "Animated", "value": str(target.animated), "inline": True},
                {"name": "Managed", "value": str(target.managed), "inline": True},
                {
                    "name": "Created",
                    "value": f"{format_dt(target.created_at, 'D')} ({format_dt(target.created_at, 'R')})",
                    "inline": False,
                },
                {
                    "name": "Author",
                    "value": str(author) if author else "Unknown",
                    "inline": True,
                },
                {"name": "URL", "value": f"[open]({target.url})", "inline": True},
            ],
        )

    async def _resolve_guild_emoji(
        self, ctx: Context, emoji: PartialEmoji
    ) -> Optional[Emoji]:
        """Resolve ``emoji`` to a real guild Emoji of this server, if any."""
        if ctx.guild is None:
            return None
        if isinstance(emoji, Emoji):
            return emoji if emoji.guild_id == ctx.guild.id else None
        emoji_id = getattr(emoji, "id", None)
        if emoji_id:
            found = ctx.guild.get_emoji(emoji_id)
            if found is not None:
                return found
        name = getattr(emoji, "name", None)
        if name:
            return discord.utils.get(ctx.guild.emojis, name=name)
        return None

    # ------------------------------------------------------------------ #
    # Shared network / image helpers
    # ------------------------------------------------------------------ #

    async def _fetch_guarded_bytes(self, url: str) -> bytes:
        """Download ``url`` with SSRF protection and size/timeout caps.

        Raises ``ValueError`` with a user-facing message on any failure.
        Every redirect hop is re-validated (scheme, credentials, port and
        DNS resolution against private/link-local/reserved ranges).
        """
        clean, error = info_helpers.validate_fetch_url(url)
        if error or not clean:
            raise ValueError(error or "That URL isn't allowed.")
        timeout = aiohttp.ClientTimeout(total=info_helpers.FETCH_TIMEOUT)
        current = clean
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for _ in range(info_helpers.MAX_REDIRECTS + 1):
                parts = urlparse(current)
                host = parts.hostname or ""
                try:
                    infos = await asyncio.to_thread(
                        socket.getaddrinfo, host, parts.port or 443, type=socket.SOCK_STREAM
                    )
                except OSError:
                    raise ValueError("Couldn't resolve that host.")
                if not info_helpers.sockaddrs_are_public(infos):
                    raise ValueError("That destination is not allowed.")
                try:
                    async with session.get(
                        current,
                        allow_redirects=False,
                        headers={"User-Agent": "Hollow-Phi/1.0"},
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise ValueError("That URL redirected nowhere.")
                            from urllib.parse import urljoin as _urljoin

                            nxt = _urljoin(current, location)
                            clean_nxt, err = info_helpers.validate_fetch_url(nxt)
                            if err or not clean_nxt:
                                raise ValueError(err or "That redirect isn't allowed.")
                            current = clean_nxt
                            continue
                        if response.status != 200:
                            raise ValueError(
                                f"That URL returned status `{response.status}`."
                            )
                        length = response.headers.get("Content-Length")
                        if length and length.isdigit() and int(length) > info_helpers.MAX_IMAGE_BYTES:
                            raise ValueError("That file is too large (max 8MB).")
                        data = await response.read()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    raise ValueError("Couldn't download that file in time.")
                if len(data) > info_helpers.MAX_IMAGE_BYTES:
                    raise ValueError("That file is too large (max 8MB).")
                if not data:
                    raise ValueError("That file is empty.")
                return data
        raise ValueError("Too many redirects.")

    async def _resolve_image_source(
        self, ctx: Context, url: Optional[str] = None
    ) -> tuple:
        """Resolve an image for the image utilities.

        Priority: message attachment, replied-to message attachment, ``url``
        argument, then the author's avatar. Returns ``(bytes, label)``.
        """
        if ctx.message.attachments:
            attachment = ctx.message.attachments[0]
            if not (attachment.content_type or "").startswith("image/"):
                raise ValueError("That attachment isn't an image.")
            if attachment.size > info_helpers.MAX_IMAGE_BYTES:
                raise ValueError("That attachment is too large (max 8MB).")
            try:
                return await attachment.read(), "attachment"
            except discord.HTTPException:
                raise ValueError("Couldn't download that attachment.")
        if ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)  # type: ignore
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                ref = None
            if ref is not None and ref.attachments:
                attachment = ref.attachments[0]
                if not (attachment.content_type or "").startswith("image/"):
                    raise ValueError("The replied-to attachment isn't an image.")
                if attachment.size > info_helpers.MAX_IMAGE_BYTES:
                    raise ValueError("That attachment is too large (max 8MB).")
                try:
                    return await attachment.read(), "replied attachment"
                except discord.HTTPException:
                    raise ValueError("Couldn't download that attachment.")
        if url:
            return await self._fetch_guarded_bytes(url), "url"
        avatar_url = ctx.author.display_avatar.url
        return await self._fetch_guarded_bytes(avatar_url), "avatar"

    async def _open_validated_image(self, data: bytes) -> Image.Image:
        """Open ``data`` with Pillow and enforce format/size limits."""
        def _open():
            try:
                image = Image.open(io.BytesIO(data))
                image.load()
            except Exception:
                raise ValueError("That isn't a supported image.")
            fmt = (image.format or "").upper()
            if fmt not in info_helpers.ALLOWED_IMAGE_FORMATS:
                raise ValueError(f"Unsupported image format `{fmt or 'unknown'}`.")
            width, height = image.size
            if width * height > info_helpers.MAX_IMAGE_PIXELS:
                raise ValueError("That image is too large to process.")
            return image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image

        try:
            return await asyncio.to_thread(_open)
        except ValueError:
            raise

    # ------------------------------------------------------------------ #
    # Core Discord information (Bleed parity, no duplicates)
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="roles",
        aliases=["serverroles", "roleslist"],
        description="List the roles in this server.",
        example="roles",
    )
    @guild_only()
    @cooldown(1, 5, BucketType.guild)
    async def roles(self, ctx: Context):
        roles = sorted(ctx.guild.roles, key=lambda r: r.position, reverse=True)  # type: ignore
        roles = [r for r in roles if not r.is_default()]
        if not roles:
            return await ctx.warn("This server has no roles.")
        lines = [f"{r.mention} — `{len(r.members)}` member(s)" for r in roles]
        embeds = []
        for index, chunk in enumerate(info_helpers.chunk_lines(lines, 15), 1):
            embed = discord.Embed(
                title=f"Roles in {ctx.guild.name} ({len(roles)})",  # type: ignore
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index}/{(len(lines) + 14) // 15}")
            embeds.append(embed)
        return await ctx.paginate(embeds)

    @hybrid_command(
        name="emotes",
        aliases=["emojis", "emotelist", "serveremotes"],
        description="List the custom emoji in this server.",
        example="emotes",
    )
    @guild_only()
    @cooldown(1, 5, BucketType.guild)
    async def emotes(self, ctx: Context):
        emojis = ctx.guild.emojis  # type: ignore
        if not emojis:
            return await ctx.warn("This server has no custom emoji.")
        lines = [
            f"{e} `{e.name}` (`{e.id}`){' — animated' if e.animated else ''}"
            for e in sorted(emojis, key=lambda e: e.name.lower())
        ]
        embeds = []
        for index, chunk in enumerate(info_helpers.chunk_lines(lines, 20), 1):
            embed = discord.Embed(
                title=f"Emotes in {ctx.guild.name} ({len(emojis)}/{ctx.guild.emoji_limit})",  # type: ignore
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index}/{(len(lines) + 19) // 20}")
            embeds.append(embed)
        return await ctx.paginate(embeds)

    @hybrid_command(
        name="members",
        aliases=["rolemembers", "inrole"],
        description="List the members that have a role.",
        example="members @Moderator",
    )
    @guild_only()
    @cooldown(1, 10, BucketType.guild)
    async def members(self, ctx: Context, *, role: Role):
        members = sorted(role.members, key=lambda m: m.display_name.lower())
        if not members:
            return await ctx.warn(f"Nobody has {role.mention}.")
        capped = members[:500]
        lines = [f"`{i}` {m.mention} (`{m.id}`)" for i, m in enumerate(capped, 1)]
        embeds = []
        pages = info_helpers.chunk_lines(lines, 25)
        for index, chunk in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"Members with {role.name} ({len(members)})",
                description="\n".join(chunk),
                color=role.color if role.color.value else COLORS.neutral,
            )
            note = "Showing the first 500." if len(members) > 500 else f"Page {index}/{len(pages)}"
            embed.set_footer(text=note)
            embeds.append(embed)
        return await ctx.paginate(embeds)

    @hybrid_command(
        name="channelinfo",
        aliases=["ci", "chinfo"],
        description="Show details about a channel.",
        example="channelinfo #general",
    )
    @guild_only()
    async def channelinfo(
        self,
        ctx: Context,
        channel: Optional[
            Union[
                discord.TextChannel,
                discord.VoiceChannel,
                discord.CategoryChannel,
                discord.ForumChannel,
            ]
        ] = None,
    ):
        target = channel or ctx.channel  # type: ignore
        fields = [
            {"name": "ID", "value": f"`{target.id}`", "inline": True},
            {"name": "Type", "value": str(getattr(target, 'type', 'unknown')), "inline": True},
            {
                "name": "Created",
                "value": f"{format_dt(target.created_at, 'D')} ({format_dt(target.created_at, 'R')})",
                "inline": False,
            },
        ]
        category = getattr(target, "category", None)
        fields.append(
            {"name": "Category", "value": category.name if category else "None", "inline": True}
        )
        fields.append(
            {"name": "Position", "value": str(getattr(target, 'position', 'N/A')), "inline": True}
        )
        if isinstance(target, discord.TextChannel):
            fields.append({"name": "NSFW", "value": str(target.nsfw), "inline": True})
            fields.append({"name": "Slowmode", "value": f"{target.slowmode_delay}s", "inline": True})
            if target.topic:
                topic = target.topic if len(target.topic) <= 300 else target.topic[:300] + "…"
                fields.append({"name": "Topic", "value": topic, "inline": False})
        elif isinstance(target, discord.VoiceChannel):
            fields.append({"name": "Bitrate", "value": f"{target.bitrate // 1000}kbps", "inline": True})
            fields.append({"name": "User limit", "value": str(target.user_limit or "None"), "inline": True})
        elif isinstance(target, discord.ForumChannel):
            fields.append({"name": "Threads", "value": str(len(target.threads)), "inline": True})
        name = getattr(target, "name", getattr(target, "mention", "channel"))
        mention = getattr(target, "mention", name)
        return await ctx.embed(title=f"#{name}", description=mention, fields=fields)  # type: ignore

    async def _resolve_guild_asset(self, ctx: Context, guild_id: Optional[int]):
        """Return the guild for asset commands (current or by ID)."""
        if guild_id is None:
            if ctx.guild is None:
                await ctx.warn("Provide a server ID when using this in DMs.")
                return None
            return ctx.guild
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            return guild
        try:
            return await self.bot.fetch_guild(guild_id)
        except discord.NotFound:
            await ctx.warn("No server found with that ID.")
            return None
        except discord.Forbidden:
            await ctx.warn("I can't access that server.")
            return None
        except discord.HTTPException:
            await ctx.warn("Couldn't fetch that server right now.")
            return None

    @hybrid_command(
        name="guildicon",
        aliases=["gi", "servericon"],
        description="Show a server's icon (current server or by ID).",
        example="guildicon",
    )
    async def guildicon(self, ctx: Context, guild_id: Optional[int] = None):
        guild = await self._resolve_guild_asset(ctx, guild_id)
        if guild is None:
            return
        icon = getattr(guild, "icon", None)
        if icon is None:
            return await ctx.warn(f"**{guild.name}** has no icon.")
        return await ctx.embed(
            title=f"{guild.name}'s icon",
            url=icon.url,
            image=icon.url,
            fields=[{"name": "Server ID", "value": f"`{guild.id}`"}],
        )

    @hybrid_command(
        name="guildbanner",
        aliases=["gb", "guildb"],
        description="Show a server's banner (current server or by ID).",
        example="guildbanner",
    )
    async def guildbanner(self, ctx: Context, guild_id: Optional[int] = None):
        guild = await self._resolve_guild_asset(ctx, guild_id)
        if guild is None:
            return
        banner = getattr(guild, "banner", None)
        if banner is None:
            return await ctx.warn(f"**{guild.name}** has no banner.")
        return await ctx.embed(
            title=f"{guild.name}'s banner",
            url=banner.url,
            image=banner.url,
            fields=[{"name": "Server ID", "value": f"`{guild.id}`"}],
        )

    @hybrid_command(
        name="splash",
        aliases=["guildsplash"],
        description="Show a server's invite splash (current server or by ID).",
        example="splash",
    )
    async def splash(self, ctx: Context, guild_id: Optional[int] = None):
        guild = await self._resolve_guild_asset(ctx, guild_id)
        if guild is None:
            return
        splash = getattr(guild, "splash", None)
        if splash is None:
            return await ctx.warn(f"**{guild.name}** has no invite splash.")
        return await ctx.embed(
            title=f"{guild.name}'s splash",
            url=splash.url,
            image=splash.url,
            fields=[{"name": "Server ID", "value": f"`{guild.id}`"}],
        )

    @Cog.listener("on_message")
    async def seen_tracker(self, message: Message):
        """Record last-seen data for the `seen` command (best effort)."""
        try:
            if message.guild is None or message.author.bot:
                return
            await self.bot.pool.execute(
                "INSERT INTO last_seen (guild_id, user_id, last_seen, channel_id) "
                "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE "
                "last_seen = VALUES(last_seen), channel_id = VALUES(channel_id)",
                message.guild.id,
                message.author.id,
                int(time.time()),
                message.channel.id,
            )
        except Exception:
            pass

    @hybrid_command(
        name="seen",
        aliases=["lastseen"],
        description="Show when a member last sent a message here.",
        example="seen @voby",
    )
    @guild_only()
    async def seen(self, ctx: Context, *, member: Union[User, Member] = Author):  # type: ignore
        target = member or ctx.author
        try:
            row = await self.bot.pool.fetchrow(
                "SELECT last_seen, channel_id FROM last_seen WHERE guild_id = %s AND user_id = %s",
                ctx.guild.id,  # type: ignore
                target.id,
            )
        except Exception:
            return await ctx.warn("Couldn't check that right now.")
        if not row:
            return await ctx.warn(
                f"I haven't seen **{target}** since tracking started."
            )
        seen_at = datetime.fromtimestamp(int(row["last_seen"]), tz=dt_timezone.utc)
        channel = ctx.guild.get_channel(row["channel_id"]) if row["channel_id"] else None  # type: ignore
        where = channel.mention if channel is not None else "unknown channel"
        return await ctx.embed(
            description=f"**{target}** was last seen {format_dt(seen_at, 'R')} "
            f"({format_dt(seen_at, 'F')}) in {where}.",
            thumbnail=target.display_avatar.url,
        )

    # ------------------------------------------------------------------ #
    # Stickers
    # ------------------------------------------------------------------ #

    @hybrid_group(
        name="sticker",
        aliases=["stickers"],
        invoke_without_command=True,
        description="Show a server sticker by name.",
        example="sticker wave",
    )
    @guild_only()
    async def sticker(self, ctx: Context, *, name: str):
        found = discord.utils.find(
            lambda s: s.name.lower() == name.strip().lower(), ctx.guild.stickers  # type: ignore
        )
        if found is None:
            close = get_close_matches(
                name.strip().lower(),
                [s.name.lower() for s in ctx.guild.stickers],  # type: ignore
                n=1,
                cutoff=0.6,
            )
            hint = f" Did you mean `{close[0]}`?" if close else ""
            return await ctx.warn(f"No sticker named `{name}` here.{hint}")
        try:
            return await ctx.send(
                file=await found.to_file(filename=f"{found.name}.png")
            )
        except discord.HTTPException:
            return await ctx.warn("Couldn't download that sticker.")

    @sticker.command(
        name="list",
        aliases=["all"],
        description="List the stickers in this server.",
        example="sticker list",
    )
    @guild_only()
    @cooldown(1, 5, BucketType.guild)
    async def sticker_list(self, ctx: Context):
        stickers = sorted(ctx.guild.stickers, key=lambda s: s.name.lower())  # type: ignore
        if not stickers:
            return await ctx.warn("This server has no stickers.")
        lines = [
            f"`{s.name}` — {s.format.name.lower().replace('_', ' ')} (`{s.id}`)"
            for s in stickers
        ]
        embeds = []
        for index, chunk in enumerate(info_helpers.chunk_lines(lines, 20), 1):
            embed = discord.Embed(
                title=f"Stickers in {ctx.guild.name} ({len(stickers)}/{ctx.guild.sticker_limit})",  # type: ignore
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index}/{(len(lines) + 19) // 20}")
            embeds.append(embed)
        return await ctx.paginate(embeds)

    @sticker.command(
        name="add",
        aliases=["create", "steal"],
        description="Add a sticker (attach an image, reply to one, or give an image URL).",
        example="sticker add wave 🙂",
    )
    @has_permissions(manage_expressions=True)
    @guild_only()
    @cooldown(1, 10, BucketType.user)
    async def sticker_add(
        self, ctx: Context, name: str, emoji: str = "🙂", *, extra: str = ""
    ):
        clean_name, error = info_helpers.validate_sticker_name(name)
        if error:
            return await ctx.warn(error)
        if len(ctx.guild.stickers) >= ctx.guild.sticker_limit:  # type: ignore
            return await ctx.warn(
                f"This server is at its sticker limit (`{ctx.guild.sticker_limit}`)."  # type: ignore
            )
        if not ctx.guild.me.guild_permissions.manage_expressions:  # type: ignore
            return await ctx.warn(
                "I need the **Manage Emojis and Stickers** permission to do that."
            )
        url_match = re.search(r"https?://\S+", extra or "")
        url = url_match.group(0).rstrip(").>,]") if url_match else None
        description_text = (extra or "").replace(url, "", 1) if url else (extra or "")
        description, desc_error = info_helpers.validate_sticker_description(
            description_text, clean_name or name
        )
        if desc_error:
            return await ctx.warn(desc_error)
        async with ctx.typing():
            try:
                data, _ = await self._resolve_image_source(ctx, url)
            except ValueError as e:
                return await ctx.warn(str(e))
            if len(data) > info_helpers.MAX_STICKER_BYTES:
                return await ctx.warn(
                    "That image is over 500KB — Discord stickers must be under 500KB."
                )

            def _normalise():
                try:
                    image = Image.open(io.BytesIO(data))
                    image.load()
                except Exception:
                    raise ValueError("That isn't a supported image.")
                image.thumbnail((320, 320))
                canvas = image.convert("RGBA") if image.mode != "RGBA" else image
                out = io.BytesIO()
                canvas.save(out, format="PNG")
                return out.getvalue()

            try:
                png = await asyncio.to_thread(_normalise)
            except ValueError as e:
                return await ctx.warn(str(e))
            if len(png) > info_helpers.MAX_STICKER_BYTES:
                return await ctx.warn(
                    "That image is still over 500KB after resizing — try a smaller one."
                )
        try:
            sticker = await ctx.guild.create_sticker(  # type: ignore
                name=clean_name,
                description=description,
                emoji=emoji,
                file=discord.File(io.BytesIO(png), filename=f"{clean_name}.png"),
                reason=f"Added by {ctx.author}",
            )
            return await ctx.approve(f"Added sticker `{sticker.name}` {sticker}")
        except discord.Forbidden:
            return await ctx.warn("I don't have permission to add stickers here.")
        except discord.HTTPException as e:
            return await ctx.warn(f"Couldn't add that sticker: `{e.text or e}`")

    @sticker.command(
        name="remove",
        aliases=["delete"],
        description="Remove a server sticker by name.",
        example="sticker remove wave",
    )
    @has_permissions(manage_expressions=True)
    @guild_only()
    async def sticker_remove(self, ctx: Context, *, name: str):
        found = discord.utils.find(
            lambda s: s.name.lower() == name.strip().lower(), ctx.guild.stickers  # type: ignore
        )
        if found is None:
            return await ctx.warn(f"No sticker named `{name}` here.")
        if not ctx.guild.me.guild_permissions.manage_expressions:  # type: ignore
            return await ctx.warn(
                "I need the **Manage Emojis and Stickers** permission to do that."
            )
        try:
            await found.delete(reason=f"Removed by {ctx.author}")
            return await ctx.approve(f"Removed sticker `{found.name}`")
        except discord.Forbidden:
            return await ctx.warn("I don't have permission to remove stickers here.")
        except discord.HTTPException as e:
            return await ctx.warn(f"Couldn't remove that sticker: `{e.text or e}`")

    @sticker.command(
        name="rename",
        aliases=["edit"],
        description="Rename a server sticker.",
        example="sticker rename wave hello",
    )
    @has_permissions(manage_expressions=True)
    @guild_only()
    async def sticker_rename(self, ctx: Context, name: str, *, new_name: str):
        clean, error = info_helpers.validate_sticker_name(new_name)
        if error:
            return await ctx.warn(error)
        found = discord.utils.find(
            lambda s: s.name.lower() == name.strip().lower(), ctx.guild.stickers  # type: ignore
        )
        if found is None:
            return await ctx.warn(f"No sticker named `{name}` here.")
        if not ctx.guild.me.guild_permissions.manage_expressions:  # type: ignore
            return await ctx.warn(
                "I need the **Manage Emojis and Stickers** permission to do that."
            )
        try:
            await found.edit(name=clean, reason=f"Renamed by {ctx.author}")
            return await ctx.approve(f"Renamed sticker to `{clean}`")
        except discord.Forbidden:
            return await ctx.warn("I don't have permission to edit stickers here.")
        except discord.HTTPException as e:
            return await ctx.warn(f"Couldn't rename that sticker: `{e.text or e}`")

    # ------------------------------------------------------------------ #
    # Image utilities (attachment / reply / URL / avatar)
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="rotate",
        aliases=["spin"],
        description="Rotate an image (attach it, reply to it, or pass an image URL).",
        example="rotate 90",
    )
    @cooldown(1, 10, BucketType.user)
    async def rotate(self, ctx: Context, degrees: int = 90, *, url: str = None):  # type: ignore
        parsed, error = info_helpers.parse_degrees(degrees)
        if error or parsed is None:
            return await ctx.warn(error or "Invalid degrees.")
        async with ctx.typing():
            try:
                data, _ = await self._resolve_image_source(ctx, url)
                image = await self._open_validated_image(data)
            except ValueError as e:
                return await ctx.warn(str(e))

            def _work():
                frame = image.convert("RGBA")
                return frame.rotate(parsed, expand=True)

            try:
                out = await asyncio.to_thread(_work)
            except Exception:
                return await ctx.warn("Couldn't process that image.")
            buffer = io.BytesIO()
            out.save(buffer, format="PNG")
            buffer.seek(0)
        return await ctx.send(
            file=discord.File(buffer, filename=f"rotated_{parsed}.png")
        )

    @hybrid_command(
        name="compress",
        aliases=["squeeze"],
        description="Compress an image to a JPEG quality (attach it, reply to it, or pass a URL).",
        example="compress 60",
    )
    @cooldown(1, 10, BucketType.user)
    async def compress(self, ctx: Context, quality: int = 70, *, url: str = None):  # type: ignore
        parsed, error = info_helpers.parse_quality(quality)
        if error or parsed is None:
            return await ctx.warn(error or "Invalid quality.")
        async with ctx.typing():
            try:
                data, _ = await self._resolve_image_source(ctx, url)
                image = await self._open_validated_image(data)
            except ValueError as e:
                return await ctx.warn(str(e))

            def _work():
                frame = image.convert("RGB")
                out = io.BytesIO()
                frame.save(out, format="JPEG", quality=parsed, optimize=True)
                return out.getvalue()

            try:
                blob = await asyncio.to_thread(_work)
            except Exception:
                return await ctx.warn("Couldn't process that image.")
        before, after = len(data), len(blob)
        embed = discord.Embed(
            title=f"Compressed to quality {parsed}",
            description=f"**Before:** `{before / 1024:.1f}KB` → **After:** `{after / 1024:.1f}KB`",
            color=COLORS.neutral,
        )
        embed.set_image(url="attachment://compressed.jpg")
        return await ctx.send(
            embed=embed, file=discord.File(io.BytesIO(blob), filename="compressed.jpg")
        )

    @hybrid_command(
        name="invert",
        aliases=["negative"],
        description="Invert an image's colors (attach it, reply to it, or pass a URL).",
        example="invert",
    )
    @cooldown(1, 10, BucketType.user)
    async def invert(self, ctx: Context, *, url: str = None):  # type: ignore
        async with ctx.typing():
            try:
                data, _ = await self._resolve_image_source(ctx, url)
                image = await self._open_validated_image(data)
            except ValueError as e:
                return await ctx.warn(str(e))

            def _work():
                return ImageOps.invert(image.convert("RGB"))

            try:
                out = await asyncio.to_thread(_work)
            except Exception:
                return await ctx.warn("Couldn't process that image.")
            buffer = io.BytesIO()
            out.save(buffer, format="PNG")
            buffer.seek(0)
        return await ctx.send(file=discord.File(buffer, filename="inverted.png"))

    # ------------------------------------------------------------------ #
    # Time zones (durable per-user preference, IANA validated)
    # ------------------------------------------------------------------ #

    def _tz_now(self, name: str) -> datetime:
        return datetime.now(ZoneInfo(name))

    @hybrid_group(
        name="timezone",
        aliases=["tz"],
        invoke_without_command=True,
        description="Show the current time in your (or a member's) time zone.",
        example="timezone @voby",
    )
    async def timezone(self, ctx: Context, *, member: Union[User, Member] = Author):  # type: ignore
        target = member or ctx.author
        try:
            row = await self.bot.pool.fetchrow(
                "SELECT tz FROM timezones WHERE user_id = %s", target.id
            )
        except Exception:
            return await ctx.warn("Couldn't check that right now.")
        if not row:
            who = "You haven't" if target.id == ctx.author.id else f"**{target}** hasn't"
            return await ctx.warn(
                f"{who} set a time zone yet. Use `{ctx.clean_prefix}timezone set <zone>`."
            )
        now = self._tz_now(row["tz"])
        return await ctx.embed(
            description=f"🕒 It's **{now:%I:%M %p}** for **{target}** (`{row['tz']}`, {now:%Z %z}).",
        )

    @timezone.command(
        name="set",
        description="Set your time zone (IANA name, e.g. Europe/Berlin).",
        example="timezone set Europe/Berlin",
    )
    @cooldown(1, 5, BucketType.user)
    async def timezone_set(self, ctx: Context, *, zone: str):
        clean, error = info_helpers.validate_timezone(zone)
        if error or not clean:
            return await ctx.warn(error or "Invalid time zone.")
        try:
            await self.bot.pool.execute(
                "INSERT INTO timezones (user_id, tz) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE tz = VALUES(tz)",
                ctx.author.id,
                clean,
            )
        except Exception:
            return await ctx.warn("Couldn't save that right now — try again.")
        now = self._tz_now(clean)
        return await ctx.approve(
            f"Your time zone is now `{clean}` (there it's **{now:%I:%M %p}**)."
        )

    @timezone.command(
        name="reset",
        aliases=["remove", "delete", "clear"],
        description="Remove your saved time zone.",
        example="timezone reset",
    )
    async def timezone_reset(self, ctx: Context):
        try:
            row = await self.bot.pool.fetchrow(
                "SELECT tz FROM timezones WHERE user_id = %s", ctx.author.id
            )
            if not row:
                return await ctx.warn("You don't have a time zone set.")
            await self.bot.pool.execute(
                "DELETE FROM timezones WHERE user_id = %s", ctx.author.id
            )
        except Exception:
            return await ctx.warn("Couldn't do that right now — try again.")
        return await ctx.approve("Your time zone was removed.")

    @timezone.command(
        name="list",
        aliases=["all"],
        description="List server members with a saved time zone and their local time.",
        example="timezone list",
    )
    @guild_only()
    @cooldown(1, 10, BucketType.guild)
    async def timezone_list(self, ctx: Context):
        try:
            rows = await self.bot.pool.fetch("SELECT user_id, tz FROM timezones")
        except Exception:
            return await ctx.warn("Couldn't load that right now — try again.")
        lines = []
        for row in rows:
            member = ctx.guild.get_member(row["user_id"])  # type: ignore
            if member is None:
                continue
            try:
                now = self._tz_now(row["tz"])
            except Exception:
                continue
            lines.append(f"**{member.display_name}** — `{row['tz']}` (**{now:%I:%M %p}**)")
        if not lines:
            return await ctx.warn("Nobody here has set a time zone yet.")
        lines.sort(key=str.lower)
        embeds = []
        pages = info_helpers.chunk_lines(lines, 15)
        for index, chunk in enumerate(pages, 1):
            embed = discord.Embed(
                title=f"Time zones in {ctx.guild.name} ({len(lines)})",  # type: ignore
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_footer(text=f"Page {index}/{len(pages)}")
            embeds.append(embed)
        return await ctx.paginate(embeds)

    # ------------------------------------------------------------------ #
    # Reference lookups (stable, keyless providers only)
    # ------------------------------------------------------------------ #

    @hybrid_command(
        name="define",
        aliases=["dictionary", "meaning"],
        description="Look up a word in the dictionary.",
        example="define serendipity",
    )
    @cooldown(1, 5, BucketType.user)
    async def define(self, ctx: Context, *, word: str):
        word = word.strip().strip(".")
        if not word or len(word) > 60 or not re.fullmatch(r"[A-Za-z][A-Za-z' -]*", word):
            return await ctx.warn("Provide a word or short phrase to define.")
        timeout = aiohttp.ClientTimeout(total=info_helpers.FETCH_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}",
                    headers={"User-Agent": "Hollow-Phi/1.0"},
                ) as response:
                    if response.status == 404:
                        return await ctx.warn(f"No definition found for `{word}`.")
                    if response.status == 429:
                        return await ctx.warn("The dictionary is rate-limiting me — try again in a bit.")
                    if response.status != 200:
                        return await ctx.warn("The dictionary is unavailable right now.")
                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError:
                        return await ctx.warn("The dictionary returned a bad response.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await ctx.warn("The dictionary is unreachable right now.")
        if not isinstance(data, list) or not data:
            return await ctx.warn(f"No definition found for `{word}`.")
        entry = data[0]
        phonetic = entry.get("phonetic") or next(
            (p.get("text") for p in entry.get("phonetics", []) if p.get("text")), ""
        )
        embeds = []
        for meaning in entry.get("meanings", [])[:3]:
            definitions = meaning.get("definitions", [])[:3]
            if not definitions:
                continue
            body = []
            for d in definitions:
                line = f"• {d.get('definition', '—')}"
                if d.get("example"):
                    line += f"\n  *\"{d['example']}\"*"
                body.append(line)
            embed = discord.Embed(
                title=f"{entry.get('word', word)} — {meaning.get('partOfSpeech', 'unknown')}",
                description="\n".join(body)[:4000],
                color=COLORS.neutral,
            )
            if phonetic and not embeds:
                embed.set_footer(text=phonetic)
            embeds.append(embed)
        if not embeds:
            return await ctx.warn(f"No definition found for `{word}`.")
        return await ctx.paginate(embeds)

    @hybrid_command(
        name="github",
        aliases=["gh"],
        description="Look up a GitHub user or repository.",
        example="github discordjs/discord.js",
    )
    @cooldown(1, 5, BucketType.user)
    async def github(self, ctx: Context, *, target: str):
        parsed, error = info_helpers.parse_github_target(target)
        if error or not parsed:
            return await ctx.warn(error or "Invalid GitHub target.")
        kind = parsed[0]
        url = (
            f"https://api.github.com/users/{parsed[1]}"
            if kind == "user"
            else f"https://api.github.com/repos/{parsed[1]}/{parsed[2]}"
        )
        timeout = aiohttp.ClientTimeout(total=info_helpers.FETCH_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={
                        "User-Agent": "Hollow-Phi/1.0",
                        "Accept": "application/vnd.github+json",
                    },
                ) as response:
                    if response.status == 404:
                        return await ctx.warn("Nothing found on GitHub for that.")
                    if response.status == 403:
                        return await ctx.warn("GitHub is rate-limiting me — try again in a bit.")
                    if response.status != 200:
                        return await ctx.warn("GitHub is unavailable right now.")
                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError:
                        return await ctx.warn("GitHub returned a bad response.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await ctx.warn("GitHub is unreachable right now.")
        if kind == "user":
            return await ctx.embed(
                title=f"{data.get('login', '?')} on GitHub",
                url=data.get("html_url"),
                description=(data.get("bio") or "No bio.")[:500],
                thumbnail=data.get("avatar_url"),
                fields=[
                    {"name": "Followers", "value": f"`{data.get('followers', 0):,}`", "inline": True},
                    {"name": "Following", "value": f"`{data.get('following', 0):,}`", "inline": True},
                    {"name": "Public repos", "value": f"`{data.get('public_repos', 0):,}`", "inline": True},
                ],
                footer={"text": f"Created {data.get('created_at', '?')[:10]}"},
            )
        return await ctx.embed(
            title=data.get("full_name", "?"),
            url=data.get("html_url"),
            description=(data.get("description") or "No description.")[:500],
            fields=[
                {"name": "Stars", "value": f"`{data.get('stargazers_count', 0):,}`", "inline": True},
                {"name": "Forks", "value": f"`{data.get('forks_count', 0):,}`", "inline": True},
                {"name": "Language", "value": str(data.get("language") or "N/A"), "inline": True},
                {"name": "Open issues", "value": f"`{data.get('open_issues_count', 0):,}`", "inline": True},
            ],
            footer={"text": f"Updated {data.get('updated_at', '?')[:10]}"},
        )

    @hybrid_command(
        name="weather",
        aliases=["forecast", "wt"],
        description="Show the current weather for a place (no key needed).",
        example="weather Berlin",
    )
    @cooldown(1, 10, BucketType.user)
    async def weather(self, ctx: Context, *, location: str):
        location = location.strip()
        if not location or len(location) > 100:
            return await ctx.warn("Provide a place to check the weather for.")
        timeout = aiohttp.ClientTimeout(total=info_helpers.FETCH_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    params={"name": location, "count": 1, "format": "json"},
                    headers={"User-Agent": "Hollow-Phi/1.0"},
                ) as geo_response:
                    if geo_response.status != 200:
                        return await ctx.warn("The weather service is unavailable right now.")
                    geo = await geo_response.json()
                results = (geo or {}).get("results") or []
                if not results:
                    return await ctx.warn(f"No place found for `{location}`.")
                place = results[0]
                async with session.get(
                    "https://api.open-meteo.com/v1/forecast",
                    params={
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
                    },
                    headers={"User-Agent": "Hollow-Phi/1.0"},
                ) as wx_response:
                    if wx_response.status != 200:
                        return await ctx.warn("The weather service is unavailable right now.")
                    current = (await wx_response.json()).get("current") or {}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await ctx.warn("The weather service is unreachable right now.")
        temp = current.get("temperature_2m")
        if temp is None:
            return await ctx.warn("No weather data for that place.")
        feels = current.get("apparent_temperature", "?")
        desc = info_helpers.wmo_description(current.get("weather_code"))
        if isinstance(temp, (int, float)):
            headline = f"**{desc}** — **{temp}°C** ({round(temp * 9 / 5 + 32, 1)}°F)"
        else:
            headline = f"**{desc}** — **{temp}°C**"
        label = place.get("name", location)
        region = ", ".join(p for p in (place.get("admin1"), place.get("country")) if p)
        return await ctx.embed(
            title=f"Weather in {label}",
            description=headline,
            fields=[
                {"name": "Feels like", "value": f"`{feels}°C`", "inline": True},
                {"name": "Humidity", "value": f"`{current.get('relative_humidity_2m', '?')}%`", "inline": True},
                {"name": "Wind", "value": f"`{current.get('wind_speed_10m', '?')} km/h`", "inline": True},
            ],
            footer={"text": f"{region} • Open-Meteo" if region else "Open-Meteo"},
        )

    @hybrid_command(
        name="minecraft",
        aliases=["mcuser"],
        description="Look up a Minecraft account by username or UUID.",
        example="minecraft Notch",
    )
    @cooldown(1, 5, BucketType.user)
    async def minecraft(self, ctx: Context, *, identifier: str):
        parsed, error = info_helpers.parse_minecraft_identifier(identifier)
        if error or not parsed:
            return await ctx.warn(error or "Invalid Minecraft account.")
        kind, value = parsed
        timeout = aiohttp.ClientTimeout(total=info_helpers.FETCH_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if kind == "username":
                    profile_url = f"https://api.mojang.com/users/profiles/minecraft/{value}"
                else:
                    profile_url = f"https://sessionserver.mojang.com/session/minecraft/profile/{value.replace('-', '')}"
                async with session.get(
                    profile_url, headers={"User-Agent": "Hollow-Phi/1.0"}
                ) as response:
                    if response.status in (404, 204):
                        return await ctx.warn(f"No Minecraft account found for `{identifier}`.")
                    if response.status == 429:
                        return await ctx.warn("Mojang is rate-limiting me — try again in a bit.")
                    if response.status != 200:
                        return await ctx.warn("Mojang is unavailable right now.")
                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError:
                        return await ctx.warn("Mojang returned a bad response.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return await ctx.warn("Mojang is unreachable right now.")
        uuid = (data.get("id") or "").lower()
        if not uuid:
            return await ctx.warn(f"No Minecraft account found for `{identifier}`.")
        dashed = f"{uuid[0:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:32]}"
        name = data.get("name", value)
        return await ctx.embed(
            title=name,
            thumbnail=f"https://crafthead.net/avatar/{dashed}/64",
            image=f"https://crafthead.net/armor/body/{dashed}",
            fields=[
                {"name": "UUID", "value": f"`{dashed}`", "inline": False},
                {"name": "Skin", "value": f"[download](https://crafthead.net/skin/{dashed})", "inline": True},
            ],
            footer={"text": "Renders via crafthead.net"},
        )

    @hybrid_command(
        name="status",
        aliases=["statpage"],
        description="Check the bot's operational status and status page.",
        example="status",
    )
    async def status(self, ctx: Context):
        return await ctx.embed(
            title="Hollow status",
            description=(
                f"**Ping:** `{self.bot.ping}ms`\n"
                f"**Uptime:** {self.bot.humanize_time(self.bot._uptime)}\n"
                f"**Servers:** `{len(self.bot.guilds):,}` • **Users:** `{len(self.bot.users):,}`\n"
                "All systems operational."
            ),
            thumbnail=self.bot.user.display_avatar.url if self.bot.user else "https://none.none",  # type: ignore
            buttons=[{"label": "Status page", "emoji": "🔗", "url": SITE_URL}],
        )
