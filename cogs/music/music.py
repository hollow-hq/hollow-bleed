import asyncio
import logging
from typing import Optional

import discord
import wavelink
from discord import ActivityType, Embed, Message
from discord.ext import commands
from discord.ext.commands import BucketType, cooldown
from base.managers.commands import hybrid_command, hybrid_group

from base.config import COLORS, EMOJIS
from base.context import Context
from base.managers.types import CogMeta

_log = logging.getLogger(__name__)

NODE_URI = "https://music.breed.rip/"
NODE_PASSWORD = "O/1g5+KImwJUCD3qt0AEPTJ4uykLZ4bRgyuvkI7fKIU="
NODE_IDENTIFIER = "hollow"

# Seconds of silence (empty queue, nothing playing) before the bot leaves.
INACTIVE_TIMEOUT = 10


def _build_8d(filters: wavelink.Filters) -> None:
    filters.rotation.set(rotation_hz=0.2)


def _build_earrape(filters: wavelink.Filters) -> None:
    filters.distortion.set(
        sin_offset=0.5,
        sin_scale=1.5,
        cos_offset=0.5,
        cos_scale=1.5,
        tan_offset=0.5,
        tan_scale=1.5,
        offset=0.5,
        scale=1.5,
    )


def _build_nightcore(filters: wavelink.Filters) -> None:
    filters.timescale.set(speed=1.3, pitch=1.3, rate=1.0)


def _build_daycore(filters: wavelink.Filters) -> None:
    filters.timescale.set(speed=0.7, pitch=0.7, rate=1.0)


PRESETS = {
    "8d": ("8D spatial audio", _build_8d),
    "earrape": ("distorted audio", _build_earrape),
    "nightcore": ("sped up and pitched up", _build_nightcore),
    "daycore": ("slowed down and pitched down", _build_daycore),
}


def _format_duration(milliseconds: int, is_stream: bool = False) -> str:
    if is_stream or milliseconds <= 0:
        return "LIVE"

    seconds = milliseconds // 1000
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


class Music(CogMeta):
    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._default_activity = bot.activity
        self._connect_task: Optional[asyncio.Task] = None

    async def cog_load(self) -> None:
        # The cog may load before the bot is logged in, so connect in the background.
        self._connect_task = asyncio.create_task(self._connect_node())

    async def cog_unload(self) -> None:
        if self._connect_task is not None:
            self._connect_task.cancel()

        try:
            await wavelink.Pool.close()
        except Exception:
            _log.exception("Failed to close the NodeLink pool")

    async def _connect_node(self) -> None:
        await self.bot.wait_until_ready()

        if NODE_IDENTIFIER in wavelink.Pool.nodes:
            return

        node = wavelink.Node(
            identifier=NODE_IDENTIFIER,
            uri=NODE_URI.rstrip("/"),
            password=NODE_PASSWORD,
        )

        try:
            await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
        except Exception:
            _log.exception("Failed to connect to the NodeLink node")

    # --------------------------------------------------------------- helpers

    async def _restore_presence(self) -> None:
        try:
            await self.bot.change_presence(activity=self._default_activity)
        except Exception:
            _log.exception("Failed to restore the bot's activity")

    @staticmethod
    def _text_channel(player: Optional[wavelink.Player]):
        return getattr(player, "text_channel", None) if player is not None else None

    async def _notify(self, player: Optional[wavelink.Player], embed: Embed) -> None:
        channel = self._text_channel(player)
        if channel is None:
            return

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- events

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        _log.info("NodeLink node %s is ready (resumed=%s)", payload.node.identifier, payload.resumed)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        track = payload.track

        try:
            await self.bot.change_presence(
                activity=discord.Activity(type=ActivityType.listening, name=track.title)
            )
        except Exception:
            _log.exception("Failed to update the bot's activity")

        requester_id = getattr(track.extras, "requester", None)
        requester = self.bot.get_user(requester_id) if requester_id else None

        embed = Embed(
            color=COLORS.neutral,
            title="Now playing",
            description=f"**[{track.title}]({track.uri})**" if track.uri else f"**{track.title}**",
        )
        embed.add_field(name="Duration", value=_format_duration(track.length, track.is_stream), inline=True)
        embed.add_field(name="Requested by", value=requester.mention if requester else "Unknown", inline=True)
        if track.author:
            embed.set_footer(text=f"{EMOJIS.info} {track.author}")

        await self._notify(player, embed)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        # Fired when the queue has been empty and nothing has played for INACTIVE_TIMEOUT seconds.
        try:
            await player.disconnect()
        except Exception:
            _log.exception("Failed to leave the voice channel")

        await self._restore_presence()

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        message = (payload.exception or {}).get("message", "Unknown error")

        _log.warning(
            "Track %s failed on guild %s: %s",
            payload.track.title,
            player.guild.id if player is not None else "?",
            message,
        )

        embed = Embed(
            color=COLORS.error,
            description=f"{EMOJIS.warn} **{payload.track.title}** failed to play: {message}",
        )
        await self._notify(player, embed)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        player = payload.player

        _log.warning(
            "Track %s got stuck on guild %s (threshold: %dms)",
            payload.track.title,
            player.guild.id if player is not None else "?",
            payload.threshold,
        )

        embed = Embed(
            color=COLORS.warn,
            description=f"{EMOJIS.warn} **{payload.track.title}** got stuck, skipping...",
        )
        await self._notify(player, embed)

    # ----------------------------------------------------------- player logic

    async def _resolve_player(self, ctx: Context, *, connect: bool = False) -> Optional[wavelink.Player]:
        if ctx.guild is None:
            await ctx.deny("This command can't be used in private messages.")
            return None

        voice_state = ctx.author.voice
        voice_client = ctx.voice_client

        if voice_state is None or voice_state.channel is None:
            if voice_client is not None:
                await ctx.deny("You need to join my voice channel first.")
            else:
                await ctx.deny("You need to join a voice channel first.")
            return None

        channel = voice_state.channel

        if voice_client is None:
            if not connect:
                await ctx.deny("I'm not connected to a voice channel.")
                return None

            permissions = channel.permissions_for(ctx.me)
            if not permissions.connect or not permissions.speak:
                await ctx.deny("I need the `CONNECT` and `SPEAK` permissions in that channel.")
                return None

            if channel.user_limit > 0 and len(channel.members) >= channel.user_limit:
                await ctx.deny("That voice channel is full.")
                return None

            try:
                player = await channel.connect(cls=wavelink.Player, self_deaf=True)
            except wavelink.WavelinkException as error:
                await ctx.deny(f"NodeLink is unavailable: {error}")
                return None

            player.autoplay = wavelink.AutoPlayMode.partial  # play the queue, no recommendations
            player.inactive_timeout = INACTIVE_TIMEOUT
        elif not isinstance(voice_client, wavelink.Player):
            await ctx.deny("I'm already connected to a voice channel using something else.")
            return None
        elif voice_client.channel.id != channel.id:
            await ctx.deny("You need to be in my voice channel to use this command.")
            return None
        else:
            player = voice_client

        player.text_channel = ctx.channel
        return player

    # -------------------------------------------------------------- commands

    @hybrid_command(
        name="play",
        description="Search for and play music.",
        example="play never gonna give you up",
        usage="play <query>",
    )
    @cooldown(1, 3, BucketType.user)
    async def play(self, ctx: Context, *, query: str) -> Message:
        player = await self._resolve_player(ctx, connect=True)
        if player is None:
            return

        query = query.strip().strip("<>")

        try:
            results = await wavelink.Playable.search(query)
        except wavelink.LavalinkLoadException as error:
            return await ctx.deny(f"Track loading failed: {error}")
        except wavelink.WavelinkException as error:
            return await ctx.deny(f"NodeLink is unavailable: {error}")

        if not results:
            return await ctx.deny("I couldn't find any tracks for that query.")

        if isinstance(results, wavelink.Playlist):
            results.track_extras(requester=ctx.author.id)
            await player.queue.put_wait(results)

            await ctx.approve(f"Enqueued **{len(results.tracks)}** tracks from **{results.name}**.")
        else:
            track = results[0]
            track.extras = {"requester": ctx.author.id}
            await player.queue.put_wait(track)

            await ctx.approve(
                f"Enqueued **[{track.title}]({track.uri})**" if track.uri else f"Enqueued **{track.title}**"
            )

        if not player.playing:
            await player.play(player.queue.get())

    @hybrid_command(name="skip", description="Skip the currently playing music.", example="skip")
    async def skip(self, ctx: Context) -> Message:
        player = await self._resolve_player(ctx)
        if player is None:
            return

        if not player.playing or player.current is None:
            return await ctx.deny("There's no music playing.")

        track = player.current
        await player.skip(force=True)

        return await ctx.approve(f"Skipped **{track.title}**.")

    @hybrid_command(
        name="stop",
        description="Stop everything and leave the voice channel.",
        example="stop",
    )
    async def stop(self, ctx: Context) -> Message:
        if ctx.guild is None:
            return await ctx.deny("This command can't be used in private messages.")

        player = ctx.voice_client
        if not isinstance(player, wavelink.Player):
            return await ctx.deny("I'm not connected to a voice channel.")

        player.queue.clear()

        try:
            await player.disconnect()
        except Exception as error:
            _log.exception("Failed to leave the voice channel")
            return await ctx.deny(f"Stopped playback, but failed to leave the voice channel: {error}")

        await self._restore_presence()
        return await ctx.approve("Stopped playback and left the voice channel.")

    @hybrid_command(
        name="volume",
        description="Change the volume (1-100).",
        example="volume 50",
        usage="volume <1-100>",
    )
    async def volume(self, ctx: Context, volume: int) -> Message:
        if not 1 <= volume <= 100:
            return await ctx.deny("Volume must be between **1** and **100**.")

        player = await self._resolve_player(ctx)
        if player is None:
            return

        await player.set_volume(volume)
        return await ctx.approve(f"Volume set to **{volume}%**.")

    @hybrid_command(
        name="preset",
        description="Apply a music preset.",
        example="preset nightcore",
        usage="preset <preset>",
    )
    async def preset(self, ctx: Context, preset: Optional[str] = None) -> Message:
        if preset is None:
            embed = Embed(
                color=COLORS.neutral,
                title="Music presets",
                description=(
                    f"Apply a preset with `{ctx.clean_prefix}preset <name>`\n\n"
                    + "\n".join(f"**{name}** — {description}" for name, (description, _) in PRESETS.items())
                    + "\n**off** — clear all presets"
                ),
            )
            return await ctx.send(embed=embed)

        preset = preset.lower()

        if preset == "off":
            player = await self._resolve_player(ctx)
            if player is None:
                return

            await player.set_filters(None, seek=True)
            return await ctx.approve("Presets cleared — audio is back to normal.")

        if preset not in PRESETS:
            return await ctx.deny(
                f"Unknown preset **{preset}**. Available: {', '.join(PRESETS)} (or **off** to clear)."
            )

        player = await self._resolve_player(ctx)
        if player is None:
            return

        description, build = PRESETS[preset]
        filters = wavelink.Filters()
        build(filters)

        await player.set_filters(filters, seek=True)
        return await ctx.approve(f"Applied the **{preset}** preset ({description}).")