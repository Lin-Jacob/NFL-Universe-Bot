"""Slash commands anyone in the server can use."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import config

from . import analytics
from .embeds import (Names, displayed, fmt_num, game_embed, player_card, score_line, short_line, stats_block, table,
                     when)
from .fantasy import breakdown
from .stats import POSITIONS, STATS, label
from .views import Pager

GAMES_PER_PAGE = 8

if TYPE_CHECKING:
    from bot import NFLUBot


def _rank(i: int) -> str:
    return ["🥇", "🥈", "🥉"][i] if i < 3 else f"**{i + 1}.**"


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

    async def _resolve(self, inter: discord.Interaction, query: str) -> str | None:
        key = self.db.resolve_player(query)
        if not key:
            await inter.response.send_message(f"I couldn't find a player called **{query}**.", ephemeral=True)
        return key

    # ------------------------------------------------------------------ games
    async def game_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        cur = current.strip().lstrip("#").lower()
        out = []
        for g in self.db.recent_games(limit=100000):
            label = f"#{g['id']} {score_line(g, bold_winner=False)} · {g['played_at'][:10]}"
            if cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=g["id"]))
            if len(out) >= 25:
                break
        return out

    @app_commands.command(name="games", description="Every game played, newest first, with date and time")
    async def games(self, inter: discord.Interaction):
        rows = self.db.recent_games(100000)
        if not rows:
            await inter.response.send_message("No games recorded yet.")
            return
        names = Names(self.db)
        per_page = GAMES_PER_PAGE
        pages = (len(rows) + per_page - 1) // per_page
        title = "Games played"

        def render(page: int) -> discord.Embed:
            out = []
            for g in rows[page * per_page:(page + 1) * per_page]:
                line = f"`#{g['id']}` {score_line(g)}\n🕒 {when(g['played_at'])}"
                players = self.db.game_players(g["id"])  # sorted by fantasy points
                if players and players[0]["fantasy_points"] > 0:
                    line += f" · ⭐ {names(players[0]['username'])} ({fmt_num(players[0]['fantasy_points'])} pts)"
                out.append(line)
            e = discord.Embed(title=title, color=config.EMBED_COLOR, description="\n\n".join(out)[:4000])
            e.set_footer(text=f"Page {page + 1}/{pages} · {len(rows)} games · "
                              "/inspect_game <#> for a game's full box score")
            return e

        await Pager(inter.user.id, pages, render).send(inter)

    @app_commands.command(name="inspect_game", description="Box score for one game, or a stat card per player (◀ ▶)")
    @app_commands.describe(game="Game number from /games (leave empty for the latest game)",
                           view="Simple: box score, ◀ ▶ flips games. Complex: a full stat card per player, "
                                "◀ ▶ flips players",
                           player="Complex view: start on this player")
    @app_commands.choices(view=[app_commands.Choice(name="Simple (box score)", value="simple"),
                                app_commands.Choice(name="Complex (player stat cards)", value="complex")])
    @app_commands.autocomplete(game=game_autocomplete, player=player_autocomplete)
    async def inspect_game(self, inter: discord.Interaction, game: int | None = None,
                           view: app_commands.Choice[str] | None = None, player: str | None = None):
        ids = [g["id"] for g in reversed(self.db.recent_games(limit=100000))]  # oldest -> newest, so ▶ = newer
        if not ids:
            await inter.response.send_message("No games recorded yet.")
            return
        if game is None:
            game = ids[-1]
        if game not in ids:
            await inter.response.send_message(f"There's no game #{game}. `/games` lists them all.", ephemeral=True)
            return

        if (view and view.value == "complex") or player:
            g = self.db.game(game)
            players = self.db.game_players(game)  # highest points first
            if not players:
                await inter.response.send_message(f"Game #{game} has no player stats.", ephemeral=True)
                return
            names = Names(self.db)
            start = 0
            if player:
                key = self.db.resolve_player(player)
                start = next((i for i, p in enumerate(players) if p["username_key"] == key), None)
                if start is None:
                    await inter.response.send_message(f"**{player}** didn't play in game #{game}.", ephemeral=True)
                    return

            def card(page: int) -> discord.Embed:
                e = player_card(g, players[page], page + 1, len(players), names)
                e.set_footer(text=f"Player {page + 1} of {len(players)} · ◀ ▶ to flip players · "
                                  "/inspect_game view:Simple for the box score")
                return e

            await Pager(inter.user.id, len(players), card, start=start).send(inter)
            return

        def render(page: int) -> discord.Embed:
            e = game_embed(self.db, ids[page])
            e.set_footer(text=f"{e.footer.text} · Game {page + 1} of {len(ids)} (◀ older · newer ▶)")
            return e

        await Pager(inter.user.id, len(ids), render, start=ids.index(game)).send(inter)

    # ---------------------------------------------------------------- players
    @app_commands.command(name="player", description="A player's stats and fantasy points")
    @app_commands.describe(name="Name, Roblox username, or @mention")
    @app_commands.autocomplete(name=player_autocomplete)
    async def player(self, inter: discord.Interaction, name: str):
        key = await self._resolve(inter, name)
        if not key:
            return
        summ = analytics.player_summary(self.db, key)
        names = Names(self.db)
        if not summ:
            await inter.response.send_message(f"**{names.full(key)}** has no games yet.")
            return
        e = discord.Embed(title=names.full(summ["username"]), color=config.EMBED_COLOR,
                          description=f"{summ['games']} games played")
        e.add_field(name="Fantasy", value=f"Total **{fmt_num(summ['fantasy_total'])}**\n"
                                          f"Per {config.FANTASY_RATE_SNAPS} snaps **{fmt_num(summ['fantasy_rate'])}**"
                                          f" ({fmt_num(summ['snaps'])} snaps)\n"
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
            f"`#{ln['game_id']}` {score_line(ln)} · "
            f"**{fmt_num(ln['fantasy_points'])} FP**\n{short_line(ln['stats'])}" for ln in lines)[:4000]
        await inter.response.send_message(embed=e)

    @app_commands.command(name="compare", description="Compare two players side by side")
    @app_commands.autocomplete(player1=player_autocomplete, player2=player_autocomplete)
    async def compare(self, inter: discord.Interaction, player1: str, player2: str):
        k1, k2 = self.db.resolve_player(player1), self.db.resolve_player(player2)
        if not k1 or not k2:
            await inter.response.send_message(f"Couldn't find **{player1 if not k1 else player2}**.", ephemeral=True)
            return
        a, b = analytics.player_summary(self.db, k1), analytics.player_summary(self.db, k2)
        if not a or not b:
            await inter.response.send_message("One of those players has no games yet.")
            return
        names = Names(self.db)
        keys = [k for k in STATS if displayed(k) and (a["totals"].get(k) or b["totals"].get(k))]
        rows = [["Games", a["games"], b["games"]],
                ["Fantasy", fmt_num(a["fantasy_total"]), fmt_num(b["fantasy_total"])],
                ["Snaps", fmt_num(a["snaps"]), fmt_num(b["snaps"])],
                [f"FP/{config.FANTASY_RATE_SNAPS} snaps", fmt_num(a["fantasy_rate"]), fmt_num(b["fantasy_rate"])],
                ["FP/game", fmt_num(a["fantasy_avg"]), fmt_num(b["fantasy_avg"])]]
        rows += [[label(k)[:18], fmt_num(a["totals"].get(k, 0)), fmt_num(b["totals"].get(k, 0))] for k in keys]
        e = discord.Embed(title=f"{names(a['username'])} vs {names(b['username'])}",
                          description=table(rows, ["", names(a["username"])[:14], names(b["username"])[:14]]),
                          color=config.EMBED_COLOR)
        await inter.response.send_message(embed=e)

    # ------------------------------------------------------------ leaderboards
    @app_commands.command(name="leaders", description="Stat leaders")
    @app_commands.describe(stat="Which stat", per_game="Rank by per-game average instead of totals")
    @app_commands.autocomplete(stat=stat_autocomplete)
    async def leaders(self, inter: discord.Interaction, stat: str, per_game: bool = False):
        if stat != "fantasy" and stat not in STATS:
            await inter.response.send_message("Pick a stat from the list.", ephemeral=True)
            return
        rows = analytics.leaders(self.db, stat, 10, per_game)
        if not rows:
            await inter.response.send_message("Nobody has recorded that stat yet.")
            return
        names = Names(self.db)
        stat_name = "Fantasy points" if stat == "fantasy" else label(stat)
        e = discord.Embed(title=f"{stat_name} leaders" + (" (per game)" if per_game else ""),
                          color=config.EMBED_COLOR,
                          description="\n".join(f"**{i + 1}.** {names(n)} · **{fmt_num(v)}** ({g} GP)"
                                                for i, (n, v, g) in enumerate(rows)))
        await inter.response.send_message(embed=e)

    @app_commands.command(name="fantasy", description="Fantasy leaderboard by points per snap (fair for late joiners)")
    async def fantasy(self, inter: discord.Interaction):
        ranked, short = analytics.fantasy_rates(self.db)
        if not ranked and not short:
            await inter.response.send_message("No fantasy points recorded yet.")
            return
        names = Names(self.db)
        per = config.FANTASY_RATE_SNAPS
        e = discord.Embed(title=f"Fantasy leaderboard · points per {per} snaps", color=config.EMBED_COLOR,
                          description="\n".join(
                              f"{_rank(i)} {names.dashed(r['username'])} · **{fmt_num(r['rate'])}**\n"
                              f"{fmt_num(r['points'])} pts in {fmt_num(r['snaps'])} snaps ({r['games']} GP)"
                              for i, r in enumerate(ranked[:15]))[:4000] or "Nobody has enough snaps yet.")
        if short:
            value = "\n".join(f"{names.dashed(r['username'])} · {fmt_num(r['rate'])} "
                              f"({fmt_num(r['points'])} pts, {fmt_num(r['snaps'])} snaps)" for r in short)
            e.add_field(name=f"Not enough snaps yet (under {config.FANTASY_MIN_SNAPS})", value=value[:1024],
                        inline=False)
        e.set_footer(text=f"Ranked by fantasy points per {per} snaps on the field, so joining a game late "
                          "doesn't cost you. A full game is about 130-145 snaps.")
        await inter.response.send_message(embed=e)

    @app_commands.command(name="stats", description="Position leaderboards (QB, RB, WR, DB, DL, ST) ranked by points")
    @app_commands.describe(position="Show the full ranking for one position")
    @app_commands.choices(position=[app_commands.Choice(name=f"{code} · {title}", value=code)
                                    for code, (title, _) in POSITIONS.items()])
    async def stats(self, inter: discord.Interaction, position: app_commands.Choice[str] | None = None):
        names = Names(self.db)

        def lines(rows: list[dict]) -> list[str]:
            return [f"{_rank(i)} {names(r['username'])} · **{fmt_num(r['points'])}** pts ({r['games']} GP)\n"
                    f"{short_line(r['stats'])}" for i, r in enumerate(rows)]

        if position:
            code = position.value
            rows = analytics.position_leaders(self.db, code, 15)
            e = discord.Embed(title=f"{code} · {POSITIONS[code][0]}", color=config.EMBED_COLOR,
                              description="\n".join(lines(rows))[:4000] or "No one has scored at this position yet.")
        else:
            e = discord.Embed(title="Position leaderboards", color=config.EMBED_COLOR,
                              description="Fantasy points earned at each position across all games. "
                                          "Use `/stats position:` for a full ranking.")
            for code, (title, _) in POSITIONS.items():
                value, out = "", lines(analytics.position_leaders(self.db, code, 5))
                for ln in out:  # keep whole entries within Discord's 1024-character field limit
                    if len(value) + len(ln) + 1 > 1024:
                        break
                    value += ln + "\n"
                e.add_field(name=f"{code} · {title}", value=value or "No one yet", inline=False)
        e.set_footer(text="Positions come from the NFL Universe stat sheet: Passing, Rushing, Receiving, "
                          "Cornerback, DLine, Special Teams")
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
        rows = [[s, fmt_num(v), f"{p:+.2f}"] for s, v, p in breakdown(line["stats"], line["pos_stats"])]
        names = Names(self.db)
        e = discord.Embed(title=f"{names(line['username'])} · {fmt_num(line['fantasy_points'])} FP",
                          description=f"Game #{line['game_id']}: {score_line(line)}\n" +
                                      table(rows, ["Stat", "Value", "Pts"]),
                          color=config.EMBED_COLOR)
        await inter.response.send_message(embed=e)

    @app_commands.command(name="scoring", description="Show the fantasy scoring rules")
    async def scoring(self, inter: discord.Interaction):
        e = discord.Embed(title="Fantasy scoring (points per unit, by position)", color=config.EMBED_COLOR,
                          description="Each stat counts toward the position group it was earned in. "
                                      "Stats not listed are worth 0.")
        for code, (title, section) in POSITIONS.items():
            rows = [[label(k), f"{v:+g}"] for k, v in config.FANTASY_SCORING.get(code, {}).items() if v]
            if rows:
                e.add_field(name=f"{code} · {title} ({section})", value=table(rows, ["Stat", "Pts"])[:1024],
                            inline=True)
        bonuses = [[f"{label(s)} ≥ {t:g}", f"{b:+g}"] for s, t, b in config.FANTASY_BONUSES]
        if bonuses:
            e.add_field(name="Single-game bonuses", value=table(bonuses, ["Stat", "Pts"])[:1024], inline=False)
        await inter.response.send_message(embed=e)

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
    async def me(self, inter: discord.Interaction):
        row = self.db.name_for_discord(inter.user.id)
        if not row:
            await inter.response.send_message("Your Discord account isn't linked to a Roblox username yet. "
                                              "Ask an admin to run `/admin setname` with your @.", ephemeral=True)
            return
        await self.player.callback(self, inter, row["roblox_username"])

    @app_commands.command(name="help", description="What this bot can do")
    async def help(self, inter: discord.Interaction):
        e = discord.Embed(title="NFL Universe stats bot", color=config.EMBED_COLOR,
                          description="Every NFL Universe game posted in the feed channel is saved automatically.")
        e.add_field(name="🏈 Games", inline=False, value=(
            "`/games` every game played with date & time (◀ ▶ to page)\n"
            "`/inspect_game [#]` full box score (latest if empty, ◀ ▶ for other games)\n"
            "`/inspect_game [#] view:Complex` a full stat card per player (◀ ▶ to flip players)"))
        e.add_field(name="📊 Leaderboards", inline=False, value=(
            "`/stats [position]` QB, RB, WR, DB, DL and ST rankings by points\n"
            "`/fantasy` fantasy leaderboard by points per 100 snaps (fair if you joined late)\n"
            "`/leaders stat` top 10 in any single stat"))
        e.add_field(name="👤 Players", inline=False, value=(
            "`/player name` a player's totals, fantasy points and best game\n"
            "`/gamelog name` their recent games · `/compare a b` side by side\n"
            "`/points name [game_id]` how their points were scored · `/me` your own stats"))
        e.add_field(name="ℹ️ Info", inline=False, value=(
            "`/scoring` fantasy scoring by position (INTs and missed FGs subtract)\n"
            "`/names` real names ↔ Roblox usernames · `/whois query` look someone up"))
        if inter.user.id in config.ADMIN_IDS:
            e.add_field(name="🔧 Admin (only you can see this)", inline=False, value=(
                "`/admin test` dry-run the latest game post (checks the feed works, saves nothing)\n"
                "`/admin backfill` import past game posts · `/admin reparseall` re-read saved games\n"
                "`/admin setname` / `removename` / `unmapped` player names\n"
                "`/admin addgame` / `editgame` / `deletegame` / `setstat` / `removeplayer` fix games "
                "(master admin only)\n"
                "`/admin intfault` move an INT's −2 to the player at fault, or excuse it · `intfaults` · "
                "`removeintfault`\n"
                "`/admin import` · `rawlog` · `raw` · `reparse` raw game data\n"
                "`/admin channel` · `announce` · `recalc` · `admins` · `webhookurl` settings"))
        await inter.response.send_message(embed=e, ephemeral=True)
