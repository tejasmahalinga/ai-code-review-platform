"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Alert, Badge, Button, Card, Field, Input, PageHeader, Select, Spinner, buttonClass, errorMessage, formatDate } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useMe } from "@/lib/hooks";
import type { ApiToken, ExternalIdentity, SetupStatus, User } from "@/lib/types";
import { ROLES } from "@/lib/types";

const SSO_MESSAGES: Record<string, { kind: "success" | "error"; text: string }> = {
  linked: { kind: "success", text: "Single sign-on is linked. You can now sign in with it." },
  in_use: { kind: "error", text: "That identity is already linked to another Reviewbot user." },
  already_linked: { kind: "error", text: "You already have an identity linked at that provider. Unlink it first." },
};

const GITHUB_MESSAGES: Record<string, { kind: "success" | "error"; text: string }> = {
  linked: { kind: "success", text: "Your GitHub account is linked. You can now sign in with GitHub." },
  in_use: { kind: "error", text: "That GitHub account is already linked to another Reviewbot user." },
};

export default function AccountPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <Account />
    </Suspense>
  );
}

function Account() {
  const me = useMe();
  const params = useSearchParams();
  if (!me.data) return <Spinner />;
  const notice = GITHUB_MESSAGES[params.get("github") ?? ""] ?? SSO_MESSAGES[params.get("sso") ?? ""];
  return (
    <>
      <PageHeader title="Your account" description={`${me.data.email} · ${ROLES.find((r) => r.id === me.data!.role)?.label}`} />
      <div className="max-w-2xl space-y-6">
        {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
        <ProfileCard user={me.data} />
        <GitHubCard user={me.data} />
        <SsoCard />
        <PasswordCard user={me.data} />
        <TokensCard />
      </div>
    </>
  );
}

function ProfileCard({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(user.name);
  const save = useMutation({
    mutationFn: () => api<User>("/auth/me", { method: "PATCH", body: { name } }),
    onSuccess: (data) => queryClient.setQueryData(["me"], data),
  });
  return (
    <Card title="Profile">
      <form
        className="flex items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <div className="flex-1">
          <Field label="Name">
            <Input value={name} maxLength={150} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        <Button type="submit" loading={save.isPending}>
          Save
        </Button>
      </form>
      {save.isSuccess && <p className="mt-2 text-sm text-emerald-700">Saved.</p>}
      {save.error && <p className="mt-2 text-sm text-red-700">{errorMessage(save.error)}</p>}
    </Card>
  );
}

function GitHubCard({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["setup-status"],
    queryFn: () => api<SetupStatus>("/setup/status"),
  });
  const unlink = useMutation({
    mutationFn: () => api<User>("/auth/me/github", { method: "DELETE" }),
    onSuccess: (data) => queryClient.setQueryData(["me"], data),
  });
  return (
    <Card title="GitHub">
      {user.github_login ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span>
            Linked to <Badge tone="violet">@{user.github_login}</Badge>
          </span>
          <Button variant="secondary" loading={unlink.isPending} onClick={() => unlink.mutate()}>
            Unlink
          </Button>
          {unlink.error && <span className="w-full text-red-700">{errorMessage(unlink.error)}</span>}
        </div>
      ) : status.data?.github_login ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="text-slate-600">Link your GitHub account to sign in with GitHub.</span>
          <a className={buttonClass("secondary")} href="/api/v1/auth/github/start?link=1">
            Link GitHub account
          </a>
        </div>
      ) : (
        <p className="text-sm text-slate-600">Sign in with GitHub is not enabled on this instance.</p>
      )}
    </Card>
  );
}

function PasswordCard({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useMutation({
    mutationFn: () => api("/auth/password", { method: "POST", body: { current_password: current, new_password: next } }),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
  const errors = change.error instanceof ApiError ? change.error.fieldErrors() : {};
  return (
    <Card title={user.has_password ? "Change password" : "Set a password"}>
      {!user.has_password && (
        <p className="mb-3 text-sm text-slate-600">You sign in with GitHub. A password lets you sign in without it too.</p>
      )}
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          change.mutate();
        }}
      >
        {user.has_password && (
          <Field label="Current password" error={errors.current_password}>
            <Input type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
          </Field>
        )}
        <Field label="New password" hint="At least 10 characters." error={errors.new_password}>
          <Input type="password" autoComplete="new-password" required minLength={10} value={next} onChange={(e) => setNext(e.target.value)} />
        </Field>
        {change.error && !errors.current_password && !errors.new_password && <Alert>{errorMessage(change.error)}</Alert>}
        {change.isSuccess && <Alert kind="success">Password updated. Your other sessions were signed out.</Alert>}
        <Button type="submit" loading={change.isPending}>
          {user.has_password ? "Change password" : "Set password"}
        </Button>
      </form>
    </Card>
  );
}

type CreatedToken = ApiToken & { token: string };

function TokensCard() {
  const queryClient = useQueryClient();
  const tokens = useQuery({ queryKey: ["api-tokens"], queryFn: () => api<ApiToken[]>("/auth/tokens") });
  const [name, setName] = useState("");
  const [expires, setExpires] = useState("90");
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
  const create = useMutation({
    mutationFn: () =>
      api<CreatedToken>("/auth/tokens", { method: "POST", body: { name, expires_in_days: expires ? Number(expires) : null } }),
    onSuccess: (data) => {
      setCreated(data);
      setName("");
      refresh();
    },
  });
  const revoke = useMutation({ mutationFn: (id: number) => api(`/auth/tokens/${id}`, { method: "DELETE" }), onSuccess: refresh });

  return (
    <Card title="API tokens">
      <p className="mb-3 text-sm text-slate-600">
        For scripts and integrations: send <code className="rounded bg-slate-100 px-1">Authorization: Bearer &lt;token&gt;</code> to{" "}
        <code className="rounded bg-slate-100 px-1">/api/v1/…</code>. A token can do what your role can, except manage tokens and
        your password.
      </p>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="min-w-48 flex-1">
          <Field label="Name">
            <Input required maxLength={100} value={name} placeholder="e.g. cost export" onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        <Field label="Expires">
          <Select value={expires} onChange={(e) => setExpires(e.target.value)}>
            <option value="7">in 7 days</option>
            <option value="30">in 30 days</option>
            <option value="90">in 90 days</option>
            <option value="365">in 1 year</option>
            <option value="">never</option>
          </Select>
        </Field>
        <Button type="submit" loading={create.isPending}>
          Create token
        </Button>
      </form>
      {create.error && <p className="mt-2 text-sm text-red-700">{errorMessage(create.error)}</p>}
      {created && (
        <div className="mt-4 space-y-2">
          <Alert kind="success">Copy the token now. It is shown only once.</Alert>
          <Input readOnly value={created.token} onFocus={(e) => e.target.select()} aria-label="New API token" className="font-mono" />
        </div>
      )}
      {(tokens.data?.length ?? 0) > 0 && (
        <ul className="mt-4 divide-y divide-slate-100 text-sm">
          {tokens.data!.map((t) => (
            <li key={t.id} className="flex flex-wrap items-center gap-3 py-2">
              <span className="font-medium">{t.name}</span>
              <code className="text-xs text-slate-500">{t.hint}…</code>
              {!t.active && <Badge tone="amber">expired</Badge>}
              <span className="text-xs text-slate-500">
                last used {t.last_used_at ? formatDate(t.last_used_at) : "never"} · {t.expires_at ? `expires ${formatDate(t.expires_at)}` : "never expires"}
              </span>
              <Button variant="ghost" className="ml-auto" onClick={() => revoke.mutate(t.id)}>
                Revoke
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function SsoCard() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["setup-status"], queryFn: () => api<SetupStatus>("/setup/status") });
  const identities = useQuery({ queryKey: ["identities"], queryFn: () => api<ExternalIdentity[]>("/auth/me/identities") });
  const unlink = useMutation({
    mutationFn: (id: number) => api(`/auth/me/identities/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["identities"] }),
  });
  const providers = (status.data?.login_providers ?? []).filter((p) => p.id !== "github");
  const linked = identities.data ?? [];
  if (providers.length === 0 && linked.length === 0) return null;
  return (
    <Card title="Single sign-on">
      <ul className="divide-y divide-slate-100 text-sm">
        {providers.map((p) => {
          const identity = linked.find((i) => i.provider === p.id);
          return (
            <li key={p.id} className="flex flex-wrap items-center gap-3 py-2">
              <span className="font-medium">{p.name}</span>
              {identity ? (
                <>
                  <Badge tone="violet">{identity.username || identity.email}</Badge>
                  <Button variant="ghost" className="ml-auto" loading={unlink.isPending} onClick={() => unlink.mutate(identity.id)}>
                    Unlink
                  </Button>
                </>
              ) : (
                <a className={buttonClass("secondary", "ml-auto")} href={`${p.start_url}?link=1`}>
                  Link {p.name}
                </a>
              )}
            </li>
          );
        })}
      </ul>
      {unlink.error && <p className="mt-2 text-sm text-red-700">{errorMessage(unlink.error)}</p>}
    </Card>
  );
}

