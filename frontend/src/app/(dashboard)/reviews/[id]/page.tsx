"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  Alert,
  Badge,
  Button,
  Card,
  PageHeader,
  SeverityBadge,
  Spinner,
  StatusBadge,
  TextLink,
  cx,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { api } from "@/lib/api";
import type { Finding, ReviewRun, ReviewRunSummary } from "@/lib/types";
import { SEVERITIES } from "@/lib/types";

const ACTIVE = new Set(["queued", "running"]);
const POST_STATUS_TONE: Record<string, "green" | "sky" | "slate" | "amber"> = {
  posted: "green",
  in_summary: "sky",
  duplicate: "slate",
  cap_exceeded: "amber",
  below_threshold: "slate",
  low_confidence: "slate",
  not_posted: "amber",
};

export default function ReviewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const review = useQuery({
    queryKey: ["review", id],
    queryFn: () => api<ReviewRun>(`/reviews/${id}`),
    refetchInterval: (q) => (q.state.data && ACTIVE.has(q.state.data.status) ? 2_000 : false),
  });
  const prId = review.data?.pull_request.id;
  const history = useQuery({
    queryKey: ["pr-reviews", prId],
    queryFn: () => api<ReviewRunSummary[]>(`/pull-requests/${prId}/reviews`),
    enabled: Boolean(prId),
    refetchInterval: review.data && ACTIVE.has(review.data.status) ? 5_000 : false,
  });
  const rerun = useMutation({
    mutationFn: () => api<ReviewRunSummary>(`/pull-requests/${prId}/reviews`, { method: "POST" }),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["pr-reviews", prId] });
      queryClient.invalidateQueries({ queryKey: ["pull-requests"] });
      router.push(`/reviews/${run.id}`);
    },
  });

  if (review.isLoading) return <Spinner />;
  if (review.error) return <Alert>{errorMessage(review.error)}</Alert>;
  const run = review.data!;
  const pr = run.pull_request;
  const byFile = groupByFile(run.findings);
  const active = ACTIVE.has(run.status);

  return (
    <>
      <PageHeader
        title={`#${pr.number} ${pr.title}`}
        description={
          <>
            {pr.repository.full_name} · <TextLink href={pr.html_url} external>Open on GitHub</TextLink>
            {run.provider_review_url && (
              <>
                {" "}
                · <TextLink href={run.provider_review_url} external>Posted review</TextLink>
              </>
            )}
          </>
        }
        actions={
          <Button onClick={() => rerun.mutate()} loading={rerun.isPending} disabled={active}>
            Re-run review
          </Button>
        }
      />
      {rerun.error && (
        <div className="mb-4">
          <Alert>{errorMessage(rerun.error)}</Alert>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[1fr_280px]">
        <div className="space-y-6">
          <Card
            title={
              <span className="flex items-center gap-2">
                Review #{run.id} <StatusBadge status={run.status} />
                {run.status_reason && <span className="text-xs font-normal text-slate-500">{run.status_reason}</span>}
              </span>
            }
          >
            {active && (
              <div className="mb-4">
                <Alert kind="info">
                  {run.status === "queued" ? "Waiting for a worker…" : `In progress: ${run.stage_label.toLowerCase()}…`}
                </Alert>
              </div>
            )}
            {run.status === "cancelled" && run.status_reason === "superseded" && (
              <div className="mb-4">
                <Alert kind="info">A newer push arrived before this review started, so it was skipped in favor of the newer commit.</Alert>
              </div>
            )}
            {run.config_error && (
              <div className="mb-4">
                <Alert kind="warning">
                  {run.config_error}. The review used the dashboard settings for anything that could not be applied.
                </Alert>
              </div>
            )}
            {run.status === "failed" && (
              <div className="mb-4">
                <Alert>
                  <strong>Failed during “{run.stage_label}”.</strong> {run.error}
                </Alert>
              </div>
            )}
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
              <Stat label="Trigger" value={triggerLabel(run)} />
              <Stat label="Commit" value={<span className="font-mono">{run.head_sha.slice(0, 7)}</span>} />
              <Stat label="Model" value={<span className="font-mono text-xs">{run.model || "—"}</span>} />
              <Stat label="LLM key" value={run.credential_name ?? "—"} />
              <Stat label="Tokens (in / out)" value={`${run.input_tokens.toLocaleString()} / ${run.output_tokens.toLocaleString()}`} />
              <Stat label="LLM requests" value={`${run.chunk_count}${run.chunks_failed ? ` (${run.chunks_failed} failed)` : ""}`} />
              <Stat label="Duration" value={run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(1)} s` : "—"} />
              <Stat label="Queued" value={formatDate(run.created_at)} />
              <Stat label="Profile" value={run.profile} />
              <Stat label="Config" value={run.config_source || "dashboard"} />
              <Stat
                label="Scope"
                value={
                  run.incremental ? (
                    <Badge tone="sky">since {run.compare_base_sha.slice(0, 7)}</Badge>
                  ) : (
                    "full pull request"
                  )
                }
              />
            </dl>
            {run.summary && (
              <div className="mt-4 whitespace-pre-line rounded-md bg-slate-50 p-3 text-sm text-slate-800">{run.summary}</div>
            )}
          </Card>

          <Card title={`Findings (${run.findings.length})`}>
            {run.findings.length === 0 ? (
              <p className="text-sm text-slate-600">{active ? "Findings will appear when the review completes." : "No findings."}</p>
            ) : (
              <div className="space-y-6">
                <div className="flex flex-wrap gap-3 text-sm">
                  {SEVERITIES.map((s) => (
                    <span key={s} className="inline-flex items-center gap-1">
                      <SeverityBadge severity={s} /> {run.findings.filter((f) => f.severity === s).length}
                    </span>
                  ))}
                </div>
                {byFile.map(([path, findings]) => (
                  <div key={path}>
                    <h3 className="mb-2 font-mono text-xs font-semibold text-slate-700">{path}</h3>
                    <ul className="space-y-3">
                      {findings.map((f) => (
                        <FindingItem key={f.id} finding={f} />
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title="Files">
            <div className="grid gap-6 md:grid-cols-2">
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Reviewed ({run.files_reviewed.length})</h3>
                <ul className="space-y-1 text-sm">
                  {run.files_reviewed.map((f) => (
                    <li key={f.path} className="font-mono text-xs">
                      {f.path} <span className="text-emerald-700">+{f.additions}</span> <span className="text-red-700">−{f.deletions}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Not reviewed ({run.files_ignored.length})</h3>
                <ul className="space-y-1 text-sm">
                  {run.files_ignored.map((f) => (
                    <li key={f.path} className="font-mono text-xs">
                      {f.path}{" "}
                      <span className="font-sans text-slate-500">
                        {f.reason === "ignore_pattern" ? `matched “${f.pattern}”` : f.reason.replaceAll("_", " ")}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </Card>
        </div>

        <aside>
          <Card title="Review history">
            {history.isLoading ? (
              <Spinner />
            ) : (
              <ul className="space-y-2 text-sm">
                {history.data?.map((h) => (
                  <li key={h.id}>
                    <Link
                      href={`/reviews/${h.id}`}
                      className={cx("flex items-center justify-between rounded px-2 py-1 hover:bg-slate-100", h.id === run.id && "bg-slate-100")}
                    >
                      <span>
                        #{h.id} <span className="font-mono text-xs text-slate-500">{h.head_sha.slice(0, 7)}</span>
                      </span>
                      <StatusBadge status={h.status} />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </aside>
      </div>
    </>
  );
}

function triggerLabel(run: ReviewRun): string {
  if (run.trigger === "manual") return `manual${run.created_by_email ? ` (${run.created_by_email})` : ""}`;
  if (run.trigger === "push") return "new commits";
  if (run.trigger === "command") return "/reviewbot comment";
  return "pull request opened";
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="text-slate-900">{value}</dd>
    </div>
  );
}

function FindingItem({ finding: f }: { finding: Finding }) {
  return (
    <li className="rounded-md border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <SeverityBadge severity={f.severity} />
        <Badge>{f.category}</Badge>
        {f.rule_id && <Badge tone="violet">rule {f.rule_id}</Badge>}
        <span className="font-medium">{f.title}</span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span className="font-mono">
          {f.line_start ? `line ${f.line_start}${f.line_end && f.line_end !== f.line_start ? `–${f.line_end}` : ""}` : "file"}
        </span>
        <span>confidence {f.confidence.toFixed(2)}</span>
        <Badge tone={POST_STATUS_TONE[f.post_status] ?? "slate"}>{f.post_status_label}</Badge>
        {f.provider_comment_url && (
          <TextLink href={f.provider_comment_url} external>
            view comment
          </TextLink>
        )}
      </div>
      <p className="mt-2 whitespace-pre-line text-sm text-slate-800">{f.body}</p>
      {f.suggestion && <pre className="mt-2 overflow-x-auto rounded bg-slate-900 p-2 text-xs text-slate-100">{f.suggestion}</pre>}
    </li>
  );
}

function groupByFile(findings: Finding[]): [string, Finding[]][] {
  const map = new Map<string, Finding[]>();
  for (const f of findings) map.set(f.path, [...(map.get(f.path) ?? []), f]);
  const rank = (f: Finding) => SEVERITIES.indexOf(f.severity);
  return [...map.entries()]
    .map(([path, list]) => [path, list.sort((a, b) => rank(a) - rank(b))] as [string, Finding[]])
    .sort((a, b) => Math.min(...a[1].map(rank)) - Math.min(...b[1].map(rank)));
}
