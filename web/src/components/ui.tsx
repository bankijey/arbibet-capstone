"use client";

import clsx from "clsx";
import { type ReactNode, useId, useState } from "react";

import { BOOK_OUTLINED, bookColour } from "@/lib/betting";

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={clsx("min-w-0 rounded-xl border border-border bg-surface shadow-[0_1px_2px_rgba(15,23,42,0.04)]", className)}>
      {children}
    </div>
  );
}

/** A collapsible page section. Open by default; the header stays clickable. */
export function Section({
  title,
  subtitle,
  children,
  defaultOpen = true,
  right,
  id,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  right?: ReactNode;
  id?: string;
}) {
  return (
    <details open={defaultOpen} id={id} className="group mt-6 min-w-0 rounded-xl border border-border bg-surface">
      <summary className="flex cursor-pointer select-none items-center gap-3 px-4 py-3.5 sm:px-5">
        <svg
          className="h-4 w-4 shrink-0 text-faint transition-transform group-open:rotate-90"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
        >
          <path d="m9 6 6 6-6 6" />
        </svg>
        <div className="min-w-0">
          <h2 className="text-base font-semibold tracking-tight sm:text-lg">{title}</h2>
          {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
        </div>
        {right && <div className="ml-auto" onClick={(e) => e.preventDefault()}>{right}</div>}
      </summary>
      <div className="min-w-0 border-t border-border px-4 pb-5 pt-4 sm:px-5">{children}</div>
    </details>
  );
}

export function SubHeading({ children, note }: { children: ReactNode; note?: ReactNode }) {
  return (
    <div className="mb-3 mt-7 first:mt-0">
      <h3 className="text-[15px] font-semibold tracking-tight">{children}</h3>
      {note && <p className="mt-1 max-w-3xl text-sm leading-relaxed text-muted">{note}</p>}
    </div>
  );
}

export function Metric({
  label,
  value,
  delta,
  tone,
  hint,
  small,
}: {
  label: string;
  value: ReactNode;
  delta?: ReactNode;
  tone?: "good" | "bad" | "warn" | "neutral";
  hint?: string;
  small?: boolean;
}) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-faint">
        {label}
        {hint && <InfoDot />}
      </div>
      <div className={clsx("mt-1 font-semibold tabular-nums tracking-tight", small ? "text-lg" : "text-2xl")}>{value}</div>
      {delta && (
        <div
          className={clsx(
            "mt-0.5 text-xs font-medium",
            tone === "good" && "text-good",
            tone === "bad" && "text-bad",
            tone === "warn" && "text-warn",
            (!tone || tone === "neutral") && "text-muted",
          )}
        >
          {delta}
        </div>
      )}
    </div>
  );
}

export function InfoDot() {
  return (
    <svg className="h-3.5 w-3.5 text-faint" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" />
    </svg>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "good" | "bad" | "warn" | "neutral" | "accent" }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium",
        tone === "good" && "bg-good-soft text-good",
        tone === "bad" && "bg-bad-soft text-bad",
        tone === "warn" && "bg-warn-soft text-warn",
        tone === "accent" && "bg-accent-soft text-accent",
        tone === "neutral" && "bg-surface-2 text-muted",
      )}
    >
      {children}
    </span>
  );
}

export function Callout({ children, tone = "neutral" }: { children: ReactNode; tone?: "good" | "bad" | "warn" | "neutral" | "accent" }) {
  return (
    <div
      className={clsx(
        "rounded-lg px-3.5 py-2.5 text-sm leading-relaxed",
        tone === "good" && "bg-good-soft text-good",
        tone === "bad" && "bg-bad-soft text-bad",
        tone === "warn" && "bg-warn-soft text-warn",
        tone === "accent" && "bg-accent-soft text-accent",
        tone === "neutral" && "bg-surface-2 text-muted",
      )}
    >
      {children}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { value: T; label: ReactNode }[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div role="tablist" className="mb-4 flex gap-1 border-b border-border">
      {tabs.map((t) => (
        <button
          key={t.value}
          role="tab"
          aria-selected={value === t.value}
          onClick={() => onChange(t.value)}
          className={clsx(
            "-mb-px border-b-2 px-3 py-2 text-sm font-medium transition-colors",
            value === t.value ? "border-accent text-text" : "border-transparent text-muted hover:text-text",
          )}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  label?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {label && <span className="text-sm text-muted">{label}</span>}
      <div className="inline-flex rounded-lg bg-surface-2 p-0.5">
        {options.map((o) => (
          <button
            key={o.value}
            onClick={() => onChange(o.value)}
            className={clsx(
              "rounded-md px-3 py-1 text-sm font-medium transition-colors",
              value === o.value ? "bg-surface text-text shadow-sm" : "text-muted hover:text-text",
            )}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2 text-sm text-muted">
      <button
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={clsx("relative h-5 w-9 rounded-full transition-colors", checked ? "bg-accent" : "bg-border")}
      >
        <span
          className={clsx(
            "absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          )}
        />
      </button>
      {label}
    </label>
  );
}

export function Select({
  label,
  value,
  onChange,
  options,
  className,
}: {
  label?: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  className?: string;
}) {
  const id = useId();
  return (
    <div className={clsx("min-w-0", className)}>
      {label && (
        <label htmlFor={id} className="mb-1 block text-xs font-medium text-muted">
          {label}
        </label>
      )}
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full truncate rounded-lg border border-border bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-sm text-muted">{children}</div>;
}

/** Table shell: horizontal scroll on narrow screens, sticky header. */
export function Table({ children, maxHeight }: { children: ReactNode; maxHeight?: number }) {
  return (
    <div className="overflow-auto rounded-lg border border-border" style={maxHeight ? { maxHeight } : undefined}>
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  );
}

export function Th({ children, right, className }: { children?: ReactNode; right?: boolean; className?: string }) {
  return (
    <th
      className={clsx(
        "sticky top-0 z-10 whitespace-nowrap border-b border-border bg-surface-2 px-3 py-2 text-xs font-medium text-muted",
        right ? "text-right" : "text-left",
        className,
      )}
    >
      {children}
    </th>
  );
}

export function Td({ children, right, className, title }: { children?: ReactNode; right?: boolean; className?: string; title?: string }) {
  return (
    <td
      title={title}
      className={clsx("border-b border-border px-3 py-2 align-middle", right && "text-right tabular-nums", className)}
    >
      {children}
    </td>
  );
}

export function BetLink({ url }: { url: string | null | undefined }) {
  if (!url) return <span className="text-faint">—</span>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 whitespace-nowrap font-medium text-accent hover:underline"
    >
      open
      <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
        <path d="M7 17 17 7M8 7h9v9" />
      </svg>
    </a>
  );
}

export function BookName({ book }: { book: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <BookDot book={book} />
      {book}
    </span>
  );
}

export function BookDot({ book }: { book: string }) {
  return (
    <span
      className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
      style={{
        background: bookColour(book),
        boxShadow: BOOK_OUTLINED.has(book) ? "0 0 0 1.5px #111" : undefined,
      }}
    />
  );
}

export function useToggleSet(initial: string[] = []) {
  const [set, setSet] = useState(() => new Set(initial));
  const toggle = (key: string) =>
    setSet((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  return [set, toggle] as const;
}

/** Model-written text: paragraphs and **bold**, nothing else rendered. */
export function Prose({ text }: { text: string }) {
  return (
    <div className="prose-summary text-[15px] leading-relaxed text-text/90">
      {text.split(/\n\s*\n/).map((para, i) => (
        <p key={i}>
          {para.split(/(\*\*[^*]+\*\*)/g).map((part, j) =>
            part.startsWith("**") && part.endsWith("**") ? <strong key={j}>{part.slice(2, -2)}</strong> : part,
          )}
        </p>
      ))}
    </div>
  );
}
