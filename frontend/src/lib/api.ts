/**
 * Typed client for the FastAPI backend.
 * Types come from `src/lib/api-types.ts`, generated from the backend's OpenAPI
 * schema (`npm run gen:api`), so a backend change that breaks the contract
 * fails `npm run typecheck`.
 */
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type AgentInfo = Schemas["AgentInfo"];
export type RunView = Schemas["RunView"];
export type RunListItem = Schemas["RunListItem"];
export type RunStatus = RunView["status"];
export type ReviewItem = Schemas["ReviewItem"];
export type Severity = Schemas["Severity"];
export type DecisionOption = Schemas["DecisionOption"];
export type Kpi = Schemas["Kpi"];
export type DecisionIn = Schemas["DecisionIn"];

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

type ErrorDetail = string | { message?: string; missing?: string[]; invalid?: string[] } | { msg: string }[];

function describe(detail: ErrorDetail | undefined, fallback: string): string {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  const extra = detail.missing?.length ? `: ${detail.missing.join(", ")}` : detail.invalid?.length ? `: ${detail.invalid.join(", ")}` : "";
  return `${detail.message ?? fallback}${extra}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-store", ...init });
  const body = (await res.json().catch(() => ({}))) as { detail?: ErrorDetail };
  if (!res.ok) throw new ApiError(describe(body.detail, res.statusText || "Permintaan gagal"), res.status);
  return body as T;
}

const json = (data: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data),
});

export const api = {
  agents: () => request<AgentInfo[]>("/api/agents"),
  runs: () => request<RunListItem[]>("/api/runs"),
  run: (id: string) => request<RunView>(`/api/runs/${encodeURIComponent(id)}`),
  demo: (agent: string) => request<RunView>("/api/runs/demo", json({ agent })),
  ask: (text: string) => {
    const fd = new FormData();
    fd.append("request", text);
    return request<RunView>("/api/ask", { method: "POST", body: fd });
  },
  upload: (files: File[], opts: { request: string; agent: string; label: string }) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    fd.append("request", opts.request);
    fd.append("agent", opts.agent);
    fd.append("label", opts.label || "Upload");
    return request<RunView>("/api/runs", { method: "POST", body: fd });
  },
  decide: (id: string, reviewer: string, decisions: DecisionIn[]) =>
    request<RunView>(`/api/runs/${encodeURIComponent(id)}/decisions`, json({ reviewer, decisions })),
  fileUrl: (id: string, name: string) => `/api/runs/${encodeURIComponent(id)}/files/${encodeURIComponent(name)}`,
};
