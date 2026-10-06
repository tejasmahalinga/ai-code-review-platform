"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Alert, Button, Card, Field, Input, Spinner, buttonClass, errorMessage } from "@/components/ui";
import { api } from "@/lib/api";
import { useSetupStatus } from "@/lib/hooks";
import type { User } from "@/lib/types";

const GITHUB_ERRORS: Record<string, string> = {
  no_account: "No Reviewbot account matches that GitHub user. Ask an admin for an invite.",
  account_disabled: "This account is deactivated. Contact an admin.",
  github_mismatch: "That email belongs to an account linked to a different GitHub user.",
  invite_invalid: "This invitation is no longer valid.",
  github_denied: "GitHub sign-in was cancelled.",
  github_state: "The GitHub sign-in expired. Try again.",
  github_failed: "GitHub sign-in failed. Try again.",
  github_unavailable: "Sign in with GitHub is not configured.",
};

export default function LoginPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <Login />
    </Suspense>
  );
}

function Login() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const setup = useSetupStatus();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(() => {
    const code = params.get("error");
    return code ? (GITHUB_ERRORS[code] ?? "Sign-in failed.") : null;
  });
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (setup.data?.needs_setup) router.replace("/setup");
  }, [setup.data, router]);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const user = await api<User>("/auth/login", { method: "POST", body: { email, password } });
      queryClient.setQueryData(["me"], user);
      router.replace("/pull-requests");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card title="Sign in">
      <form className="space-y-4" onSubmit={onSubmit}>
        {error && <Alert>{error}</Alert>}
        <Field label="Email">
          <Input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field label="Password">
          <Input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Button type="submit" className="w-full" loading={submitting}>
          Sign in
        </Button>
      </form>
      {setup.data?.github_login && (
        <>
          <p className="my-4 text-center text-xs uppercase tracking-wide text-slate-400">or</p>
          <a className={buttonClass("secondary", "w-full")} href="/api/v1/auth/github/start">
            Sign in with GitHub
          </a>
        </>
      )}
    </Card>
  );
}
