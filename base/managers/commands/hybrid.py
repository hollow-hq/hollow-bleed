from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Set, Union

import discord
from discord.ext import commands
from discord.ext.commands import HybridCommand, HybridGroup

from base.managers.commands.routing import (
    NESTED_GROUPS,
    GROUP_ALIASES,
    nested_group_for,
    is_text_only,
    text_only_reason,
    nested_group_aliases,
    truncate_description,
)

logger = logging.getLogger(__name__)

# Module-level state
_GROUPS: dict[str, HybridGroup] = {}
_NESTED_WAITING: dict[str, list[HybridCommand]] = {}
_AUTO_GROUPS: set[str] = set()


def _clean_aliases(primary_name: str, aliases: Optional[List[str]]) -> List[str]:
    """Clean aliases: drop non-strings, case-insensitive duplicates, and the primary name."""
    if not aliases:
        return []
    cleaned: List[str] = []
    seen: Set[str] = {primary_name.lower()}
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        normalized = alias.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(alias.strip())
    return cleaned


async def _default_group_callback(ctx: commands.Context) -> None:
    """Default callback for bare group invocation with no subcommand: send group help."""
    await ctx.send_help(ctx.command)


def _ensure_nested_group(group_name: str) -> Optional[HybridGroup]:
    """Return an existing group.

    If the group is in NESTED_GROUPS and not waiting on a cog-declared one,
    create a hybrid group automatically:
    (invoke_without_command=True, aliases from GROUP_ALIASES, callback shows group help,
    fallback_to_global=False, cog=None), store it in _GROUPS and _AUTO_GROUPS, and return it.
    """
    if group_name in _GROUPS:
        return _GROUPS[group_name]

    if group_name in NESTED_GROUPS:
        slash_desc, help_desc = NESTED_GROUPS[group_name]
        aliases = nested_group_aliases(group_name)

        # Create auto group
        grp = commands.hybrid_group(
            name=group_name,
            aliases=aliases,
            description=truncate_description(slash_desc),
            help=help_desc,
            invoke_without_command=True,
            fallback=None,
        )(_default_group_callback)

        grp.cog = None
        _GROUPS[group_name] = grp
        _AUTO_GROUPS.add(group_name)

        # If there were commands waiting for this group, drain them now
        _drain_waiting_for_group(group_name, grp)

        return grp

    return None


def _attach_nested_command(group_name: str, cmd: HybridCommand) -> None:
    """Attach a command's slash counterpart to the group's slash group.

    If no group is available yet, append to _NESTED_WAITING.
    Otherwise group.app_command.add_command(cmd.app_command).
    If that raises (e.g. name clash inside the group), rebuild the command with
    with_app_command=False, mark it _nested_conflict = True, and keep it prefix-only.
    Never crash the bot over a clash, but LOG a warning naming the command.
    """
    group = _ensure_nested_group(group_name)
    if group is None or not group.app_command:
        _NESTED_WAITING.setdefault(group_name, []).append(cmd)
        return

    _attach_app_command_to_group(group, cmd)


def _attach_app_command_to_group(group: HybridGroup, cmd: HybridCommand) -> None:
    """Helper to attach cmd.app_command to group.app_command safely."""
    if not cmd.app_command or not group.app_command:
        return

    try:
        if cmd.app_command.name in group.app_command._children:
            existing = group.app_command._children[cmd.app_command.name]
            if existing is not cmd.app_command:
                # Name conflict within the slash group
                logger.warning(
                    f"Slash command name conflict: '{cmd.name}' in group '{group.name}'. "
                    f"Rebuilding '{cmd.name}' as prefix-only."
                )
                cmd.with_app_command = False
                cmd.app_command = None
                cmd._nested_conflict = True
                cmd._prefix_only_reason = "slash name conflict inside group"
                return

        group.app_command.add_command(cmd.app_command)
    except Exception as e:
        logger.warning(
            f"Failed to attach slash command '{cmd.name}' to group '{group.name}': {e}. "
            f"Rebuilding as prefix-only."
        )
        if cmd.app_command and cmd.app_command.name in group.app_command._children:
            group.app_command.remove_command(cmd.app_command.name)
        cmd.with_app_command = False
        cmd.app_command = None
        cmd._nested_conflict = True
        cmd._prefix_only_reason = f"slash registration error: {e}"


def _drain_waiting_for_group(group_name: str, group: HybridGroup) -> None:
    """Drain any commands that were declared before their group was created."""
    waiting = _NESTED_WAITING.pop(group_name, [])
    for cmd in waiting:
        _attach_app_command_to_group(group, cmd)


def hybrid_command(
    name: Optional[Union[str, discord.app_commands.locale_str]] = None,
    *,
    aliases: Optional[List[str]] = None,
    description: Optional[Union[str, discord.app_commands.locale_str]] = None,
    example: Optional[str] = None,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], HybridCommand]:
    """Decorator for creating decoupled hybrid commands.

    - If nested_group_for(effective_name): prefix stays top-level, slash is nested under group.
    - If is_text_only(effective_name): with_app_command=False and cmd._prefix_only_reason is set.
    - Otherwise: plain top-level hybrid command.
    """
    def decorator(func: Callable[..., Any]) -> HybridCommand:
        nonlocal name, description, aliases, example

        func_name = getattr(func, "__name__", "unknown")
        effective_name = str(name) if name is not None else func_name

        # Clean aliases
        clean_alias_list = _clean_aliases(effective_name, aliases)

        # Docstring / description fallback
        raw_doc = inspect.cleandoc(func.__doc__ or "") if func.__doc__ else ""
        if description is None:
            description = raw_doc or "No description provided."

        slash_desc = truncate_description(str(description))
        help_desc = str(description) or raw_doc or "No description provided."

        cmd_kwargs = dict(kwargs)
        cmd_kwargs["name"] = effective_name
        cmd_kwargs["aliases"] = clean_alias_list
        cmd_kwargs["description"] = slash_desc
        cmd_kwargs["help"] = help_desc

        nested_group = nested_group_for(effective_name)
        text_only = is_text_only(effective_name)

        if text_only:
            cmd_kwargs["with_app_command"] = False

        # Build base HybridCommand via discord.py decorator
        builder = commands.hybrid_command(**cmd_kwargs)
        cmd = builder(func)

        # Store metadata
        if example is not None:
            cmd._example = example
            cmd.extras["example"] = example
            setattr(func, "example", example)

        if text_only:
            reason = text_only_reason(effective_name) or "text only"
            cmd._prefix_only_reason = reason
            cmd.extras["_prefix_only_reason"] = reason

        if nested_group:
            cmd._nested_in = nested_group
            cmd.extras["_nested_in"] = nested_group
            # Also attach on func so Cog.__new__ copy can retrieve it
            func._nested_in = nested_group

            # Attach slash command to group
            _attach_nested_command(nested_group, cmd)

        return cmd

    return decorator


def hybrid_group(
    name: Optional[Union[str, discord.app_commands.locale_str]] = None,
    *,
    aliases: Optional[List[str]] = None,
    description: Optional[Union[str, discord.app_commands.locale_str]] = None,
    example: Optional[str] = None,
    **kwargs: Any,
) -> Callable[[Callable[..., Any]], HybridGroup]:
    """Decorator for creating hybrid groups.

    - Default invoke_without_command=True
    - Registers in _GROUPS and drains _NESTED_WAITING
    - Wraps callback so bare group invocation sends group help
    - Respects text_only and explicit with_app_command=False
    """
    def decorator(func: Callable[..., Any]) -> HybridGroup:
        nonlocal name, description, aliases, example

        func_name = getattr(func, "__name__", "unknown")
        effective_name = str(name) if name is not None else func_name

        clean_alias_list = _clean_aliases(effective_name, aliases)
        raw_doc = inspect.cleandoc(func.__doc__ or "") if func.__doc__ else ""
        if description is None:
            description = raw_doc or "No description provided."

        slash_desc = truncate_description(str(description))
        help_desc = str(description) or raw_doc or "No description provided."

        group_kwargs = dict(kwargs)
        group_kwargs.pop("invoke_without_command", None)
        group_kwargs["invoke_without_command"] = True
        group_kwargs["name"] = effective_name
        group_kwargs["aliases"] = clean_alias_list
        group_kwargs["description"] = slash_desc
        group_kwargs["help"] = help_desc

        if is_text_only(effective_name):
            group_kwargs["with_app_command"] = False

        # Wrap callback to send group help if bare invocation
        @functools.wraps(func)
        async def wrapped_callback(self_or_ctx: Any, *args: Any, **cb_kwargs: Any) -> Any:
            # Check if called as method in cog: self_or_ctx is Cog, args[0] is Context
            if isinstance(self_or_ctx, commands.Context):
                ctx = self_or_ctx
            elif args and isinstance(args[0], commands.Context):
                ctx = args[0]
            else:
                ctx = None

            # If invoked directly with no subcommands, execute or show help
            try:
                res = await func(self_or_ctx, *args, **cb_kwargs)
                if res is None and ctx is not None:
                    # If callback returns None and no subcommand was executed
                    pass
                return res
            except commands.MissingRequiredArgument:
                if ctx:
                    return await ctx.send_help(ctx.command)
                raise

        builder = commands.hybrid_group(**group_kwargs)
        grp = builder(wrapped_callback)

        if example is not None:
            grp._example = example
            grp.extras["example"] = example
            setattr(wrapped_callback, "example", example)

        if is_text_only(effective_name):
            reason = text_only_reason(effective_name) or "text only"
            grp._prefix_only_reason = reason
            grp.extras["_prefix_only_reason"] = reason

        # Register in _GROUPS and drain any waiting commands
        _GROUPS[effective_name] = grp
        _drain_waiting_for_group(effective_name, grp)

        nested_group = nested_group_for(effective_name)
        if nested_group:
            grp._nested_in = nested_group
            grp.extras["_nested_in"] = nested_group
            wrapped_callback._nested_in = nested_group
            _attach_nested_command(nested_group, grp)

        return grp

    return decorator
