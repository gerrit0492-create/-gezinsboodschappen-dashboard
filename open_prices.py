"""Free price source: Checkjebon open data (github.com/supermarkt/checkjebon, MIT licence).

The dataset is one JSON file with, per supermarket, all articles as {n: name, l: link id, p: price, s: size}.
It has NO EAN codes, NO offers and NO minimum quantities, so:
- a household product is linked ONCE to one exact article per supermarket (the link id is stored as the 'Bron'
  URL of an 'Open data' row in Prijzen); after that every update is exact for that article;
- offers/minimum quantities stay manual ('Aanbieding' rows, optional 'Min. aantal' column);
- open-data prices are never confirmed prices: GO uses the checkout total.

Measurement date: the file is re-published almost daily, but not every chain is re-measured. A chain's prices
are dated by the OLDEST commit, counted back from the newest one, whose data for that chain is identical to the
newest data (17,000 shelf prices that stay identical for days means the chain was not re-measured). When every
commit we can list is identical, the real date is unknown (older): such prices always count as outdated.

Pure functions here; network access is isolated in fetch_commits / fetch_dataset (no keys, no accounts).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

from core import OPEN_DATA, number, same_name, text

SOURCE_NAME = "Checkjebon open data (MIT)"
REPO = "supermarkt/checkjebon"
DATA_PATH = "data/supermarkets.json"
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits?path={DATA_PATH}&per_page=100&page={{page}}"
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{{sha}}/{DATA_PATH}"
MAX_COMMIT_PAGES = 4

# Supplier name in the Leveranciers tab -> chain key in the dataset; house-brand name prefix (None = unknown).
CHAINS = {"jumbo": "jumbo", "albert heijn": "ah", "ah": "ah", "lidl": "lidl"}
HOUSE_PREFIX = {"jumbo": "jumbo ", "ah": "ah ", "lidl": None}   # Lidl article names carry no brand in this data

_SIZE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|kilo|gram|gr|g|ml|cl|liter|l|stuks|stuk|st)\b", re.I)
_MULTI = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|kilo|gram|gr|g|ml|cl|liter|l)\b", re.I)
_BASE = {"kg": ("g", 1000), "kilo": ("g", 1000), "gram": ("g", 1), "gr": ("g", 1), "g": ("g", 1),
         "ml": ("ml", 1), "cl": ("ml", 10), "liter": ("ml", 1000), "l": ("ml", 1000),
         "stuks": ("st", 1), "stuk": ("st", 1), "st": ("st", 1)}
# Sheet row unit -> (base unit, base units per row unit)
_ROW = {"gram": ("g", 1), "g": ("g", 1), "kg": ("g", 1000), "kilo": ("g", 1000),
        "ml": ("ml", 1), "liter": ("ml", 1000), "l": ("ml", 1000), "stuks": ("st", 1), "stuk": ("st", 1)}


def chain_of(supplier):
    return CHAINS.get(text(supplier).casefold())


def _norm(value):
    value = unicodedata.normalize("NFKD", text(value).lower())
    return "".join(c for c in value if not unicodedata.combining(c)).replace("-", " ")


def _stem(word):
    """Light Dutch stem so 'jonge' finds 'jong' and 'tomaten' finds 'tomaten' also finds 'tomatensoep'."""
    for suffix in ("en", "e", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[:-len(suffix)]
    return word


def search_terms(query):
    """'Salami / vleeswaren' -> [['salami'], ['vleeswar']]: alternatives, each a list of stemmed words (> 2 letters)."""
    return [t for t in ([_stem(w) for w in _norm(alt).split() if len(w) > 2] for alt in text(query).split("/")) if t]


def is_house_brand(chain, name):
    prefix = HOUSE_PREFIX.get(chain)
    return None if prefix is None else _norm(name).startswith(prefix)


def parse_size(size, name=""):
    """(amount, base unit) with base unit g / ml / st, from the size field or else the name; None if unknown.
    '3 x 400 g' -> (1200, 'g'); '0,75 l' -> (750, 'ml'); '6 stuks' -> (6, 'st')."""
    for raw in (size, name):
        raw = text(raw)
        m = _MULTI.search(raw)
        if m:
            base, factor = _BASE[m.group(3).lower()]
            return round(int(m.group(1)) * number(m.group(2)) * factor, 6), base
        m = _SIZE.search(raw)
        if m and number(m.group(1)):
            base, factor = _BASE[m.group(2).lower()]
            return round(number(m.group(1)) * factor, 6), base
    return None


def content_in_row_unit(size, name, row_unit):
    """Pack content expressed in the Sheet row unit, or None when it cannot be derived exactly.

    Weight/volume/count row units are converted. For pack-type row units (blikken, broden, bakjes, ...) one article
    is one pack, unless the article is a multipack ('3 x 400 g' / '4 stuks')."""
    parsed = parse_size(size, name)
    unit = text(row_unit).lower()
    if unit in _ROW:
        if parsed is None or parsed[1] != _ROW[unit][0]:
            return None
        return round(parsed[0] / _ROW[unit][1], 6)
    if not unit:
        return None
    m = _MULTI.search(f"{text(size)} {text(name)}")
    if m:
        return float(int(m.group(1)))
    if parsed and parsed[1] == "st":
        return parsed[0]
    return 1.0


def candidates(items, query, limit=8):
    """Best-matching articles for a search text. Ranking: all words of an alternative present, then house brand,
    then shorter names (fewer extras), then price. Returns [(article, house_brand)]."""
    alts = search_terms(query)
    if not alts:
        return []
    scored = []
    for chain, art in items:
        name = _norm(art.get("n"))
        if any(all(w in name for w in alt) for alt in alts):
            house = is_house_brand(chain, art.get("n"))
            scored.append(((0 if house else 1, len(name), number(art.get("p")) or 0), art, house))
    scored.sort(key=lambda s: s[0])
    return [(a, h) for _, a, h in scored[:limit]]


def article_url(base, link):
    return f"{text(base)}{text(link)}"


def chain_items(dataset, chain):
    """[(chain, article)] for one chain of a parsed dataset (list of supermarkets)."""
    for market in dataset or []:
        if market.get("n") == chain:
            return [(chain, a) for a in market.get("d") or []]
    return []


def chain_base(dataset, chain):
    return next((text(m.get("u")) for m in dataset or [] if m.get("n") == chain), "")


def find_by_url(dataset, chain, url):
    base = chain_base(dataset, chain)
    if not base or not text(url).startswith(base):
        return None
    link = text(url)[len(base):]
    return next((a for _, a in chain_items(dataset, chain) if text(a.get("l")) == link), None)


def price_values(article, url, row_unit, measured, exact=True):
    """The Prijzen row values for one linked article (inhoud/eenheidsprijs empty when the size is unknown).

    exact=False (real measurement date unknown, at the latest `measured`): 'Geldig t/m' stays empty, so the
    price can never count as current (core.open_status)."""
    price = number(article.get("p"))
    if price is None or price <= 0:
        return None
    content = content_in_row_unit(article.get("s"), article.get("n"), row_unit)
    return {"Prijs verpakking €": round(price, 2),
            "Verpakking": f"{format(content, 'g')} {text(row_unit)}" if content else "",
            "Prijs/eenheid €": round(price / content, 10) if content else "",
            "Prijssoort": OPEN_DATA, "Geldig t/m": measured.isoformat() if exact else "", "Prijsdatum": measured.isoformat(),
            "Bron": url}


def house_alternatives(dataset, chain, query, row_unit, limit=3):
    """Cheapest house-brand articles (by price per row unit) for a product; [] when house brand is unknown."""
    if HOUSE_PREFIX.get(chain) is None:
        return []
    out = []
    for art, house in candidates(chain_items(dataset, chain), query, limit=10_000):
        content = content_in_row_unit(art.get("s"), art.get("n"), row_unit) if house else None
        price = number(art.get("p"))
        if content and price:
            out.append({"name": text(art.get("n")), "price": round(price, 2), "content": content,
                        "unit_price": price / content, "url": article_url(chain_base(dataset, chain), art.get("l"))})
    return sorted(out, key=lambda x: x["unit_price"])[:limit]


# --- measurement date per chain ---------------------------------------------------------------------

def fingerprint(dataset, chains=("jumbo", "ah", "lidl")):
    """{chain: sha256 of that chain's article list} — identical hash = not re-measured."""
    out = {}
    for market in dataset or []:
        if market.get("n") in chains:
            blob = json.dumps(market.get("d"), sort_keys=True, ensure_ascii=False).encode("utf-8")
            out[market["n"]] = hashlib.sha256(blob).hexdigest()
    return out


def measured_dates(commits, fingerprint_at, chains=("jumbo", "ah", "lidl")):
    """{chain: (date, exact)} from commits [(sha, date)] newest first; fingerprint_at(sha) -> {chain: hash}.

    Gallop back from the newest commit (1, 2, 4, ... steps) until the chain's data differs, then bisect between the
    last identical and the first different commit. exact=False: identical in every listed commit (real date older).
    Assumes a chain's data never returns to an earlier identical state (true for real price lists)."""
    cache = {}

    def fp(i):
        if i not in cache:
            cache[i] = fingerprint_at(commits[i][0]) or {}
        return cache[i]

    out = {}
    if not commits:
        return out
    head = fp(0)
    for chain in chains:
        if chain not in head:
            continue
        same, step, diff = 0, 1, None
        while True:
            probe = min(same + step, len(commits) - 1)
            if fp(probe).get(chain) == head[chain]:
                same = probe
                if probe == len(commits) - 1:
                    break
                step *= 2
            else:
                diff = probe
                break
        if diff is None:
            out[chain] = (commits[same][1], False)
            continue
        while diff - same > 1:
            mid = (same + diff) // 2
            if fp(mid).get(chain) == head[chain]:
                same = mid
            else:
                diff = mid
        out[chain] = (commits[same][1], True)
    return out


# --- network (edges only) ---------------------------------------------------------------------------

def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "gezinsboodschappen-dashboard",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 (fixed https URLs)
        return resp.read()


def fetch_commits(get=_get, pages=MAX_COMMIT_PAGES):
    """[(sha, date in Europe/Amsterdam)] for the data file, newest first (public GitHub API, no key)."""
    out = []
    for page in range(1, pages + 1):
        batch = json.loads(get(COMMITS_URL.format(page=page)))
        for c in batch:
            stamp = datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))
            out.append((c["sha"], stamp.astimezone(ZoneInfo("Europe/Amsterdam")).date()))
        if len(batch) < 100:
            break
    return out


def fetch_dataset(sha, get=_get):
    return json.loads(get(RAW_URL.format(sha=sha), timeout=120))


def open_rows(prices):
    return [p for p in prices or [] if same_name(p.get("Prijssoort"), OPEN_DATA)]


def row_unit_of(product, shopping, inventory):
    """Sheet unit of a product: Aanvullijst first, else Voorraad."""
    for rows in (shopping, inventory):
        for x in rows or []:
            if same_name(x.get("Product"), product) and text(x.get("Eenheid")):
                return text(x.get("Eenheid"))
    return ""


def refresh_changes(links, dataset, dates, shopping, inventory):
    """What 'Prijzen bijwerken' writes: (changes for sheet_store.write_open_prices, report lines).

    links: the 'Open data' Prijzen rows (as the user saw them). A link whose article is gone, whose chain has no
    measurement date, or whose price is missing is reported and left untouched (never overwritten with guesses)."""
    changes, report = [], []
    for link in links:
        product, supplier = text(link.get("Product")), text(link.get("Leverancier"))
        chain = chain_of(supplier)
        if chain is None or chain not in dates:
            report.append(f"{product} bij {supplier}: geen open data/meetdatum voor deze supermarkt")
            continue
        article = find_by_url(dataset, chain, link.get("Bron"))
        if article is None:
            report.append(f"{product} bij {supplier}: artikel staat niet meer in de open data; opnieuw koppelen")
            continue
        measured, exact = dates[chain]
        values = price_values(article, text(link.get("Bron")), row_unit_of(product, shopping, inventory), measured, exact)
        if values is None:
            report.append(f"{product} bij {supplier}: geen prijs in de open data")
            continue
        if all(text(values[h]) == text(link.get(h)) for h in values if h != "Prijssoort"):
            continue          # nothing changed (same measurement): no write
        changes.append({"product": product, "supplier": supplier, "values": values, "seen": link})
    return changes, report
