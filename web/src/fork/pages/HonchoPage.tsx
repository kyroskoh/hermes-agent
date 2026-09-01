// FORK: kyroskoh/hermes-agent
// Honcho <-> state.db bridging page.
//
// Lists a Honcho peer's sessions with the matching state.db row for each
// one (matched by sessions.id when the gateway wrote the same id into
// Honcho, falling back to chat_id LIKE within a ±60 minute window of the
// Honcho session's created_at).
//
// Operator-scoped. The dashboard mounts this page on /honcho via App.tsx;
// entry shows up in the sidebar via BUILTIN_NAV_REST. The standalone
// Honcho Local dashboard (port 9000) is surfaced as an external link so
// the operator can pivot from this lightweight bridge view into the full
// chat history UI without losing context.

import { useCallback, useEffect, useState } from "react";
import { Database, ExternalLink, RefreshCw, XCircle } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { useForkI18n } from "@/fork/i18n/useForkI18n";

interface HonchoStateDbMatch {
  id: string;
  source?: string | null;
  user_id?: string | null;
  session_key?: string | null;
  chat_id?: string | null;
  profile_name?: string | null;
  display_name?: string | null;
  model?: string | null;
  started_at?: number | null;
  ended_at?: number | null;
  end_reason?: string | null;
  message_count?: number | null;
  db_path: string;
}

interface HonchoSessionEntry {
  honcho_session_id: string;
  peer: string;
  created_at: string;
  is_active?: boolean;
  metadata: Record<string, unknown>;
  state_db_match: HonchoStateDbMatch | null;
}

interface HonchoSessionsResponse {
  peer: string;
  honcho_base?: string;
  workspace?: string;
  sessions: HonchoSessionEntry[];
  matched_count: number;
  total_count: number;
}

interface HonchoPeer {
  id: string;
  created_at?: string;
  metadata?: Record<string, unknown>;
}

interface HonchoPeersResponse {
  honcho_base?: string;
  workspace?: string;
  peers: HonchoPeer[];
}

const HONCHO_LOCAL_DASHBOARD_URL =
  (typeof window !== "undefined" &&
    ((window as unknown as { __HONCHO_LOCAL_DASHBOARD_URL__?: string })
      .__HONCHO_LOCAL_DASHBOARD_URL__ ?? null)) ||
  "/honcho";

export default function HonchoPage() {
  const { honcho: t } = useForkI18n();
  const SCHEMA_VERSION = "1";

  const [peer, setPeer] = useState<string>("Kyros");
  const [peers, setPeers] = useState<HonchoPeer[]>([]);
  const [sessions, setSessions] = useState<HonchoSessionEntry[]>([]);
  const [matchedCount, setMatchedCount] = useState<number>(0);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPeers = useCallback(async () => {
    try {
      const res = await fetch(
        "/api/honcho/peers?workspace=hermes",
        { credentials: "include" },
      );
      if (!res.ok) return;
      const data = (await res.json()) as HonchoPeersResponse;
      setPeers(Array.isArray(data.peers) ? data.peers : []);
    } catch {
      // Non-fatal — peer list is a convenience, not required for the page.
    }
  }, []);

  const fetchSessions = useCallback(async (peerName: string) => {
    if (!peerName.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/honcho/sessions?peer=${encodeURIComponent(peerName.trim())}&limit=50&workspace=hermes`,
        { credentials: "include" },
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data = (await res.json()) as HonchoSessionsResponse;
      setSessions(Array.isArray(data.sessions) ? data.sessions : []);
      setMatchedCount(data.matched_count ?? 0);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setSessions([]);
      setMatchedCount(0);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchPeers();
  }, [fetchPeers]);

  useEffect(() => {
    fetchSessions(peer);
  }, [peer, fetchSessions]);

  const handleRefresh = useCallback(() => {
    fetchPeers();
    fetchSessions(peer);
  }, [fetchPeers, fetchSessions, peer]);

  return (
    <div className="space-y-6 p-6" data-honcho-schema={SCHEMA_VERSION}>
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">{t.title}</h1>
        <p className="text-muted-foreground max-w-3xl text-sm">{t.intro}</p>
        <p className="text-muted-foreground/80 text-xs italic">
          {t.upstreamNotice}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Database className="h-4 w-4" />
            {t.peerLabel}
          </CardTitle>
          <CardDescription className="flex flex-wrap items-center gap-2">
            <span>{HONCHO_LOCAL_DASHBOARD_URL}</span>
            <a
              href={HONCHO_LOCAL_DASHBOARD_URL}
              target="_blank"
              rel="noreferrer"
              className="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs underline"
            >
              <ExternalLink className="h-3 w-3" />
              {t.standaloneLink}
            </a>
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground text-xs">
                {t.peerLabel}
              </span>
              <input
                list="honcho-peer-options"
                value={peer}
                onChange={(e) => setPeer(e.target.value)}
                placeholder={t.peerPlaceholder}
                className="border-input bg-background ring-offset-background placeholder:text-muted-foreground focus-visible:ring-ring w-64 rounded-md border px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2"
              />
              <datalist id="honcho-peer-options">
                {peers.map((p) => (
                  <option key={p.id} value={p.id} />
                ))}
              </datalist>
            </label>
            <Button
              type="button"
              outlined
              size="sm"
              onClick={handleRefresh}
              disabled={loading}
            >
              <RefreshCw
                className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
              />
              {loading ? t.refreshSpinning : t.refreshButton}
            </Button>
          </div>

          {error ? (
            <div
              role="alert"
              className="border-destructive/40 bg-destructive/10 text-destructive flex items-start gap-2 rounded-md border p-3 text-sm"
            >
              <XCircle className="mt-0.5 h-4 w-4" />
              <div>
                <div className="font-medium">{t.loadError}</div>
                <div className="text-muted-foreground text-xs">{error}</div>
              </div>
            </div>
          ) : null}

          {sessions.length > 0 ? (
            <p className="text-muted-foreground text-xs">
              {t.summaryMatched}: {matchedCount} / {t.summaryTotal}:{" "}
              {sessions.length}
            </p>
          ) : !loading && !error ? (
            <p className="text-muted-foreground text-xs italic">
              {t.emptyState}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <div className="space-y-3">
        {sessions.map((s) => (
          <HonchoSessionRow
            key={s.honcho_session_id}
            entry={s}
            t={t}
          />
        ))}
      </div>
    </div>
  );
}

function HonchoSessionRow({
  entry,
  t,
}: {
  entry: HonchoSessionEntry;
  t: ReturnType<typeof useForkI18n>["honcho"];
}) {
  const m = entry.state_db_match;
  const dbBaseName = m?.db_path?.split("/").filter(Boolean).pop() ?? "-";
  const stateDbLink = m
    ? `/sessions/${encodeURIComponent(m.id)}?db=${encodeURIComponent(m.db_path)}`
    : null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <div className="flex items-center gap-2">
            <Database className="text-muted-foreground h-4 w-4" />
            <span className="font-mono text-xs">
              {entry.honcho_session_id}
            </span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <span
              className={`inline-flex items-center rounded-full border px-2 py-0.5 font-medium ${
                m
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
                  : "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300"
              }`}
            >
              {m ? t.matchBadgeMatched : t.matchBadgeUnmatched}
            </span>
            <span className="text-muted-foreground">
              {entry.is_active ? t.honchoActiveLabel : t.honchoInactiveLabel}
            </span>
          </div>
        </CardTitle>
        <CardDescription className="font-mono text-xs">
          {t.honchoSessionLabel}: {entry.honcho_session_id}
          {" · "}
          {entry.created_at
            ? new Date(entry.created_at).toLocaleString()
            : "?"}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-xs">
        {m ? (
          <div className="bg-muted/40 grid gap-x-4 gap-y-1 rounded-md border p-3 sm:grid-cols-2">
            <Field
              label={t.stateDbPathLabel}
              value={
                stateDbLink ? (
                  <a
                    href={stateDbLink}
                    className="text-foreground underline hover:no-underline"
                  >
                    {dbBaseName}
                  </a>
                ) : (
                  dbBaseName
                )
              }
            />
            <Field label={t.stateDbIdLabel} value={m.id ?? "-"} mono />
            <Field label={t.stateDbModelLabel} value={m.model ?? "-"} />
            <Field
              label={t.stateDbMessageCountLabel}
              value={String(m.message_count ?? 0)}
            />
            <Field
              label={t.stateDbProfileLabel}
              value={m.profile_name ?? m.display_name ?? "-"}
            />
            <Field
              label="started_at"
              value={
                m.started_at
                  ? new Date(m.started_at * 1000).toLocaleString()
                  : "?"
              }
              mono
            />
          </div>
        ) : (
          <div className="bg-muted/30 text-muted-foreground rounded-md border p-3 italic">
            {t.noStateDbMatch}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Field({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-muted-foreground text-[10px] uppercase tracking-wide">
        {label}
      </span>
      <span
        className={`text-foreground ${mono ? "font-mono text-xs" : ""}`}
      >
        {value}
      </span>
    </div>
  );
}
