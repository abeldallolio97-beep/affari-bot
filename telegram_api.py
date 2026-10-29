"""Piccolo client per le API di Telegram."""
import html
import os

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}"


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


MENU = {
    "keyboard": [
        ["/scan console", "/scan elettronica"],
        ["/scan casa", "/scan"],
        ["/ri", "/help"],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def send(chat_id, text, preview=False, keyboard=None):
    """Invia testo HTML, spezzandolo se supera il limite di Telegram.
    Con keyboard=MENU mostra i tasti fissi in basso (restano finché non li cambi)."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for i, c in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": c,
            "parse_mode": "HTML",
            "disable_web_page_preview": not preview,
        }
        if keyboard and i == len(chunks) - 1:
            payload["reply_markup"] = keyboard
        r = requests.post(f"{BASE}/sendMessage", json=payload, timeout=30)
        if not r.ok:  # riprova senza formattazione
            fallback = {"chat_id": chat_id, "text": html.unescape(c)}
            if "reply_markup" in payload:
                fallback["reply_markup"] = payload["reply_markup"]
            requests.post(f"{BASE}/sendMessage", json=fallback, timeout=30)


def get_updates(offset=None):
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(f"{BASE}/getUpdates", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("result", [])
