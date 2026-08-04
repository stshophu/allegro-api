"""
allegro_create_offers.py — run by GitHub Actions alongside allegro_sync.py.

Creates new Allegro listings (as INACTIVE drafts) for feed rows whose
EXTERNAL_ID does not yet exist as an offer in the account.

Flow:
1. Refresh token (shared with allegro_sync.py — run that first in the same job,
   or call refresh_access_token() independently here).
2. Download live feed, compute EXTERNAL_IDs (same logic as sync script).
3. Fetch all current offer external IDs from account.
4. For each feed row NOT yet listed:
   a. Resolve Allegro category from product type via mapping table.
   b. POST /sale/product-offers with GTIN-based product lookup, INACTIVE status.
   c. Log success (offer ID) or failure (validation errors).
5. Print summary. Drafts appear in "Mój asortyment → Drafty" — review and
   activate in bulk once satisfied with the results.

Required env vars (GitHub secrets):
    ALLEGRO_CLIENT_ID
    ALLEGRO_CLIENT_SECRET
    ALLEGRO_REFRESH_TOKEN
    ALLEGRO_SHIPPING_RATES_ID      ← from allegro_get_account_ids.py
    (Return policy and implied warranty no longer required — Allegro
     unified these platform-wide on July 2, 2025)
    GH_PAT
    GH_REPO
    FEED_URL

Optional env vars:
    ALLEGRO_MAX_CREATE_PER_RUN     ← default 100 (rate-limit safety)
    ALLEGRO_CREATE_STATUS          ← "INACTIVE" (default, safe) or "ACTIVE"
"""

import os
import re
import sys
import time
import html
import json
import unicodedata
import requests
import pandas as pd
from nacl import encoding, public
import base64

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLIENT_ID     = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ALLEGRO_REFRESH_TOKEN"]
GH_PAT        = os.environ["GH_PAT"]
GH_REPO       = os.environ["GH_REPO"]
FEED_URL      = os.environ["FEED_URL"]

SHIPPING_RATES_ID = os.environ["ALLEGRO_SHIPPING_RATES_ID"]

# Fail fast if shipping rates ID is missing
if not SHIPPING_RATES_ID or not SHIPPING_RATES_ID.strip():
    print("ERROR: ALLEGRO_SHIPPING_RATES_ID secret is empty.")
    print("Run allegro_get_account_ids.py and add it as a repo secret.")
    sys.exit(1)

MAX_CREATE   = int(os.environ.get("ALLEGRO_MAX_CREATE_PER_RUN", "100"))
CREATE_STATUS = os.environ.get("ALLEGRO_CREATE_STATUS", "INACTIVE")  # safe default

TOKEN_URL = "https://allegro.pl/auth/oauth/token"
API_BASE  = "https://api.allegro.pl"
ACCEPT    = "application/vnd.allegro.public.v1+json"
CT        = "application/vnd.allegro.public.v1+json"

# Siebentaschen location (Hanau, DE)
LOCATION = {
    "countryCode": "DE",
    "city": "Hanau",
    "postCode": "63456",
}

# ---------------------------------------------------------------------------
# Category resolution via Allegro's own matching-categories endpoint.
#
# NOTE: this used to be a hardcoded CATEGORY_MAP of Allegro category IDs,
# but those IDs were wrong — e.g. 147841 (used as the default/fallback for
# almost everything) doesn't exist on allegro.pl at all ("Category with id
# 147841 not found"). allegro.pl's real "Odzież damska" ID is 76033, not
# 147841 — the whole table was unreliable, not just one entry, so instead
# of hand-fixing dozens of guessed IDs we ask Allegro directly via
# GET /sale/matching-categories?name=... (the endpoint Allegro itself
# recommends for this). Cached per (Kategorie, Subkategorie, Produktart)
# combo so a run of ~2900 rows only makes a handful of lookup calls.
# ---------------------------------------------------------------------------

_category_resolution_cache = {}

# Genderless/generic Kategorie values seen in the feed, mapped to their
# proper Polish equivalent so the matching-categories query has a correct
# anchor term (see resolve_category below). IMPORTANT: this must only cover
# genuinely non-specific umbrella terms — "Clothing", "Accessories" and
# their Italian equivalents. Most of the feed's Kategorie values are
# already specific enough on their own (Shoes, Bags, Jacket, Sneakers...)
# and must NOT be run through this map: forcing e.g. "odzież" (clothing)
# onto a Sunglasses or Bags row is exactly as wrong as the earlier
# untranslated-English bug — a wrong anchor term is not a signal, it's
# noise that can push the match to something unrelated.
GENERIC_KATEGORIE_TRANSLATIONS = {
    "clothing": "odzież",
    "apparel": "odzież",
    "abbigliamento": "odzież",
    "women clothing": "odzież damska",
    "accessories": "akcesoria",
    "clothing accessories": "akcesoria",
    "accessori": "akcesoria",
}

# Kategorie values seen in the feed that are data-entry junk, not real
# category labels (e.g. leftover internal codes). No sensible Polish
# translation exists for these, so they're dropped from the query rather
# than passed through — relying on Subkategorie/Produktart alone.
JUNK_KATEGORIE_TERMS = {"def", "jane", "logo", "holder", "home"}


def resolve_category(access_token, kategorie, subkategorie, produktart):
    """Ask Allegro for the best-match leaf category ID for a feed row,
    via GET /sale/matching-categories. Returns None if no match is found
    (caller should skip the row rather than guess)."""
    kat = str(kategorie).strip() if pd.notna(kategorie) else ""
    sub = str(subkategorie).strip() if pd.notna(subkategorie) else ""
    art = str(produktart).strip() if pd.notna(produktart) else ""

    cache_key = (kat.lower(), sub.lower(), art.lower())
    if cache_key in _category_resolution_cache:
        return _category_resolution_cache[cache_key]

    # Some feed rows use a generic, genderless Kategorie value ("Clothing",
    # "Accessories") instead of a specific one. Dropping it from the query
    # entirely (a prior fix) was WORSE than the original problem: bare
    # single-word queries like "Shirts" or "Tops" drifted into completely
    # unrelated categories (observed: books and music-album categories,
    # asking for ISBN/Autor or Wykonawca/Wytwórnia on a shirt). An anchor
    # term is needed — but it must be the *correct* Polish term for what
    # the item actually is, not a blanket "odzież" (clothing) forced onto
    # everything: that was itself wrong for accessories/bags/sunglasses
    # rows, which aren't clothing. Values that are outright junk data (not
    # a real category at all) are dropped rather than translated, since
    # there's nothing meaningful to translate.
    kat_lower = kat.lower()
    if kat_lower in JUNK_KATEGORIE_TERMS:
        kat_for_query = ""
    else:
        kat_for_query = GENERIC_KATEGORIE_TRANSLATIONS.get(kat_lower, kat)
    query = " ".join(p for p in [art, sub, kat_for_query] if p).strip() or kat
    headers = {"Authorization": f"Bearer {access_token}", "Accept": ACCEPT}
    cat_id = None
    try:
        resp = requests.get(
            f"{API_BASE}/sale/matching-categories",
            headers=headers, params={"name": query}, timeout=30,
        )
        resp.raise_for_status()
        matches = resp.json().get("matchingCategories", [])
        if matches:
            cat_id = matches[0]["id"]
    except Exception as e:
        print(f"WARNING: category matching failed for '{query}': {e}", file=sys.stderr)

    _category_resolution_cache[cache_key] = cat_id
    return cat_id



# ---------------------------------------------------------------------------
# Auth helpers (mirrored from allegro_sync.py)
# ---------------------------------------------------------------------------

def refresh_access_token():
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    if not resp.ok:
        print(f"Token refresh failed: {resp.status_code} {resp.text}", file=sys.stderr)
        resp.raise_for_status()
    tokens = resp.json()
    return tokens["access_token"], tokens["refresh_token"]


def update_github_secret(secret_name, secret_value):
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }
    key_resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key",
        headers=headers, timeout=30,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()
    pub_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pub_key).encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(sealed).decode("utf-8")
    requests.put(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
        timeout=30,
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Persisted list of permanently-blocked EXTERNAL_IDs (no category match,
# missing required Allegro data, invalid GTIN). Without this, MAX_CREATE=100
# re-attempts the exact same doomed rows every run since new_rows.head()
# always starts from the top of the feed — the thousands of rows behind
# them never get a turn. Stored as a small JSON file committed back to the
# repo, same GitHub Contents API pattern as the secret rotation above.
# ---------------------------------------------------------------------------

BLOCKED_IDS_PATH = "blocked_offer_ids.json"

# Bump this whenever category-resolution or parameter-resolution logic
# changes in a way that could change the outcome for a previously-blocked
# row (e.g. the Kategorie-translation fix, a new dictionary-matching
# strategy). Entries blocked under an older version are treated as stale —
# not excluded from this run — so they automatically get retried under the
# new logic instead of needing a manual blocked_offer_ids.json reset.
BLOCKED_LOGIC_VERSION = 3


def fetch_blocked_ids():
    """Returns (all_blocked, active_blocked, sha).
    all_blocked: every entry ever recorded, keyed by EXTERNAL_ID -> {reason, code_version} — kept and passed to save_blocked_ids so history isn't lost.
    active_blocked: subset still tagged with the current BLOCKED_LOGIC_VERSION — only these are excluded from this run's candidate rows.
    sha is None if the file doesn't exist yet (first run)."""
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{BLOCKED_IDS_PATH}",
        headers=headers, timeout=30,
    )
    if resp.status_code == 404:
        return {}, {}, None
    resp.raise_for_status()
    data = resp.json()
    try:
        raw = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
    except Exception:
        raw = {}

    all_blocked = {}
    active_blocked = {}
    for ext_id, entry in raw.items():
        # Migrate legacy plain-string entries (pre-versioning) to the
        # versioned shape, treated as stale so they get retried once.
        if isinstance(entry, str):
            entry = {"reason": entry, "code_version": 0}
        all_blocked[ext_id] = entry
        if entry.get("code_version") == BLOCKED_LOGIC_VERSION:
            active_blocked[ext_id] = entry

    return all_blocked, active_blocked, data["sha"]


def save_blocked_ids(all_blocked, sha):
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    content_b64 = base64.b64encode(
        json.dumps(all_blocked, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).decode("utf-8")
    payload = {
        "message": f"Update blocked offer IDs ({len(all_blocked)} total)",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha
    resp = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/{BLOCKED_IDS_PATH}",
        headers=headers, json=payload, timeout=30,
    )
    if not resp.ok:
        print(f"WARNING: failed to save blocked-ids file: {resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Feed helpers (same as allegro_sync.py)
# ---------------------------------------------------------------------------

def get_eur_pln_rate():
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=PLN", timeout=15)
        r.raise_for_status()
        return float(r.json()["rates"]["PLN"])
    except Exception as e:
        print(f"WARNING: FX lookup failed ({e}), using 4.29", file=sys.stderr)
        return 4.29


def slugify(s):
    s = str(s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def to_gtin_str(x):
    if pd.isna(x):
        return None
    s = str(x).strip()
    if re.match(r"^\d+\.0$", s):
        s = s[:-2]
    return s


def valid_gtin(s):
    if not isinstance(s, str):
        return False
    return s.isdigit() and 8 <= len(s) <= 14


def parse_variant(v):
    """Split a feed 'Variant' value into (color, size).

    Examples seen in the feed:
        "Blue / 40"       -> ("Blue", "40")
        "nero / XL"       -> ("nero", "XL")
        "Red / IT50 | L"  -> ("Red", "IT50 | L")
        "M"               -> (None, "M")           # size only, no color
        "EU39/US9"        -> (None, "EU39/US9")     # compound size, not color/size
    Only a " / " (with surrounding spaces) is treated as the color/size
    separator, so slash-joined size codes like "EU39/US9" pass through
    untouched as a single size value.
    """
    if pd.isna(v):
        return None, None
    s = str(v).strip()
    if not s:
        return None, None
    if " / " in s:
        color, size = s.split(" / ", 1)
        color = color.strip() or None
        size = size.strip() or None
        return color, size
    return None, s


def build_feed():
    fx = get_eur_pln_rate()
    df = pd.read_csv(FEED_URL)
    df["_gtin"] = df["EAN/GTIN"].apply(to_gtin_str)
    df = df[df["_gtin"].apply(valid_gtin)].copy()
    ext_ids, seen = [], {}
    for handle, variant in zip(df["Handle"], df["Variant"]):
        base = slugify(f"{handle}-{variant}")[:60]
        if base not in seen:
            seen[base] = 0
            ext_ids.append(base)
        else:
            seen[base] += 1
            ext_ids.append(f"{base}-{seen[base]}")
    df["EXTERNAL_ID"] = ext_ids
    df["PRICE_PLN"] = (df["Preis (Brutto)"] * fx).round(2)
    df["STOCK"] = df["Inventory"].astype(int)
    return df


# ---------------------------------------------------------------------------
# Fetch existing external IDs from Allegro
# ---------------------------------------------------------------------------

def fetch_existing_external_ids(access_token):
    headers = {"Authorization": f"Bearer {access_token}", "Accept": ACCEPT}
    ids = set()
    offset, limit = 0, 1000
    while True:
        resp = requests.get(
            f"{API_BASE}/sale/offers",
            headers=headers,
            params={
                "limit": limit, "offset": offset,
                "publication.status": ["ACTIVE", "INACTIVE", "ACTIVATING"],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("offers", [])
        if not batch:
            break
        for o in batch:
            ext = (o.get("external") or {}).get("id")
            if ext:
                ids.add(ext)
        offset += limit
        if offset >= data.get("totalCount", 0):
            break
    return ids


# ---------------------------------------------------------------------------
# Category parameters (needed to auto-create a new Allegro product when the
# GTIN isn't already in Allegro's catalog — see MatchingProductForIdNotFoundException
# and DuplicateDetectionMissingParametersException)
# ---------------------------------------------------------------------------

_category_params_cache = {}

EAN_PARAM_ID = "225693"  # universal "EAN (GTIN)" parameter, seen across categories

def fetch_category_parameters(access_token, category_id):
    """GET /sale/categories/{id}/parameters, cached per run."""
    if category_id in _category_params_cache:
        return _category_params_cache[category_id]
    headers = {"Authorization": f"Bearer {access_token}", "Accept": ACCEPT}
    try:
        resp = requests.get(
            f"{API_BASE}/sale/categories/{category_id}/parameters",
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        params = resp.json().get("parameters", [])
    except Exception as e:
        print(f"WARNING: could not fetch parameters for category {category_id}: {e}", file=sys.stderr)
        params = []
    _category_params_cache[category_id] = params
    return params


def resolve_parameter_value(param, text_value):
    """Build a parameters-array entry for one category parameter, given a
    candidate text value from the feed row.

    Dictionary-type parameters are often *strict* (no free text accepted,
    despite Allegro's own docs suggesting you can pass a name in `values` —
    that only works when the name is already in the dictionary). So for
    dictionary params we only ever submit a matched option's ID: exact
    match, then substring match.

    If neither matches, some dictionary parameters expose an "ambiguous"
    catch-all option via options.ambiguousValueId (Allegro's own field for
    this — NOT found by guessing an "Inny"/"Other" option by name: picking
    that alone gets rejected as "Custom value proposition has not been
    provided for ambiguous value in parameter X"). When present, it must be
    submitted together with the free-text value describing what it is.

    Non-dictionary (text/numeric) parameters accept free text directly.
    """
    if not text_value:
        return None
    text_value = str(text_value).strip()
    if not text_value:
        return None

    options = param.get("dictionary") or []
    if param.get("type") == "dictionary" or options:
        match = next(
            (o for o in options if o.get("value", "").strip().lower() == text_value.lower()),
            None,
        )
        if not match:
            match = next(
                (o for o in options if text_value.lower() in o.get("value", "").strip().lower()),
                None,
            )
        if match:
            return {"id": param["id"], "valuesIds": [match["id"]]}

        param_options = param.get("options") or {}
        ambiguous_id = param_options.get("ambiguousValueId")
        if ambiguous_id and param_options.get("customValuesEnabled"):
            return {"id": param["id"], "valuesIds": [ambiguous_id], "values": [text_value]}
        return None

    return {"id": param["id"], "values": [text_value]}


def build_product_parameters(access_token, category_id, gtin, field_values):
    """Product-level parameters needed so Allegro can auto-create a new
    catalog product for a GTIN it doesn't already recognize.

    field_values: ordered list of (keyword_hints, value) pairs. For each
    category parameter, the first entry whose hint matches the parameter
    name (and has a non-empty value) is used to fill it.

    Returns (params, missing_required_names) — missing_required_names lists
    the category's requiredForProduct parameters that couldn't be filled
    (no feed data, no dictionary match, no usable ambiguous fallback), so
    the caller can skip the row before ever calling the API with a request
    that's certain to fail.
    """
    params = [{"id": EAN_PARAM_ID, "values": [gtin]}]
    cat_params = fetch_category_parameters(access_token, category_id)
    used_param_ids = {EAN_PARAM_ID}

    for cat_param in cat_params:
        name_lower = cat_param.get("name", "").lower()
        for hints, value in field_values:
            if any(h in name_lower for h in hints):
                entry = resolve_parameter_value(cat_param, value)
                if entry and entry["id"] not in used_param_ids:
                    params.append(entry)
                    used_param_ids.add(entry["id"])
                break  # first matching hint group wins, whether or not it resolved

    missing_required = [
        p.get("name", p["id"]) for p in cat_params
        if p.get("requiredForProduct") and p["id"] not in used_param_ids
    ]
    return params, missing_required


# ---------------------------------------------------------------------------
# Build offer payload for POST /sale/product-offers
# ---------------------------------------------------------------------------

def build_offer_payload(access_token, row):
    gtin     = row["_gtin"]
    name     = str(row["Produktname"]).strip()[:75]
    color, size = parse_variant(row.get("Variant"))
    price    = str(row["PRICE_PLN"])
    stock    = int(row["STOCK"])
    ext_id   = row["EXTERNAL_ID"]
    image    = row["Image URL"] if pd.notna(row.get("Image URL", None)) else None
    vendor   = str(row.get("Vendor", "")).strip()
    sku      = str(row.get("Artikelnummer im Shop", "")).strip()
    art_val  = str(row.get("Produktart", "")).strip() if pd.notna(row.get("Produktart")) else ""

    cat_id = resolve_category(
        access_token, row.get("Kategorie"), row.get("Subkategorie"), row.get("Produktart")
    )
    if cat_id is None:
        return None, "no matching Allegro category"

    product_params, missing_required = build_product_parameters(
        access_token, cat_id, gtin,
        [
            (("marka", "brand"), vendor),
            (("kod producenta", "numer katalogowy"), sku),
            (("rozmiar",), size),
            (("kolor", "kolor producenta"), color),
            (("rodzaj",), art_val),
        ],
    )
    if missing_required:
        return None, f"missing required product data: {', '.join(missing_required)}"

    # Minimal Polish description to satisfy allegro.pl language requirement
    # NOTE: Allegro's description sanitizer only allows a small HTML subset
    # (p, b, ... ) and rejects <br/> outright (422 VALIDATION_ERROR:
    # 'Invalid tag: "br"'). So each line gets its own <p> instead of being
    # joined with <br/>. All dynamic text is HTML-escaped (not just "&")
    # so a stray "<" or ">" in vendor/color/size data can't produce another
    # invalid-tag error.
    brand_safe = html.escape(vendor, quote=False)
    produktart = html.escape(str(row["Produktart"]), quote=False) if pd.notna(row.get("Produktart")) else ""
    color_safe = html.escape(color or "", quote=False)
    size_safe  = html.escape(size or "", quote=False)

    desc_lines = [f"Marka: {brand_safe}"]
    if produktart:
        desc_lines.append(f"Rodzaj: {produktart}")
    if color_safe:
        desc_lines.append(f"Kolor: {color_safe}")
    if size_safe:
        desc_lines.append(f"Rozmiar: {size_safe}")
    desc_lines.append("Stan: Nowy")
    desc_lines.append("Produkt oryginalny.")
    description_html = "".join(f"<p>{line}</p>" for line in desc_lines)

    images = [image] if image else []

    payload = {
        "name": name,
        "external": {"id": ext_id},
        "publication": {
            "status": CREATE_STATUS,
            "republish": True,
        },
        "sellingMode": {
            "format": "BUY_NOW",
            "price": {"amount": price, "currency": "PLN"},
        },
        "stock": {
            "available": stock,
            "unit": "UNIT",
        },
        "location": LOCATION,
        "delivery": {
            "shippingRates": {"id": SHIPPING_RATES_ID},
            "handlingTime": "P3D",
        },
        "payments": {
            "invoice": "VAT",
        },
        **({"images": images} if images else {}),
        # Offer-level parameters: condition = New (11323_1)
        "parameters": [
            {"id": "11323", "valuesIds": ["11323_1"]},
        ],
        "productSet": [{
            "product": {
                "id": gtin,
                "idType": "GTIN",
                "name": name,
                "category": {"id": str(cat_id)},
                "parameters": product_params,
                **({"images": images} if images else {}),

            },
            "quantity": {"value": 1},
        }],
        "language": "pl-PL",
        "description": {
            "sections": [{
                "items": [{"type": "TEXT", "content": description_html}]
            }]
        },
    }
    return payload, None


# ---------------------------------------------------------------------------
# Create offers
# ---------------------------------------------------------------------------

def create_offer(access_token, payload):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": CT,
        "Accept": ACCEPT,
    }
    resp = requests.post(
        f"{API_BASE}/sale/product-offers",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if resp.status_code == 422:
        try:
            errors = resp.json().get("errors", [])
            codes = {e.get("code") for e in errors}
            retry = False

            if "CATEGORY_MISMATCH" in codes:
                for err in errors:
                    if err.get("code") == "CATEGORY_MISMATCH":
                        correct_cat = err.get("metadata", {}).get("existingCategoryId")
                        if correct_cat:
                            payload["productSet"][0]["product"]["category"]["id"] = str(correct_cat)
                            retry = True
                        break
            elif "MatchingProductForDataNotFoundException" in codes:
                # Allegro couldn't match this GTIN + supplied data to any
                # existing catalog product. Per Allegro's docs, sending both
                # a GTIN and full product data is a hybrid of their two
                # documented variants ("product already in catalog" vs.
                # "create a new product") — dropping id/idType switches
                # cleanly to the "create new product from data" variant,
                # which doesn't attempt any GTIN-based matching at all.
                product = payload["productSet"][0]["product"]
                product.pop("id", None)
                product.pop("idType", None)
                retry = True

            if retry:
                resp = requests.post(
                    f"{API_BASE}/sale/product-offers",
                    headers=headers,
                    json=payload,
                    timeout=30,
                )
        except Exception:
            pass
    return resp


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    access_token, new_refresh = refresh_access_token()
    if new_refresh != REFRESH_TOKEN:
        update_github_secret("ALLEGRO_REFRESH_TOKEN", new_refresh)
        print("Rotated ALLEGRO_REFRESH_TOKEN.")

    df = build_feed()
    print(f"Feed rows with valid GTIN: {len(df)}")

    existing = fetch_existing_external_ids(access_token)
    print(f"Existing Allegro offers (any status): {len(existing)}")

    all_blocked, active_blocked, blocked_sha = fetch_blocked_ids()
    stale_count = len(all_blocked) - len(active_blocked)
    print(f"Previously blocked rows on file: {len(active_blocked)} active"
          + (f", {stale_count} stale (will be retried under current logic)" if stale_count else ""))
    blocked_snapshot_before = json.dumps(all_blocked, sort_keys=True)

    new_rows = df[~df["EXTERNAL_ID"].isin(existing) & ~df["EXTERNAL_ID"].isin(active_blocked.keys())]
    print(f"New rows to create: {len(new_rows)} (capped at {MAX_CREATE} this run, "
          f"{len(active_blocked)} previously-blocked rows excluded)")

    created, skipped_zero_stock, skipped_no_category, skipped_missing_data, skipped_invalid_gtin, failed = 0, 0, 0, 0, 0, 0
    errors_log = []

    for _, row in new_rows.head(MAX_CREATE).iterrows():
        # Skip zero-stock items — no point listing something unavailable
        if int(row["STOCK"]) == 0:
            skipped_zero_stock += 1
            continue

        payload, skip_reason = build_offer_payload(access_token, row)
        if payload is None:
            print(f"  ⚠ Skipping {row['EXTERNAL_ID']} | {row['Produktname'][:40]} — {skip_reason}", file=sys.stderr)
            if skip_reason and skip_reason.startswith("no matching"):
                skipped_no_category += 1
            else:
                skipped_missing_data += 1
            all_blocked[row["EXTERNAL_ID"]] = {"reason": skip_reason, "code_version": BLOCKED_LOGIC_VERSION}
            continue
        resp = create_offer(access_token, payload)

        if resp.status_code in (200, 201, 202):
            data = resp.json()
            offer_id = data.get("id", "?")
            print(f"  ✓ Created {offer_id} | {row['EXTERNAL_ID']} | {row['Produktname'][:40]}")
            created += 1
        else:
            try:
                err_body = resp.json()
                errs = err_body.get("errors", [])
                # Invalid GS1 GTINs are permanently bad, not a transient
                # failure — skip cleanly and persist rather than counting
                # as a failure to retry.
                gtin_invalid = any(
                    "EAN" in e.get("userMessage", "") or
                    "GTIN" in e.get("userMessage", "") or
                    "GS1" in e.get("userMessage", "") or
                    "does not exist" in e.get("userMessage", "")
                    for e in errs
                )
            except Exception:
                err_body = resp.text
                gtin_invalid = False

            if gtin_invalid:
                print(f"  ⚠ Invalid GS1 GTIN {row['_gtin']} | {row['EXTERNAL_ID']} — skipping (not in GS1 database)", file=sys.stderr)
                skipped_invalid_gtin += 1
                all_blocked[row["EXTERNAL_ID"]] = {"reason": "invalid GS1 GTIN", "code_version": BLOCKED_LOGIC_VERSION}
            else:
                print(f"  ✗ Failed {resp.status_code} | {row['EXTERNAL_ID']} | {err_body}", file=sys.stderr)
                failed += 1
                errors_log.append({
                    "external_id": row["EXTERNAL_ID"],
                    "name": row["Produktname"][:40],
                    "gtin": row["_gtin"],
                    "status_code": resp.status_code,
                    "error": err_body,
                })

        # Gentle rate limiting: Allegro allows 9,000 req/min but creation
        # is slower to process — 2 per second is safe
        time.sleep(0.5)

    print(f"\nDone. Created: {created}, Skipped (zero stock): {skipped_zero_stock}, "
          f"Skipped (no category match): {skipped_no_category}, "
          f"Skipped (missing required data): {skipped_missing_data}, "
          f"Skipped (invalid GTIN): {skipped_invalid_gtin}, Failed: {failed}")
    if errors_log:
        print(f"\nFirst 5 errors:")
        for e in errors_log[:5]:
            print(f"  [{e['status_code']}] {e['external_id']} — {e['error']}")

    if CREATE_STATUS == "INACTIVE":
        print("\nOffers created as INACTIVE drafts. Review in Mój asortyment → Nieopublikowane,")
        print("then activate in bulk. To auto-activate, set ALLEGRO_CREATE_STATUS=ACTIVE.")

    if json.dumps(all_blocked, sort_keys=True) != blocked_snapshot_before:
        save_blocked_ids(all_blocked, blocked_sha)
        print(f"\nSaved updated blocked rows ({len(all_blocked)} total) to "
              f"{BLOCKED_IDS_PATH} — future runs will skip currently-blocked ones.")


if __name__ == "__main__":
    main()
