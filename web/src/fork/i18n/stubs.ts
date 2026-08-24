// FORK: kyroskoh/hermes-agent — empty-string stubs for non-English locales.
//
// The dashboard ships 17 locales. We deliberately do NOT translate fork
// strings into each one — that's an ongoing translation effort that
// happens out-of-band. Instead, this file declares an empty-string
// object for every locale so `useForkI18n()` can do `STUBS[locale] ?? en`
// lookups without TypeScript complaining. The hook then falls back to
// English when a key is empty.
//
// To start a real translation for a locale: copy the English map shape
// from `en.ts` into the matching entry below and fill in the strings.
// The empty-string skeleton keeps the structure visible so translators
// can see what they're translating.
//
// IMPORTANT: keep the locale keys in lockstep with
// `web/src/i18n/index.ts` so the two never drift. The compiler will
// catch a missing locale, but it'll accept a missing key inside a
// locale silently — so verify with `npm run typecheck` after editing.

import type { ForkTranslations } from "./en";

const empty: ForkTranslations = {
  personality: {
    title: "",
    intro: "",
    upstreamNotice: "",
    howLiveTitle: "",
    howLiveBody: "",
    forkProvenanceTitle: "",
    forkProvenanceBody: "",
    storageLabel: "",
    knobDefaultBadge: "",
    knobOverrideBadge: "",
    saveButton: "",
    savingButton: "",
    resetButton: "",
    resettingButton: "",
    refreshButton: "",
    errorGeneric: "",
    errorLoad: "",
    savedToast: "",
    resetToast: "",
    outOfRangeToast: "",
    rangeLabel: () => "",
  },
  fallback: {
    title: "",
    intro: "",
    upstreamNotice: "",
    primaryLabel: "",
    primaryNone: "",
    viaProvider: "",
    smartFallbackTitle: "",
    smartFallbackBody: "",
    smartFallbackCacheAge: "",
    smartFallbackCacheStale: "",
    smartFallbackNousCredits: "",
    smartFallbackCodexCredentials: "",
    smartFallbackCachePath: "",
    chainTitle: "",
    chainDescription: "",
    chainEmpty: "",
    orderDirty: "",
    saveOrder: "",
    moveUp: "",
    moveDown: "",
    remove: "",
    clearAll: "",
    addTitle: "",
    addDescription: "",
    addProviderLabel: "",
    addModelLabel: "",
    addBaseUrlLabel: "",
    addProviderPlaceholder: "",
    addModelPlaceholder: "",
    addBaseUrlPlaceholder: "",
    addSubmit: "",
    confirmRemove: "",
    confirmClear: "",
    errorGeneric: "",
    errorPrimary: "",
    errorDuplicate: "",
    skipBadgeNoCredits: "",
    skipBadgeCooldown: "",
    skipBadgeUnknown: "",
    triggersTitle: "",
    triggersIntro: "",
    triggersRateLimit: "",
    triggersUpstream: "",
    triggersUpstream429: "",
    triggersFiveXx: "",
    triggersConnection: "",
    triggersAuth: "",
  },
  backups: {
    title: "",
    intro: "",
    upstreamNotice: "",
    dashboardLink: "",
    dashboardLinkHelp: "",
    tiersTitle: "",
    tier1Title: "",
    tier1Body: "",
    tier2Title: "",
    tier2Body: "",
    tier3Title: "",
    tier3Body: "",
    tier4Title: "",
    tier4Body: "",
    restoreTitle: "",
    restoreBody: "",
    cronTitle: "",
    cronBody: "",
    logTitle: "",
    logBody: "",
    logTailCommand: "",
    logTailHelp: "",
  },
};

// Locales must match web/src/i18n/index.ts one-for-one.
export const FORK_STUB_LOCALES = [
  "en",
  "zh",
  "zh-hant",
  "ja",
  "de",
  "es",
  "fr",
  "tr",
  "uk",
  "af",
  "ko",
  "it",
  "ga",
  "pt",
  "ru",
  "hu",
  "ar",
] as const;

export type ForkLocale = (typeof FORK_STUB_LOCALES)[number];

export const FORK_STUBS: Record<ForkLocale, ForkTranslations> = {
  en: empty, // overridden by en.ts at runtime
  zh: empty,
  "zh-hant": empty,
  ja: empty,
  de: empty,
  es: empty,
  fr: empty,
  tr: empty,
  uk: empty,
  af: empty,
  ko: empty,
  it: empty,
  ga: empty,
  pt: empty,
  ru: empty,
  hu: empty,
  ar: empty,
};