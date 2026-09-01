---
name: extending-hermes-dashboard
description: Add features to Hermes's web dashboard SPA under web/src/.
version: 1.0.0
author: kyroskoh-bot
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes, dashboard, react, vite, typescript, i18n, frontend, spa]
    related_skills: [hermes-agent-skill-authoring, requesting-code-review, test-driven-development]
---

# Extending the Hermes Web Dashboard

The Hermes Web Dashboard is a React 19 + Vite SPA in `web/src/`.  It
ships as static assets under `hermes_cli/web_dist/` and is served by
`hermes dashboard` on port 9119.  This skill captures the load-bearing
conventions you need to follow so a change actually works end-to-end.

## Core behavior

When you're asked to add or modify anything in the dashboard:

1. **Read the routing surface first.** Every page is `lazy()`-loaded in
   `web/src/App.tsx` and registered in `BUILTIN_ROUTES_CORE`.  To add a
   new page, you must (a) add a `lazy(() => import("@/pages/MyPage"))`,
   (b) register it in `BUILTIN_ROUTES_CORE`, and (c) add a `NavItem` to
   `BUILTIN_NAV_REST`.  Missing any of these three breaks the route or
   hides it from the sidebar.

2. **Use the i18n cascade.** Every user-facing string lives in
   `web/src/i18n/types.ts` as a required key on `Translations`, with
   translations in 17 locale files.  You cannot add a string in only
   `en.ts` — the type system will reject it.  See
   `references/i18n-cascade-script.md` for the exact one-liner pattern.

3. **Modals escape the dashboard column via `createPortal`.** The
   dashboard uses a `relative z-2` stacking context on its main column;
   `z-[100]` alone cannot escape it.  Every modal **must**
   `createPortal(..., document.body)` and use the shared classes from
   `web/src/lib/dashboard-modal-shell.ts`.  See
   `references/modal-shell-usage.md`.

4. **The shared `Input` component does NOT forward refs.** It is a plain
   functional component that doesn't `forwardRef`.  Either use a plain
   `<input>` element with the same Tailwind classes, or wrap your own
   `forwardRef`.  Don't waste time on this — it's the #1 mistake when
   adding modal inputs.

5. **Button uses `outlined` / `ghost` / `destructive`, not `variant`.**
   The API is class-variance-authority style booleans.  See the Button
   source under `node_modules/@nous-research/ui/ui/components/button.tsx`
   if unsure.

6. **The command palette is the canonical home for new index kinds.**
   `web/src/components/CommandPalette.tsx` accepts `manifests` and builds
   an index of nav + plugins + config + cron + skills + sessions.  See
   `references/command-palette-extending.md` to add a new kind.

## Output requirements

Every dashboard change should land with:

- **The component file(s)** under `web/src/components/` or `web/src/pages/`
- **i18n keys** in `types.ts` + English strings in `en.ts` + empty stubs
  in the other 16 locales (script in `references/i18n-cascade-script.md`)
- **A test file** next to any pure-logic helper (the web package uses
  vitest; the env already exposes it via `npm test`)
- **Updated bundle** under `hermes_cli/web_dist/` — run
  `npm run build` and let Vite regenerate it
- **Dashboard restart** via `systemctl restart hermes-dashboard.service`
  (the bundle is loaded fresh on each request, but the dashboard process
  caches the manifest of built assets)

## Verification

Always end with the full gate:

```bash
cd web
npm run typecheck    # tsc -p . --noEmit
npm test             # vitest run
npm run lint         # eslint
npm run build        # tsc -b && vite build
```

Then verify the deployed bundle:

```bash
systemctl restart hermes-dashboard.service
curl -sSL http://127.0.0.1:9119/ -o /dev/null -w '%{http_code}\n'
# expect: 200
```

And finally do a quick secret scan on anything you added:

```bash
# anything with = or : followed by a 20+ char alnum blob?
grep -rE '(api[_-]?key|secret|password|token).{0,3}[=:].{0,3}[A-Za-z0-9]{20,}' \
  web/src/components web/src/pages web/src/lib 2>/dev/null
```

## Common pitfalls

- **Don't add i18n keys to `en.ts` only.** The build will fail with
  "Property X does not exist on Translations" for every other locale.
  Run the cascade script first.
- **Don't `ref={...}` the shared `Input`.** It silently no-ops.  Use
  a plain `<input>`.
- **Don't put `z-[100]` on a normal-rendered modal.** It can't escape
  the dashboard column.  `createPortal` is mandatory.
- **Don't forget to restart the dashboard service.** `hermes-dashboard.sh`
  uses `systemd` which holds the process across requests but caches the
  initial bundle manifest. New chunks pick up automatically after a
  restart.

## References

- `references/i18n-cascade-script.md` — the script that copies a new
  i18n block across all 17 locale files
- `references/modal-shell-usage.md` — the full
  `createPortal` + `DASHBOARD_MODAL_*` recipe
- `references/command-palette-extending.md` — adding a new index kind
  to the global `⌘K` launcher