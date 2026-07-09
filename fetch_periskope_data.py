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
labels). So instead of relying on that timestamp, this script keeps its own
persisted registry in `history.json`: the first time it ever sees a given
contact carrying m0/m1/m2, it records that contact (name + phone) against
today's date, permanently. Once recorded, a contact's "day marked" never
moves again, no matter what else changes on their record later — this is
what makes the daily numbers (and the actual contact list per day) durable
across runs. `history.json` must be committed alongside data.json (the
workflow does `git add data.json history.json`) — without it, everything
looks "newly marked today" on every run.

On the very first run ever (empty history), each contact currently holding
m0/m1/m2 is backfilled using their own `updated_at` date as a best-effort
estimate of when they were marked — there's no way to know the true date
before tracking started. Every day after that, the date recorded is exact.

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
    """Paginate through every contact in the org. Also returns the API's own
    reported `count` from the first page, so main() can flag it if what we
    actually collected doesn't match — a sign of pagination drift on a
    dataset that's still growing while we page through it."""
    all_contacts = []
    offset = 0
    limit = 2000
    headers = {"x-phone": phone}
    api_reported_count = None
    while True:
        data = api_get("/contacts", {"limit": limit, "offset": offset}, headers)
        if api_reported_count is None:
            api_reported_count = data.get("count")
        batch = data.get("contacts", [])
        all_contacts.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_contacts, api_reported_count


def label_diagnostics(contacts, search_name=None):
    """Frequency count of every RAW label string actually seen (case/whitespace
    preserved) — lets us spot near-miss variants of a target label (extra
    space, singular/plural, different casing preserved for display) that
    would otherwise silently fail the exact match in has(). Also, if
    search_name is given, dumps the raw labels for any contact whose name
    contains it, so a specific "why isn't X showing up" report can be
    checked directly against what we actually received from the API."""
    freq = Counter()
    for c in contacts:
        for lab in (c.get("labels") or []):
            freq[lab] += 1
    top_labels = freq.most_common(60)

    matches = []
    if search_name:
        needle = search_name.lower()
        for c in contacts:
            if needle in (c.get("contact_name") or "").lower():
                matches.append({
                    "name": c.get("contact_name"),
                    "phone": (c.get("contact_id") or "").split("@")[0],
                    "labels": c.get("labels") or [],
                })
    return top_labels, matches


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

    return {
        "total_org_contacts": len(contacts),
        "label_counts": label_counts,
        "contacts_by_label": build_contacts_by_label(contacts),
        "home_loans_no_m_tags": home_loans_no_m,
        "m0_only": m0_only,
        "m1_no_m2": m1_no_m2,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }


def load_history():
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    else:
        history = {}
    history.setdefault("backfilled", False)
    history.setdefault("marked", {"m0": {}, "m1": {}, "m2": {}})
    return history


def update_marked_registry(history, contacts, today):
    """Record, per label, the first date each contact was ever seen carrying
    it. Once a contact_id is in the registry for a label, it's never moved —
    that's what makes the daily breakdown (and per-day contact list) durable.
    On the very first run (history not yet backfilled), use each contact's
    own updated_at date as a best-effort estimate; every run after that uses
    today, since a continuously-running daily job would have caught it
    already if it existed before."""
    marked = history["marked"]
    first_run = not history["backfilled"]
    for c in contacts:
        contact_id = c.get("contact_id") or ""
        if not contact_id:
            continue
        phone = contact_id.split("@")[0]
        name = c.get("contact_name") or phone or "Unknown"
        updated_date = (c.get("updated_at") or "")[:10]
        for lab in ("m0", "m1", "m2"):
            if has(c, lab) and contact_id not in marked[lab]:
                date = updated_date if (first_run and updated_date) else today
                marked[lab][contact_id] = {"date": date, "name": name, "phone": phone}
    history["backfilled"] = True
    return history


def compute_daily_from_registry(marked):
    """Turn the {label: {contact_id: {date, name, phone}}} registry into the
    two shapes the dashboard needs: per-day counts (for the chart) and
    per-day contact lists (for click-to-drill-down)."""
    daily_counts = {"m0": {}, "m1": {}, "m2": {}, "total": {}}
    daily_contacts = {"m0": {}, "m1": {}, "m2": {}}
    for lab in ("m0", "m1", "m2"):
        for info in marked[lab].values():
            d = info["date"]
            daily_counts[lab][d] = daily_counts[lab].get(d, 0) + 1
            daily_contacts[lab].setdefault(d, []).append(
                {"name": info["name"], "phone": info["phone"]}
            )
        daily_counts[lab] = dict(sorted(daily_counts[lab].items()))
        daily_contacts[lab] = {
            d: sorted(lst, key=lambda c: c["name"].lower())
            for d, lst in sorted(daily_contacts[lab].items())
        }
    all_dates = sorted(set(daily_counts["m0"]) | set(daily_counts["m1"]) | set(daily_counts["m2"]))
    daily_counts["total"] = {
        d: daily_counts["m0"].get(d, 0) + daily_counts["m1"].get(d, 0) + daily_counts["m2"].get(d, 0)
        for d in all_dates
    }
    return daily_counts, daily_contacts


def main():
    phone = get_org_phone()
    print(f"Using x-phone: {phone}")
    contacts, api_reported_count = fetch_all_contacts(phone)

    if api_reported_count is not None and api_reported_count != len(contacts):
        print(f"WARNING: Periskope reported count={api_reported_count} but we "
              f"collected {len(contacts)} contacts across pagination — possible "
              f"drift while paging through a live/growing dataset.")

    dataset = build_dataset(contacts)
    dataset["api_reported_count"] = api_reported_count

    # Defaults to the contact reported missing from the dashboard so this
    # run's log/data.json settles the question directly; override with the
    # DEBUG_CONTACT_NAME env var to check someone else later.
    debug_name = os.environ.get("DEBUG_CONTACT_NAME", "Abhijeet Tandel")
    top_labels, name_matches = label_diagnostics(contacts, search_name=debug_name)
    dataset["diagnostics"] = {"top_raw_labels": top_labels, "name_search_matches": name_matches}

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    history = load_history()
    history = update_marked_registry(history, contacts, today)
    dataset["daily"], dataset["daily_contacts"] = compute_daily_from_registry(history["marked"])

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)
    with open("data.json", "w") as f:
        json.dump(dataset, f, indent=2)
    total_tracked = sum(len(v) for v in history["marked"].values())
    print(f"Wrote data.json — {dataset['total_org_contacts']} contacts scanned "
          f"(Periskope reports {api_reported_count}).")
    print(f"History registry now tracks {total_tracked} label-marks across "
          f"{len(dataset['daily']['total'])} day(s), through {today}.")
    print(f"Top raw labels seen: {top_labels[:15]}")
    if name_matches:
        print(f"DEBUG_CONTACT_NAME matches: {json.dumps(name_matches, indent=2)}")


if __name__ == "__main__":
    main()
