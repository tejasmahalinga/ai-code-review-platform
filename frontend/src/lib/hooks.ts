"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { User } from "./types";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api<User>("/auth/me"), retry: false });
}

export function useSetupStatus() {
  return useQuery({
    queryKey: ["setup-status"],
    queryFn: () => api<{ needs_setup: boolean }>("/setup/status"),
    retry: false,
  });
}
