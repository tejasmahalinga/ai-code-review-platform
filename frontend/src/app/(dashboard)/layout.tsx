"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { Alert, Button, Spinner, cx } from "@/components/ui";
import { api, isUnauthenticated } from "@/lib/api";
import { useMe } from "@/lib/hooks";

const NAV = [
  { href: "/pull-requests", label: "Pull requests", admin: false },
  { href: "/repositories", label: "Repositories", admin: false },
  { href: "/settings/keys", label: "LLM keys", admin: true },
  { href: "/settings/integrations", label: "Integrations", admin: true },
  { href: "/settings/team", label: "Team", admin: true },
  { href: "/settings/audit", label: "Audit log", admin: true },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const me = useMe();
  const router = useRouter();
  const pathname = usePathname();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (me.error && isUnauthenticated(me.error)) router.replace("/login");
  }, [me.error, router]);

  async function logout() {
    await api("/auth/logout", { method: "POST" });
    queryClient.clear();
    router.replace("/login");
  }

  if (me.isLoading || (me.error && isUnauthenticated(me.error))) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner />
      </div>
    );
  }
  if (me.error) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <Alert>Could not reach the API. Check that the backend is running.</Alert>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <Link href="/pull-requests" className="font-semibold tracking-tight">
            Reviewbot
          </Link>
          <nav className="flex flex-1 flex-wrap gap-1" aria-label="Main">
            {NAV.filter((item) => !item.admin || me.data?.role === "admin").map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={cx(
                  "rounded-md px-3 py-1.5 text-sm",
                  pathname.startsWith(item.href) ? "bg-slate-900 text-white" : "text-slate-700 hover:bg-slate-100",
                )}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="flex items-center gap-3 text-sm text-slate-600">
            <Link href="/settings/account" className="hover:underline" title="Your account">
              {me.data?.name || me.data?.email}
            </Link>
            {me.data?.role !== "admin" && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs">{me.data?.role}</span>}
            <Button variant="ghost" onClick={logout}>
              Sign out
            </Button>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-8">{children}</main>
    </div>
  );
}
