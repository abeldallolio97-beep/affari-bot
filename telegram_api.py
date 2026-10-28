"""Piccolo client per le API di Telegram."""
import html
import os

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def send(chat_id, text, preview=False):
    """Invia testo HTML, spezzandolo se supera il limite di Telegram."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        r = requests.post(
            f"{BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": c,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            },
            timeout=30,
        )
        if not r.ok:  # riprova senza formattazione
            requests.post(
                f"{BASE}/sendMessage",
                json={"chat_id": chat_id, "text": html.unescape(c)},
                timeout=30,
            )


def get_updates(offset=None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{BASE}/getUpdates", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("result", [])
