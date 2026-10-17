"""
aiohttp server that receives NFL Universe webhook posts, runs in the bot's process.

Accepted URLs (all need the shared secret WEBHOOK_SECRET):
    POST /webhook/<secret>                    secret in the path (easiest: paste one URL into the game)
    POST /webhook   + header X-Webhook-Secret: <secret>   or   ?secret=<secret>
    POST /api/webhooks/<anything>/<secret>    looks like a Discord webhook URL, for games that
                                              only accept discord.com-style webhook links
    GET  /health                              liveness check, no secret needed
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Awaitable, Callable

from aiohttp import web

from .db import Database
from .parser import ParseError, ParsedGame, parse_payload, payload_hash

log = logging.getLogger("nflu.webhook")

MAX_BODY = 2 * 1024 * 1024  # 2 MB

OnGame = Callable[[int, ParsedGame, bool], Awaitable[None]]


def ingest(db: Database, body: str, remote: str | None, source: str = "webhook",
           message_id: int | None = None) -> dict:
    """Store + parse one payload. Shared by the HTTP handler, the feed channel and /admin import."""
    h = payload_hash(body.encode())
    raw_id, dup = db.store_raw(body, remote, h, message_id)
    if dup:
        row = db.raw(raw_id)
        return {"ok": True, "status": "duplicate", "raw_id": raw_id, "game_id": row["game_id"] if row else None}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        db.set_raw_status(raw_id, "failed", f"invalid JSON: {e}")
        return {"ok": False, "status": "failed", "raw_id": raw_id, "error": "invalid JSON"}
    return parse_raw(db, raw_id, payload, source)


def parse_raw(db: Database, raw_id: int, payload, source: str = "webhook") -> dict:
    try:
        game = parse_payload(payload)
    except ParseError as e:
        db.set_raw_status(raw_id, "failed", str(e))
        return {"ok": False, "status": "failed", "raw_id": raw_id, "error": str(e)}
    game_id, replaced = db.save_game(game, raw_id=raw_id, source=source)
    note = None
    if game.unknown_stats:
        note = "unrecognized stats kept in 'extra': " + ", ".join(sorted(game.unknown_stats))[:500]
    db.set_raw_status(raw_id, "parsed", note, game_id)
    return {"ok": True, "status": "replaced" if replaced else "parsed", "raw_id": raw_id,
            "game_id": game_id, "players": len(game.players), "game": game, "replaced": replaced}


def reparse_all(db: Database) -> dict[str, int]:
    """Parse every stored game payload again (after a parser change). Game numbers are kept."""
    counts: dict[str, int] = {}
    for r in db.q("SELECT id, body FROM raw_payloads WHERE status = 'parsed' ORDER BY id"):
        res = parse_raw(db, r["id"], json.loads(r["body"]))
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    return counts


class WebhookServer:
    def __init__(self, db: Database, secret: str,
                 host: str = "0.0.0.0", port: int = 8080, on_game: OnGame | None = None):
        if not secret or len(secret) < 12:
            raise ValueError("WEBHOOK_SECRET must be set in .env (at least 12 characters)")
        self.db = db
        self.secret = secret
        self.host, self.port = host, port
        self.on_game = on_game
        self.runner: web.AppRunner | None = None
        self.app = web.Application(client_max_size=MAX_BODY)
        self.app.add_routes([
            web.get("/health", self.health),
            web.get("/", self.health),
            web.post("/webhook", self.handle),
            web.post("/webhook/{secret}", self.handle),
            web.post("/api/webhooks/{hook_id}/{secret}", self.handle),
        ])

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        log.info("Webhook server listening on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "nfl-universe-bot"})

    def _authorized(self, request: web.Request) -> bool:
        given = (request.match_info.get("secret")
                 or request.headers.get("X-Webhook-Secret")
                 or request.query.get("secret")
                 or "")
        auth = request.headers.get("Authorization", "")
        if not given and auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return hmac.compare_digest(given.encode(), self.secret.encode())

    async def handle(self, request: web.Request) -> web.Response:
        remote = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.remote
        if not self._authorized(request):
            log.warning("Rejected webhook from %s (bad secret)", remote)
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        # Discord-style senders may use multipart with a payload_json field.
        if request.content_type.startswith("multipart/"):
            form = await request.post()
            body = str(form.get("payload_json") or "")
        else:
            body = await request.text()
        if not body.strip():
            return web.json_response({"ok": False, "error": "empty body"}, status=400)

        result = ingest(self.db, body, remote)
        game = result.pop("game", None)
        replaced = result.pop("replaced", False)
        log.info("Webhook from %s -> %s", remote, result)
        if game is not None and self.on_game:
            try:
                await self.on_game(result["game_id"], game, replaced)
            except Exception:
                log.exception("on_game callback failed")
        # Always 200 for authorized requests so the sender doesn't retry forever;
        # failures are visible with /admin rawlog.
        return web.json_response(result, status=200)
