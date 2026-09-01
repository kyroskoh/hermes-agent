/**
 * ConfigDiffModal — review-before-save gate for config changes.
 *
 * Replaces the immediate "Save" call with a two-step flow:
 *   1. Show every changed key, with before → after values, color-coded.
 *   2. Require the user to type SAVE to confirm (per spec — always-on,
 *      not just for security-touching changes; this is the "always" mode
 *      you picked at planning time).
 *
 * Supports two source modes:
 *   • "form"  — diff is computed by walking the nested config object and
 *               comparing the loaded snapshot against the live state.
 *   • "yaml"  — diff is computed line-by-line via the Myers algorithm in
 *               @/lib/diff.
 *
 * The modal renders through `createPortal(..., document.body)` to escape
 * the dashboard's stacking context (see SidebarTooltip for the same
 * pattern).  Uses `DASHBOARD_MODAL_BACKDROP` / `DASHBOARD_MODAL_PANEL`
 * from `dashboard-modal-shell.ts` for visual consistency.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Plus, Minus, RefreshCw, AlertTriangle, Trash2 } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { cn } from "@/lib/utils";
import { getNestedValue } from "@/lib/nested";
import {
  diffLines,
  splitLines,
  groupIntoHunks,
  type DiffRow,
} from "@/lib/diff";
import {
  DASHBOARD_MODAL_BACKDROP,
  DASHBOARD_MODAL_PANEL,
} from "@/lib/dashboard-modal-shell";
import { useI18n } from "@/i18n";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface ConfigChange {
  /** Dotted config key, e.g. "model.provider" */
  key: string;
  /** Schema category for grouping ("security", "model", …) */
  category: string;
  /** Optional human-readable label */
  label?: string;
  /** Previous value (undefined means the key didn't exist) */
  before: unknown;
  /** New value (undefined means the key was deleted) */
  after: unknown;
}

export interface ConfigYamlDiff {
  mode: "yaml";
  /** YAML text currently on disk (before) */
  beforeYaml: string;
  /** YAML text the user has edited (after) */
  afterYaml: string;
  /** Path to config.yaml — surfaced in the modal header */
  path?: string | null;
}

export interface ConfigFormDiff {
  mode: "form";
  /** Schema fields (used to recover labels + categories) */
  schema: Record<string, Record<string, unknown>>;
  /** All detected changes */
  changes: ConfigChange[];
  /** Path to config.yaml — surfaced in the modal header */
  path?: string | null;
}

export type ConfigDiffPayload = ConfigYamlDiff | ConfigFormDiff;

interface ConfigDiffModalProps {
  payload: ConfigDiffPayload | null;
  onCancel: () => void;
  onConfirm: () => Promise<void> | void;
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const DESTRUCTIVE_CATEGORIES = new Set([
  "security",
  "terminal",
  "tool_loop_guardrails",
  "tool_output",
  "logging",
]);

function serializeValue(v: unknown): string {
  if (v === undefined) return "∅";
  if (v === null) return "null";
  if (typeof v === "string") return v === "" ? '""' : v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function ConfigDiffModal({
  payload,
  onCancel,
  onConfirm,
}: ConfigDiffModalProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [confirmText, setConfirmText] = useState("");
  const [applying, setApplying] = useState(false);
  const confirmInputRef = useRef<HTMLInputElement>(null);

  // Reset the typed-confirm field every time we open, and autofocus.
  useEffect(() => {
    if (!payload) return;
    setConfirmText("");
    setApplying(false);
    const id = window.requestAnimationFrame(() => confirmInputRef.current?.focus());
    return () => window.cancelAnimationFrame(id);
  }, [payload]);

  // Escape closes (unless an apply is in flight).
  useEffect(() => {
    if (!payload) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !applying) {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [payload, applying, onCancel]);

  const confirmWord = t.configDiff?.confirmWord ?? "SAVE";
  const confirmRequired = confirmText.trim() !== confirmWord;

  // For YAML mode, compute the diff rows once.
  const yamlRows = useMemo<DiffRow[]>(() => {
    if (!payload || payload.mode !== "yaml") return [];
    return diffLines(splitLines(payload.beforeYaml), splitLines(payload.afterYaml));
  }, [payload]);

  const yamlHunks = useMemo(() => groupIntoHunks(yamlRows), [yamlRows]);

  const yamlSummary = useMemo(() => {
    if (!payload || payload.mode !== "yaml") return { adds: 0, dels: 0 };
    return {
      adds: yamlRows.filter((r) => r.op === "add").length,
      dels: yamlRows.filter((r) => r.op === "del").length,
    };
  }, [payload, yamlRows]);

  // For form mode, group changes by category.
  const formGroups = useMemo(() => {
    if (!payload || payload.mode !== "form") return [];
    const map = new Map<string, ConfigChange[]>();
    for (const c of payload.changes) {
      if (!map.has(c.category)) map.set(c.category, []);
      map.get(c.category)!.push(c);
    }
    return [...map.entries()].map(([cat, changes]) => ({ cat, changes }));
  }, [payload]);

  const isDestructive = useMemo(() => {
    if (!payload) return false;
    if (payload.mode === "yaml") {
      // YAML mode: warn if any removed line mentions destructive categories.
      return yamlRows.some(
        (r) =>
          r.op === "del" &&
          [...DESTRUCTIVE_CATEGORIES].some((cat) =>
            r.text.toLowerCase().includes(cat.replace(/_/g, " ")),
          ),
      );
    }
    return payload.changes.some((c) => DESTRUCTIVE_CATEGORIES.has(c.category));
  }, [payload, yamlRows]);

  if (!payload) return null;

  const handleConfirm = async () => {
    if (confirmRequired) {
      showToast(
        t.configDiff?.typeSaveToConfirm ?? `Type ${confirmWord} to confirm`,
        "error",
      );
      return;
    }
    setApplying(true);
    try {
      await onConfirm();
    } catch (e) {
      showToast(`${t.configDiff?.saveFailed ?? "Save failed"}: ${e}`, "error");
      setApplying(false);
    }
  };

  const changeCount =
    payload.mode === "yaml"
      ? yamlSummary.adds + yamlSummary.dels
      : payload.changes.length;

  return createPortal(
    <div
      className={DASHBOARD_MODAL_BACKDROP}
      role="dialog"
      aria-modal="true"
      aria-label={t.configDiff?.title ?? "Review config changes"}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !applying) onCancel();
      }}
    >
      <div
        className={cn(
          DASHBOARD_MODAL_PANEL,
          "w-full max-w-3xl flex flex-col max-h-[min(85vh,720px)]",
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-border">
          <div className="min-w-0">
            <h2 className="text-base font-semibold flex items-center gap-2">
              <RefreshCw className="h-4 w-4 text-muted-foreground" />
              {t.configDiff?.title ?? "Review config changes"}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {payload.path && (
                <>
                  <code className="font-mono-ui">{payload.path}</code>
                  <span className="mx-2 text-text-tertiary">·</span>
                </>
              )}
              <span>
                {changeCount === 0
                  ? t.configDiff?.noChanges ?? "No changes"
                  : t.configDiff?.changeCount
                    ? t.configDiff.changeCount
                        .replace("{n}", String(changeCount))
                        .replace(
                          "{s}",
                          changeCount !== 1 ? "s" : "",
                        )
                  : `${changeCount} change${changeCount !== 1 ? "s" : ""}`}
              </span>
              {payload.mode === "yaml" && (
                <span className="ml-2">
                  <span className="text-green-600 dark:text-green-400">
                    +{yamlSummary.adds}
                  </span>{" "}
                  <span className="text-red-600 dark:text-red-400">
                    -{yamlSummary.dels}
                  </span>
                </span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Badge
              tone={isDestructive ? "destructive" : "secondary"}
              className="text-xs"
            >
              {isDestructive
                ? t.configDiff?.destructive ?? "Destructive"
                : t.configDiff?.safe ?? "Safe"}
            </Badge>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-4">
          {changeCount === 0 ? (
            <div className="text-center text-sm text-muted-foreground py-12">
              {t.configDiff?.nothingToSave ?? "Nothing to save — no changes detected."}
            </div>
          ) : payload.mode === "form" ? (
            <FormDiff groups={formGroups} />
          ) : (
            <YamlDiff hunks={yamlHunks} rows={yamlRows} />
          )}

          {isDestructive && (
            <div className="mt-4 flex items-start gap-2 px-3 py-2 bg-destructive/10 border border-destructive/30 text-sm">
              <AlertTriangle className="h-4 w-4 text-destructive shrink-0 mt-0.5" />
              <span>
                {t.configDiff?.destructiveHint ??
                  "This change touches security or terminal settings. Make sure you have a session open to revert it if something goes wrong."}
              </span>
            </div>
          )}
        </div>

        {/* Footer / confirm */}
        <div className="border-t border-border px-5 py-4 bg-muted/20">
          <div className="flex items-center gap-3 mb-3">
            <label className="text-xs font-semibold tracking-wide uppercase text-text-secondary">
              {t.configDiff?.typeLabel ?? `Type ${confirmWord} to confirm`}
            </label>
            <input
              ref={confirmInputRef}
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !confirmRequired && !applying) {
                  e.preventDefault();
                  void handleConfirm();
                }
              }}
              placeholder={confirmWord}
              className="w-32 font-mono-ui text-center text-sm tracking-[0.2em] bg-transparent border border-border px-2 py-1 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary"
              autoComplete="off"
              spellCheck={false}
              disabled={applying}
              aria-label={t.configDiff?.typeAriaLabel ?? "Type SAVE to confirm"}
            />
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button
              outlined
              onClick={onCancel}
              disabled={applying}
            >
              {t.common.cancel ?? "Cancel"}
            </Button>
            <Button
              onClick={() => void handleConfirm()}
              disabled={confirmRequired || applying}
              prefix={
                <RefreshCw className={applying ? "animate-spin" : undefined} />
              }
            >
              {applying
                ? t.common.saving ?? "Saving…"
                : t.configDiff?.apply ?? "Apply changes"}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ------------------------------------------------------------------ */
/*  Form-mode diff                                                     */
/* ------------------------------------------------------------------ */

function FormDiff({ groups }: { groups: Array<{ cat: string; changes: ConfigChange[] }> }) {
  const { t } = useI18n();
  return (
    <div className="grid gap-3">
      {groups.map((g) => {
        const catLabel = (t.config?.categories as Record<string, string> | undefined)?.[g.cat]
          ?? g.cat;
        return (
          <Card key={g.cat} className="overflow-hidden">
            <CardHeader className="px-4 py-2 bg-muted/30">
              <CardTitle className="text-xs uppercase tracking-wider text-text-secondary">
                {catLabel}
                <span className="ml-2 text-text-tertiary font-normal normal-case tracking-normal">
                  ({g.changes.length})
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {g.changes.map((c) => (
                <div
                  key={c.key}
                  className="grid grid-cols-[1fr_auto_1fr] gap-3 px-4 py-3 border-t border-border/50 first:border-t-0 items-start"
                >
                  <div className="min-w-0">
                    <div className="font-mono-ui text-xs text-text-secondary truncate" title={c.key}>
                      {c.key}
                    </div>
                    {c.label && (
                      <div className="text-xs text-text-tertiary mt-0.5 truncate">
                        {c.label}
                      </div>
                    )}
                  </div>
                  <div className="text-text-tertiary text-xs px-2 py-0.5 shrink-0">
                    →
                  </div>
                  <div className="min-w-0 grid gap-1.5">
                    <div className="font-mono-ui text-xs px-2 py-1 bg-red-500/10 border border-red-500/30 text-red-700 dark:text-red-300 break-words whitespace-pre-wrap">
                      {c.before === undefined ? (
                        <span className="italic text-text-tertiary">
                          (not set)
                        </span>
                      ) : (
                        truncate(serializeValue(c.before), 240)
                      )}
                    </div>
                    <div className="font-mono-ui text-xs px-2 py-1 bg-green-500/10 border border-green-500/30 text-green-700 dark:text-green-300 break-words whitespace-pre-wrap">
                      {c.after === undefined ? (
                        <span className="italic text-text-tertiary flex items-center gap-1">
                          <Trash2 className="h-3 w-3" />
                          (removed)
                        </span>
                      ) : c.before === undefined ? (
                        <span className="flex items-center gap-1">
                          <Plus className="h-3 w-3" />
                          {truncate(serializeValue(c.after), 240)}
                        </span>
                      ) : (
                        truncate(serializeValue(c.after), 240)
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  YAML-mode diff                                                     */
/* ------------------------------------------------------------------ */

function YamlDiff({
  hunks,
  rows,
}: {
  hunks: Array<{ header: { beforeStart: number; beforeCount: number; afterStart: number; afterCount: number }; rows: DiffRow[] }>;
  rows: DiffRow[];
}) {
  if (hunks.length === 0) return null;

  // Pad the line-number column to a fixed width so the gutter aligns.
  const maxLine = Math.max(
    ...rows.map((r) => r.beforeLine ?? 0),
    ...rows.map((r) => r.afterLine ?? 0),
  );
  const gutterWidth = String(maxLine).length;

  return (
    <div className="grid gap-3">
      {hunks.map((hunk, hi) => (
        <Card key={hi} className="overflow-hidden">
          <CardHeader className="px-4 py-2 bg-muted/30 flex flex-row items-center justify-between">
            <CardTitle className="text-xs font-mono-ui text-text-secondary">
              @@ -{hunk.header.beforeStart},{hunk.header.beforeCount}{" "}
              +{hunk.header.afterStart},{hunk.header.afterCount} @@
            </CardTitle>
            <span className="text-xs text-text-tertiary">
              {hunk.rows.filter((r) => r.op !== "same").length} change
              {hunk.rows.filter((r) => r.op !== "same").length !== 1 ? "s" : ""}
            </span>
          </CardHeader>
          <CardContent className="p-0 font-mono-ui text-xs overflow-x-auto">
            <table className="w-full">
              <tbody>
                {hunk.rows.map((row, i) => (
                  <tr
                    key={i}
                    className={cn(
                      "leading-relaxed",
                      row.op === "add" &&
                        "bg-green-500/10 text-green-700 dark:text-green-300",
                      row.op === "del" &&
                        "bg-red-500/10 text-red-700 dark:text-red-300",
                    )}
                  >
                    <td className="select-none text-text-tertiary px-2 py-0.5 text-right tabular-nums" style={{ minWidth: `${gutterWidth + 1}ch` }}>
                      {row.beforeLine ?? ""}
                    </td>
                    <td className="select-none text-text-tertiary px-2 py-0.5 text-right tabular-nums" style={{ minWidth: `${gutterWidth + 1}ch` }}>
                      {row.afterLine ?? ""}
                    </td>
                    <td className="px-2 py-0.5 w-4 select-none text-text-tertiary">
                      {row.op === "add" ? (
                        <Plus className="h-3 w-3" />
                      ) : row.op === "del" ? (
                        <Minus className="h-3 w-3" />
                      ) : (
                        ""
                      )}
                    </td>
                    <td className="px-2 py-0.5 whitespace-pre">
                      {row.text === "" ? "\u00A0" : row.text}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Pure helper: compute the form-mode change list                    */
/* ------------------------------------------------------------------ */

/**
 * Walk two config snapshots and return the per-key differences, joined
 * against the schema so we can surface human labels + categories.
 *
 * `isEqual` uses JSON.stringify — that's fine for the config payloads we
 * care about (they round-trip through YAML).  If perf ever becomes an
 * issue we can swap in a structural comparator, but config.yaml is small.
 */
export function computeFormDiff(
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
  schema: Record<string, Record<string, unknown>>,
): ConfigChange[] {
  if (!before || !after) return [];

  const changes: ConfigChange[] = [];
  const seenKeys = new Set<string>();

  // Edited or added keys.
  for (const [key, schemaEntry] of Object.entries(schema)) {
    const beforeVal = getNestedValue(before, key);
    const afterVal = getNestedValue(after, key);
    const beforeJson = JSON.stringify(beforeVal ?? null);
    const afterJson = JSON.stringify(afterVal ?? null);
    if (beforeJson === afterJson) continue;
    seenKeys.add(key);
    const label =
      typeof schemaEntry.title === "string"
        ? schemaEntry.title
        : key.split(".").slice(-1)[0].replace(/_/g, " ");
    changes.push({
      key,
      category: String(schemaEntry.category ?? "general"),
      label,
      before: beforeVal,
      after: afterVal,
    });
  }

  // Removed keys (no longer in schema, but value differs from before).
  // Skipped — schema is the source of truth for known keys; user can't
  // remove a schema'd key without removing its schema entry, which is its
  // own surface.

  // Sort: by category, then by key.  Keeps related changes together.
  changes.sort((a, b) => {
    if (a.category !== b.category) return a.category.localeCompare(b.category);
    return a.key.localeCompare(b.key);
  });

  return changes;
}

// Tag to render in the ConfigPage header indicating a save is staged.