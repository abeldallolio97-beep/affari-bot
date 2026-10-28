"""Lettura di Subito tramite Apify (Subito blocca i server di GitHub).

Usa l'actor pubblico epicscrapers/subito-scraper (a pagamento, ~0,70 $ ogni 1000 annunci).
Serve il secret APIFY_TOKEN.
"""
import os
import time

import requests

TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
ACTOR = os.getenv("APIFY_ACTOR", "epicscrapers~subito-scraper")
MAX_USD = os.getenv("APIFY_MAX_USD", "3")  # tetto di spesa per singola lettura
API = "https://api.apify.com/v2"


def enabled():
    return bool(TOKEN)


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _features_text(f):
    if isinstance(f, dict):
        return "; ".join(f"{k}: {v}" for k, v in f.items() if v not in (None, ""))
    return ""


def normalize(it):
    price = it.get("price")
    if isinstance(price, dict):
        price = _to_int(price.get("value"))
    else:
        price = _to_int(price)
    geo = it.get("geo") or {}
    pub = it.get("publication_date") or {}
    images = [u for u in (it.get("images") or []) if isinstance(u, str)]
    if not images and it.get("mainImage"):
        images = [it["mainImage"]]
    url = it.get("page_url")
    return {
        "id": url or str(it.get("title")),
        "title": str(it.get("title") or "").strip(),
        "url": url,
        "price": price,
        "body": str(it.get("description") or "").strip(),
        "features": _features_text(it.get("features")),
        "images": images,
        "town": geo.get("city", "") if isinstance(geo, dict) else "",
        "date": pub.get("local") if isinstance(pub, dict) else None,
        "seller": it.get("advertiser_type"),
    }


def scrape(url, max_items, wait_minutes=40):
    """Lancia l'actor sul link di ricerca e restituisce (annunci, costo_usd, errore)."""
    body = {
        "startUrls": [url],
        "maxItemsPerQuery": max_items,
        "enrichPhone": False,
        "enrichShop": False,
        "enrichUserAccount": False,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    try:
        r = requests.post(
            f"{API}/acts/{ACTOR}/runs",
            params={"token": TOKEN, "maxTotalChargeUsd": MAX_USD},
            json=body,
            timeout=60,
        )
    except requests.RequestException as e:
        return [], 0.0, f"rete: {e}"
    if r.status_code not in (200, 201):
        return [], 0.0, f"Apify HTTP {r.status_code}: {r.text[:200]}"
    run = r.json()["data"]
    run_id, dataset = run["id"], run["defaultDatasetId"]
    deadline = time.time() + wait_minutes * 60
    status, cost = run.get("status"), 0.0
    while time.time() < deadline:
        try:
            s = requests.get(f"{API}/actor-runs/{run_id}", params={"token": TOKEN}, timeout=30).json()["data"]
        except (requests.RequestException, KeyError, ValueError):
            time.sleep(10)
            continue
        status, cost = s.get("status"), float(s.get("usageTotalUsd") or 0)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(10)
    # anche una lettura interrotta può avere restituito annunci parziali
    try:
        d = requests.get(
            f"{API}/datasets/{dataset}/items",
            params={"token": TOKEN, "clean": "true", "format": "json", "limit": 5000},
            timeout=120,
        )
        items = d.json() if d.ok else []
    except (requests.RequestException, ValueError):
        items = []
    ads, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        a = normalize(it)
        if a["url"] and a["id"] not in seen:
            seen.add(a["id"])
            ads.append(a)
    err = None if status == "SUCCEEDED" else f"stato Apify: {status}"
    return ads, cost, err
