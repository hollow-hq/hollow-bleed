from base.hollow import Bot


async def setup(bot: Bot) -> None:
    from .music import Music

    await bot.add_cog(Music(bot))
