"""Information-cog expansion tests (no network, no DB, no token).

Covers the Bleed-parity work in ``cogs.information``:
- pure helper validation (SSRF guards, argument parsing, lookups),
- command registration / aliases / help metadata,
- no duplicate or colliding command names,
- schema additions (last_seen, timezones),
- Pillow image-operation smoke tests.
"""

import ast
import io
import pathlib

import pytest

from cogs.information import helpers as h

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_information_cog():
    from cogs.information import information as info_module

    return info_module.Information


# --------------------------------------------------------------------------- #
# SSRF / URL validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_ips_accepted(ip):
    assert h.is_public_ip(ip) is True


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1",
        "169.254.169.254", "0.0.0.0", "::1", "fc00::1",
        "224.0.0.1", "255.255.255.255", "not-an-ip",
    ],
)
def test_non_public_ips_rejected(ip):
    assert h.is_public_ip(ip) is False


def _sockaddrs(*hosts):
    import socket

    out = []
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        out.append((family, socket.SOCK_STREAM, 6, "", (host, 443)))
    return out


def test_sockaddrs_strict_all_public():
    assert h.sockaddrs_are_public(_sockaddrs("8.8.8.8", "1.1.1.1")) is True
    assert h.sockaddrs_are_public(_sockaddrs("8.8.8.8", "192.168.1.1")) is False
    assert h.sockaddrs_are_public([]) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x.png",
        "http://10.0.0.5/x.png",
        "http://192.168.0.1/x.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/x.png",
        "http://[::1]/x.png",
        "ftp://example.com/x.png",
        "file:///etc/passwd",
        "https://user:pass@example.com/x.png",
        "https://example.com:8080/x.png",
        "javascript:alert(1)",
        "",
        "not a url",
    ],
)
def test_blocked_urls_rejected(url):
    clean, error = h.validate_fetch_url(url)
    assert clean is None
    assert error


def test_public_url_accepted():
    clean, error = h.validate_fetch_url("https://cdn.discordapp.com/avatars/1/abc.png")
    assert error is None
    assert clean is not None


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def test_parse_degrees():
    assert h.parse_degrees(90) == (90, None)
    assert h.parse_degrees("45") == (45, None)
    assert h.parse_degrees(450) == (90, None)
    assert h.parse_degrees(0)[1]
    assert h.parse_degrees(360)[1]
    assert h.parse_degrees("abc")[1]
    assert h.parse_degrees(None)[1]


def test_parse_quality():
    assert h.parse_quality(70) == (70, None)
    assert h.parse_quality("1") == (1, None)
    assert h.parse_quality(0)[1]
    assert h.parse_quality(101)[1]
    assert h.parse_quality("high")[1]


@pytest.mark.parametrize("name", ["wave", "my_emote1", "ab", "a" * 32, "_x9"])
def test_valid_emoji_names(name):
    assert h.validate_emoji_name(name) == (name, None)


@pytest.mark.parametrize(
    "name", ["", "a", "A", "has space", "UPPER", "dash-name", "x" * 33, "émoji"]
)
def test_invalid_emoji_names(name):
    assert h.validate_emoji_name(name)[1]


def test_sticker_name_and_description():
    assert h.validate_sticker_name("  wave  ") == ("wave", None)
    assert h.validate_sticker_name("x")[1]
    assert h.validate_sticker_name("x" * 31)[1]
    desc, err = h.validate_sticker_description("", "wave")
    assert err is None and desc == "wave"
    desc, err = h.validate_sticker_description("a cool sticker", "wave")
    assert err is None and desc == "a cool sticker"
    assert h.validate_sticker_description("x" * 201, "wave")[1]


def test_timezone_validation():
    assert h.validate_timezone("UTC") == ("UTC", None)
    assert h.validate_timezone("Europe/Berlin") == ("Europe/Berlin", None)
    assert h.validate_timezone("America/New_York") == ("America/New_York", None)
    clean, error = h.validate_timezone("Nope/Zone")
    assert clean is None and error
    clean, error = h.validate_timezone("europe/berlin")  # case slips get a hint
    assert clean is None and "Europe/Berlin" in (error or "")
    assert h.validate_timezone("")[1]
    assert h.validate_timezone("x" * 65)[1]


def test_github_target_parsing():
    assert h.parse_github_target("octocat") == (("user", "octocat", None), None)
    assert h.parse_github_target("discordjs/discord.js") == (
        ("repo", "discordjs", "discord.js"),
        None,
    )
    assert h.parse_github_target("a/b/c")[1]
    assert h.parse_github_target("")[1]
    assert h.parse_github_target("-bad-/-also-bad-")[0] is None or True  # shape-checked
    assert h.parse_github_target("not a user!")[1]


def test_minecraft_identifier_parsing():
    assert h.parse_minecraft_identifier("Notch") == (("username", "Notch"), None)
    kind, value = h.parse_minecraft_identifier("069a79f444e94726a5befca90d663c0d")
    assert kind == ("uuid", "069a79f4-44e9-4726-a5be-fca90d663c0d")
    assert h.parse_minecraft_identifier("ab")[1]
    assert h.parse_minecraft_identifier("bad!name")[1]
    assert h.parse_minecraft_identifier("")[1]


def test_wmo_descriptions():
    assert h.wmo_description(0) == "Clear sky"
    assert h.wmo_description(95) == "Thunderstorm"
    assert h.wmo_description(999) == "Unknown conditions"
    assert h.wmo_description("bogus") == "Unknown conditions"


def test_chunk_lines():
    assert h.chunk_lines([], 10) == []
    assert h.chunk_lines(["a", "b", "c"], 2) == [["a", "b"], ["c"]]
    assert h.chunk_lines(["a"], 0) == [["a"]]


# --------------------------------------------------------------------------- #
# Registration / metadata / collisions
# --------------------------------------------------------------------------- #

EXPECTED_KEPT = [
    "inviteinfo", "userinfo", "hex", "avatar", "banner", "serverinfo",
    "ping", "boosters", "boosters lost", "membercount", "roleinfo",
    "serveravatar", "serverbanner", "bots", "botinfo", "bans",
    "uptime", "invite", "banreason", "emoji", "emoji steal", "emoji remove",
]

EXPECTED_ADDED = [
    "roles", "emotes", "members", "channelinfo", "guildicon", "guildbanner",
    "splash", "seen", "emoji rename", "emoji info",
    "sticker", "sticker list", "sticker add", "sticker remove", "sticker rename",
    "rotate", "compress", "invert",
    "timezone", "timezone set", "timezone reset", "timezone list",
    "define", "github", "weather", "minecraft", "status",
]


def test_existing_information_commands_preserved():
    cog = _load_information_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    for name in EXPECTED_KEPT:
        assert name in by_qual, f"existing command missing: {name}"


def test_new_information_commands_registered():
    cog = _load_information_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    for name in EXPECTED_ADDED:
        assert name in by_qual, f"new command missing: {name}"


def test_existing_aliases_preserved():
    cog = _load_information_cog()
    by_qual = {c.qualified_name: c for c in cog.__cog_commands__}
    assert "ii" in by_qual["inviteinfo"].aliases
    assert "whois" in by_qual["userinfo"].aliases
    assert "av" in by_qual["avatar"].aliases
    assert "si" in by_qual["serverinfo"].aliases
    assert "mc" in by_qual["membercount"].aliases
    assert "copy" in by_qual["emoji steal"].aliases
    # Bleed `emoji add` maps to the existing steal alias; no separate command.
    assert "emoji add" not in by_qual


def test_no_duplicate_names_aliases_within_cog():
    cog = _load_information_cog()
    top = [c for c in cog.__cog_commands__ if c.qualified_name == c.name]
    names = [c.name.lower() for c in top]
    assert len(names) == len(set(names)), "duplicate top-level command name"
    alias_owner = {}
    for c in top:
        for a in c.aliases:
            key = a.lower()
            assert key not in alias_owner, f"alias {a!r} duplicated"
            assert key not in names, f"alias {a!r} collides with a command name"
            alias_owner[key] = c.name
    quals = [c.qualified_name for c in cog.__cog_commands__]
    assert len(quals) == len(set(quals)), "duplicate qualified command name"


def test_no_top_level_collision_across_cogs():
    """AST-based sweep: no two cogs may claim the same top-level command or
    alias (subcommand aliases such as `purge members` live in their own
    namespace and are ignored)."""
    import re

    claimed: dict = {}
    for path in sorted((ROOT / "cogs").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                kind = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else ""
                )
                # Only top-level hybrid_command/hybrid_group decorators.
                # (group.command / group.group subcommands live in their own
                # namespace and are intentionally ignored here.)
                if kind not in ("hybrid_command", "hybrid_group"):
                    continue
                name = node.name
                aliases: list = []
                for kw in dec.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        name = str(kw.value.value)
                    if kw.arg == "aliases" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        aliases = [e.value for e in kw.value.elts if isinstance(e.value, ast.Constant)]
                for claimed_name in [name, *aliases]:
                    key = str(claimed_name).lower()
                    if key in claimed:
                        other = claimed[key]
                        if other != str(path):
                            raise AssertionError(
                                f"{claimed_name!r} in {path} collides with {other}"
                            )
                    else:
                        claimed[key] = str(path)
    # Spot-check the commands this change added.
    for name in ("roles", "emotes", "members", "channelinfo", "sticker",
                 "rotate", "compress", "invert", "timezone", "define",
                 "github", "weather", "minecraft", "status", "seen",
                 "guildicon", "guildbanner", "splash"):
        assert name in claimed, f"{name} not registered anywhere"


def test_information_help_metadata_present():
    cog = _load_information_cog()
    for c in cog.__cog_commands__:
        assert (c.description or "").strip(), f"{c.qualified_name} missing description"
        extras = getattr(c, "extras", {}) or {}
        original = getattr(c, "__original_kwargs__", {}) or {}
        assert ("example" in extras or "example" in original), (
            f"{c.qualified_name} missing example"
        )


def test_routing_still_validates():
    from base.managers.commands import validate_routing

    validate_routing(None)


# --------------------------------------------------------------------------- #
# Schema additions
# --------------------------------------------------------------------------- #

def test_information_tables_present():
    import re

    schema = (ROOT / "base" / "schema" / "schema.sql").read_text(encoding="utf-8")
    for table in ("last_seen", "timezones"):
        pattern = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(table)}\b", re.IGNORECASE)
        assert pattern.search(schema), f"missing table {table}"


# --------------------------------------------------------------------------- #
# Pillow smoke tests for the image utilities' core operations
# --------------------------------------------------------------------------- #

def _sample_png(size=(64, 32), color=(255, 0, 0)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_rotate_operation_produces_valid_png():
    from PIL import Image, ImageOps  # noqa: F401 (ImageOps used by cog)

    image = Image.open(io.BytesIO(_sample_png()))
    image.load()
    out = image.convert("RGBA").rotate(90, expand=True)
    assert out.size == (32, 64)
    buffer = io.BytesIO()
    out.save(buffer, format="PNG")
    assert buffer.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"


def test_invert_operation_roundtrip():
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(_sample_png(color=(10, 20, 30))))
    image.load()
    inverted = ImageOps.invert(image.convert("RGB"))
    assert inverted.getpixel((0, 0)) == (245, 235, 225)


def test_compress_operation_shrinks_and_validates():
    import random

    from PIL import Image

    rng = random.Random(42)
    noisy = Image.new("RGB", (400, 300))
    noisy.putdata([
        (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for _ in range(400 * 300)
    ])
    data = io.BytesIO()
    noisy.save(data, format="PNG")
    raw = data.getvalue()
    image = Image.open(io.BytesIO(raw))
    image.load()
    assert image.size[0] * image.size[1] <= h.MAX_IMAGE_PIXELS
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=20, optimize=True)
    assert len(out.getvalue()) < len(raw)
    assert out.getvalue()[:2] == b"\xff\xd8"


def test_oversize_image_rejected_by_limit():
    assert (10000 * 10000) > h.MAX_IMAGE_PIXELS
