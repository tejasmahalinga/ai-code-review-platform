"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { Alert, Badge, Button, Empty, Field, Input, PageHeader, Select, Spinner, errorMessage, formatDate } from "@/components/ui";
import { api } from "@/lib/api";
import { useRole } from "@/lib/hooks";
import type { AuditEvent, Paginated } from "@/lib/types";

const CATEGORIES = [
  { value: "auth", label: "Sign-in" },
  { value: "user", label: "Users" },
  { value: "invite", label: "Invites" },
  { value: "credential", label: "LLM keys" },
  { value: "repository", label: "Repositories" },
  { value: "integration", label: "Integrations" },
  { value: "review", label: "Reviews" },
  { value: "finding", label: "Findings" },
];

const TONE: Record<string, "red" | "amber" | "green" | "slate" | "sky" | "violet"> = {
  "auth.login_failed": "red",
  "credential.revoked": "red",
  "user.deactivated": "red",
  "integration.github_disconnected": "red",
  "credential.rotated": "amber",
  "user.role_changed": "amber",
  "repository.settings_changed": "sky",
  "auth.login": "green",
};

export default function AuditPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <Audit />
    </Suspense>
  );
}

function Audit() {
  const { user, isAdmin } = useRole();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filters = { action: params.get("action") ?? "", actor: params.get("actor") ?? "" };
  const setFilter = (key: keyof typeof filters, value: string) => {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    router.replace(`${pathname}?${next.toString()}`);
  };
  const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
  const events = useInfiniteQuery({
    queryKey: ["audit-events", filters],
    initialPageParam: `/audit-events?${query.toString()}`,
    queryFn: ({ pageParam }) => api<Paginated<AuditEvent>>(pageParam),
    getNextPageParam: (last) => (last.next ? last.next.replace(/^.*\/api\/v1/, "") : undefined),
    enabled: isAdmin,
  });
  if (user && !isAdmin) return <Alert>Only admins can view the audit log.</Alert>;
  const items = events.data?.pages.flatMap((p) => p.results) ?? [];

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Sign-ins, key changes, settings changes, user and integration changes. Entries cannot be edited or deleted."
      />
      <div className="mb-4 grid max-w-2xl gap-3 sm:grid-cols-2">
        <Field label="Type">
          <Select value={filters.action} onChange={(e) => setFilter("action", e.target.value)}>
            <option value="">All</option>
            {CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Actor">
          <Input
            type="search"
            defaultValue={filters.actor}
            placeholder="Email"
            onKeyDown={(e) => {
              if (e.key === "Enter") setFilter("actor", (e.target as HTMLInputElement).value.trim());
            }}
            onBlur={(e) => setFilter("actor", e.target.value.trim())}
          />
        </Field>
      </div>
      {events.isLoading ? (
        <Spinner />
      ) : events.error ? (
        <Alert>{errorMessage(events.error)}</Alert>
      ) : items.length === 0 ? (
        <Empty title="No events match" />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">When</th>
                <th className="px-4 py-2">Actor</th>
                <th className="px-4 py-2">Action</th>
                <th className="px-4 py-2">Target</th>
                <th className="px-4 py-2">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((e) => (
                <tr key={e.id} className="align-top">
                  <td className="whitespace-nowrap px-4 py-2 text-slate-600">{formatDate(e.created_at)}</td>
                  <td className="px-4 py-2">
                    {e.actor_email || <span className="text-slate-400">anonymous</span>}
                    {e.ip && <div className="font-mono text-xs text-slate-500">{e.ip}</div>}
                  </td>
                  <td className="px-4 py-2">
                    <Badge tone={TONE[e.action] ?? "slate"}>{e.action}</Badge>
                  </td>
                  <td className="px-4 py-2">
                    {e.target_label || "—"}
                    {e.target_type && <div className="text-xs text-slate-500">{e.target_type}</div>}
                  </td>
                  <td className="px-4 py-2">
                    <Details metadata={e.metadata} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {events.hasNextPage && (
        <div className="mt-4">
          <Button variant="secondary" loading={events.isFetchingNextPage} onClick={() => events.fetchNextPage()}>
            Load more
          </Button>
        </div>
      )}
    </>
  );
}

function Details({ metadata }: { metadata: Record<string, unknown> }) {
  const changes = metadata.changes as Record<string, { from: unknown; to: unknown }> | undefined;
  const rest = Object.entries(metadata).filter(([k]) => k !== "changes");
  if (!changes && rest.length === 0) return <span className="text-slate-400">—</span>;
  const show = (v: unknown) => (typeof v === "string" ? v || "″″" : JSON.stringify(v));
  return (
    <div className="space-y-0.5 text-xs">
      {changes &&
        Object.entries(changes).map(([key, change]) => (
          <div key={key}>
            <span className="font-medium">{key}</span>: <span className="text-slate-500 line-through">{show(change.from)}</span> →{" "}
            {show(change.to)}
          </div>
        ))}
      {rest.map(([key, value]) => (
        <div key={key}>
          <span className="font-medium">{key}</span>: {show(value)}
        </div>
      ))}
    </div>
  );
}
