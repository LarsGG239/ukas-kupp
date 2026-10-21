#!/usr/bin/env python3
"""Ukas kupp: henter ukas tilbudsaviser (Tjek) og prisfall (Kassalapp) for handlelista
og lager docs/index.html.

Bruker bare standardbiblioteket, så ingen pip install trengs.
Kjør lokalt:  python kupp.py            (bare tilbudsaviser)
              KASSALAPP_API_KEY=... python kupp.py   (begge kildene)
"""
import html
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://kassal.app/api/v1/products"
TJEK = "https://squid-api.tjek.com/v2"
PLACE_FILE = ROOT / "sted.txt"
LIST_FILE = ROOT / "handleliste.txt"
STORES_FILE = ROOT / "butikker.txt"
HIST_FILE = ROOT / "data" / "history.json"
OUT_FILE = ROOT / "docs" / "index.html"

KUPP = 0.20          # minst 20 % under vanlig pris
GREI = 0.08          # minst 8 % under vanlig pris
TYPICAL_DAYS = 120   # hvor langt tilbake «vanlig pris» regnes fra
STALE_DAYS = 30      # hopp over varer der prisen ikke er oppdatert på en måned
MAX_DEALS = 6        # tilbud vist per vare
HIST_KEEP = 40       # egne prispunkter lagret per produkt
PAUSE = 1.1          # sekunder mellom API-kall
TJEK_PAUSE = 0.8     # sekunder mellom kall til Tjek (vær høflig)
MAX_AVIS = 5         # avistilbud vist per vare

NOW = datetime.now(timezone.utc)


# ---------- input ----------
def read_lines(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def parse_date(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- API ----------
def api_get(key, params, tries=4):
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "ukas-kupp/1.0",
    })
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("API-nøkkelen ble avvist (401). Sjekk secret-en KASSALAPP_API_KEY.")
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get("Retry-After") or 15 * (attempt + 1))
                print(f"  {e.code}, venter {wait} s …")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            print(f"  nettverksfeil ({e.reason}), prøver igjen …")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Ga opp etter {tries} forsøk: {url}")


def search(key, term):
    data = api_get(key, {"search": term, "size": 100})
    return data.get("data") or []


# ---------- Tjek (tilbudsavisene) ----------
# Uoffisielt, udokumentert API bak eTilbudsavis/Mattilbud. Formatet kan endre seg.
def tjek_get(path, params, tries=3):
    url = TJEK + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ukas-kupp/1.0 (privat bruk)"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(int(e.headers.get("Retry-After") or 15 * (attempt + 1)))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Ga opp etter {tries} forsøk: {url}")


def read_place():
    for line in read_lines(PLACE_FILE):
        parts = [x.strip() for x in line.split(",")]
        try:
            lat, lng = float(parts[0]), float(parts[1])
            km = float(parts[2]) if len(parts) > 2 else 8
            return {"r_lat": lat, "r_lng": lng, "r_radius": int(km * 1000)}
        except (ValueError, IndexError):
            continue
    return {"r_lat": 63.4305, "r_lng": 10.3951, "r_radius": 8000}


def brand_name(o):
    b = o.get("branding") or (o.get("dealer") or {}).get("branding") or {}
    return b.get("name") or (o.get("dealer") or {}).get("name")


def tjek_unit(price, q):
    """Pris per kg / l / stk ut fra Tjeks SI-faktor (g -> kg = 0.001)."""
    if price is None or not q:
        return None, None
    si = (q.get("unit") or {}).get("si") or {}
    size = (q.get("size") or {}).get("from")
    if not si.get("factor") or not size:
        return None, None
    pieces = (q.get("pieces") or {}).get("from") or 1
    sym = si.get("symbol")
    amount = size if sym == "pcs" else size * si["factor"] * pieces
    if not amount:
        return None, None
    return price / amount, ("stk" if sym == "pcs" else sym)


def norm_tjek(o, store_by_catalog):
    pricing = o.get("pricing") or {}
    price = pricing.get("price")
    if price is None:
        return None
    till = parse_date(o.get("run_till"))
    start = parse_date(o.get("run_from"))
    if till and till < NOW:
        return None
    pre = pricing.get("pre_price")
    discount = (1 - price / pre) if pre else None
    if discount is not None and discount >= KUPP:
        verdict = "kupp"
    elif discount is not None and discount >= GREI:
        verdict = "grei"
    else:
        verdict = "avis"
    unit_price, unit = tjek_unit(price, o.get("quantity"))
    return {
        "id": "tjek:" + str(o.get("id")),
        "name": (o.get("heading") or "").strip() or "Ukjent vare",
        "store": brand_name(o) or store_by_catalog.get(o.get("catalog_id")) or "Ukjent butikk",
        "price": float(price),
        "typical": float(pre) if pre else None,
        "discount": discount,
        "verdict": verdict,
        "unit_price": unit_price,
        "unit": unit or "stk",
        "url": None,
        "from": start,
        "till": till,
        "source": "avis",
    }


def store_ok(name, store_filter):
    return not store_filter or any(s in (name or "").lower() for s in store_filter)


def tjek_all_offers(geo, store_filter):
    """Alle tilbud i dagligvareavisene i nærheten (avis-modus gir flest treff i Norge)."""
    catalogs = tjek_get("/catalogs", {**geo, "order_by": "distance", "limit": 100}) or []
    store_by_catalog, chosen = {}, []
    for c in catalogs:
        name = brand_name(c)
        till = parse_date(c.get("run_till"))
        if not name or not store_ok(name, store_filter) or (till and till < NOW):
            continue
        if c.get("id") in store_by_catalog:
            continue
        store_by_catalog[c.get("id")] = name
        chosen.append(c.get("id"))
    print(f"Tjek: {len(chosen)} aviser i nærheten ({', '.join(sorted(set(store_by_catalog.values())))})")
    offers = []
    for cid in chosen:
        for page in range(6):
            time.sleep(TJEK_PAUSE)
            rows = tjek_get("/offers", {**geo, "catalog_ids": cid, "limit": 100, "offset": page * 100}) or []
            offers += [o for o in (norm_tjek(r, store_by_catalog) for r in rows) if o]
            if len(rows) < 100:
                break
    return offers, store_by_catalog


def tjek_search(geo, term, store_by_catalog, store_filter):
    rows = tjek_get("/offers/search", {**geo, "query": term, "limit": 100}) or []
    out = [o for o in (norm_tjek(r, store_by_catalog) for r in rows) if o]
    return [o for o in out if store_ok(o["store"], store_filter)]


def matches(item, name):
    words = item.lower().split()
    n = name.lower()
    return all(w in n for w in words)


# ---------- vurdering ----------
def typical_price(product, own_hist):
    """Median av prisene siste TYPICAL_DAYS dager, fra Kassalapp og egen historikk."""
    cutoff = NOW - timedelta(days=TYPICAL_DAYS)
    prices = []
    for h in product.get("price_history") or []:
        d = parse_date(h.get("date"))
        if h.get("price") and (d is None or d >= cutoff):
            prices.append(float(h["price"]))
    for d, p in own_hist:
        dd = parse_date(d)
        if p and dd and dd >= cutoff:
            prices.append(float(p))
    if len(prices) < 3:
        return None
    return statistics.median(prices)


def unit_label(product):
    u = (product.get("weight_unit") or "").lower()
    if u in ("g", "kg"):
        return "kg"
    if u in ("ml", "cl", "dl", "l"):
        return "l"
    return "stk"


def evaluate(product, history):
    price = product.get("current_price")
    if not price:
        return None
    updated = parse_date(product.get("updated_at"))
    if updated and updated < NOW - timedelta(days=STALE_DAYS):
        return None
    pid = str(product.get("id"))
    typical = typical_price(product, history.get(pid, []))
    discount = (1 - price / typical) if typical else None
    if discount is None:
        verdict = None
    elif discount >= KUPP:
        verdict = "kupp"
    elif discount >= GREI:
        verdict = "grei"
    else:
        verdict = None
    store = product.get("store") or {}
    return {
        "id": pid,
        "name": product.get("name") or "Ukjent vare",
        "store": store.get("name") or "Ukjent butikk",
        "price": float(price),
        "typical": typical,
        "discount": discount,
        "verdict": verdict,
        "unit_price": product.get("current_unit_price"),
        "unit": unit_label(product),
        "url": product.get("url"),
        "from": None,
        "till": None,
        "source": "kassalapp",
    }


# ---------- HTML ----------
def kr(p):
    if p is None:
        return "–"
    p = round(float(p), 2)
    if p == int(p):
        return f"{int(p):,}".replace(",", " ") + ",-"
    return f"{p:,.2f}".replace(",", " ").replace(".", ",")


DAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]


def short_date(d):
    d = d.astimezone(timezone(timedelta(hours=1)))
    return f"{DAYS[d.weekday()]} {d.day}.{d.month}"


def offer_html(o, small=False):
    e = html.escape
    badge = {"kupp": '<span class="v v-kupp">Kupp</span>',
             "grei": '<span class="v v-grei">Grei pris</span>',
             "avis": '<span class="v v-avis">Avistilbud</span>'}.get(o["verdict"], "")
    if o["discount"] is not None and o["typical"]:
        ref = "førpris" if o["source"] == "avis" else "vanlig pris"
        why = f"{round(o['discount'] * 100)} % under {ref} ({kr(o['typical'])})"
    elif o["source"] == "avis":
        why = "Kjeden oppgir ingen førpris"
    else:
        why = ""
    unit = f"<small>{kr(o['unit_price'])}/{o['unit']}</small>" if o["unit_price"] else ""
    name = e(o["name"])
    if o["url"]:
        name = f'<a href="{e(o["url"])}" rel="noopener">{name}</a>'
    meta = [e(o["store"])]
    if o["from"] and o["from"] > NOW:
        meta.append(f"fra {short_date(o['from'])}")
    if o["till"]:
        meta.append(f"til {short_date(o['till'])}")
    return (f'<div class="offer{" small" if small else ""}"><div class="price"><b>{kr(o["price"])}</b>{unit}</div>'
            f'<div class="info"><p class="prod">{name}</p><p class="meta">{", ".join(meta)}</p>'
            f'<p class="why">{badge}{e(why)}</p></div></div>')


CSS = """
:root{--bg:#F3F5F1;--surface:#fff;--ink:#17201C;--muted:#5D6863;--line:#D7DDD6;--tag:#FFD23F;--tag-ink:#1A1A10;--red:#C8102E;--chip:#E6EBE4;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111614;--surface:#1A211E;--ink:#E8EDE9;--muted:#9AA6A0;--line:#2B3430;--red:#FF5C70;--chip:#242D29;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Archivo",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.45}
main{max-width:680px;margin:0 auto;padding:max(20px,env(safe-area-inset-top)) 18px 64px}
.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:12px 0 18px}
h1{font-stretch:62%;font-weight:900;font-size:clamp(3.4rem,17vw,6rem);line-height:.84;margin:0}
.weektag{background:var(--tag);color:var(--tag-ink);font-stretch:75%;font-weight:800;padding:8px 14px 8px 20px;clip-path:polygon(12px 0,100% 0,100% 100%,12px 100%,0 50%);transform:rotate(-3deg);white-space:nowrap;margin-bottom:10px}
.lead{font-stretch:85%;font-weight:700;font-size:1.15rem;margin:0 0 4px}.stamp{color:var(--muted);font-size:.9rem;margin:0 0 24px}
section{border-top:2px solid var(--ink);padding:18px 0 8px}
h2{font-stretch:75%;font-weight:800;font-size:1.4rem;margin:0 0 10px}
.offer{display:grid;grid-template-columns:auto 1fr;background:var(--surface);border:1.5px solid var(--line);border-radius:6px;overflow:hidden;margin-bottom:8px}
.price{background:var(--tag);color:var(--tag-ink);padding:10px 14px;display:flex;flex-direction:column;justify-content:center;min-width:112px}
.price b{font-stretch:62%;font-weight:900;font-size:2.1rem;line-height:.95}.price small{font-size:.75rem;font-weight:600;opacity:.8;margin-top:3px}
.small .price b{font-size:1.6rem}.small .price{min-width:92px}
.info{padding:10px 12px;min-width:0}.prod{font-weight:700;margin:0;overflow-wrap:anywhere}.prod a{color:inherit}
.meta{color:var(--muted);font-size:.88rem;margin:2px 0 0}.why{font-size:.9rem;margin:6px 0 0}
.v{display:inline-block;font-size:.78rem;font-weight:800;padding:2px 8px;border-radius:3px;margin-right:6px}
.v-kupp{background:var(--red);color:#fff}.v-grei{background:var(--chip);color:var(--ink)}.v-avis{background:transparent;color:var(--ink);border:1px solid var(--line)}
h3{font-size:.95rem;font-weight:700;color:var(--muted);margin:12px 0 6px}
.cheap{color:var(--muted);font-size:.9rem;margin:4px 2px 12px}.none{color:var(--muted)}
footer{color:var(--muted);font-size:.85rem;margin-top:28px}footer a{color:inherit}
"""


def sort_key(o):
    rank = {"kupp": 0, "grei": 1, "avis": 2}.get(o["verdict"], 3)
    return (rank, -(o["discount"] or 0), o["unit_price"] if o["unit_price"] else o["price"])


def build_html(results, week, errors, sources):
    e = html.escape
    n_avis = sum(1 for r in results.values() if r["avis"])
    n_kupp = sum(1 for r in results.values() for o in r["avis"] + r["deals"] if o["verdict"] == "kupp")
    tally = {}
    for r in results.values():
        best = (r["avis"] or r["deals"] or [None])[0]
        if best:
            tally[best["store"]] = tally.get(best["store"], 0) + 1
    lead = f"{n_avis} av {len(results)} varer er på tilbud i avisene"
    lead += f", {n_kupp} av tilbudene er kupp." if n_kupp else "."
    if tally:
        top = max(tally.items(), key=lambda kv: kv[1])
        if top[1] > 1:
            lead += f" {e(top[0])} har best tilbud på {top[1]} av varene."

    parts = []
    for item, r in results.items():
        parts.append(f"<section><h2>{e(item)}</h2>")
        if r["avis"]:
            parts.append("<h3>I tilbudsavisene</h3>")
            parts.append(offer_html(r["avis"][0]))
            parts.extend(offer_html(o, small=True) for o in r["avis"][1:])
        if r["deals"]:
            parts.append("<h3>Prisfall i Kassalapp</h3>")
            parts.extend(offer_html(o, small=True) for o in r["deals"])
        if not r["avis"] and not r["deals"]:
            parts.append('<p class="none">Ingen tilbud eller prisfall denne uka.</p>')
        if r["cheapest"]:
            c = r["cheapest"]
            per = f" ({kr(c['unit_price'])}/{c['unit']})" if c["unit_price"] else ""
            parts.append(f'<p class="cheap">Billigst per {c["unit"]} uten tilbud: {e(c["name"])}, '
                         f'{e(c["store"])}, {kr(c["price"])}{per}</p>')
        parts.append("</section>")
    if errors:
        parts.append(f'<p class="none">Feil underveis: {e("; ".join(errors))}.</p>')

    stamp = NOW.astimezone(timezone(timedelta(hours=1))).strftime("%d.%m.%Y kl. %H:%M")
    src = " og ".join(sources) if sources else "ingen kilder"
    return f"""<!doctype html>
<html lang="nb"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Ukas kupp – uke {week}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,400..900&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main>
<div class="hero"><h1>Ukas<br>kupp</h1><div class="weektag">Uke {week}</div></div>
<p class="lead">{lead}</p><p class="stamp">Oppdatert {stamp}. «Vanlig pris» i Kassalapp er medianprisen siste {TYPICAL_DAYS} dager.</p>
{"".join(parts)}
<footer>Data fra {e(src)}. Prisene kan avvike fra butikken din.</footer>
</main></body></html>"""


# ---------- main ----------
def main():
    items = read_lines(LIST_FILE)
    if not items:
        sys.exit("handleliste.txt er tom.")
    store_filter = [s.lower() for s in read_lines(STORES_FILE)]
    key = os.environ.get("KASSALAPP_API_KEY")
    history = json.loads(HIST_FILE.read_text(encoding="utf-8")) if HIST_FILE.exists() else {}
    errors, sources = [], []
    results = {it: {"avis": [], "deals": [], "cheapest": None} for it in items}

    # 1) Tilbudsavisene fra Tjek
    geo = read_place()
    try:
        all_avis, store_by_catalog = tjek_all_offers(geo, store_filter)
        print(f"Tjek: {len(all_avis)} tilbud totalt")
        for item in items:
            found = {o["id"]: o for o in all_avis if matches(item, o["name"])}
            time.sleep(TJEK_PAUSE)
            try:
                for o in tjek_search(geo, item, store_by_catalog, store_filter):
                    if matches(item, o["name"]):
                        found.setdefault(o["id"], o)
            except Exception as ex:
                print(f"  søk feilet for {item}: {ex}")
            results[item]["avis"] = sorted(found.values(), key=sort_key)[:MAX_AVIS]
            print(f"  {item}: {len(found)} avistilbud")
        sources.append("tilbudsavisene (Tjek)")
    except Exception as ex:
        print(f"Tjek feilet: {ex}")
        errors.append(f"tilbudsavisene kunne ikke hentes ({ex})")

    # 2) Prisfall fra Kassalapp (valgfritt)
    if key:
        today = NOW.date().isoformat()
        for i, item in enumerate(items):
            if i:
                time.sleep(PAUSE)
            print(f"Kassalapp: {item}")
            try:
                products = search(key, item)
            except Exception as ex:
                print(f"  feil: {ex}")
                errors.append(f"Kassalapp: {item}")
                continue
            offers = []
            for p in products:
                o = evaluate(p, history)
                if not o or not store_ok(o["store"], store_filter):
                    continue
                offers.append(o)
                h = history.setdefault(o["id"], [])
                if not h or h[-1][0] != today:
                    h.append([today, o["price"]])
                    del h[:-HIST_KEEP]
            results[item]["deals"] = sorted((o for o in offers if o["verdict"]), key=lambda o: -o["discount"])[:MAX_DEALS]
            with_unit = [o for o in offers if o["unit_price"]]
            results[item]["cheapest"] = min(with_unit, key=lambda o: o["unit_price"]) if with_unit else None
        sources.append("Kassalapp")
    else:
        print("Ingen KASSALAPP_API_KEY: hopper over prisfall.")

    if not sources:
        sys.exit("Ingen kilder svarte. Se loggen over.")

    week = NOW.isocalendar()[1]
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(build_html(results, week, errors, sources), encoding="utf-8")
    HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    HIST_FILE.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Skrev {OUT_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
