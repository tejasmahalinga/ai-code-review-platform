"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Empty,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  Textarea,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useRole } from "@/lib/hooks";
import type { NotificationChannel, NotificationDelivery, NotificationKind, Repository } from "@/lib/types";
import { NOTIFICATION_EVENTS, SEVERITIES } from "@/lib/types";

const KINDS: { id: NotificationKind; label: string; hint: string }[] = [
  { id: "slack", label: "Slack", hint: "An incoming webhook URL (https://hooks.slack.com/services/…)." },
  { id: "email", label: "Email", hint: "Needs SMTP (REVIEWBOT_EMAIL_HOST)." },
  { id: "webhook", label: "Webhook", hint: "POSTs signed JSON to any HTTPS endpoint, e.g. Teams workflows or your own service." },
];

const eventLabel = (id: string) => NOTIFICATION_EVENTS.find((e) => e.id === id)?.label ?? id;

export default function NotificationsPage() {
  const { user, isAdmin } = useRole();
  const channels = useQuery({
    queryKey: ["notification-channels"],
    queryFn: () => api<NotificationChannel[]>("/notification-channels"),
    enabled: isAdmin,
  });
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<number | null>(null);
  if (user && !isAdmin) return <Alert>Only admins can manage notifications.</Alert>;

  return (
    <>
      <PageHeader
        title="Notifications"
        description="Tell your team about high-risk pull requests, failed reviews, budget alerts, and a weekly digest."
        actions={!adding && <Button onClick={() => setAdding(true)}>Add channel</Button>}
      />
      <div className="space-y-6">
        {adding && <ChannelForm onDone={() => setAdding(false)} />}
        {channels.isLoading ? (
          <Spinner />
        ) : channels.error ? (
          <Alert>{errorMessage(channels.error)}</Alert>
        ) : channels.data!.length === 0 ? (
          !adding && (
            <Empty title="No notification channels">
              Add a Slack channel, an email list, or a webhook. Without channels, admins still get budget alerts by email
              when SMTP is configured.
            </Empty>
          )
        ) : (
          channels.data!.map((c) =>
            editing === c.id ? (
              <ChannelForm key={c.id} channel={c} onDone={() => setEditing(null)} />
            ) : (
              <ChannelCard key={c.id} channel={c} onEdit={() => setEditing(c.id)} />
            ),
          )
        )}
      </div>
    </>
  );
}

function ChannelCard({ channel: c, onEdit }: { channel: NotificationChannel; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const [showLog, setShowLog] = useState(false);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
  const test = useMutation({ mutationFn: () => api<{ ok: boolean; error: string }>(`/notification-channels/${c.id}/test`, { method: "POST" }) });
  const toggle = useMutation({
    mutationFn: () => api(`/notification-channels/${c.id}`, { method: "PATCH", body: { enabled: !c.enabled } }),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: () => api(`/notification-channels/${c.id}`, { method: "DELETE" }), onSuccess: refresh });
  const log = useQuery({
    queryKey: ["notification-deliveries", c.id],
    queryFn: () => api<NotificationDelivery[]>(`/notification-channels/${c.id}/deliveries`),
    enabled: showLog,
  });

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {c.name} <Badge tone="violet">{KINDS.find((k) => k.id === c.kind)?.label}</Badge>
          {!c.enabled && <Badge tone="amber">paused</Badge>}
        </span>
      }
      actions={
        <>
          <Button variant="secondary" loading={test.isPending} onClick={() => test.mutate()}>
            Send test
          </Button>
          <Button variant="secondary" onClick={onEdit}>
            Edit
          </Button>
          <Button variant="ghost" onClick={() => toggle.mutate()}>
            {c.enabled ? "Pause" : "Resume"}
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              if (confirm(`Delete the channel "${c.name}"?`)) remove.mutate();
            }}
          >
            Delete
          </Button>
        </>
      }
    >
      <dl className="grid gap-3 text-sm md:grid-cols-3">
        <div>
          <dt className="text-xs uppercase tracking-wide text-slate-500">Destination</dt>
          <dd className="font-mono text-xs">{c.kind === "email" ? c.recipients.join(", ") : c.url_hint}</dd>
          {c.kind === "webhook" && <dd className="text-xs text-slate-500">{c.has_secret ? "signed (HMAC-SHA256)" : "unsigned"}</dd>}
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-slate-500">Events</dt>
          <dd className="flex flex-wrap gap-1">
            {c.events.map((e) => (
              <Badge key={e}>{eventLabel(e)}</Badge>
            ))}
          </dd>
        </div>
        {c.events.includes("review.high_risk") && (
          <div>
            <dt className="text-xs uppercase tracking-wide text-slate-500">High risk means</dt>
            <dd>
              risk ≥ {c.min_risk}
              {c.min_severity && ` or a ${c.min_severity}+ finding`}
              {c.repositories.length > 0 && ` · ${c.repositories.length} repositories`}
            </dd>
          </div>
        )}
      </dl>
      {test.data && (
        <div className="mt-3">
          {test.data.ok ? <Alert kind="success">Test message sent.</Alert> : <Alert>Test failed: {test.data.error}</Alert>}
        </div>
      )}
      {(test.error || toggle.error || remove.error) && (
        <div className="mt-3">
          <Alert>{errorMessage(test.error ?? toggle.error ?? remove.error)}</Alert>
        </div>
      )}
      <button type="button" className="mt-3 text-xs text-sky-700 hover:underline" onClick={() => setShowLog((s) => !s)}>
        {showLog ? "Hide recent deliveries" : "Show recent deliveries"}
      </button>
      {showLog &&
        (log.isLoading ? (
          <Spinner />
        ) : (log.data?.length ?? 0) === 0 ? (
          <p className="mt-2 text-sm text-slate-500">Nothing sent yet.</p>
        ) : (
          <ul className="mt-2 divide-y divide-slate-100 text-sm">
            {log.data!.map((d) => (
              <li key={d.id} className="flex flex-wrap items-center gap-2 py-1.5">
                <Badge tone={d.status === "sent" ? "green" : d.status === "failed" ? "red" : "slate"}>{d.status}</Badge>
                <span>{d.title}</span>
                <span className="text-xs text-slate-500">{formatDate(d.created_at)}</span>
                {d.error && <span className="w-full text-xs text-red-700">{d.error}</span>}
              </li>
            ))}
          </ul>
        ))}
    </Card>
  );
}

interface FormState {
  name: string;
  kind: NotificationKind;
  url: string;
  secret: string;
  recipients: string;
  events: string[];
  min_risk: number;
  min_severity: string;
  repositories: number[];
}

function ChannelForm({ channel, onDone }: { channel?: NotificationChannel; onDone: () => void }) {
  const queryClient = useQueryClient();
  const repos = useQuery({ queryKey: ["repositories"], queryFn: () => api<Repository[]>("/repositories") });
  const [form, setForm] = useState<FormState>({
    name: channel?.name ?? "",
    kind: channel?.kind ?? "slack",
    url: "",
    secret: "",
    recipients: channel?.recipients.join("\n") ?? "",
    events: channel?.events ?? ["review.high_risk", "review.failed", "budget.threshold", "digest.weekly"],
    min_risk: channel?.min_risk ?? 60,
    min_severity: channel?.min_severity ?? "critical",
    repositories: channel?.repositories ?? [],
  });
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((f) => ({ ...f, [key]: value }));
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: form.name,
        kind: form.kind,
        events: form.events,
        min_risk: form.min_risk,
        min_severity: form.min_severity,
        repositories: form.repositories,
        recipients: form.recipients.split(/[\s,]+/).filter(Boolean),
      };
      // Secrets are write-only: send them only when typed, so editing keeps the stored ones.
      if (form.url.trim()) body.url = form.url.trim();
      if (form.secret) body.secret = form.secret;
      return channel
        ? api(`/notification-channels/${channel.id}`, { method: "PATCH", body })
        : api("/notification-channels", { method: "POST", body });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notification-channels"] });
      onDone();
    },
  });
  const errors = save.error instanceof ApiError ? save.error.fieldErrors() : {};
  const kind = KINDS.find((k) => k.id === form.kind)!;

  return (
    <Card title={channel ? `Edit "${channel.name}"` : "New channel"}>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        {save.error && Object.keys(errors).length === 0 && <Alert>{errorMessage(save.error)}</Alert>}
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Name" error={errors.name}>
            <Input required maxLength={100} value={form.name} placeholder="e.g. #eng-reviews" onChange={(e) => set("name", e.target.value)} />
          </Field>
          <Field label="Type" hint={kind.hint} error={errors.kind}>
            <Select value={form.kind} disabled={Boolean(channel)} onChange={(e) => set("kind", e.target.value as NotificationKind)}>
              {KINDS.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.label}
                </option>
              ))}
            </Select>
          </Field>
          {form.kind === "email" ? (
            <div className="md:col-span-2">
              <Field label="Recipients" hint="One address per line." error={errors.recipients}>
                <Textarea rows={3} value={form.recipients} onChange={(e) => set("recipients", e.target.value)} />
              </Field>
            </div>
          ) : (
            <>
              <Field
                label={form.kind === "slack" ? "Slack webhook URL" : "Webhook URL"}
                hint={channel ? `Stored: ${channel.url_hint}. Leave empty to keep it.` : undefined}
                error={errors.url}
              >
                <Input type="password" autoComplete="off" required={!channel} value={form.url} onChange={(e) => set("url", e.target.value)} />
              </Field>
              {form.kind === "webhook" && (
                <Field
                  label="Signing secret (optional)"
                  hint={`Sent as X-Reviewbot-Signature-256: sha256=HMAC(secret, body).${channel?.has_secret ? " Leave empty to keep the stored secret." : ""}`}
                  error={errors.secret}
                >
                  <Input type="password" autoComplete="off" value={form.secret} onChange={(e) => set("secret", e.target.value)} />
                </Field>
              )}
            </>
          )}
        </div>

        <fieldset>
          <legend className="mb-2 text-sm font-medium text-slate-800">Events</legend>
          {errors.events && <p className="mb-2 text-sm text-red-700">{errors.events}</p>}
          <div className="grid gap-2 md:grid-cols-2">
            {NOTIFICATION_EVENTS.map((e) => (
              <Checkbox
                key={e.id}
                label={e.label}
                hint={e.hint}
                checked={form.events.includes(e.id)}
                onChange={(ev) => set("events", ev.target.checked ? [...form.events, e.id] : form.events.filter((x) => x !== e.id))}
              />
            ))}
          </div>
        </fieldset>

        {form.events.includes("review.high_risk") && (
          <div className="grid gap-4 md:grid-cols-3">
            <Field label="Notify when risk is at least" hint="0–100" error={errors.min_risk}>
              <Input type="number" min={0} max={100} value={form.min_risk} onChange={(e) => set("min_risk", Number(e.target.value))} />
            </Field>
            <Field label="…or a finding is at least" error={errors.min_severity}>
              <Select value={form.min_severity} onChange={(e) => set("min_severity", e.target.value)}>
                <option value="">(ignore severity)</option>
                {SEVERITIES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Repositories" hint="None selected means all repositories." error={errors.repositories}>
              <select
                multiple
                className="h-24 w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
                value={form.repositories.map(String)}
                onChange={(e) => set("repositories", Array.from(e.target.selectedOptions, (o) => Number(o.value)))}
              >
                {repos.data?.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.full_name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        )}

        <div className="flex gap-2">
          <Button type="submit" loading={save.isPending}>
            {channel ? "Save" : "Create channel"}
          </Button>
          <Button type="button" variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        </div>
      </form>
    </Card>
  );
}
