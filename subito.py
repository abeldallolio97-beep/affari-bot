"""Lettura degli annunci di Subito.it (pagine di ricerca e singolo annuncio).

Subito non ha API pubbliche: le pagine sono Next.js e contengono i dati in un
blocco JSON (__NEXT_DATA__). Qui lo si legge e lo si normalizza.
"""
import json
import re
import time

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
}

NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def page_url(base, n):
    if n <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}o={n}"


def fetch(session, url, retries=2):
    """Restituisce (status_code, testo). status 0 = errore di rete."""
    last = (0, "")
    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=25)
            last = (r.status_code, r.text)
            if r.status_code == 200:
                return last
            if r.status_code in (403, 429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            return last
        except requests.RequestException as e:  # rete
            last = (0, str(e))
            time.sleep(2)
    return last


def _to_int(v):
    try:
        return int(float(str(v).replace(",", ".")))
    except (ValueError, TypeError):
        return None


def _feature_text(feats):
    """Trasforma le 'features' dell'annuncio in testo 'Etichetta: valore'."""
    out = []
    if not isinstance(feats, dict):
        return ""
    for key, f in feats.items():
        if key == "/price" or not isinstance(f, dict):
            continue
        label = f.get("label") or key.strip("/")
        vals = f.get("values") or []
        txt = ", ".join(str(v.get("value")) for v in vals if isinstance(v, dict) and v.get("value"))
        if txt:
            out.append(f"{label}: {txt}")
    return "; ".join(out)


def normalize(d):
    urls = d.get("urls") or {}
    url = urls.get("default") if isinstance(urls, dict) else None
    url = url or d.get("url")
    feats = d.get("features") or {}
    price = None
    p = feats.get("/price") if isinstance(feats, dict) else None
    if isinstance(p, dict):
        vals = p.get("values") or []
        if vals and isinstance(vals[0], dict):
            price = _to_int(vals[0].get("key"))
            if price is None:
                m = re.search(r"\d[\d.]*", str(vals[0].get("value", "")))
                price = _to_int(m.group(0).replace(".", "")) if m else None
    images = []
    for im in d.get("images") or []:
        if isinstance(im, dict):
            u = im.get("scalable") or im.get("url")
            if u:
                images.append(u)
    geo = d.get("geo") or {}
    town = ""
    if isinstance(geo, dict):
        t = geo.get("town")
        town = t.get("value", "") if isinstance(t, dict) else ""
    dates = d.get("dates") or {}
    return {
        "id": str(d.get("urn") or url or d.get("subject")),
        "title": str(d.get("subject") or "").strip(),
        "url": url,
        "price": price,
        "body": str(d.get("body") or "").strip(),
        "features": _feature_text(feats),
        "images": images,
        "town": town,
        "date": dates.get("display") if isinstance(dates, dict) else None,
    }


def _walk(node, found):
    if isinstance(node, dict):
        if isinstance(node.get("subject"), str) and (
            isinstance(node.get("urls"), dict) or node.get("urn")
        ):
            found.append(node)
            return
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def extract_ads(html):
    """Estrae gli annunci da una pagina (ricerca o singolo annuncio)."""
    m = NEXT_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    raw = []
    _walk(data, raw)
    ads, seen = [], set()
    for d in raw:
        ad = normalize(d)
        if ad["url"] and ad["id"] not in seen:
            seen.add(ad["id"])
            ads.append(ad)
    return ads


def fetch_all(session, base_url, target=1000, max_pages=60, delay=1.5, progress=None):
    """Scarica pagine consecutive finché raggiunge `target` annunci o finiscono."""
    ads, seen = [], set()
    status_last = 200
    for n in range(1, max_pages + 1):
        status, html = fetch(session, page_url(base_url, n))
        status_last = status
        if status != 200:
            break
        page_ads = extract_ads(html)
        new = [a for a in page_ads if a["id"] not in seen]
        if not new:  # pagina vuota o ripetuta: finito
            break
        for a in new:
            seen.add(a["id"])
            ads.append(a)
        if progress and n % 10 == 0:
            progress(f"…pagina {n}, {len(ads)} annunci letti")
        if len(ads) >= target:
            break
        time.sleep(delay)
    return ads, status_last


def fetch_detail(session, url):
    """Pagina del singolo annuncio: descrizione completa e tutte le foto."""
    status, html = fetch(session, url)
    if status != 200:
        return None
    for ad in extract_ads(html):
        if ad["url"] and ad["url"].split("?")[0] == url.split("?")[0]:
            return ad
    ads = extract_ads(html)
    return ads[0] if ads else None
