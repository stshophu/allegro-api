"""
resolve_categories.py — run manually via GitHub Actions.

Fixes the root cause found in the "kategoria: brak" (no category) failures:
Allegro's extended-CSV CATEGORY column expects a REAL category path from
Allegro's own taxonomy, Polish, in "Parent/Child" format (confirmed from
Allegro's own official template — e.g. "Odzież Męska", "Elektronika/Komputery")
— NOT free-text English keywords joined with commas, which is what
reshape_feed_for_import_and_list.py / shopify_export_to_allegro.py were
generating. That mismatch is why 100% of the last batch got stuck without
a category despite the CATEGORY column being non-empty.

This script reads an already-built import CSV (any of this repo's
converters' output), looks up each product's real Allegro category via
GET /sale/matching-categories (same endpoint allegro_create_offers.py uses),
walks up to the parent category name via GET /sale/categories/{id}, and
writes CATEGORY as "Parent/Child" (or just "Child" for a top-level
category) — replacing whatever CATEGORY value was there before.

Caches category id->name lookups since many products share categories.

Required env vars: ALLEGRO_CLIENT_ID, ALLEGRO_CLIENT_SECRET,
ALLEGRO_REFRESH_TOKEN, GH_PAT, GH_REPO
"""

import os
import sys
import base64
import requests
import pandas as pd
from nacl import encoding, public

CLIENT_ID = os.environ["ALLEGRO_CLIENT_ID"]
CLIENT_SECRET = os.environ["ALLEGRO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["ALLEGRO_REFRESH_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
GH_REPO = os.environ["GH_REPO"]

INPUT_PATH = os.environ.get("INPUT_CSV", "import_and_list_feed.csv")
OUTPUT_PATH = os.environ.get("OUTPUT_CSV", "import_and_list_feed_with_categories.csv")

TOKEN_URL = "https://allegro.pl/auth/oauth/token"
API_BASE = "https://api.allegro.pl"


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


_category_name_cache = {}


def get_category_name(access_token, category_id):
    if category_id in _category_name_cache:
        return _category_name_cache[category_id]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    resp = requests.get(f"{API_BASE}/sale/categories/{category_id}", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _category_name_cache[category_id] = data
    return data


def resolve_category_path(access_token, product_name):
    """Returns 'Parent/Child' Polish category path, or '' if no match found."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.allegro.public.v1+json",
    }
    resp = requests.get(
        f"{API_BASE}/sale/matching-categories",
        headers=headers,
        params={"name": product_name[:100]},
        timeout=30,
    )
    if resp.status_code != 200:
        return ""
    matches = resp.json().get("matchingCategories", [])
    if not matches:
        return ""

    leaf_id = matches[0]["id"]
    leaf = get_category_name(access_token, leaf_id)
    leaf_name = leaf.get("name", "")
    parent = leaf.get("parent")

    if parent and parent.get("id"):
        parent_data = get_category_name(access_token, parent["id"])
        parent_name = parent_data.get("name", "")
        if parent_name:
            return f"{parent_name}/{leaf_name}"

    return leaf_name


def main():
    access_token, new_refresh_token = refresh_access_token()
    if new_refresh_token != REFRESH_TOKEN:
        update_github_secret("ALLEGRO_REFRESH_TOKEN", new_refresh_token)
        print("Rotated ALLEGRO_REFRESH_TOKEN secret.")

    df = pd.read_csv(INPUT_PATH)
    print(f"Rows to resolve: {len(df)}")

    resolved = []
    unresolved_count = 0
    for i, r in df.iterrows():
        path = resolve_category_path(access_token, str(r["NAME"]))
        if not path:
            unresolved_count += 1
        resolved.append(path)
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(df)} resolved")

    df["CATEGORY"] = resolved
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8")
    print(f"Wrote {len(df)} rows to {OUTPUT_PATH}")
    print(f"Unresolved (no category match found): {unresolved_count} / {len(df)}")


if __name__ == "__main__":
    main()
