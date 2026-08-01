import { describe, expect, it } from "vitest";

import {
  canReclassifyDocument,
  reclassificationStatus,
  retryTargetDocType,
  shouldPollReclassification,
} from "./reclassification";
import type { SourceDocument } from "@/lib/types";

function doc(overrides: Partial<SourceDocument> = {}): SourceDocument {
  return {
    id: "doc-1",
    kb_id: "kb-1",
    filename: "手册.pdf",
    doc_type: "general",
    status: "completed",
    lifecycle_status: "active",
    latest_reclassification: null,
    ...overrides,
  } as SourceDocument;
}

describe("二次抽取重分类", () => {
  it("只有 active + 终态的 general 文档可以发起", () => {
    expect(canReclassifyDocument(doc())).toBe(true);
    expect(canReclassifyDocument(doc({ status: "parsing" }))).toBe(false);
    expect(canReclassifyDocument(doc({ lifecycle_status: "withdrawn" }))).toBe(
      false,
    );
    // 已经按某类型抽过的,不再重复抽(除非上次失败可重试)
    expect(canReclassifyDocument(doc({ doc_type: "policy" }))).toBe(false);
  });

  it("上次失败且可重试时允许重试,并沿用原目标类型", () => {
    const failed = doc({
      doc_type: "policy",
      latest_reclassification: {
        status: "failed",
        retryable: true,
        target_doc_type: "policy",
        error: "抽取超时",
      },
    } as Partial<SourceDocument>);
    expect(canReclassifyDocument(failed)).toBe(true);
    expect(retryTargetDocType(failed)).toBe("policy");
    expect(retryTargetDocType(doc())).toBe(null);
  });

  it("状态文案带上目标类型的展示名,解析不出就回落 type_key", () => {
    const running = doc({
      latest_reclassification: {
        status: "running" as const,
        retryable: false,
        target_doc_type: "policy",
        error: null,
      },
    } as Partial<SourceDocument>);
    expect(
      reclassificationStatus(running, (key) =>
        key === "policy" ? "政策条款" : key,
      ),
    ).toEqual({ text: "正在重新抽取为「政策条款」", failed: false });
    // 没给解析函数时原样回显 type_key,不猜
    expect(reclassificationStatus(running)?.text).toContain("policy");
    expect(reclassificationStatus(doc())).toBe(null);
  });

  it("排队中/执行中才轮询", () => {
    expect(shouldPollReclassification(doc())).toBe(false);
    expect(
      shouldPollReclassification(
        doc({
          latest_reclassification: {
            status: "queued",
            retryable: false,
            target_doc_type: "policy",
            error: null,
          },
        } as Partial<SourceDocument>),
      ),
    ).toBe(true);
  });
});
