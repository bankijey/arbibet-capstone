import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import { Nav } from "@/components/Nav";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: { default: "Arbibet", template: "%s · Arbibet" },
  description:
    "Cross-bookmaker market signals: surebets, positive EV and booking slips, measured across five bookmakers.",
};

// Set the theme class before first paint: stored choice, else the system's.
const themeScript = `(function(){try{var t=localStorage.getItem('theme');var d=t?t==='dark':matchMedia('(prefers-color-scheme: dark)').matches;document.documentElement.classList.add(d?'dark':'light')}catch(e){}})()`;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="min-h-full flex flex-col font-sans text-[15px]">
        <Nav />
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 pb-16 pt-6 sm:px-6">{children}</main>
        <footer className="border-t border-border py-6 text-center text-xs text-faint">
          Arbibet · reference implementation, not a betting service · every figure is a reading taken
          at the time shown
        </footer>
      </body>
    </html>
  );
}
