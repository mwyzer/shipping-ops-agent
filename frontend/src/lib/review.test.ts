import { describe, expect, it } from "vitest";
import type { ReviewItem } from "./api";
import { decideAllOpen, decidedCount, fileLabel, groupStats, toPayload, toggleDecision, visibleItems } from "./review";

const item = (id: string, severity: ReviewItem["severity"], group: string, subject = id): ReviewItem => ({
  id, severity, group, subject, kind: "k", title: `title ${id}`, detail: "", sources: [], expected: null,
  actual: null, suggestion: "s", draft: null, data: {},
});

const items = [item("F001", "HIGH", "A", "MSKU1"), item("F002", "MEDIUM", "B"), item("F003", "LOW", "B"), item("F004", "HIGH", "B")];

describe("review logic", () => {
  it("filters by severity, open items, group and search", () => {
    const draft = { F001: { decision: "approve" } };
    expect(visibleItems(items, draft, "HIGH", "", "").map((i) => i.id)).toEqual(["F001", "F004"]);
    expect(visibleItems(items, draft, "OPEN", "", "").map((i) => i.id)).toEqual(["F002", "F003", "F004"]);
    expect(visibleItems(items, draft, "ALL", "B", "").length).toBe(3);
    expect(visibleItems(items, draft, "ALL", "", "msku").map((i) => i.id)).toEqual(["F001"]);
  });

  it("orders chart groups by HIGH count, then size", () => {
    const stats = groupStats(items);
    expect(stats.map((s) => s.group)).toEqual(["B", "A"]); // same HIGH count, B is larger
    expect(stats[0]?.bySeverity).toEqual({ HIGH: 1, MEDIUM: 1, LOW: 1 });
  });

  it("toggles a decision off when clicked twice", () => {
    const once = toggleDecision({}, "F001", "reject");
    expect(once.F001?.decision).toBe("reject");
    expect(toggleDecision(once, "F001", "reject").F001?.decision).toBeUndefined();
  });

  it("bulk-decides only undecided items and keeps notes", () => {
    const { draft, changed } = decideAllOpen({ F001: { decision: "reject" }, F002: { note: "cek" } }, items, "approve");
    expect(changed).toBe(3);
    expect(draft.F001?.decision).toBe("reject");
    expect(draft.F002).toEqual({ note: "cek", decision: "approve" });
    expect(decidedCount(items, draft)).toBe(4);
  });

  it("refuses to build a partial payload", () => {
    expect(() => toPayload(items, { F001: { decision: "approve" } })).toThrow("F002");
    const full = decideAllOpen({}, items, "approve").draft;
    expect(toPayload(items, full)).toHaveLength(4);
  });

  it("labels output files", () => {
    expect(fileLabel("audit_log.jsonl")).toBe("Jejak audit (JSONL)");
    expect(fileLabel("draft_email_20261004T1.zip")).toBe("Draft email (.eml, ZIP)");
    expect(fileLabel("draft_email_POL_GA123.txt")).toBe("Draft email konfirmasi");
    expect(fileLabel("quotation_x.xlsx")).toBe("Laporan Excel (quotation_x.xlsx)");
  });
});
