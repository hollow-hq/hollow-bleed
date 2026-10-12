import glob
import re
import traceback
import discord_ios
import os
import time
import pathlib

from typing import Any, List, Dict, Optional, Set
from datetime import datetime

import asyncmy

from base.database import Database

import base.env  # noqa: F401  (loads .env into os.environ first)

from discord import (
    AllowedMentions,
    Message,
    CustomActivity,
    Intents,
    Embed,
    Guild,
    Forbidden,
    Member,
)
from discord.utils import format_dt
from discord.ext.commands import (
    AutoShardedBot,
    when_mentioned_or,
    CommandError,
    CheckFailure,
    CommandNotFound,
    NotOwner,
    CommandOnCooldown,
    ChannelNotFound,
    RoleNotFound,
    ThreadNotFound,
    UserNotFound,
    MissingPermissions,
    MinimalHelpCommand,
    CooldownMapping,
    BucketType,
    MissingRequiredArgument,
    UserInputError,
    BadArgument,
)

from loguru import logger
from base.context import Context, HollowHelp
# from base.managers.browser import BrowserHandler
from collections import defaultdict
from os import environ

environ["JISHAKU_HIDE"] = "True"
environ["JISHAKU_RETAIN"] = "True"
environ["JISHAKU_NO_UNDERSCORE"] = "True"
environ["JISHAKU_SHELL_NO_DM_TRACEBACK"] = "True"


class Bot(AutoShardedBot):
    pool: Database
    boot: datetime

    def __init__(self) -> None:
        super().__init__(
            command_prefix=",",  # type: ignore
            case_insensitive=True,
            intents=Intents.all(),
            help_command=HollowHelp(),
            allowed_mentions=AllowedMentions(
                everyone=False, roles=False, replied_user=False
            ),
            activity=CustomActivity(name="🔗 hollow.js.cool"),
            owner_ids=[
                1470775670262202590,
                1519298805852410009
            ],
        )
        # self.browser_handler = BrowserHandler()
        self._uptime = time.time()
        self.message_cache = defaultdict(list)
        self.cache_expiry_seconds = 30
        self._prefix_cache: Dict[int, tuple] = {}
        self._blacklist: Set[int] = set()
        self._blacklist_loaded: bool = False
        self._blacklist_expiry_seconds: int = 300
        self._blacklist_loaded_at: float = 0.0
        self._afk: Set[int] = set()
        self._afk_loaded: bool = False
        self._afk_expiry_seconds: int = 30
        self._afk_loaded_at: float = 0.0
        self.member_cooldown = CooldownMapping.from_cooldown(3, 5, BucketType.user)
        self.channel_cooldown = CooldownMapping.from_cooldown(4, 5, BucketType.channel)

    @property
    def ping(self) -> int:
        return round(self.latency * 1000)

    @property
    def booted(self):
        return format_dt(self.boot, style="R")

    @property
    def uptime(self) -> str:
        return self.humanize_time(self._uptime)

    @property
    def linecount(self) -> int:
        return sum(
            [
                len(f.open("r").readlines())
                for f in [
                    f
                    for f in pathlib.Path("/home/container/hollow").glob("**/*.py")
                    if f.is_file()
                ]
            ]
        )

    async def get_prefix(self, message: Message) -> tuple:
        if message.guild is None:
            return  # type: ignore

        guild_id = message.guild.id

        cached = self._prefix_cache.get(guild_id)
        if cached is None:
            try:
                cached = await self._load_guild_prefixes(guild_id)
            except Exception as e:
                logger.warning(f"Prefix lookup failed for guild {guild_id}: {e}")
                return (",",)
            self._prefix_cache[guild_id] = cached

        guild_prefix, self_prefixes, noprefix_users = cached

        prefixes = [guild_prefix]
        prefixes.extend(self_prefixes.get(message.author.id, ()))  # type: ignore
        if message.author.id in noprefix_users:
            prefixes.append("")

        return tuple(prefixes)

    async def _load_guild_prefixes(self, guild_id: int) -> tuple:
        """Fetch every prefix row for a guild in one go so it can live in RAM."""
        guild_prefix = (
            await self.pool.fetchval(
                "SELECT prefix FROM prefix WHERE guild_id = %s", guild_id
            )
            or ","
        )

        self_rows = await self.pool.fetch(
            "SELECT user_id, prefix FROM self_prefixes WHERE guild_id = %s",
            guild_id,
        )
        self_prefixes: Dict[int, List[str]] = defaultdict(list)
        for row in self_rows:
            self_prefixes[row["user_id"]].append(row["prefix"])

        noprefix_rows = await self.pool.fetch(
            "SELECT user_id FROM noprefix WHERE guild_id = %s", guild_id
        )
        noprefix_users = {row["user_id"] for row in noprefix_rows}

        return guild_prefix, dict(self_prefixes), noprefix_users

    async def is_blacklisted(self, user_id: int) -> bool:
        """Answer from RAM; only hit the DB when the cached set is stale or the
        user is actually blacklisted (so a fresh hit still propagates at once)."""
        now = time.time()
        if (
            self._blacklist_loaded
            and now - self._blacklist_loaded_at < self._blacklist_expiry_seconds
        ):
            return user_id in self._blacklist

        rows = await self.pool.fetch("SELECT user_id FROM blacklist")
        self._blacklist = {row["user_id"] for row in rows}
        self._blacklist_loaded = True
        self._blacklist_loaded_at = now
        return user_id in self._blacklist

    def reset_blacklist_cache(self) -> None:
        """Force the next is_blacklisted() call to reload from the database."""
        self._blacklist.clear()
        self._blacklist_loaded = False
        self._blacklist_loaded_at = 0.0

    async def guild_blacklist(self, guild_id: int) -> bool:
        """Blacklist a guild. Returns True when it was newly blacklisted."""
        already = await self.pool.fetchval(
            "SELECT guild_id FROM guild_blacklist WHERE guild_id = %s", guild_id
        )

        if already:
            return False

        await self.pool.execute(
            "INSERT INTO guild_blacklist (guild_id) VALUES (%s)", guild_id
        )
        return True

    async def guild_unblacklist(self, guild_id: int) -> bool:
        """Unblacklist a guild. Returns True when it was actually blacklisted."""
        was_blacklisted = await self.pool.fetchval(
            "SELECT guild_id FROM guild_blacklist WHERE guild_id = %s", guild_id
        )

        if not was_blacklisted:
            return False

        await self.pool.execute(
            "DELETE FROM guild_blacklist WHERE guild_id = %s", guild_id
        )
        return True

    async def is_guild_blacklisted(self, guild_id: int) -> bool:
        """Whether the guild is on the guild blacklist."""
        check = await self.pool.fetchval(
            "SELECT guild_id FROM guild_blacklist WHERE guild_id = %s", guild_id
        )
        return check is not None

    async def is_afk(self, user_id: int) -> bool:
        """Fast check backed by a short TTL cache; refreshes the id set when stale."""
        now = time.time()
        if self._afk_loaded and now - self._afk_loaded_at < self._afk_expiry_seconds:
            return user_id in self._afk

        rows = await self.pool.fetch("SELECT user_id FROM afk")
        self._afk = {row["user_id"] for row in rows}
        self._afk_loaded = True
        self._afk_loaded_at = now
        return user_id in self._afk

    def mark_afk(self, user_id: int) -> None:
        """Record locally that a user went AFK so the cache refreshes at once."""
        self._afk.add(user_id)

    def mark_not_afk(self, user_id: int) -> None:
        self._afk.discard(user_id)

    def reset_prefix_cache(self, guild_id: Optional[int] = None) -> None:
        """Drop cached prefixes (single guild or everything).

        Call this after editing the prefix, self_prefixes or noprefix tables so
        the change shows up without a restart.
        """
        if guild_id is None:
            self._prefix_cache.clear()
        else:
            self._prefix_cache.pop(guild_id, None)

    async def load_modules(self, directory: str = "cogs"):
        """
        Recursively load all modules in the directory selected.

        Args:
            directory (str): This is the directory meant to contain all modules, defaults to 'extensions'.
        """
        for module in glob.glob(f"{directory}/**/__init__.py", recursive=True):
            module_path = (
                module.replace("/", ".")
                .replace("\\", ".")
                .replace(".__init__", "")[:-3]
            )
            try:
                await self.load_extension(module_path)
                logger.info(f"Loaded extension: {module_path}")
            except Exception:
                logger.warning(
                    f"Extension failed to load: {module}\n{traceback.format_exc()}"
                )

    @staticmethod
    def _db_config() -> dict:
        """Database connection settings from the environment.

        Required: DB_HOST, DB_USER, DB_PASSWORD, DB_NAME.
        Optional: DB_PORT (default 3306), DB_POOL_MAX (default 30),
        DB_POOL_MIN (default 2), DB_CONNECT_TIMEOUT (default 10).

        Raises RuntimeError with a sanitized message (never echoes the
        password) when required settings are missing so misconfiguration
        fails fast instead of connecting with defaults.
        """
        host = os.getenv("DB_HOST", "").strip()
        user = os.getenv("DB_USER", "").strip()
        password = os.getenv("DB_PASSWORD", "")
        name = os.getenv("DB_NAME", "").strip()
        missing = [k for k, v in (("DB_HOST", host), ("DB_USER", user), ("DB_PASSWORD", password), ("DB_NAME", name)) if not v]
        if missing:
            raise RuntimeError(
                "Missing database configuration: "
                + ", ".join(missing)
                + ". Set them in the environment or a .env file (see .env.example)."
            )
        try:
            port = int(os.getenv("DB_PORT", "3306"))
            maxsize = int(os.getenv("DB_POOL_MAX", "30"))
            minsize = int(os.getenv("DB_POOL_MIN", "2"))
            connect_timeout = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
        except ValueError as exc:
            raise RuntimeError(f"Invalid numeric database setting: {exc}")
        return {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "db": name,
            "maxsize": maxsize,
            "minsize": minsize,
            "connect_timeout": connect_timeout,
            "autocommit": True,
        }

    async def _load_database(self) -> Database:
        try:
            config = self._db_config()
            pool = await asyncmy.create_pool(**config)
            logger.info(
                f"Database connection established to {config['host']}:{config['port']} "
                f"as {config['user']} (db={config['db']})"
            )
            database = Database(pool)

            schema_path = pathlib.Path(__file__).parent.parent / "base" / "schema" / "schema.sql"
            with open(schema_path, "r") as file:
                schema = file.read()
                if schema.strip():
                    existing_tables = await self._get_existing_tables(database)
                    for statement in self._split_statements(schema):
                        if statement.startswith("-- migrate"):
                            # Migration statements run on every boot; a
                            # duplicate-column error just means it's already
                            # applied, so it is swallowed instead of logged.
                            body = self._unwrap_migrate(statement)
                            if not body:
                                continue
                            try:
                                await database.execute(body)
                            except Exception as exc:
                                if not self._is_duplicate_column(exc):
                                    logger.error(
                                        f"Failed to run schema migration: {exc}\n{body}"
                                    )
                            continue
                        table_name = self._table_name(statement)
                        if table_name and table_name in existing_tables:
                            continue
                        try:
                            await database.execute(statement)
                        except Exception as e:
                            if self._is_duplicate_column(e):
                                continue
                            logger.error(
                                f"Failed to run schema statement: {e}\n{statement.strip()}"
                            )
                    logger.info("Database schema loaded")
                else:
                    logger.warning("Database schema file is empty")

            return database
        except Exception as e:
            logger.error(f"Error loading database: {e}")
            raise e

    @staticmethod
    async def _get_existing_tables(database: Database) -> set:
        rows = await database.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()"
        )
        return {row["table_name"] for row in rows}

    @staticmethod
    def _unwrap_migrate(statement: str) -> Optional[str]:
        """Return the SQL of a ``-- migrate`` statement without its marker.

        The schema marks a statement as an every-boot migration by opening it
        with a ``-- migrate ...`` comment line (``_split_statements`` keeps the
        marker and the SQL together). SQL itself uses ``--`` for comments, so
        when the marker and the SQL are separated by a blank line MariaDB only
        sees the marker line and reports the following SQL as a syntax error
        (which is what made every migration fail on boot). Stripping the
        comment lines here guarantees the migration actually runs.
        """
        return "\n".join(
            line
            for line in statement.splitlines()
            if not line.strip().startswith("--")
        ).strip() or None

    @staticmethod
    def _is_duplicate_column(exc: Exception) -> bool:
        """Return ``True`` for idempotent re-run errors that are safe to swallow.

        Error 1060 (ER_DUP_FIELDNAME) is what ``ALTER TABLE ... ADD COLUMN``
        reports when the column already exists, i.e. when the migration was
        applied on a previous boot. Error 1050 (ER_TABLE_EXISTS_ERROR) can
        surface the same way for ``CREATE TABLE`` when the existence check
        raced the create (or the server surfaces ``IF NOT EXISTS`` as a
        warning the driver promotes); that one is equally expected and safe
        to ignore.
        """
        args: tuple = getattr(exc, "args", ()) or ()
        code = args[0] if args else None
        if code in (1050, 1060):
            return True
        text = str(exc).lower()
        return "duplicate column" in text or "already exists" in text

    @staticmethod
    def _table_name(statement: str) -> Optional[str]:
        match = re.match(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([A-Za-z0-9_]+)`?",
            statement,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    @staticmethod
    def _split_statements(schema: str) -> List[str]:
        """Split a schema dump into single statements.

        Each statement runs on its own so a failure cannot abort the rest of
        the batch, which is how missing tables go unnoticed until a command
        or task queries them. Statements tagged with a leading ``-- migrate``
        comment are kept together with that marker so the loader can run them
        on every boot (see :meth:`_load_database`).

        Semicolons inside ``--`` comment lines do **not** end a statement.
        Schema comments are prose and routinely contain a ``;`` mid-sentence
        (e.g. "stored lowercased; the listener matches ..."); splitting on
        them used to leave the remainder of the comment as a bogus statement
        whose first line is raw garbage once the leading ``--`` line is gone,
        which is exactly the ``near 'the listener matches ...'`` syntax error
        MariaDB reported. Plain comment lines are also dropped from the
        emitted SQL so only the ``-- migrate`` marker is carried along.
        """
        statements: List[str] = []
        current: List[str] = []

        def flush() -> None:
            body_lines = [line for line in current if not line.strip().startswith("--")]
            body = "\n".join(body_lines).strip()
            if not body:
                return
            marker = next(
                (line.strip() for line in current if line.strip().startswith("-- migrate")),
                None,
            )
            statements.append(f"{marker}\n{body}" if marker else body)

        for line in schema.splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                # Comment lines never terminate a statement, so their
                # semicolons are ignored; keep them for marker detection.
                current.append(line)
                continue
            # A line may contain several statements separated by semicolons.
            parts = line.split(";")
            for index, part in enumerate(parts):
                if index < len(parts) - 1:
                    current.append(part)
                    flush()
                    current = []
                else:
                    current.append(part)
        flush()
        return statements

    async def setup_hook(self):
        self.pool = await self._load_database()  # type: ignore
        await self.load_modules()
        self.boot = datetime.now()
        await self.load_extension("jishaku")

        # Mirror auto-created groups onto the bot before tree sync
        from base.managers.commands import _AUTO_GROUPS, _GROUPS, validate_routing
        for grp_name in _AUTO_GROUPS:
            grp = _GROUPS.get(grp_name)
            if grp and grp.name not in self.all_commands:
                self.add_command(grp)

        # Validate routing configuration
        validate_routing(self)

        return await super().setup_hook()

    def format_size(self, bytes):
        """
        Convert bytes to a human-readable format.
        """
        if bytes >= 1024**3:
            return f"{bytes / (1024**3):.2f}GB"
        elif bytes >= 1024**2:
            return f"{bytes / (1024**2):.2f}MB"
        elif bytes >= 1024:
            return f"{bytes / 1024:.2f}KB"
        else:
            return f"{bytes} Bytes"

    def humanize_number(self, number: int) -> str:
        suffixes = ["", "k", "m", "b", "t"]
        magnitude = min(len(suffixes) - 1, (len(str(abs(number))) - 1) // 3)
        formatted_number = (
            "{:.1f}".format(number / 10 ** (3 * magnitude)).rstrip("0").rstrip(".")
        )
        return "{}{}".format(formatted_number, suffixes[magnitude])

    async def get_context(self, message: Message, *, cls=Context):  # type: ignore
        return await super().get_context(message, cls=cls)

    def humanize_time(self, start_time: float) -> str:
        uptime_seconds = abs(time.time() - start_time)
        intervals = (
            ("year", 31556952),
            ("month", 2629746),
            ("day", 86400),
            ("hour", 3600),
            ("minute", 60),
            ("second", 1),
        )

        result = []
        for name, count in intervals:
            value = uptime_seconds // count
            if value:
                uptime_seconds -= value * count
                result.append(f"{int(value)} {name}{'s' if value > 1 else ''}")

        return ", ".join(result)

    def add_command(self, command, /) -> None:
        """Add command to bot, handling decoupled slash routing for nested commands."""
        nested_group = getattr(command, "_nested_in", None)
        if not nested_group and hasattr(command, "extras"):
            nested_group = command.extras.get("_nested_in")
        if not nested_group and hasattr(command, "callback"):
            nested_group = getattr(command.callback, "_nested_in", None)

        if nested_group:
            from discord.ext.commands.core import GroupMixin
            # Add to prefix commands only so prefix works top-level (e.g. ,ban)
            GroupMixin.add_command(self, command)

            # Ensure slash command is attached to the group in bot.tree / _GROUPS
            from base.managers.commands import _GROUPS, _ensure_nested_group
            grp = _ensure_nested_group(nested_group)
            if grp and grp.app_command and command.app_command:
                if command.app_command.name in grp.app_command._children:
                    grp.app_command.remove_command(command.app_command.name)
                grp.app_command.add_command(command.app_command)
        else:
            super().add_command(command)

    def remove_command(self, name: str, /):
        """Remove command, cleaning up from nested slash groups if applicable."""
        cmd = super().remove_command(name)
        if cmd:
            nested_group = getattr(cmd, "_nested_in", None)
            if not nested_group and hasattr(cmd, "extras"):
                nested_group = cmd.extras.get("_nested_in")
            if not nested_group and hasattr(cmd, "callback"):
                nested_group = getattr(cmd.callback, "_nested_in", None)

            if nested_group:
                from base.managers.commands import _GROUPS
                grp = _GROUPS.get(nested_group)
                if grp and grp.app_command and cmd.app_command:
                    if cmd.app_command.name in grp.app_command._children:
                        grp.app_command.remove_command(cmd.app_command.name)
        return cmd

    async def on_command_error(self, ctx: Context, exception: CommandError) -> None:
        exception = getattr(exception, "original", exception)  # type: ignore
        if isinstance(exception, NotOwner):
            return

        elif isinstance(exception, MissingPermissions):
            return await ctx.warn(
                f"You're **missing** permission: `{', '.join(p for p in exception.missing_permissions)}`"  # type: ignore
            )

        elif isinstance(exception, CheckFailure):
            # Ignore silent check failures (is_owner, guild_only, predicate checks without custom msg)
            return

        elif isinstance(exception, CommandOnCooldown):
            return await ctx.cooldown(
                f"Please wait **{exception.retry_after:.2f} seconds** before using this command again."
            )  # type: ignore

        elif isinstance(exception, CheckFailure):
            if isinstance(exception, MissingPermissions):
                return await ctx.warn(
                    f"You're **missing** permission: `{', '.join(p for p in exception.missing_permissions)}`"  # type: ignore
                )

        elif isinstance(exception, CommandNotFound):
            # No-prefix users make every message look like a command attempt, so
            # an unknown command name is silently ignored instead of warned about.
            # Still allow custom aliases to resolve to a real command first.
            if ctx.guild is None:
                return

            alias = ctx.message.content[len(ctx.clean_prefix) :].split(" ")[0]
            check = await self.pool.fetchval(
                "SELECT command FROM aliases WHERE guild_id = %s AND alias = %s",
                ctx.guild.id,
                alias,
            )
            if check:
                ctx.message.content = ctx.message.content.replace(alias, check, 1)
                return await self.process_commands(ctx.message)

            return

        elif isinstance(exception, MissingRequiredArgument):
            return await ctx.send_help(ctx.command)

        elif isinstance(exception, BadArgument):
            return await ctx.warn(exception.args[0])

        elif isinstance(exception, UserInputError):
            return await ctx.send_help(ctx.command)

        return await ctx.warn(f"{exception}")

    async def on_message_edit(self, before, after):
        if before.content == after.content:
            return
        if after.author.bot:
            return

        ctx = await self.get_context(after)
        if ctx.valid:
            await self.invoke(ctx)

    async def log_lost_boost(self, member: Member):
        try:
            query = """
            INSERT INTO lost_boosters (guild_id, user_id, username, discriminator, lost_at)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE guild_id = VALUES(guild_id), username = VALUES(username), discriminator = VALUES(discriminator), lost_at = VALUES(lost_at);
            """
            await self.pool.execute(
                query,
                member.guild.id,
                member.id,
                member.name,
                member.discriminator,
                datetime.utcnow(),
            )  # type: ignore
            logger.info(f"Logged lost boost for {member.name}#{member.discriminator}")
        except Exception as e:
            logger.error(f"Error logging lost boost: {e}")

    async def on_message(self, message: Message) -> None:
        if message.author.bot:
            return

        if await self.is_blacklisted(message.author.id):
            return

        prefix = await self.get_prefix(message)
        if not prefix or not message.content.startswith(tuple(prefix)):
            return

        now = time.time()
        author_id = message.author.id

        self.message_cache[author_id] = [
            timestamp
            for timestamp in self.message_cache[author_id]
            if now - timestamp < self.cache_expiry_seconds
        ]
        self.message_cache[author_id].append(now)

        await self.process_commands(message)

    async def on_command(self, ctx) -> None:
        logger.info(
            f"{ctx.author} ({ctx.author.id}) executed {ctx.command} in {ctx.guild} ({ctx.guild.id})."
        )

    async def on_shard_ready(self, ctx) -> None:
        logger.debug(f"Shard {self.shard_id} has spawned.")

    def member_ratelimit(self, message: Message) -> Optional[int]:
        bucket = self.member_cooldown.get_bucket(message)
        return bucket.update_rate_limit()  # type: ignore

    def channel_ratelimit(self, message: Message) -> Optional[int]:
        bucket = self.channel_cooldown.get_bucket(message)
        return bucket.update_rate_limit()  # type: ignore

    async def process_commands(
        self: "Bot",
        message: Message,
    ):
        prefixes = await self.get_prefix(message)
        if message.guild is not None and prefixes and message.content.startswith(
            tuple(prefixes)
        ):
            mcd = self.member_ratelimit(message)
            ccd = self.channel_ratelimit(message)

            if mcd or ccd:
                return

            return await super().process_commands(message)
