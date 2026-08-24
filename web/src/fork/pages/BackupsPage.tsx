// FORK: kyroskoh/hermes-agent
// Backups overview. Surfaces the 4-tier backup strategy and the
// restore CLIs. The interactive dashboard (cron schedules, last-run
// status, manual-trigger buttons) is on the Honcho Local web dashboard
// at /backups; this page is the lightweight summary reachable from
// the main Hermes dashboard.

import { Database, FileArchive, Folder, Info, RefreshCw } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { useForkI18n } from "@/fork/i18n/useForkI18n";

export default function BackupsPage() {
  const { backups: t } = useForkI18n();
  // Schema-bumped 2026-08-24 to flush any stale browser caches that
  // were holding a pre-fork BackupsPage bundle (the older launcher
  // page had different card content). Surfaced as a data attribute so
  // a future inspector can see which version rendered.
  const SCHEMA_VERSION = "2";

  const tiers = [
    {
      key: "tier1",
      title: t.tier1Title,
      body: t.tier1Body,
      icon: FileArchive,
    },
    {
      key: "tier2",
      title: t.tier2Title,
      body: t.tier2Body,
      icon: Database,
    },
    {
      key: "tier3",
      title: t.tier3Title,
      body: t.tier3Body,
      icon: Folder,
    },
    {
      key: "tier4",
      title: t.tier4Title,
      body: t.tier4Body,
      icon: RefreshCw,
    },
  ] as const;

  return (
    <div className="space-y-6 p-6" data-backups-schema={SCHEMA_VERSION}>
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">{t.title}</h1>
        <p className="text-muted-foreground max-w-3xl text-sm">{t.intro}</p>
        <p className="text-muted-foreground/80 text-xs italic">
          {t.upstreamNotice}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.tiersTitle}</CardTitle>
          <CardDescription>
            {tierCount(tiers.length)} tiers running from{" "}
            <span className="font-mono">/etc/cron.d</span>.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {tiers.map((tier) => {
            const Icon = tier.icon;
            return (
              <div
                key={tier.key}
                className="bg-muted/30 space-y-1 rounded-md border p-3"
              >
                <div className="flex items-center gap-2">
                  <Icon className="text-muted-foreground h-4 w-4" />
                  <span className="text-sm font-medium">{tier.title}</span>
                </div>
                <p className="text-muted-foreground text-xs">{tier.body}</p>
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.restoreTitle}</CardTitle>
          <CardDescription>{t.restoreBody}</CardDescription>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t.cronTitle}</CardTitle>
          <CardDescription>{t.cronBody}</CardDescription>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Info className="h-4 w-4" />
            {t.logTitle}
          </CardTitle>
          <CardDescription>{t.logBody}</CardDescription>
        </CardHeader>
        <CardContent>
          <pre className="bg-muted overflow-x-auto rounded-md p-3 font-mono text-xs">
            {t.logTailCommand}
          </pre>
          <p className="text-muted-foreground mt-2 text-xs">{t.logTailHelp}</p>
        </CardContent>
      </Card>
    </div>
  );
}

function tierCount(n: number): string {
  return n === 1 ? "1" : String(n);
}