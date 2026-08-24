// FORK: kyroskoh/hermes-agent
// Manage the fallback provider chain (`fallback_providers` in
// ~/.hermes/config.yaml) from the dashboard. The runtime consumer
// (agent/agent_runtime_helpers._try_activate_fallback) is upstream-owned
// and unchanged — this page only wraps the existing CLI / REST surface so
// the operator can view and edit the chain without a shell.
//
// UX:
//   * Primary model is read-only at the top so the operator knows what the
//     chain falls back FROM.
//   * Chain is editable inline: ↑/↓ to reorder, ✕ to remove. There's a
//     Save Order button that only appears when the local order has drifted
//     from the server.
//   * Add form lets the operator append one entry at a time. Server-side
//     validation rejects duplicates and entries that match the primary.
//   * Clear button wipes the chain (with confirmation).
//
// Triggers card shows the upstream failure classes that fall through to
// the chain (HTTP 429, 5xx, connection errors) and explicitly notes that
// auth errors (401/403) rotate credentials instead — to avoid the
// operator expecting a fallback to kick in for an auth failure.

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Ban,
  CheckCircle2,
  Clock,
  Plus,
  RefreshCw,
  Trash2,
  Wallet,
  XCircle,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { api } from "@/lib/api";
import { useForkI18n } from "@/fork/i18n/useForkI18n";
import type {
  FallbackChainResponse,
  FallbackEntryPayload,
  FallbackChainStatusResponse,
} from "@/lib/api";

interface AddForm {
  provider: string;
  model: string;
  base_url: string;
}

const EMPTY_ADD: AddForm = { provider: "", model: "", base_url: "" };

function formatError(err: unknown, genericMsg: string): string {
  if (err instanceof Error) {
    const match = err.message.match(/^\d+:\s*(.*)$/);
    if (match && match[1]) {
      try {
        const parsed = JSON.parse(match[1]) as { detail?: string };
        if (parsed.detail) return parsed.detail;
      } catch {
        /* not JSON, fall through */
      }
      return match[1].slice(0, 200);
    }
    return err.message;
  }
  return genericMsg;
}

export default function FallbackPage() {
  const { fallback: t } = useForkI18n();
  const [data, setData] = useState<FallbackChainResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Working copy of the chain for in-flight reorders. Once saved, sync
  // back to data.chain so the "save order" button hides itself.
  const [draft, setDraft] = useState<FallbackEntryPayload[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);

  // Smart-fallback cache snapshot. Populated by refresh() so each entry
  // can render its skip badge (Available / No credits / Cooldown / Unknown).
  const [status, setStatus] = useState<FallbackChainStatusResponse | null>(
    null,
  );

  // Add form state.
  const [addForm, setAddForm] = useState<AddForm>(EMPTY_ADD);
  const [addError, setAddError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [chain, statusResp] = await Promise.all([
        api.getFallbackChain(),
        api.getFallbackStatus().catch(() => null),
      ]);
      setData(chain);
      setStatus(statusResp);
      setDraft(chain.chain);
      setDraftDirty(false);
    } catch (e) {
      setError(formatError(e, t.errorGeneric));
    } finally {
      setLoading(false);
    }
  }, [t.errorGeneric]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const persistDraft = useCallback(async (next: FallbackEntryPayload[]) => {
    setBusy(true);
    setError(null);
    try {
      const resp = await api.reorderFallbackChain(next);
      setData((prev) =>
        prev
          ? { ...prev, chain: resp.chain ?? next }
          : prev,
      );
      setDraft(resp.chain ?? next);
      setDraftDirty(false);
    } catch (e) {
      setError(formatError(e, t.errorGeneric));
    } finally {
      setBusy(false);
    }
  }, [t.errorGeneric]);

  const moveEntry = (idx: number, dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= draft.length) return;
    const next = [...draft];
    [next[idx], next[j]] = [next[j], next[idx]];
    setDraft(next);
    setDraftDirty(true);
  };

  const removeEntry = async (idx: number) => {
    if (!window.confirm(t.confirmRemove)) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await api.removeFallbackEntry(idx);
      setData((prev) => (prev ? { ...prev, chain: resp.chain ?? [] } : prev));
      setDraft(resp.chain ?? []);
      setDraftDirty(false);
    } catch (e) {
      setError(formatError(e, t.errorGeneric));
    } finally {
      setBusy(false);
    }
  };

  const clearChain = async () => {
    if (!window.confirm(t.confirmClear)) return;
    setBusy(true);
    setError(null);
    try {
      await api.clearFallbackChain();
      setData((prev) => (prev ? { ...prev, chain: [] } : prev));
      setDraft([]);
      setDraftDirty(false);
    } catch (e) {
      setError(formatError(e, t.errorGeneric));
    } finally {
      setBusy(false);
    }
  };

  const submitAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!addForm.provider.trim() || !addForm.model.trim()) {
      setAddError(t.errorGeneric);
      return;
    }
    setBusy(true);
    setAddError(null);
    setError(null);
    try {
      const entry: FallbackEntryPayload = {
        provider: addForm.provider.trim(),
        model: addForm.model.trim(),
      };
      if (addForm.base_url.trim()) {
        entry.base_url = addForm.base_url.trim();
      }
      const resp = await api.appendFallbackEntry(entry);
      setData((prev) =>
        prev ? { ...prev, chain: resp.chain ?? [] } : prev,
      );
      setDraft(resp.chain ?? []);
      setDraftDirty(false);
      setAddForm(EMPTY_ADD);
    } catch (e) {
      const msg = formatError(e, t.errorGeneric);
      // Surface backend-rejection messages verbatim so the operator sees
      // the actual reason (e.g. "primary", "duplicate").
      if (msg.toLowerCase().includes("primary")) setAddError(t.errorPrimary);
      else if (msg.toLowerCase().includes("exists")) setAddError(t.errorDuplicate);
      else setAddError(msg);
    } finally {
      setBusy(false);
    }
  };

  const primary = data?.primary;
  const chain = data?.chain ?? [];
  const triggers = data?.triggers;

  return (
    <div className="space-y-6 p-6">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">{t.title}</h1>
        <p className="text-muted-foreground max-w-3xl text-sm">{t.intro}</p>
        <p className="text-muted-foreground/80 text-xs italic">
          {t.upstreamNotice}
        </p>
      </header>

      {/* Primary model & Auto-Orchestrator */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>{t.primaryLabel}</CardTitle>
            <div className="flex items-center gap-2">
              <Badge tone="success" className="text-xs">Orchestrator Active</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {primary && primary.model ? (
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="default">{primary.model}</Badge>
              <span className="text-muted-foreground text-sm">
                {t.viaProvider} <span className="font-mono">{primary.provider}</span>
              </span>
              {primary.base_url && (
                <span className="text-muted-foreground/70 font-mono text-xs">
                  ({primary.base_url})
                </span>
              )}
            </div>
          ) : (
            <p className="text-muted-foreground text-sm">{t.primaryNone}</p>
          )}
        </CardContent>
      </Card>

      {/* Smart-fallback cache — explains why each entry may be skipped */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Wallet className="h-4 w-4" />
            {t.smartFallbackTitle}
            {status?.cache.ok ? (
              <Badge tone="success" className="ml-1 inline-flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {t.smartFallbackCacheAge} {status.cache.age_seconds ?? "?"}s
              </Badge>
            ) : (
              <Badge tone="warning" className="ml-1">
                {t.smartFallbackCacheStale}
              </Badge>
            )}
          </CardTitle>
          <CardDescription>{t.smartFallbackBody}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {status?.cache.providers &&
            Object.entries(status.cache.providers).map(([name, info]) => (
              <div key={name} className="border-muted rounded-md border p-3">
                <div className="flex items-center gap-2">
                  <Badge
                    tone={
                      info.state === "available"
                        ? "success"
                        : info.state === "unknown"
                          ? "secondary"
                          : "warning"
                    }
                  >
                    {name}
                  </Badge>
                  <span className="text-muted-foreground font-mono text-xs">
                    {info.state}
                  </span>
                </div>
                <div className="text-muted-foreground/80 mt-1 text-xs">
                  {info.details}
                </div>
                {name === "nous" && typeof info.total_usable_credits === "number" && (
                  <div className="mt-1 text-xs">
                    <span className="text-muted-foreground">
                      {t.smartFallbackNousCredits}:
                    </span>{" "}
                    <span className="font-mono">{info.total_usable_credits}</span>
                    {typeof info.monthly_credits === "number" && (
                      <span className="text-muted-foreground/70">
                        {" "}/ {info.monthly_credits} monthly
                      </span>
                    )}
                  </div>
                )}
                {name === "openai-codex" &&
                  Array.isArray(info.credentials) &&
                  info.credentials.length > 0 && (
                    <div className="mt-2 space-y-1">
                      <div className="text-muted-foreground text-xs">
                        {t.smartFallbackCodexCredentials}: {info.credentials.length}
                      </div>
                      {info.credentials.map((c, i) => (
                        <div
                          key={i}
                          className="text-muted-foreground/80 ml-2 font-mono text-xs"
                        >
                          {c.id ?? "?"} —
                          {(c.exhausted_until ?? 0) > Date.now() / 1000
                            ? ` cooldown until ${new Date(
                                (c.exhausted_until ?? 0) * 1000,
                              ).toLocaleTimeString()}`
                            : " ok"}
                          {c.is_quarantined ? " (quarantined)" : ""}
                        </div>
                      ))}
                    </div>
                  )}
              </div>
            ))}
          <div className="text-muted-foreground/70 text-xs">
            {t.smartFallbackCachePath}: <span className="font-mono">{status?.cache.cache_path ?? "—"}</span>
          </div>
        </CardContent>
      </Card>

      {/* Chain editor */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {t.chainTitle}
            <Badge tone="secondary">{chain.length}</Badge>
          </CardTitle>
          <CardDescription>{t.chainDescription}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {error && (
            <div className="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border p-3 text-sm">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {chain.length === 0 ? (
            <p className="text-muted-foreground text-sm italic">{t.chainEmpty}</p>
          ) : (
            <ol className="space-y-2">
              {draft.map((entry, idx) => {
                // Look up the skip_reason from the status endpoint's
                // annotated chain (matched by provider+model+base_url).
                const statusEntry = status?.chain.find(
                  (s) =>
                    s.provider === entry.provider &&
                    s.model === entry.model &&
                    (s.base_url ?? "") === (entry.base_url ?? ""),
                );
                const skipReason = statusEntry?.skip_reason ?? null;
                return (
                <li
                  key={`${entry.provider}-${entry.model}-${entry.base_url ?? ""}-${idx}`}
                  className="bg-muted/30 flex items-center gap-3 rounded-md border p-3"
                >
                  <Badge tone="outline" className="w-8 justify-center">
                    {idx + 1}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="font-mono text-sm font-medium">
                        {entry.model}
                      </span>
                      <span className="text-muted-foreground text-xs">
                        {t.viaProvider} {entry.provider}
                      </span>
                      {skipReason && (
                        <Badge
                          tone="warning"
                          className="ml-1 inline-flex items-center gap-1"
                          title={skipReason}
                        >
                          <Ban className="h-3 w-3" />
                          {skipReason.includes("no_credits")
                            ? t.skipBadgeNoCredits
                            : skipReason.includes("cooldown")
                              ? t.skipBadgeCooldown
                              : t.skipBadgeUnknown}
                        </Badge>
                      )}
                    </div>
                    {entry.base_url && (
                      <div className="text-muted-foreground/70 mt-1 truncate font-mono text-xs">
                        {entry.base_url}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-1">
                    <Button
                      ghost
                      size="icon"
                      disabled={idx === 0 || busy}
                      onClick={() => moveEntry(idx, -1)}
                      aria-label={t.moveUp}
                      title={t.moveUp}
                    >
                      <ArrowUp className="h-4 w-4" />
                    </Button>
                    <Button
                      ghost
                      size="icon"
                      disabled={idx === draft.length - 1 || busy}
                      onClick={() => moveEntry(idx, 1)}
                      aria-label={t.moveDown}
                      title={t.moveDown}
                    >
                      <ArrowDown className="h-4 w-4" />
                    </Button>
                    <Button
                      ghost
                      size="icon"
                      disabled={busy}
                      onClick={() => void removeEntry(idx)}
                      aria-label={t.remove}
                      title={t.remove}
                    >
                      <XCircle className="text-destructive h-4 w-4" />
                    </Button>
                  </div>
                </li>
                );
              })}
            </ol>
          )}

          <div className="flex flex-wrap items-center gap-2">
            {draftDirty && (
              <span className="text-muted-foreground text-xs italic">
                {t.orderDirty}
              </span>
            )}
            <Button
              onClick={() => void persistDraft(draft)}
              disabled={!draftDirty || busy}
              size="sm"
            >
              <CheckCircle2 className="mr-1 h-4 w-4" />
              {t.saveOrder}
            </Button>
            <Button
              ghost
              size="sm"
              onClick={() => void refresh()}
              disabled={loading || busy}
            >
              <RefreshCw
                className={`mr-1 h-4 w-4 ${loading ? "animate-spin" : ""}`}
              />
              {loading ? "…" : ""}
            </Button>
            {chain.length > 0 && (
              <Button
                ghost
                destructive
                size="sm"
                onClick={() => void clearChain()}
                disabled={busy}
              >
                <Trash2 className="mr-1 h-4 w-4" />
                {t.clearAll}
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Add form */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="h-4 w-4" />
            {t.addTitle}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form className="space-y-3" onSubmit={submitAdd}>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label htmlFor="fb-provider">{t.addProviderLabel}</Label>
                <Input
                  id="fb-provider"
                  value={addForm.provider}
                  onChange={(e) =>
                    setAddForm({ ...addForm, provider: e.target.value })
                  }
                  placeholder="minimax-oauth"
                  autoComplete="off"
                  disabled={busy}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="fb-model">{t.addModelLabel}</Label>
                <Input
                  id="fb-model"
                  value={addForm.model}
                  onChange={(e) =>
                    setAddForm({ ...addForm, model: e.target.value })
                  }
                  placeholder="MiniMax-M2.7-highspeed"
                  autoComplete="off"
                  disabled={busy}
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="fb-base-url">{t.addBaseUrlLabel}</Label>
              <Input
                id="fb-base-url"
                value={addForm.base_url}
                onChange={(e) =>
                  setAddForm({ ...addForm, base_url: e.target.value })
                }
                placeholder={t.addBaseUrlPlaceholder}
                autoComplete="off"
                disabled={busy}
              />
            </div>
            {addError && (
              <p className="text-destructive text-sm">{addError}</p>
            )}
            <Button type="submit" disabled={busy}>
              <Plus className="mr-1 h-4 w-4" />
              {t.addSubmit}
            </Button>
          </form>
        </CardContent>
      </Card>

      {/* Triggers reference */}
      <Card>
        <CardHeader>
          <CardTitle>{t.triggersTitle}</CardTitle>
          <CardDescription>{t.triggersIntro}</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2 text-sm">
            <li className="flex items-center gap-2">
              {triggers?.rate_limit ? (
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              ) : (
                <XCircle className="text-muted-foreground h-4 w-4" />
              )}
              {t.triggersRateLimit}
            </li>
            <li className="flex items-center gap-2">
              {triggers?.upstream_429 ? (
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              ) : (
                <XCircle className="text-muted-foreground h-4 w-4" />
              )}
              {t.triggersUpstream429}
            </li>
            <li className="flex items-center gap-2">
              {triggers?.five_xx ? (
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              ) : (
                <XCircle className="text-muted-foreground h-4 w-4" />
              )}
              {t.triggersFiveXx}
            </li>
            <li className="flex items-center gap-2">
              {triggers?.connection ? (
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              ) : (
                <XCircle className="text-muted-foreground h-4 w-4" />
              )}
              {t.triggersConnection}
            </li>
            <li className="flex items-center gap-2">
              {triggers?.auth ? (
                <CheckCircle2 className="h-4 w-4 text-green-600" />
              ) : (
                <XCircle className="text-muted-foreground h-4 w-4" />
              )}
              {t.triggersAuth}
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
