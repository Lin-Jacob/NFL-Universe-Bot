"""Slash commands anyone in the server can use."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config

from . import analytics
from .embeds import Names, fmt_num, game_embed, game_header, score_line, short_line, stats_block, table
from .fantasy import breakdown
from .stats import STATS, label

if TYPE_CHECKING:
    from bot import NFLUBot


async def stat_autocomplete(_: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    opts = [("Fantasy points", "fantasy")] + [(lbl, k) for k, (lbl, _) in STATS.items()]
    cur = current.lower()
    return [app_commands.Choice(name=n, value=v) for n, v in opts
            if cur in n.lower() or cur in v][:25]


class Stats(commands.Cog):
    def __init__(self, bot: "NFLUBot"):
        self.bot = bot
        self.db = bot.db

    async def player_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        names = Names(self.db)
        cur = current.lower()
        out = []
        for u in self.db.known_usernames():
            full = names.full(u)
            if cur in full.lower():
                out.append(app_commands.Choice(name=full[:100], value=u))
            if len(out) >= 25:
                break
        return out

    async def team_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        teams = {r["team"] for r in analytics.standings(self.db, None)}
        return [app_commands.Choice(name=t, value=t) for t in sorted(teams) if current.lower() in t.lower()][:25]

    def _season(self, season: int | None) -> int:
        return season if season is not None else self.db.current_season(config.DEFAULT_SEASON)

    async def _resolve(self, inter: discord.Interaction, query: str) -> str | None:
        key = self.db.resolve_player(query)
        if not key:
            await inter.response.send_message(f"I couldn't find a player called **{query}**.", ephemeral=True)
        return key

    # ------------------------------------------------------------------ games
    @app_commands.command(name="games", description="Recent game results")
    @app_commands.describe(team="Only this team's games", season="Season number (default: current)",
                           count="How many games (max 25)")
    @app_commands.autocomplete(team=team_autocomplete)
    async def games(self, inter: discord.Interaction, team: str | None = None, season: int | None = None,
                    count: app_commands.Range[int, 1, 25] = 10):
        rows = self.db.recent_games(count, self._season(season), team)
        if not rows:
            await inter.response.send_message("No games recorded yet for that filter.")
            return
        e = discord.Embed(title=f"Recent games · Season {self._season(season)}" + (f" · {team}" if team else ""),
                          color=config.EMBED_COLOR)
        e.description = "\n".join(f"`#{g['id']}` {score_line(g)}" + (f" · Wk {g['week']}" if g["week"] else "")
                                  for g in rows)
        e.set_footer(text="Use /game <id> for the box score")
        await inter.response.send_message(embed=e)

    @app_commands.command(name="game", description="Full box score for one game")
    @app_commands.describe(game_id="Game number shown in /games (leave empty for the latest game)")
    async def game(self, inter: discord.Interaction, game_id: int | None = None):
        if game_id is None:
            latest = self.db.recent_games(1)
            if not latest:
                await inter.response.send_message("No games recorded yet.")
                return
            game_id = latest[0]["id"]
        e = game_embed(self.db, game_id)
        if not e:
            await inter.response.send_message(f"No game #{game_id}.", ephemeral=True)
            return
        await inter.response.send_message(embed=e)

    @app_commands.command(name="standings", description="Team standings")
    @app_commands.describe(season="Season number (default: current)")
    async def standings(self, inter: discord.Interaction, season: int | None = None):
        rows = analytics.standings(self.db, self._season(season))
        if not rows:
            await inter.response.send_message("No games recorded yet this season.")
            return
        t = table([[i + 1, r["team"][:18], f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else ""),
                    f"{r['pct']:.3f}", r["pf"], r["pa"], f"{r['diff']:+d}"] for i, r in enumerate(rows)],
                  ["#", "Team", "W-L", "PCT", "PF", "PA", "DIFF"])
        await inter.response.send_message(embed=discord.Embed(
            title=f"Standings · Season {self._season(season)}", description=t, color=config.EMBED_COLOR))

    @app_commands.command(name="h2h", description="Head-to-head history between two teams")
    @app_commands.autocomplete(team_a=team_autocomplete, team_b=team_autocomplete)
    async def h2h(self, inter: discord.Interaction, team_a: str, team_b: str):
        games = analytics.head_to_head(self.db, team_a, team_b)
        if not games:
            await inter.response.send_message(f"{team_a} and {team_b} haven't played each other yet.")
            return
        wins_a = sum(1 for g in games if (g["home_team"].lower() == team_a.lower() and g["home_score"] > g["away_score"])
                     or (g["away_team"].lower() == team_a.lower() and g["away_score"] > g["home_score"]))
        wins_b = sum(1 for g in games if (g["home_team"].lower() == team_b.lower() and g["home_score"] > g["away_score"])
                     or (g["away_team"].lower() == team_b.lower() and g["away_score"] > g["home_score"]))
        e = discord.Embed(title=f"{team_a} vs {team_b}", color=config.EMBED_COLOR,
                          description=f"**{team_a} {wins_a} – {wins_b} {team_b}**"
                                      + (f" ({len(games) - wins_a - wins_b} ties)" if len(games) - wins_a - wins_b else ""))
        e.add_field(name="Games", value="\n".join(f"`#{g['id']}` {score_line(g)} · S{g['season']}" for g in games[:15]))
        await inter.response.send_message(embed=e)

    # ---------------------------------------------------------------- players
    @app_commands.command(name="player", description="A player's stats and fantasy points")
    @app_commands.describe(name="Name, Roblox username, or @mention", season="Season (default: current; 0 = career)")
    @app_commands.autocomplete(name=player_autocomplete)
    async def player(self, inter: discord.Interaction, name: str, season: int | None = None):
        key = await self._resolve(inter, name)
        if not key:
            return
        s = None if season == 0 else self._season(season)
        summ = analytics.player_summary(self.db, key, s)
        names = Names(self.db)
        if not summ:
            await inter.response.send_message(f"**{names.full(key)}** has no games in season {s}. "
                                              "Try `season: 0` for career stats.")
            return
        e = discord.Embed(title=names.full(summ["username"]), color=config.EMBED_COLOR,
                          description=f"{'Career' if s is None else f'Season {s}'} · {summ['games']} games · "
                                      f"{summ['wins']} wins · Teams: {', '.join(summ['teams']) or '?'}")
        e.add_field(name="Fantasy", value=f"Total **{fmt_num(summ['fantasy_total'])}**\n"
                                          f"Per game **{fmt_num(summ['fantasy_avg'])}**", inline=True)
        b = summ["best_game"]
        e.add_field(name="Best game", value=f"{fmt_num(b['fantasy_points'])} FP · game #{b['game_id']}\n"
                                            f"{short_line(b['stats'])}", inline=True)
        for title, body in stats_block(summ["totals"]):
            e.add_field(name=title, value=body, inline=True)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="gamelog", description="A player's recent games")
    @app_commands.autocomplete(name=player_autocomplete)
    async def gamelog(self, inter: discord.Interaction, name: str, count: app_commands.Range[int, 1, 20] = 10):
        key = await self._resolve(inter, name)
        if not key:
            return
        lines = self.db.player_lines(key)[:count]
        names = Names(self.db)
        e = discord.Embed(title=f"Game log · {names.full(lines[0]['username'])}", color=config.EMBED_COLOR)
        e.description = "\n".join(
            f"`#{ln['game_id']}` {ln['away_team']} {ln['away_score']} @ {ln['home_team']} {ln['home_score']} · "
            f"**{fmt_num(ln['fantasy_points'])} FP**\n{short_line(ln['stats'])}" for ln in lines)[:4000]
        await inter.response.send_message(embed=e)

    @app_commands.command(name="compare", description="Compare two players side by side")
    @app_commands.describe(season="Season (default: current; 0 = career)")
    @app_commands.autocomplete(player1=player_autocomplete, player2=player_autocomplete)
    async def compare(self, inter: discord.Interaction, player1: str, player2: str, season: int | None = None):
        k1, k2 = self.db.resolve_player(player1), self.db.resolve_player(player2)
        if not k1 or not k2:
            await inter.response.send_message(f"Couldn't find **{player1 if not k1 else player2}**.", ephemeral=True)
            return
        s = None if season == 0 else self._season(season)
        a, b = analytics.player_summary(self.db, k1, s), analytics.player_summary(self.db, k2, s)
        if not a or not b:
            await inter.response.send_message("One of those players has no games in that season.")
            return
        names = Names(self.db)
        keys = [k for k in STATS if a["totals"].get(k) or b["totals"].get(k)]
        rows = [["Games", a["games"], b["games"]],
                ["Fantasy", fmt_num(a["fantasy_total"]), fmt_num(b["fantasy_total"])],
                ["FP/game", fmt_num(a["fantasy_avg"]), fmt_num(b["fantasy_avg"])]]
        rows += [[label(k)[:18], fmt_num(a["totals"].get(k, 0)), fmt_num(b["totals"].get(k, 0))] for k in keys]
        e = discord.Embed(title=f"{names(a['username'])} vs {names(b['username'])}",
                          description=table(rows, ["", names(a["username"])[:14], names(b["username"])[:14]]),
                          color=config.EMBED_COLOR)
        e.set_footer(text="Career" if s is None else f"Season {s}")
        await inter.response.send_message(embed=e)

    # ------------------------------------------------------------ leaderboards
    @app_commands.command(name="leaders", description="Stat leaders")
    @app_commands.describe(stat="Which stat", season="Season (default: current; 0 = all-time)",
                           per_game="Rank by per-game average instead of totals")
    @app_commands.autocomplete(stat=stat_autocomplete)
    async def leaders(self, inter: discord.Interaction, stat: str, season: int | None = None,
                      per_game: bool = False):
        if stat != "fantasy" and stat not in STATS:
            await inter.response.send_message("Pick a stat from the list.", ephemeral=True)
            return
        s = None if season == 0 else self._season(season)
        rows = analytics.leaders(self.db, stat, s, 10, per_game)
        if not rows:
            await inter.response.send_message("Nobody has recorded that stat yet.")
            return
        names = Names(self.db)
        stat_name = "Fantasy points" if stat == "fantasy" else label(stat)
        e = discord.Embed(title=f"{stat_name} leaders" + (" (per game)" if per_game else ""),
                          color=config.EMBED_COLOR,
                          description="\n".join(f"**{i + 1}.** {names(n)} · **{fmt_num(v)}** ({g} GP)"
                                                for i, (n, v, g) in enumerate(rows)))
        e.set_footer(text="All-time" if s is None else f"Season {s}")
        await inter.response.send_message(embed=e)

    @app_commands.command(name="fantasy", description="Fantasy points leaderboard")
    @app_commands.describe(season="Season (default: current)", week="Only this week")
    async def fantasy(self, inter: discord.Interaction, season: int | None = None, week: int | None = None):
        s = self._season(season)
        rows = analytics.fantasy_week(self.db, s, week, 15)
        if not rows:
            await inter.response.send_message("No fantasy points recorded for that filter yet.")
            return
        names = Names(self.db)
        e = discord.Embed(title=f"Fantasy leaderboard · Season {s}" + (f" · Week {week}" if week else ""),
                          color=config.EMBED_COLOR,
                          description="\n".join(
                              f"{['🥇', '🥈', '🥉'][i] if i < 3 else f'**{i + 1}.**'} {names(n)} · "
                              f"**{fmt_num(v)}** FP ({g} GP, {fmt_num(v / g)}/g)"
                              for i, (n, v, g) in enumerate(rows)))
        await inter.response.send_message(embed=e)

    @app_commands.command(name="points", description="How a player's fantasy points were scored in a game")
    @app_commands.describe(game_id="Game number (default: their latest game)")
    @app_commands.autocomplete(name=player_autocomplete)
    async def points(self, inter: discord.Interaction, name: str, game_id: int | None = None):
        key = await self._resolve(inter, name)
        if not key:
            return
        lines = self.db.player_lines(key)
        line = next((l for l in lines if game_id is None or l["game_id"] == game_id), None)
        if not line:
            await inter.response.send_message("That player has no stats in that game.", ephemeral=True)
            return
        rows = [[label(s) if s in STATS else s, fmt_num(v), f"{p:+.2f}"] for s, v, p in breakdown(line["stats"])]
        names = Names(self.db)
        e = discord.Embed(title=f"{names(line['username'])} · {fmt_num(line['fantasy_points'])} FP",
                          description=f"Game #{line['game_id']}: {line['away_team']} {line['away_score']} @ "
                                      f"{line['home_team']} {line['home_score']}\n" +
                                      table(rows, ["Stat", "Value", "Pts"]),
                          color=config.EMBED_COLOR)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="scoring", description="Show the fantasy scoring rules")
    async def scoring(self, inter: discord.Interaction):
        rows = [[label(k), f"{v:+g}"] for k, v in config.FANTASY_SCORING.items() if v]
        rows += [[f"{label(s)} ≥ {t:g}", f"{b:+g} bonus"] for s, t, b in config.FANTASY_BONUSES]
        await inter.response.send_message(embed=discord.Embed(
            title="Fantasy scoring (per unit)", description=table(rows, ["Stat", "Points"]),
            color=config.EMBED_COLOR))

    # ------------------------------------------------------------------ names
    @app_commands.command(name="names", description="Who's who: Roblox usernames and real names")
    async def names_cmd(self, inter: discord.Interaction):
        rows = self.db.names()
        if not rows:
            await inter.response.send_message("No names mapped yet. An admin can add them with /admin setname.")
            return
        e = discord.Embed(title="Name mappings", color=config.EMBED_COLOR,
                          description="\n".join(f"**{r['display_name']}** · `{r['roblox_username']}`"
                                                + (f" · <@{r['discord_id']}>" if r["discord_id"] else "")
                                                for r in rows)[:4000])
        await inter.response.send_message(embed=e, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="whois", description="Look up a player by name, Roblox username or @mention")
    async def whois(self, inter: discord.Interaction, query: str):
        key = await self._resolve(inter, query)
        if not key:
            return
        row = self.db.one("SELECT * FROM names WHERE username_key = ?", (key,))
        games = len(self.db.player_lines(key))
        if row:
            msg = (f"**{row['display_name']}** plays as `{row['roblox_username']}`"
                   + (f" (<@{row['discord_id']}>)" if row["discord_id"] else "") + f" · {games} games recorded")
        else:
            msg = f"`{key}` has {games} games recorded but no name mapped yet."
        await inter.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="me", description="Your own stats (needs your Discord account linked by an admin)")
    @app_commands.describe(season="Season (default: current; 0 = career)")
    async def me(self, inter: discord.Interaction, season: int | None = None):
        row = self.db.name_for_discord(inter.user.id)
        if not row:
            await inter.response.send_message("Your Discord account isn't linked to a Roblox username yet. "
                                              "Ask an admin to run `/admin setname` with your @.", ephemeral=True)
            return
        await self.player.callback(self, inter, row["roblox_username"], season)

    @app_commands.command(name="help", description="What this bot can do")
    async def help(self, inter: discord.Interaction):
        e = discord.Embed(title="NFL Universe stats bot", color=config.EMBED_COLOR, description=(
            "**Games**\n"
            "`/games` recent results · `/game` box score · `/standings` · `/h2h` team vs team\n"
            "**Players**\n"
            "`/player` stats · `/me` your stats · `/gamelog` · `/compare` two players\n"
            "**Leaderboards**\n"
            "`/leaders` any stat · `/fantasy` fantasy standings · `/points` fantasy breakdown · `/scoring`\n"
            "**Names**\n"
            "`/names` everyone's mapped name · `/whois` look someone up\n"
            "**Admins**\n"
            "`/admin …` manage names, games, stats, seasons and the webhook"))
        await inter.response.send_message(embed=e, ephemeral=True)
