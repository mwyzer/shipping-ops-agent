import type { Metadata } from "next";
import type { ReactNode } from "react";
import { Providers } from "@/components/providers";
import { Sidebar } from "@/components/sidebar";
import { TopBar } from "@/components/topbar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Shipping Ops Agent · Review",
  description: "Koordinator + 6 agent spesialis untuk operasional pelayaran, dengan review staf sebelum ada yang keluar.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="id">
      <body>
        <Providers>
          <TopBar />
          <div className="layout">
            <Sidebar />
            <main>{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
