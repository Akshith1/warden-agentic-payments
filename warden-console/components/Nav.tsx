"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { Me } from "@/lib/me";

const LINKS = [
  { href: "/", label: "Runs" },
  { href: "/submit", label: "Submit invoice" },
  { href: "/ledger", label: "Ledger" },
  { href: "/governance", label: "Governance" },
  { href: "/audit", label: "Audit log" },
];

export function Nav({ pending, me }: { pending?: number; me?: Me }) {
  const path = usePathname();
  return (
    <nav className="rail" aria-label="Main">
      <Link href="/" className="brand">Warden</Link>
      <ul>
        {LINKS.map((l) => {
          const current = l.href === "/" ? path === "/" || path.startsWith("/runs") : path.startsWith(l.href);
          return (
            <li key={l.href}>
              <Link href={l.href} aria-current={current ? "page" : undefined}>
                {l.label}
                {l.href === "/" && pending ? <span className="count" aria-label={`${pending} waiting`}>{pending}</span> : null}
              </Link>
            </li>
          );
        })}
      </ul>
      {me?.signedIn && (
        <div className="who">
          <span className="who-name">{me.name}</span>
          <span className="who-roles">{me.roles?.length ? me.roles.join(", ") : "no Warden role"}</span>
          {/* A full page load, so the server can end the Keycloak session as well. */}
          <a href="/api/auth/logout">Sign out</a>
        </div>
      )}
    </nav>
  );
}
