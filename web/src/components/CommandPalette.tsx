/**
 * CommandPalette — ⌘K / Ctrl-K launcher.
 *
 * Global search/jump surface that overlays the whole dashboard. Backed by
 * `createPortal(..., document.body)` with z-[100] to escape the dashboard
 * column's stacking context (see SidebarTooltip for the same pattern).
 *
 * Index composition
 * ──────────────────
 *  • Built-in navigation items                                          →  "Go to …"
 *  • Plugin pages (from usePlugins())                                  →  "Go to <plugin>"
 *  • Config keys (from /api/config/schema + /api/config)               →  "Set <key>" + /config?focus=
 *  • Cron jobs (from /api/cron/jobs)                                   →  sub-actions: Run / Pause / Resume / Open
 *  • Skills (from /api/skills)                                         →  "Open skill: <name>"
 *  • Recent sessions (from /api/sessions)                              →  "Resume session <title|id>"
 *  • Quick actions                                                     →  Reload config, Open env, Open logs, …
 *
 * Each item carries a typed `kind` so the action handler can route
 * correctly (navigate / dispatch action).  Matching uses the existing
 * fuzzy scorer in @/lib/fuzzy — no new dependency.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
} from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate } from "react-router";
import {
  BarChart3,
  BookOpen,
  Calendar,
  Clock,
  Cpu,
  ExternalLink,
  FileText,
  FolderOpen,
  History,
  KeyRound,
  Loader2,
  MessageSquare,
  Package,
  Puzzle,
  Radio,
  Search,
  Settings,
  ShieldCheck,
  Users,
  Wrench,
  Webhook,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { cn } from "@/lib/utils";
import { fuzzyRank } from "@/lib/fuzzy";
import { api } from "@/lib/api";
import type { CronJob, SessionInfo, SkillInfo } from "@/lib/api";
import type { PluginManifest } from "@/plugins";
import { useI18n } from "@/i18n";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import {
  DASHBOARD_MODAL_BACKDROP,
  DASHBOARD_MODAL_PANEL,
} from "@/lib/dashboard-modal-shell";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

type PaletteKind =
  | "nav"
  | "plugin"
  | "config"
  | "cron"
  | "skill"
  | "session"
  | "action";

interface PaletteItemBase {
  /** Stable id (used as React key + ranking key) */
  id: string;
  /** Display title (used for fuzzy matching) */
  title: string;
  /** Optional secondary text (description, path, etc.) — also matches */
  subtitle?: string;
  /** Group header label */
  group: string;
  icon: ComponentType<{ className?: string }>;
  kind: PaletteKind;
}

interface NavItem extends PaletteItemBase {
  kind: "nav" | "plugin";
  path: string;
}

interface ConfigItem extends PaletteItemBase {
  kind: "config";
  key: string;
  /** Current serialized value, used as the action hint */
  currentValue?: unknown;
}

interface CronItem extends PaletteItemBase {
  kind: "cron";
  cronId: string;
  profile?: string | null;
  /** True when the job is currently running or armed; affects available actions */
  status: "running" | "paused" | "scheduled" | "completed";
}

interface SkillItem extends PaletteItemBase {
  kind: "skill";
  skillName: string;
}

interface SessionItem extends PaletteItemBase {
  kind: "session";
  sessionId: string;
}

interface ActionItem extends PaletteItemBase {
  kind: "action";
  action: () => void | Promise<void>;
}

type PaletteItem =
  | NavItem
  | ConfigItem
  | CronItem
  | SkillItem
  | SessionItem
  | ActionItem;

/* ------------------------------------------------------------------ */
/*  Built-in nav (mirrors BUILTIN_NAV_REST in App.tsx)                */
/* ------------------------------------------------------------------ */

interface BuiltinNav {
  path: string;
  labelKey: keyof Translations["app"]["nav"];
  fallback: string;
  icon: ComponentType<{ className?: string }>;
}

import type { Translations } from "@/i18n/types";

const BUILTIN_NAV: BuiltinNav[] = [
  { path: "/sessions", labelKey: "sessions", fallback: "Sessions", icon: MessageSquare },
  { path: "/files", labelKey: "files" as never, fallback: "Files", icon: FolderOpen },
  { path: "/analytics", labelKey: "analytics", fallback: "Analytics", icon: BarChart3 },
  { path: "/models", labelKey: "models", fallback: "Models", icon: Cpu },
  { path: "/logs", labelKey: "logs", fallback: "Logs", icon: FileText },
  { path: "/cron", labelKey: "cron", fallback: "Cron", icon: Clock },
  { path: "/skills", labelKey: "skills", fallback: "Skills", icon: Package },
  { path: "/plugins", labelKey: "plugins", fallback: "Plugins", icon: Puzzle },
  { path: "/channels", labelKey: "channels" as never, fallback: "Channels", icon: Radio },
  { path: "/webhooks", labelKey: "webhooks" as never, fallback: "Webhooks", icon: Webhook },
  { path: "/pairing", labelKey: "pairing" as never, fallback: "Pairing", icon: ShieldCheck },
  { path: "/profiles", labelKey: "profiles", fallback: "Profiles", icon: Users },
  { path: "/config", labelKey: "config", fallback: "Config", icon: Settings },
  { path: "/env", labelKey: "keys", fallback: "Keys", icon: KeyRound },
  { path: "/system", labelKey: "system" as never, fallback: "System", icon: Wrench },
  { path: "/docs", labelKey: "documentation", fallback: "Documentation", icon: BookOpen },
];

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function getCronStatus(job: CronJob): CronItem["status"] {
  // CronJob exposes `enabled`, `state` ("running"|"idle"|"paused"|...), and
  // `last_status` for the most recent run result.  Map those onto the small
  // set the palette UI cares about.
  const state = (job.state ?? "").toLowerCase();
  if (state === "running") return "running";
  if (!job.enabled || state === "paused") return "paused";
  if (job.last_status === "completed" && job.state === "completed") {
    return "completed";
  }
  return "scheduled";
}

function describeCronSchedule(job: CronJob): string {
  return (
    job.schedule_display ??
    job.schedule?.display ??
    job.schedule?.expr ??
    job.schedule?.run_at ??
    "—"
  );
}

function serializeValue(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function formatRelative(unixSeconds: number): string {
  // Best-effort, locale-free — the dashboard already shows timestamps
  // elsewhere with absolute formatting; here we just want a short hint.
  const diff = Math.floor(Date.now() / 1000 - unixSeconds);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/* ------------------------------------------------------------------ */
/*  Tiny util: getNested (replicates @/lib/nested without importing) */
/* ------------------------------------------------------------------ */

function getNested(obj: unknown, dotted: string): unknown {
  if (!obj || typeof obj !== "object") return undefined;
  let cur: unknown = obj;
  for (const part of dotted.split(".")) {
    if (!cur || typeof cur !== "object") return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return cur;
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

interface CommandPaletteProps {
  /** Plugins from usePlugins() — already in scope where this is mounted */
  manifests: PluginManifest[];
}

export function CommandPalette({ manifests }: CommandPaletteProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { showToast } = useToast();

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [loadingRemote, setLoadingRemote] = useState(false);

  // Remote index sources — only fetched when the palette opens for the first
  // time per session. Subsequent opens reuse the cache.
  const [cronJobs, setCronJobs] = useState<CronJob[] | null>(null);
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Hotkey: ⌘K / Ctrl-K toggles open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const isHotkey =
        (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k";
      if (isHotkey) {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Reset state when the palette opens, focus the input.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    // Defer focus until after the portal paints, otherwise the autofocus
    // races the createPortal call and the input is unmounted.
    const id = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(id);
  }, [open]);

  // Lazy fetch of remote index on first open.
  useEffect(() => {
    if (!open) return;
    if (cronJobs && skills && sessions) return;
    setLoadingRemote(true);
    Promise.allSettled([
      cronJobs ? Promise.resolve(null) : api.getCronJobs("all"),
      skills ? Promise.resolve(null) : api.getSkills(),
      sessions
        ? Promise.resolve(null)
        : api
            .getSessions(10, 0, { order: "recent" } as never)
            .then((resp) => resp.sessions)
            .catch(() => null),
    ])
      .then((results) => {
        const [cronRes, skillsRes, sessionsRes] = results;
        if (cronRes.status === "fulfilled" && cronRes.value) {
          setCronJobs(cronRes.value as CronJob[]);
        }
        if (skillsRes.status === "fulfilled" && skillsRes.value) {
          setSkills(skillsRes.value as SkillInfo[]);
        }
        if (sessionsRes.status === "fulfilled" && sessionsRes.value) {
          setSessions(sessionsRes.value as SessionInfo[]);
        }
      })
      .finally(() => setLoadingRemote(false));
  }, [open, cronJobs, skills, sessions]);

  // Build the static nav index (cheap; rebuilds only on manifest/lang change).
  const navItems = useMemo<NavItem[]>(() => {
    const navGroup = t.palette.groupNavigation ?? "Navigation";
    const pluginGroup = t.palette.groupPlugins ?? "Plugins";
    const items: NavItem[] = [];
    for (const nav of BUILTIN_NAV) {
      const label =
        (t.app.nav[nav.labelKey] as string | undefined) ?? nav.fallback;
      items.push({
        id: `nav:${nav.path}`,
        kind: "nav",
        path: nav.path,
        title: label,
        subtitle: nav.path,
        group: navGroup,
        icon: nav.icon,
      });
    }
    for (const m of manifests) {
      if (m.tab.hidden) continue;
      const path = m.tab.override ?? m.tab.path;
      if (!path || path === "/plugins") continue;
      items.push({
        id: `plugin:${m.name}`,
        kind: "plugin",
        path,
        title: m.label,
        subtitle: m.description ?? m.name,
        group: pluginGroup,
        icon: Puzzle,
      });
    }
    return items;
  }, [manifests, t]);

  // Config items: fetched on open via /api/config/schema + /api/config.
  const [configKeys, setConfigKeys] = useState<
    Array<{ key: string; description?: string; category?: string; current?: unknown }>
  >([]);
  const configKeysLoadedRef = useRef(false);
  useEffect(() => {
    if (!open || configKeysLoadedRef.current) return;
    configKeysLoadedRef.current = true;
    Promise.allSettled([api.getSchema(), api.getConfig()])
      .then(([schemaRes, configRes]) => {
        if (schemaRes.status !== "fulfilled") return;
        const fields = schemaRes.value.fields ?? {};
        const current = configRes.status === "fulfilled" ? configRes.value : {};
        const items: typeof configKeys = [];
        for (const [key, schema] of Object.entries(fields)) {
          const s = schema as {
            description?: string;
            category?: string;
          };
          items.push({
            key,
            description: s.description,
            category: s.category,
            current: getNested(current, key),
          });
        }
        setConfigKeys(items);
      })
      .catch(() => {});
  }, [open]);

  const configItems = useMemo<ConfigItem[]>(
    () =>
      configKeys.map((c) => ({
        id: `config:${c.key}`,
        kind: "config",
        key: c.key,
        title: c.key,
        subtitle:
          (c.description ? truncate(c.description, 80) : c.category) ?? "",
        group: t.palette.groupConfig ?? "Config keys",
        icon: Settings,
        currentValue: c.current,
      })),
    [configKeys, t],
  );

  const cronItems = useMemo<CronItem[]>(
    () =>
      (cronJobs ?? []).map((job) => ({
        id: `cron:${job.id}`,
        kind: "cron",
        cronId: job.id,
        profile: job.profile ?? null,
        title: job.name ?? `Cron ${job.id.slice(0, 8)}`,
        subtitle: describeCronSchedule(job),
        group: t.palette.groupCron ?? "Cron jobs",
        icon: Calendar,
        status: getCronStatus(job),
      })),
    [cronJobs, t],
  );

  const skillItems = useMemo<SkillItem[]>(
    () =>
      (skills ?? []).map((s) => ({
        id: `skill:${s.name}`,
        kind: "skill",
        skillName: s.name,
        title: s.name,
        subtitle: s.description ?? "",
        group: t.palette.groupSkills ?? "Skills",
        icon: Package,
      })),
    [skills, t],
  );

  const sessionItems = useMemo<SessionItem[]>(
    () =>
      (sessions ?? []).map((sess) => ({
        id: `session:${sess.id}`,
        kind: "session",
        sessionId: sess.id,
        title: sess.title ?? sess.id,
        subtitle:
          sess.last_active > 0
            ? formatRelative(sess.last_active)
            : sess.preview ?? "",
        group: t.palette.groupSessions ?? "Recent sessions",
        icon: History,
      })),
    [sessions, t],
  );

  // Quick actions — always present, no remote fetch.
  const actionItems = useMemo<ActionItem[]>(
    () => [
      {
        id: "action:open-config",
        kind: "action",
        title: t.palette.actionOpenConfig ?? "Open config",
        subtitle: t.palette.actionOpenConfigSub ?? "Edit config.yaml",
        group: t.palette.groupActions ?? "Quick actions",
        icon: Settings,
        action: () => navigate("/config"),
      },
      {
        id: "action:open-env",
        kind: "action",
        title: t.palette.actionOpenEnv ?? "Open env vars",
        subtitle: t.palette.actionOpenEnvSub ?? "Manage API keys & env",
        group: t.palette.groupActions ?? "Quick actions",
        icon: KeyRound,
        action: () => navigate("/env"),
      },
      {
        id: "action:open-logs",
        kind: "action",
        title: t.palette.actionOpenLogs ?? "Open logs",
        subtitle: t.palette.actionOpenLogsSub ?? "Tail service logs",
        group: t.palette.groupActions ?? "Quick actions",
        icon: FileText,
        action: () => navigate("/logs"),
      },
      {
        id: "action:open-cron",
        kind: "action",
        title: t.palette.actionOpenCron ?? "Open cron",
        subtitle: t.palette.actionOpenCronSub ?? "Manage scheduled jobs",
        group: t.palette.groupActions ?? "Quick actions",
        icon: Clock,
        action: () => navigate("/cron"),
      },
      {
        id: "action:open-skills",
        kind: "action",
        title: t.palette.actionOpenSkills ?? "Open skills",
        subtitle: t.palette.actionOpenSkillsSub ?? "Browse installed skills",
        group: t.palette.groupActions ?? "Quick actions",
        icon: Package,
        action: () => navigate("/skills"),
      },
    ],
    [t, navigate],
  );

  // Assemble + rank.
  const allItems = useMemo<PaletteItem[]>(
    () => [
      ...actionItems,
      ...navItems,
      ...configItems,
      ...cronItems,
      ...skillItems,
      ...sessionItems,
    ],
    [actionItems, navItems, configItems, cronItems, skillItems, sessionItems],
  );

  const ranked = useMemo(() => {
    const text = (i: PaletteItem) =>
      `${i.title} ${i.subtitle ?? ""} ${i.group}`;
    return fuzzyRank(allItems, query, text).map((r) => r.item);
  }, [allItems, query]);

  const grouped = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, PaletteItem[]>();
    for (const item of ranked) {
      if (!map.has(item.group)) {
        map.set(item.group, []);
        order.push(item.group);
      }
      map.get(item.group)!.push(item);
    }
    return order.map((g) => ({ group: g, items: map.get(g) ?? [] }));
  }, [ranked]);

  const flatRanked = useMemo(() => ranked, [ranked]);

  /* ---- Cron sub-actions ---- */
  function cronActionsFor(item: CronItem) {
    const profile = item.profile ?? "default";
    const out: Array<{
      key: "run" | "pause" | "resume" | "open";
      label: string;
      run: () => Promise<void> | void;
    }> = [];
    if (item.status !== "running") {
      out.push({
        key: "run",
        label: t.palette.cronRun ?? "Run now",
        run: () => api.triggerCronJob(item.cronId, profile).then(() => undefined),
      });
    }
    if (item.status !== "paused") {
      out.push({
        key: "pause",
        label: t.palette.cronPause ?? "Pause",
        run: () => api.pauseCronJob(item.cronId, profile).then(() => undefined),
      });
    }
    if (item.status === "paused") {
      out.push({
        key: "resume",
        label: t.palette.cronResume ?? "Resume",
        run: () => api.resumeCronJob(item.cronId, profile).then(() => undefined),
      });
    }
    out.push({
      key: "open",
      label: t.palette.cronOpen ?? "Open in cron page",
      run: () => navigate("/cron"),
    });
    return out;
  }

  /* ---- Action dispatch ---- */
  const runItem = useCallback(
    async (item: PaletteItem) => {
      try {
        switch (item.kind) {
          case "nav":
          case "plugin":
            navigate(item.path);
            break;
          case "config":
            navigate(`/config?focus=${encodeURIComponent(item.key)}`);
            break;
          case "cron": {
            // Pick the most useful sub-action: run if scheduled, resume if
            // paused, open if running. One keystroke — no nested picker.
            const actions = cronActionsFor(item);
            let preferred: "run" | "pause" | "resume" | "open" = "open";
            if (item.status === "scheduled" || item.status === "completed") {
              preferred = "run";
            } else if (item.status === "paused") {
              preferred = "resume";
            } else if (item.status === "running") {
              preferred = "open";
            }
            const chosen =
              actions.find((a) => a.key === preferred) ?? actions[0];
            if (chosen) {
              await chosen.run();
              showToast(`${chosen.label}: ${item.title}`, "success");
            }
            break;
          }
          case "skill":
            navigate(`/skills?focus=${encodeURIComponent(item.skillName)}`);
            break;
          case "session":
            navigate(`/sessions/${encodeURIComponent(item.sessionId)}`);
            break;
          case "action":
            await item.action();
            break;
        }
      } catch (e) {
        showToast(`${e}`, "error");
      } finally {
        setOpen(false);
      }
    },
    // cronActionsFor depends on `t` + `navigate`; both are stable per render
    // and ESLint's exhaustive-deps would flag `t`. Including `t` here would
    // cause the callback to rebuild on every keystroke into the search box.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [navigate, showToast],
  );

  /* ---- Keyboard handler inside the palette ---- */
  const onInputKey = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) =>
          Math.min(i + 1, Math.max(flatRanked.length - 1, 0)),
        );
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Home") {
        e.preventDefault();
        setActiveIndex(0);
        return;
      }
      if (e.key === "End") {
        e.preventDefault();
        setActiveIndex(Math.max(flatRanked.length - 1, 0));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const item = flatRanked[activeIndex];
        if (item) void runItem(item);
      }
    },
    [flatRanked, activeIndex, runItem],
  );

  // Keep active index in range when results change.
  useEffect(() => {
    if (activeIndex >= flatRanked.length) {
      setActiveIndex(Math.max(flatRanked.length - 1, 0));
    }
  }, [flatRanked.length, activeIndex]);

  // Scroll the active row into view when it changes.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(
      `[data-palette-index="${activeIndex}"]`,
    );
    el?.scrollIntoView({ block: "nearest" });
  }, [activeIndex]);

  if (!open) return null;

  // Compute running offset for each item so the global activeIndex matches
  // the flat ranked list.
  let runningIndex = -1;

  return createPortal(
    <div
      className={DASHBOARD_MODAL_BACKDROP}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) setOpen(false);
      }}
      role="dialog"
      aria-modal="true"
      aria-label={t.palette.title ?? "Command palette"}
    >
      <div
        className={cn(
          DASHBOARD_MODAL_PANEL,
          "w-full max-w-2xl flex flex-col max-h-[min(80vh,640px)]",
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {/* Header / input */}
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border">
          <Search className="h-4 w-4 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={onInputKey}
            placeholder={
              t.palette.placeholder ??
              "Type a command, search pages, keys, cron…"
            }
            className="flex-1 border-0 bg-transparent focus-visible:outline-none px-0 text-sm placeholder:text-muted-foreground"
            aria-label={t.palette.searchAriaLabel ?? "Search palette"}
            autoComplete="off"
            spellCheck={false}
          />
          {loadingRemote && (
            <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
          )}
          <Button
            ghost
            size="xs"
            onClick={() => setOpen(false)}
            className="text-muted-foreground"
            aria-label={t.common.close ?? "Close"}
          >
            <kbd className="font-mono-ui text-[10px] px-1.5 py-0.5 border border-border rounded">
              esc
            </kbd>
          </Button>
        </div>

        {/* Results */}
        <div
          ref={listRef}
          className="flex-1 min-h-0 overflow-y-auto py-1"
          role="listbox"
          aria-label={t.palette.resultsAriaLabel ?? "Palette results"}
        >
          {flatRanked.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 gap-2 text-muted-foreground">
              <Search className="h-6 w-6 opacity-40" />
              <p className="text-sm">
                {query.trim()
                  ? t.palette.noResults ?? "No matches"
                  : t.palette.startTyping ?? "Start typing to search…"}
              </p>
              {pathname && (
                <p className="text-xs text-text-tertiary">
                  {t.palette.currentPath ?? "Current"}: <code>{pathname}</code>
                </p>
              )}
            </div>
          ) : (
            grouped.map((g) => (
              <div key={g.group} className="pb-1">
                <div className="px-3 pt-2 pb-1 font-mondwest text-display text-[10px] tracking-[0.18em] uppercase text-text-tertiary">
                  {g.group}
                </div>
                {g.items.map((item) => {
                  runningIndex += 1;
                  const isActive = runningIndex === activeIndex;
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.id}
                      data-palette-index={runningIndex}
                      role="option"
                      aria-selected={isActive}
                      onMouseEnter={() => setActiveIndex(runningIndex)}
                      onClick={() => void runItem(item)}
                      className={cn(
                        "flex w-full items-center gap-3 px-3 py-2 text-left text-sm transition-colors",
                        isActive
                          ? "bg-primary/10 text-foreground"
                          : "text-text-secondary hover:bg-muted/30",
                      )}
                    >
                      <span
                        className={cn(
                          "flex h-6 w-6 shrink-0 items-center justify-center rounded",
                          isActive
                            ? "bg-primary/20 text-primary"
                            : "bg-muted/40 text-muted-foreground",
                        )}
                      >
                        <Icon className="h-3.5 w-3.5" />
                      </span>
                      <span className="flex-1 min-w-0">
                        <span className="block truncate font-medium">
                          {item.title}
                        </span>
                        {item.subtitle && (
                          <span className="block truncate text-xs text-text-tertiary">
                            {item.subtitle}
                          </span>
                        )}
                      </span>
                      <PaletteItemBadge item={item} />
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>

        {/* Footer / shortcuts */}
        <div className="flex items-center justify-between gap-2 border-t border-border px-3 py-1.5 text-[10px] text-text-tertiary">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1">
              <kbd className="font-mono-ui px-1 border border-border rounded">
                ↑↓
              </kbd>
              {t.palette.shortcutNavigate ?? "navigate"}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="font-mono-ui px-1 border border-border rounded">
                ↵
              </kbd>
              {t.palette.shortcutSelect ?? "select"}
            </span>
            <span className="flex items-center gap-1">
              <kbd className="font-mono-ui px-1 border border-border rounded">
                esc
              </kbd>
              {t.palette.shortcutClose ?? "close"}
            </span>
          </div>
          <span>
            {t.palette.tipHotkey ?? "Hotkey"}:{" "}
            <kbd className="font-mono-ui px-1 border border-border rounded">
              ⌘K
            </kbd>
          </span>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ------------------------------------------------------------------ */
/*  Per-item right-aligned badge                                       */
/* ------------------------------------------------------------------ */

function PaletteItemBadge({ item }: { item: PaletteItem }) {
  if (item.kind === "config") {
    return (
      <Badge tone="outline" className="text-[10px] shrink-0">
        {truncate(serializeValue(item.currentValue), 24)}
      </Badge>
    );
  }
  if (item.kind === "cron") {
    const tone =
      item.status === "running"
        ? "success"
        : item.status === "paused"
          ? "warning"
          : item.status === "completed"
            ? "outline"
            : "secondary";
    const label =
      item.status === "running"
        ? "running"
        : item.status === "paused"
          ? "paused"
          : item.status === "completed"
            ? "done"
            : "scheduled";
    return (
      <Badge tone={tone} className="text-[10px] shrink-0 capitalize">
        {label}
      </Badge>
    );
  }
  if (item.kind === "session") {
    return (
      <ExternalLink className="h-3 w-3 text-text-tertiary shrink-0" />
    );
  }
  if (item.kind === "skill") {
    return (
      <Badge tone="secondary" className="text-[10px] shrink-0">
        skill
      </Badge>
    );
  }
  return null;
}