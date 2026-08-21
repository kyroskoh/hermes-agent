# Extending the command palette

The command palette (`web/src/components/CommandPalette.tsx`) is the
canonical way to expose new functionality to power users.  This
reference walks through adding a new index kind, taking "Models" as
the worked example.

## Architecture

The palette is one component, mounted once at the root in `App.tsx`:

```typescript
import { CommandPalette } from "@/components/CommandPalette";

export default function App() {
  // ...
  return (
    // ...
    <>
      <PluginSlot name="overlay" />
      <CommandPalette manifests={manifests} />
    </>
  );
}
```

It accepts `manifests` (the result of `usePlugins()`) so it can show
plugin nav items.  Everything else it indexes is fetched lazily on
first open.

The palette builds an `allItems: PaletteItem[]` array, ranks it with
`fuzzyRank`, then groups by `group` for display.

## The data shape

```typescript
type PaletteKind = "nav" | "plugin" | "config" | "cron" | "skill" | "session" | "action";

interface PaletteItemBase {
  id: string;             // stable, used as React key + ranking key
  title: string;          // primary display text (fuzzy-matched)
  subtitle?: string;      // secondary text (also matched)
  group: string;          // section header
  icon: ComponentType;    // left-aligned icon
  kind: PaletteKind;
}
```

Each kind extends `PaletteItemBase` with kind-specific fields:

```typescript
interface ConfigItem extends PaletteItemBase {
  kind: "config";
  key: string;            // dotted config key, e.g. "model.provider"
  currentValue?: unknown;
}

interface CronItem extends PaletteItemBase {
  kind: "cron";
  cronId: string;
  profile?: string | null;
  status: "running" | "paused" | "scheduled" | "completed";
}
```

## Adding a new kind (Models worked example)

### Step 1: extend the union

```typescript
type PaletteKind = ... | "model";
interface ModelItem extends PaletteItemBase {
  kind: "model";
  modelId: string;
  provider?: string;
}
```

### Step 2: add state for the remote data

```typescript
const [models, setModels] = useState<ModelInfo[] | null>(null);
```

### Step 3: lazy-fetch on open

Find the existing `useEffect` that lazy-loads cron/skills/sessions and
add the new fetch:

```typescript
useEffect(() => {
  if (!open) return;
  if (cronJobs && skills && sessions && models) return;
  setLoadingRemote(true);
  Promise.allSettled([
    // ... existing fetches
    models ? Promise.resolve(null) : api.getModels(),
  ])
    .then((results) => {
      // ... existing branches
      const modelsRes = results[results.length - 1];
      if (modelsRes.status === "fulfilled" && modelsRes.value) {
        setModels(modelsRes.value);
      }
    })
    .finally(() => setLoadingRemote(false));
}, [open, cronJobs, skills, sessions, models]);
```

### Step 4: build the items array

```typescript
const modelItems = useMemo<ModelItem[]>(
  () => (models ?? []).map((m) => ({
    id: `model:${m.id}`,
    kind: "model",
    modelId: m.id,
    provider: m.provider,
    title: m.display_name ?? m.id,
    subtitle: m.provider,
    group: t.palette.groupModels ?? "Models",
    icon: Cpu,
  })),
  [models, t],
);
```

### Step 5: include in the assembled list

```typescript
const allItems = useMemo(() => [
  ...actionItems,
  ...navItems,
  ...modelItems,    // ← here
  ...configItems,
  // ...
], [/* ... */]);
```

### Step 6: handle the action

In `runItem`, add a case to the `switch (item.kind)`:

```typescript
case "model":
  navigate(`/models?focus=${encodeURIComponent(item.modelId)}`);
  break;
```

### Step 7: optional right-aligned badge

If the kind has useful metadata, render it via `PaletteItemBadge`:

```typescript
function PaletteItemBadge({ item }: { item: PaletteItem }) {
  // existing branches...
  if (item.kind === "model") {
    return <Badge tone="outline" className="text-[10px]">{item.provider}</Badge>;
  }
  return null;
}
```

### Step 8: i18n

Add the group label to `web/src/i18n/types.ts` + `en.ts`, then cascade
to the other 16 locales (see `i18n-cascade-script.md`):

```typescript
// types.ts
palette: {
  // ...
  groupModels: string;
}
```

## What NOT to do

- **Don't add a new prop for the new kind.** The palette accepts
  `manifests` and lazy-fetches everything else internally.  Adding a
  prop would couple the palette to every feature page.
- **Don't render the items inline in the JSX.** Build them in
  `useMemo` so the fuzzy ranking is stable across renders.
- **Don't fetch eagerly.** The palette is mounted but invisible 99%
  of the time. Lazy-on-first-open keeps initial paint fast.
- **Don't add a "Settings" action that opens a sub-menu.** The palette
  is single-keypress only. Two-stage picks break muscle memory.
- **Don't store fetched data in a context.** Per-session cache is fine;
  components remount on hot-reload and lose cache anyway.

## Performance budget

For each new kind, aim for:

- **<100ms fetch on first open** — measure on a real Hermes install
  with 100+ models / 50+ skills. If slower, cache or paginate.
- **<500 items total in the index** — fuzzy ranking on more than that
  starts to feel laggy on a mid-range laptop. Truncate with a sensible
  cap and show "(showing top N)" if you hit it.
- **Stable IDs** — `id: \`${kind}:${uniqueId}\`` so React's key reuse
  doesn't flicker when items reorder.

## Testing

There are no tests for the palette itself (it's a presentational
component with no public surface).  The two helpers it depends on —
`fuzzyRank` in `web/src/lib/fuzzy.ts` and the API client in
`web/src/lib/api.ts` — both have vitest suites.  When adding a new
kind, smoke-test manually:

1. Open the dashboard, press `⌘K`
2. Type a unique fragment of your new item's title
3. Verify it appears, ranked correctly
4. Press `Enter` and verify the action fires
5. Press `Esc` and verify the modal closes without firing anything

## Related

- The actual data sources the palette uses (`/api/cron/jobs`,
  `/api/skills`, `/api/sessions`) are defined in `web/src/lib/api.ts`
  and `apps/server/...` — check the API client before assuming a
  field shape
- `web/src/i18n/types.ts` `palette:` block — the canonical list of
  group / action label keys