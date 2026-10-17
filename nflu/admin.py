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
from .embeds import Names, fmt_num, game_embed, score_line, short_line, table
from .fantasy import fantasy_points
from .feed import is_nflu_csv, nflu_embed
from .parser import ParsedGame, ParseError, parse_payload
from .stats import POSITIONS, STATS, label
from .webhook import ingest, parse_raw, reparse_all

if TYPE_CHECKING:
    from bot import NFLUBot


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS or user_id in config.MASTER_ADMIN_IDS


# Commands that edit game data by hand: config.MASTER_ADMIN_IDS only
MASTER_ONLY = {"addgame", "editgame", "deletegame", "setstat", "removeplayer"}


class Admin(commands.GroupCog, group_name="admin", group_description="Admin-only league management"):
    def __init__(self, bot: "NFLUBot"):
        self.bot = bot
        self.db = bot.db
        super().__init__()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if not is_admin(inter.user.id):
            await inter.response.send_message("Only league admins can use this command.", ephemeral=True)
            return False
        if inter.command and inter.command.name in MASTER_ONLY and inter.user.id not in config.MASTER_ADMIN_IDS:
            await inter.response.send_message("Only the master admin can change game data by hand.", ephemeral=True)
            return False
        return True

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
    @app_commands.command(name="addgame", description="Manually record a game result (master admin)")
    async def addgame(self, inter: discord.Interaction, team_1: str, score_1: int, team_2: str, score_2: int,
                      week: int | None = None):
        g = ParsedGame(external_id=None, home_team=team_1, away_team=team_2, home_score=score_1,
                       away_score=score_2, week=week)
        gid, _ = self.db.save_game(g, source="manual")
        await self._ok(inter, f"Recorded game **#{gid}**. Add player stats with `/admin setstat game_id:{gid}`.")

    @app_commands.command(name="editgame", description="Fix a game's team names, score or week (master admin)")
    async def editgame(self, inter: discord.Interaction, game_id: int, team_1: str | None = None,
                       score_1: int | None = None, team_2: str | None = None, score_2: int | None = None,
                       week: int | None = None):
        ok = self.db.update_game(game_id, home_team=team_1, away_team=team_2, home_score=score_1,
                                 away_score=score_2, week=week)
        await self._ok(inter, f"Updated game #{game_id}." if ok else "Nothing changed (bad id or no fields given).")

    @app_commands.command(name="deletegame", description="Delete a game and all its player stats (master admin)")
    async def deletegame(self, inter: discord.Interaction, game_id: int, confirm: bool = False):
        g = self.db.game(game_id)
        if not g:
            await self._ok(inter, f"No game #{game_id}.")
            return
        if not confirm:
            await self._ok(inter, f"This deletes game #{game_id} ({g['home_team']} vs {g['away_team']}) and its stats. "
                                  f"Run again with `confirm: True` to go ahead.")
            return
        self.db.delete_game(game_id)
        await self._ok(inter, f"Deleted game #{game_id}.")

    @app_commands.command(name="setstat", description="Set one stat for a player in a game, 0 removes it (master admin)")
    @app_commands.describe(position="Which position group the stat counts toward (default: the first that scores it)")
    @app_commands.choices(position=[app_commands.Choice(name=f"{code} · {title}", value=code)
                                    for code, (title, _) in POSITIONS.items()])
    @app_commands.autocomplete(stat=stat_autocomplete)
    async def setstat(self, inter: discord.Interaction, game_id: int, roblox_username: str, stat: str,
                      value: float, position: app_commands.Choice[str] | None = None):
        if stat not in STATS:
            await self._ok(inter, "Pick a stat from the list.")
            return
        if not self.db.game(game_id):
            await self._ok(inter, f"No game #{game_id}.")
            return
        group = position.value if position else None
        fp = self.db.set_player_stat(game_id, roblox_username, stat, value, group)
        await self._ok(inter, f"`{roblox_username}` {label(stat)} = {fmt_num(value)}"
                              + (f" ({group})" if group else "") + f" in game #{game_id}. "
                              f"Fantasy points now **{fmt_num(fp)}**.")

    @app_commands.command(name="removeplayer", description="Remove a player's stat line from a game (master admin)")
    async def removeplayer(self, inter: discord.Interaction, game_id: int, roblox_username: str):
        ok = self.db.remove_player_from_game(game_id, roblox_username)
        await self._ok(inter, "Removed." if ok else "That player has no line in that game.")

    @app_commands.command(name="import", description="Import a game from a JSON file (same format as the webhook)")
    async def import_(self, inter: discord.Interaction, file: discord.Attachment):
        if file.size > 2 * 1024 * 1024:
            await self._ok(inter, "File too big (2 MB max).")
            return
        body = (await file.read()).decode("utf-8", errors="replace")
        res = ingest(self.db, body, f"discord:{inter.user.id}", source="import")
        if res.get("game_id") and res["status"] != "failed":
            await self.bot.announce_game(res["game_id"], res.get("replaced", False))
        await self._ok(inter, _result_text(res))

    @app_commands.command(name="test", description="Read the latest game post in the feed channel (dry run, saves nothing)")
    async def test(self, inter: discord.Interaction):
        if not self.bot.feed_channel_id:
            await self._ok(inter, "FEED_CHANNEL_ID isn't set in .env.")
            return
        channel = self.bot.get_channel(self.bot.feed_channel_id)
        if channel is None:
            await self._ok(inter, f"Can't see the feed channel <#{self.bot.feed_channel_id}>. "
                                  "Check the bot has View Channel there.")
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            recent = [m async for m in channel.history(limit=50)]  # newest first
        except discord.Forbidden:
            await inter.followup.send(f"No permission to read history in {channel.mention} "
                                      "(needs Read Message History).", ephemeral=True)
            return
        if not recent:
            await inter.followup.send(f"{channel.mention} has no messages.", ephemeral=True)
            return

        # The very last message, and whether the bot would treat it as NFL Universe's
        last = recent[0]
        from_game = last.author.id == config.NFLU_WEBHOOK_ID
        kind = "CSV stats file" if is_nflu_csv(last) else "score message" if nflu_embed(last) else "other message"
        lines = [f"**Last message in {channel.mention}:** {kind} by **{last.author}** (ID `{last.author.id}`) "
                 f"{discord.utils.format_dt(last.created_at, 'R')} · [jump]({last.jump_url})"]
        if from_game:
            lines.append("✅ From the NFL Universe webhook (`NFLU_WEBHOOK_ID`).")
        elif last.webhook_id and (nflu_embed(last) or is_nflu_csv(last)):
            lines.append(f"⚠️ Looks like an NFL Universe post, but from a different webhook than `NFLU_WEBHOOK_ID` "
                         f"(`{config.NFLU_WEBHOOK_ID}`). The bot ignores it. Put `{last.author.id}` in config.py.")
        else:
            lines.append("Not from the NFL Universe webhook, so the bot ignores it.")

        # Latest score message from the game, plus the CSV posted just before it
        idx = next((i for i, m in enumerate(recent)
                    if m.author.id == config.NFLU_WEBHOOK_ID and nflu_embed(m) is not None), None)
        if idx is None:
            lines.append(f"\nNo NFL Universe score message in the last {len(recent)} messages.")
            await inter.followup.send("\n".join(lines), ephemeral=True)
            return
        post = recent[idx]
        csv_msg = next((m for m in recent[idx + 1:idx + 4]
                        if m.author.id == config.NFLU_WEBHOOK_ID and is_nflu_csv(m)), None)
        csv_text = None
        if csv_msg and 0 <= (post.created_at - csv_msg.created_at).total_seconds() <= 30:
            att = next(a for a in csv_msg.attachments if a.filename.lower().endswith(".csv"))
            csv_text = (await att.read()).decode("utf-8", errors="replace")
        try:
            game = parse_payload({"nfl_universe": {"embed": nflu_embed(post), "csv": csv_text}})
        except ParseError as e:
            lines.append(f"\n❌ Latest score message ([jump]({post.jump_url})) couldn't be read: {e}")
            await inter.followup.send("\n".join(lines), ephemeral=True)
            return

        saved = self.db.has_message(post.id)
        e = discord.Embed(title=f"Dry run: {game.home_team} {game.home_score} vs {game.away_team} {game.away_score}",
                          color=config.EMBED_COLOR, url=post.jump_url,
                          description=f"Posted {discord.utils.format_dt(post.created_at, 'f')}\n"
                                      f"Stats CSV: {'found ✅' if csv_text else 'not found ❌ (score only)'}\n"
                                      f"Saved in the database: {'yes ✅' if saved else 'no (run /admin backfill)'}")
        names = Names(self.db)
        rows = []
        for p in game.players:
            stats, pos = dict(p.stats), {g: dict(st) for g, st in p.pos_stats.items()}
            if stats.get("interceptions"):  # what /admin intfault rulings start from
                stats["int_charged"] = stats["interceptions"]
                if pos:
                    pos.setdefault("QB", {})["int_charged"] = stats["interceptions"]
            rows.append((fantasy_points(stats, pos), p))
        rows.sort(key=lambda r: r[0], reverse=True)
        body, shown = "", 0
        for fp, p in rows:  # whole entries only, within Discord's 1024-character field limit
            entry = (f"**{names(p.username)}** · {fmt_num(fp)} pts · {fmt_num(p.extra.get('Snaps', 0))} snaps\n"
                     f"{short_line(p.stats)}\n")
            if len(body) + len(entry) > 1000:
                body += f"…and {len(rows) - shown} more"
                break
            body, shown = body + entry, shown + 1
        e.add_field(name=f"Players read ({len(rows)})", value=body or "None", inline=False)
        await inter.followup.send("\n".join(lines), embed=e, ephemeral=True)

    @app_commands.command(name="backfill", description="Import every past NFL Universe game post in the feed channel")
    async def backfill(self, inter: discord.Interaction):
        if not self.bot.feed_channel_id:
            await self._ok(inter, "FEED_CHANNEL_ID isn't set in .env.")
            return
        channel = self.bot.get_channel(self.bot.feed_channel_id)
        if channel is None:
            await self._ok(inter, f"Can't see the feed channel <#{self.bot.feed_channel_id}>.")
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        # Discord can't filter history by author, so read the channel oldest first, but only from when
        # the NFL Universe webhook was created (its ID encodes that time); nothing older can be from it.
        # Each game is a CSV message followed by its score message. Already-saved posts (by message ID)
        # are skipped, so this is safe to re-run.
        games = 0
        counts: dict[str, int] = {}
        csv_message = None
        async for m in channel.history(limit=None, after=discord.Object(id=config.NFLU_WEBHOOK_ID),
                                       oldest_first=True):
            if m.author.id != config.NFLU_WEBHOOK_ID:
                continue
            if is_nflu_csv(m):
                csv_message = m
            elif nflu_embed(m) is not None:
                games += 1
                res = await self.bot.ingest_game_post(m, csv_message, live=False)
                csv_message = None
                counts[res["status"]] = counts.get(res["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to import"
        await inter.followup.send(f"Found {games} NFL Universe games in {channel.mention}: {summary}. "
                                  "`/admin rawlog` shows any failures.", ephemeral=True)

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
        res = parse_raw(self.db, raw_id, payload)
        await self._ok(inter, _result_text(res))

    # ---------------------------------------------------- interception faults
    async def _game_ac(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        cur = current.strip().lstrip("#").lower()
        out = []
        for g in self.db.recent_games(limit=100000):
            label = f"#{g['id']} {score_line(g, bold_winner=False)} · {g['played_at'][:10]}"
            if cur in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=g["id"]))
            if len(out) >= 25:
                break
        return out

    async def _game_player_ac(self, inter: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Players in the game picked in the 'game' option, ones who threw interceptions first."""
        game_id = getattr(inter.namespace, "game", None)
        if not game_id:
            return []
        names = Names(self.db)
        players = self.db.game_players(int(game_id))
        players.sort(key=lambda p: -(p["stats"].get("interceptions") or 0))
        out = []
        for p in players:
            ints = int(p["stats"].get("interceptions") or 0)
            label = names.full(p["username"]) + (f" · {ints} INT thrown" if ints else "")
            if current.lower() in label.lower():
                out.append(app_commands.Choice(name=label[:100], value=p["username"]))
        return out[:25]

    @app_commands.command(name="intfault", description="Move an interception's -pts to the player at fault, or excuse it")
    @app_commands.describe(game="Game number", thrower="Player who threw the interception",
                           at_fault="Player who loses the points instead (leave empty to excuse it: nobody loses points)",
                           count="How many of their interceptions this applies to", note="Optional reason")
    @app_commands.autocomplete(game=_game_ac, thrower=_game_player_ac, at_fault=_game_player_ac)
    async def intfault(self, inter: discord.Interaction, game: int, thrower: str, at_fault: str | None = None,
                       count: app_commands.Range[int, 1, 20] = 1, note: str | None = None):
        g = self.db.game(game)
        if not g:
            await self._ok(inter, f"No game #{game}.")
            return
        in_game = {p["username_key"]: p["username"] for p in self.db.game_players(game)}
        # Accept a Roblox username (what autocomplete fills in) or a mapped real name
        thrower = thrower if thrower.lower() in in_game else (self.db.resolve_player(thrower) or thrower)
        if at_fault and at_fault.lower() not in in_game:
            at_fault = self.db.resolve_player(at_fault) or at_fault
        t_key = thrower.lower()
        if t_key not in in_game:
            await self._ok(inter, f"`{thrower}` didn't play in game #{game}.")
            return
        free = self.db.int_fault_free(game, t_key)
        if count > free:
            await self._ok(inter, f"{in_game[t_key]} has {free} interception(s) in game #{game} that haven't been "
                                  "ruled on yet. `/admin intfaults` shows existing rulings.")
            return
        c_key = at_fault.lower() if at_fault else None
        if c_key and c_key not in in_game:
            await self._ok(inter, f"`{at_fault}` didn't play in game #{game}.")
            return
        if c_key == t_key:
            await self._ok(inter, "Interceptions are already charged to the thrower. Pick someone else, "
                                  "or leave `at_fault` empty to excuse it.")
            return
        fid = self.db.add_int_fault(game, t_key, c_key, count, note, inter.user.id)
        names = Names(self.db)
        # The thrower's INTs score under QB; INTs moved onto someone score under WR
        pts = abs(config.FANTASY_SCORING.get("QB", {}).get("int_charged", 0)) * count
        cost = abs(config.FANTASY_SCORING.get("WR", {}).get("int_charged", 0)) * count
        what = (f"moved to **{names.full(in_game[c_key])}** (−{fmt_num(cost)} for them)" if c_key
                else "**excused** (nobody loses points)")
        await self._ok(inter, f"Ruling #{fid}: {count} interception(s) thrown by **{names.full(in_game[t_key])}** "
                              f"in game #{game} {what}. {names(in_game[t_key])} gets {fmt_num(pts)} points back.\n"
                              f"Undo with `/admin removeintfault fault_id:{fid}`.")

    @app_commands.command(name="intfaults", description="List interception fault rulings")
    @app_commands.describe(game="Only this game")
    @app_commands.autocomplete(game=_game_ac)
    async def intfaults(self, inter: discord.Interaction, game: int | None = None):
        rows = self.db.int_faults(game)
        if not rows:
            await self._ok(inter, "No interception rulings" + (f" for game #{game}." if game else " yet."))
            return
        names = Names(self.db)
        spelled = {u.lower(): u for u in self.db.known_usernames()}  # rulings store lowercase keys
        who = lambda key: names(spelled.get(key, key))[:16]  # noqa: E731
        t = table([[r["id"], f"#{r['game_id']}", who(r["thrower_key"]),
                    who(r["charged_key"]) if r["charged_key"] else "excused", r["count"],
                    (r["note"] or "")[:20]] for r in rows], ["ID", "Game", "Thrower", "Charged to", "INTs", "Note"])
        await self._ok(inter, t)

    @app_commands.command(name="removeintfault", description="Undo an interception fault ruling")
    @app_commands.describe(fault_id="Ruling ID from /admin intfaults")
    async def removeintfault(self, inter: discord.Interaction, fault_id: int):
        game_id = self.db.remove_int_fault(fault_id)
        await self._ok(inter, f"Removed ruling #{fault_id}; game #{game_id} rescored." if game_id
                       else f"No ruling #{fault_id}.")

    @app_commands.command(name="reparseall", description="Parse every stored game again (after a bot update)")
    async def reparseall(self, inter: discord.Interaction):
        counts = reparse_all(self.db)
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no stored games"
        await self._ok(inter, f"Re-parsed stored games: {summary}.")

    # --------------------------------------------------------------- settings
    @app_commands.command(name="channel", description="Channel where new game results are posted")
    async def channel(self, inter: discord.Interaction, channel: discord.TextChannel | None = None):
        self.db.set_setting("announce_channel", str(channel.id) if channel else None)
        await self._ok(inter, f"Game results will be posted in {channel.mention}." if channel
                       else "Game results won't be posted anywhere.")

    @app_commands.command(name="recalc", description="Reload config.py (scoring, player names) and rescore every game")
    async def recalc(self, inter: discord.Interaction):
        import importlib
        importlib.reload(config)
        n = self.bot.apply_config()
        await self._ok(inter, f"Reloaded config.py ({len(config.PLAYER_NAMES)} player names) "
                              f"and recalculated {n} stat lines.")

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
