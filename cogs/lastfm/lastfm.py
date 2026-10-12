from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import math
import random
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.parse

import aiohttp
import discord
from discord import Embed, Member, User
from discord.ext.commands import BucketType, cooldown
from base.managers.commands import hybrid_command, hybrid_group
from discord.utils import format_dt
from loguru import logger

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.paginator import Paginator
from base.managers.types import CogMeta

# Last.fm Credentials
API_KEY = "a39127523d133fc4e0cee6fbac3d7510"
API_SECRET = "f9f1083d1d1a2cf3650170a523542a94"
APPLICATION_NAME = "hollow."
REGISTERED_TO = "Voby7"
BASE_URL = "https://ws.audioscrobbler.com/2.0/"


class LastFM(CogMeta):
    """Cog for Last.fm integration featuring nowplaying, scrobbles, top artists, profile info, and recommendations."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": f"{APPLICATION_NAME}/1.0 ({REGISTERED_TO})"}
        )

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def ensure_table(self) -> None:
        await self.bot.pool.execute(
            """
            CREATE TABLE IF NOT EXISTS lastfm (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR(64) NOT NULL
            )
            """
        )

    async def get_username(self, user_id: int) -> Optional[str]:
        return await self.bot.pool.fetchval(
            "SELECT username FROM lastfm WHERE user_id = %s", user_id
        )

    async def set_username(self, user_id: int, username: str) -> None:
        await self.bot.pool.execute(
            """
            INSERT INTO lastfm (user_id, username)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE username = VALUES(username)
            """,
            user_id,
            username,
        )

    async def remove_username(self, user_id: int) -> None:
        await self.bot.pool.execute(
            "DELETE FROM lastfm WHERE user_id = %s", user_id
        )

    async def request(self, method: str, **params: Any) -> Dict[str, Any]:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": f"{APPLICATION_NAME}/1.0 ({REGISTERED_TO})"}
            )

        req_params = {
            "method": method,
            "api_key": API_KEY,
            "format": "json",
            **params,
        }

        try:
            async with self.session.get(BASE_URL, params=req_params) as resp:
                data = await resp.json(content_type=None)
                if "error" in data:
                    error_code = data.get("error")
                    message = data.get("message", "Unknown Last.fm API error.")
                    if error_code == 6:
                        raise ValueError("User not found on Last.fm.")
                    raise ValueError(f"Last.fm API error: {message}")
                return data
        except aiohttp.ClientError as e:
            logger.error(f"Last.fm request network error: {e}")
            raise ValueError("Failed to connect to Last.fm. Please try again later.")

    @hybrid_group(
        name="lastfm",
        aliases=["fm"],
        invoke_without_command=True,
        description="Main command group for Last.fm features.",
        example="lastfm",
    )
    async def lastfm(self, ctx: Context) -> None:
        """Alias / default invoker for nowplaying."""
        if ctx.invoked_subcommand is None:
            await self.nowplaying(ctx)

    @lastfm.command(
        name="set",
        description="Set your Last.fm username.",
        example="lastfm set voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def set_profile(self, ctx: Context, *, username: str) -> None:
        """Set user profile without web auth."""
        clean_user = username.strip()
        if not clean_user:
            return await ctx.warn("Please provide a valid **Last.fm username**.")

        # Verify username exists on Last.fm
        try:
            user_info = await self.request("user.getInfo", user=clean_user)
            real_username = user_info["user"]["name"]
        except ValueError as e:
            return await ctx.warn(str(e))

        await self.set_username(ctx.author.id, real_username)
        await ctx.approve(f"Set your **Last.fm username** to **[{real_username}](https://www.last.fm/user/{real_username})**.")

    @lastfm.command(
        name="reset",
        description="Reset your Last.fm configuration.",
        example="lastfm reset",
    )
    @cooldown(1, 3, BucketType.user)
    async def reset(self, ctx: Context) -> None:
        """Reset user's Last.fm info from the database."""
        existing = await self.get_username(ctx.author.id)
        if not existing:
            return await ctx.warn("You do not have a **Last.fm username** set.")

        await self.remove_username(ctx.author.id)
        await ctx.approve("Successfully reset your **Last.fm info**.")

    @lastfm.command(
        name="nowplaying",
        aliases=["np", "playing", "now"],
        description="Show the track currently playing on Last.fm.",
        example="lastfm nowplaying",
    )
    @cooldown(1, 3, BucketType.user)
    async def nowplaying(self, ctx: Context, member: Optional[Member] = None) -> None:
        """Display the user's currently playing track or recent track."""
        target = member or ctx.author
        username = await self.get_username(target.id)
        if not username:
            if target == ctx.author:
                return await ctx.warn("You don't have a **Last.fm username** set! Set one using `,lastfm set <username>`.")
            return await ctx.warn(f"**{target.name}** doesn't have a **Last.fm username** set.")

        try:
            recent_tracks = await self.request("user.getRecentTracks", user=username, limit=1)
        except ValueError as e:
            return await ctx.warn(str(e))

        tracks = recent_tracks.get("recenttracks", {}).get("track", [])
        if not tracks:
            return await ctx.warn(f"No recent tracks found for **[{username}](https://www.last.fm/user/{username})**.")

        track = tracks[0] if isinstance(tracks, list) else tracks
        track_name = track.get("name", "Unknown Track")
        artist_name = track.get("artist", {}).get("#text") or track.get("artist", {}).get("name") or "Unknown Artist"
        album_name = track.get("album", {}).get("#text", "")
        track_url = track.get("url", f"https://www.last.fm/user/{username}")

        # Check if currently playing
        attr = track.get("@attr", {})
        is_now_playing = attr.get("nowplaying") == "true"

        # Image extraction
        images = track.get("image", [])
        image_url = ""
        if isinstance(images, list) and images:
            # Grab extralarge or large image if available
            for img in reversed(images):
                if img.get("#text"):
                    image_url = img.get("#text")
                    break

        # Fetch total scrobbles & artist scrobbles asynchronously
        user_scrobbles = "0"
        artist_scrobbles = "0"
        try:
            user_info = await self.request("user.getInfo", user=username)
            user_scrobbles = f"{int(user_info.get('user', {}).get('playcount', 0)):,}"
        except Exception:
            pass

        try:
            artist_info = await self.request("artist.getInfo", artist=artist_name, username=username)
            artist_scrobbles = f"{int(artist_info.get('artist', {}).get('stats', {}).get('userplaycount', 0)):,}"
        except Exception:
            pass

        embed = Embed(color=COLORS.neutral)
        embed.set_author(
            name=f"Now playing - {username}" if is_now_playing else f"Last played - {username}",
            icon_url=target.display_avatar.url,
            url=f"https://www.last.fm/user/{username}",
        )

        description_lines = [
            f"**[{track_name}]({track_url})**",
            f"by **{artist_name}**" + (f" on **{album_name}**" if album_name else "")
        ]
        embed.description = "\n".join(description_lines)

        if image_url:
            embed.set_thumbnail(url=image_url)

        embed.set_footer(
            text=f"Artist Scrobbles: {artist_scrobbles} • Total Scrobbles: {user_scrobbles}",
            icon_url="https://cdn.discordapp.com/emojis/1047402685718315028.png?v=1" # standard lastfm icon or blank
        )

        await ctx.send(embed=embed)

    @lastfm.command(
        name="scrobbles",
        description="View scrobbles info for a Last.fm user.",
        example="lastfm scrobbles",
    )
    @cooldown(1, 3, BucketType.user)
    async def scrobbles(self, ctx: Context, member: Optional[Member] = None) -> None:
        """View scrobble statistics and overview."""
        target = member or ctx.author
        username = await self.get_username(target.id)
        if not username:
            if target == ctx.author:
                return await ctx.warn("You don't have a **Last.fm username** set! Set one using `,lastfm set <username>`.")
            return await ctx.warn(f"**{target.name}** doesn't have a **Last.fm username** set.")

        try:
            user_info = await self.request("user.getInfo", user=username)
        except ValueError as e:
            return await ctx.warn(str(e))

        user_data = user_info.get("user", {})
        playcount = int(user_data.get("playcount", 0))
        artist_count = int(user_data.get("artist_count", 0))
        track_count = int(user_data.get("track_count", 0))
        album_count = int(user_data.get("album_count", 0))
        registered = int(user_data.get("registered", {}).get("unixtime", 0))

        images = user_data.get("image", [])
        avatar_url = ""
        if isinstance(images, list) and images:
            avatar_url = images[-1].get("#text", "")

        embed = Embed(
            title=f"{username}'s Scrobble Stats",
            url=f"https://www.last.fm/user/{username}",
            color=COLORS.neutral
        )
        embed.set_author(name=target.display_avatar.url, icon_url=target.display_avatar.url)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="Total Scrobbles", value=f"**{playcount:,}**", inline=True)
        embed.add_field(name="Artists Played", value=f"**{artist_count:,}**", inline=True)
        embed.add_field(name="Tracks Played", value=f"**{track_count:,}**", inline=True)
        embed.add_field(name="Albums Played", value=f"**{album_count:,}**", inline=True)

        if registered:
            reg_dt = datetime.fromtimestamp(registered, tz=timezone.utc)
            embed.add_field(name="Account Created", value=format_dt(reg_dt, style="R"), inline=True)

        await ctx.send(embed=embed)

    @lastfm.command(
        name="info",
        description="View your Last.fm profile information.",
        example="lastfm info",
    )
    @cooldown(1, 3, BucketType.user)
    async def info(self, ctx: Context, member: Optional[Member] = None) -> None:
        """View detailed profile information for self or target member."""
        await self.whois(ctx, member=member)

    @lastfm.command(
        name="whois",
        description="View a Last.fm profile for you or another member.",
        example="lastfm whois @voby",
    )
    @cooldown(1, 3, BucketType.user)
    async def whois(self, ctx: Context, member: Optional[Member] = None) -> None:
        """View a Last.fm profile for a Discord member."""
        target = member or ctx.author
        username = await self.get_username(target.id)
        if not username:
            if target == ctx.author:
                return await ctx.warn("You don't have a **Last.fm username** set! Set one using `,lastfm set <username>`.")
            return await ctx.warn(f"**{target.name}** doesn't have a **Last.fm username** set.")

        try:
            user_info = await self.request("user.getInfo", user=username)
        except ValueError as e:
            return await ctx.warn(str(e))

        user_data = user_info.get("user", {})
        playcount = int(user_data.get("playcount", 0))
        country = user_data.get("country", "Unknown")
        registered = int(user_data.get("registered", {}).get("unixtime", 0))
        user_url = user_data.get("url", f"https://www.last.fm/user/{username}")

        images = user_data.get("image", [])
        avatar_url = ""
        if isinstance(images, list) and images:
            avatar_url = images[-1].get("#text", "")

        embed = Embed(
            title=f"Last.fm Profile: {username}",
            url=user_url,
            color=COLORS.neutral
        )
        embed.set_author(name=f"{target.name} ({target.id})", icon_url=target.display_avatar.url)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        embed.add_field(name="Username", value=f"[{username}]({user_url})", inline=True)
        embed.add_field(name="Scrobbles", value=f"{playcount:,}", inline=True)
        embed.add_field(name="Country", value=country if country != "None" else "Unknown", inline=True)

        if registered:
            reg_dt = datetime.fromtimestamp(registered, tz=timezone.utc)
            embed.add_field(name="Registered", value=format_dt(reg_dt, style="f") + f" ({format_dt(reg_dt, style='R')})", inline=False)

        await ctx.send(embed=embed)

    @lastfm.command(
        name="topartists",
        description="View your top artists on Last.fm.",
        example="lastfm topartists",
    )
    @cooldown(1, 3, BucketType.user)
    async def topartists(self, ctx: Context, member: Optional[Member] = None) -> None:
        """View top artists with paginator."""
        target = member or ctx.author
        username = await self.get_username(target.id)
        if not username:
            if target == ctx.author:
                return await ctx.warn("You don't have a **Last.fm username** set! Set one using `,lastfm set <username>`.")
            return await ctx.warn(f"**{target.name}** doesn't have a **Last.fm username** set.")

        try:
            top_artists_data = await self.request("user.getTopArtists", user=username, limit=50)
        except ValueError as e:
            return await ctx.warn(str(e))

        artists = top_artists_data.get("topartists", {}).get("artist", [])
        if not artists:
            return await ctx.warn(f"No top artists found for **[{username}](https://www.last.fm/user/{username})**.")

        entries = []
        for idx, artist in enumerate(artists, start=1):
            name = artist.get("name", "Unknown Artist")
            plays = int(artist.get("playcount", 0))
            url = artist.get("url", "https://www.last.fm")
            entries.append(f"`{idx}` **[{name}]({url})** — **{plays:,}** plays")

        per_page = 10
        pages = []
        total_pages = math.ceil(len(entries) / per_page)

        for i in range(0, len(entries), per_page):
            chunk = entries[i : i + per_page]
            embed = Embed(
                title=f"{username}'s Top Artists",
                url=f"https://www.last.fm/user/{username}",
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_author(name=target.display_avatar.url, icon_url=target.display_avatar.url)
            embed.set_footer(text=f"Page {len(pages) + 1}/{total_pages} • Total Artists: {len(entries)}")
            pages.append(embed)

        await ctx.paginate(pages)

    @lastfm.command(
        name="recommendation",
        aliases=["recommend"],
        description="Get random recommendations based on your top artists library.",
        example="lastfm recommendation",
    )
    @cooldown(1, 3, BucketType.user)
    async def recommendation(self, ctx: Context) -> None:
        """Get random artist recommendations with paginator."""
        username = await self.get_username(ctx.author.id)
        if not username:
            return await ctx.warn("You don't have a **Last.fm username** set! Set one using `,lastfm set <username>`.")

        # Fetch user's top artists to base recommendation on
        try:
            top_artists_data = await self.request("user.getTopArtists", user=username, limit=20)
        except ValueError as e:
            return await ctx.warn(str(e))

        top_artists = top_artists_data.get("topartists", {}).get("artist", [])
        if not top_artists:
            return await ctx.warn("Could not find enough top artists in your library for recommendations.")

        # Pick random 3 seed artists
        seeds = random.sample(top_artists, min(3, len(top_artists)))
        similar_artists: List[Tuple[str, str, str]] = [] # (Name, URL, BasedOn)
        seen_names = {a.get("name", "").lower() for a in top_artists}

        for seed in seeds:
            seed_name = seed.get("name")
            try:
                similar_data = await self.request("artist.getSimilar", artist=seed_name, limit=10)
                sim_list = similar_data.get("similarartists", {}).get("artist", [])
                for sim in sim_list:
                    sim_name = sim.get("name", "")
                    if sim_name and sim_name.lower() not in seen_names:
                        seen_names.add(sim_name.lower())
                        similar_artists.append((sim_name, sim.get("url", "https://www.last.fm"), seed_name))
            except Exception:
                continue

        if not similar_artists:
            return await ctx.warn("Could not generate recommendations based on your library at this time.")

        random.shuffle(similar_artists)

        entries = []
        for idx, (name, url, based_on) in enumerate(similar_artists[:25], start=1):
            entries.append(f"`{idx}` **[{name}]({url})** (Similar to *{based_on}*)")

        per_page = 5
        pages = []
        total_pages = math.ceil(len(entries) / per_page)

        for i in range(0, len(entries), per_page):
            chunk = entries[i : i + per_page]
            embed = Embed(
                title=f"Recommendations for {username}",
                description="\n".join(chunk),
                color=COLORS.neutral,
            )
            embed.set_author(name=ctx.author.display_avatar.url, icon_url=ctx.author.display_avatar.url)
            embed.set_footer(text=f"Page {len(pages) + 1}/{total_pages} • Based on your listening history")
            pages.append(embed)

        await ctx.paginate(pages)
