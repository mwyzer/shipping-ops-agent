import type { Kpi, ReviewItem, RunView } from "@/lib/api";
import { api } from "@/lib/api";
import { decisionSummary, fileLabel, groupStats, SEVERITIES, SEVERITY_LABEL, STATUS_LABEL } from "@/lib/review";

export function RunHeader({ run }: { run: RunView }) {
  return (
    <div className="head">
      <div>
        <h1>
          {run.title ?? run.label} <span className={`chip ${run.status}`}>{STATUS_LABEL[run.status]}</span>{" "}
          {run.agent_label && <span className="chip agent">{run.agent_label}</span>}
        </h1>
        <div className="sub">
          Koordinator → <b>{run.agent_label ?? run.route ?? "-"}</b> · {run.route_reason}
        </div>
        {run.request && <div className="sub">Permintaan: “{run.request}”</div>}
      </div>
      <div className="sub">Run {run.run_id}</div>
    </div>
  );
}

export function KpiCards({ kpis, decided, total }: { kpis: Kpi[]; decided: number; total: number }) {
  const pct = total ? Math.round((decided / total) * 100) : 100;
  const count = Math.min(kpis.length + 1, 5);
  return (
    <div className={`kpis n${count}`}>
      {kpis.map((k) => (
        <div className="card kpi" key={k.label}>
          <div className="label">{k.label}</div>
          <div className="value">{k.value}</div>
          <div className="detail">{k.detail}</div>
        </div>
      ))}
      <div className="card kpi">
        <div className="label">Progres review</div>
        <div className="value" data-testid="progress">{decided}/{total}</div>
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <span style={{ width: `${pct}%` }} />
        </div>
      </div>
    </div>
  );
}

export function GroupChart({ items }: { items: ReviewItem[] }) {
  const stats = groupStats(items);
  if (!stats.length) return <p className="notes">Tidak ada item.</p>;
  const max = Math.max(...stats.map((s) => s.total));
  return (
    <>
      <div className="bars">
        {stats.map((s) => (
          <div className="bar" key={s.group}>
            <span>{s.group}</span>
            <div className="track stack" style={{ width: `${(s.total / max) * 100}%` }}>
              {SEVERITIES.filter((sev) => s.bySeverity[sev]).map((sev) => (
                <div key={sev} className={`fill ${sev}`} style={{ flex: s.bySeverity[sev] }} title={`${SEVERITY_LABEL[sev]}: ${s.bySeverity[sev]}`} />
              ))}
            </div>
            <span className="n">{s.total}</span>
          </div>
        ))}
      </div>
      <div className="legend">
        {SEVERITIES.map((s) => (
          <span key={s}><i className={s} />{SEVERITY_LABEL[s]}</span>
        ))}
      </div>
    </>
  );
}

export function StepLog({ log }: { log: string[] }) {
  if (!log.length) return null;
  return (
    <details style={{ marginTop: 12 }}>
      <summary className="notes">Jejak langkah agent</summary>
      <ol className="log">{log.map((l, i) => <li key={i}>{l}</li>)}</ol>
    </details>
  );
}

export function ResultCard({ run }: { run: RunView }) {
  return (
    <div className="card">
      <h3>Hasil review</h3>
      <p>
        Diputuskan oleh <b>{run.decisions[0]?.reviewer ?? "-"}</b>: {decisionSummary(run.options, run.decisions)}.
      </p>
      <div className="files">
        {run.files.map((name) => (
          <a key={name} href={api.fileUrl(run.run_id, name)} download>
            ⬇ {fileLabel(name)}
          </a>
        ))}
      </div>
      <StepLog log={run.log} />
    </div>
  );
}
