"""Bot affari Subito.it -> Telegram.

Uso:
  python bot.py poll   # legge i comandi Telegram (/scan, /test, /help) ed esegue
  python bot.py scan   # scansione completa subito (serve TELEGRAM_CHAT_ID)
  python bot.py test   # prova di lettura di Subito (non serve l'AI)
"""
import html as _html
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
TARGET_ADS = int(os.getenv("TARGET_ADS", "500"))          # annunci per link
MAX_FINALISTS = int(os.getenv("MAX_FINALISTS", "5"))      # analisi a fondo per scansione
MAX_USD_PER_SCAN = float(os.getenv("MAX_USD_PER_SCAN", "1"))  # tetto di spesa per scansione
VERIFY_MAX_LOSS = int(os.getenv("VERIFY_MAX_LOSS", "150"))      # perdita massima accettata per "da verificare"
TRIAGE_MARGIN = int(os.getenv("TRIAGE_MARGIN", "90"))      # soglia larga del triage
RANK_SHOW = int(os.getenv("RANK_SHOW", "100"))              # righe della classifica veloce
MAX_AGE_DAYS = float(os.getenv("MAX_AGE_DAYS", "3"))        # ignora annunci più vecchi di N giorni

WEEKLY_BUDGET_USD = float(os.getenv("WEEKLY_BUDGET_USD", "6"))   # ~5 € a settimana
STRICT_READY = os.getenv("STRICT_READY", "1") == "1"            # solo oggetti pronti da rivendere, senza test/riparazioni
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "log.csv")
LAST_RUN = {}


def info(chat, text, **kw):
    """Messaggio di avanzamento: nelle scansioni (modo silenzioso) non si invia, per avere solo 2 messaggi."""
    if not LAST_RUN.get("quiet"):
        tg.send(chat, text, **kw)

BUYER_WORDS = re.compile(r"\b(cerco|cercasi|compro|acquisto|ritiro usato)\b", re.I)

# Categorie facili da contraffare e difficili da riconoscere per un non esperto: escluse.
# Si può aggiungere altro dalla variabile EXCLUDE_EXTRA (parole separate da virgola).
_FAKE_PRONE = (
    r"scarp\w*|sneaker\w*|stivali|stivaletti|mocassin\w*|ciabatt\w*|sandali|"
    r"felp\w*|maglia|maglietta|t-?shirt|polo|camicia|pantalon\w*|jeans|tuta|"
    r"giubb\w*|giacc\w*|piumin\w*|cappotto|k-?way|cappell\w*|berretto|sciarpa|"
    r"borsa|borsetta|borsello|marsupio|zaino|portafogli\w*|cintura|"
    r"occhiali|ray-?ban|profum\w*|"
    r"nike|adidas|jordan|yeezy|new balance|converse|vans|puma|asics|"
    r"supreme|off-?white|stone island|moncler|north face|carhartt|stussy|palace|"
    r"gucci|prada|louis vuitton|\blv\b|chanel|dior|hermes|balenciaga|fendi|versace|burberry|armani|"
    r"rolex|omega|cartier|patek|audemars|tag heuer|"
    r"airpods?|apple pencil|labubu fake|replica|copia"
)
_extra = [w.strip() for w in os.getenv("EXCLUDE_EXTRA", "").split(",") if w.strip()]
if _extra:
    _FAKE_PRONE += "|" + "|".join(re.escape(w) for w in _extra)
FAKE_PRONE = re.compile(r"\b(" + _FAKE_PRONE + r")\b", re.I)


def load_links(only=None, auto_only=False):
    """links.txt: 'nome | link' oppure 'nome | link | auto' (le righe auto partono nella scansione automatica)."""
    links = []
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "links.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            parts = [x.strip() for x in line.split("|")]
            name, url = parts[0], parts[1]
            flags = [x.lower() for x in parts[2:]]
            if only and only.lower() not in name.lower():
                continue
            if auto_only and "auto" not in flags:
                continue
            links.append((name, url))
    return links


def weekly_spent(kind=None):
    """Dollari spesi (lettura + AI) negli ultimi 7 giorni, dal registro state/log.csv (kind='auto': solo scansioni automatiche)."""
    import csv

    total, now = 0.0, datetime.now(timezone.utc)
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if kind and r.get("kind") != kind:
                    continue
                try:
                    when = datetime.fromisoformat(r["when_utc"])
                    if (now - when).total_seconds() <= 7 * 86400:
                        total += float(r["apify_usd"] or 0) + float(r["ai_usd"] or 0)
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return total


def log_run(kind, links, ads_read, apify_usd):
    """Aggiunge una riga al registro dei costi (serve al foglio costi/ricavi)."""
    import csv

    sp = LAST_RUN.get("spend")
    row = {
        "when_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "links": ";".join(n for n, _ in links),
        "ads_read": ads_read,
        "candidates": LAST_RUN.get("candidates", 0),
        "deep_analyzed": LAST_RUN.get("analyzed", 0),
        "deals": LAST_RUN.get("deals", 0),
        "apify_usd": round(apify_usd, 3),
        "ai_usd": round(sp.usd, 3) if sp else 0,
    }
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


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
    if FAKE_PRONE.search(a["title"]) or FAKE_PRONE.search(a.get("features") or ""):
        return False
    return True


def validate(v, price):
    """Applica i criteri dell'utente al verdetto dell'AI. Ritorna (stato, v): 'buy', 'verify' o None."""
    try:
        refs = v.get("references") or []
        n_refs = len([r for r in refs if isinstance(r, dict) and r.get("price_eur")])
        rel = int(v.get("reliability_pct") or 0)
        types = {str(r.get("type", "")).strip() for r in refs if isinstance(r, dict) and r.get("price_eur")}
        types.discard("")
        if n_refs < MIN_REFS or len(types) < 2:  # servono 3 prezzi e almeno 2 tipi diversi (nuovo/usato...)
            rel = min(rel, 50)
        v["reliability_pct"] = rel
        verdict = str(v.get("verdict", "")).upper()
        if STRICT_READY and v.get("ready") is not True:
            v["_scarto"] = "richiede controlli/test/riparazioni prima di poterlo rivendere"
            return None, v
        probable = float(v.get("margin_probable_eur") or 0)
        worst = float(v.get("margin_worst_eur") or 0)
        if verdict == "BUY" and probable >= MIN_MARGIN and worst >= 0 and rel >= MIN_RELIABILITY:
            return "buy", v
        if_ok = float(v.get("margin_if_ok_eur") or 0)
        loss = abs(float(v.get("loss_if_bad_eur") or 0))
        if verdict in ("VERIFY", "BUY") and if_ok >= MIN_MARGIN and rel >= MIN_RELIABILITY and loss <= max(VERIFY_MAX_LOSS, 0):
            return "verify", v
        return None, v
    except (TypeError, ValueError):
        return None, v


STATUS_LABEL = {
    "buy": "✅ AFFARE (rispetta i tuoi criteri)",
    "verify": "🟡 DA VERIFICARE: potenziale alto, un punto aperto da chiarire col venditore",
    None: "❌ NON rispetta i tuoi criteri",
}


def format_deal(rank, ad, v):
    refs = ", ".join(
        f"{esc(r.get('source'))} ({esc(r.get('type', '?'))}) {r.get('price_eur')}€"
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
    new_p, used_p = v.get("new_price_eur"), v.get("used_price_eur")
    if new_p or used_p:
        out.append(f"🏷️ Nuovo: {new_p if new_p else 'fuori produzione/n.d.'}{'€' if new_p else ''} · Usato (privati): {used_p if used_p else 'n.d.'}{'€' if used_p else ''}")
    if v.get("ready") is True:
        out.append("✅ Pronto da rivendere: nessun test o riparazione necessari")
    elif v.get("ready") is False:
        out.append("🛠️ NON pronto: servono controlli/test/riparazioni" + (f" ({esc(v['_scarto'])})" if v.get("_scarto") else ""))
    if v.get("authenticity") and v.get("authenticity") != "non_applicabile":
        out.append(f"🔎 Autenticità/stato: {esc(str(v['authenticity']).replace('_', ' '))}")
    if v.get("margin_if_ok_eur") is not None and v.get("loss_if_bad_eur") is not None:
        try:
            out.append(f"⚖️ Se tutto ok: +{round(float(v['margin_if_ok_eur']))}€ · se va male: -{abs(round(float(v['loss_if_bad_eur'])))}€")
        except (TypeError, ValueError):
            pass
    if v.get("ambiguity"):
        out.append(f"⚠️ Ambiguità: {esc(v['ambiguity'])}")
    if v.get("ask_seller"):
        out.append(f"❓ Chiedi al venditore: {esc(v['ask_seller'])}")
    if v.get("reasoning"):
        out.append(f"🧠 {esc(v['reasoning'])}")
    out.append(f"🔗 {esc(ad['url'])}\n")
    return "\n".join(out)


def run_scan(chat, only=None, auto=False):
    import ai

    t0 = time.time()
    links = load_links(only, auto_only=auto)
    if not links:
        tg.send(chat, "Nessun link trovato in links.txt per quella ricerca.")
        return
    spent = weekly_spent("auto")
    if auto and spent >= WEEKLY_BUDGET_USD:
        tg.send(chat, f"⏸️ Scansione automatica saltata: spesi ${spent:.2f} negli ultimi 7 giorni, tetto ${WEEKLY_BUDGET_USD:.2f}.")
        return
    LAST_RUN.clear()
    LAST_RUN["quiet"] = True
    LAST_RUN["per_link"] = []
    tg.send(chat, f"🚀 <b>SCANSIONE AVVIATA</b> ({'automatica' if auto else 'manuale'}) su {len(links)} ricerca/e: {esc(', '.join(n for n, _ in links))}. Ci vorranno circa 10–15 minuti.")

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
        LAST_RUN.setdefault("per_link", []).append(f"• {esc(name)}: {len(ads)} annunci")
        info(chat, f"✅ {esc(name)}: {len(ads)} annunci letti")
    if not all_ads:
        log_run("auto" if auto else "scan", links, 0, apify_cost)
        return

    analyze_ads(chat, all_ads, apify_cost, t0, use_apify, s)
    log_run("auto" if auto else "scan", links, len(all_ads), apify_cost)
    info(chat, f"🧾 Spesa ultimi 7 giorni: ${weekly_spent():.2f} su tetto ${WEEKLY_BUDGET_USD:.2f} (dettaglio nel registro costi).")


def analyze_ads(chat, all_ads, apify_cost, t0, use_apify, s, deep=True):
    import ai

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
    LAST_RUN["head"] = f"{len(all_ads)} annunci letti, {len(candidates)} dopo i filtri di prezzo e di età ({age_stats(all_ads)})"
    info(chat, f"📋 {len(all_ads)} annunci totali, {len(candidates)} dopo i filtri di prezzo e di età ({age_stats(all_ads)}). Valutazione veloce con l'AI…")

    c, spend = ai.client(), ai.Spend()
    LAST_RUN.update(spend=spend, candidates=len(candidates), analyzed=0, deals=0)
    tri = ai.triage(c, candidates, spend)
    ranked = []  # (punteggio, margine stimato, valore stimato, confidenza, nota, annuncio)
    for idx, a in enumerate(candidates):
        t = tri.get(idx)
        if not t:
            continue
        try:
            v, conf = float(t.get("v") or 0), float(t.get("c") or 0)
        except (TypeError, ValueError):
            continue
        if v < 1:
            continue
        est = v * 0.9 - a["price"]
        ranked.append((est * max(conf, 20) / 100, est, v, conf, str(t.get("n") or ""), a))
    ranked.sort(key=lambda x: -x[0])

    lt = ai.LAST_TRIAGE
    LAST_RUN["tri"] = (f"{lt['items']}/{len(candidates)} valutati, errori API {lt['api_errors']}, "
                       f"risposte illeggibili {lt['parse_failures']}, costo AI veloce ${spend.usd:.2f}")
    if LAST_RUN.get("quiet"):
        tg.send(
            chat,
            "📥 <b>RECAP ANNUNCI LETTI</b>\n" + "\n".join(LAST_RUN.get("per_link", [])) +
            f"\nTotale: {len(all_ads)} annunci letti, {len(candidates)} dopo i filtri di prezzo ({MIN_PRICE}-{MAX_PRICE}€), "
            f"età (max {MAX_AGE_DAYS:g} giorni) e categorie facili da falsificare.\n{esc(age_stats(all_ads))}.\n"
            f"Valutazione veloce con l'AI: {lt['items']}/{len(candidates)} annunci valutati, {len(ranked)} con un valore stimato "
            f"(errori API {lt['api_errors']}, risposte illeggibili {lt['parse_failures']}). Costo finora ≈ ${spend.usd:.2f}.",
        )
    info(
        chat,
        f"🧪 Valutazione veloce: {lt['items']}/{len(candidates)} annunci valutati, {len(ranked)} con un valore stimato "
        f"(errori API {lt['api_errors']}, risposte illeggibili {lt['parse_failures']}) · costo ≈ ${spend.usd:.2f}",
    )
    if lt["sample"]:
        tg.send(chat, "Esempio di risposta che non riesco a leggere:\n" + esc(lt["sample"]))
    send_ranking(chat, ranked)

    # analisi a fondo (costosa) solo per i pochi candidati che superano le soglie
    finalists = [r[5] for r in ranked if r[2] >= MIN_VALUE and r[1] >= TRIAGE_MARGIN][:MAX_FINALISTS] if deep else []
    if not deep:
        tg.send(chat, f"💡 Costo totale ≈ ${spend.usd:.2f}. Per verificare a fondo un annuncio: /analizza <link>.")
        return
    deals, analyzed, scartati = [], 0, []
    if finalists:
        info(chat, f"🔬 Analisi a fondo (foto + prezzi di mercato) dei {len(finalists)} più promettenti…")
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
            scartati.append(f"• {esc(a['title'][:60])} — analisi non riuscita\n{esc(a['url'])}")
            continue
        status, v = validate(v, a["price"])
        if not status:
            scartati.append(
                f"• <b>{esc(a['title'][:60])}</b> ({a['price']}€) — {esc(v.get('verdict'))}, margine probabile "
                f"{esc(v.get('margin_probable_eur'))}€, affidabilità {esc(v.get('reliability_pct'))}%\n"
                f"  {esc(str(v.get('_scarto') or v.get('reasoning') or v.get('ambiguity') or '')[:240])}\n{esc(a['url'])}"
            )
        if status:
            score = float(v.get("margin_probable_eur", 0)) if status == "buy" else float(v.get("margin_if_ok_eur", 0)) / 2
            deals.append((score + (10000 if status == "buy" else 0), a, v, status))
        time.sleep(0.5)

    LAST_RUN.update(analyzed=analyzed, deals=len(deals))
    deals.sort(key=lambda x: -x[0])
    mins = (time.time() - t0) / 60
    summary = f"\n📊 Letti {len(all_ads)} · analizzati a fondo {analyzed} · costo AI ≈ ${spend.usd:.2f} · lettura Subito ≈ ${apify_cost:.2f} · {mins:.0f} min"
    if not deals:
        text = (f"🎯 <b>AFFARI DA CONCLUDERE</b>\n😶 Controllo fatto: nessun affare pronto da rivendere (senza test né riparazioni) con margine ≥ {MIN_MARGIN}€ e affidabilità ≥ {MIN_RELIABILITY}%. "
                f"La lista dei risultati qui sopra resta solo indicativa (stime non verificate)." + summary)
        if scartati:
            text += "\n\n🔎 <b>Perché sono stati scartati i " + str(len(scartati)) + " analizzati a fondo:</b>\n\n" + "\n\n".join(scartati)
        tg.send(chat, text)
        return
    n_buy = sum(1 for d in deals if d[3] == "buy")
    tg.send(chat, f"🎯 <b>AFFARI DA CONCLUDERE</b>\n🏆 <b>{n_buy} affari verificati</b> e <b>{len(deals) - n_buy} da verificare</b> (margine ≥ {MIN_MARGIN}€), dal migliore:" + summary)
    for rank, (_, a, v, status) in enumerate(deals, 1):
        tg.send(chat, STATUS_LABEL[status] + "\n" + format_deal(rank, a, v))


STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "ranking.json")


def save_ranking(top):
    """Memorizza la classifica (numero -> annuncio) così /analizza 2 5 3 funziona anche in un'altra esecuzione."""
    import json

    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    data = [{"n": n, "url": r[5]["url"], "title": r[5]["title"], "price": r[5]["price"]} for n, r in enumerate(top, 1)]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def load_ranking():
    import json

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return {d["n"]: d for d in json.load(f)}
    except (OSError, ValueError, KeyError):
        return {}


def send_ranking(chat, ranked):
    """Classifica veloce: i migliori RANK_SHOW annunci dal migliore al peggiore, con gli importi."""
    top = ranked[:RANK_SHOW]
    if top:
        save_ranking(top)
    if not top:
        tg.send(chat, "Nessun annuncio con un valore stimato.")
        return
    lines = [f"📋 <b>RISULTATI</b> (top {len(top)}, dal migliore al peggiore). Sono stime NON verificate: "
             "prezzo → rivendita stimata, margine al netto del 10%, confidenza dell'AI. Da guardare con calma; "
             "gli affari veri sono nell'ultimo messaggio.\n"]
    for n, (_, est, v, conf, note, a) in enumerate(top, 1):
        title = esc(a["title"][:45])
        url = _html.escape(a["url"] or "", quote=True)
        lines.append(
            f'{n}. <a href="{url}">{title}</a>\n   {a["price"]}€ → ~{v:.0f}€ · margine ~{est:.0f}€ · conf {conf:.0f}%'
            + (f" · {esc(note[:40])}" if note else "")
        )
    lines.append("\nPer verificare a fondo uno o più annunci (foto + prezzi di mercato, circa 0,15 $ ciascuno) scrivi i loro NUMERI, per esempio: /analizza 2 5 3")
    tg.send(chat, "\n".join(lines))


def run_rescan(chat):
    LAST_RUN.clear()
    """Rianalizza gli ultimi dati già letti da Apify, senza pagare di nuovo la lettura."""
    t0 = time.time()
    if not apify_source.enabled():
        tg.send(chat, "Serve APIFY_TOKEN.")
        return
    links = load_links()
    tg.send(chat, "♻️ Rianalizzo gli ultimi annunci già letti (nessun costo di lettura)…")
    ads, err = apify_source.latest_ads(len(links))
    if not ads:
        tg.send(chat, f"Nessun dato recente trovato ({esc(err or 'vuoto')}). Fai prima /scan.")
        return
    analyze_ads(chat, ads, 0.0, t0, True, subito.make_session(), deep=False)


MAX_ANALYZE = 5


def run_analyze(chat, arg):
    """/analizza 2 5 3  (numeri della classifica)  oppure  /analizza <link>."""
    import ai

    t0 = time.time()
    if not os.environ.get("ANTHROPIC_API_KEY") or not apify_source.enabled():
        tg.send(chat, "Servono ANTHROPIC_API_KEY e APIFY_TOKEN.")
        return
    ranking = load_ranking()
    urls, bad = [], []
    for tok in re.split(r"[\s,;]+", arg.strip()):
        if not tok:
            continue
        if tok.isdigit():
            d = ranking.get(int(tok))
            (urls.append(d["url"]) if d else bad.append(tok))
        elif tok.startswith("http"):
            urls.append(tok)
        else:
            bad.append(tok)
    if bad:
        tg.send(chat, f"Non capisco o non trovo: {esc(', '.join(bad))}. Usa i numeri della classifica (fai prima /ri) o un link.")
    urls = urls[:MAX_ANALYZE]
    if not urls:
        return
    pool, _ = apify_source.latest_ads(len(load_links()))
    index = {(a["url"] or "").split("?")[0].rstrip("/"): a for a in pool}
    c, spend, s = ai.client(), ai.Spend(), subito.make_session()
    apify_cost = 0.0
    for url in urls:
        key = url.split("?")[0].rstrip("/")
        ad = index.get(key)
        if not ad:
            ad, cost, err = apify_source.scrape_one(url)
            apify_cost += cost
        if not ad:
            tg.send(chat, f"Non riesco a leggere questo annuncio (forse è stato venduto o rimosso): {esc(url)}")
            continue
        if spend.usd >= MAX_USD_PER_SCAN * 2:
            tg.send(chat, f"⛔ Tetto di spesa raggiunto (${spend.usd:.2f}).")
            break
        tg.send(chat, f"🔬 Analizzo a fondo: {esc(ad['title'][:60])} …")
        v = ai.analyze(c, ad, s, spend)
        if not v:
            tg.send(chat, "L'analisi non ha prodotto un risultato. Riprova.")
            continue
        status, v = validate(v, ad["price"])
        tg.send(chat, STATUS_LABEL[status] + "\n" + format_deal(1, ad, v))
    tg.send(chat, f"💡 Costo AI ≈ ${spend.usd:.2f} · lettura ≈ ${apify_cost:.3f} · {(time.time() - t0) / 60:.0f} min")


# ----------------------------------------------------------------------- poll
HELP = (
    "Comandi:\n/scan – scansione completa di tutte le ricerche\n"
    "/scan elettronica – solo una ricerca (nome in links.txt)\n"
    "/analizza 2 5 3 – analisi a fondo degli annunci con quei numeri della classifica (~0,15 $ l'uno); accetta anche un link\n/ri – rianalizza gli ultimi annunci già letti (non ripaga la lettura)\n/test – prova la lettura di Subito (senza AI)\n/help – questo messaggio"
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
        if cmd == "/analizza":
            run_analyze(chat, arg) if arg.strip() else tg.send(chat, "Scrivi per esempio: /analizza 2 5 3  (numeri della classifica)")
        elif cmd == "/ri":
            run_rescan(chat)
        elif cmd == "/scan":
            run_scan(chat, arg.strip() or None)
        elif cmd == "/test":
            run_test(chat)
        else:  # /start, /help, /menu o qualsiasi altro testo: mostra i tasti
            tg.send(chat, HELP, keyboard=tg.MENU)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    if mode == "poll":
        run_poll()
    elif mode == "scan":
        run_scan(CHAT_ID, sys.argv[2] if len(sys.argv) > 2 else None)
    elif mode == "auto":
        run_scan(CHAT_ID, None, auto=True)
    elif mode == "test":
        run_test(CHAT_ID)
