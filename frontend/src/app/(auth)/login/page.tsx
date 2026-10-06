"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { Alert, Button, Card, Field, Input, Spinner, buttonClass, errorMessage } from "@/components/ui";
import { api } from "@/lib/api";
import { useSetupStatus } from "@/lib/hooks";
import type { User } from "@/lib/types";

const SIGN_IN_ERRORS: Record<string, string> = {
  no_account: "No Reviewbot account matches that sign-in. Ask an admin for an invite.",
  not_in_allowed_group: "Your account is not in a group that may use Reviewbot. Ask your identity provider admin.",
  identity_mismatch: "That email belongs to an account linked to a different identity at this provider.",
  sso_denied: "Single sign-on was cancelled.",
  sso_state: "The sign-in expired. Try again.",
  sso_failed: "Single sign-on failed. Try again, or contact an admin.",
  sso_unavailable: "This sign-in method is not configured.",
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
    return code ? (SIGN_IN_ERRORS[code] ?? "Sign-in failed.") : null;
  });
  const [submitting, setSubmitting] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const providers = setup.data?.login_providers ?? [];
  const passwordMode = setup.data?.password_login ?? "all";
  const passwordForm = passwordMode === "all" || (passwordMode === "admins" && (showPassword || providers.length === 0));

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
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}
      {providers.length > 0 && (
        <div className="space-y-2">
          {providers.map((p) => (
            <a key={p.id} className={buttonClass(passwordMode === "all" ? "secondary" : "primary", "w-full")} href={p.start_url}>
              Sign in with {p.name}
            </a>
          ))}
        </div>
      )}
      {providers.length > 0 && passwordForm && (
        <p className="my-4 text-center text-xs uppercase tracking-wide text-slate-400">or</p>
      )}
      {passwordForm && (
        <form className="space-y-4" onSubmit={onSubmit}>
          {passwordMode === "admins" && <p className="text-xs text-slate-500">Password sign-in is limited to admins.</p>}
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
      )}
      {passwordMode === "admins" && !passwordForm && (
        <button type="button" className="mt-4 w-full text-center text-xs text-slate-500 hover:underline" onClick={() => setShowPassword(true)}>
          Admin sign-in with password
        </button>
      )}
    </Card>
  );
}
