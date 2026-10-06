"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { SetupStatus, User } from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api<User>("/auth/me"), retry: false });
}

/** Role helpers mirroring the API's permission matrix (the API enforces it; the UI only hides controls). */
export function useRole() {
  const me = useMe();
  const role = me.data?.role;
  return { user: me.data, isAdmin: role === "admin", canReview: role === "admin" || role === "reviewer" };
}

export function useSetupStatus() {
  return useQuery({
    queryKey: ["setup-status"],
    queryFn: () => api<SetupStatus>("/setup/status"),
    retry: false,
  });
}
