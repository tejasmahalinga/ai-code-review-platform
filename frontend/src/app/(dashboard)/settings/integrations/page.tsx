"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Field,
  Input,
  PageHeader,
  Spinner,
  TextLink,
  Textarea,
  buttonClass,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { GitHubIntegration, GitHubManifest, GitLabIntegration, Paginated, WebhookDelivery } from "@/lib/types";

const RESULT_MESSAGES: Record<string, { kind: "success" | "error" | "info"; text: string }> = {
  connected: { kind: "success", text: "GitHub App created. Now install it on the repositories you want reviewed." },
  invalid_state: { kind: "error", text: "The GitHub setup link expired or was tampered with. Please start again." },
  already_connected: { kind: "info", text: "GitHub was already connected." },
  exchange_failed: { kind: "error", text: "GitHub did not accept the setup code. Please start again." },
};

export default function IntegrationsPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <Integrations />
    </Suspense>
  );
}

function Integrations() {
  const queryClient = useQueryClient();
  const github = useQuery({ queryKey: ["github"], queryFn: () => api<GitHubIntegration>("/integrations/github") });
  const result = useSearchParams().get("github");

  const sync = useMutation({
    mutationFn: () => api<{ installations: number; repositories: number }>("/integrations/github/sync", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["github"] }),
  });
  const disconnect = useMutation({
    mutationFn: () => api("/integrations/github", { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries(),
  });

  const message = result ? RESULT_MESSAGES[result] : null;

  return (
    <>
      <PageHeader title="Integrations" description="Connect Reviewbot to your Git provider." />
      {message && (
        <div className="mb-4">
          <Alert kind={message.kind}>{message.text}</Alert>
        </div>
      )}
      {github.isLoading ? (
        <Spinner />
      ) : github.error ? (
        <Alert>{errorMessage(github.error)}</Alert>
      ) : github.data!.connected ? (
        <div className="space-y-6">
          <Card
            title={
              <span className="flex items-center gap-2">
                GitHub <Badge tone="green">connected</Badge>
              </span>
            }
            actions={
              <>
                <Button variant="secondary" loading={sync.isPending} onClick={() => sync.mutate()}>
                  Sync repositories
                </Button>
                <a href={github.data!.install_url} className={buttonClass("primary")}>
                  Install on repositories
                </a>
              </>
            }
          >
            <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
              <dt className="text-slate-500">GitHub App</dt>
              <dd>
                <TextLink href={github.data!.app_html_url || github.data!.web_url} external>
                  {github.data!.app_name || github.data!.app_slug}
                </TextLink>
              </dd>
              <dt className="text-slate-500">Webhook URL</dt>
              <dd className="font-mono text-xs">{github.data!.webhook_url}</dd>
              <dt className="text-slate-500">Installations</dt>
              <dd>
                {github.data!.installations.length === 0 ? (
                  <span className="text-slate-600">None yet. Use “Install on repositories”.</span>
                ) : (
                  <ul className="space-y-1">
                    {github.data!.installations.map((i) => (
                      <li key={i.id}>
                        <span className="font-medium">{i.account_login}</span>{" "}
                        <span className="text-slate-500">
                          ({i.account_type || "account"}, {i.repository_count} repositories)
                        </span>{" "}
                        {i.suspended && <Badge tone="amber">suspended</Badge>}
                      </li>
                    ))}
                  </ul>
                )}
              </dd>
            </dl>
            {sync.data && (
              <div className="mt-4">
                <Alert kind="success">
                  Synced {sync.data.installations} installation(s) and {sync.data.repositories} repositories.{" "}
                  <TextLink href="/repositories">Choose repositories to review →</TextLink>
                </Alert>
              </div>
            )}
            {(sync.error || disconnect.error) && (
              <div className="mt-4">
                <Alert>{errorMessage(sync.error ?? disconnect.error)}</Alert>
              </div>
            )}
            <div className="mt-6 border-t border-slate-200 pt-4">
              <Button
                variant="danger"
                onClick={() => {
                  if (
                    confirm(
                      "Disconnect GitHub? This deletes the stored App credentials and all repository and review history. Uninstall the App on GitHub as well.",
                    )
                  )
                    disconnect.mutate();
                }}
              >
                Disconnect GitHub
              </Button>
            </div>
          </Card>
        </div>
      ) : (
        <ConnectGitHub webUrl={github.data!.web_url} webhookUrl={github.data!.webhook_url} />
      )}
      <div className="mt-8">
        <GitLabSection />
      </div>
      <div className="mt-8">
        <DeliveryLog />
      </div>
    </>
  );
}

function GitLabSection() {
  const queryClient = useQueryClient();
  const gitlab = useQuery({ queryKey: ["gitlab"], queryFn: () => api<GitLabIntegration>("/integrations/gitlab") });
  const [url, setUrl] = useState("https://gitlab.com");
  const [token, setToken] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["gitlab"] });
    queryClient.invalidateQueries({ queryKey: ["repositories"] });
  };
  const connect = useMutation({
    mutationFn: () => api<{ repositories: number }>("/integrations/gitlab", { method: "POST", body: { url, token } }),
    onSuccess: () => {
      setToken("");
      refresh();
    },
  });
  const sync = useMutation({
    mutationFn: () => api<{ repositories: number }>("/integrations/gitlab/sync", { method: "POST" }),
    onSuccess: refresh,
  });
  const disconnect = useMutation({ mutationFn: () => api("/integrations/gitlab", { method: "DELETE" }), onSuccess: () => queryClient.invalidateQueries() });
  const errors = connect.error instanceof ApiError ? connect.error.fieldErrors() : {};

  if (gitlab.isLoading) return <Spinner />;
  if (gitlab.error) return <Alert>{errorMessage(gitlab.error)}</Alert>;
  const data = gitlab.data!;

  if (!data.connected) {
    return (
      <Card title="GitLab">
        <p className="mb-4 text-sm text-slate-600">
          Reviewbot acts as a GitLab user. Create a dedicated bot user (or a group access token), give it the{" "}
          <strong>Developer</strong> role on the projects to review (<strong>Maintainer</strong> lets Reviewbot add project
          webhooks itself), and create an access token with the <code>api</code> scope. Works with gitlab.com and
          self-managed GitLab 15.0 or later.
        </p>
        <form
          className="grid gap-4 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            connect.mutate();
          }}
        >
          {connect.error && Object.keys(errors).length === 0 && (
            <div className="md:col-span-2">
              <Alert>{errorMessage(connect.error)}</Alert>
            </div>
          )}
          <Field label="GitLab URL" error={errors.url}>
            <Input type="url" required value={url} onChange={(e) => setUrl(e.target.value)} />
          </Field>
          <Field label="Access token" hint="Scope: api. Stored encrypted." error={errors.token}>
            <Input type="password" autoComplete="off" required value={token} onChange={(e) => setToken(e.target.value)} placeholder="glpat-…" />
          </Field>
          <div className="md:col-span-2">
            <Button type="submit" loading={connect.isPending}>
              Connect GitLab
            </Button>
          </div>
        </form>
      </Card>
    );
  }

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          GitLab <Badge tone="green">connected</Badge>
        </span>
      }
      actions={
        <Button variant="secondary" loading={sync.isPending} onClick={() => sync.mutate()}>
          Sync projects
        </Button>
      }
    >
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="text-slate-500">Instance</dt>
        <dd>
          <TextLink href={data.web_url!} external>
            {data.web_url}
          </TextLink>
        </dd>
        <dt className="text-slate-500">Acting as</dt>
        <dd>@{data.username}</dd>
        <dt className="text-slate-500">Projects</dt>
        <dd>
          {data.repositories} with Developer access or more · <TextLink href="/repositories">enable them on Repositories</TextLink>
        </dd>
        <dt className="text-slate-500">Webhook URL</dt>
        <dd className="font-mono text-xs">{data.webhook_url}</dd>
        <dt className="text-slate-500">Secret token</dt>
        <dd className="flex items-center gap-2 font-mono text-xs">
          {showSecret ? data.webhook_secret : "••••••••••••"}
          <button type="button" className="font-sans text-sky-700 hover:underline" onClick={() => setShowSecret((v) => !v)}>
            {showSecret ? "hide" : "show"}
          </button>
        </dd>
      </dl>
      <p className="mt-3 text-xs text-slate-500">
        Enabling a project adds its webhook automatically when the bot user is a Maintainer. Otherwise add it under the
        project&apos;s Settings → Webhooks with the URL and secret token above, and trigger &quot;Merge request events&quot; and
        &quot;Comments&quot;.
      </p>
      {sync.data && (
        <div className="mt-4">
          <Alert kind="success">Synced {sync.data.repositories} projects.</Alert>
        </div>
      )}
      {(sync.error || disconnect.error) && (
        <div className="mt-4">
          <Alert>{errorMessage(sync.error ?? disconnect.error)}</Alert>
        </div>
      )}
      <div className="mt-6 border-t border-slate-200 pt-4">
        <Button
          variant="danger"
          onClick={() => {
            if (confirm("Disconnect GitLab? This deletes the stored token and all GitLab repository and review history. Remove the project webhooks in GitLab as well.")) disconnect.mutate();
          }}
        >
          Disconnect GitLab
        </Button>
      </div>
    </Card>
  );
}

function ConnectGitHub({ webUrl, webhookUrl }: { webUrl: string; webhookUrl: string }) {
  const [appName, setAppName] = useState("Reviewbot");
  const [organization, setOrganization] = useState("");
  const [manual, setManual] = useState(false);
  const start = useMutation({
    mutationFn: () =>
      api<GitHubManifest>("/integrations/github/manifest", { method: "POST", body: { app_name: appName, organization } }),
    onSuccess: (data) => {
      // GitHub's manifest flow requires a browser form POST to github.com.
      const form = document.createElement("form");
      form.method = "post";
      form.action = data.post_url;
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = "manifest";
      input.value = data.manifest;
      form.appendChild(input);
      document.body.appendChild(form);
      form.submit();
    },
  });

  return (
    <div className="space-y-6">
      <Card title="Connect GitHub">
        <p className="mb-4 text-sm text-slate-600">
          Reviewbot creates a private GitHub App on {webUrl} with read access to code and write access to pull request
          reviews only. GitHub must be able to reach <span className="font-mono text-xs">{webhookUrl}</span>.
        </p>
        <form
          className="grid gap-4 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            start.mutate();
          }}
        >
          {start.error && (
            <div className="md:col-span-2">
              <Alert>{errorMessage(start.error)}</Alert>
            </div>
          )}
          <Field label="App name" hint="Must be unique on GitHub (max 34 characters).">
            <Input required maxLength={34} value={appName} onChange={(e) => setAppName(e.target.value)} />
          </Field>
          <Field label="Organization (optional)" hint="Create the App under an organization instead of your account.">
            <Input value={organization} placeholder="my-org" onChange={(e) => setOrganization(e.target.value)} />
          </Field>
          <div className="flex gap-2 md:col-span-2">
            <Button type="submit" loading={start.isPending}>
              Create GitHub App
            </Button>
            <Button type="button" variant="ghost" onClick={() => setManual((m) => !m)}>
              {manual ? "Hide manual setup" : "I already have a GitHub App"}
            </Button>
          </div>
        </form>
      </Card>
      {manual && <ManualGitHubApp />}
    </div>
  );
}

function ManualGitHubApp() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ app_id: "", app_slug: "", private_key: "", webhook_secret: "", client_id: "", client_secret: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => api("/integrations/github/manual", { method: "POST", body: form }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["github"] }),
    onError: (err) => err instanceof ApiError && setErrors(err.fieldErrors()),
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));
  return (
    <Card title="Use an existing GitHub App">
      <p className="mb-4 text-sm text-slate-600">
        Required permissions: Pull requests (read &amp; write), Contents (read), Metadata (read), Checks (read &amp;
        write), and the account permission Email addresses (read) for sign-in. Subscribe to the “Pull request” and
        “Issue comment” events and set the webhook URL and secret.
      </p>
      <form
        className="grid gap-4 md:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          setErrors({});
          save.mutate();
        }}
      >
        {save.error && (
          <div className="md:col-span-2">
            <Alert>{errorMessage(save.error)}</Alert>
          </div>
        )}
        <Field label="App ID" error={errors.app_id}>
          <Input required value={form.app_id} onChange={set("app_id")} />
        </Field>
        <Field label="App slug" hint="From the App's public URL: github.com/apps/<slug>" error={errors.app_slug}>
          <Input required value={form.app_slug} onChange={set("app_slug")} />
        </Field>
        <Field label="Webhook secret" error={errors.webhook_secret}>
          <Input type="password" required value={form.webhook_secret} onChange={set("webhook_secret")} />
        </Field>
        <Field label="Client ID (optional)" hint="Enables “Sign in with GitHub”." error={errors.client_id}>
          <Input value={form.client_id} onChange={set("client_id")} />
        </Field>
        <Field label="Client secret (optional)" error={errors.client_secret}>
          <Input type="password" value={form.client_secret} onChange={set("client_secret")} />
        </Field>
        <div className="md:col-span-2">
          <Field label="Private key (PEM)" error={errors.private_key}>
            <Textarea required rows={6} value={form.private_key} onChange={set("private_key")} placeholder="-----BEGIN RSA PRIVATE KEY-----" />
          </Field>
        </div>
        <div className="md:col-span-2">
          <Button type="submit" loading={save.isPending}>
            Save
          </Button>
        </div>
      </form>
    </Card>
  );
}

function DeliveryLog() {
  const deliveries = useQuery({
    queryKey: ["webhook-deliveries"],
    queryFn: () => api<Paginated<WebhookDelivery>>("/webhook-deliveries?page_size=20"),
    refetchInterval: 15_000,
  });
  return (
    <Card title="Recent webhook deliveries">
      {deliveries.isLoading ? (
        <Spinner />
      ) : !deliveries.data?.results.length ? (
        <p className="text-sm text-slate-600">No deliveries received yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-1 pr-4">Received</th>
                <th className="py-1 pr-4">Event</th>
                <th className="py-1 pr-4">Repository</th>
                <th className="py-1 pr-4">Result</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {deliveries.data.results.map((d) => (
                <tr key={d.id}>
                  <td className="py-1 pr-4 text-slate-600">{formatDate(d.received_at)}</td>
                  <td className="py-1 pr-4 font-mono text-xs">
                    {d.event}
                    {d.action && `.${d.action}`}
                  </td>
                  <td className="py-1 pr-4">{d.repository ?? "—"}</td>
                  <td className="py-1 pr-4">
                    <Badge tone={d.status === "processed" ? "green" : d.status === "failed" ? "red" : "slate"}>{d.status}</Badge>{" "}
                    <span className="text-xs text-slate-500">{d.reason}</span>
                    {d.review_run && (
                      <>
                        {" "}
                        <TextLink href={`/reviews/${d.review_run}`}>review</TextLink>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
