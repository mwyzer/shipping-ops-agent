"use client";

import Link from "next/link";
import { useApp } from "./providers";

export function TopBar() {
  const { reviewer, setReviewer } = useApp();
  return (
    <header className="topbar">
      <Link href="/" className="brand" style={{ color: "inherit", textDecoration: "none" }}>
        Shipping Ops Agent<small>Koordinator + 6 agent spesialis · review staf</small>
      </Link>
      <div className="spacer" />
      <label className="reviewer">
        Reviewer
        <input type="text" placeholder="nama Anda" autoComplete="name" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
      </label>
      <a href="/api-docs" target="_blank" rel="noopener">
        API docs
      </a>
    </header>
  );
}
