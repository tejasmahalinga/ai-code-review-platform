"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  Alert,
  Badge,
  Button,
  Empty,
  Field,
  PageHeader,
  Select,
  SeverityBadge,
  Spinner,
  StatusBadge,
  TextLink,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { api } from "@/lib/api";
import type { Paginated, PullRequest, Repository } from "@/lib/types";
import { SEVERITIES } from "@/lib/types";

const REVIEW_STATUSES = ["queued", "running", "completed", "failed", "skipped", "none"];

export default function PullRequestsPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <PullRequests />
    </Suspense>
  );
}

function PullRequests() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filters = {
    repository: params.get("repository") ?? "",
    state: params.get("state") ?? "open",
    review_status: params.get("review_status") ?? "",
  };

  const setFilter = (key: keyof typeof filters, value: string) => {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    router.replace(`${pathname}?${next.toString()}`);
  };

  const repos = useQuery({ queryKey: ["repositories"], queryFn: () => api<Repository[]>("/repositories") });
  const query = new URLSearchParams(Object.entries(filters).filter(([, v]) => v));
  const prs = useInfiniteQuery({
    queryKey: ["pull-requests", filters],
    initialPageParam: `/pull-requests?${query.toString()}`,
    queryFn: ({ pageParam }) => api<Paginated<PullRequest>>(pageParam),
    getNextPageParam: (last) => (last.next ? last.next.replace(/^.*\/api\/v1/, "") : undefined),
    refetchInterval: (q) => {
      const active = q.state.data?.pages.some((p) => p.results.some((pr) => ["queued", "running"].includes(pr.latest_review?.status ?? "")));
      return active ? 5_000 : 30_000;
    },
  });
  const items = prs.data?.pages.flatMap((p) => p.results) ?? [];

  return (
    <>
      <PageHeader title="Pull requests" description="Every pull request Reviewbot has seen, across all repositories." />
      <div className="mb-4 grid max-w-3xl gap-3 sm:grid-cols-3">
        <Field label="Repository">
          <Select value={filters.repository} onChange={(e) => setFilter("repository", e.target.value)}>
            <option value="">All repositories</option>
            {repos.data?.map((r) => (
              <option key={r.id} value={r.id}>
                {r.full_name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="State">
          <Select value={filters.state} onChange={(e) => setFilter("state", e.target.value)}>
            <option value="">Any</option>
            <option value="open">Open</option>
            <option value="closed,merged">Closed or merged</option>
          </Select>
        </Field>
        <Field label="Latest review">
          <Select value={filters.review_status} onChange={(e) => setFilter("review_status", e.target.value)}>
            <option value="">Any</option>
            {REVIEW_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "none" ? "not reviewed" : s}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {prs.isLoading ? (
        <Spinner />
      ) : prs.error ? (
        <Alert>{errorMessage(prs.error)}</Alert>
      ) : items.length === 0 ? (
        <Empty title="No pull requests match">
          Pull requests appear here once an enabled repository receives one.{" "}
          <TextLink href="/repositories">Manage repositories</TextLink>
        </Empty>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Pull request</th>
                <th className="px-4 py-2">State</th>
                <th className="px-4 py-2">Latest review</th>
                <th className="px-4 py-2">Findings</th>
                <th className="px-4 py-2">Updated</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {items.map((pr) => (
                <tr key={pr.id} className="align-top">
                  <td className="px-4 py-2">
                    <div className="text-xs text-slate-500">{pr.repository.full_name}</div>
                    <a href={pr.html_url} target="_blank" rel="noreferrer" className="font-medium hover:underline">
                      #{pr.number} {pr.title}
                    </a>
                    <div className="text-xs text-slate-500">by {pr.author_login || "unknown"}</div>
                  </td>
                  <td className="px-4 py-2">
                    <Badge tone={pr.state === "open" ? "green" : pr.state === "merged" ? "violet" : "slate"}>{pr.state}</Badge>{" "}
                    {pr.is_draft && <Badge>draft</Badge>}{" "}
                    {pr.reviews_paused && <Badge tone="amber">paused</Badge>}
                  </td>
                  <td className="px-4 py-2">
                    {pr.latest_review ? (
                      <Link href={`/reviews/${pr.latest_review.id}`} className="inline-flex items-center gap-2 hover:underline">
                        <StatusBadge status={pr.latest_review.status} />
                        <span className="text-xs text-slate-500">{pr.latest_review.status_reason}</span>
                      </Link>
                    ) : (
                      <span className="text-slate-400">not reviewed</span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    {pr.latest_review ? (
                      <div className="flex flex-wrap gap-1">
                        {SEVERITIES.filter((s) => pr.latest_review!.counts[s] > 0).map((s) => (
                          <span key={s} className="inline-flex items-center gap-1">
                            <SeverityBadge severity={s} />
                            <span className="text-xs">{pr.latest_review!.counts[s]}</span>
                          </span>
                        ))}
                        {SEVERITIES.every((s) => pr.latest_review!.counts[s] === 0) && <span className="text-slate-400">—</span>}
                      </div>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2 text-slate-600">{formatDate(pr.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {prs.hasNextPage && (
        <div className="mt-4">
          <Button variant="secondary" loading={prs.isFetchingNextPage} onClick={() => prs.fetchNextPage()}>
            Load more
          </Button>
        </div>
      )}
    </>
  );
}
