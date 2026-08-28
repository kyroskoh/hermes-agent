// FORK: kyroskoh/hermes-agent — canonical English strings for fork pages.
//
// All fork-owned pages and components source their translations from this
// module via `useForkI18n()`. We deliberately do NOT extend the upstream
// `Translations` interface — adding new top-level keys forces a cascade
// edit across all 17 locale files. Keeping fork strings in a parallel
// dictionary lets us ship new pages without touching any upstream-owned
// file.
//
// Stub locales (empty-string map) are provided for every language the
// upstream dashboard ships so the type signature stays stable even when
// translations haven't been written yet. `useForkI18n` falls back to
// English when a key is empty.

export interface ForkPersonalityTranslations {
  title: string;
  intro: string;
  upstreamNotice: string;
  howLiveTitle: string;
  howLiveBody: string;
  forkProvenanceTitle: string;
  forkProvenanceBody: string;
  storageLabel: string;
  knobDefaultBadge: string;
  knobOverrideBadge: string;
  saveButton: string;
  savingButton: string;
  resetButton: string;
  resettingButton: string;
  refreshButton: string;
  errorGeneric: string;
  errorLoad: string;
  savedToast: string;
  resetToast: string;
  outOfRangeToast: string;
  rangeLabel: (min: number, max: number, defaultValue: number) => string;
}

export interface ForkFallbackTranslations {
  title: string;
  intro: string;
  upstreamNotice: string;
  primaryLabel: string;
  primaryNone: string;
  viaProvider: string;
  smartFallbackTitle: string;
  smartFallbackBody: string;
  smartFallbackCacheAge: string;
  smartFallbackCacheStale: string;
  smartFallbackNousCredits: string;
  smartFallbackCodexCredentials: string;
  smartFallbackCachePath: string;
  chainTitle: string;
  chainDescription: string;
  chainEmpty: string;
  orderDirty: string;
  saveOrder: string;
  moveUp: string;
  moveDown: string;
  remove: string;
  clearAll: string;
  addTitle: string;
  addDescription: string;
  addProviderLabel: string;
  addModelLabel: string;
  addBaseUrlLabel: string;
  addProviderPlaceholder: string;
  addModelPlaceholder: string;
  addBaseUrlPlaceholder: string;
  addSubmit: string;
  confirmRemove: string;
  confirmClear: string;
  errorGeneric: string;
  errorPrimary: string;
  errorDuplicate: string;
  skipBadgeNoCredits: string;
  skipBadgeCooldown: string;
  skipBadgeUnknown: string;
  triggersTitle: string;
  triggersIntro: string;
  triggersRateLimit: string;
  triggersUpstream: string;
  triggersUpstream429: string;
  triggersFiveXx: string;
  triggersConnection: string;
  triggersAuth: string;
}

export interface ForkBackupsTranslations {
  title: string;
  intro: string;
  upstreamNotice: string;
  dashboardLink: string;
  dashboardLinkHelp: string;
  tiersTitle: string;
  tier1Title: string;
  tier1Body: string;
  tier2Title: string;
  tier2Body: string;
  tier3Title: string;
  tier3Body: string;
  tier4Title: string;
  tier4Body: string;
  restoreTitle: string;
  restoreBody: string;
  cronTitle: string;
  cronBody: string;
  logTitle: string;
  logBody: string;
  logTailCommand: string;
  logTailHelp: string;
}

export interface ForkHonchoTranslations {
  title: string;
  intro: string;
  upstreamNotice: string;
  standaloneLink: string;
  standaloneLinkHelp: string;
  standaloneUnavailable: string;
  peerLabel: string;
  peerPlaceholder: string;
  refreshButton: string;
  refreshSpinning: string;
  matchBadgeMatched: string;
  matchBadgeUnmatched: string;
  emptyState: string;
  loadError: string;
  stateDbPathLabel: string;
  stateDbIdLabel: string;
  stateDbModelLabel: string;
  stateDbMessageCountLabel: string;
  stateDbProfileLabel: string;
  honchoSessionLabel: string;
  honchoActiveLabel: string;
  honchoInactiveLabel: string;
  noStateDbMatch: string;
  summaryMatched: string;
  summaryTotal: string;
  summaryDbCount: string;
}

export interface ForkTranslations {
  personality: ForkPersonalityTranslations;
  fallback: ForkFallbackTranslations;
  backups: ForkBackupsTranslations;
  honcho: ForkHonchoTranslations;
}

export const en: ForkTranslations = {
  personality: {
    title: "Personality",
    intro:
      "Per-profile persona knobs. Each knob is a 0–100 percentage that tunes a specific characteristic of the persona defined in SOUL.md. Changes take effect on the next session for that profile.",
    upstreamNotice:
      "Upstream-owned: SOUL.md (the prose). Fork-owned: the knob storage shape and UI surface.",
    howLiveTitle: "How live values reach the persona",
    howLiveBody:
      "Each knob is appended to the injected SOUL.md as an HTML-comment footer at runtime, so the persona sees the live value every turn. Server-side validation clamps writes to the knob's min/max range.",
    forkProvenanceTitle: "Fork provenance",
    forkProvenanceBody:
      "Knobs are a fork addition (kyroskoh/hermes-agent). Storage lives under personality.knobs.<name> in each profile's config.yaml. Backend module: hermes_cli/personality_knobs.py. UI: web/src/fork/pages/PersonalityPage.tsx.",
    storageLabel: "Storage path",
    knobDefaultBadge: "default",
    knobOverrideBadge: "override",
    saveButton: "Save",
    savingButton: "Saving…",
    resetButton: "Reset to default",
    resettingButton: "Resetting…",
    refreshButton: "Refresh",
    errorGeneric: "Something went wrong.",
    errorLoad: "Failed to load personality knobs",
    savedToast: "Saved.",
    resetToast: "Reset to default.",
    outOfRangeToast: "Value out of range — clamped.",
    rangeLabel: (min, max, defaultValue) =>
      `Range ${min}–${max}, default ${defaultValue}`,
  },
  fallback: {
    title: "Fallback providers",
    intro:
      "Edit the fallback_providers chain in ~/.hermes/config.yaml. The chain runs after the primary model fails; the agent walks it in order until one responds.",
    upstreamNotice:
      "Runtime consumer is upstream-owned (agent/agent_runtime_helpers). This page only wraps the existing CLI / REST surface.",
    primaryLabel: "Primary model",
    primaryNone: "No primary model configured.",
    viaProvider: "via",
    smartFallbackTitle: "Smart-fallback cache",
    smartFallbackBody:
      "Pre-flight check that skips entries the cache says are unavailable (no credits / cooldown / unknown) before burning a network round-trip. Refreshed every 5 minutes.",
    smartFallbackCacheAge: "Cache age:",
    smartFallbackCacheStale: "Cache stale",
    smartFallbackNousCredits: "Nous usable credits",
    smartFallbackCodexCredentials: "Codex credentials",
    smartFallbackCachePath: "Cache file",
    chainTitle: "Fallback chain",
    chainDescription:
      "Reorder with the arrow buttons, or remove an entry entirely. The next runtime failure walks this order top-to-bottom.",
    chainEmpty: "No fallback entries configured.",
    orderDirty: "Order changed locally — click Save Order to apply.",
    saveOrder: "Save order",
    moveUp: "Move up",
    moveDown: "Move down",
    remove: "Remove entry",
    clearAll: "Clear all",
    addTitle: "Add a fallback entry",
    addDescription:
      "Append a new provider/model to the end of the chain. Server-side validation rejects duplicates and entries that match the primary.",
    addProviderLabel: "Provider",
    addModelLabel: "Model",
    addBaseUrlLabel: "Base URL (optional)",
    addProviderPlaceholder: "provider (e.g. nous, openai-codex)",
    addModelPlaceholder: "model (e.g. openai/gpt-5.6-terra)",
    addBaseUrlPlaceholder: "custom OpenAI-compatible endpoint",
    addSubmit: "Add",
    confirmRemove: "Remove this fallback entry?",
    confirmClear: "Clear the entire fallback chain?",
    errorGeneric: "Something went wrong.",
    errorPrimary: "This entry matches the primary model — not allowed.",
    errorDuplicate: "An identical entry already exists in the chain.",
    skipBadgeNoCredits: "no credits",
    skipBadgeCooldown: "cooldown",
    skipBadgeUnknown: "unknown",
    triggersTitle: "Triggers",
    triggersIntro:
      "The chain is consulted only when the primary model fails with one of these classes. Auth errors rotate credentials instead — they do NOT walk the chain.",
    triggersRateLimit: "HTTP 429",
    triggersUpstream: "Upstream 429",
    triggersUpstream429: "429 from the upstream provider (counts toward rate-limit budget).",
    triggersFiveXx: "HTTP 5xx",
    triggersConnection: "Connection / network failure",
    triggersAuth: "401 / 403 (auth errors do NOT trigger the chain)",
  },
  backups: {
    title: "Backups",
    intro:
      "Four-tier backup strategy for ~/.hermes/ and Honcho. All tiers run from /etc/cron.d and log to /var/log/hermes-backup.log.",
    upstreamNotice:
      "Fork-owned: the tier scripts and the restore CLI. Upstream-owned: nothing — the backup directory structure is plain POSIX.",
    dashboardLink: "Honcho Local dashboard",
    dashboardLinkHelp:
      "The full backup dashboard with cron schedules and per-tier controls lives on the Honcho Local web dashboard.",
    tiersTitle: "Backup tiers",
    tier1Title: "Tier 1 — Curated config (every 2h, 14d retention)",
    tier1Body:
      "SOUL.md, MEMORY.md, USER.md, config.yaml, honcho.json, cron jobs. Preserves symlinks. Fastest restore — covers everything you'd hand-edit.",
    tier2Title: "Tier 2 — Honcho API snapshot (every 4h, 30d retention)",
    tier2Body:
      "Structured JSON dump of every peer card, conclusion, and message thread through the Honcho REST API. Restore with hermes-honcho-restore.sh --card <peer>.",
    tier3Title: "Tier 3 — Scripts archive (every 6h, 30d retention)",
    tier3Body:
      "Dedicated tarball of ~/.hermes/scripts/ with per-file SHA256 manifests and a diff against the previous tier. Cheap to keep, lets you audit script drift.",
    tier4Title: "Tier 4 — Full tree (daily 03:00 SGT, 30d retention)",
    tier4Body:
      "Complete tarball of ~/.hermes/ minus transient caches, plus a pg_dump of the Honcho Postgres container. Slowest, biggest — last-resort restore.",
    restoreTitle: "Restore",
    restoreBody:
      "Two CLIs ship with the tiers: hermes-restore.sh (full restore, dry-run default, requires --yes) and hermes-honcho-restore.sh (selective card restore). Both refuse to overwrite without an explicit yes.",
    cronTitle: "Cron schedule",
    cronBody:
      "All four tiers are defined in /etc/cron.d/hermes-backup*. The schedule comments list each script's retention and target path so a future operator can re-tune without reading the scripts.",
    logTitle: "Logs",
    logBody: "All tiers write to a single log file for grep-ability.",
    logTailCommand: "tail -f /var/log/hermes-backup.log",
    logTailHelp: "Run from any shell to follow tier transitions and per-script exit codes.",
  },
  honcho: {
    title: "Honcho sessions",
    intro:
      "Correlate Honcho peer/session state with the matching state.db row. Use this when you need to verify what Hermes actually persisted versus what Honcho remembers — useful for accuracy / debugging reviews, not for surfacing third-party chat content.",
    upstreamNotice:
      "Fork-owned: the bridging endpoint /api/honcho/sessions + this page. Upstream-owned: Honcho REST API (local container at :8000, workspace 'hermes').",
    standaloneLink: "Open Honcho Local",
    standaloneLinkHelp:
      "The standalone Honcho Local dashboard runs at :9000 and has the full chat history view. This page is the lightweight dashboard-side link layer.",
    standaloneUnavailable: "Honcho Local dashboard URL unknown (set HONCHO_LOCAL_DASHBOARD_URL to override)",
    peerLabel: "Honcho peer",
    peerPlaceholder: "e.g. Kyros, Wilnice",
    refreshButton: "Refresh",
    refreshSpinning: "Refreshing…",
    matchBadgeMatched: "matched",
    matchBadgeUnmatched: "no state.db row",
    emptyState: "No sessions for this peer (or Honcho is unreachable).",
    loadError: "Failed to load Honcho sessions. Is the local Honcho container up?",
    stateDbPathLabel: "state.db",
    stateDbIdLabel: "session id",
    stateDbModelLabel: "model",
    stateDbMessageCountLabel: "messages",
    stateDbProfileLabel: "profile",
    honchoSessionLabel: "Honcho session",
    honchoActiveLabel: "active",
    honchoInactiveLabel: "ended",
    noStateDbMatch: "no state.db row found (±60 min window)",
    summaryMatched: "matched",
    summaryTotal: "sessions",
    summaryDbCount: "DBs scanned",
  },
};