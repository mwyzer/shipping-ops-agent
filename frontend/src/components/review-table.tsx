import type { DecisionOption, ReviewItem } from "@/lib/api";
import type { Draft } from "@/lib/review";

interface Props {
  items: ReviewItem[];
  options: DecisionOption[];
  subjectLabel: string;
  draft: Draft;
  readOnly: boolean;
  onDecide: (id: string, key: string) => void;
  onNote: (id: string, note: string) => void;
}

export function ReviewTable({ items, options, subjectLabel, draft, readOnly, onDecide, onNote }: Props) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Urgensi</th><th>{subjectLabel}</th><th>Masalah</th><th>Usulan agent</th><th>Keputusan</th>
          </tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr><td colSpan={6} className="notes">Tidak ada item untuk filter ini.</td></tr>
          )}
          {items.map((i) => {
            const d = draft[i.id] ?? {};
            const sources = i.sources ?? [];
            return (
              <tr key={i.id} className={d.decision ? "decided" : ""}>
                <td className="mono">{i.id}</td>
                <td><span className={`sev ${i.severity}`}>{i.severity}</span></td>
                <td className="mono">{i.subject}</td>
                <td className="problem">
                  {i.title}
                  {i.detail && <div className="detail">{i.detail}</div>}
                  <div className="src">{[i.group, ...sources].join(" · ")}</div>
                  {(i.expected || i.actual) && (
                    <div className="diff">
                      {i.expected && <ins>{i.expected}</ins>}
                      {i.expected && i.actual && " → "}
                      {i.actual && <s>{i.actual}</s>}
                    </div>
                  )}
                  {i.draft && (
                    <details className="draft">
                      <summary>Lihat draft</summary>
                      <pre>{i.draft}</pre>
                    </details>
                  )}
                </td>
                <td>{i.suggestion}</td>
                <td style={{ minWidth: 240 }}>
                  <div className={`seg${readOnly ? " readonly" : ""}`} role="group" aria-label={`Keputusan ${i.id}`}>
                    {options.map((o) => (
                      <button key={o.key} type="button" data-id={i.id} data-decision={o.key} disabled={readOnly}
                        className={`${o.tone}${d.decision === o.key ? " on" : ""}`} aria-pressed={d.decision === o.key}
                        onClick={() => onDecide(i.id, o.key)}>
                        {o.label}
                      </button>
                    ))}
                  </div>
                  {readOnly ? (
                    d.note && <div className="src">Catatan: {d.note}</div>
                  ) : (
                    <input className="note" type="text" data-note={i.id} placeholder="Catatan (opsional)" maxLength={500}
                      value={d.note ?? ""} onChange={(e) => onNote(i.id, e.target.value)} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
