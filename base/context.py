from __future__ import annotations

from base.config import *
from base.managers.paginator import *
from discord.ext.commands import HelpCommand, Group
from datetime import datetime
from xxhash import xxh32_hexdigest

from discord.ext.commands import Command, Group


from typing import Union
try:
    from typing import Unpack
except ImportError:
    from typing_extensions import Unpack

from typing import Any, Dict, List, Optional, TYPE_CHECKING, TypedDict, cast

import discord
from discord import (
    AllowedMentions,
    ButtonStyle,
    Color,
    Message,
    MessageReference,
    Embed,
    Role,
    Member,
    ui,
)
from discord.ui import View, Button
from discord.ui import button
from discord.ext.commands import Context as BaseContext
from base.config import *

if TYPE_CHECKING:
    from base.hollow import Bot


SITE_URL = "https://hollow-phi.vercel.app/"
HOME_KEY = "__home__"
HIDDEN_COGS = frozenset({"Owner", "Other"})


class FieldDict(TypedDict, total=False):
    name: str
    value: str
    inline: bool


class FooterDict(TypedDict, total=False):
    text: Optional[str]
    icon_url: Optional[str]


class AuthorDict(TypedDict, total=False):
    name: Optional[str]
    icon_url: Optional[str]


def build_usage(command) -> str:
    syntax = f"Syntax: {command.qualified_name} {command.usage or ''}".rstrip()

    try:
        example = command.example  # type: ignore
    except (AttributeError, TypeError):
        example = ""

    if example:
        return f"```{syntax}\nExample: {example}```"

    return f"```{syntax}```"


class ButtonDict(TypedDict, total=False):
    url: Optional[str]
    emoji: Optional[str]
    style: Optional[ButtonStyle]
    label: Optional[str]


class MessageKwargs(TypedDict, total=False):
    content: Optional[str]
    tts: Optional[bool]
    allowed_mentions: Optional[AllowedMentions]
    reference: Optional[MessageReference]
    mention_author: Optional[bool]
    delete_after: Optional[float]

    # Embed Related
    url: Optional[str]
    title: Optional[str]
    color: Optional[Color]
    image: Optional[str]
    description: Optional[str]
    thumbnail: Optional[str]
    footer: Optional[FooterDict]
    author: Optional[AuthorDict]
    fields: Optional[List[FieldDict]]
    timestamp: Optional[datetime]
    view: Optional[View]
    buttons: Optional[List[ButtonDict]]


class Context(BaseContext):
    bot: "Bot"

    def is_dangerous(self, role: Role) -> bool:
        permissions = role.permissions

        return any(
            [
                permissions.kick_members,
                permissions.ban_members,
                permissions.administrator,
                permissions.manage_channels,
                permissions.manage_guild,
                permissions.manage_messages,
                permissions.manage_roles,
                permissions.manage_webhooks,
                permissions.manage_emojis_and_stickers,
                permissions.manage_threads,
                permissions.mention_everyone,
                permissions.moderate_members,
            ]
        )

    async def embed(self, **kwargs: Unpack[MessageKwargs]) -> Message:
        return await self.send(**self.create(**kwargs))

    def create(self, **kwargs: Unpack[MessageKwargs]) -> Dict[str, Any]:
        """Create a message with the given keword arguments.

        Returns:
            Dict[str, Any]: The message content, embed, view and delete_after.
        """
        view = View()

        for button in kwargs.get("buttons") or []:
            if not button or not button.get("label"):
                continue

            view.add_item(
                Button(
                    label=button.get("label"),
                    style=button.get("style") or ButtonStyle.secondary,
                    emoji=button.get("emoji"),
                    url=button.get("url"),
                )
            )

        embed = (
            Embed(
                url=kwargs.get("url"),
                description=kwargs.get("description"),
                title=kwargs.get("title"),
                color=kwargs.get("color") or COLORS.neutral,
                timestamp=kwargs.get("timestamp"),
            )
            .set_image(url=kwargs.get("image"))
            .set_thumbnail(url=kwargs.get("thumbnail"))
            .set_footer(
                text=cast(dict, kwargs.get("footer", {})).get("text"),
                icon_url=cast(dict, kwargs.get("footer", {})).get("icon_url"),
            )
            .set_author(
                name=cast(dict, kwargs.get("author", {})).get("name", ""),
                icon_url=cast(dict, kwargs.get("author", {})).get("icon_url", ""),
            )
        )

        for field in kwargs.get("fields") or []:
            if not field:
                continue

            embed.add_field(
                name=field.get("name"),
                value=field.get("value"),
                inline=field.get("inline", False),
            )

        return {
            "content": kwargs.get("content"),
            "embed": embed,
            "view": kwargs.get("view") or view,
            "delete_after": kwargs.get("delete_after"),
        }

    async def approve(self, message: str, **kwargs) -> Message:
        return await self.send(
            embed=Embed(
                color=COLORS.approve,
                description=f"{EMOJIS.APPROVE} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def warn(self, message: str, **kwargs) -> Message:
        return await self.send(
            embed=Embed(
                color=COLORS.warn,
                description=f"{EMOJIS.WARN} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def deny(self, message: str, **kwargs) -> Message:
        return await self.send(
            embed=Embed(
                color=COLORS.deny,
                description=f"{EMOJIS.DENY} {self.author.mention}: {message}",
            ),
            **kwargs,
        )

    async def cooldown(self, message: str, **kwargs) -> Message:
        return await self.send(
            embed=Embed(
                color=0x38A9E1,
                description=f"{EMOJIS.COOLDOWN} {self.author.mention}: {message}",
            )
        )

    async def paginate(self, embeds: List[discord.Embed], **kwargs) -> Message:
        if len(embeds) == 1:
            if isinstance(embeds[0], discord.Embed):
                return await self.send(embed=embeds[0])

        return await self.send(embed=embeds[0], view=Paginator(self, embeds), **kwargs)


# ---------------------------------------------------------------------------
# Help menu (Components V2) - requires discord.py 2.6+
# ---------------------------------------------------------------------------


def count_commands(cmds: List[Command]) -> int:
    """Count commands, including visible subcommands of groups."""
    total = 0
    for cmd in cmds:
        total += 1
        if isinstance(cmd, Group):
            total += sum(1 for c in cmd.walk_commands() if not c.hidden)
    return total


def list_command_names(cmds: List[Command]) -> List[str]:
    """Flat list of visible command names (groups expand to their subcommands)."""
    names: List[str] = []
    for cmd in cmds:
        if isinstance(cmd, Group):
            subs = [c.qualified_name for c in cmd.walk_commands() if not c.hidden]
            names.extend(subs or [cmd.qualified_name])
        else:
            names.append(cmd.qualified_name)
    return names


class HelpSelect(ui.Select):
    def __init__(self, help_view: "HelpView", current: str):
        self.help_view = help_view

        options = [
            discord.SelectOption(
                label="Home",
                value=HOME_KEY,
                description="Back to the overview",
                emoji="🏠",
                default=current == HOME_KEY,
            )
        ]
        # Discord allows max 25 options (1 is used by Home)
        for name, (cog, cmds) in list(help_view.categories.items())[:24]:
            options.append(
                discord.SelectOption(
                    label=name.title(),
                    value=name,
                    description=f"{count_commands(cmds)} commands",
                    default=current == name,
                )
            )

        super().__init__(
            placeholder="Select a plugin...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.help_view.render(self.values[0])
        await interaction.response.edit_message(view=self.help_view)


class HelpView(ui.LayoutView):
    def __init__(self, ctx: Context, categories: Dict[str, tuple]):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.categories = categories  # {cog_name: (cog, [commands])}
        self.message: Optional[discord.Message] = None
        self.select: Optional[HelpSelect] = None
        self.render(HOME_KEY)

    # -- layout -------------------------------------------------------------
    def render(self, key: str) -> None:
        self.clear_items()

        bot = self.ctx.bot
        avatar = bot.user.display_avatar.url if bot.user else None
        prefix = self.ctx.clean_prefix

        if key == HOME_KEY:
            total = sum(count_commands(cmds) for _, cmds in self.categories.values())
            header = ui.TextDisplay(
                f"# [hollow.]({SITE_URL})\n"
                f"**An all-in-one Discord bot made with features in mind**\n"
                f"Serving **{total}** commands across **{len(self.categories)}** plugins"
            )
            body = ui.TextDisplay(
                f"-# Pick a plugin from the menu below to see its commands.\n"
                f"-# Use `{prefix}help <command>` for details on a specific command."
            )
        else:
            _, cmds = self.categories[key]
            names = ", ".join(list_command_names(cmds))
            if len(names) > 3800:
                names = names[:3800].rsplit(",", 1)[0] + ", ..."
            header = ui.TextDisplay(
                f"# {key.title()}\n-# {count_commands(cmds)} commands inside this plugin"
            )
            body = ui.TextDisplay(f"```{names}```")

        container = ui.Container(accent_colour=COLORS.neutral)

        if avatar:
            container.add_item(ui.Section(header, accessory=ui.Thumbnail(media=avatar)))
        else:
            container.add_item(header)

        container.add_item(body)
        container.add_item(
            ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)
        )

        self.select = HelpSelect(self, key)
        container.add_item(ui.ActionRow(self.select))
        self.add_item(container)

    # -- behaviour ----------------------------------------------------------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message(
                "This help menu isn't yours. Run the help command yourself!",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.select:
            self.select.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HollowHelp(HelpCommand):
    context: "Context"

    def __init__(self, **options):
        super().__init__(
            command_attrs={"aliases": ["h", "cmds", "commands"], "hidden": True},
            verify_checks=False,
            **options,
        )

    async def send_bot_help(self, mapping):
        categories: Dict[str, tuple] = {}

        for cog, cmds in mapping.items():
            visible = await self.filter_commands(cmds, sort=True)
            if not visible:
                continue
            name = cog.qualified_name if cog else "Other"
            if name in HIDDEN_COGS:
                continue
            categories[name] = (cog, visible)

        categories = dict(sorted(categories.items(), key=lambda kv: kv[0].lower()))

        view = HelpView(self.context, categories)
        view.message = await self.context.send(view=view)

    async def send_command_help(self, command: Command):
        aliases = command.aliases

        try:
            permissions = command.permissions  # type: ignore
        except (AttributeError, TypeError):
            permissions = []

        # Check for nested slash info or prefix-only reason
        nested_in = getattr(command, "_nested_in", None)
        if not nested_in and hasattr(command, "extras"):
            nested_in = command.extras.get("_nested_in")

        prefix_only_reason = getattr(command, "_prefix_only_reason", None)
        if not prefix_only_reason and hasattr(command, "extras"):
            prefix_only_reason = command.extras.get("_prefix_only_reason")

        desc = command.help or "No description provided"

        embed = (
            Embed(
                color=COLORS.neutral,
                title=f"Command: {command.qualified_name}",
                description=desc,
            )
            .set_author(
                name=self.context.author.name,
                icon_url=self.context.author.display_avatar.url,
            )
            .add_field(
                name="Aliases",
                value=", ".join(aliases) if aliases else "N/A",
                inline=True,
            )
            .add_field(
                name="Parameters",
                value=(
                    ", ".join(command.clean_params) if command.clean_params else "N/A"
                ),
                inline=True,
            )
            .add_field(
                name="Information",
                value=f"{EMOJIS.WARN} "
                + (", ".join(permissions) if permissions else "N/A"),
                inline=True,
            )
            .add_field(
                name="Usage",
                value=build_usage(command),
                inline=False,
            )
            .set_footer(
                text=(
                    f"Page 1/1 • Module: " + command.cog_name.lower()
                    if command.cog_name
                    else "N/A"
                ),
            )
        )
        return await self.context.send(embed=embed)

    async def send_group_help(self, group: Group):
        embeds = []

        # Collect child commands: include group.commands plus any nested commands
        commands_list = list(group.commands)
        seen_names = {c.name for c in commands_list}

        # Check if any commands in bot are nested in this group
        if self.context.bot:
            for bot_cmd in self.context.bot.commands:
                nested_in = getattr(bot_cmd, "_nested_in", None)
                if not nested_in and hasattr(bot_cmd, "extras"):
                    nested_in = bot_cmd.extras.get("_nested_in")
                if nested_in == group.name and bot_cmd.name not in seen_names:
                    commands_list.append(bot_cmd)
                    seen_names.add(bot_cmd.name)

        group_permissions = set()
        for cmd in commands_list:
            try:
                if hasattr(cmd, "permissions") and cmd.permissions:  # type: ignore
                    group_permissions.update(cmd.permissions)  # type: ignore
            except (AttributeError, TypeError):
                continue

        group_embed = (
            Embed(
                color=COLORS.neutral,
                title=f"Command Group: {group.name}",
                description=group.help or "No description provided",
            )
            .set_author(
                name=self.context.author.name,
                icon_url=self.context.author.display_avatar.url,
            )
            .add_field(
                name="Aliases",
                value=", ".join(group.aliases) if group.aliases else "N/A",
                inline=True,
            )
            .add_field(
                name="Parameters",
                value=", ".join(group.clean_params) if group.clean_params else "N/A",
                inline=True,
            )
            .add_field(
                name="Information",
                value=f"{EMOJIS.WARN} "
                + (", ".join(group_permissions) if group_permissions else "N/A"),
                inline=True,
            )
            .add_field(
                name="Usage",
                value=build_usage(group),
                inline=False,
            )
            .set_footer(
                text=f"Page 1/{len(commands_list) + 1} • Module: {group.cog_name.lower() if group.cog_name else 'N/A'}"
            )
        )
        embeds.append(group_embed)

        for i, command in enumerate(commands_list):
            try:
                permissions = command.permissions  # type: ignore
            except (AttributeError, TypeError):
                permissions = []

            nested_in = getattr(command, "_nested_in", None)
            if not nested_in and hasattr(command, "extras"):
                nested_in = command.extras.get("_nested_in")

            desc = command.help or "No description provided"

            command_embed = (
                Embed(
                    color=COLORS.neutral,
                    title=f"Command: {command.name}",
                    description=desc,
                )
                .set_author(
                    name=self.context.author.name,
                    icon_url=self.context.author.display_avatar.url,
                )
                .add_field(
                    name="Aliases",
                    value=", ".join(command.aliases) if command.aliases else "N/A",
                    inline=True,
                )
                .add_field(
                    name="Parameters",
                    value=(
                        ", ".join(command.clean_params)
                        if command.clean_params
                        else "N/A"
                    ),
                    inline=True,
                )
                .add_field(
                    name="Information",
                    value=f"{EMOJIS.WARN} "
                    + (", ".join(permissions) if permissions else "N/A"),
                    inline=True,
                )
                .add_field(
                    name="Usage",
                    value=build_usage(command),
                    inline=False,
                )
                .set_footer(
                    text=f"Page {i + 2}/{len(commands_list) + 1} • Module: {command.cog_name.lower() if command.cog_name else 'N/A'}"
                )
            )

            embeds.append(command_embed)
        await self.context.paginate(embeds)


class Confirmation(View):
    def __init__(self, ctx: Context, user: Member, reason: str, action: str):
        super().__init__()
        self.ctx = ctx
        self.user = user
        self.reason = reason
        self.action = action
        self.message = None

    async def send_confirmation(self):
        embed = Embed(
            title=f"{EMOJIS.WARN} Confirm Action",
            description=f"Are you sure you want to {self.action} {self.user.mention if self.user else ''}?",
            color=COLORS.neutral,
        )
        self.message = await self.ctx.send(embed=embed, view=self)

    @ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def yes_button(self, button: Button, interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message(
                "You cannot confirm this action.", ephemeral=True
            )
            return
        if self.action == "ban" and self.user:
            await self.user.ban(reason=self.reason)
            await self.ctx.approve(f"{self.user.mention} has been **banned**.")
        elif self.action == "kick" and self.user:
            await self.user.kick(reason=self.reason)
            await self.ctx.approve(f"{self.user.mention} has been **kicked**.")

        if self.message:
            await self.message.delete()
        self.stop()

    @ui.button(label="No", style=discord.ButtonStyle.red)
    async def no_button(self, button: Button, interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message(
                "You cannot confirm cancel action.", ephemeral=True
            )
            return
        await self.ctx.approve(
            f"{self.action.capitalize()} action has been **cancelled**."
        )
        if self.message:
            await self.message.delete()
        self.stop()