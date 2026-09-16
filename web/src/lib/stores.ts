"use client";

import { useSyncExternalStore } from "react";

/** Whether html has the `dark` class, following the theme toggle. */
export function useDark(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const observer = new MutationObserver(onChange);
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
      return () => observer.disconnect();
    },
    () => document.documentElement.classList.contains("dark"),
    () => false,
  );
}

const LOCAL_EVENT = "arbibet-local-storage";

/** A localStorage entry as raw text, re-read when this tab or another writes it. */
export function useLocalText(key: string, fallback: string): string {
  return useSyncExternalStore(
    (onChange) => {
      window.addEventListener("storage", onChange);
      window.addEventListener(LOCAL_EVENT, onChange);
      return () => {
        window.removeEventListener("storage", onChange);
        window.removeEventListener(LOCAL_EVENT, onChange);
      };
    },
    () => {
      try {
        return localStorage.getItem(key) ?? fallback;
      } catch {
        return fallback;
      }
    },
    () => fallback,
  );
}

export function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {}
  window.dispatchEvent(new Event(LOCAL_EVENT));
}
