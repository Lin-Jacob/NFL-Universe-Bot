"""/seasonadmin commands: start and end seasons, move games between them, close the awards vote.
Only league admins (config.ADMIN_IDS / MASTER_ADMIN_IDS) can use them. They're their own group
because Discord allows at most 25 commands in /admin."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config

from . import analytics
from .admin import is_admin
from .embeds import season_embed, vote_embed

if TYPE_CHECKING:
    from bot import NFLUBot


class SeasonAdmin(commands.GroupCog, group_name="seasonadmin", group_description="Admin: seasons and awards votes"):
    def __init__(self, bot: "NFLUBot"):
        self.bot = bot
        self.db = bot.db
        super().__init__()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not is_admin(inter.user.id):
            await inter.response.send_message("Only league admins can use this command.", ephemeral=True)
            return False
        return True

    async def _ok(self, inter: discord.Interaction, msg: str, **kw):
        await inter.response.send_message(msg, ephemeral=True, **kw)

    # ---------------------------------------------------------------- seasons
    @app_commands.command(name="start", description="Start a new season: games from now on count toward it")
    @app_commands.describe(name="Name to show (default: Season N)",
                           include_past_games="Also put every game that isn't in a season yet into this one")
    async def startseason(self, inter: discord.Interaction, name: str | None = None,
                          include_past_games: bool = False):
        active = self.db.active_season()
        if active:
            await self._ok(inter, f"**{active['name']}** is still going. End it first with `/seasonadmin end`.")
            return
        s = self.db.start_season(name, include_past_games)
        made = [r for r in self.db.seasons() if r["id"] >= s["id"]]
        limit = int(getattr(config, "SEASON_GAMES", 20) or 0)
        msg = f"Started **{s['name']}**. New games count toward it"
        if include_past_games:
            msg += (", and the earlier games were filed into " + ", ".join(
                f"{r['name']} ({self.db.season_game_count(r['id'])} games)" for r in made) + ".")
        else:
            msg += "."
        if limit:
            msg += f" Each season ends by itself after {limit} games, or end it early with `/seasonadmin end`."
        if any(r["ended_at"] for r in made):
            msg += " Seasons that filled up have ended, and their awards and votes are announced shortly."
        await self._ok(inter, msg)

    @app_commands.command(name="end", description="End the season and announce the MVP for each role")
    @app_commands.describe(confirm="Set to True to really end it (False shows a preview of the awards)")
    async def endseason(self, inter: discord.Interaction, confirm: bool = False):
        s = self.db.active_season()
        if not s:
            await self._ok(inter, "No season is in progress. Start one with `/seasonadmin start`.")
            return
        race = analytics.season_mvps(self.db, s["id"])
        if not confirm:
            await self._ok(inter, "Preview only. Run `/seasonadmin end confirm:True` to end the season, post the "
                                  "stat awards and open the vote for the voted awards.",
                           embed=season_embed(self.db, s, race, final=False))
            return
        self.db.end_season(s["id"], race)
        s = self.db.season(s["id"])
        e = season_embed(self.db, s, race, final=True)
        posted = await self.bot.announce_embed(e)
        await self._ok(inter, f"Ended **{s['name']}**." + ("" if posted else
                       " No results channel is set (`/admin channel`), so here are the awards to share:"),
                       embed=e)

    @app_commands.command(name="reopen", description="Undo /seasonadmin end for the latest season")
    async def reopenseason(self, inter: discord.Interaction):
        s = self.db.latest_season()
        if not s or not s["ended_at"]:
            await self._ok(inter, "The latest season hasn't ended, so there's nothing to reopen.")
            return
        self.db.reopen_season(s["id"])
        await self._ok(inter, f"Reopened **{s['name']}**. Its awards and votes were cleared; end it again to "
                              f"recalculate.")

    @app_commands.command(name="closevote", description="Close the season vote now and post the voted awards")
    async def closevoting(self, inter: discord.Interaction):
        s = self.db.voting_season()
        if not s:
            await self._ok(inter, "No vote is open.")
            return
        results = await self.bot.close_vote(s["id"])
        await self._ok(inter, f"Closed voting for **{s['name']}** ({results['voters']} players voted). "
                              "The results are posted in the results channel.",
                       embed=vote_embed(self.db, self.db.season(s["id"]), results))

    @app_commands.command(name="move", description="Move a game into a season (0 = no season)")
    @app_commands.describe(game_id="Game number", season="Season number from /season (0 = not in any season)")
    async def setseason(self, inter: discord.Interaction, game_id: int, season: app_commands.Range[int, 0, 1000]):
        if season and not self.db.season(season):
            await self._ok(inter, f"There's no season {season}.")
            return
        ok = self.db.set_game_season(game_id, season)
        await self._ok(inter, (f"Game #{game_id} is now in season {season}." if season else
                               f"Game #{game_id} is no longer in a season.") if ok else f"No game #{game_id}.")
