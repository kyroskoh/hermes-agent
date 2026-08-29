---
name: honcho-peer-roster
description: "Audit Honcho peers and flag fresh ones with first message."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, honcho, peers, audit, identity, new-contact, roster]
    category: autonomous-ai-agents
---

# Honcho Peer Roster

## When to use

- Operator asks **"any new people talked to you?"**, **"who's in your Honcho"**, **"show me the peer list"**
- After a deployment or gateway change, audit which peers actually exist
- When an inbound message lands from an unknown sender and you need to identify them
- Before claiming "I haven't heard from X lately" — verify against the peer roster, not memory

## What it produces

A deterministic roster of every peer in the Honcho workspace, partitioned into:

- **Known** — operator-approved humans, internal bots, webhook peers, owner phones/LIDs
- **Fresh** — created within the last 7 days, with the first inbound message pulled from `state.db`
- **Unidentified** — older peers with no `display_name` and no first message

Each fresh peer carries enough context (first message, session ID, platform) for the operator to recognize them without leaving the chat.

## Why this skill exists

Honcho is the long-term memory layer. Every WhatsApp/Telegram/Discord/Web sender eventually becomes a `peer` in `/v3/workspaces/{workspace_id}/peers`. On a multi-profile, multi-gateway deployment, that list grows quietly — sometimes with contacts the operator doesn't recognize (a friend's friend in a group, a one-off inquiry, a mis-routed message). Without a routine audit, the operator only finds out when something goes sideways.

This skill turns "list everyone + flag the unfamiliar" into a single deterministic workflow.

## Step 1 — Confirm the workspace and endpoint

Honcho runs locally on `http://localhost:8000` in this deployment (see `references/honcho-endpoint.md` for the full topology). The workspace is `hermes`.

```bash
# Sanity check Honcho is up
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/v3/workspaces/hermes/peers/list \
  -X POST -H "Content-Type: application/json" -d '{}'
```

Expected: `200`. Anything else → Honcho is down; check `docker compose ps honcho-api-1` and the LLM sync watchdog before continuing.

## Step 2 — Pull the full peer list

Honcho's list endpoint is `POST /v3/workspaces/{workspace_id}/peers/list` with an empty JSON body. `GET` returns 405.

```bash
curl -sS -X POST http://localhost:8000/v3/workspaces/hermes/peers/list \
  -H "Content-Type: application/json" -d '{}' \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
items = data.get('items', data) if isinstance(data, dict) else data
print(f'TOTAL PEERS: {len(items)}')
print()
print(f'{\"PEER_ID\":<35} {\"CREATED_UTC\":<28} DISPLAY_NAME')
print('-' * 90)
for p in sorted(items, key=lambda x: x.get('created_at','')):
    pid   = p['id']
    ts    = p.get('created_at','')[:19]
    name  = p.get('metadata',{}).get('display_name','') or ''
    print(f'{pid:<35} {ts:<28} {name}')
"
```

Each peer has `id`, `workspace_id`, `created_at`, `metadata.display_name`, `configuration`. **There is no `updated_at` field** — freshness is inferred from `created_at` and from recent message activity in `state.db`.

## Step 3 — Build the known-peers allowlist

Different per deployment. For Kyros's setup (as of 2026-08-28) the canonical allowlist is:

```python
KNOWN = {
    # Internal AI peers
    "kyroskoh_bot", "WilniceBot", "KyrosBot",
    # Operator + girlfriend human peers (canonical names)
    "Kyros", "Wilnice",
    # Webhook / system peers
    "webhook-github-prs", "webhook-github-pr-review",
}

# Identity aliasing: a single person may have multiple Honcho peer IDs
# (one per WhatsApp JID + one per WhatsApp LID + one per Telegram/Discord UID).
# Map them here so the script knows they all belong to the same human.
KNOWN_ALIASES = {
    "Kyros": {
        "phones":   ["6580323587"],
        "lids":     [
            "5927843410163-lid",              # main WhatsApp privacy-LID
            "199999480688782-lid",            # secondary phone (Vivo x70 Pro+)
            "188661404582023-lid",            # platform=whatsapp, operator (secondary WhatsApp LID; primary is 5927843410163-lid)
        ],
    },
    "Wilnice": {
        "phones":   ["6581103465"],
        "telegram": ["7233071505"],
        "lids":     ["171666202210553-lid"],
    },
    "Wai Loong": {
        "lids":     ["226576889331767-lid"],
    },
    "Bille": {
        "lids":     ["113048287211723-lid"], # cosplay partner, KL trip companion
    },
    "Stephy": {
        "lids":     ["266356792533046-lid"],
    },
    "Shiva": {
        "lids":     ["267056872190171-lid"], # ex-colleague, introduced by Kyros 2026-08-28
    },
    "Kingsfield": {
        "lids":     ["62758129242347-lid"],  # new WhatsApp contact, first chatted 2026-08-28
    },
}

def is_known(pid: str) -> bool:
    if pid in KNOWN:
        return True
    for human, aliases in KNOWN_ALIASES.items():
        for ids in aliases.values():
            if pid in ids:
                return True
    return False
```

**Phone ↔ LID note:** WhatsApp sends two IDs per contact — the phone-number-based JID (`6581103465`) and the privacy-LID (`171666202210553-lid`). Both belong to the same person but they are separate Honcho peers. Match by metadata or by chat history, not by ID string.

If the operator adds new human peers (e.g. Bille — `113048287211723-lid`), append to `KNOWN` with a comment so future runs don't flag them as fresh.

## Step 4 — Surface fresh and unidentified peers

A peer is "fresh" when ANY of:

1. `created_at` is within the last 7 days (or whatever window the operator asked for)
2. No `display_name` in metadata
3. Not in the `KNOWN` allowlist and ID doesn't match a recognised pattern (`*-lid`, digit-only phone, or a Telegram/Discord user ID the operator has previously identified)

For each fresh peer, pull the **first inbound message** from `state.db` so the operator can recognize them:

```bash
sqlite3 /root/.hermes/state.db "
  SELECT m.timestamp, m.session_id, m.content
  FROM messages m
  WHERE m.role = 'user'
    AND (m.session_id LIKE '%<peer_id>%' OR m.content LIKE '%<peer_id>%')
  ORDER BY m.timestamp ASC
  LIMIT 3;
"
```

If `sqlite3` isn't installed, use the Python wrapper (see `scripts/scan_peers.py` in this skill).

Also enumerate the **per-profile state DBs** under `/root/.hermes/profiles/*/state.db` — the gateway multiplexes by profile and the operator may have forgotten one exists.

## Step 5 — Produce the report

Output structure:

```
HONCHO PEER ROSTER — workspace=hermes
Total peers: N
Known: K  Fresh (≤7d): F  Unidentified: U

KNOWN PEERS (operator-approved)
  Kyros              2026-08-20 15:35  display_name=Kyros
  Wilnice            2026-08-20 16:15
  kyroskoh_bot       2026-08-22 13:06
  ...

FRESH PEERS — last 7 days
  113048287211723-lid  2026-08-28 13:38  (WhatsApp LID)
    First message: "Sorry I am Bille, not kyros"
    Session: 20260828_133812_cc4f97e4 (3 msgs)
    Verdict: FRIEND — cosplay partner, KL trip companion

UNIDENTIFIED
  199999480688782-lid  2026-08-21 10:50  (WhatsApp LID)
    No inbound content found in state.db
    Verdict: NEEDS_OPERATOR_INPUT
```

`Verdict` is inferred from the first message content; mark `NEEDS_OPERATOR_INPUT` when no chat history exists yet.

## Pitfalls

- **`GET /peers` returns 405** — the list endpoint is `POST /peers/list` with body `{}`. Don't waste a turn on the wrong verb.
- **Gateway `chat_id` form ≠ Honcho peer_id form.** Gateway logs show `chat=62758129242347@lid` but the Honcho peer_id is `62758129242347-lid`. The `@` becomes `-`. When you're looking up a peer by ID from a gateway log line, swap `@lid` → `-lid` before calling `POST /peers/list` or `GET /peers/{id}/card`. (Bit by this 2026-08-28 onboarding Kingsfield.)
- **Two IDs per WhatsApp contact.** Phone JID and `-lid` are separate peers; the same person shows up twice. Cross-reference via `state.db` session content or via `creds.json` lid-mapping (see `channel-binding-and-identity-verification`).
- **Home-channel destination ≠ sender.** A `chat=…` value in gateway.log is the conversation/route ID, not the sender. The sender is `user=<display_name>` on the same line. Don't try to onboard someone whose `chat_id` happens to match Kyros's own LID — that's just the Home channel route, not a new contact. (Bit by this 2026-08-28 onboarding Kingsfield.)
- **No `updated_at` on the peer object.** A peer from August 2026 with no `display_name` is still "fresh" in the metadata sense even if it's been silent for weeks. Use `state.db` activity as the freshness signal for known-but-quiet peers.
- **Cron/system peers are normal.** The list will include bot peers like `kyroskoh_bot` and webhook peers like `webhook-github-pr-review`. These are NOT new contacts — keep them in the allowlist.
- **WhatsApp peer IDs change format.** Bare digits (`6581103465`) are the phone-JID form; `171666202210553-lid` is the privacy-LID form. Both are valid; both must be matched against the allowlist using the appropriate substring.
- **Don't write a peer card without operator confirmation.** Surfacing a fresh peer is one thing; writing `honcho_conclude(peer="Bille", conclusion="cosplay partner")` is a durable fact and should require explicit "yes, treat them as known" before being saved.
- **Honcho `card` field exists separately.** Use `honcho_profile(peer=<id>)` or `GET /v3/workspaces/{ws}/peers/<id>/card` to read the durable fact list for that peer. The roster is for discovery; the card is for already-known context.

## Verification

After running the report, the operator should be able to:

1. See the total peer count match the previous known count + any new ones.
2. Identify each fresh peer by name/context from the first message snippet.
3. Decide per fresh peer: known-and-should-be-carded, transient (don't card), or unknown (ask operator).

If the operator says "yes, treat them as known" (or a stranger introduces themselves by name and relationship), hand off to the **`new-contact-onboarding`** skill. That skill owns Steps 4–5 (peer card write + Honcho conclusion + allowlist patch via `register_peer.py`); this skill only does the discovery half. Don't try to write a peer card from inside this skill — it duplicates work and bypasses the onboarding handshake the operator explicitly requires.

## Support files

- `scripts/scan_peers.py` — single-file CLI that does steps 2–4 and prints the report.
- `references/honcho-endpoint.md` — local Honcho topology and port map for this deployment.
