# NFL Universe League Bot

A Discord bot for a Roblox **NFL Universe Football** league. It receives game results from the NFL Universe webhook, stores every game and stat line in a local SQLite database, scores fantasy points, maps Roblox usernames to real names, and gives your server slash commands for scores, player stats and leaderboards.

It runs entirely on your own computer. You do **not** need a domain: when the bot starts it can open a free tunnel so the game can reach it.

---

## 1. Quick start

1. **Install Python 3.10+** (python.org; on Windows tick "Add Python to PATH").
2. **Install the dependencies** in this folder:
   ```bash
   python -m venv .venv
   # Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Create the Discord bot** at <https://discord.com/developers/applications>:
   - New Application → **Bot** → *Reset Token* → copy the token.
   - **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *Send Messages*, *Embed Links*, *Attach Files*, *Add Reactions*, *Read Message History*. Open the URL and add the bot to your server.
4. **Copy `.env.example` to `.env`** and fill in:
   - `DISCORD_TOKEN`: the token from step 3.
   - `GUILD_ID`: your server ID (turn on Developer Mode, right-click the server → Copy Server ID). This makes slash commands show up instantly.
   - `WEBHOOK_SECRET`: a long random string. Generate one with
     `python -c "import secrets; print(secrets.token_urlsafe(24))"`
5. **Add yourself as an admin** in `config.py`:
   ```python
   ADMIN_IDS: list[int] = [
       123456789012345678,  # you
       234567890123456789,  # a co-commissioner
   ]
   ```
   (Right-click your name in Discord → Copy User ID.)
6. **Install a tunnel program** (see section 2), then run:
   ```bash
   python bot.py
   ```
   The console prints the public webhook URL (set `DM_ADMINS_WEBHOOK_URL=true` in `.env` to also have the bot DM it to every admin on startup). `/admin webhookurl` shows it again any time.
7. **Paste that URL into NFL Universe's webhook setting**, then pick a channel for results with `/admin channel`.

To test without playing a game: `python scripts/send_test_webhook.py` posts `samples/sample_game.json` to your running bot.

---

## 2. Hosting the webhook on your own computer (no domain)

The bot runs a small web server on `WEBHOOK_PORT` (default 8080). The game needs a public HTTPS address to reach it, and `TUNNEL` in `.env` decides how you get one. The bot starts and stops the tunnel itself, so it is only online while the bot is running.

| `TUNNEL=` | Account needed | URL stays the same? | Setup |
|---|---|---|---|
| `cloudflared` (default) | No | **No**, new URL every restart | Install cloudflared |
| `ngrok` | Free ngrok account | **Yes**, with your free static domain | Install ngrok, set `NGROK_AUTHTOKEN` and `NGROK_DOMAIN` |
| `none` | – | Yes | Forward the port on your router yourself, set `PUBLIC_URL` |

**Recommendation:** start with `cloudflared` to try it out. If re-pasting the URL into the game after every restart gets annoying, switch to `ngrok`, whose free plan includes one permanent domain.

### cloudflared
- Windows: `winget install --id Cloudflare.cloudflared`
- macOS: `brew install cloudflared`
- Linux: download from <https://github.com/cloudflare/cloudflared/releases>

If it isn't on your PATH, set `CLOUDFLARED_PATH` to the full path of the program.

### ngrok
1. Sign up at <https://ngrok.com> (free), install ngrok (`winget install ngrok.ngrok` / `brew install ngrok`).
2. Dashboard → *Your Authtoken* → put it in `NGROK_AUTHTOKEN`.
3. Dashboard → *Domains* → claim your free static domain (something like `yourname.ngrok-free.app`) → put it in `NGROK_DOMAIN`.
4. `TUNNEL=ngrok`. Your webhook URL is now `https://yourname.ngrok-free.app/webhook/<WEBHOOK_SECRET>` forever.

The bot uses ngrok v3's `--url` flag. If you have an old ngrok that doesn't know it, run `ngrok update`.

### Security
- The webhook only accepts posts that carry `WEBHOOK_SECRET`, either at the end of the URL (`/webhook/<secret>`), in an `X-Webhook-Secret` header, or as `?secret=`. Anything else gets `401`.
- Treat the full webhook URL like a password. If it leaks, change `WEBHOOK_SECRET` and restart.
- `.env` and the `data/` folder are git-ignored. Don't share them.

### Option B: the game only accepts a Discord webhook URL
Some Roblox games only let you paste a `https://discord.com/api/webhooks/...` link. If NFL Universe is like that:
1. In Discord: channel settings → Integrations → Webhooks → New Webhook → Copy URL. Paste **that** into the game.
2. Put that channel's ID in `FEED_CHANNEL_ID` in `.env`.
3. Developer Portal → your app → Bot → turn on **Message Content Intent**.

The bot then reads the posts from that webhook (set its ID as `NFLU_WEBHOOK_ID` in `config.py`), parses the scores and stats from the message, saves the game, and reacts ✅. You don't need a tunnel in this mode (`TUNNEL=none`).

NFL Universe posts each game as two messages: a `.csv` file with every player's stats, then a "Team A vs Team B" embed with the score. The bot pairs them up. The CSV doesn't say which team a player was on, so player stats are saved without a team. Games posted while the bot was offline can be imported with `/admin backfill`.

---

## 3. Webhook payload format

I couldn't find public documentation for the NFL Universe webhook's JSON, so the parser accepts many shapes and **every payload is stored raw before parsing**. If the real format turns out to be different, nothing is lost: look at what arrived, adjust the parser, then re-parse.

The shape it handles best (also in `samples/sample_game.json`):

```json
{
  "game_id": "unique-id-per-game",
  "week": 3,
  "timestamp": "2026-10-15T19:00:00Z",
  "home": { "name": "Ravens", "score": 28 },
  "away": { "name": "Bills",  "score": 21 },
  "players": [
    { "username": "QBKing_123", "team": "Ravens", "position": "QB",
      "stats": { "passing": { "completions": 18, "attempts": 25, "yards": 312, "tds": 3, "ints": 1 },
                 "rushing": { "carries": 4, "yards": 22, "tds": 0 } } }
  ]
}
```

It also understands:
- payloads wrapped in `data`, `game`, `payload`, `match` or `result`;
- `home_team` / `away_team` / `home_score` / `away_score`, or `"score": "28-21"`;
- a `teams` list (`[{name, score, players: [...]}, ...]`) or players keyed by username (`{"QBKing_123": {...}}`);
- flat stats on the player (`"Pass Yds": 312`, `"passing_tds": 3`, `"Comp/Att": "18/25"`). Key matching ignores case, spaces and punctuation, and knows common abbreviations (`yds`, `td`, `int`, `tkl`, `pd`, `ff` …);
- Discord-style messages (`content` + `embeds`) with a score like `Ravens 24 - 17 Bills` and stat lines like `Pass Yds: 250`.

Notes:
- A game with the same `game_id` posted again **replaces** the earlier one instead of duplicating it. An exactly identical payload is ignored.
- Numeric stats it doesn't recognise are kept in an `extra` column and listed in `/admin rawlog`, so you can add aliases for them in `nflu/stats.py`.
- Flat `interceptions` / `int` means interceptions **thrown**. Use `def_ints` or put it under `"defense": {...}` for interceptions caught.

### When a payload doesn't parse
1. `/admin rawlog` lists recent deliveries with their status and errors.
2. `/admin raw raw_id:<n>` downloads the exact JSON that arrived.
3. Add the new key names to `nflu/stats.py` (or adjust `nflu/parser.py`), restart, then `/admin reparse raw_id:<n>`.

---

## 4. Commands

**Everyone**

| Command | What it does |
|---|---|
| `/games` | Every game played, newest first, with the date and time (in each viewer's time zone) and top player. ◀ ▶ buttons page through long lists |
| `/inspect_game [game] [view] [player]` | One game (latest if empty). **Simple** (default): the box score, with ◀ ▶ flipping to older / newer games. **Complex**: a stat card per player showing the matchup, fantasy points, rank in the game, snaps and every stat they recorded grouped by position (QB/RB/WR/DB/DL/ST) with points per group, with ◀ ▶ flipping between players (best first). `player` starts the cards on that player |
| `/player name` | Career stats, fantasy totals, best game |
| `/me` | Your own stats (once an admin links your Discord account) |
| `/gamelog name [count]` | A player's recent games |
| `/compare player1 player2` | Side-by-side stats |
| `/leaders stat [per_game]` | Top 10 in any stat, or fantasy points |
| `/fantasy` | Fantasy leaderboard ranked by points per 100 snaps, so players who joined late aren't penalized. Players under `FANTASY_MIN_SNAPS` are listed separately |
| `/stats [position]` | Position leaderboards: QB (Passing), RB (Rushing), WR (Receiving), DB (Cornerback), DL (DLine), ST (Special Teams), ranked by fantasy points earned at that position |
| `/points name [game_id]` | How a player's fantasy points were scored |
| `/scoring` | Fantasy scoring rules |
| `/names`, `/whois query` | Who's who |
| `/help` | Command overview |

Anywhere a player name is asked for you can type a mapped real name, a Roblox username, or an @mention, and it autocompletes.

**Admins only** (Discord IDs in `ADMIN_IDS`):

| Command | What it does |
|---|---|
| `/admin setname roblox_username display_name [discord_user]` | Show a real name instead of the Roblox username; linking a Discord user enables `/me` |
| `/admin removename`, `/admin unmapped` | Remove a mapping / list usernames with no name yet |
| `/admin addgame`, `/admin editgame`, `/admin deletegame` | Record, fix or delete games by hand (**master admin only**, `MASTER_ADMIN_IDS` in `config.py`) |
| `/admin setstat game_id roblox_username stat value [position]`, `/admin removeplayer` | Fix one stat on one player (0 removes it), or remove a player from a game (**master admin only**) |
| `/admin import file` | Import a game from a `.json` attachment (same format as the webhook) |
| `/admin test` | Dry run: reads the latest NFL Universe game post in the feed channel and shows the teams, score and every player's points, plus whether it's saved yet. Also reports who sent the channel's last message, and warns if a game post came from a webhook other than `NFLU_WEBHOOK_ID`. Saves nothing |
| `/admin backfill` | Import every past NFL Universe game post in the feed channel (posts by `NFLU_WEBHOOK_ID` in `config.py`). Messages already saved are skipped by message ID, so it is safe to re-run |
| `/admin intfault game thrower [at_fault] [count] [note]` | Move an interception's −2 to the player at fault, or excuse it (leave `at_fault` empty) |
| `/admin intfaults [game]`, `/admin removeintfault fault_id` | List / undo interception rulings |
| `/admin rawlog`, `/admin raw`, `/admin reparse` | Inspect and re-process raw webhook deliveries |
| `/admin reparseall` | Re-read every saved game with the current parser (run after a bot update) |
| `/admin channel [channel]` | Where final scores are posted automatically |
| `/admin announce game_id` | Re-post a game's result |
| `/admin recalc` | Reload `config.py` (scoring and admins) and recalculate all fantasy points |
| `/admin webhookurl` | Show the current webhook URL |
| `/admin admins` | List admins |

---

## 5. Fantasy scoring

Scoring is set **per position group** in `FANTASY_SCORING` in `config.py`. Each stat counts toward the group whose section of NFL Universe's stat sheet it came from:

| Group | Stat sheet section | Defaults |
|---|---|---|
| QB | Passing | 1 pt / 25 yds, 4 per TD, −2 per interception charged |
| RB | Rushing | 1 pt / 10 yds, 6 per TD |
| WR | Receiving | 1 per reception, 1 pt / 10 yds, 6 per TD, −2 per interception an admin moved onto them |
| DB | Cornerback | tackle 1, INT 3, pass break-up 1, pick-six 6 |
| DL | DLine | tackle 1, sack 2, swat 1, forced fumble 2, fumble recovery 2, fumble TD 6, safety 2 |
| ST | Special Teams | FG made 3, **FG missed −2**, kick/punt return TD 6 |

Bonuses: +3 for 300 passing yards, 100 rushing yards or 100 receiving yards in one game.

Every column on the stat sheet is listed in its group, including drops, yards after catch, first downs, 3rd/4th down conversions, QB hits, pressures, tackles for loss, coverage allowed, punts inside the 10 and onside recoveries. Most start at 0. Give any of them points (or a negative number to subtract), then restart or run `/admin recalc`. Because groups are separate, the same stat can be worth different amounts. For example, set DL `tackles` to 2 and leave DB `tackles` at 1.

The penalties are interceptions charged (−2) and missed field goals (−2). Negative yardage never costs points. The stat sheet has no extra points or 2-point conversions, so those can't be scored. Averages and percentages (yards per carry, FG %) aren't stored, because they're worked out from other columns.

`/scoring` shows every non-zero value by group, and `/points` shows which group each point came from. `/admin setstat` takes an optional `position` for stats that exist in more than one group, such as tackles.

### Points per snap

`/fantasy` ranks players by fantasy points per 100 snaps, not by total points, so someone who joined a game late (fewer snaps) isn't penalized for it. Snaps come from NFL Universe's stat sheet: every section's Snaps column added up, which is the number of plays the player was on the field. A full game is about 130–145 snaps, so "per 100 snaps" reads like a game's score.

Players with fewer than `FANTASY_MIN_SNAPS` total snaps (default 50) are listed under "Not enough snaps yet" instead of ranked, so a few lucky plays can't top the board. Change both settings in `config.py`. `/player` and `/compare` also show points per 100 snaps.

### Interception faults

Every interception thrown costs the QB −2 (`int_charged` in `config.py`). When a pick wasn't the QB's fault, an admin can rule on it:

- `/admin intfault game thrower at_fault` moves the −2 to the player at fault, for example the receiver who tipped it. The QB gets the points back.
- `/admin intfault game thrower` with `at_fault` left empty excuses it, so nobody loses points.
- `count` applies the ruling to several of that QB's interceptions at once.
- `/admin intfaults [game]` lists rulings, and `/admin removeintfault fault_id` undoes one.

Rulings are stored separately from the game data, so they survive `/admin reparseall`. Deleting a game deletes its rulings. In `/stats`, a QB's own interceptions count against QB, and ones moved onto a player count against WR.

Change any of it in `config.py` (`FANTASY_SCORING`, `FANTASY_BONUSES`), then restart or run `/admin recalc` to rescore every stored game.

### Player names

To show a real name instead of a Roblox username, add it to `PLAYER_NAMES` in `config.py`:

```python
PLAYER_NAMES = {
    "Jacob": "roblox_user_759822057",   # real name: IGN
}
```

Restart the bot or run `/admin recalc`. Commands like `/player` then accept either the name or the IGN. `/admin setname` does the same thing from Discord, and can also link a Discord account for `/me`.

---

## 6. Files

```
bot.py                     entry point: Discord bot + webhook server + tunnel
config.py                  ADMIN_IDS, fantasy scoring, player names, NFL Universe webhook ID   <- edit me
.env.example               copy to .env and fill in                     <- edit me
nflu/parser.py             webhook JSON -> game + player stats
nflu/stats.py              stat names and the aliases the parser accepts
nflu/db.py                 SQLite tables: games, player_games, names, raw_payloads, settings
nflu/fantasy.py            fantasy point calculation
nflu/analytics.py          totals and leaderboards
nflu/commands.py           public slash commands
nflu/admin.py              /admin slash commands
nflu/webhook.py            aiohttp webhook server
nflu/tunnel.py             starts cloudflared / ngrok
samples/sample_game.json   example payload
scripts/send_test_webhook.py
tests/test_bot.py          run with:  pip install pytest && python -m pytest
```

The database lives at `data/nfl_universe.db`. Back it up by copying that file while the bot is stopped.
