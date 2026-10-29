"""Lettura di Vinted (catalogo) tramite Apify. Serve APIFY_TOKEN.

Usa l'actor studio-amba/vinted-scraper, che accetta direttamente il link di un catalogo.
I campi di uscita vengono portati allo stesso formato degli annunci di Subito.
"""
import os

import apify_source

ACTOR = os.getenv("VINTED_ACTOR", "studio-amba~vinted-scraper")


def is_vinted(url):
    return "vinted." in (url or "")


def _num(v):
    try:
        return int(round(float(str(v).replace(",", "."))))
    except (TypeError, ValueError):
        return None


def normalize(it):
    price = it.get("price")
    if isinstance(price, dict):
        price = price.get("amount") or price.get("value")
    url = it.get("url")
    if not url:
        return None
    images = [u for u in (it.get("imageUrls") or []) if isinstance(u, str)]
    if not images and it.get("imageUrl"):
        images = [it["imageUrl"]]
    feats = "; ".join(
        f"{k}: {it[k]}" for k in ("condition", "brand", "size", "color", "category") if it.get(k)
    )
    return {
        "id": url,
        "title": str(it.get("title") or "").strip(),
        "url": url,
        "price": _num(price),
        "body": str(it.get("description") or "").strip(),
        "features": feats + ("; fonte: Vinted (il prezzo non include la commissione di protezione acquirente)" if feats else "fonte: Vinted"),
        "images": images,
        "town": it.get("sellerCity") or "",
        "date": it.get("uploadDate") or it.get("scrapedAt"),
        "date_iso": it.get("uploadDate") or None,
        "seller": it.get("sellerName"),
    }


def scrape(url, max_items, wait_minutes=40):
    payload = {
        "startUrls": [url],
        "maxResults": max_items,
        "proxyConfiguration": {"useApifyProxy": True},
    }
    return apify_source.scrape(url, max_items, wait_minutes, actor=ACTOR, payload=payload, norm=normalize)
