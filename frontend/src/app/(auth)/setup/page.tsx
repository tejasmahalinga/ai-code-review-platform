"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Alert, Button, Card, Field, Input, Spinner } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useSetupStatus } from "@/lib/hooks";
import type { User } from "@/lib/types";

export default function SetupPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const status = useSetupStatus();
  const [form, setForm] = useState({ name: "", email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (status.data && !status.data.needs_setup) router.replace("/login");
  }, [status.data, router]);

  if (status.isLoading) return <Spinner />;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setErrors({});
    setError(null);
    if (form.password !== form.confirm) {
      setErrors({ confirm: "Passwords do not match." });
      return;
    }
    setSubmitting(true);
    try {
      const user = await api<User>("/setup", {
        method: "POST",
        body: { name: form.name, email: form.email, password: form.password },
      });
      queryClient.setQueryData(["me"], user);
      queryClient.setQueryData(["setup-status"], { needs_setup: false });
      router.replace("/settings/keys?welcome=1");
    } catch (err) {
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors());
        setError(err.message);
      } else setError("Setup failed.");
    } finally {
      setSubmitting(false);
    }
  }

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  return (
    <Card title="Create the admin account">
      <p className="mb-4 text-sm text-slate-600">
        This instance has no users yet. The first account becomes the administrator.
      </p>
      <form className="space-y-4" onSubmit={onSubmit}>
        {error && <Alert>{error}</Alert>}
        <Field label="Name">
          <Input value={form.name} onChange={set("name")} autoComplete="name" />
        </Field>
        <Field label="Email" error={errors.email}>
          <Input type="email" required value={form.email} onChange={set("email")} autoComplete="username" />
        </Field>
        <Field label="Password" hint="At least 10 characters." error={errors.password}>
          <Input type="password" required minLength={10} value={form.password} onChange={set("password")} autoComplete="new-password" />
        </Field>
        <Field label="Confirm password" error={errors.confirm}>
          <Input type="password" required value={form.confirm} onChange={set("confirm")} autoComplete="new-password" />
        </Field>
        <Button type="submit" className="w-full" loading={submitting}>
          Create admin
        </Button>
      </form>
    </Card>
  );
}
