"""
Send a sample game to your running bot to check the whole pipeline.

    python scripts/send_test_webhook.py                      # posts samples/sample_game.json to localhost
    python scripts/send_test_webhook.py https://xyz.trycloudflare.com   # through your tunnel
    python scripts/send_test_webhook.py http://localhost:8080 my_game.json
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

root = Path(__file__).resolve().parent.parent
load_dotenv(root / ".env")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
base = args[0] if args else f"http://localhost:{os.getenv('WEBHOOK_PORT', '8080')}"
file = Path(args[1]) if len(args) > 1 else root / "samples" / "sample_game.json"
payload = json.loads(file.read_text())
# vary the id so repeated tests create new games instead of replacing the same one
if "--new" in sys.argv:
    import uuid
    payload["game_id"] = str(uuid.uuid4())

req = urllib.request.Request(f"{base.rstrip('/')}/webhook/{os.environ['WEBHOOK_SECRET']}",
                             data=json.dumps(payload).encode(), method="POST",
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    print(r.status, r.read().decode())
