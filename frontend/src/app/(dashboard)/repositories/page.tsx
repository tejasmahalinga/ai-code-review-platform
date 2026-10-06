"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Alert, Badge, Empty, Input, PageHeader, Spinner, TextLink, buttonClass, errorMessage } from "@/components/ui";
import { api } from "@/lib/api";
import { useRole } from "@/lib/hooks";
import type { GitHubIntegration, Repository } from "@/lib/types";

export default function RepositoriesPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState("");
  const { isAdmin } = useRole();
  const repos = useQuery({ queryKey: ["repositories"], queryFn: () => api<Repository[]>("/repositories") });
  const github = useQuery({
    queryKey: ["github"],
    queryFn: () => api<GitHubIntegration>("/integrations/github"),
    enabled: isAdmin,
  });
  const [webhookWarning, setWebhookWarning] = useState<string | null>(null);
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      api<Repository>(`/repositories/${id}`, { method: "PATCH", body: { enabled } }),
    onSuccess: (repo) => {
      setWebhookWarning(repo.webhook_warning ? `${repo.full_name}: ${repo.webhook_warning}` : null);
      queryClient.invalidateQueries({ queryKey: ["repositories"] });
    },
  });

  const filtered = (repos.data ?? []).filter((r) => r.full_name.toLowerCase().includes(query.toLowerCase()));

  return (
    <>
      <PageHeader
        title="Repositories"
        description="Repositories the GitHub App is installed on. Reviews run only for enabled repositories."
        actions={
          github.data?.install_url && (
            <a href={github.data.install_url} className="text-sm text-sky-700 hover:underline">
              Add repositories on GitHub →
            </a>
          )
        }
      />
      {webhookWarning && (
        <div className="mb-4">
          <Alert kind="warning">
            {webhookWarning} <TextLink href="/settings/integrations">Webhook URL and secret</TextLink>
          </Alert>
        </div>
      )}
      {toggle.error && (
        <div className="mb-4">
          <Alert>{errorMessage(toggle.error)}</Alert>
        </div>
      )}
      {repos.isLoading ? (
        <Spinner />
      ) : repos.error ? (
        <Alert>{errorMessage(repos.error)}</Alert>
      ) : repos.data!.length === 0 ? (
        <Empty title="No repositories yet">
          {github.data?.connected ? (
            <>Install the GitHub App on at least one repository, then sync on the Integrations page.</>
          ) : (
            <>
              Connect GitHub first on the <TextLink href="/settings/integrations">Integrations</TextLink> page.
            </>
          )}
        </Empty>
      ) : (
        <>
          <div className="mb-4 max-w-sm">
            <Input placeholder="Filter repositories…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Filter repositories" />
          </div>
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full divide-y divide-slate-200 text-sm">
              <thead className="bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2">Repository</th>
                  <th className="px-4 py-2">LLM key / model</th>
                  <th className="px-4 py-2">Auto-review</th>
                  <th className="px-4 py-2">PRs</th>
                  <th className="px-4 py-2">Reviews</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {filtered.map((r) => (
                  <tr key={r.id}>
                    <td className="px-4 py-2">
                      <a href={r.html_url} target="_blank" rel="noreferrer" className="font-medium hover:underline">
                        {r.full_name}
                      </a>{" "}
                      {r.provider === "gitlab" && <Badge tone="orange">GitLab</Badge>} {r.private && <Badge>private</Badge>}{" "}
                      {r.enabled && r.webhook_managed === false && (
                        <span title="Add the project webhook in GitLab by hand (see Integrations), or give the bot user the Maintainer role and enable again.">
                          <Badge tone="amber">webhook not managed</Badge>
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {r.credential_name ? (
                        <>
                          {r.credential_name} <span className="font-mono text-xs text-slate-500">{r.model}</span>
                        </>
                      ) : (
                        <span className="text-amber-700">No key selected</span>
                      )}
                    </td>
                    <td className="px-4 py-2">{r.auto_review ? "On" : "Off"}</td>
                    <td className="px-4 py-2">
                      <Link href={`/pull-requests?repository=${r.id}`} className="text-sky-700 hover:underline">
                        {r.pull_request_count}
                      </Link>
                    </td>
                    <td className="px-4 py-2">
                      <label className="inline-flex cursor-pointer items-center gap-2">
                        <input
                          type="checkbox"
                          className="h-4 w-4"
                          checked={r.enabled}
                          disabled={toggle.isPending || !isAdmin}
                          title={isAdmin ? undefined : "Only admins can enable or disable reviews"}
                          onChange={(e) => toggle.mutate({ id: r.id, enabled: e.target.checked })}
                          aria-label={`Enable reviews for ${r.full_name}`}
                        />
                        <span>{r.enabled ? <Badge tone="green">enabled</Badge> : <Badge>disabled</Badge>}</span>
                      </label>
                    </td>
                    <td className="px-4 py-2 text-right">
                      <Link href={`/repositories/${r.id}`} className={buttonClass("secondary")}>
                        Settings
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  );
}
