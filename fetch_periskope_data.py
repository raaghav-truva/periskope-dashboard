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

DAILY HISTORY: Periskope's API doesn't expose a label-change log — a
contact only has a single `updated_at` timestamp for the whole record, which
drifts forward whenever anything about the contact changes (not just its
labels). Using it directly as "the day this label was applied" (which the
`daily` proxy below still does, for one-time backfill) silently loses
historical accuracy over time. To get durable day-by-day numbers, this
script keeps a persisted `history.json` snapshot of cumulative M0/M1/M2
totals on every run and derives "marked today" as the delta between today's
totals and the last recorded snapshot. That file must be committed
alongside data.json (the workflow does `git add data.json history.json`) —
without it, history resets to zero on every run.

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
import datetime
from collections import Counter
import urllib.request
import urllib.error

HISTORY_PATH = "history.json"

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


def build_contacts_by_label(contacts):
    """For each target label, list the contacts carrying it (name + phone),
    so the dashboard can show who's in each bucket on click."""
    result = {name: [] for name in TARGET_LABELS}
    for c in contacts:
        phone = (c.get("contact_id") or "").split("@")[0]
        display_name = c.get("contact_name") or phone or "Unknown"
        for name, key in TARGET_LABELS.items():
            if has(c, key):
                result[name].append({"name": display_name, "phone": phone})
    for name in result:
        result[name].sort(key=lambda c: c["name"].lower())
    return result


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

    daily_proxy = {"m0": Counter(), "m1": Counter(), "m2": Counter()}
    for c in contacts:
        d = (c.get("updated_at") or "")[:10]
        if not d:
            continue
        for lab in ("m0", "m1", "m2"):
            if has(c, lab):
                daily_proxy[lab][d] += 1

    return {
        "total_org_contacts": len(contacts),
        "label_counts": label_counts,
        "contacts_by_label": build_contacts_by_label(contacts),
        "home_loans_no_m_tags": home_loans_no_m,
        "m0_only": m0_only,
        "m1_no_m2": m1_no_m2,
        "daily_proxy": {k: dict(sorted(v.items())) for k, v in daily_proxy.items()},
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            return json.load(f)
    return {"snapshots": {}}


def backfill_history_from_proxy(history, daily_proxy):
    """First-ever run: seed history from the updated_at-based proxy so past
    days aren't just zero. Only runs while history is empty — once real
    snapshots start accumulating, this is never touched again."""
    if history["snapshots"]:
        return history
    all_dates = sorted(set(daily_proxy["m0"]) | set(daily_proxy["m1"]) | set(daily_proxy["m2"]))
    running = {"m0": 0, "m1": 0, "m2": 0}
    for d in all_dates:
        for lab in ("m0", "m1", "m2"):
            running[lab] += daily_proxy[lab].get(d, 0)
        history["snapshots"][d] = dict(running)
    return history


def record_today_snapshot(history, today, label_counts):
    history["snapshots"][today] = {
        "m0": label_counts["M0"],
        "m1": label_counts["M1"],
        "m2": label_counts["M2"],
    }
    return history


def compute_daily_marked(history):
    """Derive 'marked today' series as the day-over-day delta of cumulative
    totals — robust even if individual contacts' updated_at drifts for
    unrelated reasons, since it only diffs aggregate counts per calendar day."""
    dates = sorted(history["snapshots"].keys())
    daily = {"m0": {}, "m1": {}, "m2": {}, "total": {}}
    prev = {"m0": 0, "m1": 0, "m2": 0}
    for d in dates:
        snap = history["snapshots"][d]
        total = 0
        for lab in ("m0", "m1", "m2"):
            marked = max(0, snap.get(lab, 0) - prev[lab])
            daily[lab][d] = marked
            total += marked
            prev[lab] = snap.get(lab, 0)
        daily["total"][d] = total
    return daily


def main():
    phone = get_org_phone()
    print(f"Using x-phone: {phone}")
    contacts = fetch_all_contacts(phone)
    dataset = build_dataset(contacts)

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    history = load_history()
    history = backfill_history_from_proxy(history, dataset["daily_proxy"])
    history = record_today_snapshot(history, today, dataset["label_counts"])
    dataset["daily"] = compute_daily_marked(history)

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
    with open("data.json", "w") as f:
        json.dump(dataset, f, indent=2)
    print(f"Wrote data.json — {dataset['total_org_contacts']} contacts scanned.")
    print(f"History now covers {len(history['snapshots'])} day(s), through {today}.")


if __name__ == "__main__":
    main()
