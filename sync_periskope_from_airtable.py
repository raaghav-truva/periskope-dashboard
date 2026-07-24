#!/usr/bin/env python3
"""
Makes Periskope match Airtable's "Periskop View", in two phases:

  Phase 1 — CREATE: for every lead in Periskop View with no matching
  Periskope contact (by phone), create one via POST /contacts/create,
  seeded with the correct status label straight away.

  Phase 2 — RELABEL: for every lead now matched to a Periskope contact,
  make sure its "status" label (the one AIRTABLE_STATUS_TO_PERISKOPE_LABEL
  maps to) matches Airtable's current Status field exactly, REMOVING any
  stale/older status label in the process (per explicit instruction — a
  lead that moved from "Still Looking" to "Active Warm" should not keep
  wearing the old label). All other labels a contact already has (e.g.
  "home loans", "m0"/"m1"/"m2") are preserved untouched — Periskope's
  PATCH /contacts/labels REPLACES a contact's entire label list, so this
  script always fetches each contact's current labels first and rebuilds
  the full set rather than sending only the new label.

This is a WRITE script against your live Periskope org and is separate
from fetch_periskope_data.py (which is read-only and runs nightly via CI).
It defaults to a DRY RUN — it only prints what it would do. Nothing is
written until you pass --apply.

Usage:
    python sync_periskope_from_airtable.py            # dry run, writes nothing
    python sync_periskope_from_airtable.py --apply     # actually creates/relabels

Requires the same PERISKOPE_API_KEY / AIRTABLE_TOKEN env vars as
fetch_periskope_data.py (this script imports its helpers directly, so run
it from the same directory).

CONTACT ID FORMAT: confirmed against a live Periskope call that
contact_id is ALWAYS in the form "<country_code><number>@c.us" (e.g.
"919876543210@c.us") — both when reading existing contacts and when
creating new ones. Airtable's "Phone Number" field is not reliably in that
full format (often just a 10-digit local number), so this script assumes
an India (+91) country code for any phone that normalizes to exactly 10
digits. If any Periskop View leads are NOT Indian numbers, check the dry
run output carefully before applying — a wrong prefix would create a
contact against the wrong country's number entirely.
"""
import sys
import re
import time
import json
import urllib.request
import urllib.error

from fetch_periskope_data import (
    api_get, get_org_phone, fetch_all_contacts, normalize_phone, has,
    fetch_airtable_leads, AIRTABLE_STATUS_TO_PERISKOPE_LABEL,
    AIRTABLE_LEADS_VIEW_ID, BASE_URL, API_KEY,
)

STATUS_LABEL_VALUES = {v.lower() for v in AIRTABLE_STATUS_TO_PERISKOPE_LABEL.values()}
DEFAULT_COUNTRY_CODE = "91"
RATE_LIMIT_DELAY = 0.4  # seconds between writes, gentle on Periskope's rate limit


def full_contact_id(phone_raw, default_cc=DEFAULT_COUNTRY_CODE):
    """Reconstruct a full '<country_code><number>@c.us' contact_id from a
    raw Airtable phone string. Assumes India (+91) for any number that's
    exactly 10 digits once non-digits are stripped; passes through
    anything longer as-is (assumed to already carry a country code)."""
    digits = re.sub(r"\D", "", phone_raw or "")
    if len(digits) == 10:
        digits = default_cc + digits
    return f"{digits}@c.us" if digits else None


def periskope_write(path, method, phone, body):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "x-phone": phone,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"{e.code} {e.reason} calling {method} {url}: {body_text}")


def plan(contacts, leads):
    """Pure decision logic (no I/O) — figures out what to create/relabel.
    Kept separate from execution so it can be unit-tested with synthetic
    data."""
    by_phone10 = {}
    for c in contacts:
        p10 = normalize_phone((c.get("contact_id") or "").split("@")[0])
        if p10:
            by_phone10[p10] = c

    to_create, to_relabel = [], []
    no_change = no_phone = unmapped_status = 0

    for lead in leads:
        phone10 = normalize_phone(lead.get("phone_raw") or "")
        if not phone10:
            no_phone += 1
            continue
        expected_label = AIRTABLE_STATUS_TO_PERISKOPE_LABEL.get(lead.get("status") or "")
        contact = by_phone10.get(phone10)

        if contact is None:
            cid = full_contact_id(lead.get("phone_raw"))
            if not cid:
                no_phone += 1
                continue
            labels = ["home loans"]
            if expected_label:
                labels.append(expected_label)
            to_create.append({
                "contact_id": cid, "name": lead["name"], "status": lead.get("status"),
                "labels": labels,
            })
            continue

        if not expected_label:
            unmapped_status += 1
            continue

        current_labels = contact.get("labels") or []
        managed = [l for l in current_labels if l.lower() in STATUS_LABEL_VALUES]
        if len(managed) == 1 and managed[0].lower() == expected_label.lower():
            no_change += 1
            continue

        new_labels = [l for l in current_labels if l.lower() not in STATUS_LABEL_VALUES] + [expected_label]
        to_relabel.append({
            "contact_id": contact["contact_id"], "name": lead["name"],
            "status": lead.get("status"),
            "old_labels": current_labels, "new_labels": new_labels,
        })

    return {
        "to_create": to_create, "to_relabel": to_relabel,
        "no_change": no_change, "no_phone": no_phone, "unmapped_status": unmapped_status,
    }


def print_plan(p):
    print("\n--- Plan ---")
    print(f"{len(p['to_create'])} contact(s) to CREATE in Periskope")
    print(f"{len(p['to_relabel'])} contact(s) to RELABEL")
    print(f"{p['no_change']} already correct")
    print(f"{p['no_phone']} lead(s) with no usable phone number (skipped)")
    print(f"{p['unmapped_status']} lead(s) exist in Periskope but have a Status with no "
          f"Periskope-label mapping, e.g. Blocking Paid (left untouched)")

    if p["to_create"]:
        print("\nTo create:")
        for c in p["to_create"]:
            print(f"  + {c['name']} ({c['contact_id']}) — status: {c['status']!r} -> labels: {c['labels']}")
    if p["to_relabel"]:
        print("\nTo relabel:")
        for c in p["to_relabel"]:
            print(f"  ~ {c['name']} ({c['contact_id']}): {c['old_labels']} -> {c['new_labels']}")


def apply_plan(p, phone):
    created, relabeled, errors = 0, 0, []

    for c in p["to_create"]:
        try:
            body = {"contact_name": c["name"], "contact_id": c["contact_id"], "labels": ", ".join(c["labels"])}
            periskope_write("/contacts/create", "POST", phone, body)
            created += 1
        except Exception as e:
            errors.append(f"create {c['contact_id']}: {e}")
        time.sleep(RATE_LIMIT_DELAY)

    for c in p["to_relabel"]:
        try:
            body = {"contact_ids": [c["contact_id"]], "labels": ", ".join(c["new_labels"])}
            periskope_write("/contacts/labels", "PATCH", phone, body)
            relabeled += 1
        except Exception as e:
            errors.append(f"relabel {c['contact_id']}: {e}")
        time.sleep(RATE_LIMIT_DELAY)

    print(f"\nDone: {created} created, {relabeled} relabeled, {len(errors)} error(s).")
    for e in errors:
        print(f"  ERROR: {e}")


def main():
    apply_changes = "--apply" in sys.argv

    org_phone = get_org_phone()
    print(f"Using x-phone: {org_phone}")

    print("Fetching current Periskope contacts...")
    contacts, _ = fetch_all_contacts(org_phone)
    print(f"{len(contacts)} contact(s) in Periskope.")

    print("Fetching Airtable Periskop View...")
    leads = fetch_airtable_leads(view_id=AIRTABLE_LEADS_VIEW_ID)
    print(f"{len(leads)} lead(s) in Periskop View.")

    p = plan(contacts, leads)
    print_plan(p)

    if not apply_changes:
        print("\nDRY RUN ONLY — no changes made. Re-run with --apply to write these changes.")
        return

    print("\nAPPLYING CHANGES...")
    apply_plan(p, org_phone)


if __name__ == "__main__":
    main()
