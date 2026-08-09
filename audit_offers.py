"""
audit_offers.py — READ-ONLY. Run manually (not scheduled).

Finds every live Allegro offer whose sygnatura (external.id) does NOT match
what reshape_feed_for_import_and_list.py / allegro_sync.py would generate for
it from the current feed. This is the confirmed signature of an offer that
Import-and-List merged into a pre-existing, unrelated Allegro Catalog product
instead of creating fresh from your CSV row (see: Tom Ford Tops Yellow,
Dolce & Gabbana XOXO Polo, 3x Alpha Studio knitwear — all found this way,
one screenshot at a time; this script finds all of them in one pass).

For every mismatched offer, fetches its full live details (EAN, image count,
price, name) via GET /sale/offers/{id}, then tries to find the matching feed
row by EAN so you can see exactly what SHOULD be there vs. what actually is.

Makes NO changes to Allegro — pure read + report. Writes audit_report.csv.

Required environment variables (same as allegro_sync.py):
    ALLEGRO_CLIENT_ID
    ALLEGRO_CLIENT_SECRET
    ALLEGRO_REFRESH_TOKEN
    GH_PAT
    GH_REPO
    FEED_URL
"""

import os
import re
import sys
import unicodedata
import requests
import pandas as pd
from nacl import encoding, public
import base64

CLIENT_ID = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ALLEGRO_REFRESH_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
GH_REPO = os.environ["GH_REPO"]
FEED_URL = os.environ["FEED_URL"]

TOKEN_URL = "https://allegro.pl/auth/oauth/token"
API_BASE = "https://api.allegro.pl"
OUTPUT_PATH = "audit_report.csv"


# --- auth (identical to allegro_sync.py) ------------------------------------

def refresh_access_token():
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()
    return tokens["access_token"], tokens["refresh_token"]


def update_github_secret(secret_name, secret_value):
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    key_resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key",
        headers=headers, timeout=30,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()
    public_key = public.PublicKey(key_data["key"].encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")
    put_resp = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
        timeout=30,
    )
    put_resp.raise_for_status()


# --- feed (identical slug logic to allegro_sync.py / reshape script) --------

def get_eur_pln_rate():
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=EUR&to=PLN", timeout=15)
        r.raise_for_status()
        return float(r.json()["rates"]["PLN"])
    except Exception as e:
        print(f"WARNING: FX lookup failed ({e}), falling back to 4.29", file=sys.stderr)
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
    return isinstance(s, str) and s.isdigit() and 8 <= len(s) <= 14


def build_feed():
    """Returns (by_external_id, by_gtin) dicts for cross-referencing."""
    df = pd.read_csv(FEED_URL)
    df["_gtin"] = df["EAN/GTIN"].apply(to_gtin_str)
    df = df[df["_gtin"].apply(valid_gtin)].copy()

    ext_ids = []
    seen = {}
    for handle, variant in zip(df["Handle"], df["Variant"]):
        base = slugify(f"{handle}-{variant}")[:60]
        if base not in seen:
            seen[base] = 0
            ext_ids.append(base)
        else:
            seen[base] += 1
            ext_ids.append(f"{base}-{seen[base]}")
    df["EXTERNAL_ID"] = ext_ids

    by_external_id = {}
    by_gtin = {}
    for _, r in df.iterrows():
        row = {
            "gtin": r["_gtin"],
            "name": r["Produktname"],
            "external_id": r["EXTERNAL_ID"],
            "image": r.get("Image URL", ""),
        }
        by_external_id[r["EXTERNAL_ID"]] = row
        by_gtin[r["_gtin"]] = row
    return by_external_id, by_gtin


# --- live offers --------------------------------------------------------

def fetch_all_offers(access_token):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    offers = []
    offset = 0
    limit = 1000
    while True:
        resp = requests.get(
            f"{API_BASE}/sale/offers",
            headers=headers,
            params={"limit": limit, "offset": offset,
                    "publication.status": ["ACTIVE", "INACTIVE", "ACTIVATING"]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("offers", [])
        if not batch:
            break
        offers.extend(batch)
        offset += limit
        if offset >= data.get("totalCount", 0):
            break
    return offers


def fetch_offer_details(access_token, offer_id):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    resp = requests.get(f"{API_BASE}/sale/offers/{offer_id}", headers=headers, timeout=30)
    if resp.status_code != 200:
        return None
    return resp.json()


def extract_ean(offer_detail):
    for p in offer_detail.get("parameters", []):
        if p.get("name", "").upper() in ("EAN (GTIN)", "EAN", "GTIN"):
            vals = p.get("values") or []
            return vals[0] if vals else None
    return None


# --- main -----------------------------------------------------------------

def main():
    access_token, new_refresh_token = refresh_access_token()
    if new_refresh_token != REFRESH_TOKEN:
        update_github_secret("ALLEGRO_REFRESH_TOKEN", new_refresh_token)
        print("Rotated ALLEGRO_REFRESH_TOKEN secret.")

    by_external_id, by_gtin = build_feed()
    print(f"Feed rows (valid GTIN): {len(by_external_id)}")

    live_offers = fetch_all_offers(access_token)
    print(f"Live Allegro offers fetched: {len(live_offers)}")

    mismatched = []
    for o in live_offers:
        ext = (o.get("external") or {}).get("id")
        if ext in by_external_id:
            continue  # correctly matches feed, nothing to audit
        mismatched.append(o)

    print(f"Matched (sygnatura matches feed): {len(live_offers) - len(mismatched)}")
    print(f"MISMATCHED (sygnatura does NOT match feed — auditing each): {len(mismatched)}")

    rows = []
    for o in mismatched:
        offer_id = o["id"]
        live_name = o.get("name", "")
        live_ext_id = (o.get("external") or {}).get("id", "")
        detail = fetch_offer_details(access_token, offer_id)
        live_ean = extract_ean(detail) if detail else None
        live_images = detail.get("images", []) if detail else []
        live_price = o.get("sellingMode", {}).get("price", {}).get("amount")

        expected = by_gtin.get(live_ean) if live_ean else None

        if expected is None:
            classification = "CATALOG_MERGE — EAN not in your feed at all"
        elif expected["external_id"] != live_ext_id:
            classification = "CATALOG_MERGE — EAN matches your product but sygnatura is wrong"
        else:
            classification = "unclear"

        rows.append({
            "offer_id": offer_id,
            "offer_url": f"https://allegro.pl/oferta/{offer_id}",
            "live_name": live_name,
            "live_sygnatura": live_ext_id,
            "live_ean": live_ean,
            "live_image_count": len(live_images),
            "live_price": live_price,
            "expected_name": expected["name"] if expected else "",
            "expected_sygnatura": expected["external_id"] if expected else "",
            "expected_image": expected["image"] if expected else "",
            "classification": classification,
        })

    report = pd.DataFrame(rows)
    report.to_csv(OUTPUT_PATH, index=False, encoding="utf-8")
    print(f"Wrote {len(report)} flagged offers to {OUTPUT_PATH}")
    if len(report):
        print(report["classification"].value_counts().to_string())


if __name__ == "__main__":
    main()
