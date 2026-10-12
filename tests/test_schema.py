"""Schema sanity: required tables exist in schema.sql and are idempotent."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "base" / "schema" / "schema.sql").read_text(encoding="utf-8")

REQUIRED = [
    "mod_cases", "mod_notes", "reminders", "temp_actions",
    "jail_config", "jail_state", "sticky_roles", "hardbans",
    "restrict_commands", "mute_config",
    # Pre-existing tables that moderation depends on.
    "forcenick", "lockdown", "warnings",
]


def test_required_tables_present():
    for table in REQUIRED:
        pattern = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(table)}\b", re.IGNORECASE)
        assert pattern.search(SCHEMA), f"missing table {table}"


def test_mod_cases_has_unique_case_key():
    assert "uniq_mod_case" in SCHEMA


def test_schema_uses_if_not_exists():
    # Ignore prose comments (e.g. "CREATE TABLE IF NOT EXISTS above ...").
    statements = [
        line for line in SCHEMA.splitlines()
        if not line.strip().startswith("--") and "CREATE TABLE" in line.upper()
    ]
    assert statements, "no CREATE TABLE statements found"
    for line in statements:
        assert "IF NOT EXISTS" in line.upper(), f"missing IF NOT EXISTS: {line}"


def test_schema_ends_with_newline():
    assert SCHEMA.endswith("\n")
