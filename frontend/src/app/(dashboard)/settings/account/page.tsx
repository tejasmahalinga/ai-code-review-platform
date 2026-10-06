"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { Alert, Badge, Button, Card, Field, Input, PageHeader, Spinner, buttonClass, errorMessage } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useMe } from "@/lib/hooks";
import type { User } from "@/lib/types";
import { ROLES } from "@/lib/types";

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
  const notice = GITHUB_MESSAGES[params.get("github") ?? ""];
  return (
    <>
      <PageHeader title="Your account" description={`${me.data.email} · ${ROLES.find((r) => r.id === me.data!.role)?.label}`} />
      <div className="max-w-2xl space-y-6">
        {notice && <Alert kind={notice.kind}>{notice.text}</Alert>}
        <ProfileCard user={me.data} />
        <GitHubCard user={me.data} />
        <PasswordCard user={me.data} />
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
    queryFn: () => api<{ needs_setup: boolean; github_login: boolean }>("/setup/status"),
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
