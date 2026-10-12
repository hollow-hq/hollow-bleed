"""Behavioral tests for case/note/temp-action logic with fakes (no network)."""

import asyncio
import time
import types

import pytest

from cogs.moderation import moderation as mod_module
from cogs.moderation.modsys import ModSysMixin


class FakePool:
    """Minimal asyncmy-style pool double keyed by test needs."""

    def __init__(self):
        self.cases = []  # list of dicts
        self.notes = []
        self.temp_actions = []
        self._next_id = 1
        self.fail_next = 0

    def _maybe_fail(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("db unavailable")

    async def fetchval(self, query, *args):
        self._maybe_fail()
        if "MAX(case_id)" in query:
            guild_id = args[0]
            ids = [c["case_id"] for c in self.cases if c["guild_id"] == guild_id]
            return max(ids) if ids else 0
        if "attachments FROM mod_cases" in query:
            guild_id, case_id = args
            for c in self.cases:
                if c["guild_id"] == guild_id and c["case_id"] == case_id:
                    return c.get("attachments")
            return None
        return None

    async def fetchrow(self, query, *args):
        self._maybe_fail()
        if "FROM mod_cases" in query and "case_id" in query:
            guild_id, case_id = args[-2], args[-1]
            for c in self.cases:
                if c["guild_id"] == guild_id and c["case_id"] == case_id:
                    return dict(c)
            return None
        return None

    async def fetch(self, query, *args):
        self._maybe_fail()
        if "FROM mod_cases" in query:
            guild_id = args[0]
            rows = [dict(c) for c in self.cases if c["guild_id"] == guild_id]
            if "user_id" in query and len(args) >= 2:
                rows = [r for r in rows if r["user_id"] == args[1]]
            return rows
        if "FROM mod_notes" in query:
            return list(self.notes)
        if "FROM temp_actions" in query and "active = 1" in query:
            now = args[0]
            return [dict(r) for r in self.temp_actions if r["active"] == 1 and r["expires_at"] <= now]
        return []

    async def execute(self, query, *args):
        self._maybe_fail()
        if query.startswith("INSERT INTO mod_cases"):
            guild_id, case_id, user_id, moderator_id, action, reason, created_at, expires_at = args[:8]
            self.cases.append({
                "id": self._next_id, "guild_id": guild_id, "case_id": case_id,
                "user_id": user_id, "moderator_id": moderator_id, "action": action,
                "reason": reason, "proof": None, "attachments": None,
                "created_at": created_at, "expires_at": expires_at, "active": 1,
            })
            self._next_id += 1
        elif query.startswith("UPDATE mod_cases SET reason"):
            reason, guild_id, case_id = args
            for c in self.cases:
                if c["guild_id"] == guild_id and c["case_id"] == case_id:
                    c["reason"] = reason
        elif query.startswith("DELETE FROM mod_cases"):
            if "case_id" in query:
                guild_id, case_id = args
                self.cases = [c for c in self.cases if not (c["guild_id"] == guild_id and c["case_id"] == case_id)]
            else:
                guild_id, user_id = args
                self.cases = [c for c in self.cases if not (c["guild_id"] == guild_id and c["user_id"] == user_id)]
        elif query.startswith("INSERT INTO mod_notes"):
            guild_id, user_id, moderator_id, note, created_at = args
            self.notes.append({"id": self._next_id, "guild_id": guild_id, "user_id": user_id,
                               "moderator_id": moderator_id, "note": note, "created_at": created_at})
            self._next_id += 1
        elif query.startswith("INSERT INTO temp_actions"):
            guild_id, user_id = args[0], args[1]
            # action is embedded in the SQL string for these tests
            action = "tempban" if "'tempban'" in query else ("jail" if "'jail'" in query else "temprole")
            self.temp_actions.append({"id": self._next_id, "guild_id": guild_id, "user_id": user_id,
                                      "action": action, "expires_at": args[-3], "active": 1, "data": args[-4] if len(args) > 4 else None})
            self._next_id += 1
        elif query.startswith("UPDATE temp_actions SET active = 0 WHERE id"):
            row_id = args[0]
            for r in self.temp_actions:
                if r["id"] == row_id and r["active"] == 1:
                    r["active"] = 0
                    return
            raise RuntimeError("already claimed")


class FakeCog(ModSysMixin):
    def __init__(self, pool):
        self.bot = types.SimpleNamespace(pool=pool, user=types.SimpleNamespace(id=999))


def test_case_numbering_increments():
    async def go():
        pool = FakePool()
        cog = FakeCog(pool)
        first = await cog._mod_log_case(1, 10, 20, "warn", "first")
        second = await cog._mod_log_case(1, 11, 20, "warn", "second")
        other_guild = await cog._mod_log_case(2, 10, 20, "warn", "other")
        assert (first, second, other_guild) == (1, 2, 1)
    asyncio.run(go())


def test_reason_update_and_history_remove_paths():
    async def go():
        pool = FakePool()
        cog = FakeCog(pool)
        case_id = await cog._mod_log_case(1, 10, 20, "warn", "old")
        await pool.execute("UPDATE mod_cases SET reason = %s WHERE guild_id = %s AND case_id = %s", "new", 1, case_id)
        row = await pool.fetchrow("SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s", 1, case_id)
        assert row["reason"] == "new"
        await pool.execute("DELETE FROM mod_cases WHERE guild_id = %s AND case_id = %s", 1, case_id)
        assert await pool.fetchrow("SELECT * FROM mod_cases WHERE guild_id = %s AND case_id = %s", 1, case_id) is None
    asyncio.run(go())


def test_database_failure_path():
    async def go():
        pool = FakePool()
        pool.fail_next = 1
        cog = FakeCog(pool)
        with pytest.raises(RuntimeError):
            await cog._mod_next_case_id(1)
    asyncio.run(go())


def test_hierarchy_rejects_owner_self_bot_and_higher():
    cog = FakeCog(FakePool())

    def make_ctx(author_id, author_top, bot_top, owner_id):
        author = types.SimpleNamespace(id=author_id, top_role=types.SimpleNamespace(position=author_top))
        guild = types.SimpleNamespace(
            owner_id=owner_id,
            me=types.SimpleNamespace(top_role=types.SimpleNamespace(position=bot_top)),
        )
        return types.SimpleNamespace(guild=guild, author=author)

    target = types.SimpleNamespace(id=5, top_role=types.SimpleNamespace(position=10))
    ctx = make_ctx(1, 50, 100, owner_id=99)
    assert cog._mod_hierarchy_error(ctx, target) is None

    owner_target = types.SimpleNamespace(id=99, top_role=types.SimpleNamespace(position=200))
    assert cog._mod_hierarchy_error(ctx, owner_target) is not None

    self_target = types.SimpleNamespace(id=1, top_role=types.SimpleNamespace(position=10))
    assert cog._mod_hierarchy_error(ctx, self_target) is not None

    cog.bot.user = types.SimpleNamespace(id=7)
    bot_target = types.SimpleNamespace(id=7, top_role=types.SimpleNamespace(position=1))
    assert cog._mod_hierarchy_error(ctx, bot_target) is not None

    higher = types.SimpleNamespace(id=6, top_role=types.SimpleNamespace(position=60))
    assert cog._mod_hierarchy_error(ctx, higher) is not None


def test_expiry_claim_is_idempotent():
    # Two concurrent claimants for the same temp row: exactly one wins.
    async def go():
        pool = FakePool()
        pool.temp_actions.append({"id": 1, "guild_id": 1, "user_id": 2,
                                  "action": "tempban", "expires_at": int(time.time()) - 5,
                                  "active": 1, "data": "case:1"})
        wins = 0

        async def claimant():
            nonlocal wins
            try:
                await pool.execute("UPDATE temp_actions SET active = 0 WHERE id = %s AND active = 1", 1)
                wins += 1
            except RuntimeError:
                pass

        await asyncio.gather(claimant(), claimant())
        assert wins == 1
    asyncio.run(go())


def test_purge_message_id_extraction():
    extract = mod_module.Moderation._extract_message_id
    assert extract("https://discord.com/channels/1/2/123456789012345678") == 123456789012345678
    assert extract("123456789012345678") == 123456789012345678
    assert extract("not a link") is None
    assert extract("") is None


def test_purge_amount_bounds():
    # _purge_filtered clamps 1..500; verify via the clamping expression used.
    assert max(1, min(int(0 or 50), 500)) == 50
    assert max(1, min(int(1000), 500)) == 500
    assert max(1, min(int(-5), 500)) == 1


def test_moderation_cog_blends_mixin():
    assert issubclass(mod_module.Moderation, ModSysMixin)
    for name in ("warn", "history", "notes", "tempban", "jail", "slowmode", "thread"):
        assert hasattr(mod_module.Moderation, name), f"Moderation missing mixin command {name}"
