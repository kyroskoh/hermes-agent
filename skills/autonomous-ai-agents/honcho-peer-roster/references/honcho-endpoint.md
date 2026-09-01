# Honcho Endpoint Reference (this deployment)

For Kyros's Hermes fleet as of 2026-08-28.

## URLs

| Purpose | URL |
|---|---|
| Honcho REST API | `http://localhost:8000` |
| Workspace | `hermes` |
| OpenAPI spec | `http://localhost:8000/openapi.json` |
| Local UI (proxied at `/honcho/`) | internal `:9000` (Honcho Local) |

Honcho runs as a local Docker stack under `/root/honcho/` — do **not** assume it's a remote SaaS endpoint.

## Peer endpoints actually used by this skill

| Verb | Path | Purpose |
|---|---|---|
| `POST` | `/v3/workspaces/{ws}/peers/list` | List every peer. Body must be `{}`. **GET returns 405.** |
| `GET`  | `/v3/workspaces/{ws}/peers/{id}/card` | Read the durable fact list for one peer. |
| `POST` | `/v3/workspaces/{ws}/peers/{id}/search` | Semantic search over a peer's message history. |

Other peer endpoints (chat, context, representation, sessions) live in the OpenAPI spec but are not part of this skill's roster workflow.

## Auth

No header-based auth in this deployment — Honcho listens on `127.0.0.1:8000` and the gateway is colocated. Don't put an `Authorization` header on these requests.

## Health check

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:8000/v3/workspaces/hermes/peers/list \
  -X POST -H "Content-Type: application/json" -d '{}'
```

- `200` → healthy, proceed.
- `404` → wrong workspace or Honcho not configured for `hermes`.
- `500` → Honcho process up but LLM-dependent endpoints broken; check `/etc/cron.d/honcho-llm-watchdog` and the JWT `exp` on `LLM_OPENAI_API_KEY` (`docker exec honcho-api-1 printenv LLM_OPENAI_API_KEY` then decode the middle segment).
- connection refused → `docker compose ps honcho-api-1`; restart with `docker compose up -d --force-recreate api deriver` (restart does NOT re-read env_file).

## Known-peers allowlist (this deployment, 2026-08-28)

```python
KNOWN = {
    # Internal AI peers (one per profile + the operator-facing one)
    "kyroskoh_bot", "WilniceBot", "KyrosBot",
    # Operator + girlfriend human peers (canonical names)
    "Kyros", "Wilnice",
    # Webhook / system peers
    "webhook-github-prs", "webhook-github-pr-review",
}

KNOWN_ALIASES = {
    "Kyros": {
        "phones":   ["6580323587"],
        "lids":     [
            "5927843410163-lid",              # main WhatsApp privacy-LID
            "199999480688782-lid",            # secondary phone (Vivo x70 Pro+)
        ],
    },
    "Wilnice": {
        "phones":   ["6581103465"],
        "telegram": ["7233071505"],
        "lids":     ["171666202210553-lid"],
    },
    "Wai Loong": {
        "lids":     ["226576889331767-lid"],  # kawaii-personality trigger peer
    },
}
```

When the operator confirms a new peer (e.g. Bille → `113048287211723-lid`), append to `KNOWN_ALIASES` with a one-line comment so future runs don't re-flag them as fresh.

## Phone ↔ LID note

WhatsApp sends two IDs per contact:

- **Phone JID** — bare digits, e.g. `6581103465`
- **Privacy LID** — `171666202210553-lid` (note the trailing `-lid`, not `@lid` — Honcho strips the `@`)

Both belong to the same person but they are separate Honcho peers. Match by metadata, display name, or `state.db` session content — not by ID string.

## state.db lookup order

When enriching a fresh peer with first-message context, the script probes in this order:

1. **`sessions` table** — match by `user_id` or `chat_id` (state.db stores WhatsApp LIDs with `@lid`, Honcho stores them with `-lid` — the script tries both forms).
2. **`messages` table** — substring search of `session_id` or `content` for the peer_id.

Per-profile state DBs to check: `/root/.hermes/state.db`, `/root/.hermes/profiles/{kyros,wilnice}/state.db`. Override with `--state-db` to point elsewhere.
