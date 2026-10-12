from typing import Union
import requests
import io
import time
import aiohttp

from base.managers.types import CogMeta
from base.context import Context
from base.managers.paginator import *
from base.config import *
from random import random, choice
from humanize import naturaltime
import humanize
import os
from datetime import datetime
import time
from jishaku.codeblocks import codeblock_converter

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
    Guild,
    HTTPException,
    Forbidden,
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
    has_permissions,
    is_owner,
)
from base.managers.commands import hybrid_command, hybrid_group
from discord.ui import View
import discord
from discord.utils import format_dt, oauth_url
from collections import defaultdict
import inspect

from loguru import logger as log

COMMANDS_API_URL = "https://hollow-phi.vercel.app/api/commands/commands"
BOT_API_KEY = "xryptonnostalgia"



class Owner(CogMeta):
    @Cog.listener("on_guild_join")
    async def on_guild_join(self, guild: Guild) -> None:
        #       is_whitelisted = await self.bot.pool.fetchval("SELECT 1 FROM guild_whitelist WHERE guild_id = %s", guild.id)  # type: ignore

        #      if is_whitelisted is None:
        #          await guild.leave()

        if await self.bot.is_guild_blacklisted(guild.id):
            await self._notify_and_leave(guild)
            return

        embed = Embed(description=f"Joined {guild.name} (`{guild.id}`)")
        embed.add_field(
            name="Owner",
            value=f"{guild.owner.name}",  # type: ignore
            inline=True,
        )
        embed.add_field(
            name="Member Count",
            value=f"Total: **{guild.member_count}**",
            inline=True,
        )
        embed.set_footer(
            text=f"{self.bot.user.name} joined a server | We are at {len(self.bot.guilds)} guilds"
        )  # type: ignore
        embed.set_thumbnail(url=guild.icon.url if guild.icon else "https://none.none")

        channel = self.bot.get_channel(
            1333520082151669841
        )  # replace with your channel id - psutil

        if channel:
            return await channel.send(content=f"{guild.id}", embed=embed)  # type: ignore

    @Cog.listener("on_guild_remove")
    async def on_guild_remove(self, guild: Guild) -> None:
        embed = Embed(description=f"Left {guild.name} (`{guild.id}`)")
        embed.add_field(
            name="Owner",
            value=f"{guild.owner.name}",  # type: ignore
            inline=True,
        )
        embed.add_field(
            name="Member Count",
            value=f"Total: **{guild.member_count}**",
            inline=True,
        )
        embed.set_footer(
            text=f"{self.bot.user.name} left a server | We are at {len(self.bot.guilds)} guilds"
        )  # type: ignore
        embed.set_thumbnail(url=guild.icon.url if guild.icon else "https://none.none")
        channel = self.bot.get_channel(
            1333520082151669841
        )  # replace with your channel id - psutil

        if channel:
            return await channel.send(content=f"{guild.id}", embed=embed)  # type: ignore

    @hybrid_command(name="whitelist", aliases=["wl"], example="whitelist 123456789012345678")
    @is_owner()
    async def whitelist(self, ctx: Context, *, id: int) -> None:
        if await self.bot.pool.fetch(
            "SELECT guild_id FROM guild_whitelist WHERE guild_id = %s", id
        ):
            await self.bot.pool.execute(
                "DELETE FROM guild_whitelist WHERE guild_id = %s", id
            )
            guild = self.bot.get_guild(id)
            if guild:
                await guild.leave()
            return await ctx.approve(f"Unwhitelisted the guild: `{id}`")  # type: ignore

        await self.bot.pool.execute(
            "INSERT INTO guild_whitelist (guild_id) VALUES (%s)", id
        )
        return await ctx.approve(f"Whitelisted the guild: `{id}`")  # type: ignore

    @hybrid_command(name="portal", example="portal 123456789012345678")
    @is_owner()
    async def portal(self, ctx: Context, *, id: int) -> None:
        guild = self.bot.get_guild(id)
        await ctx.author.send(await self.generate_invite(guild))
        await ctx.message.delete()

    async def generate_invite(self, guild):
        invite = await guild.text_channels[0].create_invite()
        return invite.url

    @hybrid_command(name="guilds")
    @is_owner()
    async def guilds(self, ctx: Context):
        entries = [
            f"`{i}`  **{guild.name}** (`{guild.id}`) - {guild.member_count:,} members"
            for i, guild in enumerate(
                sorted(self.bot.guilds, key=lambda g: g.member_count, reverse=True),
                start=1,  # type: ignore
            )
        ]

        total_pages = (len(entries) + 9) // 10
        embeds = []
        embed = discord.Embed(
            color=COLORS.neutral,
            title=f"List of Guilds ({len(entries)})",
            description="",
        )

        count = 0

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
                    title=f"List of Guilds ({len(entries)})",
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

    @hybrid_command(name="blacklist", example="blacklist @voby")
    @is_owner()
    async def blacklist(self, ctx: Context, *, user: Union[User, Member]):
        res = await self.bot.pool.fetch(
            "SELECT user_id FROM blacklist WHERE user_id = %s", user.id
        )
        if res:
            await self.bot.pool.execute(
                "DELETE FROM blacklist WHERE user_id = %s", user.id
            )
            return await ctx.approve(f"**Unblacklisted** {user.name}")

        await self.bot.pool.execute(
            "INSERT IGNORE INTO blacklist (user_id) VALUES (%s)",
            user.id,
        )

        return await ctx.approve(f"**Blacklisted** {user.name}")

    @hybrid_command(name="restart", aliases=["reboot"])
    @is_owner()
    async def restart(self, ctx: Context):
        await ctx.message.add_reaction("✅")
        os.system("pm2 restart hollow")

    @hybrid_command(name="push", description="Push to the github repo", example="push my commit message")
    @is_owner()
    async def push(self, ctx: Context, *, msg: str):
        os.system(f"git add . && git commit -m '{msg}' && git push")
        await ctx.message.add_reaction("✅")

    @hybrid_command()
    @is_owner()
    async def pull(self: "Owner", ctx: Context):
        """
        Pull the latest updates from github
        """
        await ctx.invoke(
            self.bot.get_command("jishaku shell"),
            argument=codeblock_converter("git pull"),
        )

    @hybrid_command(aliases=["py"], example="eval 1 + 1")
    @is_owner()
    async def eval(self: "Owner", ctx: Context, *, argument: codeblock_converter):
        """
        Run some python code
        """
        return await ctx.invoke(self.bot.get_command("jsk py"), argument=argument)

    @hybrid_command(name="sql", description="Execute some SQL.", example="sql SELECT * FROM prefix")
    @is_owner()
    async def sql(self, ctx: Context, *, argument: str):
        return await ctx.invoke(self.bot.hybrid_command("jsk sql execute"), argument=argument)

    async def sync_bot_data(self) -> dict:
        synced_commands = []
        for cog_name, cog in self.bot.cogs.items():
            if cog_name == "HelpCog" or cog_name.startswith("Jishaku"):
                continue

            # walk_commands() recurses into hybrid_group subcommands,
            # unlike get_commands() which only yields top-level ones.
            for cmd in cog.walk_commands():
                if cmd.hidden or (hasattr(cmd, "builtin") and cmd.builtin):
                    continue

                params = getattr(cmd, "params", {})
                args = []
                for p in params.values():
                    if p.name in ("self", "ctx", "context", "bot"):
                        continue
                    args.append(
                        {
                            "name": p.name,
                            "description": getattr(p, "description", "")
                            or getattr(p, "documentation", "")
                            or "",
                            "required": p.default is inspect.Parameter.empty,
                        }
                    )

                try:
                    permissions = cmd.permissions
                    if isinstance(permissions, list):
                        permissions = [str(p) for p in permissions]
                    else:
                        permissions = []
                except Exception:
                    permissions = []

                description = (
                    cmd.help
                    or getattr(cmd, "description", "")
                    or "No description provided... read the name closely"
                )

                synced_commands.append(
                    {
                        "name": cmd.qualified_name,
                        "usage_count": 0,
                        "category": (cmd.cog_name or cog_name).lower(),
                        "description": description,
                        "arguments": args,
                        "permissions": permissions,
                    }
                )

        try:
            rows = await self.bot.pool.fetch(
                "SELECT command_name, usage_count FROM command_usage"
            )
        except Exception:
            rows = []
        usage_map = {row["command_name"]: row["usage_count"] for row in rows}
        for cmd in synced_commands:
            cmd["usage_count"] = usage_map.get(cmd["name"], 0)

        guilds_payload = [
            {
                "name": g.name,
                "members": g.member_count,
                "icon": g.icon.url if g.icon else "",
                "verified": True,
            }
            for g in self.bot.guilds
        ]

        total_users = sum(g.member_count or 0 for g in self.bot.guilds)
        info_payload = {"guilds": len(self.bot.guilds), "users": total_users}

        base_url = COMMANDS_API_URL.replace("/api/commands/commands", "").rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {BOT_API_KEY}",
        }

        results = {"commands": False, "guilds": False, "info": False}
        try:
            async with aiohttp.ClientSession() as session:
                if synced_commands:
                    try:
                        async with session.post(
                            f"{base_url}/api/commands/commands",
                            json={"commands": synced_commands},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                log.error(
                                    f"Failed to sync commands: {resp.status} {text}"
                                )
                            else:
                                results["commands"] = True
                    except Exception:
                        pass

                if guilds_payload:
                    try:
                        async with session.post(
                            f"{base_url}/api/guilds/guilds",
                            json={"guilds": guilds_payload},
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status >= 400:
                                text = await resp.text()
                                log.error(
                                    f"Failed to sync guilds: {resp.status} {text}"
                                )
                            else:
                                results["guilds"] = True
                    except Exception:
                        pass

                try:
                    async with session.post(
                        f"{base_url}/api/info/info",
                        json=info_payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status >= 400:
                            text = await resp.text()
                            log.error(f"Failed to sync info: {resp.status} {text}")
                        else:
                            results["info"] = True
                except Exception:
                    pass
        except Exception as e:
            log.error(f"Sync failed: {e}")

        return results


    @hybrid_command(name="websitesync", description="Sync bot commands, guilds, and info to the website")
    @is_owner()
    async def websitesync(self, ctx: Context):
        results = await self.sync_bot_data()
        parts = [
            ("Commands" if results["commands"] else "[FAILED] Commands"),
            ("Guilds" if results["guilds"] else "[FAILED] Guilds"),
            ("Info" if results["info"] else "[FAILED] Info"),
        ]
        return await ctx.approve("Synced: " + ", ".join(parts))

    @hybrid_command(name="guild_blacklist", example="guild_blacklist 123456789012345678")
    @is_owner()
    async def guild_blacklist_cmd(self, ctx: Context, *, guild_id: int) -> None:
        """Blacklist a guild from using the bot."""

        was_added = await self.bot.guild_blacklist(guild_id)

        if not was_added:
            return await ctx.warn(f"That guild is already **blacklisted**.")

        guild = self.bot.get_guild(guild_id)
        if guild:
            await self._notify_and_leave(guild)

        return await ctx.approve(f"Blacklisted the guild: `{guild_id}`")

    @hybrid_command(name="guild_unblacklist", example="guild_unblacklist 123456789012345678")
    @is_owner()
    async def guild_unblacklist_cmd(self, ctx: Context, *, guild_id: int) -> None:
        """Remove a guild from the blacklist."""

        was_removed = await self.bot.guild_unblacklist(guild_id)

        if not was_removed:
            return await ctx.warn(f"That guild is not **blacklisted**.")

        return await ctx.approve(f"Unblacklisted the guild: `{guild_id}`")

    async def _notify_and_leave(self, guild: Guild) -> None:
        """DM the guild owner that the bot is leaving, then leave the guild."""

        owner = guild.owner
        if owner:
            try:
                await owner.send(
                    embed=Embed(
                        color=COLORS.deny,
                        description=f"Your server **{guild.name}** has been blacklisted from using **{self.bot.user.name}**.",
                    )
                )
            except (HTTPException, Forbidden):
                pass

        try:
            await guild.leave()
        except HTTPException:
            pass
    @hybrid_group(name="debug", invoke_without_command=True)
    @is_owner()
    async def debug(self, ctx: Context):
        """Debug utilities."""
        return await ctx.send_help(ctx.command)

    @debug.command(name="slashtree", description="Print the registered slash commands tree.")
    @is_owner()
    async def debug_slashtree(self, ctx: Context):
        """Show top-level commands/groups, children, and total slot count vs 100."""
        commands = self.bot.tree.get_commands()
        total_slots = len(commands)

        lines = [f"**Slash Command Tree** ({total_slots}/100 slots used):\n"]
        for cmd in sorted(commands, key=lambda c: c.name):
            if hasattr(cmd, "commands") and cmd.commands:
                sub_names = [f"`{sub.name}`" for sub in cmd.commands]
                lines.append(f"📁 **/{cmd.name}** ({len(cmd.commands)} subcommands): {', '.join(sub_names)}")
            else:
                lines.append(f"⚡ **/{cmd.name}**")

        text = "\n".join(lines)
        if len(text) > 2000:
            # Paginate or send in parts
            for i in range(0, len(text), 1900):
                await ctx.send(text[i:i+1900])
        else:
            await ctx.send(text)
