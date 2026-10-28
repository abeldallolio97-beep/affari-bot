"""Test offline del parser con una pagina finta (struttura attesa di Subito)."""
import json
import subito

item = lambda n, price: {"kind": "AdItem", "item": {
    "urn": f"id:ad:{n}:list:{n}", "subject": f"iPhone {n}", "body": "ottimo stato",
    "urls": {"default": f"https://www.subito.it/telefonia/iphone-{n}.htm"},
    "features": {"/price": {"label": "Prezzo", "values": [{"key": str(price), "value": f"{price} €"}]},
                 "/memory": {"label": "Memoria", "values": [{"key": "128", "value": "128 GB"}]}},
    "images": [{"scalable": f"https://images.sbito.it/{n}"}],
    "geo": {"town": {"value": "Torino"}}, "dates": {"display": "Oggi alle 10:00"}}}
data = {"props": {"pageProps": {"initialState": {"items": {"list": [item(1, 120), item(2, 999)]}}}}}
html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'
ads = subito.extract_ads(html)
assert len(ads) == 2, ads
assert ads[0]["price"] == 120 and ads[0]["town"] == "Torino" and "Memoria: 128 GB" in ads[0]["features"]
assert subito.page_url("https://x/y/", 3) == "https://x/y/?o=3"
assert subito.extract_ads("<html></html>") == []
print("parser OK")
