"""Recursive variable + conditional scripting engine.

This is the single parser used by the ticket module (and anything else that
registers providers).  It deliberately keeps the *existing* bleed syntax
intact:

    {embed}$v{title: ...}$v{description: ...}$v{field: Name && Value && inline}

and only replaces ``{...}`` tokens with values.  Nothing is invented: if a
token cannot be resolved it is left exactly as written so the rest of the
pipeline (``EmbedBuilder.get_parts``) behaves as before.

Resolution order
----------------
1. ``{if}`` / ``{elseif}`` / ``{else}`` / ``{/if}`` blocks are resolved first
   (outermost first, no nesting).  Conditions may contain variables.
2. Variable tokens are resolved innermost-out, so ``{ticket.claimer.mention}``
   is matched whole rather than being shadowed by ``{ticket.claimer}``.
   Providers are asked longest-first and the leftover text is never consumed,
   which is what makes the prefix case safe.

Variable sources (``VAR_SOURCES``) are callables ``(value, ctx) -> str|None``
returning ``None`` for "not mine".  The bundled providers live in
``cogs.tickets.variables`` and are registered when the tickets cog loads; this
module never imports the ticket module, so it stays a leaf.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "ScriptContext",
    "VAR_SOURCES",
    "PROVIDERS",
    "register_var_source",
    "clear_var_sources",
    "register_ticket_providers",
    "register_provider",
    "render_script",
    "resolve_value",
    "humanize_seconds",
]

# A provider resolves a ``{value}`` against a runtime context.
#   value  -> the text inside the braces, e.g. ``ticket.claimer.mention``
#   ctx    -> ScriptContext
#   return -> replacement string, or None when it does not own the token.
VarSource = Callable[[str, "ScriptContext"], Optional[str]]

VAR_SOURCES: List[VarSource] = []
# Map of extension table -> resolver data, filled by the ticket module so that
# ``ScriptContext.get_extension("ticket")`` works.
PROVIDERS: Dict[str, dict] = {}

_IF_OPEN = re.compile(r"\{if\s+([^{}]*)\}")
_IF_BRANCH = re.compile(r"\{(elseif|else|/if)\b([^{}]*)\}")
_TOKEN = re.compile(r"\{([^{}]+)\}")


class ScriptContext:
    """Runtime information handed to variable providers.

    Every field is optional: the ticket cog fills what it needs and the plain
    ``{user}`` / ``{guild}`` fallback keeps working elsewhere.
    """

    __slots__ = ("bot", "guild", "author", "channel", "message", "ticket", "extra")

    def __init__(
        self,
        bot: Any = None,
        guild: Any = None,
        author: Any = None,
        channel: Any = None,
        message: Any = None,
        ticket: Any = None,
        **extra: Any,
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.author = author
        self.channel = channel
        self.message = message
        # ``ticket`` is the raw DB row plus a few computed helpers; providers
        # handle both dicts and plain objects.
        self.ticket = ticket
        self.extra = extra

    def get_extension(self, table: str) -> dict:
        data = PROVIDERS.get(table)
        return data if isinstance(data, dict) else {}

    @property
    def user(self) -> Any:
        return self.author


def register_var_source(fn: VarSource) -> None:
    VAR_SOURCES.append(fn)


def clear_var_sources() -> None:
    VAR_SOURCES.clear()


def register_provider(table: str, data: Optional[dict]) -> None:
    """Register an extension table (used by the ticket module)."""
    if data is None:
        PROVIDERS.pop(table, None)
    else:
        PROVIDERS[table] = data


# ---------------------------------------------------------------------------
# Small formatting helpers shared by providers
# ---------------------------------------------------------------------------


def humanize_seconds(seconds: Any) -> str:
    """``4500 -> "1h15m"`` / ``0 -> "0s"``.  Used by every ``.human`` variant."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "N/A"
    if total < 0:
        total = 0
    if total == 0:
        return "0s"
    units = (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
    out = ""
    for suffix, size in units:
        if total >= size:
            value, total = divmod(total, size)
            out += f"{value}{suffix}"
    return out or "0s"


def _value(ctx: ScriptContext, value: str) -> Optional[str]:
    """Resolve a single ``{...}`` body, or None when unknown."""
    for source in VAR_SOURCES:
        try:
            result = source(value, ctx)
        except Exception:
            # A provider must never break rendering; try the next one.
            continue
        if result is not None:
            return str(result)
    return None


def resolve_value(value: str, ctx: ScriptContext) -> Optional[str]:
    """Public single-token resolver (used by tests/other cogs)."""
    return _value(ctx, value)


# ---------------------------------------------------------------------------
# Conditionals
# ---------------------------------------------------------------------------

_OP_RE = re.compile(r"^(.*?)\s*(==|!=)\s*(.*)$", re.DOTALL)


def _truthy(raw: Optional[str]) -> bool:
    if raw is None:
        return False
    text = str(raw).strip().lower()
    return text not in ("", "0", "false", "none", "n/a", "null", "off")


def _eval_condition(condition: str, ctx: ScriptContext) -> bool:
    condition = condition.strip()
    match = _OP_RE.match(condition)

    if match:
        left_raw, op, right_raw = match.group(1), match.group(2), match.group(3)
        left = _resolve_fragment(left_raw, ctx)
        right = _resolve_fragment(right_raw, ctx)
        left_cmp = left.strip().strip('"').strip("'")
        right_cmp = right.strip().strip('"').strip("'")
        equal = left_cmp == right_cmp
        return equal if op == "==" else not equal

    return _truthy(_resolve_fragment(condition, ctx))


def _resolve_fragment(fragment: str, ctx: ScriptContext) -> str:
    """Resolve every token inside a fragment, leaving unknown ones alone."""

    def _sub(match: re.Match) -> str:
        resolved = _value(ctx, match.group(1))
        return resolved if resolved is not None else match.group(0)

    return _TOKEN.sub(_sub, fragment)


def _resolve_conditionals(text: str, ctx: ScriptContext) -> str:
    """Resolve one ``{if}`` block at a time by matching its own ``{/if}``.

    Brace counting (rather than a naive first-match regex) is what makes the
    ``{elseif}`` / ``{else}`` splitting correct when the bodies contain literal
    braces.  Nested ``{if}`` is intentionally unsupported.
    """
    while True:
        open_match = _IF_OPEN.search(text)
        if not open_match:
            return text

        cond = open_match.group(1)
        pos = open_match.end()
        depth = 0
        branches: List[tuple] = []  # (kind, condition, start)
        branches.append(("if", cond, pos))
        close_start = close_end = None

        while pos < len(text):
            branch = _IF_BRANCH.search(text, pos)
            if not branch:
                break
            kind, extra = branch.group(1), branch.group(2).strip()
            if kind == "/if":
                if depth == 0:
                    close_start, close_end = branch.start(), branch.end()
                else:
                    depth -= 1
            else:
                if depth == 0:
                    branches.append((kind, extra, branch.end()))
                else:
                    depth += 1
            pos = branch.end()

        if close_start is None:
            # Malformed block: leave it untouched instead of eating the text.
            return text

        chosen: Optional[str] = None
        for i, (kind, branch_cond, start) in enumerate(branches):
            end = branches[i + 1][2] if i + 1 < len(branches) else close_start
            segment = text[start:end]
            if kind == "else" or _eval_condition(branch_cond, ctx):
                chosen = segment
                break
        if chosen is None:
            chosen = ""

        text = text[: open_match.start()] + chosen + text[close_end:]


# ---------------------------------------------------------------------------
# Variable tokens
# ---------------------------------------------------------------------------


def _resolve_tokens(text: str, ctx: ScriptContext, depth: int = 0) -> str:
    if depth > 6:
        return text

    def _sub(match: re.Match) -> str:
        resolved = _value(ctx, match.group(1))
        if resolved is None:
            return match.group(0)
        return resolved

    resolved = _TOKEN.sub(_sub, text)
    if resolved != text and "{" in resolved:
        # A value itself contained tokens (e.g. a template rendered into a
        # form answer) - resolve once more, bounded by ``depth``.
        return _resolve_tokens(resolved, ctx, depth + 1)
    return resolved


# ---------------------------------------------------------------------------
# Public entrypoint (called by EmbedBuilder.to_object)
# ---------------------------------------------------------------------------


def render_script(text: Any, context: Any = None) -> Any:
    """Resolve conditionals + variables in ``text``.

    ``context`` may be a :class:`ScriptContext`, a ``dict`` with optional
    ``ticket``/``author``/``guild`` keys, or ``None`` (globals only).
    Unknown tokens are returned untouched.
    """
    if text is None or not isinstance(text, str):
        return text

    ctx = _coerce_context(context)
    text = _resolve_conditionals(text, ctx)
    text = _resolve_tokens(text, ctx)
    return text


def _coerce_context(context: Any) -> ScriptContext:
    if isinstance(context, ScriptContext):
        return context
    if isinstance(context, dict):
        data = dict(context)
        return ScriptContext(
            bot=data.pop("bot", None),
            guild=data.pop("guild", None),
            author=data.pop("author", None) or data.pop("user", None),
            channel=data.pop("channel", None),
            message=data.pop("message", None),
            ticket=data.pop("ticket", None),
            **data,
        )
    if context is None:
        return ScriptContext()
    if hasattr(context, "ticket") or hasattr(context, "author"):
        return ScriptContext(
            bot=getattr(context, "bot", None),
            guild=getattr(context, "guild", None),
            author=getattr(context, "author", None),
            channel=getattr(context, "channel", None),
            message=getattr(context, "message", None),
            ticket=getattr(context, "ticket", None),
        )
    return ScriptContext()


# ---------------------------------------------------------------------------
# Global {user} / {guild} style fallback (mirrors EmbedBuilder.embed_replacement
# but is token driven, so it also runs inside conditionals)
# ---------------------------------------------------------------------------

try:  # discord is optional so this module imports in isolation
    import discord  # type: ignore

    _HAS_DISCORD = True
except Exception:  # pragma: no cover
    _HAS_DISCORD = False


def _format_dt(value: Any, style: str = "R") -> str:
    if value is None:
        return "N/A"
    if not _HAS_DISCORD:
        return str(value)
    try:
        return discord.utils.format_dt(value, style=style)  # type: ignore[attr-defined]
    except Exception:
        return str(value)


def _global_source(value: str, ctx: ScriptContext) -> Optional[str]:
    """``{user}`` / ``{guild}`` tokens (also available inside ticket messages)."""
    user = ctx.author
    guild = ctx.guild or getattr(user, "guild", None)

    if user is not None:
        if value == "user":
            return f"{user}"
        if value == "user.mention":
            return getattr(user, "mention", str(user))
        if value == "user.name":
            return getattr(user, "name", str(user))
        if value == "user.display_name":
            return getattr(user, "display_name", getattr(user, "name", str(user)))
        if value == "user.avatar":
            return str(getattr(user, "display_avatar", user).url)
        if value == "user.id":
            return str(user.id)
        if value == "user.discriminator":
            return str(getattr(user, "discriminator", "0"))
        if value == "user.joined_at":
            return _format_dt(getattr(user, "joined_at", None))
        if value == "user.created_at":
            return _format_dt(getattr(user, "created_at", None))

    if guild is not None:
        if value == "guild.name":
            return str(getattr(guild, "name", "N/A"))
        if value == "guild.id":
            return str(getattr(guild, "id", "N/A"))
        if value == "guild.count":
            return str(getattr(guild, "member_count", "N/A"))
        if value == "guild.count.format":
            return str(getattr(guild, "member_count", "N/A"))
        if value == "guild.created_at":
            return _format_dt(getattr(guild, "created_at", None))
        if value == "guild.icon":
            icon = getattr(guild, "icon", None)
            return icon.url if icon else "https://none.none"
        if value == "guild.boost_count":
            return str(getattr(guild, "premium_subscription_count", 0))
        if value == "guild.boost_tier":
            return str(getattr(guild, "premium_tier", 0))
        if value == "guild.vanity":
            vanity = getattr(guild, "vanity_url_code", None)
            return f"/{vanity}" if vanity else "none"

    if value == "invisible":
        return "2B2D31"
    if value == "botcolor":
        return "7d7ead"

    return None


register_var_source(_global_source)


# ---------------------------------------------------------------------------
# Ticket providers (registered by cogs.tickets when it loads)
# ---------------------------------------------------------------------------


def register_ticket_providers(
    resolver: Callable[[str, ScriptContext], Optional[str]]
) -> None:
    """Install the ticket provider *before* the global fallback.

    The ticket resolver is the catch-all for ``ticket.*`` tokens; inserting it
    at the front keeps it authoritative while leaving the ``{user}`` / ``{guild}``
    fallback reachable for everything else.
    """
    if resolver in VAR_SOURCES:
        VAR_SOURCES.remove(resolver)
    VAR_SOURCES.insert(0, resolver)
