"""Bot affari Subito.it -> Telegram.

Uso:
  python bot.py poll   # legge i comandi Telegram (/scan, /test, /help) ed esegue
  python bot.py scan   # scansione completa subito (serve TELEGRAM_CHAT_ID)
  python bot.py test   # prova di lettura di Subito (non serve l'AI)
"""
import os
import re
import sys
import time
from datetime import datetime, timezone

import apify_source
import subito
import telegram_api as tg
from telegram_api import esc

CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- criteri (modificabili dai secrets/variabili del workflow) ---
MIN_MARGIN = int(os.getenv("MIN_MARGIN", "100"))          # margine netto minimo probabile
MIN_RELIABILITY = int(os.getenv("MIN_RELIABILITY", "50"))  # affidabilità minima %
MIN_REFS = int(os.getenv("MIN_REFS", "3"))                 # riferimenti di prezzo minimi
MIN_PRICE = int(os.getenv("MIN_PRICE", "40"))              # scarta sotto questo prezzo
MAX_PRICE = int(os.getenv("MAX_PRICE", "2500"))            # scarta sopra (capitale)
MIN_VALUE = int(os.getenv("MIN_VALUE", "150"))             # scarta se rivendita stimata sotto
TARGET_ADS = int(os.getenv("TARGET_ADS", "1000"))          # annunci per link
MAX_FINALISTS = int(os.getenv("MAX_FINALISTS", "12"))      # analisi a fondo per scansione
MAX_USD_PER_SCAN = float(os.getenv("MAX_USD_PER_SCAN", "2"))  # tetto di spesa per scansione
TRIAGE_MARGIN = int(os.getenv("TRIAGE_MARGIN", "90"))      # soglia larga del triage
MAX_AGE_DAYS = float(os.getenv("MAX_AGE_DAYS", "3"))        # ignora annunci più vecchi di N giorni

BUYER_WORDS = re.compile(r"\b(cerco|cercasi|compro|acquisto|ritiro usato)\b", re.I)


def load_links(only=None):
    links = []
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "links.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            name, url = [x.strip() for x in line.split("|", 1)]
            if only and only.lower() not in name.lower():
                continue
            links.append((name, url))
    return links


# ----------------------------------------------------------------------- test
def run_test_apify(chat):
    name, url = load_links()[0]
    tg.send(chat, f"🔎 Test lettura via Apify su «{esc(name)}» (30 annunci, costo ~0,03 $)…")
    ads, cost, err = apify_source.scrape(url, 30, wait_minutes=10)
    lines = [f"Annunci letti: <b>{len(ads)}</b> · costo ${cost:.3f}" + (f" · ⚠️ {esc(err)}" if err else "")]
    with_desc = sum(1 for a in ads if a["body"])
    with_img = sum(1 for a in ads if a["images"])
    with_price = sum(1 for a in ads if a["price"])
    lines.append(f"Con prezzo: {with_price} · con descrizione: {with_desc} · con foto: {with_img}")
    for a in ads[:5]:
        lines.append(f"• {esc(a['title'][:60])} — {a['price']}€ · {esc(a['date'])} ({len(a['images'])} foto)")
    tg.send(chat, "\n".join(lines))


def run_test(chat):
    if apify_source.enabled():
        return run_test_apify(chat)
    s = subito.make_session()
    lines = ["🔎 <b>Test lettura Subito</b>"]
    for name, url in load_links():
        status, html = subito.fetch(s, url)
        ads = subito.extract_ads(html) if status == 200 else []
        has_json = bool(subito.NEXT_RE.search(html or ""))
        lines.append(f"\n<b>{esc(name)}</b>\nHTTP {status} · JSON trovato: {'sì' if has_json else 'no'} · annunci letti: {len(ads)}")
        for a in ads[:3]:
            lines.append(f"• {esc(a['title'][:60])} — {a['price']}€ ({len(a['images'])} foto)")
        if status == 200 and ads:
            time.sleep(1.5)
            st2, html2 = subito.fetch(s, subito.page_url(url, 2))
            ads2 = subito.extract_ads(html2) if st2 == 200 else []
            ids1 = {a["id"] for a in ads}
            new2 = [a for a in ads2 if a["id"] not in ids1]
            lines.append(f"Pagina 2: HTTP {st2}, annunci nuovi: {len(new2)} → paginazione {'OK ✅' if new2 else 'NON funziona ❌'}")
        elif status != 200:
            lines.append("⚠️ Subito non risponde da questo server (possibile blocco).")
    tg.send(chat, "\n".join(lines))


# ----------------------------------------------------------------------- scan
def age_days(a):
    """Età dell'annuncio in giorni (None se la data non è nota)."""
    try:
        d = datetime.fromisoformat(a["date_iso"].replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds() / 86400
    except (KeyError, AttributeError, ValueError, TypeError):
        return None


def age_stats(ads):
    ages = [x for x in (age_days(a) for a in ads) if x is not None]
    if not ages:
        return "età annunci: n/d"
    ages.sort()
    h24 = sum(1 for x in ages if x <= 1)
    return (f"età: {h24} nelle ultime 24h, {sum(1 for x in ages if x <= 3)} negli ultimi 3 giorni; "
            f"il più vecchio ha {ages[-1]:.1f} giorni")


def passes_rules(a):
    if not a["price"] or a["price"] < MIN_PRICE or a["price"] > MAX_PRICE:
        return False
    age = age_days(a)
    if age is not None and age > MAX_AGE_DAYS:
        return False
    if BUYER_WORDS.search(a["title"]):
        return False
    return True


def validate(v, price):
    """Applica i criteri dell'utente al verdetto dell'AI. Ritorna (ok, v)."""
    try:
        refs = v.get("references") or []
        n_refs = len([r for r in refs if isinstance(r, dict) and r.get("price_eur")])
        rel = int(v.get("reliability_pct") or 0)
        if n_refs < MIN_REFS:
            rel = min(rel, 50)
        v["reliability_pct"] = rel
        probable = float(v.get("margin_probable_eur") or 0)
        worst = float(v.get("margin_worst_eur") or 0)
        ok = (
            str(v.get("verdict", "")).upper() == "BUY"
            and probable >= MIN_MARGIN
            and worst >= 0
            and rel >= MIN_RELIABILITY
        )
        return ok, v
    except (TypeError, ValueError):
        return False, v


def format_deal(rank, ad, v):
    refs = ", ".join(
        f"{esc(r.get('source'))} {r.get('price_eur')}€"
        for r in (v.get("references") or [])
        if isinstance(r, dict)
    )
    out = [
        f"<b>#{rank} {esc(ad['title'][:80])}</b>",
        f"💰 Prezzo <b>{ad['price']}€</b> → rivendita ~<b>{v.get('resale_estimate_eur')}€</b>",
        f"📈 Margine netto probabile <b>{round(float(v.get('margin_probable_eur', 0)))}€</b> · caso peggiore {round(float(v.get('margin_worst_eur', 0)))}€",
        f"🎯 Affidabilità {v.get('reliability_pct')}% · {esc(ad.get('town'))} · {esc(ad.get('date'))}",
        f"📊 Riferimenti: {refs or 'n/d'}",
    ]
    if v.get("ambiguity"):
        out.append(f"⚠️ Ambiguità: {esc(v['ambiguity'])}")
    if v.get("ask_seller"):
        out.append(f"❓ Chiedi al venditore: {esc(v['ask_seller'])}")
    if v.get("reasoning"):
        out.append(f"🧠 {esc(v['reasoning'])}")
    out.append(f"🔗 {esc(ad['url'])}\n")
    return "\n".join(out)


def run_scan(chat, only=None):
    import ai

    t0 = time.time()
    links = load_links(only)
    if not links:
        tg.send(chat, "Nessun link trovato in links.txt per quella ricerca.")
        return
    tg.send(chat, f"🚀 Scansione avviata su {len(links)} ricerca/e (obiettivo ~{TARGET_ADS} annunci ciascuna). Ci vorranno alcuni minuti.")

    s = subito.make_session()
    use_apify = apify_source.enabled()
    apify_cost = 0.0
    all_ads, seen = [], set()
    for name, url in links:
        if use_apify:
            ads, cost, err = apify_source.scrape(url, TARGET_ADS)
            apify_cost += cost
            status = err or "ok"
        else:
            ads, status = subito.fetch_all(s, url, target=TARGET_ADS, progress=lambda m: tg.send(chat, f"{name}: {m}"))
        if not ads:
            tg.send(chat, f"⚠️ {esc(name)}: nessun annuncio letto ({esc(status)}). Prova /test per capire.")
        new = [a for a in ads if a["id"] not in seen]
        seen.update(a["id"] for a in new)
        all_ads.extend(new)
        tg.send(chat, f"✅ {esc(name)}: {len(ads)} annunci letti")
    if not all_ads:
        return

    candidates = [a for a in all_ads if passes_rules(a)]
    if not os.environ.get("ANTHROPIC_API_KEY"):
        prices = sorted(a["price"] for a in candidates)
        med = prices[len(prices) // 2] if prices else 0
        tg.send(
            chat,
            f"📋 <b>Solo lettura (AI non ancora attiva)</b>\n{len(all_ads)} annunci letti, {len(candidates)} dopo i filtri di prezzo "
            f"({MIN_PRICE}-{MAX_PRICE}€, max {MAX_AGE_DAYS:g} giorni), prezzo mediano {med}€.\n{age_stats(all_ads)}\nCosto lettura Subito ≈ ${apify_cost:.2f} · {(time.time() - t0) / 60:.0f} min\n"
            "Per la valutazione degli affari serve il secret ANTHROPIC_API_KEY.",
        )
        return
    tg.send(chat, f"📋 {len(all_ads)} annunci totali, {len(candidates)} dopo i filtri di prezzo e di età ({age_stats(all_ads)}). Valutazione veloce con l'AI…")

    c, spend = ai.client(), ai.Spend()
    tri = ai.triage(c, candidates, spend)
    scored = []
    for idx, a in enumerate(candidates):
        t = tri.get(idx)
        if not t:
            continue
        v, conf = float(t.get("v") or 0), float(t.get("c") or 0)
        if v < MIN_VALUE:
            continue
        est = v * 0.9 - a["price"]
        if est >= TRIAGE_MARGIN:
            scored.append((est * max(conf, 20) / 100, a))
    scored.sort(key=lambda x: -x[0])
    finalists = [a for _, a in scored[:MAX_FINALISTS]]
    tg.send(chat, f"🔬 {len(finalists)} annunci promettenti: analisi a fondo con foto e prezzi di mercato…")

    deals, analyzed = [], 0
    for a in finalists:
        if spend.usd >= MAX_USD_PER_SCAN:
            tg.send(chat, f"⛔ Tetto di spesa per scansione raggiunto (${spend.usd:.2f}). Mi fermo qui.")
            break
        detail = None if use_apify else subito.fetch_detail(s, a["url"])
        if detail:
            for k in ("body", "features", "images"):
                if detail.get(k):
                    a[k] = detail[k]
        v = ai.analyze(c, a, s, spend)
        analyzed += 1
        if not v:
            continue
        ok, v = validate(v, a["price"])
        if ok:
            deals.append((float(v.get("margin_probable_eur", 0)), a, v))
        time.sleep(0.5)

    deals.sort(key=lambda x: -x[0])
    mins = (time.time() - t0) / 60
    summary = f"\n📊 Letti {len(all_ads)} · analizzati a fondo {analyzed} · costo AI ≈ ${spend.usd:.2f} · lettura Subito ≈ ${apify_cost:.2f} · {mins:.0f} min"
    if not deals:
        tg.send(chat, f"😶 Nessun affare con margine ≥ {MIN_MARGIN}€ e affidabilità ≥ {MIN_RELIABILITY}% in questa scansione." + summary)
        return
    tg.send(chat, f"🏆 <b>{len(deals)} affari trovati</b> (margine ≥ {MIN_MARGIN}€), dal migliore:" + summary)
    for rank, (_, a, v) in enumerate(deals, 1):
        tg.send(chat, format_deal(rank, a, v))


# ----------------------------------------------------------------------- poll
HELP = (
    "Comandi:\n/scan – scansione completa di tutte le ricerche\n"
    "/scan elettronica – solo una ricerca (nome in links.txt)\n"
    "/test – prova la lettura di Subito (senza AI)\n/help – questo messaggio"
)


def run_poll():
    updates = tg.get_updates()
    if not updates:
        print("nessun messaggio")
        return
    tg.get_updates(offset=max(u["update_id"] for u in updates) + 1)  # conferma
    for u in updates:
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        if not chat or not text:
            continue
        if not CHAT_ID:
            tg.send(chat, f"Il tuo chat id è <b>{chat}</b>.\nAggiungilo su GitHub come Secret chiamato <b>TELEGRAM_CHAT_ID</b>, poi riscrivimi.")
            continue
        if str(chat) != CHAT_ID:
            continue  # ignora gli sconosciuti
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/scan":
            run_scan(chat, arg.strip() or None)
        elif cmd == "/test":
            run_test(chat)
        else:
            tg.send(chat, HELP)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if mode == "poll":
        run_poll()
    elif mode == "scan":
        run_scan(CHAT_ID, sys.argv[2] if len(sys.argv) > 2 else None)
    elif mode == "test":
        run_test(CHAT_ID)
