"""
NFL Universe league bot.

Run:  python bot.py
Starts the Discord bot, the webhook server (port WEBHOOK_PORT) and, if TUNNEL
is set, a public tunnel to that port. See README.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import discord
from discord.ext import commands
from dotenv import load_dotenv

import config
from nflu.admin import Admin
from nflu.commands import Stats
from nflu.db import Database
from nflu.embeds import game_embed
from nflu.parser import ParsedGame
from nflu.tunnel import Tunnel
from nflu.webhook import WebhookServer, ingest

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("nflu")


class NFLUBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Reading webhook messages in a Discord channel needs the Message Content intent.
        self.feed_channel_id = int(os.getenv("FEED_CHANNEL_ID") or 0)
        intents.message_content = bool(self.feed_channel_id)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Database(os.getenv("DATABASE_PATH", "data/nfl_universe.db"))
        self.port = int(os.getenv("WEBHOOK_PORT", "8080"))
        self.secret = os.getenv("WEBHOOK_SECRET", "")
        self.webhook = WebhookServer(self.db, self.secret, config.DEFAULT_SEASON,
                                     host=os.getenv("WEBHOOK_HOST", "0.0.0.0"), port=self.port,
                                     on_game=self.on_webhook_game)
        self.tunnel = Tunnel(os.getenv("TUNNEL", "cloudflared"), self.port)
        self._notified = False

    async def setup_hook(self) -> None:
        await self.add_cog(Stats(self))
        await self.add_cog(Admin(self))
        await self.webhook.start()
        url = await self.tunnel.start()
        self._print_banner(url)

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global slash commands (can take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._notified and os.getenv("DM_ADMINS_WEBHOOK_URL", "true").lower() == "true":
            self._notified = True
            for uid in config.ADMIN_IDS:
                try:
                    user = await self.fetch_user(uid)
                    await user.send("Bot started.\n" + self.webhook_info())
                except Exception as e:
                    log.warning("Couldn't DM admin %s: %s", uid, e)

    async def on_message(self, message: discord.Message) -> None:
        """Option B: NFL Universe posts to a normal Discord webhook in FEED_CHANNEL_ID; we read those posts."""
        if not self.feed_channel_id or message.channel.id != self.feed_channel_id:
            return
        if not message.webhook_id and not message.author.bot:
            return
        bodies = []
        for att in message.attachments:
            if att.filename.lower().endswith(".json") and att.size < 2 * 1024 * 1024:
                bodies.append((await att.read()).decode("utf-8", errors="replace"))
        if not bodies:
            payload = {"content": message.content, "embeds": [e.to_dict() for e in message.embeds]}
            # The content itself may be raw JSON
            stripped = message.content.strip().strip("`").removeprefix("json").strip()
            if stripped.startswith("{"):
                bodies.append(stripped)
            else:
                bodies.append(json.dumps(payload))
        for body in bodies:
            res = ingest(self.db, body, f"discord-feed:{message.id}", config.DEFAULT_SEASON)
            log.info("Feed message %s -> %s", message.id, {k: v for k, v in res.items() if k != "game"})
            if res.get("game") is not None:
                await self.announce_game(res["game_id"], res.get("replaced", False))
                try:
                    await message.add_reaction("✅")
                except discord.DiscordException:
                    pass

    async def close(self) -> None:
        await self.tunnel.stop()
        await self.webhook.stop()
        await super().close()

    # ----------------------------------------------------------------- helpers
    def webhook_url(self) -> str | None:
        base = self.tunnel.public_url or os.getenv("PUBLIC_URL")
        return f"{base.rstrip('/')}/webhook/{self.secret}" if base else None

    def webhook_info(self) -> str:
        url = self.webhook_url()
        local = f"http://localhost:{self.port}/webhook/{self.secret}"
        if not url:
            return (f"No public URL (TUNNEL={self.tunnel.kind}). Local URL: `{local}`\n"
                    "Set TUNNEL=cloudflared or ngrok in .env, or PUBLIC_URL if you expose the port yourself.")
        msg = f"Webhook URL to paste into NFL Universe:\n`{url}`\nKeep it private: the last part is your secret."
        if self.tunnel.kind == "cloudflared":
            msg += "\n(Cloudflare quick tunnel URLs change every time the bot restarts.)"
        return msg

    def _print_banner(self, url: str | None) -> None:
        line = "=" * 72
        print(f"\n{line}\n NFL Universe bot webhook")
        print(f"   local : http://localhost:{self.port}/webhook/<WEBHOOK_SECRET>")
        print(f"   public: {self.webhook_url() if url or os.getenv('PUBLIC_URL') else '(no tunnel running)'}")
        print(f"{line}\n", flush=True)

    async def on_webhook_game(self, game_id: int, game: ParsedGame, replaced: bool) -> None:
        await self.announce_game(game_id, replaced)

    async def announce_game(self, game_id: int, replaced: bool) -> bool:
        chan_id = self.db.get_setting("announce_channel") or os.getenv("ANNOUNCE_CHANNEL_ID")
        if not chan_id:
            return False
        channel = self.get_channel(int(chan_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(chan_id))
            except discord.DiscordException:
                log.warning("Announcement channel %s not found", chan_id)
                return False
        e = game_embed(self.db, game_id, "🔁 Updated: " if replaced else "🏈 Final: ")
        if e:
            await channel.send(embed=e)
        return True


async def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    bot = NFLUBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:  # Windows
            pass
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
