# i18n cascade for the Hermes dashboard

The dashboard's `Translations` interface in `web/src/i18n/types.ts` is the
**type contract** that every locale file must satisfy.  Adding a string
to `en.ts` alone causes a build failure (`Property X does not exist on
Translations` for every missing locale).  This reference shows the
exact script to add a new block across all 17 locales in one go.

## The shape

```typescript
// web/src/i18n/types.ts
export interface Translations {
  common: { ... };
  app: { ... };
  // ... existing top-level blocks
  palette: {           // ← new block you're adding
    title: string;
    placeholder: string;
    // ...
  };
}
```

Every locale (`en`, `zh`, `zh-hant`, `ja`, `de`, `es`, `fr`, `tr`, `uk`,
`af`, `ko`, `it`, `ga`, `pt`, `ru`, `hu`, `ar`) must declare the same
top-level keys.

## Adding a new block — step by step

### 1. Declare the keys in `types.ts`

Add the new block between existing siblings (alphabetical order is
conventional — `palette` sits between `oauth`/`language`/`theme`/
`achievements` and `kanban` in the current file).  Every key must be
`string` (the contract is "every locale translates to a string",
including `""` for now).

### 2. Translate in `en.ts`

Write the canonical English strings.  Use `{name}` / `{n}` / `{s}`
placeholders for any substitutions — the components use these via
`String.prototype.replace`.

### 3. Cascade to the other 16 locales

Run this Python script from the repo root.  It clones the new block's
shape into every locale that doesn't already have it, leaving the
translations as empty strings (the components use `?? "English fallback"`
for every key, so empty strings degrade gracefully).

```python
"""
Cascade a new i18n block from en.ts into all non-English locale files.
Pass the canonical block (with English strings OR empty stubs) and the
script inserts it before the existing "kanban:" line.

Usage:
    python3 scripts/cascade_i18n.py palette <<'EOF'
      palette: {
        title: "Command palette",
        placeholder: "Type a command, search pages, keys, cron…",
        // ...
      },
    EOF
"""

import os, re, sys

i18n_dir = "web/src/i18n"

# Read the block from stdin.
new_block = sys.stdin.read().rstrip()

# Insert before "  kanban: {" in every locale that doesn't have the block.
files = sorted(f for f in os.listdir(i18n_dir)
                if f.endswith(".ts") and f not in
                ("types.ts", "en.ts", "context.tsx", "index.ts", "define-locale.ts"))

updated = 0
for fname in files:
    path = os.path.join(i18n_dir, fname)
    with open(path) as f:
        src = f.read()
    # Detect "first key of the new block" — assume it's `<name>:` where
    # <name> is the first line of the block (e.g. "palette:").
    first_key = new_block.split(":", 1)[0].strip()
    if f"{first_key}:" in src:
        continue  # already present
    new_src = re.sub(
        r"(  kanban: \{)",
        new_block + r",\n\n\1",
        src,
        count=1,
    )
    if new_src == src:
        print(f"  {fname}: NO MATCH (kanban block not found)")
        continue
    with open(path, "w") as f:
        f.write(new_src)
    updated += 1

print(f"Updated {updated} locale files.")
```

### 4. Verify

```bash
cd web
npm run typecheck   # catches any remaining gaps
```

If you forgot a locale, the error names it explicitly:

```
src/i18n/af.ts(629,5): error TS2353: Object literal may only specify
known properties, and 'palette' does not exist in type '{...}'
```

Run the script again with the missing block.

## Common gotchas

- **The cascade only adds what's missing.** Re-running the script is
  idempotent — already-present keys are skipped.
- **Empty-string translations render as English via `??`.** Every
  component reads i18n like `t.palette.title ?? "English fallback"`,
  so an empty string in `af.ts` falls through to `en.ts` at runtime.
  Users see the English string with no visible breakage.
- **Watch the trailing comma.** The block must end with `,` (a comma,
  not nothing) so it joins the sibling chain: `palette: { … },\n\n  kanban: { … }`.
- **The script targets `kanban:` as the insertion anchor.** If you add
  a new block *after* `kanban` alphabetically, change the regex in the
  script to match the next sibling.

## A faster alternative: the inline approach

For very small additions (one or two keys), you can skip the script and
manually edit each locale file.  The format is mechanical:

```typescript
// web/src/i18n/af.ts — inside the `palette:` block
palette: {
    title: "Opdragpalette",      // ← translated
    placeholder: "",            // ← empty (falls back to en)
    // ...
},
```

But for any non-trivial block (10+ keys), the script is faster and
less error-prone.

## Related

- `references/command-palette-extending.md` — example of a feature that
  needed the cascade (the palette block is 30+ keys across 17 locales)
- `references/modal-shell-usage.md` — companion patterns for adding
  modals with i18n strings