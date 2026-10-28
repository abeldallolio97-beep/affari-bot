"""Valutazione degli annunci con Claude: triage veloce (Haiku) + analisi a fondo (Sonnet)."""
import base64
import json
import os
import re

import anthropic
import requests

TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "claude-haiku-5-5")
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-sonnet-5-5")
USE_WEB_SEARCH = os.getenv("USE_WEB_SEARCH", "1") == "1"
WEB_SEARCH_MAX = int(os.getenv("WEB_SEARCH_MAX", "6"))

# Prezzi in dollari per milione di token (input, output)
PRICE_HAIKU = (0.10, 0.50)
PRICE_SONNET = (2.0, 10.0)
PRICE_WEB_SEARCH = 0.01  # per ricerca


class Spend:
    def __init__(self):
        self.usd = 0.0

    def add(self, model, usage):
        if usage is None:
            return
        pin, pout = PRICE_HAIKU if "haiku" in model else PRICE_SONNET
        self.usd += (getattr(usage, "input_tokens", 0) or 0) * pin / 1e6
        self.usd += (getattr(usage, "output_tokens", 0) or 0) * pout / 1e6
        st = getattr(usage, "server_tool_use", None)
        if st is not None:
            self.usd += (getattr(st, "web_search_requests", 0) or 0) * PRICE_WEB_SEARCH


def client():
    return anthropic.Anthropic()


def _json_from(text, opener, closer):
    a, b = text.find(opener), text.rfind(closer)
    if a == -1 or b <= a:
        return None
    try:
        return json.loads(text[a : b + 1])
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------- triage
TRIAGE_SYSTEM = (
    "Sei un rivenditore esperto del mercato dell'usato in Italia. Ti do annunci di "
    "Subito.it (Torino). Per ciascuno stima a quanto lo si rivende REALISTICAMENTE "
    "(prezzo di vendita usato in Italia, privati/Vinted/eBay), se ha un valore sensato. "
    "Se è spazzatura, ricambi, un oggetto non identificabile o un annuncio di acquisto, "
    "metti v=0. Se manca qualche dato (es. memoria) usa il caso più probabile e abbassa c.\n"
    "Rispondi SOLO con un array JSON, un elemento per annuncio: "
    '[{"i":<numero>,"v":<rivendita stimata in euro>,"c":<confidenza 0-100>,"n":"<max 8 parole>"}]'
)


def triage(c, ads, spend, batch=30):
    """Restituisce {indice_annuncio: {v, c, n}} per tutti gli annunci."""
    out = {}
    for start in range(0, len(ads), batch):
        chunk = ads[start : start + batch]
        lines = []
        for k, a in enumerate(chunk):
            desc = (a["body"] or "")[:220].replace("\n", " ")
            lines.append(
                f'{start + k}|{a["price"]}€|{a["title"][:90]}|{a["features"][:120]}|{desc}'
            )
        try:
            r = c.messages.create(
                model=TRIAGE_MODEL,
                max_tokens=2500,
                system=TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": "\n".join(lines)}],
            )
        except anthropic.APIError as e:
            print("triage error", e)
            continue
        spend.add(TRIAGE_MODEL, r.usage)
        text = "".join(b.text for b in r.content if b.type == "text")
        arr = _json_from(text, "[", "]") or []
        for item in arr:
            if isinstance(item, dict) and "i" in item:
                out[int(item["i"])] = item
    return out


# ------------------------------------------------------------------- analysis
ANALYSIS_SYSTEM = """Sei un rivenditore professionista di usato in Italia. Valuti UN annuncio di Subito.it (zona Torino, ritiro a mano) per capire se è un vero affare da rivendere.

REGOLE
- Margine netto = prezzo di rivendita realistico - prezzo di acquisto - 10% (commissioni/spedizione) - eventuale riparazione.
- Trova prezzi di mercato con la ricerca web: ALMENO 3 riferimenti. Devono coprire tipi DIVERSI (almeno 2 tipi tra questi, e se esiste in commercio cerca sempre anche il NUOVO):
  "nuovo" (prezzo da nuovo in negozio/store ufficiale), "ricondizionato" (negozio/garantito), "usato_negozio" (usato da negozio), "usato_privato" (annunci di privati: Subito, Vinted, Marketplace), "venduto" (prezzo a cui è stato davvero venduto, es. eBay venduti).
  Per ogni riferimento indica fonte, tipo e prezzo. La rivendita realistica ha come tetto il prezzo dell'usato di privati/venduto, non quello del nuovo. Con meno di 3 riferimenti, o tutti dello stesso tipo, l'affidabilità massima è 50.
- Guarda le foto: difetti, graffi, accessori mancanti, incongruenze con la descrizione, segni di contraffazione o truffa.
- Se manca un dato (es. memoria, modello esatto, accessori) NON scartare: calcola il margine PROBABILE e il margine PEGGIORE, spiega l'ambiguità e cosa chiedere al venditore.
- Sii prudente: prezzo di rivendita = quello a cui si vende davvero in tempi ragionevoli, non il massimo sperato.
- Affidabilità (0-100) = quanto sei sicuro della valutazione.

OUTPUT: SOLO un oggetto JSON, senza altro testo:
{"verdict":"BUY"|"SKIP","identified_item":"","resale_estimate_eur":0,"margin_probable_eur":0,"margin_worst_eur":0,"reliability_pct":0,"references":[{"source":"","type":"nuovo|ricondizionato|usato_negozio|usato_privato|venduto","price_eur":0,"note":""}],"ambiguity":"","ask_seller":"","reasoning":"max 3 frasi"}"""


def _image_blocks(urls, session, limit=3):
    """Foto per l'AI: scaricate da noi; se non si riesce, le scarica Anthropic dal link."""
    blocks = []
    for u in urls[:limit]:
        block = None
        try:
            r = session.get(u, timeout=20)
            ct = (r.headers.get("content-type") or "").split(";")[0]
            if r.ok and ct in ("image/jpeg", "image/png", "image/webp", "image/gif") and len(r.content) < 4_500_000:
                block = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": ct, "data": base64.b64encode(r.content).decode()},
                }
        except requests.RequestException:
            pass
        if block is None and u.startswith("http"):
            block = {"type": "image", "source": {"type": "url", "url": u}}
        if block:
            blocks.append(block)
    return blocks


def analyze(c, ad, session, spend):
    """Analisi completa di un annuncio. Restituisce un dict (verdetto) o None."""
    text = (
        f"TITOLO: {ad['title']}\nPREZZO: {ad['price']} €\nZONA: {ad.get('town')}\n"
        f"DATA: {ad.get('date')}\nCARATTERISTICHE: {ad['features']}\n"
        f"DESCRIZIONE: {(ad['body'] or '')[:2500]}\nLINK: {ad['url']}"
    )
    images = _image_blocks(ad["images"], session)
    content = images + [{"type": "text", "text": text}]
    kwargs = dict(
        model=ANALYSIS_MODEL,
        max_tokens=4000,
        system=ANALYSIS_SYSTEM,
    )
    if USE_WEB_SEARCH:
        kwargs["tools"] = [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX}
        ]
    messages = [{"role": "user", "content": content}]
    resp = None
    for _ in range(4):  # gestisce le pause delle ricerche web lunghe
        try:
            resp = c.messages.create(messages=messages, **kwargs)
        except anthropic.BadRequestError as e:
            if images:  # foto non leggibili: riprova solo con il testo
                print("immagini rifiutate, riprovo senza:", e)
                images = []
                messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
                continue
            print("analysis error", e)
            return None
        except anthropic.APIError as e:
            print("analysis error", e)
            return None
        spend.add(ANALYSIS_MODEL, resp.usage)
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    if resp is None:
        return None
    text_out = "".join(b.text for b in resp.content if b.type == "text")
    return _json_from(text_out, "{", "}")
