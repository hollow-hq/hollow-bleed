from base.hollow import Bot


async def setup(bot: Bot) -> None:
    from .lastfm import LastFM

    await bot.add_cog(LastFM(bot))
