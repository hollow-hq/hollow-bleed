"""Registry / routing / help-metadata tests (no network, no DB)."""

from base.managers.commands import validate_routing
from base.managers.commands.routing import (
    NESTED_COMMANDS,
    NESTED_GROUPS,
    nested_group_for,
)


def _load_moderation_cog():
    from cogs.moderation import moderation as mod_module

    return mod_module.Moderation


def test_routing_validates():
    # Must not raise; also asserts the 25-per-group Discord limit.
    validate_routing(None)


def test_moderation_group_within_limit():
    counts = {g: 0 for g in NESTED_GROUPS}
    for _cmd, grp in NESTED_COMMANDS.items():
        counts[grp] += 1
    assert counts["moderation"] <= 25
    # Every slash group stays within Discord's 25-subcommand limit.
    for group, count in counts.items():
        assert count <= 25, f"/{group} has {count} subcommands (limit 25)"


def test_all_mod_commands_are_routed():
    # Every top-level moderation command must live in the routing system
    # (prefix stays top-level, slash nests under its group) so the bot
    # cannot blow past Discord's 100 global top-level slots again.
    cog = _load_moderation_cog()
    unrouted = [
        c.name
        for c in cog.__cog_commands__
        if c.qualified_name == c.name and nested_group_for(c.name) is None
    ]
    assert not unrouted, f"mod commands missing from routing: {unrouted}"


def test_owner_commands_are_prefix_only():
    from cogs.owner.owner import Owner

    missing = [
        c.name
        for c in Owner.__cog_commands__
        if c.qualified_name == c.name and getattr(c, "with_app_command", True)
    ]
    assert not missing, f"owner commands with slash registered: {missing}"


def test_existing_commands_preserved():
    cog = _load_moderation_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    # Snapshot from the Hollow-Phi registry: these must still exist.
    for name in [
        "ban", "kick", "nuke", "forcenickname", "lockdown",
        "lockdown all", "lockdown ignore", "lockdown ignored",
        "lockdown role", "unlock", "unlock all", "purge",
        "purge links", "purge mentions", "purge humans", "purge user",
        "purge bots", "purge stickers", "purge attachments",
        "stripstaff", "imute", "iunmute", "reactionmute",
        "reactionunmute", "role", "role create", "role color",
        "role delete", "role humans", "role all", "role hoist",
        "role mentionable", "unban", "mute", "unmute", "channel",
        "channel nsfw", "channel create", "channel delete",
        "channel rename", "reveal", "hide",
    ]:
        assert name in by_qual, f"existing command missing: {name}"


def test_existing_aliases_preserved():
    cog = _load_moderation_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    assert "evict" in by_qual["ban"].aliases
    assert "rmute" in by_qual["reactionmute"].aliases
    assert "runmute" in by_qual["reactionunmute"].aliases
    assert "c" in by_qual["purge"].aliases


def test_new_commands_registered():
    cog = _load_moderation_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    for name in [
        "warn", "warnings", "history", "history view",
        "history remove", "history removeall", "case", "reason",
        "modstats", "modhistory", "proof", "proof view",
        "proof add", "proof remove", "proof list", "proof set",
        "notes", "notes add", "notes remove", "notes clear",
        "tempban", "softban", "hardban", "recentban",
        "unbanall", "timeout", "timeout list", "untimeout",
        "jail", "unjail", "jaillist", "temprole", "temprole list",
        "slowmode", "slowmode on", "slowmode off",
        "thread", "thread lock", "thread unlock",
        "thread add", "thread remove", "thread watch", "thread list",
        "topic", "clearinvites", "moveall", "newmembers",
        "rename", "permissions", "restrictcommand",
        "stickyrole", "forcenickname list",
        "purge after", "purge before", "purge between",
        "purge contains", "purge embeds", "purge files",
        "purge images", "purge reactions", "purge webhooks",
        "role add", "role remove", "role edit", "role icon",
        "role restore", "role bots", "role has",
    ]:
        assert name in by_qual, f"new command missing: {name}"


def test_no_duplicate_top_level_names_or_aliases():
    cog = _load_moderation_cog()
    top = [c for c in cog.__cog_commands__ if c.qualified_name == c.name]
    names = [c.name.lower() for c in top]
    assert len(names) == len(set(names)), "duplicate top-level command name"
    alias_owner = {}
    for c in top:
        for a in c.aliases:
            key = a.lower()
            assert key not in alias_owner, f"alias {a!r} on {c.name} duplicates {alias_owner[key]}"
            assert key not in names, f"alias {a!r} collides with command name"
            alias_owner[key] = c.name


def test_no_duplicate_qualified_names():
    cog = _load_moderation_cog()
    quals = [c.qualified_name for c in cog.__cog_commands__]
    assert len(quals) == len(set(quals)), "duplicate qualified command name"


def test_help_metadata_present():
    cog = _load_moderation_cog()
    for c in cog.__cog_commands__:
        assert c.description, f"{c.qualified_name} missing description"
        # Regular subcommands (via group.command) only carry description;
        # hybrid top-level commands carry both. Either is fine for help.
        assert ((c.help or "").strip() or (c.description or "").strip()), f"{c.qualified_name} missing help text"
        # example is stored in extras by our hybrid decorators, or in
        # __original_kwargs__ for regular subcommands.
        extras = getattr(c, "extras", {}) or {}
        original = getattr(c, "__original_kwargs__", {}) or {}
        assert ("example" in extras or "example" in original), f"{c.qualified_name} missing example"


def test_no_voicemaster_drag_duplicate():
    # Moderation must not define its own single-member drag: voicemaster
    # already owns `voicemaster drag`. Only moveall (bulk) lives here.
    cog = _load_moderation_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    assert "drag" not in by_qual
    assert "moveall" in by_qual


def test_rmute_runmute_not_duplicated():
    # Bleed rmute/runmute map to existing reactionmute/reactionunmute
    # aliases; no separate top-level commands may exist.
    cog = _load_moderation_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    assert "rmute" not in by_qual
    assert "runmute" not in by_qual
