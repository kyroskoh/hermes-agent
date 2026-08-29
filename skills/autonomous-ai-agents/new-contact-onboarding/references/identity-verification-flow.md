# Identity Verification Flow — operator/impersonation defense

The bot identifies the sender via **three layered signals**. None of them is
cryptographic; each has a stated weakness. The combination plus an optional
challenge phrase (see "Hardening" below) raises the bar meaningfully.

## The three layers

| Layer | What it verifies | Strength |
|---|---|---|
| **Platform identifier** | The gateway-attached `chat_id` / `user_id` on every inbound message — e.g. `188661404582023@lid` on WhatsApp, `1441204397` on Telegram, `<discord snowflake>` on Discord | Routing label, not proof. Spoofable by anyone with platform-level access (SIM swap, account takeover). |
| **Honcho alias map** | The `KNOWN_ALIASES` block in `honcho-peer-roster/scripts/scan_peers.py` (and the matching block in `SKILL.md`) binding that LID to a canonical human (`Kyros`, `Wilnice`, `Bille`, …) | Persistent across sessions. Only as strong as the human who set it up. |
| **Cross-session context** | Prior conversations, message style, knowledge depth, displayed personality preset, ability to authoritatively audit the bot's identity model | Behavioral signal. A prepared attacker who has read transcripts can mimic it. |

When the bot says "I call you Kyros", what it actually means is: "I see
`chat_id=188661404582023@lid`, that LID is in `KNOWN_ALIASES["Kyros"]["lids"]`,
and prior session context corroborates you're the operator." A determined
impersonator with the same LID would sail through all three.

## Gateway `chat_id` ↔ Honcho `peer_id` translation

The two forms differ by a single character (`@` vs `-`):

| Gateway log shows | Honcho peer_id |
|---|---|
| `chat=188661404582023@lid` | `188661404582023-lid` |
| `chat=62758129242347@lid` | `62758129242347-lid` |
| `chat=6581103465` (bare digits) | `6581103465` (same) |

Always substitute `@lid` → `-lid` before calling Honcho's `GET /peers/{id}/card`
or `POST /peers/list`. Picking the wrong form returns 404.

## How an operator gets recognized vs impersonated

- **Recognized operator:** incoming LID is in `KNOWN_ALIASES` → bot uses the
  canonical human's persona, runs operator-level actions directly.
- **Unrecognized LID claiming to be operator:** bot should give an honest
  identity-mechanics answer (the three layers above), then offer to:
  1. Bind the LID to the operator's canonical human (`register_peer.py`).
  2. Set up a challenge phrase (see Hardening) for stronger checks.
- **Unrecognized LID claiming nothing:** standard new-contact handshake
  (`Step 2` in the parent SKILL.md).

## Hardening: bcrypt challenge phrase

Filesystem layout (mode 0600/0700, gitignored):

```
/root/.hermes/.auth/
├── challenge.txt            # one bcrypt hash per line, cost 12
├── challenge.meta.json      # hint + accepted_phrases[] + rotation date
├── cooldown_state.json      # progressive rate-limit trip history
├── attempts.log             # MATCH / NO-MATCH verdicts, no plaintext
└── verify_challenge.py      # CLI: --candidate "..." / --show-hint
```

### Verifier behaviour

- bcrypt cost 12, **strict exact-match** against every hash in
  `challenge.txt`. No fuzzy logic, no canonicalisation — `Leopard-AETC-321SCE-MR`
  and `AETC-Leopard-321SCE-MR` are different phrases unless both are stored.
- `accepted_phrases[]` in `challenge.meta.json` carries the SHA-256
  fingerprint (first 16 hex chars) of every stored phrase. The hint block
  encodes the order via parenthetical initials (e.g. `post-BMT unit (starts
  with A)`) so reconstruction does not depend on narrative memory.
- **Never log or echo the candidate plaintext.** Only `MATCH` / `NO-MATCH`
  / `RATE_LIMITED:{seconds}_remaining_trip#{N}` leave the script.

### Rate limit (anti-DoS + typo recovery)

```
WINDOW_SECONDS          = 60
MAX_ATTEMPTS_PER_WINDOW = 3
COOLDOWN_SECONDS        = 300       # 5 min base
REPEAT_TRIP_WINDOW      = 1800      # 30 min — if next trip within this, multiply
REPEAT_COOLDOWN_MULTIPLIER = 3.0
MAX_COOLDOWN_SECONDS    = 86400     # 24 h cap
```

Sequence on persistent failure: 5 min → 15 min → 45 min → 2 h 15 m → … capped
at 24 h. Trip history persists in `cooldown_state.json`; cooldowns anchor to
the trip timestamp, not to the rolling window.

### Trigger

First inbound from a peer ID that:
- Is not in `KNOWN_ALIASES`, AND
- Claims to be the operator (Kyros).

Do **not** trigger for: non-operator humans, webhook peers (`webhook-*`),
cron peers (`cron_*`), or internal bot peers (`kyroskoh_bot`, `WilniceBot`, …).

### Response

- **MATCH:** proceed with operator-level actions; run `register_peer.py` to
  bind the new LID so the next inbound is friction-free.
- **NO-MATCH / no answer / RATE_LIMITED:** treat as non-operator; respond
  with the standard new-contact handshake, not operator-level.

## When to rotate

The `rotation_recommended_at` field in `challenge.meta.json` defaults to
30 days from creation. On rotation:

1. Pick a new phrase (use the hint-encoding pattern so it's memorable).
2. Run a fresh `register_peer.py`-style flow with `--skip-card --skip-conclusion --skip-roster-patch`
   that just regenerates `challenge.txt` from the new phrase(s).
3. Update `accepted_phrases[]` and `hint` in `challenge.meta.json`.
4. Append a Honcho conclusion recording the rotation (no plaintext).

## Pitfalls

- **Hash file is multi-line now.** Treat it as `hashes = file.read().splitlines()`,
  not `file.read()`. The single-hash form from earlier iterations will silently
  reject all candidates.
- **`peer_card` vs `card`.** Honcho's PUT body field is `peer_card`. Using
  `card` returns 422 missing-field. (Bit by this 2026-08-28 Shiva onboarding.)
- **The verifier's read-back is your own verification.** Run
  `python3 /root/.hermes/.auth/verify_challenge.py --show-hint` after any
  rotation to confirm the hint landed; run a no-op
  `--candidate "..."` against the new phrase to confirm `MATCH`.
- **`KNOWN_ALIASES` duplicate-key landmine.** Python dicts silently
  overwrite on duplicate keys. The `register_peer.py` patcher used to do this
  silently — it now merges into the existing entry. Always run
  `python3 -c "import ast; ast.parse(open('<file>').read())"` after any auto-patch.
- **Plaintext phrase leaks.** The phrase, once chosen, ends up in: this
  conversation transcript, any Honcho conclusion the deriver distilled from
  this session, and (if you write it down) your password manager. None of
  those are in the verifier's threat model — but they do expand the set of
  people with read access.
