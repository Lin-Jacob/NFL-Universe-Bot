"""
Optionally expose the local webhook server to the internet when the bot starts,
so no domain or port forwarding is needed.

TUNNEL=cloudflared  Cloudflare quick tunnel. Free, no account. The URL
                    (https://<random>.trycloudflare.com) changes every run.
TUNNEL=ngrok        ngrok. Free account gives one static domain, so the URL
                    stays the same between runs (set NGROK_DOMAIN).
TUNNEL=none         Don't start anything (you run a tunnel yourself, or forward a port).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import urllib.request

log = logging.getLogger("nflu.tunnel")

_TRYCF = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")


class Tunnel:
    def __init__(self, kind: str, port: int):
        self.kind = (kind or "none").lower()
        self.port = port
        self.proc: asyncio.subprocess.Process | None = None
        self.public_url: str | None = None
        self._reader: asyncio.Task | None = None

    async def start(self, timeout: float = 30) -> str | None:
        if self.kind in ("", "none", "off", "false"):
            self.public_url = os.getenv("PUBLIC_URL") or None
            return self.public_url
        if self.kind == "cloudflared":
            return await self._start_cloudflared(timeout)
        if self.kind == "ngrok":
            return await self._start_ngrok(timeout)
        log.error("Unknown TUNNEL=%s (use cloudflared, ngrok or none)", self.kind)
        return None

    async def _start_cloudflared(self, timeout: float) -> str | None:
        exe = os.getenv("CLOUDFLARED_PATH") or shutil.which("cloudflared")
        if not exe:
            log.error("TUNNEL=cloudflared but the 'cloudflared' program was not found. "
                      "Install it (see README) or set CLOUDFLARED_PATH.")
            return None
        self.proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        found = asyncio.get_running_loop().create_future()

        async def read():
            assert self.proc and self.proc.stdout
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                log.debug("cloudflared: %s", line)
                m = _TRYCF.search(line)
                if m and not found.done():
                    found.set_result(m.group(0))
            if not found.done():
                found.set_result(None)

        self._reader = asyncio.create_task(read())
        try:
            self.public_url = await asyncio.wait_for(found, timeout)
        except asyncio.TimeoutError:
            log.error("cloudflared did not report a URL within %ss", timeout)
        return self.public_url

    async def _start_ngrok(self, timeout: float) -> str | None:
        exe = os.getenv("NGROK_PATH") or shutil.which("ngrok")
        if not exe:
            log.error("TUNNEL=ngrok but the 'ngrok' program was not found. Install it (see README) or set NGROK_PATH.")
            return None
        args = [exe, "http", str(self.port), "--log", "stdout"]
        if os.getenv("NGROK_DOMAIN"):
            args.append(f"--url={os.getenv('NGROK_DOMAIN')}")
        env = os.environ.copy()
        if os.getenv("NGROK_AUTHTOKEN"):
            env["NGROK_AUTHTOKEN"] = os.environ["NGROK_AUTHTOKEN"]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env)
        # ngrok exposes its tunnels on a local API
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1)
            if self.proc.returncode is not None:
                log.error("ngrok exited (code %s). Check NGROK_AUTHTOKEN / NGROK_DOMAIN.", self.proc.returncode)
                return None
            try:
                data = await asyncio.to_thread(_get_json, "http://127.0.0.1:4040/api/tunnels")
                for t in data.get("tunnels", []):
                    if t.get("public_url", "").startswith("https://"):
                        self.public_url = t["public_url"]
                        return self.public_url
            except Exception:
                continue
        log.error("ngrok did not report a URL within %ss", timeout)
        return None

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._reader:
            self._reader.cancel()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read())
