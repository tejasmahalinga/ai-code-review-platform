"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  errorMessage,
  formatDate,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { useRole } from "@/lib/hooks";
import type { CreatedInvite, Invite, Role, User } from "@/lib/types";
import { ROLES } from "@/lib/types";

const INVITE_TONE = { pending: "sky", accepted: "green", revoked: "slate", expired: "amber" } as const;

export default function TeamPage() {
  const { user: me, isAdmin } = useRole();
  if (me && !isAdmin) return <Alert>Only admins can manage the team.</Alert>;
  return (
    <>
      <PageHeader
        title="Team"
        description="Invite people and choose what they can do. Developers don't need an account to get reviews on their pull requests."
      />
      <div className="space-y-6">
        <InviteCard />
        <UsersCard meId={me?.id} />
        <InvitesCard />
        <RolesCard />
      </div>
    </>
  );
}

function InviteCard() {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("reviewer");
  const [created, setCreated] = useState<CreatedInvite | null>(null);
  const [copied, setCopied] = useState(false);
  const invite = useMutation({
    mutationFn: () => api<CreatedInvite>("/invites", { method: "POST", body: { email, role } }),
    onSuccess: (data) => {
      setCreated(data);
      setCopied(false);
      setEmail("");
      queryClient.invalidateQueries({ queryKey: ["invites"] });
    },
  });
  const fieldError = invite.error instanceof ApiError ? invite.error.fieldErrors().email : undefined;

  return (
    <Card title="Invite someone">
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          invite.mutate();
        }}
      >
        <div className="min-w-64 flex-1">
          <Field label="Email" error={fieldError}>
            <Input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="dev@company.com" />
          </Field>
        </div>
        <Field label="Role">
          <Select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </Select>
        </Field>
        <Button type="submit" loading={invite.isPending}>
          Create invite
        </Button>
      </form>
      {invite.error && !fieldError && (
        <div className="mt-3">
          <Alert>{errorMessage(invite.error)}</Alert>
        </div>
      )}
      {created && (
        <div className="mt-4 space-y-2">
          <Alert kind="success">
            Invite for {created.email} created.{" "}
            {created.email_sent
              ? "We emailed the link. You can also share it yourself:"
              : "Email is not configured, so share this link with them:"}
          </Alert>
          <div className="flex gap-2">
            <Input readOnly value={created.url} onFocus={(e) => e.target.select()} aria-label="Invite link" />
            <Button
              variant="secondary"
              onClick={async () => {
                await navigator.clipboard.writeText(created.url);
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
          <p className="text-xs text-slate-500">
            This link is shown only once and expires {formatDate(created.expires_at)}. It works once.
          </p>
        </div>
      )}
    </Card>
  );
}

function UsersCard({ meId }: { meId?: number }) {
  const queryClient = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<User[]>("/users") });
  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Partial<Pick<User, "role" | "is_active">> }) =>
      api<User>(`/users/${id}`, { method: "PATCH", body }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  return (
    <Card title="Members">
      {update.error && (
        <div className="mb-3">
          <Alert>{errorMessage(update.error)}</Alert>
        </div>
      )}
      {users.isLoading ? (
        <Spinner />
      ) : users.error ? (
        <Alert>{errorMessage(users.error)}</Alert>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-200 text-sm">
            <thead className="text-left text-xs font-medium uppercase tracking-wide text-slate-500">
              <tr>
                <th className="py-2 pr-4">User</th>
                <th className="py-2 pr-4">Role</th>
                <th className="py-2 pr-4">Sign-in</th>
                <th className="py-2 pr-4">Last sign-in</th>
                <th className="py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {users.data!.map((u) => (
                <tr key={u.id} className={u.is_active ? "" : "opacity-60"}>
                  <td className="py-2 pr-4">
                    <div className="font-medium">
                      {u.name || u.email} {u.id === meId && <Badge>you</Badge>} {!u.is_active && <Badge tone="amber">deactivated</Badge>}
                    </div>
                    {u.name && <div className="text-xs text-slate-500">{u.email}</div>}
                  </td>
                  <td className="py-2 pr-4">
                    <Select
                      value={u.role}
                      aria-label={`Role for ${u.email}`}
                      disabled={!u.is_active || update.isPending}
                      onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value as Role } })}
                    >
                      {ROLES.map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.label}
                        </option>
                      ))}
                    </Select>
                  </td>
                  <td className="py-2 pr-4 text-xs text-slate-600">
                    {[u.has_password && "password", u.github_login && `GitHub @${u.github_login}`].filter(Boolean).join(" · ") || "—"}
                  </td>
                  <td className="py-2 pr-4 text-slate-600">{formatDate(u.last_login)}</td>
                  <td className="py-2 text-right">
                    {u.id !== meId &&
                      (u.is_active ? (
                        <Button
                          variant="danger"
                          onClick={() => {
                            if (confirm(`Deactivate ${u.email}? They are signed out immediately.`))
                              update.mutate({ id: u.id, body: { is_active: false } });
                          }}
                        >
                          Deactivate
                        </Button>
                      ) : (
                        <Button variant="secondary" onClick={() => update.mutate({ id: u.id, body: { is_active: true } })}>
                          Reactivate
                        </Button>
                      ))}
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

function InvitesCard() {
  const queryClient = useQueryClient();
  const invites = useQuery({ queryKey: ["invites"], queryFn: () => api<Invite[]>("/invites") });
  const revoke = useMutation({
    mutationFn: (id: number) => api(`/invites/${id}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["invites"] }),
  });
  const open = (invites.data ?? []).filter((i) => i.status === "pending" || i.status === "expired");
  if (invites.isLoading || open.length === 0) return null;

  return (
    <Card title="Open invites">
      {revoke.error && (
        <div className="mb-3">
          <Alert>{errorMessage(revoke.error)}</Alert>
        </div>
      )}
      <ul className="divide-y divide-slate-100 text-sm">
        {open.map((i) => (
          <li key={i.id} className="flex flex-wrap items-center gap-3 py-2">
            <span className="font-medium">{i.email}</span>
            <Badge>{i.role}</Badge>
            <Badge tone={INVITE_TONE[i.status]}>{i.status}</Badge>
            <span className="text-xs text-slate-500">
              {i.status === "expired" ? "expired" : "expires"} {formatDate(i.expires_at)}
              {i.created_by_email && ` · invited by ${i.created_by_email}`}
            </span>
            {i.status === "pending" && (
              <Button variant="ghost" className="ml-auto" onClick={() => revoke.mutate(i.id)}>
                Revoke
              </Button>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}

function RolesCard() {
  return (
    <Card title="Roles">
      <dl className="grid gap-3 text-sm md:grid-cols-3">
        {ROLES.map((r) => (
          <div key={r.id}>
            <dt className="font-medium">{r.label}</dt>
            <dd className="text-slate-600">{r.description}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}
