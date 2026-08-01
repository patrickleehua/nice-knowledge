import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AnswerFeedback } from "@/components/kb/search/answer-feedback";
import type { SearchHit } from "@/lib/types";

const postMock = vi.hoisted(() => vi.fn());
const toastErrorMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  api: { post: postMock },
  ApiError: class ApiError extends Error {},
}));

vi.mock("sonner", () => ({
  toast: { error: toastErrorMock },
}));

function hit(name: string, sourceDocId?: string): SearchHit {
  return {
    kind: "chunk",
    layer: "tenant",
    kb_id: "kb-1",
    source: `docs/${name}`,
    confidence: 0.8,
    data: { scores: { native: {}, rrf: 0.1, rerank: null } },
    citation: sourceDocId
      ? {
          kind: "source_span",
          revision_id: "rev-1",
          source_doc_id: sourceDocId,
          source_sha256: "hash",
          quote_text: name,
          chunk_id: "chunk-1",
          page: 1,
          start_line: 1,
          end_line: 2,
          cell_ref: null,
        }
      : null,
  };
}

const props = {
  query: "冰岛冬季自驾要注意什么",
  answerText: "冬季自驾需关注路况 [1][2]。",
  sources: [
    { ref: 1, hit: hit("冰岛攻略.pdf", "doc-1") },
    { ref: 2, hit: hit("租车须知.md") },
  ],
};

beforeEach(() => {
  postMock.mockReset().mockResolvedValue({ id: "fb-1" });
  toastErrorMock.mockReset();
});

describe("AnswerFeedback", () => {
  it("点赞直接提交最小来源快照并置灰", async () => {
    render(<AnswerFeedback {...props} />);

    fireEvent.click(screen.getByRole("button", { name: "有帮助" }));

    await waitFor(() => {
      expect(screen.getByText("已收到反馈,谢谢")).toBeDefined();
    });
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock).toHaveBeenCalledWith("/kb/answer/feedback", {
      query: props.query,
      answer_text: props.answerText,
      rating: "up",
      comment: undefined,
      sources: [
        {
          ref: 1,
          kind: "chunk",
          layer: "tenant",
          source: "docs/冰岛攻略.pdf",
          source_doc_id: "doc-1",
        },
        // 无 citation 的来源不携带 source_doc_id
        { ref: 2, kind: "chunk", layer: "tenant", source: "docs/租车须知.md" },
      ],
    });
    // 置灰后不再有可点的赞/踩按钮
    expect(screen.queryByRole("button", { name: "有帮助" })).toBeNull();
  });

  it("点踩先展开原因输入,提交时携带 comment", async () => {
    render(<AnswerFeedback {...props} />);

    fireEvent.click(screen.getByRole("button", { name: "没帮助" }));
    // 展开输入阶段不应发请求
    expect(postMock).not.toHaveBeenCalled();

    const input = screen.getByLabelText("反馈原因");
    fireEvent.change(input, { target: { value: "  没有提到租车保险  " } });
    fireEvent.click(screen.getByRole("button", { name: "提交" }));

    await waitFor(() => {
      expect(screen.getByText("已收到反馈,谢谢")).toBeDefined();
    });
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toMatchObject({
      rating: "down",
      comment: "没有提到租车保险",
    });
    expect(screen.queryByLabelText("反馈原因")).toBeNull();
  });

  it("提交失败走 toast 且仍可重试", async () => {
    postMock.mockRejectedValueOnce(new Error("网络错误"));
    render(<AnswerFeedback {...props} />);

    fireEvent.click(screen.getByRole("button", { name: "有帮助" }));

    await waitFor(() => {
      expect(toastErrorMock).toHaveBeenCalledTimes(1);
    });
    // 失败不置灰,按钮仍可再点
    const upButton = screen.getByRole("button", { name: "有帮助" });
    expect(upButton.hasAttribute("disabled")).toBe(false);

    fireEvent.click(upButton);
    await waitFor(() => {
      expect(screen.getByText("已收到反馈,谢谢")).toBeDefined();
    });
    expect(postMock).toHaveBeenCalledTimes(2);
  });
});
