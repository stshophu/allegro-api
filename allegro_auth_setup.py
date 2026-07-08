"""
ONE-TIME SETUP SCRIPT — run this locally on your Mac, once.

Purpose: obtain the initial Allegro OAuth `refresh_token` for your seller
account, so the automated 30-min sync script can keep getting fresh
access tokens without you logging in again.

Before running:
1. Go to https://apps.developer.allegro.pl/ and register a new application:
   - type: "Aplikacja będzie posiadać dostęp do przeglądarki..."
     (Authorization Code flow)
   - redirect URI: http://localhost:8765/callback
   - scopes needed: allegro:api:sale:offers:read, allegro:api:sale:offers:write
     (you can leave all scopes checked if unsure)
2. Copy the Client ID and Client Secret it gives you, paste below.

Run:
    pip install requests --break-system-packages
    python3 allegro_auth_setup.py

It will print a URL — open it in your browser, log in to Allegro, approve
the app, and you'll be redirected to localhost:8765/callback?code=XXXX.
Copy the `code` value from that URL and paste it back into this script
when prompted.

At the end it prints CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN — store all
three as GitHub repo secrets:
    ALLEGRO_CLIENT_ID
    ALLEGRO_CLIENT_SECRET
    ALLEGRO_REFRESH_TOKEN
"""

import requests
import urllib.parse

CLIENT_ID = ""      # paste your Client ID here
CLIENT_SECRET = ""  # paste your Client Secret here
REDIRECT_URI = "http://localhost:8765/callback"

AUTH_URL = "https://allegro.pl/auth/oauth/authorize"
TOKEN_URL = "https://allegro.pl/auth/oauth/token"


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print("Fill in CLIENT_ID and CLIENT_SECRET at the top of this file first.")
        return

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("\n1. Open this URL in your browser and log in / approve the app:\n")
    print(auth_url)
    print("\n2. After approving, your browser will try to redirect to")
    print(f"   {REDIRECT_URI}?code=XXXX (it will show 'can't connect' — that's fine).")
    print("   Copy everything after 'code=' from the address bar.\n")

    code = input("Paste the code here: ").strip()

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    resp = requests.post(TOKEN_URL, data=data, auth=(CLIENT_ID, CLIENT_SECRET))
    if resp.status_code != 200:
        print(f"\nSomething went wrong: {resp.status_code}\n{resp.text}")
        return

    tokens = resp.json()
    print("\n--- SUCCESS ---")
    print(f"access_token (valid 12h, not needed long-term): {tokens['access_token'][:20]}...")
    print(f"refresh_token (store this one!): {tokens['refresh_token']}")
    print("\nNow go to your GitHub repo > Settings > Secrets and variables > Actions")
    print("and add these three repository secrets:")
    print(f"  ALLEGRO_CLIENT_ID      = {CLIENT_ID}")
    print(f"  ALLEGRO_CLIENT_SECRET  = {CLIENT_SECRET}")
    print(f"  ALLEGRO_REFRESH_TOKEN  = {tokens['refresh_token']}")
    print("\nAlso add a GH_PAT secret: a GitHub Personal Access Token (classic,")
    print("'repo' scope) so the sync workflow can rotate ALLEGRO_REFRESH_TOKEN")
    print("automatically each run (Allegro issues a new refresh_token every time it's used).")


if __name__ == "__main__":
    main()
