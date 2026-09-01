# Dashboard: command palette + diff-preview modals

> Hermes Web Dashboard (`hermes-dashboard.service`, port 9119)

Two new quality-of-life features for the operator/admin dashboard SPA under
`web/src/`:

## 1. Command palette (`⌘K` / `Ctrl-K`)

A global launcher mounted once at the root of `App.tsx` via
`createPortal(..., document.body)` with `z-[100]`.  Searches across:

- Built-in navigation pages (Sessions, Models, Logs, Cron, Skills, Config, …)
- Plugin pages from `usePlugins()`
- Config keys (from `/api/config/schema` + `/api/config` — current value shown)
- Cron jobs (from `/api/cron/jobs`) — `Enter` picks the most useful
  sub-action automatically (Run if scheduled, Resume if paused)
- Skills (from `/api/skills`)
- Recent sessions (from `/api/sessions`)
- Quick actions (Open config / env / logs / cron / skills)

Keyboard nav: `↑/↓/Home/End/Enter/Esc`.  Matching uses the existing
`fuzzyRank` scorer in `web/src/lib/fuzzy.ts` — no new dependency.  Cron,
skills, and sessions are lazy-fetched on first open and cached for the
session.

## 2. Diff preview before save (config + env)

Both the **Config page** and the **Env page** now require an explicit
review step before any write to disk.

### ConfigPage
- Form mode: per-key change list, grouped by category, color-coded
  red→green with schema-aware labels.
- YAML mode: line-level Myers diff with `git diff -U3`-style hunks and
  line-number gutters.
- Always requires typing `SAVE` to confirm (per chosen UX spec).
- Destructive categories (`security`, `terminal`, `tool_loop_guardrails`,
  `tool_output`, `logging`) trigger a warning banner.

### EnvPage
- Row Save / Clear buttons now stage the change instead of writing
  immediately.
- A sticky bottom banner shows: *"N pending changes (X set, Y clear)"*
  with **Discard all** and **Review & apply** buttons.
- The diff modal lists every pending key with masked before/after,
  per-row reveal (using existing `/api/env/reveal`), per-row discard,
  and a typed `APPLY` confirmation.
- After apply, the env state is re-fetched from the server so the local
  cache reflects the new `redacted_value`.

## Algorithm: Myers diff

`web/src/lib/diff.ts` is a self-contained, ~220-line line-level Myers
implementation with no new dependencies.  Carries 16 vitest cases
covering pure additions, pure deletions, single replacements, multi-op
mixes, hunk grouping, and edge cases (empty inputs, identical lines).

The `diffLines` algorithm uses `Record<number, …>` keyed by diagonal `k`
plus a separate `direction` trace — a common compact formulation that's
both fast (`O((N+M)·D)`) and small enough to inline into the bundle.

## Files

```
web/src/components/CommandPalette.tsx     (new)
web/src/components/ConfigDiffModal.tsx    (new)
web/src/components/EnvDiffModal.tsx       (new)
web/src/lib/diff.ts                        (new, Myers algo)
web/src/lib/diff.test.ts                   (new, 16 vitest cases)
web/src/pages/ConfigPage.tsx              (wired in diff modal)
web/src/pages/EnvPage.tsx                 (staged-batch flow + diff modal)
web/src/App.tsx                           (mount CommandPalette)
web/src/i18n/types.ts                     (palette + configDiff + envDiff blocks)
web/src/i18n/en.ts                        (English strings)
web/src/i18n/{af,ar,de,es,fr,ga,hu,it,ja,ko,pt,ru,tr,uk,zh-hant,zh}.ts
                                         (empty stubs — fall back to English)
docs/changelog/dashboard-palette-diff-modal.md
skills/software-development/extending-hermes-dashboard/
  SKILL.md
  references/i18n-cascade-script.md
  references/modal-shell-usage.md
  references/command-palette-extending.md
```

## Verification

- typecheck: clean (`tsc -p .`)
- tests: **295/295** pass (16 new for `diff.ts`, all others pre-existing)
- lint: 0 errors
- build: 680 ms, bundles regenerated under `hermes_cli/web_dist/`
- smoke: dashboard restarts cleanly, returns HTTP 200 on `/`, no errors
  in `journalctl -u hermes-dashboard.service`
- secret scan: clean (no tokens, keys, or passwords in any new file)

## Risks / migration notes

- All 16 non-English locales received empty i18n stubs (`""`) for the new
  `palette`, `configDiff`, and `envDiff` blocks. The components use `??`
  fallbacks so English is shown automatically. Translators can fill these
  in over time; no runtime breakage.
- `web/src/lib/api.ts` shows 10 lines added but those are unrelated
  upstream work that was already in the working tree when this feature
  landed; **not included in this commit**.
- Dashboard is exposed on `0.0.0.0:9119` by design (intentionally not
  `127.0.0.1`-bound per operator request). Consider adding CSRF + auth
  in a follow-up — see feature idea #1 from the dashboard backlog.