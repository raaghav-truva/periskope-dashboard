#!/usr/bin/env python3
"""
Pulls all contacts from Periskope, computes the funnel/label metrics used by
the dashboard, and writes the result to data.json.

Run daily by the GitHub Actions workflow (update-dashboard.yml).

CONFIRMED against Periskope's official API docs (docs.periskope.app):
- Base URL: https://api.periskope.app/v1
- Auth: `Authorization: Bearer <api key>` header (as originally guessed).
- Endpoint: GET /contacts with `limit` / `offset` query params, returning
  {"contacts": [...], "count": N, "from": X, "to": Y}. Each contact has a
  `labels` array of plain strings and an `updated_at` timestamp — matches
  the `has()` helper below, no changes needed there.
- Max `limit` per page is 2000 (bumped up from 500 to cut down on requests).

THE ACTUAL BUG: Periskope requires a SECOND header on every request —
`x-phone`, the org's connected WhatsApp number (digits only, e.g.
"919876543210"). Requests without it fail. This script auto-discovers it by
calling GET /phones/all (which does not require x-phone) and picking the
first CONNECTED phone. You can override this by setting a PERISKOPE_PHONE
env var / GitHub secret if auto-discovery ever picks the wrong number (e.g.
if the org has multiple connected WhatsApp lines).

SETUP REQUIRED BEFORE THIS WORKS:
1. Get a Periskope API key: console.periskope.app -> Settings -> Integrations
   -> API. Set it as the PERISKOPE_API_KEY GitHub Actions secret.
2. (Optional) If the org has more than one connected WhatsApp number and
   auto-discovery picks the wrong one, set PERISKOPE_PHONE as an additional
   secret/variable with the correct number.
"""

import os
import json
import sys
from collections import Counter
import urllib.request
import urllib.error

API_KEY = os.environ.get("PERISKOPE_API_KEY")
BASE_URL = os.environ.get("PERISKOPE_BASE_URL", "https://api.periskope.app/v1")
PHONE_OVERRIDE = os.environ.get("PERISKOPE_PHONE")

if not API_KEY:
    sys.exit("ERROR: PERISKOPE_API_KEY environment variable is not set.")

TARGET_LABELS = {
    "Home Loans": "home loans",
    "Active Warm": "active warm (1 month)",
    "Still Looking": "still looking (2-4 months)",
    "Future Plan": "future plan (4+ months)",
    "Balance Transfer (Immediate)": "balance transfer (immediate)",
    "Balance Transfer (Future)": "balance transfer (future)",
    "Registration Complete": "registration complete",
    "M0": "m0",
    "M1": "m1",
    "M2": "m2",
}


def api_get(path, params=None, extra_headers=None):
    """Minimal GET helper against the Periskope API."""
    url = f"{BASE_URL}{path}"
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"ERROR: {e.code} {e.reason} calling {url}\n{body}")


def get_org_phone():
    """Discover the org's connected WhatsApp number for the x-phone header."""
    if PHONE_OVERRIDE:
        return PHONE_OVERRIDE
    phones = api_get("/phones/all")
    if not phones:
        sys.exit("ERROR: No phones found on this Periskope org (GET /phones/all "
                  "returned empty). Set PERISKOPE_PHONE explicitly instead.")
    connected = [p for p in phones if p.get("wa_state") == "CONNECTED"] or phones
    org_phone = connected[0].get("org_phone", "")
    return org_phone.split("@")[0]


def fetch_all_contacts(phone):
    """Paginate through every contact in the org."""
    all_contacts = []
    offset = 0
    limit = 2000
    headers = {"x-phone": phone}
    while True:
        data = api_get("/contacts", {"limit": limit, "offset": offset}, headers)
        batch = data.get("contacts", [])
        all_contacts.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_contacts


def has(contact, label_name):
    labels = [l.lower() for l in (contact.get("labels") or [])]
    return label_name.lower() in labels


def build_dataset(contacts):
    label_counts = {
        name: sum(1 for c in contacts if has(c, key))
        for name, key in TARGET_LABELS.items()
    }

    home_loans_no_m = sum(
        1 for c in contacts
        if has(c, "home loans") and not has(c, "m0")
        and not has(c, "m1") and not has(c, "m2")
    )
    m0_only = sum(
        1 for c in contacts
        if has(c, "m0") and not has(c, "m1") and not has(c, "m2")
    )
    m1_no_m2 = sum(
        1 for c in contacts if has(c, "m1") and not has(c, "m2")
    )

    daily = {"m0": Counter(), "m1": Counter(), "m2": Counter()}
    for c in contacts:
        d = (c.get("updated_at") or "")[:10]
        if not d:
            continue
        for lab in ("m0", "m1", "m2"):
            if has(c, lab):
                daily[lab][d] += 1

    return {
        "total_org_contacts": len(contacts),
        "label_counts": label_counts,
        "home_loans_no_m_tags": home_loans_no_m,
        "m0_only": m0_only,
        "m1_no_m2": m1_no_m2,
        "daily": {k: dict(sorted(v.items())) for k, v in daily.items()},
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }


def main():
    phone = get_org_phone()
    print(f"Using x-phone: {phone}")
    contacts = fetch_all_contacts(phone)
    dataset = build_dataset(contacts)
    with open("data.json", "w") as f:
        json.dump(dataset, f, indent=2)
    print(f"Wrote data.json — {dataset['total_org_contacts']} contacts scanned.")


if __name__ == "__main__":
    main()
