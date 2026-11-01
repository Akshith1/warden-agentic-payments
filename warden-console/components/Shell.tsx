"use client";
import { Nav } from "./Nav";
import { useLive } from "@/lib/useLive";
import type { RunRow } from "@/lib/types";

export function Shell({ children }: { children: React.ReactNode }) {
  const { data } = useLive<RunRow[]>("/api/backend/runs?status=pending_human", 4000);
  return (
    <div className="shell">
      <Nav pending={data?.length} />
      <main className="page">{children}</main>
    </div>
  );
}
