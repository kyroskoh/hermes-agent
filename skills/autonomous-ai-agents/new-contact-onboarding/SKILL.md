---
name: new-contact-onboarding
description: "Greet a new person, ask who they are, register identity."
version: 1.2.0
author: kyroskoh, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, honcho, peer, onboarding, identity, new-contact, greeting, register]
    related_skills: [hermes-persona-routing, honcho-peer-roster]
    category: autonomous-ai-agents
---

# New Contact Onboarding

## When to use

- An inbound message lands from a peer that is **not** in `KNOWN_ALIASES` (see `honcho-peer-roster`)
- The Honcho peer card is empty/null for that peer
- A new phone number, Telegram UID, Discord ID, or webhook user_id shows up that isn't tied to Kyros, Wilnice, or a known contact
- Operator says "register this new person", "this is my friend X", or "I just gave someone your number"
- **Operator contacts you from a *new* LID that isn't yet in `KNOWN_ALIASES`** — cross-session context confirms it's them, but the ID is unbound. Greet as the operator, then bind the new LID as a secondary alias on the same canonical human (see "Operator-new-LID variant" below). Do NOT run the full stranger-onboarding handshake — that would be insulting to someone you already know.
- Operator runs an **identity-verification audit** ("how do you tag this LID?", "what if I'm impersonating you?", "do both checks"): explain the platform identifier + Honcho alias-map mechanism honestly, and offer to bind the LID + set up a challenge phrase for stronger checks (see "references/identity-verification-flow.md").

## What it produces

A polite first-time greeting that:

1. Addresses the sender by their **platform display_name** if any (never an assumed name)
2. Asks 2–3 short clarifying questions about who they are and their relationship to Kyros
3. Persists the identity to Honcho (peer card + conclusion) and updates `KNOWN_ALIASES` in the `honcho-peer-roster` skill
4. Ensures the next inbound from the same person is friction-free — addressed correctly, with the right persona

## When NOT to use

- The peer matches a known alias (Kyros, Wilnice, Wai Loong, Bille, etc.)
- The peer is a webhook peer (`webhook-*`) or an internal bot peer (`kyroskoh_bot`, `WilniceBot`, etc.)
- The peer is a cron/system peer (`cron_*`)
- The platform is `cli` or `web` and the operator's profile is `default`

## Why this skill exists

Kyros's Hermes fleet multiplexes many gateways. An inbound WhatsApp from a friend's phone number, a Telegram DM from a new contact, or a Discord message from a stranger can all look identical to a message from the operator. If the bot:

- Replies with Kyros's name or Wilnice's voice
- Treats the new person as an admin
- Says "hi BB" to a stranger
- Skips the intro and just answers the technical question

…any of those are a social failure that erodes trust. The fix: a deterministic onboarding handshake whenever a sender has no display name AND no peer card AND isn't in the known allowlist.

## When to trigger

Trigger this skill when ANY of:

1. The inbound sender's `peer_id` (or `user_id`/`chat_id`) is not in the `KNOWN_ALIASES` allowlist maintained by `honcho-peer-roster`.
2. The Honcho peer record for this sender has no `display_name` set in metadata.
3. The peer's peer card is `null`/empty.
4. The message is the first one in the session.

**Do NOT trigger** when:

- The peer matches a known alias (Kyros, Wilnice, Wai Loong, Bille, etc.) — let the existing persona routing handle it.
- The peer is a webhook peer (`webhook-*`) or an internal bot peer (`kyroskoh_bot`, `WilniceBot`, etc.).
- The message is from a cron/system peer (`cron_*`).
- The platform is `cli` or `web` and the operator's profile is `default` — those routes don't need onboarding.

## The onboarding handshake (5 steps)

### Step 1 — Detect unknown sender

Check the sender's identity through the cheapest available signal. **First translate the gateway's `chat_id`/`user_id` form into the Honcho peer_id form** — they differ by a single character (`@lid` ↔ `-lid`):

| Gateway log shows | Honcho peer_id |
|---|---|
| `chat=62758129242347@lid` | `62758129242347-lid` |
| `chat=6581103465` (bare digits) | `6581103465` (same) |
| `user=Kingsfield` (display_name) | (lookup by display_name if peer_id unknown) |

```bash
# Read the current peer card. Empty card = un-onboarded peer.
# Use the Honcho peer_id form, not the gateway @lid form.
PEER_ID="62758129242347-lid"
curl -sS "http://localhost:8000/v3/workspaces/hermes/peers/${PEER_ID}/card"

# If the gateway log only gave you chat_id @lid form, swap the @ for -:
#   chat_id  = 62758129242347@lid   →   peer_id = 62758129242347-lid
```

If the card returns `{"peer_card": null, "representation": null, ...}` (or an empty `peer_card` list) AND the peer isn't in `KNOWN_ALIASES`, this skill triggers.

### Step 2 — Address by their current display_name (if any)

If the platform session has a `display_name` (e.g. WhatsApp pushes the contact's saved name as `AO1`), use that as a placeholder:

> "Hi AO1 — I think this is our first time chatting. I'm kyroskoh_bot, Kyros's assistant. Mind introducing yourself so I know who I'm talking to?"

If there's no display name at all, fall back to a neutral address and offer to learn their name:

> "Hi — first time we chat as far as I can tell. I'm kyroskoh_bot. Who am I speaking with?"

**Never assume.** Don't guess "Hi Bille" just because the inbound message mentioned "Bille" — that's still an assumption the user hasn't confirmed.

> **Operator rule (Kyros, 2026-08-28):** "Whenever there's a new person chat with you, don't assume it's all me, always ask the user to introduce so you can address him/her (or identify by the display_name if not introduced) next time so it's also better to register as a new peer with that info." This skill exists to enforce that rule mechanically — routing alone is not enough; the bot must actively onboard strangers, not just fall through to the `*` profile default.

### Step 3 — Ask the clarifying questions

Use the `clarify` tool to ask 2–3 short questions in one form so the user can answer in one turn:

1. **Name** (open text) — "What's your name (or what should I call you)?"
2. **Relationship to Kyros** (single-select, recommended first): friend / family / colleague / work acquaintance / other
3. **How they found the bot** (single-select, optional if it slows things down): group chat / mutual friend / shared contact / other

Don't dump all three on the first message if the user just wanted a quick reply — read the room. If they asked a technical question, answer the question first, THEN ask the intro at the end of the reply.

### Step 4 — Persist the identity durably

Once the user identifies themselves, write to **all three** tiers so the next session is friction-free:

```bash
# Tier 3a — Honcho peer card (curated, hand-written facts).
# Body field MUST be `peer_card` — passing `card` returns 422 missing field.
curl -sS -X PUT \
  "http://localhost:8000/v3/workspaces/hermes/peers/kyroskoh_bot/card?target=<peer_id>" \
  -H "Content-Type: application/json" \
  -d '{
    "peer_card": [
      "<Name> is Kyros <relationship>; first chatted <date>.",
      "Display name: <name>. Language: <observed>."
    ]
  }'

# Tier 3b — Honcho conclusion (deriver-distilled, semantically retrievable)
# Use the honcho_conclude MCP tool:
honcho_conclude(peer="<peer_id>", conclusion="<Name> introduced themselves on <date>: <relationship>. Preferred address: <name>.")
```

If using the MCP `honcho_profile`/`honcho_conclude` tools, prefer those — they handle the observer/target asymmetry correctly. If doing raw curl, remember `PUT /peers/{observer}/card?target={observed}` is observer-scoped, not self-scoped.

### Step 5 — Update the allowlist

Add the new peer to the `KNOWN_ALIASES` block in the `honcho-peer-roster` skill's script (`scripts/scan_peers.py`) so future roster scans don't re-flag them as fresh:

```python
KNOWN_ALIASES["<Display Name>"] = {
    "lids":     ["<peer_id>"],   # or phones / telegram / discord — whatever applies
    # Optional context comment for future maintainers:
    # "<relationship to Kyros>; <how they found the bot>"
}
```

Also append the same entry to the `KNOWN_ALIASES` block in the `honcho-peer-roster/SKILL.md` body so the documented allowlist stays in sync with the script.

## Operator identity challenge (since 2026-08-29)

For any inbound from a peer ID that **is not** in `KNOWN_ALIASES` AND **claims to be** the operator (Kyros), gate the operator-level response on **two factors**: a short bcrypt-hashed phrase AND a TOTP code (Authy-compatible). See `references/identity-verification-flow.md` for the full threat model and design rationale.

**Phrase factor** (`${HERMES_HOME}/.auth/challenge.txt` + `verify_challenge.py`):
- 2 bcrypt hashes (cost 12) for the canonical orderings of the 4-token NS history. Stored normalized (lowercase, no separators) so dashes/casing don't trip.
- Strict exact-match against stored hashes. No fuzzy logic, no canonicalization beyond cosmetic normalization.
- `--show-hint` prints a non-secret tag-list (e.g. "post-BMT unit (starts with A)") so the operator can reconstruct the phrase from memory.
- Rate limit: **3 failed attempts / 60s**. Successful matches do **NOT** count toward the trip. On trip, a 300s cooldown activates, then 3× each repeat trip within 1800s, capped at 24h.
- Trip state persists in `${HERMES_HOME}/.auth/cooldown_state.json` (mode 0600).
- The verifier returns exit-code 5 + `RATE_LIMITED:{seconds}_remaining_trip#{N}`; the combined verifier (`verify_factor.py`) propagates that as its own exit 4.

**TOTP factor** (`${HERMES_HOME}/.auth/mfa/` + `verify_mfa.py`):
- 20-byte (160-bit) base32 secret, RFC 6238, 6 digits, 30s period, ±30s drift window.
- Seed file `seed.b32` mode 0600; QR PNG `seed.png` mode 0600 (scan into Authy with issuer `Hermes Agent (Kyros / admin)`, account `kyros@kyroskoh_bot`).
- Plaintext secret is NEVER logged, NEVER echoed, NEVER written to Honcho. Only a SHA-256 fingerprint goes into `mfa.meta.json`.

**Combined verifier** (`${HERMES_HOME}/.auth/verify_factor.py`):
- Requires BOTH factors match in the same call.
- Exit codes: `0 FULL_MATCH` / `2 PHRASE_NO_MATCH` / `3 TOTP_NO_MATCH` / `4 RATE_LIMITED` (rate-limit hit on either sub-verifier).
- Sub-verifier stderr is passed through verbatim so the gateway can show the operator the right cooldown remaining.

**Trigger:** first inbound from a peer claiming operator status where the LID is not in KNOWN_ALIASES. Do NOT trigger for non-operator identities, webhook peers (`webhook-*`), cron peers (`cron_*`), or internal bot peers (`kyroskoh_bot`, `WilniceBot`, …).

**Response on FULL_MATCH:** proceed with operator-level actions and run `register_peer.py` to bind the new LID so the next session is friction-free.

**Response on NO_MATCH / no answer / RATE_LIMITED:** treat as non-operator; respond with the standard new-contact handshake (Step 2 above), not operator-level.

**Rotation cadence:** phrase at 30 days, MFA seed at 90 days. Both fields stored in their respective meta JSONs as `rotation_recommended_at`.

**Honcho audit trail** for this section (conclusions recorded, no plaintexts): `LLr2pgeuO6d5tXbAamYAe` (initial), `ow27J6ZH-9_ZMNVXg41rM` (second phrase), `JNu7tpJHK_TkGARfLwyJU` (progressive cooldown), `Q6ofOYvj3Rez_zGQceupF` (MFA).

## Fallback: incomplete identification

If the user declines to introduce themselves ("I'd rather not say", or just keeps chatting without answering), still register the minimum needed:

- The peer card gets a single observation: "Self-introduced peer; relationship unknown. Display name from platform: <display_name>."
- The peer stays OUT of `KNOWN_ALIASES` — they remain "unidentified" in the roster until they self-identify.
- The bot keeps using the platform display_name (or "you") as the address.

Don't push for an intro on every subsequent message. Ask once, register what you have, and move on.

## Operator-new-LID variant (operator, secondary identifier)

When the inbound sender is **clearly the operator** (cross-session context, message style, knowledge of the system, ability to authoritatively audit the bot's identity model) but their LID/phone is NOT in `KNOWN_ALIASES`, do not run the stranger-onboarding handshake. That path assumes a stranger and is socially wrong for the operator.

Instead:

1. Acknowledge the gap honestly: "This LID isn't in my Honcho alias map yet — I'll bind it."
2. Run `register_peer.py` with `--name "Kyros"` (or the canonical human name) and a relationship string that flags it as a secondary ID: `"operator (secondary WhatsApp LID; primary is <other-lid>)"`.
3. The script writes the peer card, fires the Honcho conclusion, and patches `KNOWN_ALIASES` in both `scan_peers.py` and `SKILL.md` of `honcho-peer-roster`.
4. Subsequent inbound from the new LID is recognized without ceremony.

**Don't** ask "who am I speaking with?" — that wastes the operator's time and reads as the bot having forgotten who they are. The whole point of the binding step is to make the next session silent.

(Bit by this 2026-08-29 when Kyros messaged from `188661404582023-lid` — primary is `5927843410163-lid` — and I greeted generically, then doubled back with the wrong name on the correction.)

## Identity-verification audit (operator asks "how do you identify me?")

When the operator probes identity mechanics — "how do you tag this number?", "what if I'm impersonating you?", "do both checks" — give them an honest, technical answer with these three layers and what's actually true about each:

| Layer | What it verifies | Strength |
|---|---|---|
| **Platform identifier** | The gateway-attached chat_id / user_id on every inbound message | Routing label, not proof |
| **Honcho alias map** | `KNOWN_ALIASES` binding that LID to a canonical human | Persistent across sessions, but only as strong as whoever set it up |
| **Cross-session context** | Prior conversations, message style, knowledge depth, displayed personality preset | Behavioral signal; a prepared attacker can mimic it |

State the limits plainly: "I'd fail as a guard against a determined impersonator. My identity check is basically pattern-matching." Then offer the binding + challenge-phrase combination as a real upgrade — see `references/identity-verification-flow.md`.

## Pitfalls

- **Don't address an unknown peer by a name they haven't given you.** "AO1" was WhatsApp's saved name for Bille's number, not his self-identification. Even though it's a string, treating a platform label as a person's stated name is wrong.
- **Don't assume a sender is Kyros or Wilnice based on context.** A WhatsApp message from a number adjacent to Wilnice's could still be someone else — Kyros explicitly said so: "don't assume it's all me".
- **Don't auto-write identity conclusions without operator or sender confirmation.** `honcho_conclude` is a durable fact. A bot guessing "Bille is Kyros's cosplay partner" without the sender introducing themselves is fabricating memory.
- **Don't bypass the intro just because the technical question is urgent.** If someone asks a legit question, answer it — then ask the intro at the tail. Don't be rude; don't be overbearing.
- **Don't put webhook/system/cron peers through onboarding.** They'll never introduce themselves and it's fine. Keep them in the allowlist so the onboarding path skips them.
- **Phone JID ≠ LID ≠ Telegram UID ≠ Discord ID.** A single person may appear under multiple IDs across platforms. The card stores the canonical human; aliases are tracked in `KNOWN_ALIASES`. See `honcho-peer-roster` for the routing logic.
- **Display name on a long-running peer may be stale.** WhatsApp updates `display_name` opportunistically. Trust the Honcho peer card and `KNOWN_ALIASES` mapping over the live `display_name` once a person has been onboarded.
- **Honcho peer-card body field is `peer_card`, not `card`.** The PUT endpoint returns `422 {"detail":[{"type":"missing","loc":["body","peer_card"]}]}` if you pass `card`. Other Honcho endpoints have similar mismatches — when something 422s on a field name, check the OpenAPI spec or the response body before guessing a different shape. (Bit by this 2026-08-28 onboarding Shiva.)
- **Don't skip Step 4 verification.** After PUT, GET the card back and confirm the facts landed. A 200 response on PUT only means the request was well-formed, not that the contents were what you wanted.
- **The `<observer_peer_id>` in the PUT URL matters.** `kyroskoh_bot` (operator profile) vs `ai` (observer-only) write to different card slots. Pick deliberately — and remember the operator's profile-scoped `kyroskoh_bot` card is what the operator's session sees, while `ai` is what general-purpose peers see.
- **Gateway `chat_id` form vs peer_id form: `@lid` ≠ `-lid`.** Gateway logs show `chat=62758129242347@lid`; the Honcho peer_id is `62758129242347-lid`. The `@` becomes `-` and the rest is identical. If you grab the chat_id straight from the gateway log and try to look up the peer card, you'll get a 404 — always substitute `@lid` → `-lid` before calling Honcho. (Bit by this 2026-08-28 onboarding Kingsfield.)
- **Don't confuse a Home-channel route with a fresh contact.** The Home channel destination `5927843410163@lid` is Kyros's own LID (used as a routing target). A new inbound `chat=62758129242347@lid` is NOT another one of Kyros's IDs — it's a separate person. Confirm via `gateway.log` line: `user=<display_name>` is the actual sender; `chat=…` is just the conversation ID. (Bit by this 2026-08-28 onboarding Kingsfield.)
- **`register_peer.py` PUT body field is `peer_card`, not `card`.** The SKILL.md got the pitfall right; the script itself drifted and shipped with `{"card": [...]}`. The script then 422s with `{"detail":[{"type":"missing","loc":["body","peer_card"],...}]}` on the first run. Always run the script once and verify the read-back `card facts now: N` is non-zero — a 200 PUT with `card facts now: 0` means the field name is wrong. Fixed in the script 2026-08-29.
- **The script's read-back also read the wrong key.** It asked `card.get('card', [])` while the GET response uses `peer_card`. So a successful PUT would print `card facts now: 0` even when 6 facts actually landed. Always do an independent `curl ... /card?target=…` after `register_peer.py` to confirm — never trust the script's own print line alone. (Bit by this 2026-08-29 binding Kyros's secondary LID.)
- **Patch-allowlist inserts can crush the closing brace.** The script's `KNOWN_ALIASES` patcher walks braces from the opening `{` to find the matching `}` — if the file ends with `},\n}` (no newline before `}`), the walker still works, but the inserted block lands tightly against the prior entry. Run `python3 -c "import ast; ast.parse(open(...).read())"` after any auto-patch to confirm the script is still syntactically valid. Cosmetic but easy to miss in a multi-day diff.
- **Operator audits deserve honest answers, not flattery.** When the operator asks "what if I'm impersonating you?" or "how do you even tag this LID?", give the technical truth (platform ID + Honcho alias + behavioral signals, all with stated weaknesses) rather than reassuring platitudes. The audit is a check on the bot's reasoning; answering it confidently-but-vacuously tells the operator the bot can't self-critique.

## Verification

After onboarding a new contact:

1. The peer card reads back the new facts via `GET /peers/{observer}/card?target={peer_id}`.
2. `honcho_conclude(list=true, peer=<peer_id>)` returns the new conclusion.
3. `KNOWN_ALIASES` in `honcho-peer-roster/scripts/scan_peers.py` and the matching SKILL.md body include the new entry.
4. The next roster scan flags the peer as "known" rather than "fresh".
5. A subsequent inbound from the same peer/phone addresses them by their preferred name automatically.

## Support files

- `references/onboarding-conversation-templates.md` — ready-to-send message templates for WhatsApp, Telegram, Discord, and the web UI, with multi-language variants.
- `references/identity-verification-flow.md` — concrete 3-layer model (platform ID + Honcho alias map + cross-session context) with the challenge-phrase upgrade path; use when the operator audits how the bot identifies them.
- `scripts/register_peer.py` — single-command helper that does Steps 1+4+5 (card write + conclusion + allowlist patch).
