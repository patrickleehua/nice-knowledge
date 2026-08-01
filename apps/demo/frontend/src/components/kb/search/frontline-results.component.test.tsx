import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { KnowledgeAnswerPanel } from "@/components/kb/search/frontline-results";
import type { KnowledgeAnswerSource, SearchHit } from "@/lib/types";

function hit(name: string): SearchHit {
  return {
    kind: "policy",
    layer: "tenant",
    kb_id: "kb-1",
    source: `entity/${name}`,
    confidence: 0.9,
    data: {
      id: `id-${name}`,
      name,
      department: "合规",
      source_doc_id: "doc-1",
      // SearchHitData 必带 scores(检索排序分),夹具缺这项会让 tsc 整体失败
      scores: { native: {}, rrf: 0.5, rerank: null },
    },
    citation: {
      kind: "source_span",
      revision_id: "rev-1",
      source_doc_id: "doc-1",
      source_sha256: "hash",
      quote_text: `${name} 条款 220 号`,
      chunk_id: "chunk-1",
      page: 2,
      start_line: 4,
      end_line: 6,
      cell_ref: null,
    },
  };
}

const sources: KnowledgeAnswerSource[] = [
  { ref: 1, hit: hit("左岸制度") },
  { ref: 2, hit: hit("右岸制度") },
];

describe("KnowledgeAnswerPanel", () => {
  it("renders the answer with citation links into numbered source cards", () => {
    const { container } = render(
      <KnowledgeAnswerPanel
        status="success"
        answerText="推荐右岸制度，见 220 号条款 [2]。"
        sources={sources}
      />,
    );

    // 正文里的 [2] 变成指向来源卡的锚点链接
    const citation = screen.getByRole("link", { name: "[2]" });
    expect(citation.getAttribute("href")).toBe("#source-2");

    // 来源卡按编号可定位(source 整包传入,ref 编号不能被 React 吞掉)
    const card = container.querySelector("#source-2");
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain("右岸制度");
    expect(card?.textContent).toContain("右岸制度 条款 220 号");
    expect(screen.getByText("基于 2 个可核验来源")).toBeDefined();
  });

  it("renders nothing before a question is submitted", () => {
    const { container } = render(
      <KnowledgeAnswerPanel status="idle" answerText="" sources={[]} />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("shows the error message with a retry action", () => {
    const onRetry = vi.fn();
    render(
      <KnowledgeAnswerPanel
        status="error"
        answerText=""
        sources={sources}
        errorMessage="本月 AI 解答额度已用完"
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText("本月 AI 解答额度已用完")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
