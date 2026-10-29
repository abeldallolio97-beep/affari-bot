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


def _json_objects(text):
    """Tutti gli oggetti JSON validi trovati nel testo (anche dentro ```json o con testo attorno)."""
    dec, out, i = json.JSONDecoder(), [], 0
    while True:
        i = text.find("{", i)
        if i == -1:
            return out
        try:
            obj, end = dec.raw_decode(text[i:])
            out.append(obj)
            i += end
        except json.JSONDecodeError:
            i += 1


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
    "Subito.it (Torino), uno per riga nel formato: numero|prezzo|titolo|caratteristiche|descrizione. "
    "Per ciascuno stima a quanto lo si rivende REALISTICAMENTE (prezzo di vendita usato in Italia, "
    "privati/Vinted/eBay), se ha un valore sensato. Se è spazzatura, ricambi, un oggetto non identificabile "
    "o un annuncio di acquisto, metti valore 0. Se manca qualche dato (es. memoria) usa il caso più probabile "
    "e abbassa la confidenza. Non essere ottimista: il valore è il prezzo a cui si vende davvero in poche "
    "settimane, in condizioni medie, non il massimo. Se è un lotto, ha difetti dichiarati o è un oggetto raro "
    "e di difficile prezzo, abbassa la confidenza.\n"
    "RISPOSTA: SOLO una riga per annuncio, nello stesso ordine, nel formato esatto\n"
    "numero|valore_di_rivendita_in_euro|confidenza_0_100|nota di massimo 6 parole\n"
    "Esempio: 12|320|70|iPhone 13 128GB probabile\n"
    "Niente altro testo, niente titoli, niente markdown."
)

_LINE = re.compile(r"^\s*(\d+)\s*\|\s*~?(\d+(?:[.,]\d+)?)\s*(?:€|eur\w*)?\s*\|\s*(\d+)\s*%?\s*(?:\|(.*))?$", re.I)


def parse_triage(text):
    """Legge le righe 'numero|valore|confidenza|nota'. Restituisce {indice: {v,c,n}}."""
    out = {}
    for line in text.splitlines():
        m = _LINE.match(line.replace("`", ""))
        if m:
            out[int(m.group(1))] = {
                "v": float(m.group(2).replace(",", ".")),
                "c": float(m.group(3)),
                "n": (m.group(4) or "").strip(),
            }
    return out


LAST_TRIAGE = {"batches": 0, "api_errors": 0, "parse_failures": 0, "items": 0, "sample": ""}


def triage(c, ads, spend, batch=30):
    """Restituisce {indice_annuncio: {v, c, n}} per tutti gli annunci."""
    out = {}
    LAST_TRIAGE.update(batches=0, api_errors=0, parse_failures=0, items=0, sample="")
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
                max_tokens=3000,
                system=TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": "\n".join(lines)}],
            )
        except anthropic.APIError as e:
            print("triage error", e)
            LAST_TRIAGE["api_errors"] += 1
            continue
        LAST_TRIAGE["batches"] += 1
        spend.add(TRIAGE_MODEL, r.usage)
        text = "".join(b.text for b in r.content if b.type == "text")
        parsed = parse_triage(text)
        if not parsed:
            LAST_TRIAGE["parse_failures"] += 1
            if not LAST_TRIAGE["sample"]:
                LAST_TRIAGE["sample"] = text[:300]
            print("triage: risposta non leggibile:", text[:300])
            continue
        out.update(parsed)
        LAST_TRIAGE["items"] += len(parsed)
    return out


# ------------------------------------------------------------------- analysis
ANALYSIS_SYSTEM = """Sei un rivenditore professionista di usato in Italia. Valuti UN annuncio di Subito.it (zona Torino, ritiro a mano) per capire se è un vero affare da rivendere.

SCOPO: trovare venditori che sbagliano il prezzo (per fretta, ignoranza, o perché non sanno cosa hanno). Quindi un prezzo basso, da solo, NON è un indizio di contraffazione, truffa o difetto: non usarlo come prova.

REGOLE
- Margine netto = prezzo di rivendita realistico - prezzo di acquisto - 10% (commissioni/spedizione) - eventuale riparazione.
- Trova prezzi di mercato con la ricerca web: ALMENO 3 riferimenti. Devono coprire tipi DIVERSI (almeno 2 tipi tra questi, e se esiste in commercio cerca sempre anche il NUOVO):
  "nuovo" (prezzo da nuovo in negozio/store ufficiale), "ricondizionato" (negozio/garantito), "usato_negozio" (usato da negozio), "usato_privato" (annunci di privati: Subito, Vinted, Marketplace), "venduto" (prezzo a cui è stato davvero venduto, es. eBay venduti).
  Per ogni riferimento indica fonte, tipo e prezzo. La rivendita realistica ha come tetto il prezzo dell'usato di privati/venduto, non quello del nuovo. Con meno di 3 riferimenti, o tutti dello stesso tipo, l'affidabilità massima è 50.
- AUTENTICITÀ E STATO: giudicali SOLO da indizi concreti: foto (loghi, etichette, cuciture, seriali, refusi, condizioni, accessori), testo dell'annuncio, scatola/ricevuta, profilo del venditore. Descrivi cosa vedi davvero. Se le foto non bastano a decidere, scrivi "non_verificabile": NON assumere il falso né l'originale per via del prezzo. Stima la probabilità che sia a posto solo dagli indizi (senza indizi: neutra, 50-60%), e indica cosa far verificare al venditore (foto del seriale, prova d'acquisto, video acceso...).
- Se manca un dato (modello, memoria, accessori) NON scartare: calcola il margine PROBABILE e il margine PEGGIORE, spiega l'ambiguità e cosa chiedere.
- Sii prudente sul prezzo di rivendita: quello a cui si vende davvero in tempi ragionevoli, non il massimo sperato.
- Affidabilità (0-100) = quanto sei sicuro della VALUTAZIONE DI MERCATO (identificazione dell'oggetto e prezzi).

VERDETTO
- "BUY": margine probabile >= 100 € anche tenendo conto dei rischi.
- "VERIFY": il potenziale è alto (margine_se_tutto_ok >= 100 €) ma resta UN punto aperto verificabile prima di pagare (autenticità, funzionamento, accessori) e la perdita se va male è contenuta. Vale la pena scrivere al venditore.
- "SKIP": non c'è margine, oppure ci sono indizi CONCRETI di falso/truffa/difetto grave.

OUTPUT: SOLO un oggetto JSON, senza altro testo:
{"verdict":"BUY"|"VERIFY"|"SKIP","identified_item":"","authenticity":"originale_probabile|non_verificabile|dubbia|falso_probabile|non_applicabile","resale_estimate_eur":0,"margin_probable_eur":0,"margin_worst_eur":0,"margin_if_ok_eur":0,"loss_if_bad_eur":0,"reliability_pct":0,"references":[{"source":"","type":"nuovo|ricondizionato|usato_negozio|usato_privato|venduto","price_eur":0,"note":""}],"ambiguity":"","ask_seller":"","reasoning":"max 3 frasi"}"""


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
    cands = [o for o in _json_objects(text_out) if isinstance(o, dict) and "verdict" in o]
    if not cands:
        print("analisi: risposta non leggibile:", text_out[:400])
        return None
    return cands[-1]
