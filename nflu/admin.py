"""/admin commands. Only Discord IDs listed in config.ADMIN_IDS can use them."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config

from .commands import stat_autocomplete
from .embeds import fmt_num, game_embed, table
from .parser import ParsedGame
from .stats import STATS, label
from .webhook import ingest, parse_raw

if TYPE_CHECKING:
    from bot import NFLUBot


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


class Admin(commands.GroupCog, group_name="admin", group_description="Admin-only league management"):
    def __init__(self, bot: "NFLUBot"):
        self.bot = bot
        self.db = bot.db
        super().__init__()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if is_admin(inter.user.id):
            return True
        await inter.response.send_message("Only league admins can use this command.", ephemeral=True)
        return False

    async def _ok(self, inter: discord.Interaction, msg: str, **kw):
        await inter.response.send_message(msg, ephemeral=True, **kw)

    # ------------------------------------------------------------------ names
    @app_commands.command(name="setname", description="Map a Roblox username to a real name (and optionally a Discord user)")
    @app_commands.describe(roblox_username="Their in-game Roblox username", display_name="Name to show instead",
                           discord_user="Their Discord account, so /me works for them")
    async def setname(self, inter: discord.Interaction, roblox_username: str, display_name: str,
                      discord_user: discord.User | None = None):
        self.db.set_name(roblox_username, display_name, discord_user.id if discord_user else None)
        await self._ok(inter, f"`{roblox_username}` will now show as **{display_name}**"
                       + (f" (linked to {discord_user.mention})" if discord_user else "") + ".")

    @app_commands.command(name="removename", description="Remove a name mapping")
    async def removename(self, inter: discord.Interaction, roblox_username: str):
        ok = self.db.remove_name(roblox_username)
        await self._ok(inter, "Removed." if ok else f"No mapping for `{roblox_username}`.")

    @app_commands.command(name="unmapped", description="Usernames that have stats but no name mapping")
    async def unmapped(self, inter: discord.Interaction):
        mapped = set(self.db.name_map())
        missing = [u for u in self.db.known_usernames() if u.lower() not in mapped]
        await self._ok(inter, ("Everyone is mapped." if not missing else
                               "No name yet: " + ", ".join(f"`{u}`" for u in missing))[:1900])

    # ------------------------------------------------------------------ games
    @app_commands.command(name="addgame", description="Manually record a game result")
    async def addgame(self, inter: discord.Interaction, home_team: str, away_team: str, home_score: int,
                      away_score: int, week: int | None = None, season: int | None = None):
        g = ParsedGame(external_id=None, home_team=home_team, away_team=away_team, home_score=home_score,
                       away_score=away_score, season=season, week=week)
        gid, _ = self.db.save_game(g, season=self.db.current_season(config.DEFAULT_SEASON), source="manual")
        await self._ok(inter, f"Recorded game **#{gid}**. Add player stats with `/admin setstat game_id:{gid}`.")

    @app_commands.command(name="editgame", description="Fix a game's teams, score, week or season")
    async def editgame(self, inter: discord.Interaction, game_id: int, home_team: str | None = None,
                       away_team: str | None = None, home_score: int | None = None, away_score: int | None = None,
                       week: int | None = None, season: int | None = None):
        ok = self.db.update_game(game_id, home_team=home_team, away_team=away_team, home_score=home_score,
                                 away_score=away_score, week=week, season=season)
        await self._ok(inter, f"Updated game #{game_id}." if ok else "Nothing changed (bad id or no fields given).")

    @app_commands.command(name="deletegame", description="Delete a game and all its player stats")
    async def deletegame(self, inter: discord.Interaction, game_id: int, confirm: bool = False):
        g = self.db.game(game_id)
        if not g:
            await self._ok(inter, f"No game #{game_id}.")
            return
        if not confirm:
            await self._ok(inter, f"This deletes game #{game_id} ({g['away_team']} @ {g['home_team']}) and its stats. "
                                  f"Run again with `confirm: True` to go ahead.")
            return
        self.db.delete_game(game_id)
        await self._ok(inter, f"Deleted game #{game_id}.")

    @app_commands.command(name="setstat", description="Set one stat for a player in a game (0 removes it)")
    @app_commands.autocomplete(stat=stat_autocomplete)
    async def setstat(self, inter: discord.Interaction, game_id: int, roblox_username: str, stat: str,
                      value: float, team: str | None = None):
        if stat not in STATS:
            await self._ok(inter, "Pick a stat from the list.")
            return
        if not self.db.game(game_id):
            await self._ok(inter, f"No game #{game_id}.")
            return
        fp = self.db.set_player_stat(game_id, roblox_username, stat, value, team)
        await self._ok(inter, f"`{roblox_username}` {label(stat)} = {fmt_num(value)} in game #{game_id}. "
                              f"Fantasy points now **{fmt_num(fp)}**.")

    @app_commands.command(name="removeplayer", description="Remove a player's stat line from a game")
    async def removeplayer(self, inter: discord.Interaction, game_id: int, roblox_username: str):
        ok = self.db.remove_player_from_game(game_id, roblox_username)
        await self._ok(inter, "Removed." if ok else "That player has no line in that game.")

    @app_commands.command(name="import", description="Import a game from a JSON file (same format as the webhook)")
    async def import_(self, inter: discord.Interaction, file: discord.Attachment):
        if file.size > 2 * 1024 * 1024:
            await self._ok(inter, "File too big (2 MB max).")
            return
        body = (await file.read()).decode("utf-8", errors="replace")
        res = ingest(self.db, body, f"discord:{inter.user.id}", config.DEFAULT_SEASON, source="import")
        if res.get("game_id") and res["status"] != "failed":
            await self.bot.announce_game(res["game_id"], res.get("replaced", False))
        await self._ok(inter, _result_text(res))

    # ------------------------------------------------------------ raw payloads
    @app_commands.command(name="rawlog", description="Recent webhook deliveries and whether they parsed")
    async def rawlog(self, inter: discord.Interaction, count: app_commands.Range[int, 1, 25] = 10):
        rows = self.db.recent_raw(count)
        if not rows:
            await self._ok(inter, "No webhook deliveries yet.")
            return
        t = table([[r["id"], r["received_at"][5:16].replace("T", " "), r["status"], r["game_id"] or "-",
                    f"{r['size']}B"] for r in rows], ["ID", "When (UTC)", "Status", "Game", "Size"])
        errs = "\n".join(f"#{r['id']}: {r['error'][:150]}" for r in rows if r["error"])
        await self._ok(inter, t + (f"\n{errs}"[:1900 - len(t)] if errs else ""))

    @app_commands.command(name="raw", description="Download one raw webhook payload as a file")
    async def raw(self, inter: discord.Interaction, raw_id: int):
        r = self.db.raw(raw_id)
        if not r:
            await self._ok(inter, f"No payload #{raw_id}.")
            return
        try:
            pretty = json.dumps(json.loads(r["body"]), indent=2)
        except Exception:
            pretty = r["body"]
        await self._ok(inter, f"Payload #{raw_id} · {r['status']}" + (f" · {r['error']}" if r["error"] else ""),
                       file=discord.File(io.BytesIO(pretty.encode()), filename=f"payload_{raw_id}.json"))

    @app_commands.command(name="reparse", description="Parse a stored payload again (after fixing the parser)")
    async def reparse(self, inter: discord.Interaction, raw_id: int):
        r = self.db.raw(raw_id)
        if not r:
            await self._ok(inter, f"No payload #{raw_id}.")
            return
        try:
            payload = json.loads(r["body"])
        except json.JSONDecodeError:
            await self._ok(inter, "That payload isn't valid JSON.")
            return
        if r["game_id"]:
            self.db.delete_game(r["game_id"])
        res = parse_raw(self.db, raw_id, payload, config.DEFAULT_SEASON)
        await self._ok(inter, _result_text(res))

    # --------------------------------------------------------------- settings
    @app_commands.command(name="season", description="Set the current season number")
    async def season(self, inter: discord.Interaction, number: int):
        self.db.set_setting("season", str(number))
        await self._ok(inter, f"Current season is now **{number}**. New games will be filed under it.")

    @app_commands.command(name="channel", description="Channel where new game results are posted")
    async def channel(self, inter: discord.Interaction, channel: discord.TextChannel | None = None):
        self.db.set_setting("announce_channel", str(channel.id) if channel else None)
        await self._ok(inter, f"Game results will be posted in {channel.mention}." if channel
                       else "Game results won't be posted anywhere.")

    @app_commands.command(name="recalc", description="Recalculate all fantasy points (after editing scoring in config.py)")
    async def recalc(self, inter: discord.Interaction):
        import importlib
        importlib.reload(config)
        n = self.db.recompute_fantasy()
        await self._ok(inter, f"Reloaded config.py and recalculated {n} stat lines.")

    @app_commands.command(name="webhookurl", description="Show the URL to paste into NFL Universe")
    async def webhookurl(self, inter: discord.Interaction):
        await self._ok(inter, self.bot.webhook_info())

    @app_commands.command(name="admins", description="List bot admins (edit ADMIN_IDS in config.py)")
    async def admins(self, inter: discord.Interaction):
        await self._ok(inter, "Admins: " + ", ".join(f"<@{i}>" for i in config.ADMIN_IDS),
                       allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="announce", description="Re-post a game's result to the results channel")
    async def announce(self, inter: discord.Interaction, game_id: int):
        e = game_embed(self.db, game_id)
        if not e:
            await self._ok(inter, f"No game #{game_id}.")
            return
        sent = await self.bot.announce_game(game_id, False)
        await self._ok(inter, "Posted." if sent else "No results channel set. Use `/admin channel` first.")


def _result_text(res: dict) -> str:
    s = res["status"]
    if s == "failed":
        return f"Couldn't parse payload #{res['raw_id']}: {res.get('error')}. See `/admin raw raw_id:{res['raw_id']}`."
    if s == "duplicate":
        return f"Already received this exact payload (#{res['raw_id']}, game #{res.get('game_id')})."
    return (f"{'Updated' if s == 'replaced' else 'Saved'} game **#{res['game_id']}** "
            f"with {res.get('players', 0)} player lines (payload #{res['raw_id']}).")
