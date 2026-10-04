"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type RunView } from "@/lib/api";
import {
  decideAllOpen, decidedCount, filterCount, toggleDecision, toPayload, visibleItems,
  type Draft, type Filter,
} from "@/lib/review";
import { storage } from "@/lib/storage";
import { useApp } from "./providers";
import { ReviewTable } from "./review-table";
import { GroupChart, KpiCards, ResultCard, RunHeader, StepLog } from "./run-parts";

const FILTERS: [Filter, string][] = [["ALL", "Semua"], ["HIGH", "Tinggi"], ["MEDIUM", "Sedang"], ["LOW", "Rendah"]];

export function RunScreen({ id }: { id: string }) {
  const { reviewer, refreshRuns, toast } = useApp();
  const [run, setRun] = useState<RunView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraftState] = useState<Draft>({});
  const [filter, setFilter] = useState<Filter>("ALL");
  const [group, setGroup] = useState("");
  const [query, setQuery] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const draftKey = `draft:${id}`;

  const load = useCallback((r: RunView) => {
    setRun(r);
    setDraftState(r.status === "done"
      ? Object.fromEntries(r.decisions.map((d) => [d.finding_id, { decision: d.decision, note: d.note }]))
      : storage.get<Draft>(`draft:${r.run_id}`, {}));
  }, []);

  useEffect(() => {
    setRun(null);
    setError(null);
    setFilter("ALL");
    setGroup("");
    setQuery("");
    api.run(id).then(load).catch((e: Error) => setError(e.message));
  }, [id, load]);

  const setDraft = (next: Draft) => {
    setDraftState(next);
    storage.set(draftKey, next);
  };

  const items = run?.items ?? [];
  const shown = useMemo(() => visibleItems(items, draft, filter, group, query), [items, draft, filter, group, query]);
  const groups = useMemo(() => [...new Set(items.map((i) => i.group))], [items]);

  if (error) return <div className="card error-card"><h3>Run tidak bisa dibuka</h3><p>{error}</p></div>;
  if (!run) return <div className="loading">Memuat run…</div>;

  if (run.status === "answered" || run.status === "error") {
    return (
      <>
        <RunHeader run={run} />
        <div className="card">
          <h3>Jawaban koordinator</h3>
          <p className="answer">{run.answer ?? "Tidak ada jawaban."}</p>
          <StepLog log={run.log} />
        </div>
      </>
    );
  }

  const reviewing = run.status === "awaiting_review";
  const decided = decidedCount(items, draft);
  const total = items.length;
  const filters: [Filter, string][] = reviewing ? [...FILTERS, ["OPEN", "Belum diputuskan"]] : FILTERS;
  const firstOption = run.options[0];

  async function submit() {
    if (!run) return;
    if (!reviewer.trim()) return toast("Isi nama reviewer di kanan atas dulu.");
    setSubmitting(true);
    try {
      const done = await api.decide(run.run_id, reviewer.trim(), toPayload(items, draft));
      storage.remove(draftKey);
      load(done);
      await refreshRuns();
      toast("Keputusan tersimpan. File hasil siap diunduh.");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <>
      <RunHeader run={run} />
      <KpiCards kpis={run.kpis} decided={decided} total={total} />
      <div className="grid2">
        <div className="card">
          <h3>Ringkasan agent</h3>
          <div className="narrative">{run.narrative}</div>
          {run.notes.length > 0 && <div className="notes">{run.notes.map((n) => <div key={n}>{n}</div>)}</div>}
        </div>
        <div className="card">
          <h3>Item per kelompok</h3>
          <GroupChart items={items} />
        </div>
      </div>

      {run.status === "done" && <ResultCard run={run} />}

      <div className="card">
        <div className="toolbar">
          <h3 style={{ margin: 0 }}>Review</h3>
          <div className="filters">
            {filters.map(([key, label]) => (
              <button key={key} type="button" data-filter={key} className={filter === key ? "on" : ""} onClick={() => setFilter(key)}>
                {label} {filterCount(items, draft, key)}
              </button>
            ))}
          </div>
          <select aria-label="Filter kelompok" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">Semua kelompok</option>
            {groups.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
          <input type="search" placeholder={`Cari ${run.subject_label.toLowerCase()}`} value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="spacer" />
          {reviewing && firstOption && (
            <button className="btn small" id="bulk" type="button" onClick={() => {
              const { draft: next, changed } = decideAllOpen(draft, shown, firstOption.key);
              setDraft(next);
              toast(`${changed} item diputuskan. Tinjau lagi sebelum mengirim.`);
            }}>
              {firstOption.label} semua yang tampil & belum diputuskan
            </button>
          )}
        </div>
        <ReviewTable items={shown} options={run.options} subjectLabel={run.subject_label} draft={draft} readOnly={!reviewing}
          onDecide={(itemId, key) => setDraft(toggleDecision(draft, itemId, key))}
          onNote={(itemId, note) => setDraft({ ...draft, [itemId]: { ...draft[itemId], note } })} />
      </div>

      {reviewing && (
        <div className="submitbar">
          <span>
            {decided === total
              ? <><b>Semua {total} item sudah diputuskan.</b> File hasil baru dibuat setelah dikirim.</>
              : <><b>{decided}/{total}</b> item diputuskan · {total - decided} tersisa</>}
          </span>
          <div className="spacer" />
          <button className="btn primary" id="submit" type="button" disabled={decided !== total || submitting} onClick={submit}>
            {submitting ? "Mengirim…" : "Kirim keputusan"}
          </button>
        </div>
      )}
    </>
  );
}
