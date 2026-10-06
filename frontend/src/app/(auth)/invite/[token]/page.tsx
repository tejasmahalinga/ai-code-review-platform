"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Alert, Button, Card, Field, Input, Spinner, buttonClass, errorMessage } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { InviteInfo, User } from "@/lib/types";
import { ROLES } from "@/lib/types";

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const invite = useQuery({
    queryKey: ["invite", token],
    queryFn: () => api<InviteInfo>(`/auth/invites/${encodeURIComponent(token)}`),
    retry: false,
  });
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (invite.isLoading) return <Spinner />;
  if (invite.error) {
    return (
      <Card title="Invitation unavailable">
        <p className="text-sm text-slate-600">
          This invitation link is invalid, expired, or already used. Ask an admin to send you a new one.
        </p>
      </Card>
    );
  }
  const info = invite.data!;
  const role = ROLES.find((r) => r.id === info.role);
  const fieldErrors = error instanceof ApiError ? error.fieldErrors() : {};

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const user = await api<User>(`/auth/invites/${encodeURIComponent(token)}`, { method: "POST", body: { name, password } });
      queryClient.setQueryData(["me"], user);
      router.replace("/pull-requests");
    } catch (err) {
      setError(err as Error);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Join Reviewbot">
      <p className="mb-4 text-sm text-slate-600">
        You were invited as <strong>{role?.label}</strong>: {role?.description}
      </p>
      {info.github_login && (
        <>
          <a className={buttonClass("secondary", "w-full")} href={`/api/v1/auth/github/start?invite=${encodeURIComponent(token)}`}>
            Continue with GitHub
          </a>
          <p className="my-4 text-center text-xs uppercase tracking-wide text-slate-400">or set a password</p>
        </>
      )}
      <form className="space-y-4" onSubmit={onSubmit}>
        {error && !fieldErrors.password && <Alert>{errorMessage(error)}</Alert>}
        <Field label="Email">
          <Input value={info.email} disabled />
        </Field>
        <Field label="Name">
          <Input autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Password" hint="At least 10 characters." error={fieldErrors.password}>
          <Input
            type="password"
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Button type="submit" className="w-full" loading={submitting}>
          Create account
        </Button>
      </form>
    </Card>
  );
}
