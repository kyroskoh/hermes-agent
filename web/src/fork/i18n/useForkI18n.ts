// FORK: kyroskoh/hermes-agent — `useForkI18n()` hook for fork pages.
//
// Fork pages get their strings from this hook rather than the upstream
// `useI18n()` because the upstream `Translations` interface is shared
// across all 17 locales — adding a new top-level key there forces a
// cascade edit across every locale file. Fork strings live in their
// own dictionary so a new fork page can ship without touching any
// upstream-owned i18n file.
//
// Resolution order:
//   1. Current locale's fork stub (empty for non-English)
//   2. English (the canonical source)
// For each key, the first non-empty value wins. This lets translators
// partially localize a page without redoing the whole thing — and it
// guarantees the page renders even if a locale's map is entirely empty.

import { useI18n } from "@/i18n/context";
import { en } from "./en";
import { FORK_STUBS, type ForkLocale } from "./stubs";
import type { ForkTranslations } from "./en";

function isForkLocale(value: string): value is ForkLocale {
  return (FORK_STUBS as Record<string, unknown>)[value] !== undefined;
}

function pick(
  localeMap: ForkTranslations,
  englishMap: ForkTranslations,
  accessor: (m: ForkTranslations) => string,
): string {
  // `localeMap` may have empty strings (the stub default). Walk it and
  // fall back to English whenever the picked value is empty / undefined.
  const localized = accessor(localeMap);
  const fallback = accessor(englishMap);
  if (typeof localized === "string" && localized.length > 0) return localized;
  return fallback;
}

export function useForkI18n(): {
  locale: ForkLocale;
  personality: import("./en").ForkPersonalityTranslations;
  fallback: import("./en").ForkFallbackTranslations;
  backups: import("./en").ForkBackupsTranslations;
  honcho: import("./en").ForkHonchoTranslations;
} {
  const { locale } = useI18n();
  const safeLocale: ForkLocale = isForkLocale(locale) ? locale : "en";
  // English is the source of truth; everything else is a partial overlay.
  const localizedMap: ForkTranslations =
    safeLocale === "en" ? en : { ...en, ...FORK_STUBS[safeLocale] };

  return {
    locale: safeLocale,
    personality: {
      title: pick(localizedMap, en, (m) => m.personality.title),
      intro: pick(localizedMap, en, (m) => m.personality.intro),
      upstreamNotice: pick(localizedMap, en, (m) => m.personality.upstreamNotice),
      howLiveTitle: pick(localizedMap, en, (m) => m.personality.howLiveTitle),
      howLiveBody: pick(localizedMap, en, (m) => m.personality.howLiveBody),
      forkProvenanceTitle: pick(localizedMap, en, (m) => m.personality.forkProvenanceTitle),
      forkProvenanceBody: pick(localizedMap, en, (m) => m.personality.forkProvenanceBody),
      storageLabel: pick(localizedMap, en, (m) => m.personality.storageLabel),
      knobDefaultBadge: pick(localizedMap, en, (m) => m.personality.knobDefaultBadge),
      knobOverrideBadge: pick(localizedMap, en, (m) => m.personality.knobOverrideBadge),
      saveButton: pick(localizedMap, en, (m) => m.personality.saveButton),
      savingButton: pick(localizedMap, en, (m) => m.personality.savingButton),
      resetButton: pick(localizedMap, en, (m) => m.personality.resetButton),
      resettingButton: pick(localizedMap, en, (m) => m.personality.resettingButton),
      refreshButton: pick(localizedMap, en, (m) => m.personality.refreshButton),
      errorGeneric: pick(localizedMap, en, (m) => m.personality.errorGeneric),
      errorLoad: pick(localizedMap, en, (m) => m.personality.errorLoad),
      savedToast: pick(localizedMap, en, (m) => m.personality.savedToast),
      resetToast: pick(localizedMap, en, (m) => m.personality.resetToast),
      outOfRangeToast: pick(localizedMap, en, (m) => m.personality.outOfRangeToast),
      rangeLabel: (min, max, defaultValue) =>
        localizedMap.personality.rangeLabel?.(min, max, defaultValue) ??
        en.personality.rangeLabel(min, max, defaultValue),
    },
    fallback: {
      title: pick(localizedMap, en, (m) => m.fallback.title),
      intro: pick(localizedMap, en, (m) => m.fallback.intro),
      upstreamNotice: pick(localizedMap, en, (m) => m.fallback.upstreamNotice),
      primaryLabel: pick(localizedMap, en, (m) => m.fallback.primaryLabel),
      primaryNone: pick(localizedMap, en, (m) => m.fallback.primaryNone),
      viaProvider: pick(localizedMap, en, (m) => m.fallback.viaProvider),
      smartFallbackTitle: pick(localizedMap, en, (m) => m.fallback.smartFallbackTitle),
      smartFallbackBody: pick(localizedMap, en, (m) => m.fallback.smartFallbackBody),
      smartFallbackCacheAge: pick(localizedMap, en, (m) => m.fallback.smartFallbackCacheAge),
      smartFallbackCacheStale: pick(localizedMap, en, (m) => m.fallback.smartFallbackCacheStale),
      smartFallbackNousCredits: pick(localizedMap, en, (m) => m.fallback.smartFallbackNousCredits),
      smartFallbackCodexCredentials: pick(localizedMap, en, (m) => m.fallback.smartFallbackCodexCredentials),
      smartFallbackCachePath: pick(localizedMap, en, (m) => m.fallback.smartFallbackCachePath),
      chainTitle: pick(localizedMap, en, (m) => m.fallback.chainTitle),
      chainDescription: pick(localizedMap, en, (m) => m.fallback.chainDescription),
      chainEmpty: pick(localizedMap, en, (m) => m.fallback.chainEmpty),
      orderDirty: pick(localizedMap, en, (m) => m.fallback.orderDirty),
      saveOrder: pick(localizedMap, en, (m) => m.fallback.saveOrder),
      moveUp: pick(localizedMap, en, (m) => m.fallback.moveUp),
      moveDown: pick(localizedMap, en, (m) => m.fallback.moveDown),
      remove: pick(localizedMap, en, (m) => m.fallback.remove),
      clearAll: pick(localizedMap, en, (m) => m.fallback.clearAll),
      addTitle: pick(localizedMap, en, (m) => m.fallback.addTitle),
      addDescription: pick(localizedMap, en, (m) => m.fallback.addDescription),
      addProviderLabel: pick(localizedMap, en, (m) => m.fallback.addProviderLabel),
      addModelLabel: pick(localizedMap, en, (m) => m.fallback.addModelLabel),
      addBaseUrlLabel: pick(localizedMap, en, (m) => m.fallback.addBaseUrlLabel),
      addProviderPlaceholder: pick(localizedMap, en, (m) => m.fallback.addProviderPlaceholder),
      addModelPlaceholder: pick(localizedMap, en, (m) => m.fallback.addModelPlaceholder),
      addBaseUrlPlaceholder: pick(localizedMap, en, (m) => m.fallback.addBaseUrlPlaceholder),
      addSubmit: pick(localizedMap, en, (m) => m.fallback.addSubmit),
      confirmRemove: pick(localizedMap, en, (m) => m.fallback.confirmRemove),
      confirmClear: pick(localizedMap, en, (m) => m.fallback.confirmClear),
      errorGeneric: pick(localizedMap, en, (m) => m.fallback.errorGeneric),
      errorPrimary: pick(localizedMap, en, (m) => m.fallback.errorPrimary),
      errorDuplicate: pick(localizedMap, en, (m) => m.fallback.errorDuplicate),
      skipBadgeNoCredits: pick(localizedMap, en, (m) => m.fallback.skipBadgeNoCredits),
      skipBadgeCooldown: pick(localizedMap, en, (m) => m.fallback.skipBadgeCooldown),
      skipBadgeUnknown: pick(localizedMap, en, (m) => m.fallback.skipBadgeUnknown),
      triggersTitle: pick(localizedMap, en, (m) => m.fallback.triggersTitle),
      triggersIntro: pick(localizedMap, en, (m) => m.fallback.triggersIntro),
      triggersRateLimit: pick(localizedMap, en, (m) => m.fallback.triggersRateLimit),
      triggersUpstream: pick(localizedMap, en, (m) => m.fallback.triggersUpstream),
      triggersUpstream429: pick(localizedMap, en, (m) => m.fallback.triggersUpstream429),
      triggersFiveXx: pick(localizedMap, en, (m) => m.fallback.triggersFiveXx),
      triggersConnection: pick(localizedMap, en, (m) => m.fallback.triggersConnection),
      triggersAuth: pick(localizedMap, en, (m) => m.fallback.triggersAuth),
    },
    backups: {
      title: pick(localizedMap, en, (m) => m.backups.title),
      intro: pick(localizedMap, en, (m) => m.backups.intro),
      upstreamNotice: pick(localizedMap, en, (m) => m.backups.upstreamNotice),
      dashboardLink: pick(localizedMap, en, (m) => m.backups.dashboardLink),
      dashboardLinkHelp: pick(localizedMap, en, (m) => m.backups.dashboardLinkHelp),
      tiersTitle: pick(localizedMap, en, (m) => m.backups.tiersTitle),
      tier1Title: pick(localizedMap, en, (m) => m.backups.tier1Title),
      tier1Body: pick(localizedMap, en, (m) => m.backups.tier1Body),
      tier2Title: pick(localizedMap, en, (m) => m.backups.tier2Title),
      tier2Body: pick(localizedMap, en, (m) => m.backups.tier2Body),
      tier3Title: pick(localizedMap, en, (m) => m.backups.tier3Title),
      tier3Body: pick(localizedMap, en, (m) => m.backups.tier3Body),
      tier4Title: pick(localizedMap, en, (m) => m.backups.tier4Title),
      tier4Body: pick(localizedMap, en, (m) => m.backups.tier4Body),
      restoreTitle: pick(localizedMap, en, (m) => m.backups.restoreTitle),
      restoreBody: pick(localizedMap, en, (m) => m.backups.restoreBody),
      cronTitle: pick(localizedMap, en, (m) => m.backups.cronTitle),
      cronBody: pick(localizedMap, en, (m) => m.backups.cronBody),
      logTitle: pick(localizedMap, en, (m) => m.backups.logTitle),
      logBody: pick(localizedMap, en, (m) => m.backups.logBody),
      logTailCommand: pick(localizedMap, en, (m) => m.backups.logTailCommand),
      logTailHelp: pick(localizedMap, en, (m) => m.backups.logTailHelp),
    },
    honcho: {
      title: pick(localizedMap, en, (m) => m.honcho.title),
      intro: pick(localizedMap, en, (m) => m.honcho.intro),
      upstreamNotice: pick(localizedMap, en, (m) => m.honcho.upstreamNotice),
      standaloneLink: pick(localizedMap, en, (m) => m.honcho.standaloneLink),
      standaloneLinkHelp: pick(localizedMap, en, (m) => m.honcho.standaloneLinkHelp),
      standaloneUnavailable: pick(localizedMap, en, (m) => m.honcho.standaloneUnavailable),
      peerLabel: pick(localizedMap, en, (m) => m.honcho.peerLabel),
      peerPlaceholder: pick(localizedMap, en, (m) => m.honcho.peerPlaceholder),
      refreshButton: pick(localizedMap, en, (m) => m.honcho.refreshButton),
      refreshSpinning: pick(localizedMap, en, (m) => m.honcho.refreshSpinning),
      matchBadgeMatched: pick(localizedMap, en, (m) => m.honcho.matchBadgeMatched),
      matchBadgeUnmatched: pick(localizedMap, en, (m) => m.honcho.matchBadgeUnmatched),
      emptyState: pick(localizedMap, en, (m) => m.honcho.emptyState),
      loadError: pick(localizedMap, en, (m) => m.honcho.loadError),
      stateDbPathLabel: pick(localizedMap, en, (m) => m.honcho.stateDbPathLabel),
      stateDbIdLabel: pick(localizedMap, en, (m) => m.honcho.stateDbIdLabel),
      stateDbModelLabel: pick(localizedMap, en, (m) => m.honcho.stateDbModelLabel),
      stateDbMessageCountLabel: pick(localizedMap, en, (m) => m.honcho.stateDbMessageCountLabel),
      stateDbProfileLabel: pick(localizedMap, en, (m) => m.honcho.stateDbProfileLabel),
      honchoSessionLabel: pick(localizedMap, en, (m) => m.honcho.honchoSessionLabel),
      honchoActiveLabel: pick(localizedMap, en, (m) => m.honcho.honchoActiveLabel),
      honchoInactiveLabel: pick(localizedMap, en, (m) => m.honcho.honchoInactiveLabel),
      noStateDbMatch: pick(localizedMap, en, (m) => m.honcho.noStateDbMatch),
      summaryMatched: pick(localizedMap, en, (m) => m.honcho.summaryMatched),
      summaryTotal: pick(localizedMap, en, (m) => m.honcho.summaryTotal),
      summaryDbCount: pick(localizedMap, en, (m) => m.honcho.summaryDbCount),
    },
  };
}