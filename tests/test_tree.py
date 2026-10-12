"""Full slash-tree test: load every cog like production and enforce limits.

Regression test for the `CommandLimitReached: maximum number of slash
commands exceeded 100 globally` crash and the `'X' is too nested` crash:
Discord allows max 100 top-level slash commands globally, max 25
subcommands per group, and only one subgroup level.
"""

import asyncio

import pytest


class FakePool:
    async def fetch(self, *args, **kwargs):
        return []

    async def fetchrow(self, *args, **kwargs):
        return None

    async def fetchval(self, *args, **kwargs):
        return None

    async def execute(self, *args, **kwargs):
        return None

    async def fetchmany(self, *args, **kwargs):
        return []

    async def executemany(self, *args, **kwargs):
        return None


def _walk(command, depth=0):
    children = getattr(command, "_children", {}) or {}
    yield depth, command
    for child in children.values():
        yield from _walk(child, depth + 1)


def test_full_tree_within_discord_limits():
    try:
        from base.hollow import Bot
        from base.managers.commands import _AUTO_GROUPS, _GROUPS, validate_routing
    except ModuleNotFoundError as exc:
        pytest.skip(f"missing dependency: {exc}")

    async def go():
        bot = Bot()
        bot.pool = FakePool()
        await bot.load_modules()
        for grp_name in _AUTO_GROUPS:
            grp = _GROUPS.get(grp_name)
            if grp and grp.name not in bot.all_commands:
                bot.add_command(grp)
        return bot

    try:
        bot = asyncio.run(go())
    except ModuleNotFoundError as exc:
        pytest.skip(f"missing cog dependency: {exc}")

    top = bot.tree.get_commands()
    assert len(top) <= 100 - 5, (
        f"{len(top)} top-level slash commands; keep >= 5 slots of headroom"
    )
    for command in top:
        levels = list(_walk(command))
        depth = max(d for d, _ in levels)
        assert depth <= 2, f"/{command.name} nested too deep ({depth})"
        children = [n for d, n in ((d, getattr(c, "name", "?")) for d, c in levels) if d == 1]
        assert len(children) <= 25, f"/{command.name} has {len(children)} children"
    validate_routing(bot)

    # Every singleton command must use the routing system (nested slash group)
    # or be explicitly prefix-only. Groups are organized by definition, so
    # only bare commands are checked. Groups that own subgroups (antiraid,
    # giveaway, birthday, ...) must stay top-level: Discord forbids two
    # subgroup levels.
    from base.managers.commands.routing import (
        NESTED_GROUPS,
        is_text_only,
        nested_group_for,
    )

    stragglers = []
    for command in bot.commands:
        if list(getattr(command, "commands", []) or []):
            continue
        if command.name in NESTED_GROUPS:
            continue  # auto-created slash group shells
        if command.name == "help":
            continue  # discord.py built-in
        if nested_group_for(command.name) is None and not is_text_only(command.name):
            stragglers.append(command.qualified_name)
    assert not stragglers, f"singleton commands outside routing: {stragglers}"
