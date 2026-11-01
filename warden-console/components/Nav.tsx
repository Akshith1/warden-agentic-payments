"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Runs" },
  { href: "/submit", label: "Submit invoice" },
  { href: "/ledger", label: "Ledger" },
  { href: "/governance", label: "Governance" },
];

export function Nav({ pending }: { pending?: number }) {
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
    </nav>
  );
}
