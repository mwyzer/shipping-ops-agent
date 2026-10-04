"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type AgentInfo, type RunView } from "@/lib/api";
import { STATUS_LABEL } from "@/lib/review";
import { useApp } from "./providers";

const ACCEPT = ".xlsx,.xls,.csv,.txt,.eml";

export function Sidebar() {
  const { runs, refreshRuns, toast } = useApp();
  const router = useRouter();
  const pathname = usePathname();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [request, setRequest] = useState("");
  const [agent, setAgent] = useState("");
  const [label, setLabel] = useState("Upload");
  const [ask, setAsk] = useState("");
  const filesRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.agents().then(setAgents).catch((e: Error) => toast(`Backend tidak bisa dihubungi: ${e.message}`));
  }, [toast]);

  async function start(key: string, call: () => Promise<RunView>) {
    setBusy(key);
    try {
      const run = await call();
      await refreshRuns();
      router.push(`/runs/${run.run_id}`);
      return true;
    } catch (e) {
      toast((e as Error).message);
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function onUpload(e: FormEvent) {
    e.preventDefault();
    const files = Array.from(filesRef.current?.files ?? []);
    if (!files.length) return toast("Pilih file dulu.");
    if (await start("upload", () => api.upload(files, { request, agent, label }))) {
      setRequest("");
      if (filesRef.current) filesRef.current.value = "";
    }
  }

  async function onAsk(e: FormEvent) {
    e.preventDefault();
    if (await start("ask", () => api.ask(ask))) setAsk("");
  }

  return (
    <aside>
      <section>
        <h2>Coba dengan data contoh</h2>
        <div className="agents">
          {agents.length === 0 && <span className="notes">Memuat agent…</span>}
          {agents.map((a) => (
            <button key={a.name} type="button" data-demo={a.name} disabled={busy !== null}
              onClick={() => start(a.name, () => api.demo(a.name))}>
              <b>{busy === a.name ? "Memproses…" : a.label}</b>
              <span>{a.description}</span>
            </button>
          ))}
        </div>
      </section>

      <section>
        <h2>Agent Email: inbox nyata</h2>
        <p className="notes">Ambil email baru dari mailbox ops via IMAP, lalu pilah seperti biasa.</p>
        <button className="btn block" type="button" disabled={busy !== null}
          onClick={() => start("fetch-inbox", () => api.fetchInbox())}>
          {busy === "fetch-inbox" ? "Mengambil…" : "Ambil Email Baru (IMAP)"}
        </button>
      </section>

      <section>
        <h2>Unggah file sendiri</h2>
        <form onSubmit={onUpload}>
          <label className="field" htmlFor="request">Apa yang perlu dikerjakan?</label>
          <input id="request" type="text" placeholder="mis. cocokkan manifest dengan container list"
            value={request} onChange={(e) => setRequest(e.target.value)} />
          <label className="field" htmlFor="agent-select">Agent</label>
          <select id="agent-select" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">Otomatis (dipilih koordinator)</option>
            {agents.map((a) => <option key={a.name} value={a.name}>{a.label}</option>)}
          </select>
          <label className="field" htmlFor="files">File (.xlsx, .csv, .txt, .eml)</label>
          <input id="files" ref={filesRef} type="file" multiple accept={ACCEPT} required />
          <label className="field" htmlFor="label">Nama run</label>
          <input id="label" type="text" maxLength={60} value={label} onChange={(e) => setLabel(e.target.value)} />
          <button className="btn block" style={{ marginTop: 10 }} type="submit" disabled={busy !== null}>
            {busy === "upload" ? "Memproses…" : "Jalankan"}
          </button>
        </form>
      </section>

      <section>
        <h2>Tanya koordinator</h2>
        <form id="ask-form" onSubmit={onAsk}>
          <input id="ask" type="text" placeholder="mis. kapal delay, closing CY berubah" required
            value={ask} onChange={(e) => setAsk(e.target.value)} />
          <button className="btn block" style={{ marginTop: 8 }} type="submit" disabled={busy !== null}>
            {busy === "ask" ? "Memproses…" : "Kirim"}
          </button>
        </form>
      </section>

      <section>
        <h2>Riwayat run</h2>
        <ul className="runs">
          {runs.length === 0 && <li className="notes">Belum ada run.</li>}
          {runs.map((r) => (
            <li key={r.run_id}>
              <Link href={`/runs/${r.run_id}`} className={pathname === `/runs/${r.run_id}` ? "active" : ""}>
                <div><b>{r.label}</b></div>
                <div className="meta">
                  <span>
                    {new Date(r.created_at).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" })} · {r.items} item
                  </span>
                  <span className={`chip ${r.status}`}>{STATUS_LABEL[r.status]}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </aside>
  );
}
