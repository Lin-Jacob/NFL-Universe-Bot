"""Arrow-button paging for long embeds (/games, /inspect_game)."""

from __future__ import annotations

from typing import Callable

import discord


class Pager(discord.ui.View):
    """⏮ ◀ ▶ ⏭ buttons that flip through `count` pages drawn by `render(page)`.
    Only the person who ran the command can flip; buttons switch off after `timeout` seconds."""

    def __init__(self, owner_id: int, count: int, render: Callable[[int], discord.Embed],
                 start: int = 0, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.owner_id, self.count, self.render = owner_id, count, render
        self.page = max(0, min(start, count - 1))
        self.message: discord.InteractionMessage | None = None
        self._sync()

    async def send(self, inter: discord.Interaction) -> None:
        """Send page `start` as the reply to `inter` (without buttons if there's only one page)."""
        if self.count <= 1:
            await inter.response.send_message(embed=self.render(self.page))
            return
        await inter.response.send_message(embed=self.render(self.page), view=self)
        self.message = await inter.original_response()

    def _sync(self) -> None:
        self.first.disabled = self.prev.disabled = self.page <= 0
        self.next.disabled = self.last.disabled = self.page >= self.count - 1

    async def _go(self, inter: discord.Interaction, page: int) -> None:
        self.page = max(0, min(page, self.count - 1))
        self._sync()
        await inter.response.edit_message(embed=self.render(self.page), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary)
    async def first(self, inter: discord.Interaction, _: discord.ui.Button):
        await self._go(inter, 0)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary)
    async def prev(self, inter: discord.Interaction, _: discord.ui.Button):
        await self._go(inter, self.page - 1)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary)
    async def next(self, inter: discord.Interaction, _: discord.ui.Button):
        await self._go(inter, self.page + 1)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def last(self, inter: discord.Interaction, _: discord.ui.Button):
        await self._go(inter, self.count - 1)
