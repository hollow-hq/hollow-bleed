from discord.ext.commands import check, MissingPermissions
from typing import Any


def has_permissions(**perms: str) -> Any:
    from base.context import Context

    async def predicate(ctx: Context):
        if ctx.author.id in ctx.bot.owner_ids:
            return True
        author_perms = ctx.author.guild_permissions

        if all(getattr(author_perms, perm, False) for perm in perms):
            return True

        role_ids = [r.id for r in ctx.author.roles]
        if not role_ids:
            raise MissingPermissions(perms)

        placeholders = ",".join("%s" for _ in role_ids)
        results = await ctx.bot.pool.fetch(
            "SELECT permission FROM fake_permissions WHERE guild_id = %s AND role_id IN (%s)" % placeholders,
            ctx.guild.id,
            *role_ids,
        )

        fake_perms = {row["permission"] for row in results}
        if "administrator" in fake_perms or any(perm in fake_perms for perm in perms):
            return True

        raise MissingPermissions(perms)

    return check(predicate)