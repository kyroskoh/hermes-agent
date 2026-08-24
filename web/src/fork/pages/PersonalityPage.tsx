// FORK: kyroskoh/hermes-agent
// Per-profile persona knob editor. Each knob is a 0–100 percentage that
// tunes a specific characteristic of the persona defined in SOUL.md.
// Changes are persisted under `personality.knobs.<name>` in the active
// profile's config.yaml via the `/api/personality` REST surface.
//
// UX:
//     * Knobs render as sliders with a numeric input alongside so the
//       operator can either drag or type a value.
//     * A small "default" / "override" badge tells the operator whether
//       the live value matches the factory default — clicking Reset
//       removes the override and falls back to default.
//     * Save button is only enabled when the slider has moved off the
//       server-reported value (or, when editing an override, off the
//       override). Server clamps out-of-range writes silently and the
//       success toast flags the clamp.
//
// The runtime consumer that actually injects these values into the
// agent's SOUL.md lives at agent/prompt_builder.load_soul_md (upstream-
// owned; see FORK PROVENANCE card on the page for the full pipeline).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  RotateCcw,
  Save,
  Sliders,
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
import { api, type PersonalityKnob } from "@/lib/api";
import { useForkI18n } from "@/fork/i18n/useForkI18n";

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

interface KnobDraft {
  value: number;
  /** Snapshot of the server-reported value when the draft was created. */
  baseline: number;
  /** True when the operator is editing an override (vs. the default). */
  hasOverride: boolean;
}

export default function PersonalityPage() {
  const { personality: t } = useForkI18n();
  const [knobs, setKnobs] = useState<PersonalityKnob[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, KnobDraft>>({});
  const [busyName, setBusyName] = useState<string | null>(null);
  const [toast, setToast] = useState<{ tone: "ok" | "info"; text: string } | null>(
    null,
  );
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback(
    (tone: "ok" | "info", text: string) => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
      setToast({ tone, text });
      toastTimer.current = setTimeout(() => setToast(null), 2200);
    },
    [],
  );

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const resp = await api.getPersonality();
      setKnobs(resp.knobs);
      // Reset drafts to the freshly-reported values; this discards any
      // unsaved edits so a manual refresh is a hard "reload from server".
      const next: Record<string, KnobDraft> = {};
      for (const k of resp.knobs) {
        next[k.name] = {
          value: k.value,
          baseline: k.value,
          hasOverride: !k.is_default,
        };
      }
      setDrafts(next);
    } catch (e) {
      setLoadError(formatError(e, t.errorLoad));
    }
  }, [t.errorLoad]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setDraftValue = (name: string, value: number) => {
    setDrafts((prev) => ({
      ...prev,
      [name]: { ...prev[name], value },
    }));
  };

  const isDirty = useMemo(() => {
    if (!knobs) return false;
    return knobs.some((k) => {
      const d = drafts[k.name];
      if (!d) return false;
      return d.value !== d.baseline;
    });
  }, [knobs, drafts]);

  const saveAll = useCallback(async () => {
    if (!knobs) return;
    setBusyName("__all__");
    setLoadError(null);
    try {
      for (const k of knobs) {
        const d = drafts[k.name];
        if (!d || d.value === d.baseline) continue;
        await api.setPersonalityKnob(k.name, d.value);
      }
      await refresh();
      showToast("ok", t.savedToast);
    } catch (e) {
      setLoadError(formatError(e, t.errorGeneric));
    } finally {
      setBusyName(null);
    }
  }, [knobs, drafts, refresh, showToast, t.savedToast, t.errorGeneric]);

  const resetOne = useCallback(
    async (k: PersonalityKnob) => {
      setBusyName(k.name);
      setLoadError(null);
      try {
        await api.resetPersonalityKnob(k.name);
        await refresh();
        showToast("info", t.resetToast);
      } catch (e) {
        setLoadError(formatError(e, t.errorGeneric));
      } finally {
        setBusyName(null);
      }
    },
    [refresh, showToast, t.resetToast, t.errorGeneric],
  );

  const resetAll = useCallback(async () => {
    if (!knobs) return;
    setBusyName("__all__");
    setLoadError(null);
    try {
      for (const k of knobs) {
        const d = drafts[k.name];
        // Only reset knobs that actually have an override — no point
        // round-tripping a DELETE for knobs already on the default.
        if (!d?.hasOverride) continue;
        await api.resetPersonalityKnob(k.name);
      }
      await refresh();
      showToast("info", t.resetToast);
    } catch (e) {
      setLoadError(formatError(e, t.errorGeneric));
    } finally {
      setBusyName(null);
    }
  }, [knobs, drafts, refresh, showToast, t.resetToast, t.errorGeneric]);

  return (
    <div className="space-y-6 p-6">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">{t.title}</h1>
        <p className="text-muted-foreground max-w-3xl text-sm">{t.intro}</p>
        <p className="text-muted-foreground/80 text-xs italic">
          {t.upstreamNotice}
        </p>
      </header>

      {loadError && (
        <div className="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border p-3 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
          <span>{loadError}</span>
        </div>
      )}

      {/* Knob list */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sliders className="h-4 w-4" />
            {knobs?.length ?? 0} knob{(knobs?.length ?? 0) === 1 ? "" : "s"}
          </CardTitle>
          <CardDescription>
            {isDirty
              ? "Unsaved changes — click Save to apply."
              : "All values match the server."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {knobs === null && !loadError && (
            <p className="text-muted-foreground text-sm italic">Loading…</p>
          )}
          {knobs?.length === 0 && (
            <p className="text-muted-foreground text-sm italic">
              No personality knobs registered. Add entries to
              hermes_cli.personality_knobs.REGISTRY to surface them here.
            </p>
          )}
          {knobs?.map((k) => {
            const draft = drafts[k.name];
            const dirty = !!draft && draft.value !== draft.baseline;
            const busy = busyName === k.name;
            const savingAll = busyName === "__all__";
            return (
              <div key={k.name} className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="space-y-0.5">
                    <Label
                      htmlFor={`knob-${k.name}`}
                      className="flex items-center gap-2 text-sm font-medium"
                    >
                      {k.label}
                      <span className="text-muted-foreground font-mono text-xs">
                        {k.name}
                      </span>
                      {draft?.hasOverride ? (
                        <Badge tone="warning" className="text-xs">
                          {t.knobOverrideBadge}
                        </Badge>
                      ) : (
                        <Badge tone="secondary" className="text-xs">
                          {t.knobDefaultBadge}
                        </Badge>
                      )}
                    </Label>
                    <p className="text-muted-foreground text-xs">
                      {k.description}
                    </p>
                    <p className="text-muted-foreground/70 text-xs">
                      {t.rangeLabel(k.min, k.max, k.default)}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Input
                      id={`knob-${k.name}`}
                      type="number"
                      min={k.min}
                      max={k.max}
                      value={draft?.value ?? k.value}
                      onChange={(e) => {
                        const v = Number(e.target.value);
                        if (Number.isFinite(v)) {
                          setDraftValue(k.name, v);
                          if (v < k.min || v > k.max) {
                            showToast("info", t.outOfRangeToast);
                          }
                        }
                      }}
                      className="w-20 text-right font-mono"
                      disabled={busy || savingAll}
                    />
                    <Button
                      ghost
                      size="sm"
                      disabled={busy || savingAll || !draft?.hasOverride}
                      onClick={() => void resetOne(k)}
                      title={t.resetButton}
                    >
                      <RotateCcw className="mr-1 h-3 w-3" />
                      {busy ? t.resettingButton : t.resetButton}
                    </Button>
                  </div>
                </div>
                <input
                  type="range"
                  aria-label={k.label}
                  value={draft?.value ?? k.value}
                  min={k.min}
                  max={k.max}
                  step={1}
                  disabled={busy || savingAll}
                  onChange={(e) =>
                    setDraftValue(k.name, Number(e.target.value))
                  }
                  className={`w-full accent-primary ${dirty ? "ring-warning/40 ring-2" : ""}`}
                />
              </div>
            );
          })}

          <div className="flex flex-wrap items-center gap-2 border-t pt-4">
            <Button
              onClick={() => void saveAll()}
              disabled={!isDirty || busyName !== null}
              size="sm"
            >
              <Save className="mr-1 h-4 w-4" />
              {busyName === "__all__" ? t.savingButton : t.saveButton}
            </Button>
            <Button
              ghost
              size="sm"
              disabled={busyName !== null}
              onClick={() => void resetAll()}
            >
              <RotateCcw className="mr-1 h-3 w-3" />
              {t.resetButton}
            </Button>
            <Button
              ghost
              size="sm"
              disabled={busyName !== null}
              onClick={() => void refresh()}
            >
              {t.refreshButton}
            </Button>
            {toast && (
              <span
                className={`flex items-center gap-1 text-xs ${
                  toast.tone === "ok" ? "text-success" : "text-info"
                }`}
              >
                {toast.tone === "ok" ? (
                  <CheckCircle2 className="h-3 w-3" />
                ) : (
                  <Info className="h-3 w-3" />
                )}
                {toast.text}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* How live values reach the persona */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Info className="h-4 w-4" />
            {t.howLiveTitle}
          </CardTitle>
          <CardDescription>{t.howLiveBody}</CardDescription>
        </CardHeader>
      </Card>

      {/* Fork provenance */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.forkProvenanceTitle}</CardTitle>
          <CardDescription>{t.forkProvenanceBody}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-1 text-xs">
          <div>
            <span className="text-muted-foreground">{t.storageLabel}:</span>{" "}
            <span className="font-mono">
              ~/.hermes/profiles/&lt;name&gt;/config.yaml
            </span>{" "}
            <span className="text-muted-foreground">→</span>{" "}
            <span className="font-mono">personality.knobs.&lt;name&gt;</span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}