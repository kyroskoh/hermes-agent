# Fork — kyroskoh/hermes-agent additions

Every file in this directory is **owned by the fork** (`kyroskoh/hermes-agent`)
and is excluded from upstream `NousResearch/hermes-agent` rebases by virtue of
its path. New fork-only features go under `web/src/fork/`; upstream-owned files
(`web/src/App.tsx`, `web/src/i18n/*`, etc.) are not edited for fork additions.

## Layout

```
web/src/fork/
├── README.md                  ← this file (rebase playbook)
├── registry.ts                ← single mount point: registers routes + nav + i18n
├── pages/                     ← fork-only React pages (lazy-loaded by registry)
│   └── BackupsPage.tsx
├── i18n/                      ← fork-only translations (no type-cascade)
│   ├── fork-translations.ts
│   ├── en.ts                  ← canonical English strings
│   └── stubs.ts               ← empty-string stubs for non-English locales
├── api/                       ← (optional) fork-only API client wrappers
└── tests/                     ← fork-only smoke tests (vitest)
```

## How `registry.ts` works

`registry.ts` is the **single import point** that upstream sees. It uses the
existing plugin-slot system (`registerSlot`, `PluginSlot`) so:

- No edits to `web/src/App.tsx`
- No edits to `web/src/i18n/types.ts`
- No cascade to 16 locale files
- No edits to the nav/route maps in upstream

The only upstream-owned edit is **one import line** in `web/src/App.tsx`:

```typescript
import "@/fork/registry"; // FORK: kyroskoh/hermes-agent — registers /backups route
```

That import is the entire rebase surface. When upstream changes anything else,
the fork recompiles unchanged.

## Rebase playbook (5 min, weekly)

```bash
cd /usr/local/lib/hermes-agent

# 1. Make sure the working tree is clean
git status --short

# 2. Fetch upstream
git fetch origin

# 3. Check what upstream changed in our risk surface
git log --oneline origin/main --since='2 weeks ago' -- \
  web/src/App.tsx web/src/i18n/ web/src/plugins/ web/src/main.tsx

# 4. Try a dry-run merge to see conflicts
git merge-tree $(git merge-base HEAD origin/main) HEAD origin/main | \
  grep -E '^(changed|added|removed) in both' \
  && echo "⚠️ conflicts in upstream files we touched" \
  || echo "✓ clean merge"

# 5. If clean: rebase
git rebase origin/main

# 6. If conflicts: they will be in web/src/App.tsx around the one import line.
#    Resolution is mechanical: keep both the upstream import block AND the
#    `import "@/fork/registry"` line.

# 7. Verify the build
cd web && npm run typecheck && npm run build
cd /root/honcho-local-cli && python3 -m unittest tests.test_backups

# 8. Force-push (we use Option A: linear history)
git push fork HEAD:main
```

## Discipline for new fork features

1. Put every fork file under `web/src/fork/`.
2. Mark every fork commit message with `FORK: kyroskoh/hermes-agent`.
3. Put a `// FORK: kyroskoh/hermes-agent` comment at the top of any fork file.
4. Never edit upstream-owned files for fork logic. If you need to mount
   something upstream, extend `web/src/fork/registry.ts` instead.
5. For translations: extend `web/src/fork/i18n/en.ts` and the stub map. Do NOT
   add keys to the upstream `Translations` interface.
6. Run `/root/.hermes/scripts/hermes-fork-sync.sh` after every pull.

## Why this design

- **Zero conflict on every PR** as long as upstream doesn't add another
  `import "@/fork/registry"` line (probability ≈ 0).
- **One-line rebase** for almost all upstream updates.
- **Pluggable**: when the upstream plugin system stabilises further, swap the
  registry mount-point for a real plugin manifest with zero fork surface
  changes.
- **Visible**: a single `git grep "FORK:"` lists every fork-owned file, so you
  can audit the fork surface at any time.

## Fork-owned file index (audit this with `git grep "FORK: kyroskoh"`)

| Path | Purpose |
|------|---------|
| `web/src/fork/README.md` | This file |
| `web/src/fork/index.ts` | Single mount point (routes + nav) imported by App.tsx |
| `web/src/fork/pages/BackupsPage.tsx` | Backups landing page |
| `web/src/fork/pages/FallbackPage.tsx` | Fallback provider chain editor |
| `web/src/fork/i18n/en.ts` | Canonical English strings for fork features |
| `web/src/fork/i18n/stubs.ts` | Empty-string stubs for 17 locales |
| `web/src/fork/i18n/useForkI18n.ts` | useForkI18n() hook with English fallback |

## Fork additions to upstream-owned files

These are minimal, additive edits to upstream files. Each carries a
`// FORK: kyroskoh/hermes-agent` comment so a `git grep` lists them all:

| File | What changed | Rebase cost |
|------|--------------|-------------|
| `web/src/App.tsx` | Two `import { FORK_ROUTES, FORK_NAV } from "@/fork"` references + a `for...FORK_ROUTES` loop and a `...FORK_NAV.map(...)` spread. ~10 lines total. | Very low — almost all upstream App.tsx edits are below or above this block. |
| `web/src/lib/api.ts` | Added `/api/model/fallback` and `/api/model/fallback/status` to `PROFILE_SCOPED_PREFIXES`; 7 new methods (`getFallbackChain`, `getFallbackStatus`, `setFallbackChain`, `appendFallbackEntry`, `removeFallbackEntry`, `clearFallbackChain`, `reorderFallbackChain`); 7 new types (`FallbackEntryPayload`, `FallbackChainTriggers`, `FallbackChainResponse`, `FallbackChainMutationResponse`, `FallbackProviderStatus`, `FallbackChainStatusCache`, `FallbackChainStatusResponse`). | Low — additions are sandwiched between other model-related entries; merge conflicts only if upstream renames the model section. |
| `hermes_cli/web_server.py` | New `FallbackEntry` / `FallbackChainUpdate` Pydantic models + 7 new endpoints under `/api/model/fallback/` + smart-skip hook installer + `get_fallback_chain_endpoint` annotates chain with skip_reason. ~440 lines added across two blocks. | Low — placed between `/api/model/set` and `/api/config`; conflict only if upstream refactors the model section. |
| `hermes_cli/config_defaults.py` | `smart_fallback` block with `enabled`, `cache_path`, `stale_after_ttl_multiplier`, `primary_preempt_after_n_429s` keys (~13 lines). | Very low — added right after `fallback_providers` so any rebase touching that key is mechanical. |

## When upstream updates touch the fork surface

The fork follows the **Option A** linear-history workflow. Rebase playbook:

```bash
cd /usr/local/lib/hermes-agent
git fetch origin

# 1. Look at what upstream changed in the risk surface
git log --oneline origin/main --since='2 weeks ago' -- \
  web/src/App.tsx web/src/lib/api.ts hermes_cli/web_server.py

# 2. Dry-run merge to see if conflicts will land in our edits
git merge-tree $(git merge-base HEAD origin/main) HEAD origin/main | \
  grep -E '^(changed|added|removed) in both' \
  && echo "⚠️ conflicts in upstream files we touched" \
  || echo "✓ clean merge"

# 3. Rebase
git rebase origin/main

# 4. Resolve any conflicts (mechanical: keep BOTH sides of the FORK block
#    and the upstream changes; the fork block is wrapped in
#    // FORK: kyroskoh/hermes-agent comments so it's easy to spot)

# 5. Verify
cd web && npm run typecheck && npm run build
python3 -m py_compile /usr/local/lib/hermes-agent/hermes_cli/web_server.py

# 6. Force-push
cd /usr/local/lib/hermes-agent
git push fork HEAD:main
```

The shared scripts that automate steps 1–3, 5, and 6:
- `/root/.hermes/scripts/hermes-fork-sync.sh` — weekly automated sync
- `/root/.hermes/scripts/hermes-fork-rebase.sh` — interactive rebase helper

## Fork features currently shipped

| Feature | Page | API | CLI | Notes |
|---------|------|-----|-----|-------|
| Backups landing page | `/backups` | (proxy to honcho-local) | — | Surfaces the Honcho Local backup dashboard + cron schedule. |
| Fallback provider chain | `/fallback` | `/api/model/fallback/*` | `hermes fallback add\|remove\|list\|clear` | Manage the `fallback_providers:` chain from the dashboard. Triggers auto from 429/5xx; auto-resets to primary next turn. |
| Smart-fallback (Layers 1-3) | `/fallback` (badges + cache card) | `GET /api/model/fallback/status` | `hermes-fallback-status.py` | Skips fallback entries that the cache says are unavailable (Nous out of credits, Codex in cooldown) BEFORE burning a network round-trip. Runtime hook in `agent.chat_completion_helpers._fallback_entry_unavailable_without_network`; cache written by cron every 5 minutes. |
| Model Orchestrator | `/fallback` (banner) | `/api/model/orchestrator/*` | `auto_promote_orchestrator.py` | Layer 4 of smart-fallback. When the target primary hits rate limits (429), credit exhaustion, or auth errors, auto-promotes the top healthy fallback into `~/.hermes/config.yaml` so `/new`, `/reset`, and fresh sessions start with the healthy model. Self-healing: when the target primary recovers, automatically restores it. Toggle via `POST /api/model/orchestrator/toggle`. |
| Repeating-fallback notice dedup | n/a (gateway) | n/a | n/a | Suppresses repeat `🔄 Switched to fallback model: …` notices when the agent is already running on the same fallback as the previous turn. Tracks `last_fallback_notice` in `ConversationState`. Cleared on `/new` / `/reset`. |

## Model Orchestrator architecture

Layer 4 sits on top of the smart-fallback cache (Layers 1-3). It runs on
the same 5-minute cron as `hermes-fallback-status.py` and converts a
session-scoped runtime fallback into a durable config-level primary so
fresh sessions inherit the healthy model.

```
┌────────────────────────────────────────────────────────────────────────┐
│  /etc/cron.d (every 5 min) → hermes-fallback-status.py                 │
│                                                                        │
│  1. Probe providers: _check_minimax / _check_nous / _check_codex       │
│     write ~/.hermes/cache/fallback_status.json (the smart-fallback cache)│
│                                                                        │
│  2. Invoke auto_promote_orchestrator.evaluate_and_orchestrate():       │
│     • If target primary (minimax-oauth / MiniMax-M3) is "available"    │
│       AND current config primary differs from target → RESTORE target  │
│     • If target primary is "no_credits" / "cooldown" / "token_missing"│
│       → PROMOTE first healthy fallback into ~/.hermes/config.yaml     │
│     • State persisted at ~/.hermes/cache/orchestrator_state.json       │
│                                                                        │
│  3. /new, /reset, fresh sessions read the updated config.yaml and      │
│     start with the promoted model automatically.                       │
└────────────────────────────────────────────────────────────────────────┘
```

REST surface (mounted under `/api/model/orchestrator/` in our fork's
`hermes_cli/web_server.py`):
- `GET /api/model/orchestrator` — live state (target, current, history).
- `POST /api/model/orchestrator/toggle` — enable/disable auto-promotion.
- `POST /api/model/orchestrator/target` — set the desired target primary.
- `POST /api/model/orchestrator/evaluate` — trigger an immediate eval.

The dashboard `/fallback` page surfaces an "Orchestrator Active" banner
alongside the primary model badge so operators can see what the
orchestrator considers healthy without leaving the fallback editor.

## Repeating fallback notice dedup

Previously, every turn that re-emitted the same fallback chain produced a
new `🔄 Switched to fallback model: …` status line. The dedup layer
records the most recent emitted notice on `ConversationState.last_fallback_notice`
and short-circuits identical repeats. Notice tracking is reset on every
conversation boundary (`/new`, `/reset`, auto-reset) so a fresh
conversation can re-announce a switch if the new session boots on a
fallback.

Files touched:
- `gateway/run.py` — dedup check in `_status_callback_sync` for messages
  containing `Switched to fallback model`.
- `gateway/session_state.py` — `last_fallback_notice` field on
  `ConversationState`, cleared by `clear()`.
- `gateway/run.py` — added `_last_fallback_notice` to
  `_CONVERSATION_SCOPED_STATE` so the conversation-boundary funnel wipes it.

## Smart-fallback architecture

Three layers, all opt-out via `smart_fallback.enabled: false` in config:

1. **Runtime smart-skip** — `/root/.hermes/scripts/fallback_smart_skip.py` wraps
   `_fallback_entry_unavailable_without_network` at web_server startup.
   When the fallback walker is about to try an entry, it consults the
   cache (`~/.hermes/cache/fallback_status.json`) and returns a skip reason
   if the entry's provider is currently unavailable. The wrapper preserves
   the upstream Nous-token-missing check and adds no_credits / cooldown
   skip reasons on top. Same `agent._unavailable_fallback_keys` memoization
   so a skipped entry stays skipped for the rest of the session.

2. **Status surface** — `GET /api/model/fallback/status` returns the full
   cache snapshot plus per-entry `skip_reason` annotations. The
   FallbackPage renders these as badges next to each chain entry and a
   dedicated cache card showing each provider's state (Nous credits,
   Codex credential cooldowns, etc.).

3. **Proactive cron** — `/etc/cron.d/hermes-fallback-status` runs
   `hermes-fallback-status.py` every 5 minutes (matching the cache TTL).
   Polls Nous Portal via the existing `get_nous_portal_account_info()`
   accessor and Codex credential-pool cooldown timestamps via the
   existing `read_credential_pool()` accessor. No new HTTP surface — the
   poller reuses upstream helpers.

Adding a new provider to the smart-skip list is a 3-line change in
`hermes-fallback-status.py:_check_<provider>` plus a new entry in
`_COOLDOWN_STATES` or `_NO_CREDITS_STATES` in `fallback_smart_skip.py`.