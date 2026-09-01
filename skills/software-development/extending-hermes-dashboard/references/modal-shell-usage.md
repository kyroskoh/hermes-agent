# Modal shell usage

Every modal in the Hermes dashboard **must** follow this recipe.  Skip
any step and the modal will render behind the sidebar, leak clicks to
the page beneath, or look wrong on the Cyberpunk theme.

## Why `createPortal` is mandatory

The dashboard's main column has `class="relative z-2"`.  This creates a
**stacking context** that's higher than the rest of the page chrome but
*lower* than anything outside it.  A modal rendered inside that column
with `z-[100]` will sit above the column's contents but **still appear
behind** the sidebar, header banners, and status strips — and worse,
its backdrop click handler will fire when the user clicks the visible
"sidebar area" behind the panel.

The fix is to render the modal **outside** the column entirely:

```typescript
import { createPortal } from "react-dom";

return createPortal(
  <div className={DASHBOARD_MODAL_BACKDROP} role="dialog" aria-modal="true">
    <div className={DASHBOARD_MODAL_PANEL}>
      {/* ... */}
    </div>
  </div>,
  document.body,    // ← the key
);
```

`document.body` is outside the dashboard column, so the modal escapes
the stacking context.  `z-[100]` is now meaningful.

## The shell classes

Use the two classes exported from `web/src/lib/dashboard-modal-shell.ts`:

```typescript
import {
  DASHBOARD_MODAL_BACKDROP,
  DASHBOARD_MODAL_PANEL,
} from "@/lib/dashboard-modal-shell";

// DASHBOARD_MODAL_BACKDROP:
// "fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
//
// DASHBOARD_MODAL_PANEL:
// "relative w-full border border-border bg-card shadow-2xl"
```

`bg-background/85` on the backdrop dims the page.  `bg-card` (NOT
`bg-background-base/80` — that's the page card style) on the panel
ensures readability on Cyberpunk + mobile.  Without `bg-card`, the
underlying Models/Plugins pages show through the modal.

## Full recipe

Here's the structure used by `ConfigDiffModal.tsx` and `EnvDiffModal.tsx`:

```typescript
export function MyModal({ payload, onCancel, onConfirm }: MyModalProps) {
  // Bail early when closed — keeps the modal completely unmounted
  // so it doesn't trap focus or steal events when not in use.
  if (!payload) return null;

  // Escape key closes (unless an async op is in flight).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !applying) {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [applying, onCancel]);

  return createPortal(
    <div
      className={DASHBOARD_MODAL_BACKDROP}
      role="dialog"
      aria-modal="true"
      aria-label="My modal title"
      onMouseDown={(e) => {
        // Click-outside-to-close: only fire if the click landed on
        // the backdrop itself, not bubbled from inside the panel.
        if (e.target === e.currentTarget && !applying) onCancel();
      }}
    >
      <div
        className={cn(
          DASHBOARD_MODAL_PANEL,
          "w-full max-w-2xl flex flex-col max-h-[min(85vh,640px)]",
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* header / body / footer */}
      </div>
    </div>,
    document.body,
  );
}
```

## Things to remember

- **`if (!payload) return null;`** keeps the portal unmounted.  Don't
  render the modal conditionally *inside* the portal — that defeats
  the purpose.
- **Click-outside closes** via the `e.target === e.currentTarget`
  guard on the backdrop.  Always `stopPropagation()` on the panel so
  the panel's own clicks don't trigger close.
- **Escape closes** — but only when there's no async operation in
  flight.  Otherwise users can cancel mid-write and leave the server
  state inconsistent.
- **Body scroll lock** is not strictly needed for short-lived modals
  (under ~5 seconds).  If your modal can stay open indefinitely, add
  the same `document.body.style.overflow = "hidden"` pattern that
  `App.tsx` uses for the mobile sidebar.
- **Autofocus an input** with `requestAnimationFrame` AFTER the portal
  paints.  The shared `Input` component doesn't accept `ref` — use a
  plain `<input>` (see the SKILL.md "common pitfalls").
- **Type `createPortal`** from `react-dom`, not `react`.  Easy mistake.

## Nested pickers

If your modal hosts a nested picker (e.g. the Model picker inside the
MCP edit modal), the outer modal must yield Escape to the inner one.
The shell exports a tiny helper:

```typescript
import { shouldCloseOuterModalOnEscape } from "@/lib/dashboard-modal-shell";

useEffect(() => {
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && shouldCloseOuterModalOnEscape(nestedOpen)) {
      e.preventDefault();
      onCancel();
    }
  };
  // ...
}, [nestedOpen, onCancel]);
```

`shouldCloseOuterModalOnEscape(nestedOpen)` returns `!nestedOpen` — i.e.
"don't close me while a child picker is open".

## Why this matters

A modal that doesn't use this recipe:

1. Renders behind the sidebar → users can't see the buttons
2. Doesn't close on outside click → users think it's stuck
3. Doesn't close on Escape → power users get frustrated
4. Doesn't block page clicks → user accidentally mutates state behind it

Use the recipe. Every time.