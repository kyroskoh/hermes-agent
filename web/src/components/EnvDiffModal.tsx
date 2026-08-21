/**
 * EnvDiffModal — review-before-save gate for staged env-var edits.
 *
 * EnvPage accumulates edits in `edits` (already exists).  This modal
 * takes a snapshot of those edits + any pending clears (keys the user
 * wants to remove) and asks the user to confirm with a typed APPLY.
 *
 * Behavior:
 *  • Each row shows: key, redacted-before, new-value (also redacted by
 *    default — value is sensitive).  An eye icon reveals both sides
 *    (single source of truth: the API call returns the real value, the
 *    UI never echoes it without an explicit reveal).
 *  • Per-row discard button drops one staged change.
 *  • "Discard all" wipes all staged edits.
 *  • "Apply all" with APPLY typed → runs PUT /api/env/{key} for each
 *    set, and DELETE /api/env/{key} for each clear, sequentially.  Stops
 *    on first failure and surfaces a toast.
 *
 * Rendered via `createPortal(..., document.body)` to escape the dashboard
 * column's stacking context.
 */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Eye,
  EyeOff,
  Plus,
  Trash2,
  CheckCircle2,
  XCircle,
  X,
  ListChecks,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { useI18n } from "@/i18n";
import {
  DASHBOARD_MODAL_BACKDROP,
  DASHBOARD_MODAL_PANEL,
} from "@/lib/dashboard-modal-shell";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface EnvEdit {
  /** Variable name, e.g. "ANTHROPIC_API_KEY" */
  key: string;
  /** True if the operation is a clear (DELETE), false for a set (PUT) */
  clearing: boolean;
  /**
   * Redacted form of the previous value (or null if unset).  We don't
   * store the plaintext in the modal state — it's fetched on demand.
   */
  redactedBefore: string | null;
  /** True if the variable was set before this edit. */
  wasSet: boolean;
  /**
   * For set operations, the pending value (already in `edits` map).
   * For clear operations, null.
   */
  pendingValue: string | null;
}

export interface EnvDiffApplyResult {
  /** Number of successful set/clear operations */
  succeeded: number;
  /** Total operations attempted */
  total: number;
  /** First failure error message, if any */
  error?: string;
}

interface EnvDiffModalProps {
  /** Snapshot of all staged edits to review */
  edits: EnvEdit[];
  /** Drop a single edit from the staging area */
  onDiscard: (key: string) => void;
  /** Wipe the entire staging area */
  onDiscardAll: () => void;
  onCancel: () => void;
  /** Called with the final result after the user confirms.  Receives the
   * updated edit list (already applied server-side) on success. */
  onApplied: (result: EnvDiffApplyResult) => void;
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function EnvDiffModal({
  edits,
  onDiscard,
  onDiscardAll,
  onCancel,
  onApplied,
}: EnvDiffModalProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [confirmText, setConfirmText] = useState("");
  const [applying, setApplying] = useState(false);
  const [revealed, setRevealed] = useState<Record<string, string>>({});
  const confirmInputRef = useRef<HTMLInputElement>(null);

  // Reset state when the modal opens.
  useEffect(() => {
    if (edits.length === 0) return;
    setConfirmText("");
    setApplying(false);
    setRevealed({});
    const id = window.requestAnimationFrame(() => confirmInputRef.current?.focus());
    return () => window.cancelAnimationFrame(id);
  }, [edits]);

  // Escape closes (unless an apply is in flight).
  useEffect(() => {
    if (edits.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !applying) {
        e.preventDefault();
        onCancel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [edits, applying, onCancel]);

  const confirmWord = t.envDiff?.confirmApplyWord ?? "APPLY";
  const confirmRequired = confirmText.trim() !== confirmWord;

  // Lazy-fetch the real value when the user clicks reveal.  We don't load
  // every variable up front — many have secrets, and the modal can show
  // many at once.  Reveals are scoped to one key at a time.
  const handleReveal = async (key: string) => {
    if (revealed[key]) {
      setRevealed((prev) => {
        const n = { ...prev };
        delete n[key];
        return n;
      });
      return;
    }
    try {
      const resp = await api.revealEnvVar(key);
      setRevealed((prev) => ({ ...prev, [key]: resp.value }));
    } catch {
      showToast(`Failed to reveal ${key}`, "error");
    }
  };

  const handleConfirm = async () => {
    if (confirmRequired || applying) {
      if (confirmRequired) {
        showToast(
          t.envDiff?.typeApplyToConfirm ?? `Type ${confirmWord} to confirm`,
          "error",
        );
      }
      return;
    }
    setApplying(true);
    let succeeded = 0;
    let firstError: string | undefined;
    for (const edit of edits) {
      try {
        if (edit.clearing) {
          await api.deleteEnvVar(edit.key);
        } else if (edit.pendingValue !== null) {
          await api.setEnvVar(edit.key, edit.pendingValue);
        }
        succeeded += 1;
      } catch (e) {
        firstError = String(e);
        break;
      }
    }
    setApplying(false);
    if (firstError) {
      showToast(
        `${t.envDiff?.applyAllFailed ?? "One or more env-var writes failed"}: ${firstError}`,
        "error",
      );
      onApplied({
        succeeded,
        total: edits.length,
        error: firstError,
      });
    } else {
      onApplied({ succeeded, total: edits.length });
    }
  };

  if (edits.length === 0) return null;

  const sets = edits.filter((e) => !e.clearing).length;
  const clears = edits.filter((e) => e.clearing).length;

  return createPortal(
    <div
      className={DASHBOARD_MODAL_BACKDROP}
      role="dialog"
      aria-modal="true"
      aria-label={t.envDiff?.title ?? "Review env-var changes"}
      onMouseDown={(e) => {
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
        {/* Header */}
        <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-border">
          <div className="min-w-0">
            <h2 className="text-base font-semibold flex items-center gap-2">
              <ListChecks className="h-4 w-4 text-muted-foreground" />
              {t.envDiff?.title ?? "Review env-var changes"}
            </h2>
            <p className="text-xs text-muted-foreground mt-0.5">
              {t.envDiff?.description ??
                "These env-var edits are staged and will be written to ~/.hermes/.env when you apply."}
            </p>
            <p className="text-xs text-text-secondary mt-1 flex items-center gap-2 flex-wrap">
              <Badge tone="secondary" className="text-[10px]">
                {sets} {t.envDiff?.setLabel ?? "set"}
              </Badge>
              {clears > 0 && (
                <Badge tone="destructive" className="text-[10px]">
                  {clears} {t.envDiff?.clearLabel ?? "clear"}
                </Badge>
              )}
              <span className="text-text-tertiary">
                {t.envDiff?.pendingCount
                  ? t.envDiff.pendingCount
                      .replace("{n}", String(edits.length))
                      .replace("{s}", edits.length !== 1 ? "s" : "")
                  : `${edits.length} pending`}
              </span>
            </p>
          </div>
          <Button
            ghost
            size="icon"
            onClick={onCancel}
            disabled={applying}
            aria-label={t.envDiff?.cancel ?? "Cancel"}
          >
            <X />
          </Button>
        </div>

        {/* Body — list of pending changes */}
        <div className="flex-1 min-h-0 overflow-y-auto px-5 py-3">
          <Card>
            <CardHeader className="px-4 py-2 bg-muted/30">
              <CardTitle className="text-xs uppercase tracking-wider text-text-secondary">
                {t.envDiff?.pendingChanges ?? "Pending changes"}
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {edits.map((edit) => (
                <EnvDiffRow
                  key={edit.key}
                  edit={edit}
                  revealed={revealed[edit.key]}
                  onReveal={() => void handleReveal(edit.key)}
                  onDiscard={() => onDiscard(edit.key)}
                  disabled={applying}
                />
              ))}
            </CardContent>
          </Card>
        </div>

        {/* Footer / confirm */}
        <div className="border-t border-border px-5 py-4 bg-muted/20">
          <div className="flex items-center gap-3 mb-3">
            <label className="text-xs font-semibold tracking-wide uppercase text-text-secondary whitespace-nowrap">
              {t.envDiff?.typeLabel ?? `Type ${confirmWord} to confirm`}
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
              aria-label={t.envDiff?.typeAriaLabel ?? `Type ${confirmWord} to confirm`}
            />
          </div>
          <div className="flex items-center justify-between gap-2">
            <Button
              ghost
              destructive
              onClick={() => {
                if (
                  !applying &&
                  window.confirm(
                    t.envDiff?.discardAll ??
                      "Discard all staged env-var edits?",
                  )
                ) {
                  onDiscardAll();
                }
              }}
              disabled={applying}
              prefix={<Trash2 />}
            >
              {t.envDiff?.discardAll ?? "Discard all"}
            </Button>
            <div className="flex items-center gap-2">
              <Button outlined onClick={onCancel} disabled={applying}>
                {t.envDiff?.cancel ?? t.common.cancel ?? "Cancel"}
              </Button>
              <Button
                onClick={() => void handleConfirm()}
                disabled={confirmRequired || applying}
                prefix={
                  applying ? undefined : <CheckCircle2 className="h-3.5 w-3.5" />
                }
              >
                {applying
                  ? t.common.saving ?? "Saving…"
                  : t.envDiff?.apply ?? "Apply all"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ------------------------------------------------------------------ */
/*  Per-edit row                                                       */
/* ------------------------------------------------------------------ */

function EnvDiffRow({
  edit,
  revealed,
  onReveal,
  onDiscard,
  disabled,
}: {
  edit: EnvEdit;
  revealed?: string;
  onReveal: () => void;
  onDiscard: () => void;
  disabled: boolean;
}) {
  const isRevealed = revealed !== undefined;
  const displayBefore = isRevealed
    ? revealed
    : edit.redactedBefore ?? "---";
  const displayAfter =
    edit.clearing
      ? "(unset)"
      : isRevealed && revealed
        ? revealed // — placeholder so
        : edit.pendingValue
          ? mask(edit.pendingValue)
          : "(empty)";

  return (
    <div className="grid gap-2 px-4 py-3 border-t border-border/50 first:border-t-0">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-mono-ui text-xs truncate">{edit.key}</span>
          <Badge
            tone={edit.clearing ? "destructive" : "secondary"}
            className="text-[10px] shrink-0"
          >
            {edit.clearing ? (
              <span className="flex items-center gap-1">
                <XCircle className="h-3 w-3" /> clear
              </span>
            ) : (
              <span className="flex items-center gap-1">
                <Plus className="h-3 w-3" /> set
              </span>
            )}
          </Badge>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <Button
            ghost
            size="icon"
            onClick={onReveal}
            disabled={disabled}
            title={isRevealed ? "Hide value" : "Reveal value"}
            aria-label={isRevealed ? `Hide ${edit.key}` : `Reveal ${edit.key}`}
          >
            {isRevealed ? <EyeOff /> : <Eye />}
          </Button>
          <Button
            ghost
            size="icon"
            destructive
            onClick={onDiscard}
            disabled={disabled}
            title="Discard this change"
            aria-label={`Discard change to ${edit.key}`}
          >
            <Trash2 />
          </Button>
        </div>
      </div>
      <div className="grid grid-cols-[auto_1fr] gap-2 items-center">
        <span className="font-mono-ui text-[10px] uppercase tracking-wider text-text-tertiary">
          before
        </span>
        <div
          className={cn(
            "font-mono-ui text-xs px-2 py-1 border break-all",
            edit.wasSet
              ? "bg-red-500/10 border-red-500/30 text-red-700 dark:text-red-300"
              : "bg-muted/30 border-border text-text-tertiary italic",
          )}
        >
          {displayBefore || (edit.wasSet ? "" : "(unset)")}
        </div>
        <span className="font-mono-ui text-[10px] uppercase tracking-wider text-text-tertiary">
          after
        </span>
        <div
          className={cn(
            "font-mono-ui text-xs px-2 py-1 border break-all",
            edit.clearing
              ? "bg-red-500/10 border-red-500/30 text-red-700 dark:text-red-300 italic"
              : "bg-green-500/10 border-green-500/30 text-green-700 dark:text-green-300",
          )}
        >
          {displayAfter}
        </div>
      </div>
    </div>
  );
}

function mask(value: string): string {
  if (value.length <= 8) return "•".repeat(value.length);
  return value.slice(0, 4) + "•••" + value.slice(-4);
}