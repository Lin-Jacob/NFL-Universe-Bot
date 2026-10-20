"""
NFL Universe league bot.

Run:  python bot.py
Starts the Discord bot, the webhook server (port WEBHOOK_PORT) and, if TUNNEL
is set, a public tunnel to that port. See README.md.
"""

from __future__ import annotations

import asyncio
import hashlib
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
from nflu.feed import PAIR_WINDOW, nearest_csv, nflu_embed, nflu_server_id, server_allowed
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
        self.webhook = WebhookServer(self.db, self.secret,
                                     host=os.getenv("WEBHOOK_HOST", "0.0.0.0"), port=self.port,
                                     on_game=self.on_webhook_game)
        self.tunnel = Tunnel(os.getenv("TUNNEL", "cloudflared"), self.port)
        self._notified = False

    def apply_config(self) -> int:
        """Load PLAYER_NAMES into the name map and rescore every game with FANTASY_SCORING."""
        for name, ign in config.PLAYER_NAMES.items():
            self.db.set_name(ign, name)
        return self.db.recompute_fantasy()

    async def setup_hook(self) -> None:
        self.apply_config()
        await self.add_cog(Stats(self))
        await self.add_cog(Admin(self))
        await self.webhook.start()
        url = await self.tunnel.start()
        self._print_banner(url)

        guild_id = os.getenv("GUILD_ID")
        guild = discord.Object(id=int(guild_id)) if guild_id else None
        if guild:
            self.tree.copy_global_to(guild=guild)
        # Only re-register commands with Discord when they changed: every sync counts against
        # Discord's daily limit, and when it's hit the library waits silently (looks like a freeze).
        payload = json.dumps([c.to_dict(self.tree) for c in self.tree.get_commands(guild=guild)], sort_keys=True)
        fingerprint = hashlib.sha256(f"{guild_id}|{payload}".encode()).hexdigest()
        force = os.getenv("FORCE_COMMAND_SYNC", "").lower() in ("1", "true", "yes")
        if not force and self.db.get_setting("commands_fingerprint") == fingerprint:
            log.info("Slash commands unchanged since last start; not re-registering them "
                     "(set FORCE_COMMAND_SYNC=1 in .env to force)")
            return
        log.info("Syncing slash commands with Discord (if this takes long, Discord is rate-limiting; just wait)...")
        synced = await self.tree.sync(guild=guild)
        self.db.set_setting("commands_fingerprint", fingerprint)
        if guild:
            log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
        else:
            log.info("Synced %d global slash commands (can take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._notified and os.getenv("DM_ADMINS_WEBHOOK_URL", "false").lower() == "true":
            self._notified = True
            for uid in config.ADMIN_IDS:
                try:
                    user = await self.fetch_user(uid)
                    await user.send("Bot started.\n" + self.webhook_info())
                except Exception as e:
                    log.warning("Couldn't DM admin %s: %s", uid, e)

    async def on_message(self, message: discord.Message) -> None:
        """NFL Universe posts each game to FEED_CHANNEL_ID as two messages, a stats CSV and a score embed,
        in the same second and in either order. Each game is handled when its score message arrives."""
        if not self.feed_channel_id or message.channel.id != self.feed_channel_id:
            return
        if message.author.id != config.NFLU_WEBHOOK_ID or nflu_embed(message) is None:
            return  # the CSV is picked up from the channel when its score message is handled
        await asyncio.sleep(3)  # let the CSV land if it's posted (or delivered to us) just after
        try:
            nearby = [m async for m in message.channel.history(limit=7, around=message)]
        except discord.DiscordException:
            log.exception("Couldn't read the messages around game post %s", message.id)
            nearby = []
        await self.ingest_game_post(message, nearest_csv(message, nearby))

    async def ingest_game_post(self, message: discord.Message, csv_message: discord.Message | None,
                               *, live: bool = True) -> dict:
        """Store + parse one NFL Universe score message plus its CSV (see feed.nearest_csv).
        A message already saved is skipped, unless it was saved without stats and the CSV is
        available now: then it's saved again with stats (same game number). live=False
        (backfill) doesn't announce."""
        prev = self.db.raws_for_message(message.id)
        if any(r["status"] == "deleted" for r in prev):
            return {"ok": True, "status": "already saved"}  # deleted on purpose: don't bring it back
        server_id = nflu_server_id(nflu_embed(message) or {})
        if not server_allowed(server_id):
            log.info("Game post %s ignored: server %s isn't in NFLU_SERVER_IDS %s",
                     message.id, server_id, config.NFLU_SERVER_IDS)
            return {"ok": False, "status": "other server (ignored)"}
        had_stats = any(json.loads(r["body"]).get("nfl_universe", {}).get("csv") for r in prev)
        if prev and (had_stats or csv_message is None):
            return {"ok": True, "status": "already saved"}
        csv_text = None
        if csv_message:
            att = next(a for a in csv_message.attachments if a.filename.lower().endswith(".csv"))
            csv_text = (await att.read()).decode("utf-8", errors="replace")
        else:
            log.warning("Game post %s: no stats CSV within %ss of it; saving the score only",
                        message.id, PAIR_WINDOW)
        for r in prev:  # an earlier save without stats is replaced by this one
            self.db.set_raw_status(r["id"], "superseded", "re-saved with the stats CSV")
        body = json.dumps({"nfl_universe": {"embed": nflu_embed(message), "csv": csv_text}})
        res = ingest(self.db, body, f"discord-feed:{message.id}", message_id=message.id)
        log.info("Game post %s -> %s", message.id, {k: v for k, v in res.items() if k != "game"})
        if res.get("game") is not None:
            if live:
                await self.announce_game(res["game_id"], res.get("replaced", False))
            try:
                await message.add_reaction("✅")
            except discord.DiscordException:
                pass
        return res

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
