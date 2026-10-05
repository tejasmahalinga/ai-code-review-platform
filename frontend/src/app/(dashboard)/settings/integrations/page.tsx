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
import type { GitHubIntegration, GitHubManifest, Paginated, WebhookDelivery } from "@/lib/types";

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
          <DeliveryLog />
        </div>
      ) : (
        <ConnectGitHub webUrl={github.data!.web_url} webhookUrl={github.data!.webhook_url} />
      )}
    </>
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
  const [form, setForm] = useState({ app_id: "", app_slug: "", private_key: "", webhook_secret: "" });
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
        Required permissions: Pull requests (read &amp; write), Contents (read), Metadata (read). Subscribe to the
        “Pull request” event and set the webhook URL and secret.
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
