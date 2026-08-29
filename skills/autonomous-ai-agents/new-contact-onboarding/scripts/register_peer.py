#!/usr/bin/env python3
"""
Register a newly-identified contact into Honcho + the honcho-peer-roster
allowlist. Performs Steps 4 + 5 of the new-contact-onboarding skill.

Usage:
  python3 register_peer.py --peer-id 113048287211723-lid \
      --name Bille --relationship "cosplay partner, KL trip companion" \
      --display-name "Bille"

  python3 register_peer.py --peer-id 7233071505 --name Wilnice \
      --relationship "girlfriend" --platform telegram \
      --skip-roster-patch   # don't touch the allowlist (e.g. transient peer)

Honcho endpoint is hard-coded to http://localhost:8000 — Kyros's local
deployment. Adjust HONCHO_URL for other environments.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HONCHO_URL = "http://localhost:8000"
DEFAULT_WORKSPACE = "hermes"
DEFAULT_OBSERVER = "kyroskoh_bot"

# Allowlist files we keep in sync. Edit these paths if you relocate the skill.
SKILL_DIR = Path(__file__).resolve().parent.parent
ROSTER_SKILL_DIR = SKILL_DIR.parent / "honcho-peer-roster"
ALLOWLIST_FILES = [
    ROSTER_SKILL_DIR / "scripts" / "scan_peers.py",
    ROSTER_SKILL_DIR / "SKILL.md",
]


def honcho_request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{HONCHO_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {body}") from None


def write_peer_card(observer: str, target: str, facts: list[str]) -> None:
    """PUT the curated card. Observer-scoped — see honcho-memory-operations."""
    path = f"/v3/workspaces/{DEFAULT_WORKSPACE}/peers/{observer}/card?target={target}"
    # Honcho requires the body field to be `peer_card`, not `card` —
    # using `card` returns 422 missing field. (Bit by this 2026-08-29 binding
    # Kyros's secondary LID 188661404582023-lid.)
    honcho_request("PUT", path, {"peer_card": facts})


def write_conclusion(observer: str, target: str, content: str) -> None:
    """POST a single fact as a deriver-distilled conclusion."""
    path = f"/v3/workspaces/{DEFAULT_WORKSPACE}/conclusions"
    honcho_request(
        "POST",
        path,
        {
            "conclusions": [
                {
                    "content": content,
                    "observer_id": observer,
                    "observed_id": target,
                }
            ]
        },
    )


def detect_id_kind(peer_id: str) -> str:
    """Classify a peer ID for the right KNOWN_ALIASES key."""
    if peer_id.endswith("-lid"):
        return "lids"
    if re.fullmatch(r"\d{10,15}", peer_id):
        return "phones"
    if re.fullmatch(r"\d{8,}", peer_id):
        return "telegram"  # Telegram UIDs are 8-10 digit numbers
    if peer_id.isdigit() and len(peer_id) >= 17:
        return "discord"  # Discord snowflakes are 17-19 digit
    return "other"


def patch_allowlist(name: str, peer_id: str, relationship: str, platform: str | None) -> list[Path]:
    """
    Append the new alias entry to KNOWN_ALIASES in both the roster script
    and the SKILL.md body. Returns the list of files that were modified.

    The patch is idempotent — if the peer_id already exists anywhere in
    KNOWN_ALIASES, no change. If a canonical entry for `name` already
    exists, the new peer_id is MERGED into that entry's list rather than
    added as a duplicate top-level key (which would silently shadow the
    existing entry under Python's last-key-wins dict semantics).

    Inserts as a child of KNOWN_ALIASES, before its closing brace, properly
    indented to match the existing entries (4 spaces for the key, 8 for the
    inner list).
    """
    kind = detect_id_kind(peer_id)
    indent_key = "    "      # matches `"Kyros": {` etc.
    indent_inner = "        "  # matches `"phones":   ["..."],`

    comment = ""
    if platform:
        comment += f" platform={platform}"
    if relationship:
        comment += f", {relationship}"

    new_line = (
        f'{indent_inner}"{kind}":     ["{peer_id}"], # {comment.strip()}\n'
        if comment
        else f'{indent_inner}"{kind}":     ["{peer_id}"],\n'
    )

    modified = []
    for path in ALLOWLIST_FILES:
        if not path.exists():
            print(f"  WARN: {path} not found, skipping", file=sys.stderr)
            continue
        text = path.read_text()

        # Idempotency check — exact peer_id string present anywhere.
        if f'"{peer_id}"' in text:
            print(f"  SKIP: {peer_id} already in {path.name}")
            continue

        # Check whether the canonical human already has an entry. If yes,
        # MERGE the peer_id into the existing list rather than inserting a
        # duplicate top-level key (which Python silently overwrites).
        import re as _re
        canonical_block = _re.search(
            rf'"{_re.escape(name)}":\s*\{{(.*?)\n\s*\}}\s*,',
            text,
            flags=_re.DOTALL,
        )
        if canonical_block:
            block_text = canonical_block.group(0)
            inner = canonical_block.group(1)
            # If the inner block already has a list for `kind`, append.
            kind_match = _re.search(
                rf'"{kind}":\s*\[([^\]]*)\]',
                inner,
            )
            if kind_match:
                existing_list = kind_match.group(1).strip()
                if existing_list and not existing_list.endswith(","):
                    existing_list += ","
                # Preserve the trailing comment on the first item if present.
                first_item_match = _re.match(r'\s*"([^"]+)"(?:\s*,\s*#\s*([^\n]+))?', existing_list)
                first_comment = ""
                if first_item_match and first_item_match.group(2):
                    first_comment = f" # {first_item_match.group(2).strip()}"
                new_inner_list = (
                    f'"{first_item_match.group(1) if first_item_match else ""}"{first_comment},\n'
                    f'{indent_inner}    "{peer_id}", # {comment.strip() or peer_id}\n'
                )
                new_block_text = block_text.replace(
                    kind_match.group(0),
                    f'"{kind}": [{new_inner_list}{indent_inner}]',
                )
                # The "]" we just inserted has a trailing newline inside the
                # bracket; that's fine for Python.
            else:
                # No list for this `kind` yet — inject one at the start of
                # the canonical block's inner body.
                new_block_text = block_text.replace(
                    inner,
                    f'\n{indent_inner}"{kind}":     ["{peer_id}"], # {comment.strip() or peer_id}\n'
                    + inner.lstrip("\n"),
                    1,
                )

            new_text = text.replace(block_text, new_block_text, 1)
            if new_text == text:
                print(f"  WARN: merge failed for {name}/{peer_id} in {path.name}", file=sys.stderr)
                continue
            path.write_text(new_text)
            modified.append(path)
            print(f"  MERGED: {path.name} ({name}.{kind})")
            continue

        # No canonical entry — insert as a new top-level child before the
        # closing brace (legacy behaviour, kept for first-time humans).
        marker = "KNOWN_ALIASES = {"
        idx = text.find(marker)
        if idx == -1:
            print(f"  WARN: KNOWN_ALIASES not found in {path.name}, skipping", file=sys.stderr)
            continue
        depth = 0
        end_idx = None
        for i in range(idx + len(marker) - 1, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx is None:
            print(f"  WARN: could not find closing brace of KNOWN_ALIASES in {path.name}", file=sys.stderr)
            continue
        new_block = (
            f'{indent_key}"{name}": {{\n'
            f'{new_line}'
            f'{indent_key}}},\n'
        )
        new_text = text[:end_idx] + new_block + text[end_idx:]
        path.write_text(new_text)
        modified.append(path)
        print(f"  INSERTED: {path.name} (new {name})")
    return modified


def main():
    ap = argparse.ArgumentParser(description="Register a new contact into Honcho + roster allowlist")
    ap.add_argument("--peer-id", required=True, help="Honcho peer ID (e.g. 113048287211723-lid)")
    ap.add_argument("--name", required=True, help='Canonical display name (e.g. "Bille")')
    ap.add_argument("--relationship", required=True, help='Relationship to Kyros (e.g. "cosplay partner")')
    ap.add_argument("--display-name", help="Platform display name if different from --name")
    ap.add_argument("--platform", help="Platform (whatsapp, telegram, discord, ...)")
    ap.add_argument("--observer", default=DEFAULT_OBSERVER, help="AI peer doing the writing")
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--language", default="English", help="Observed language for the card")
    ap.add_argument("--skip-card", action="store_true")
    ap.add_argument("--skip-conclusion", action="store_true")
    ap.add_argument("--skip-roster-patch", action="store_true")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    display = args.display_name or args.name

    facts = [
        f'{args.name} is Kyros\u2019s {args.relationship}.',
        f'Preferred address: {args.name}.',
        f'Platform display name: {display}.',
        f'Language: {args.language}.',
        f'First chatted: {today}.',
    ]
    if args.platform:
        facts.append(f'Platform: {args.platform}.')

    conclusion = (
        f'{args.name} introduced themselves on {today}: {args.relationship}. '
        f'Preferred address: {args.name}. Platform display name: {display}.'
    )

    print(f"Registering peer: {args.peer_id}")
    print(f"  Name:          {args.name}")
    print(f"  Relationship:  {args.relationship}")
    print(f"  Display name:  {display}")
    print(f"  Platform:      {args.platform or '(unspecified)'}")
    print(f"  Observer:      {args.observer}")
    print()

    if not args.skip_card:
        print(f"Writing peer card → {args.observer} → {args.peer_id}")
        write_peer_card(args.observer, args.peer_id, facts)
        # read-back
        card = honcho_request(
            "GET",
            f"/v3/workspaces/{args.workspace}/peers/{args.observer}/card?target={args.peer_id}",
        )
        # GET response uses field name `peer_card`, not `card` (same field-name
        # asymmetry that bit us on PUT).
        facts_now = card.get("peer_card") or card.get("card") or []
        print(f"  card facts now: {len(facts_now)}")

    if not args.skip_conclusion:
        print(f"Writing conclusion → {args.observer} observed {args.peer_id}")
        write_conclusion(args.observer, args.peer_id, conclusion)

    if not args.skip_roster_patch:
        print("Patching KNOWN_ALIASES in honcho-peer-roster files:")
        modified = patch_allowlist(args.name, args.peer_id, args.relationship, args.platform)
        if not modified:
            print("  (no files modified)")

    print()
    print(f"Done. Next inbound from {args.peer_id} will be addressed as '{args.name}'.")


if __name__ == "__main__":
    main()
