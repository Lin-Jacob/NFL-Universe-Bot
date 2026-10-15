# NFL Universe League Bot

A Discord bot for a Roblox **NFL Universe Football** league. It receives game results from the NFL Universe webhook, stores every game and stat line in a local SQLite database, scores fantasy points, maps Roblox usernames to real names, and gives your server slash commands for scores, standings, player stats and leaderboards.

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
   The console prints the public webhook URL, and the bot DMs it to every admin. `/admin webhookurl` shows it again any time.
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

The bot then reads every webhook post in that channel, parses the scores and stats from the message, saves the game, and reacts ✅. You don't need a tunnel in this mode (`TUNNEL=none`).

---

## 3. Webhook payload format

I couldn't find public documentation for the NFL Universe webhook's JSON, so the parser accepts many shapes and **every payload is stored raw before parsing**. If the real format turns out to be different, nothing is lost: look at what arrived, adjust the parser, then re-parse.

The shape it handles best (also in `samples/sample_game.json`):

```json
{
  "game_id": "unique-id-per-game",
  "season": 1,
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
| `/games [team] [season] [count]` | Recent results with game numbers |
| `/game [game_id]` | Full box score (latest game if no id) with player of the game |
| `/standings [season]` | W-L, win %, points for/against, point differential |
| `/h2h team_a team_b` | Head-to-head record and history |
| `/player name [season]` | Season or career (`season: 0`) stats, fantasy totals, best game |
| `/me [season]` | Your own stats (once an admin links your Discord account) |
| `/gamelog name [count]` | A player's recent games |
| `/compare player1 player2 [season]` | Side-by-side stats |
| `/leaders stat [season] [per_game]` | Top 10 in any stat, or fantasy points |
| `/fantasy [season] [week]` | Fantasy leaderboard |
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
| `/admin addgame`, `/admin editgame`, `/admin deletegame` | Record, fix or delete games by hand |
| `/admin setstat game_id roblox_username stat value` | Fix one stat on one player (0 removes it) |
| `/admin removeplayer` | Remove a player's line from a game |
| `/admin import file` | Import a game from a `.json` attachment (same format as the webhook) |
| `/admin rawlog`, `/admin raw`, `/admin reparse` | Inspect and re-process raw webhook deliveries |
| `/admin season number` | Set the current season (new games are filed under it unless the payload says otherwise) |
| `/admin channel [channel]` | Where final scores are posted automatically |
| `/admin announce game_id` | Re-post a game's result |
| `/admin recalc` | Reload `config.py` (scoring and admins) and recalculate all fantasy points |
| `/admin webhookurl` | Show the current webhook URL |
| `/admin admins` | List admins |

---

## 5. Fantasy scoring

Defaults are PPR offense plus IDP defense, because every NFL Universe player is a real person on the field:

- Passing: 1 pt / 25 yds, 4 per TD, −2 per INT thrown
- Rushing & receiving: 1 pt / 10 yds, 6 per TD, 1 per reception
- Fumble lost −2
- Defense: tackle 1, sack 2, INT 3, pass deflection 1, forced fumble 2, fumble recovery 2, defensive TD 6, safety 2
- Kicking: FG 3, XP 1, misses −1
- Bonuses: +3 for 300 passing yards, 100 rushing yards or 100 receiving yards

Change any of it in `config.py` (`FANTASY_SCORING`, `FANTASY_BONUSES`), then run `/admin recalc` to rescore every stored game.

---

## 6. Files

```
bot.py                     entry point: Discord bot + webhook server + tunnel
config.py                  ADMIN_IDS, fantasy scoring, default season   <- edit me
.env.example               copy to .env and fill in                     <- edit me
nflu/parser.py             webhook JSON -> game + player stats
nflu/stats.py              stat names and the aliases the parser accepts
nflu/db.py                 SQLite tables: games, player_games, names, raw_payloads, settings
nflu/fantasy.py            fantasy point calculation
nflu/analytics.py          totals, leaderboards, standings
nflu/commands.py           public slash commands
nflu/admin.py              /admin slash commands
nflu/webhook.py            aiohttp webhook server
nflu/tunnel.py             starts cloudflared / ngrok
samples/sample_game.json   example payload
scripts/send_test_webhook.py
tests/test_bot.py          run with:  pip install pytest && python -m pytest
```

The database lives at `data/nfl_universe.db`. Back it up by copying that file while the bot is stopped.
