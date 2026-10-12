from typing import Optional, List, Dict, Tuple
import logging

logger = logging.getLogger(__name__)

# group name -> (slash description, help text)
# NOTE: prefix commands stay top-level (`,warn`, `,tempban`, ...) while their
# slash counterparts are nested under these groups. Discord allows max 100
# top-level slash commands globally and max 25 subcommands per group, so the
# moderation surface is split into four groups instead of one.
NESTED_GROUPS: dict[str, tuple[str, str]] = {
    "moderation": ("Moderation commands.", "Ban, kick, mute and more."),
    "cases": ("Case and warning commands.", "Warn, review and track moderation cases."),
    "punish": ("Punishment commands.", "Temporary bans, jail, timeouts and mass unban."),
    "modtools": ("Moderation utilities.", "Slowmode, threads, invites, topics and more."),
    "fun": ("Fun commands.", "Rate, ship, roast and more."),
    "tools": ("Utility commands.", "Image tools, lookups and converters."),
    "miscellaneous": ("Miscellaneous commands.", "AFK, embeds, polls and more."),
    "server": ("Server commands.", "Welcome, prefixes, snipes, counters and more."),
    "music": ("Music player commands.", "Play and control music."),
    "information": ("Information commands.", "Look up users, servers and bot info."),
    # ``giveaway`` is declared as a real hybrid group inside ``cogs.engagement``
    # and owns its own subcommands, so this entry exists only to keep the group
    # name reserved / documented (and to satisfy validate_routing).
    "giveaway": ("Manage giveaways.", "Run and manage giveaways."),
}

# command name -> group name (slash path becomes /<group> <command>)
NESTED_COMMANDS: dict[str, str] = {
    # Core moderation commands (19, slash: /moderation ...)
    "ban": "moderation",
    "kick": "moderation",
    "unban": "moderation",
    "mute": "moderation",
    "unmute": "moderation",
    "imute": "moderation",
    "iunmute": "moderation",
    "reactionmute": "moderation",
    "reactionunmute": "moderation",
    "nuke": "moderation",
    "forcenickname": "moderation",
    "stripstaff": "moderation",
    "lockdown": "moderation",
    "unlock": "moderation",
    "purge": "moderation",
    "role": "moderation",
    "channel": "moderation",
    "reveal": "moderation",
    "hide": "moderation",

    # Case records (9, slash: /cases ...)
    "warn": "cases",
    "warnings": "cases",
    "history": "cases",
    "case": "cases",
    "reason": "cases",
    "modstats": "cases",
    "modhistory": "cases",
    "proof": "cases",
    "notes": "cases",

    # Punishments (12, slash: /punish ...)
    "tempban": "punish",
    "softban": "punish",
    "hardban": "punish",
    "recentban": "punish",
    "unbanall": "punish",
    "timeout": "punish",
    "untimeout": "punish",
    "jail": "punish",
    "unjail": "punish",
    "jaillist": "punish",
    "temprole": "punish",
    "raid": "punish",

    # Moderation utilities (18, slash: /modtools ...)
    "slowmode": "modtools",
    "revokefiles": "modtools",
    "topic": "modtools",
    "talk": "modtools",
    "unhide": "modtools",
    "moveall": "modtools",
    "clearinvites": "modtools",
    "newmembers": "modtools",
    "rename": "modtools",
    "permissions": "modtools",
    "setup": "modtools",
    "setupmute": "modtools",
    "restrictcommand": "modtools",
    "stickyrole": "modtools",
    "naughty": "modtools",
    "remind": "modtools",
    "reminders": "modtools",
    "thread": "modtools",

    # Music commands
    "play": "music",
    "skip": "music",
    "stop": "music",
    "volume": "music",

    # Information commands
    "userinfo": "information",
    "serverinfo": "information",

    # Server identity & stats (slash: /information ...).
    "avatar": "information",
    "banner": "information",
    "botinfo": "information",
    "serveravatar": "information",
    "serverbanner": "information",
    "guildicon": "information",
    "guildbanner": "information",
    "splash": "information",
    "membercount": "information",
    "members": "information",
    "roles": "information",
    "roleinfo": "information",
    "boosters": "information",
    "bots": "information",
    "invite": "information",
    "inviteinfo": "information",
    "banreason": "information",
    "bans": "information",

    # Lookup & converter utilities (17, slash: /tools ...).
    # NOTE: ``ping``/``uptime``/``status``/``channelinfo`` live here rather
    # than /information so that group keeps headroom under the 25-cap.
    "ping": "tools",
    "uptime": "tools",
    "status": "tools",
    "channelinfo": "tools",
    "hex": "tools",
    "invert": "tools",
    "rotate": "tools",
    "compress": "tools",
    "define": "tools",
    "emoji": "tools",
    "emotes": "tools",
    "github": "tools",
    "minecraft": "tools",
    "weather": "tools",
    "timezone": "tools",
    "seen": "tools",
    "sticker": "tools",

    # Fun singletons & flat groups (15, slash: /fun ...).
    # NOTE: ``birthday`` and ``chatbot`` stay top-level: they own subgroups,
    # which cannot nest two levels deep under /fun.
    "base64": "fun",
    "8ball": "fun",
    "bitches": "fun",
    "chatgpt": "fun",
    "diary": "fun",
    "dih": "fun",
    "dihr": "fun",
    "howautism": "fun",
    "howgay": "fun",
    "howlesbian": "fun",
    "howsimp": "fun",
    "imagine": "fun",
    "roast": "fun",
    "ship": "fun",
    "tts": "fun",

    # Miscellaneous singletons (9, slash: /miscellaneous ...).
    "afk": "miscellaneous",
    "createembed": "miscellaneous",
    "donate": "miscellaneous",
    "embedcode": "miscellaneous",
    "pin": "miscellaneous",
    "quickpoll": "miscellaneous",
    "suggest": "miscellaneous",
    "unpin": "miscellaneous",
    "urban": "miscellaneous",

    # Server management groups & utilities (15, slash: /server ...).
    # NOTE: ``prefix`` stays top-level: it owns the ``self``/``noprefix``
    # subgroups, which cannot nest two levels deep under /server.
    "welcome": "server",
    "alias": "server",
    "autorole": "server",
    "stickymessage": "server",
    "boosts": "server",
    "autoresponder": "server",
    "boosterrole": "server",
    "leaves": "server",
    "fakeperms": "server",
    "reactionsnipe": "server",
    "editsnipe": "server",
    "snipe": "server",
    "clearsnipes": "server",
    "counters": "server",
    "customize": "server",

    # Music player preset joins the existing /music group.
    "preset": "music",

    # Engagement: the ``giveaway`` hybrid group is declared inside
    # ``cogs.engagement`` and owns all of its own subcommands (start, end,
    # reroll, list, channel, entries and the edit / blacklist sub-groups), so no
    # routing entries are required here. ``highlight`` owns the ``ignore``
    # subgroup so it cannot nest; ``level`` alone would trade one top-level
    # slot for another, so both stay top-level.
}

# commands that must NOT be registered as slash at all (prefix only)
# NOTE: owner commands are prefix-only on purpose. They are bot-owner-only
# tools hidden from help, and each slash registration consumes one of
# Discord's 100 global top-level slots. Keeping them prefix-only frees ~13
# slots so feature cogs fit under the cap with headroom to spare.
TEXT_ONLY: dict[str, str] = {
    # "eval": "owner only",
    # ``level exempt`` uses ``Greedy`` (a prefix-only converter) for its channel
    # arguments, which cannot be represented as a slash command, so it is
    # registered as prefix-only.
    "level_exempt": "Greedy channel argument cannot be a slash command",
    "whitelist": "owner only",
    "portal": "owner only",
    "guilds": "owner only",
    "blacklist": "owner only",
    "restart": "owner only",
    "push": "owner only",
    "pull": "owner only",
    "eval": "owner only",
    "sql": "owner only",
    "websitesync": "owner only",
    "guild_blacklist": "owner only",
    "guild_unblacklist": "owner only",
    "debug": "owner only",
}

# prefix aliases for auto-created groups, e.g. ,mod -> moderation
GROUP_ALIASES: dict[str, list[str]] = {
    "moderation": ["mod"],
    "information": ["info"],
}


def nested_group_for(name: str) -> Optional[str]:
    """Return the nested group name for a command, or None if not nested."""
    return NESTED_COMMANDS.get(name.lower()) if name else None


def is_text_only(name: str) -> bool:
    """Return True if command is prefix-only."""
    return bool(name and name.lower() in TEXT_ONLY)


def text_only_reason(name: str) -> Optional[str]:
    """Return the reason why a command is prefix-only, or None."""
    return TEXT_ONLY.get(name.lower()) if name else None


def nested_group_aliases(group: str) -> List[str]:
    """Return the configured prefix aliases for a group."""
    return GROUP_ALIASES.get(group.lower(), [])


def truncate_description(desc: Optional[str], max_length: int = 100) -> str:
    """Truncate description safely within Discord limits."""
    if not desc:
        return "No description provided."
    clean = desc.strip().split("\n")[0].strip()
    if len(clean) > max_length:
        return clean[: max_length - 3].rstrip() + "..."
    return clean or "No description provided."


def validate_routing(bot=None) -> None:
    """Validate routing configuration at startup.

    Raises ValueError with a clear message if:
    - A command maps to an unknown group
    - A command is both nested and text-only
    - Slash command constraints are violated
    - Total top-level slash commands exceed 100
    Warns if:
    - A group has >= 20 subcommands (near the 25 limit)
    """
    # 1. Check commands mapping to unknown groups
    for cmd_name, grp_name in NESTED_COMMANDS.items():
        if grp_name not in NESTED_GROUPS:
            raise ValueError(
                f"Command '{cmd_name}' is routed to unknown group '{grp_name}'. "
                f"Available groups: {list(NESTED_GROUPS.keys())}"
            )

    # 2. Check commands that are both nested and text-only
    conflicts = set(NESTED_COMMANDS.keys()) & set(TEXT_ONLY.keys())
    if conflicts:
        raise ValueError(
            f"Commands cannot be both nested and text-only: {sorted(conflicts)}"
        )

    # 3. Validate group name constraints (lowercase, no spaces, <= 32 chars)
    for grp_name, (slash_desc, _) in NESTED_GROUPS.items():
        if not grp_name.islower() or " " in grp_name or len(grp_name) > 32:
            raise ValueError(
                f"Invalid group name '{grp_name}': must be lowercase, no spaces, max 32 chars."
            )
        if len(slash_desc) > 100:
            raise ValueError(
                f"Group '{grp_name}' description exceeds 100 chars ({len(slash_desc)} chars)."
            )

    # 4. Count subcommands per group and warn if near 25
    group_counts: Dict[str, int] = {grp: 0 for grp in NESTED_GROUPS}
    for cmd_name, grp_name in NESTED_COMMANDS.items():
        if not cmd_name.islower() or " " in cmd_name or len(cmd_name) > 32:
            raise ValueError(
                f"Invalid slash command name '{cmd_name}': must be lowercase, no spaces, max 32 chars."
            )
        group_counts[grp_name] += 1

    for grp_name, count in group_counts.items():
        if count > 25:
            raise ValueError(
                f"Group '{grp_name}' has {count} subcommands (Discord limit is 25). Split the group."
            )
        elif count >= 20:
            logger.warning(
                f"Group '{grp_name}' has {count}/25 subcommands. Consider splitting it soon."
            )

    # 5. Check total top-level slash commands if bot is provided
    if bot is not None:
        # Top-level slash slots: global groups + un-nested hybrid commands/groups
        top_level_names = set()
        for cmd in bot.tree.get_commands():
            top_level_names.add(cmd.name)

        if len(top_level_names) > 100:
            raise ValueError(
                f"Total top-level slash commands ({len(top_level_names)}) exceeds Discord limit of 100!"
            )
