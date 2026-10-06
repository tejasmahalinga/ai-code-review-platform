"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Alert, Badge, Card, Field, PageHeader, Select, Spinner, TextLink, buttonClass, cx, errorMessage } from "@/components/ui";
import { api } from "@/lib/api";
import { useRole } from "@/lib/hooks";
import { BudgetBar } from "@/components/budget";
import type { LLMCredential, UsageReport, UsageRow } from "@/lib/types";
import { formatUsd } from "@/lib/types";

const RANGES = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
];

const num = (v: string | number | null | undefined) => (v === null || v === undefined ? 0 : Number(v));

function rangeParams(days: number, now: number): URLSearchParams {
  const to = new Date(now);
  const from = new Date(to.getTime() - days * 86_400_000);
  from.setUTCHours(0, 0, 0, 0);
  return new URLSearchParams({ from: from.toISOString(), to: to.toISOString() });
}

export default function UsagePage() {
  const { user, isAdmin } = useRole();
  const [days, setDays] = useState(30);
  // Fixed per page view, so the period and the query keys stay stable across renders.
  const [now] = useState(() => Date.now());
  const params = rangeParams(days, now);
  const query = (groupBy: string) => ({
    queryKey: ["usage", days, groupBy],
    queryFn: () => api<UsageReport>(`/usage?${params.toString()}&group_by=${groupBy}`),
    enabled: isAdmin,
  });
  const byDay = useQuery(query("day"));
  const byRepo = useQuery(query("repository"));
  const byModel = useQuery(query("model"));
  const keys = useQuery({ queryKey: ["llm-credentials"], queryFn: () => api<LLMCredential[]>("/llm-credentials"), enabled: isAdmin });

  if (user && !isAdmin) return <Alert>Only admins can view usage and cost.</Alert>;
  const totals = byDay.data?.totals;
  const csvUrl = `/api/v1/usage?${params.toString()}&group_by=day,repository,credential,model&export=csv`;

  return (
    <>
      <PageHeader
        title="Usage & cost"
        description={
          <>
            LLM spend across all repositories. Prices come from the{" "}
            <TextLink href="/settings/keys#prices">model price table</TextLink>
            {byDay.data && ` (defaults as of ${byDay.data.prices_as_of})`}.
          </>
        }
        actions={
          <>
            <Field label="Period">
              <Select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period">
                {RANGES.map((r) => (
                  <option key={r.days} value={r.days}>
                    {r.label}
                  </option>
                ))}
              </Select>
            </Field>
            <a className={cx(buttonClass("secondary"), "self-end")} href={csvUrl} download>
              Download CSV
            </a>
          </>
        }
      />
      {byDay.error && <Alert>{errorMessage(byDay.error)}</Alert>}
      {!totals ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="Cost" value={formatUsd(totals.cost_usd)} hint={totals.unpriced ? `${plural(totals.unpriced, "call")} without a price` : undefined} />
            <Metric label="Reviews" value={totals.reviews.toLocaleString()} hint={totals.reviews ? `${formatUsd(num(totals.cost_usd) / totals.reviews, 3)} per review` : undefined} />
            <Metric label="Tokens in / out" value={`${compact(totals.input_tokens)} / ${compact(totals.output_tokens)}`} />
            <Metric label="LLM requests" value={totals.requests.toLocaleString()} hint={totals.errors ? `${totals.errors} failed` : undefined} />
          </div>
          {totals.unpriced > 0 && (
            <Alert kind="warning">
              {plural(totals.unpriced, "LLM call")} used models without a price, so their cost is unknown and not counted in budgets.{" "}
              <TextLink href="/settings/keys#prices">Add prices</TextLink>, then recalculate.
            </Alert>
          )}
          <Budgets keys={keys.data} />
          <Card title="Daily cost">
            <DailyChart rows={byDay.data?.rows ?? []} days={days} now={now} />
          </Card>
          <div className="grid gap-6 lg:grid-cols-2">
            <Breakdown title="By repository" rows={byRepo.data?.rows} label={(r) => r.repository__full_name ?? "(deleted repository)"} />
            <Breakdown title="By model" rows={byModel.data?.rows} label={(r) => r.model ?? "—"} />
          </div>
        </div>
      )}
    </>
  );
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

function compact(n: number): string {
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-900">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

function Budgets({ keys }: { keys?: LLMCredential[] }) {
  const active = (keys ?? []).filter((k) => k.status !== "revoked");
  if (active.length === 0) return null;
  return (
    <Card title="Monthly budgets (UTC calendar month)" actions={<TextLink href="/settings/keys">Edit budgets</TextLink>}>
      <ul className="divide-y divide-slate-100">
        {active.map((k) => (
          <li key={k.id} className="flex flex-wrap items-center justify-between gap-3 py-2 text-sm">
            <span className="font-medium">
              {k.name} {k.budget.state === "warning" && <Badge tone="amber">over 80%</Badge>}
              {k.budget.state === "exceeded" && <Badge tone="red">budget reached</Badge>}
            </span>
            <BudgetBar budget={k.budget} />
          </li>
        ))}
      </ul>
    </Card>
  );
}

function DailyChart({ rows, days, now }: { rows: UsageRow[]; days: number; now: number }) {
  const byDay = new Map(rows.map((r) => [r.day!.slice(0, 10), r]));
  const series = Array.from({ length: days }, (_, i) => {
    const d = new Date(now - (days - 1 - i) * 86_400_000).toISOString().slice(0, 10);
    const row = byDay.get(d);
    return { day: d, cost: num(row?.cost_usd), reviews: row?.reviews ?? 0 };
  });
  const max = Math.max(...series.map((s) => s.cost), 0);
  if (max === 0) return <p className="text-sm text-slate-500">No priced usage in this period.</p>;
  return (
    <div>
      <div className="flex h-40 items-end gap-px" aria-label="Daily cost chart">
        {series.map((s) => (
          <div key={s.day} className="group relative flex h-full flex-1 items-end">
            <div className="w-full rounded-t bg-sky-500 group-hover:bg-sky-700" style={{ height: `${(s.cost / max) * 100}%`, minHeight: s.cost > 0 ? 2 : 0 }} />
            <div className="pointer-events-none absolute bottom-full left-1/2 z-10 mb-1 hidden -translate-x-1/2 whitespace-nowrap rounded bg-slate-900 px-2 py-1 text-xs text-white group-hover:block">
              {s.day}: {formatUsd(s.cost, 4)} · {s.reviews} reviews
            </div>
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-xs text-slate-500">
        <span>{series[0].day}</span>
        <span>max {formatUsd(max)}/day</span>
        <span>{series[series.length - 1].day}</span>
      </div>
    </div>
  );
}

function Breakdown({ title, rows, label }: { title: string; rows?: UsageRow[]; label: (r: UsageRow) => string }) {
  const sorted = [...(rows ?? [])].sort((a, b) => num(b.cost_usd) - num(a.cost_usd) || b.input_tokens - a.input_tokens);
  return (
    <Card title={title}>
      {sorted.length === 0 ? (
        <p className="text-sm text-slate-500">No usage in this period.</p>
      ) : (
        <table className="min-w-full text-sm">
          <thead className="text-left text-xs font-medium uppercase tracking-wide text-slate-500">
            <tr>
              <th className="py-1 pr-3">Name</th>
              <th className="py-1 pr-3 text-right">Reviews</th>
              <th className="py-1 pr-3 text-right">Tokens</th>
              <th className="py-1 text-right">Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {sorted.slice(0, 15).map((r, i) => (
              <tr key={i}>
                <td className="max-w-56 truncate py-1 pr-3 font-mono text-xs">{label(r)}</td>
                <td className="py-1 pr-3 text-right">{r.reviews}</td>
                <td className="py-1 pr-3 text-right">{compact(r.input_tokens + r.output_tokens)}</td>
                <td className="py-1 text-right">
                  {formatUsd(r.cost_usd)}
                  {r.unpriced > 0 && <span className="ml-1 text-xs text-amber-700" title={`${r.unpriced} unpriced calls`}>+?</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
