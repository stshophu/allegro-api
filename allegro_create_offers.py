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
    ALLEGRO_RETURN_POLICY_ID       ← from allegro_get_account_ids.py
    ALLEGRO_IMPLIED_WARRANTY_ID    ← from allegro_get_account_ids.py
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

SHIPPING_RATES_ID    = os.environ["ALLEGRO_SHIPPING_RATES_ID"]
RETURN_POLICY_ID     = os.environ["ALLEGRO_RETURN_POLICY_ID"]
IMPLIED_WARRANTY_ID  = os.environ["ALLEGRO_IMPLIED_WARRANTY_ID"]

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
# Allegro fashion category ID mapping
# Key: (Kategorie, Subkategorie) or (Kategorie,) — lowercase stripped
# Value: Allegro category ID (allegro.pl production)
# Extend this table as needed — IDs verified against allegro.pl category tree
# ---------------------------------------------------------------------------
CATEGORY_MAP = {
    # ---- Women's clothing (Damenbekleidung) ----
    ("damenbekleidung", "kleider"):          147850,
    ("damenbekleidung", "tops"):             147857,
    ("damenbekleidung", "t-shirts"):         147857,
    ("damenbekleidung", "hosen"):            147847,
    ("damenbekleidung", "röcke"):            147853,
    ("damenbekleidung", "jacken"):           147845,
    ("damenbekleidung", "mäntel"):           147845,
    ("damenbekleidung", "pullover"):         147849,
    ("damenbekleidung", "strickwaren"):      147849,
    ("damenbekleidung", "blusen"):           147843,
    ("damenbekleidung", "hemden"):           147843,
    ("damenbekleidung", "shorts"):           147852,
    ("damenbekleidung", "sweatshirts"):      147856,
    ("damenbekleidung", "hoodies"):          147856,
    ("damenbekleidung", "sportbekleidung"):  147855,
    ("damenbekleidung", "overalls"):         147848,
    ("damenbekleidung", "jumpsuits"):        147848,
    ("damenbekleidung",):                    147841,  # fallback: Odzież damska

    # ---- Men's clothing (Herrenbekleidung) ----
    ("herrenbekleidung", "t-shirts"):        147879,
    ("herrenbekleidung", "hosen"):           147876,
    ("herrenbekleidung", "jeans"):           147876,
    ("herrenbekleidung", "hemden"):          147875,
    ("herrenbekleidung", "jacken"):          147874,
    ("herrenbekleidung", "mäntel"):          147874,
    ("herrenbekleidung", "pullover"):        147878,
    ("herrenbekleidung", "strickwaren"):     147878,
    ("herrenbekleidung", "shorts"):          147880,
    ("herrenbekleidung", "sweatshirts"):     147882,
    ("herrenbekleidung", "hoodies"):         147882,
    ("herrenbekleidung", "sportbekleidung"): 147883,
    ("herrenbekleidung",):                   147864,  # fallback: Odzież męska

    # ---- Shoes ----
    ("damenschuhe",):  147893,   # Buty damskie
    ("herrenschuhe",): 147907,   # Buty męskie
    ("schuhe",):       147893,   # generic fallback → women's

    # ---- Bags ----
    ("damentaschen",):  147921,  # Torebki damskie
    ("herrentaschen",): 147929,  # Torby męskie
    ("taschen",):       147921,  # generic fallback

    # ---- Accessories ----
    ("accessoires", "schmuck"):    147935,
    ("accessoires", "gürtel"):     147938,
    ("accessoires", "schals"):     147940,
    ("accessoires", "mützen"):     147941,
    ("accessoires", "handschuhe"): 147942,
    ("accessoires", "sonnenbrillen"): 147944,
    ("accessoires",): 147935,    # fallback: Akcesoria

    # ---- Kids ----
    ("kinderbekleidung",): 147800,
    ("kinderschuhe",):     147820,
}

# Last-resort fallback if nothing maps
DEFAULT_CATEGORY_ID = 147841  # Odzież damska


def resolve_category(kategorie, subkategorie, produktart):
    """Return the best-match Allegro category ID for a feed row."""
    kat = str(kategorie).strip().lower() if pd.notna(kategorie) else ""
    sub = str(subkategorie).strip().lower() if pd.notna(subkategorie) else ""
    art = str(produktart).strip().lower() if pd.notna(produktart) else ""

    # Try most-specific first: kat + sub
    for key_sub in [sub, art]:
        if (kat, key_sub) in CATEGORY_MAP:
            return CATEGORY_MAP[(kat, key_sub)]

    # Partial match on subkategorie keywords
    for (k, s), cat_id in CATEGORY_MAP.items():
        if len((k, s)) == 2 and k == kat and s and s in sub:
            return cat_id

    # Fallback to kat-only
    if (kat,) in CATEGORY_MAP:
        return CATEGORY_MAP[(kat,)]

    return DEFAULT_CATEGORY_ID


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
    return s is not None and s.isdigit() and 8 <= len(s) <= 14


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
# Build offer payload for POST /sale/product-offers
# ---------------------------------------------------------------------------

def build_offer_payload(row):
    gtin     = row["_gtin"]
    name     = str(row["Produktname"]).strip()[:75]
    price    = str(row["PRICE_PLN"])
    stock    = int(row["STOCK"])
    ext_id   = row["EXTERNAL_ID"]
    image    = row["Image URL"] if pd.notna(row.get("Image URL", None)) else None
    vendor   = str(row.get("Vendor", "")).strip()

    cat_id = resolve_category(
        row.get("Kategorie"), row.get("Subkategorie"), row.get("Produktart")
    )

    # Build a minimal description from what we have
    desc_parts = [vendor]
    if pd.notna(row.get("Produktart")):
        desc_parts.append(str(row["Produktart"]))
    description_html = "<p>" + " — ".join(desc_parts) + "</p>"

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
        "afterSalesServices": {
            "impliedWarranty": {"id": IMPLIED_WARRANTY_ID},
            "returnPolicy":    {"id": RETURN_POLICY_ID},
        },
        "payments": {
            "invoice": "VAT",
        },
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
                **({"images": images} if images else {}),
            },
            "quantity": {"value": 1},
        }],
        "description": {
            "sections": [{
                "items": [{"type": "TEXT", "content": description_html}]
            }]
        },
    }
    return payload


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

    new_rows = df[~df["EXTERNAL_ID"].isin(existing)]
    print(f"New rows to create: {len(new_rows)} (capped at {MAX_CREATE} this run)")

    created, skipped_zero_stock, failed = 0, 0, 0
    errors_log = []

    for _, row in new_rows.head(MAX_CREATE).iterrows():
        # Skip zero-stock items — no point listing something unavailable
        if int(row["STOCK"]) == 0:
            skipped_zero_stock += 1
            continue

        payload = build_offer_payload(row)
        resp = create_offer(access_token, payload)

        if resp.status_code in (200, 201, 202):
            data = resp.json()
            offer_id = data.get("id", "?")
            print(f"  ✓ Created {offer_id} | {row['EXTERNAL_ID']} | {row['Produktname'][:40]}")
            created += 1
        else:
            failed += 1
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            errors_log.append({
                "external_id": row["EXTERNAL_ID"],
                "name": row["Produktname"][:40],
                "gtin": row["_gtin"],
                "status_code": resp.status_code,
                "error": err_body,
            })
            print(f"  ✗ Failed {resp.status_code} | {row['EXTERNAL_ID']} | {err_body}", file=sys.stderr)

        # Gentle rate limiting: Allegro allows 9,000 req/min but creation
        # is slower to process — 2 per second is safe
        time.sleep(0.5)

    print(f"\nDone. Created: {created}, Skipped (zero stock): {skipped_zero_stock}, Failed: {failed}")
    if errors_log:
        print(f"\nFirst 5 errors:")
        for e in errors_log[:5]:
            print(f"  [{e['status_code']}] {e['external_id']} — {e['error']}")

    if CREATE_STATUS == "INACTIVE":
        print("\nOffers created as INACTIVE drafts. Review in Mój asortyment → Nieopublikowane,")
        print("then activate in bulk. To auto-activate, set ALLEGRO_CREATE_STATUS=ACTIVE.")


if __name__ == "__main__":
    main()
