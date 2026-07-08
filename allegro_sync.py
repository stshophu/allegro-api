"""
allegro_sync.py — runs every 30 minutes via GitHub Actions.

What it does, each run:
1. Refreshes the Allegro OAuth access token using the stored refresh_token,
   then rotates the stored refresh_token (Allegro issues a new one on every
   refresh; the old one stops working).
2. Downloads your live Shopify feed (siebentaschen_product_feed.csv).
3. Recomputes GTIN / EXTERNAL_ID / PRICE (EUR->PLN at today's live rate) /
   STOCK exactly like the original one-off conversion did.
4. Fetches all of your current Allegro offers (paginated) to see their
   live price/stock and match them to your feed rows via "external.id"
   (the EXTERNAL_ID column from the CSV becomes Allegro's offer "sygnatura").
5. Diffs target vs. current. Only offers where price or stock actually
   changed get queued for an update.
6. Pushes changes via POST /sale/offer-bulk-modification-commands in
   batches of 25 (Allegro's per-request limit for distinct per-offer
   values), and reports a summary.

Required environment variables (set as GitHub repo secrets):
    ALLEGRO_CLIENT_ID
    ALLEGRO_CLIENT_SECRET
    ALLEGRO_REFRESH_TOKEN
    GH_PAT                  (GitHub PAT, 'repo' scope, to rotate the secret above)
    GH_REPO                 (e.g. "stshophu/siebentaschen-feed")
    FEED_URL                (raw CSV URL, e.g. the GitHub raw feed link)
"""

import os
import re
import sys
import uuid
import time
import base64
import unicodedata
import requests
import pandas as pd
from nacl import encoding, public

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CLIENT_ID = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ALLEGRO_REFRESH_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
GH_REPO = os.environ["GH_REPO"]
FEED_URL = os.environ["FEED_URL"]

TOKEN_URL = "https://allegro.pl/auth/oauth/token"
API_BASE = "https://api.allegro.pl"
MARKETPLACE = "allegro-pl"
TARGET_CURRENCY = "PLN"

# ---------------------------------------------------------------------------
# Step 1: refresh access token + rotate refresh_token secret on GitHub
# ---------------------------------------------------------------------------

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
    """Encrypts and writes a new value for a GitHub Actions repo secret."""
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


# ---------------------------------------------------------------------------
# Step 2-3: rebuild target feed (GTIN / EXTERNAL_ID / PRICE / STOCK)
# ---------------------------------------------------------------------------

def get_eur_pln_rate():
    # Free, no-auth FX API. Falls back to a fixed rate if it's unreachable.
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
    if s is None:
        return False
    return s.isdigit() and 8 <= len(s) <= 14


def build_target_feed():
    fx_rate = get_eur_pln_rate()
    print(f"Using EUR->PLN rate: {fx_rate}")

    df = pd.read_csv(FEED_URL)
    df["_gtin_str"] = df["EAN/GTIN"].apply(to_gtin_str)
    df = df[df["_gtin_str"].apply(valid_gtin)].copy()

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

    df["PRICE"] = (df["Preis (Brutto)"] * fx_rate).round(2)
    df["STOCK"] = df["Inventory"].astype(int)

    return df[["EXTERNAL_ID", "PRICE", "STOCK"]].set_index("EXTERNAL_ID")


# ---------------------------------------------------------------------------
# Step 4: fetch all current Allegro offers, keyed by external.id
# ---------------------------------------------------------------------------

def fetch_all_offers(access_token):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    offers_by_ext_id = {}
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
        for o in batch:
            ext = o.get("external") or {}
            ext_id = ext.get("id")
            if not ext_id:
                continue
            price = o.get("sellingMode", {}).get("price", {})
            stock = o.get("stock", {})
            offers_by_ext_id[ext_id] = {
                "offerId": o["id"],
                "price": float(price.get("amount")) if price.get("amount") is not None else None,
                "stock": stock.get("available"),
            }
        offset += limit
        if offset >= data.get("totalCount", 0):
            break
    return offers_by_ext_id


# ---------------------------------------------------------------------------
# Step 5-6: diff + push bulk modifications
# ---------------------------------------------------------------------------

def chunked(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


def push_modifications(access_token, modifications):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.beta.v1+json",
        "Content-Type": "application/vnd.allegro.beta.v1+json",
    }
    total_ok, total_fail = 0, 0
    for batch in chunked(modifications, 25):
        command_id = str(uuid.uuid4())
        payload = {"commandId": command_id, "modifications": batch}
        resp = requests.post(
            f"{API_BASE}/sale/offer-bulk-modification-commands",
            headers=headers, json=payload, timeout=30,
        )
        if resp.status_code not in (200, 201):
            print(f"Batch failed: {resp.status_code} {resp.text}", file=sys.stderr)
            total_fail += len(batch)
            continue
        # poll for completion briefly
        for _ in range(6):
            time.sleep(2)
            status_resp = requests.get(
                f"{API_BASE}/sale/offer-bulk-modification-commands/{command_id}",
                headers={"Authorization": f"Bearer {access_token}",
                         "Accept": "application/vnd.allegro.beta.v1+json"},
                timeout=30,
            )
            if status_resp.status_code == 200:
                sc = status_resp.json()
                if sc.get("completedAt"):
                    total_ok += sc["taskCount"]["success"]
                    total_fail += sc["taskCount"]["failed"]
                    break
        time.sleep(0.3)  # be gentle on rate limits
    return total_ok, total_fail


def main():
    access_token, new_refresh_token = refresh_access_token()
    if new_refresh_token != REFRESH_TOKEN:
        update_github_secret("ALLEGRO_REFRESH_TOKEN", new_refresh_token)
        print("Rotated ALLEGRO_REFRESH_TOKEN secret.")

    target = build_target_feed()
    print(f"Target feed rows (valid GTIN): {len(target)}")

    current = fetch_all_offers(access_token)
    print(f"Current Allegro offers fetched: {len(current)}")

    modifications = []
    matched, price_changes, stock_changes, unmatched = 0, 0, 0, 0
    for ext_id, row in target.iterrows():
        live = current.get(ext_id)
        if live is None:
            unmatched += 1
            continue
        matched += 1
        offer_id = live["offerId"]
        if live["price"] is not None and abs(live["price"] - row["PRICE"]) >= 0.01:
            modifications.append({
                "offerId": offer_id,
                "prices": {MARKETPLACE: {
                    "changeType": "FIXED",
                    "value": {"amount": str(row["PRICE"]), "currency": TARGET_CURRENCY},
                }},
            })
            price_changes += 1
        if live["stock"] is not None and int(live["stock"]) != int(row["STOCK"]):
            modifications.append({
                "offerId": offer_id,
                "stock": {"changeType": "FIXED", "value": int(row["STOCK"])},
            })
            stock_changes += 1

    print(f"Matched: {matched}, unmatched (not yet listed on Allegro): {unmatched}")
    print(f"Price changes queued: {price_changes}, stock changes queued: {stock_changes}")

    if not modifications:
        print("Nothing to update this run.")
        return

    ok, fail = push_modifications(access_token, modifications)
    print(f"Done. Successful field updates: {ok}, failed: {fail}")


if __name__ == "__main__":
    main()
