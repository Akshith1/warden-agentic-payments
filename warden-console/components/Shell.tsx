"use client";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { Nav } from "./Nav";
import { type Me, MeContext } from "@/lib/me";
import { useLive } from "@/lib/useLive";
import type { ControlStatus, RunRow } from "@/lib/types";
import { clock } from "@/lib/format";
import Link from "next/link";

export function Shell({ children }: { children: React.ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const path = usePathname();
  useEffect(() => {
    fetch("/api/auth/me", { cache: "no-store" }).then((r) => r.json()).then(setMe)
      .catch(() => setMe({ signedIn: false }));
  }, []);

  if (!me) return <div className="shell"><main className="page"><p className="muted">Loading…</p></main></div>;
  if (!me.signedIn) return <SignIn next={path} />;
  return (
    <MeContext.Provider value={me}>
      <SignedIn me={me}>{children}</SignedIn>
    </MeContext.Provider>
  );
}

function SignedIn({ me, children }: { me: Me; children: React.ReactNode }) {
  const { data } = useLive<RunRow[]>("/api/backend/runs?status=pending_human", 4000);
  const { data: control } = useLive<ControlStatus>("/api/control/status", 4000);
  return (
    <div className="shell">
      <Nav pending={data?.length} me={me} />
      <main className="page">
        {control?.global_stop && control.global && (
          <div className="kill-banner" role="alert">
            <strong>Payments are stopped.</strong> {control.global.reason} (by {control.global.by},
            since {clock(control.global.since)}). No money moves until a controller resumes payments.{" "}
            <Link href="/governance#kill-switch">Kill switch</Link>
          </div>
        )}
        {control && control.paused_agents.length > 0 && (
          <div className="kill-banner kill-banner-agent" role="status">
            Paused by the kill switch: {control.paused_agents.join(", ")}. Runs that need {control.paused_agents.length > 1 ? "them" : "it"} wait.{" "}
            <Link href="/governance#kill-switch">Kill switch</Link>
          </div>
        )}
        {children}
      </main>
    </div>
  );
}

function SignIn({ next }: { next: string }) {
  return (
    <main className="signin">
      <div className="signin-card">
        <p className="brand">Warden</p>
        <h1>Sign in to approve and pay</h1>
        <p className="muted">
          Sign-in is handled by Keycloak. Warden never sees your password, and every decision you make is
          recorded under your account.
        </p>
        <a className="button" href={`/api/auth/login?next=${encodeURIComponent(next)}`}>Sign in</a>
      </div>
    </main>
  );
}
