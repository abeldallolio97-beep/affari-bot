"""Lettura di Vinted tramite Apify. Serve APIFY_TOKEN.

Usa l'actor lowlanddata/vinted-scraper, che cerca per parole (non legge un catalogo per numero).
Dal link di Vinted prende: search_text (cosa cercare), price_from / price_to e il paese (vinted.it -> it).
I campi di uscita vengono portati allo stesso formato degli annunci di Subito.
"""
import os
from urllib.parse import parse_qs, urlparse

import apify_source

ACTOR = os.getenv("VINTED_ACTOR", "lowlanddata~vinted-scraper")


def is_vinted(url):
    return "vinted." in (url or "")


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def build_payload(url, max_items):
    u = urlparse(url)
    q = parse_qs(u.query)
    domain = u.netloc.lower().replace("www.", "").replace("vinted.", "") or "it"
    payload = {
        "searchQuery": (q.get("search_text") or [""])[0],
        "domain": domain,
        "sortBy": "newest",
        "maxItems": max_items,
        "scrapeDetails": True,  # serve la descrizione per capire se l'oggetto è sigillato/funzionante
        "proxyConfiguration": {"useApifyProxy": True},
    }
    if q.get("price_from"):
        payload["priceMin"] = _num(q["price_from"][0])
    if q.get("price_to"):
        payload["priceMax"] = _num(q["price_to"][0])
    return payload


def normalize(it):
    url = it.get("url")
    if not url:
        return None
    cents = it.get("priceCents")
    price = round(cents / 100) if isinstance(cents, (int, float)) else None
    total = it.get("totalPriceCents")
    images = [u for u in (it.get("imageUrls") or []) if isinstance(u, str)]
    parts = [f"{k}: {it[k]}" for k in ("condition", "brand", "size", "color") if it.get(k)]
    if it.get("businessSeller"):
        parts.append("venditore professionale")
    if isinstance(total, (int, float)):
        parts.append(f"totale con protezione acquirente: {total / 100:.2f}€")
    parts.append("fonte: Vinted")
    return {
        "id": url,
        "title": str(it.get("title") or "").strip(),
        "url": url,
        "price": price,
        "body": str(it.get("description") or "").strip(),
        "features": "; ".join(parts),
        "images": images,
        "town": "",
        "date": None,
        "date_iso": None,
        "seller": None,
    }


def scrape(url, max_items, wait_minutes=40):
    payload = build_payload(url, max_items)
    if not payload["searchQuery"]:
        return [], 0.0, "il link di Vinted non ha una ricerca (search_text): il lettore cerca per parole, non per categoria"
    return apify_source.scrape(url, max_items, wait_minutes, actor=ACTOR, payload=payload, norm=normalize)
