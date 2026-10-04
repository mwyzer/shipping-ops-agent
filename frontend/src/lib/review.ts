/** Pure review logic, kept out of components so it can be unit-tested. */
import type { DecisionIn, DecisionOption, ReviewItem, RunStatus, Severity } from "./api";

export const SEVERITIES: Severity[] = ["HIGH", "MEDIUM", "LOW"];
export const SEVERITY_LABEL: Record<Severity, string> = { HIGH: "Tinggi", MEDIUM: "Sedang", LOW: "Rendah" };

export type Draft = Record<string, { decision?: string; note?: string }>;
export type Filter = "ALL" | "OPEN" | Severity;

export const STATUS_LABEL: Record<RunStatus, string> = {
  awaiting_review: "Menunggu review",
  done: "Selesai",
  answered: "Dijawab",
  error: "Error",
};

export function decidedCount(items: ReviewItem[], draft: Draft): number {
  return items.filter((i) => draft[i.id]?.decision).length;
}

export function visibleItems(items: ReviewItem[], draft: Draft, filter: Filter, group: string, query: string): ReviewItem[] {
  const q = query.trim().toLowerCase();
  return items.filter((i) => {
    if (filter === "OPEN" && draft[i.id]?.decision) return false;
    if (filter !== "ALL" && filter !== "OPEN" && i.severity !== filter) return false;
    if (group && i.group !== group) return false;
    return !q || `${i.subject} ${i.title} ${i.actual ?? ""}`.toLowerCase().includes(q);
  });
}

export function filterCount(items: ReviewItem[], draft: Draft, filter: Filter): number {
  return visibleItems(items, draft, filter, "", "").length;
}

export interface GroupStat {
  group: string;
  total: number;
  bySeverity: Record<Severity, number>;
}

/** Groups for the chart: most HIGH first, then largest. */
export function groupStats(items: ReviewItem[]): GroupStat[] {
  const map = new Map<string, GroupStat>();
  for (const i of items) {
    const g = map.get(i.group) ?? { group: i.group, total: 0, bySeverity: { HIGH: 0, MEDIUM: 0, LOW: 0 } };
    g.total += 1;
    g.bySeverity[i.severity] += 1;
    map.set(i.group, g);
  }
  return [...map.values()].sort((a, b) => b.bySeverity.HIGH - a.bySeverity.HIGH || b.total - a.total || a.group.localeCompare(b.group));
}

/** Toggle: clicking the chosen option again clears it. */
export function toggleDecision(draft: Draft, id: string, key: string): Draft {
  const cur = draft[id] ?? {};
  return { ...draft, [id]: { ...cur, decision: cur.decision === key ? undefined : key } };
}

export function decideAllOpen(draft: Draft, items: ReviewItem[], key: string): { draft: Draft; changed: number } {
  let changed = 0;
  const next = { ...draft };
  for (const i of items) {
    if (!next[i.id]?.decision) {
      next[i.id] = { ...next[i.id], decision: key };
      changed += 1;
    }
  }
  return { draft: next, changed };
}

/** Payload for the API; throws if anything is undecided so a partial review never leaves the browser. */
export function toPayload(items: ReviewItem[], draft: Draft): DecisionIn[] {
  return items.map((i) => {
    const d = draft[i.id];
    if (!d?.decision) throw new Error(`${i.id} belum diputuskan`);
    return { finding_id: i.id, decision: d.decision, note: d.note ?? "" };
  });
}

export function decisionSummary(options: DecisionOption[], decisions: { decision: string }[]): string {
  return options.map((o) => `${decisions.filter((d) => d.decision === o.key).length} ${o.label.toLowerCase()}`).join(", ");
}

const FILE_LABELS: [string, string][] = [
  ["audit_log", "Jejak audit (JSONL)"],
  ["impor_accounting", "File impor accounting (CSV)"],
  ["weekly_report", "Weekly report (Markdown)"],
  ["ringkasan_inbox", "Ringkasan inbox (Markdown)"],
  ["draft_email_20", "Draft email (.eml, ZIP)"],
  ["draft_quotation", "Draft quotation"],
  ["draft_notice", "Draft notice pelanggan"],
  ["draft_email", "Draft email konfirmasi"],
];

export function fileLabel(name: string): string {
  const hit = FILE_LABELS.find(([prefix]) => name.startsWith(prefix));
  if (hit) return hit[1];
  return name.endsWith(".xlsx") ? `Laporan Excel (${name})` : name;
}
