"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useDark } from "@/lib/stores";

const LINKS = [
  { href: "/", label: "Market signals" },
  { href: "/slips", label: "Betting slips" },
];

export function Nav() {
  const path = usePathname();
  return (
    <header className="sticky top-0 z-30 border-b border-border bg-surface/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-2 px-4 sm:gap-6 sm:px-6">
        <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
          <span className="grid h-7 w-7 place-items-center rounded-lg bg-accent text-sm font-bold text-white dark:text-bg">
            A
          </span>
          <span className="hidden sm:inline">Arbibet</span>
        </Link>
        <nav className="flex gap-1">
          {LINKS.map((l) => {
            const active = l.href === "/" ? path === "/" : path.startsWith(l.href) || (l.href === "/slips" && path.startsWith("/fixture"));
            return (
              <Link
                key={l.href}
                href={l.href}
                className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  active ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface-2 hover:text-text"
                }`}
              >
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <ThemeToggle />
        </div>
      </div>
    </header>
  );
}

function ThemeToggle() {
  const dark = useDark();
  const flip = () => {
    const next = !dark;
    const root = document.documentElement;
    root.classList.toggle("dark", next);
    root.classList.toggle("light", !next);
    try {
      localStorage.setItem("theme", next ? "dark" : "light");
    } catch {}
  };
  return (
    <button
      onClick={flip}
      aria-label="Toggle dark mode"
      className="grid h-9 w-9 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-text"
    >
      {dark ? (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      )}
    </button>
  );
}
