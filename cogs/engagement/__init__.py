from base.hollow import Bot


async def setup(bot: Bot) -> None:
    from .engagement import Engagement

    await bot.add_cog(Engagement(bot))
