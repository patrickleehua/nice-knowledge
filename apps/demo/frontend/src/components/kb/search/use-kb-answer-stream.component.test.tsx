// 流式 AI 解答的状态机 + 渲染联测:mock postSse 后按帧序驱动
// (sources → delta* → restart? → done / no_evidence / error),
// 断言面板增量渲染、done 裁剪、restart 清空、空态一键切原文与会话级缓存不重复计费。

import { fireEvent, render, screen } from "@testing-library/react";
import { act } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  KnowledgeAnswerEmptyState,
  KnowledgeAnswerPanel,
} from "@/components/kb/search/frontline-results";
import { useKbAnswerStream } from "@/components/kb/search/use-kb-answer-stream";
import { postSse } from "@/lib/api";
import type { KnowledgeAnswerSource, SearchHit } from "@/lib/types";

// 只替换 postSse,api 模块其余导出保持原样(ErrorState 等间接依赖它们)
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, postSse: vi.fn() };
});

const postSseMock = vi.mocked(postSse);

/** 当前活跃流的帧注入口,由 mockImplementation 在每次建流时刷新 */
let emit: (frame: unknown) => void = () => {
  throw new Error("postSse 尚未被调用");
};

function hit(name: string): SearchHit {
  return {
    kind: "product",
    layer: "tenant",
    kb_id: "kb-1",
    source: `product/${name}`,
    confidence: 0.9,
    data: {
      id: `id-${name}`,
      name,
      city: "巴黎",
      source_doc_id: "doc-1",
      // SearchHitData 必带 scores(检索排序分),夹具缺这项会让 tsc 整体失败
      scores: { native: {}, rrf: 0.5, rerank: null },
    },
    citation: {
      kind: "source_span",
      revision_id: "rev-1",
      source_doc_id: "doc-1",
      source_sha256: "hash",
      quote_text: `${name} 引文摘录`,
      chunk_id: "chunk-1",
      page: 2,
      start_line: 4,
      end_line: 6,
      cell_ref: null,
    },
  };
}

function sourcesFrame(refs: number[]): unknown {
  const sources: KnowledgeAnswerSource[] = refs.map((ref) => ({
    ref,
    hit: hit(`条款${ref}号`),
  }));
  return { type: "sources", sources };
}

/** 页面接线的最小复刻:no_evidence 走空态组件,其余交给面板 */
function Harness({
  query,
  onShowSources = () => undefined,
}: {
  query: string;
  onShowSources?: () => void;
}) {
  const answer = useKbAnswerStream(query);
  if (answer.status === "no_evidence") {
    return <KnowledgeAnswerEmptyState onShowSources={onShowSources} />;
  }
  return (
    <KnowledgeAnswerPanel
      status={answer.status}
      answerText={answer.answerText}
      sources={answer.sources}
      errorMessage={answer.errorMessage}
      onRetry={answer.retry}
    />
  );
}

beforeEach(() => {
  postSseMock.mockReset();
  postSseMock.mockImplementation((_path, _body, onEvent) => {
    emit = onEvent;
    // 流保持挂起,由测试用帧驱动;卸载 abort 后悬空 promise 无副作用
    return new Promise<never>(() => undefined);
  });
});

describe("useKbAnswerStream + KnowledgeAnswerPanel", () => {
  it("accumulates delta frames into incrementally rendered markdown", async () => {
    const { container } = render(<Harness query="delta 累积" />);

    expect(postSseMock).toHaveBeenCalledWith(
      "/kb/answer/stream",
      { query: "delta 累积", kb_ids: undefined },
      expect.any(Function),
      expect.any(AbortSignal),
    );

    await act(async () => emit(sourcesFrame([1, 2])));
    // sources 帧一到,答案依据区先行渲染
    expect(screen.getByText("答案依据")).toBeDefined();
    expect(container.querySelector("#source-1")).not.toBeNull();

    await act(async () => emit({ type: "delta", text: "推荐住在" }));
    expect(container.textContent).toContain("推荐住在");

    await act(async () => emit({ type: "delta", text: "右岸 [2]。" }));
    expect(container.textContent).toContain("推荐住在右岸 [2]。");
    // 未 done 时引用锚点已用候选全集判定
    expect(
      screen.getByRole("link", { name: "[2]" }).getAttribute("href"),
    ).toBe("#source-2");
  });

  it("trims sources to used_refs after done, keeping ref numbers", async () => {
    const { container } = render(<Harness query="done 裁剪" />);

    await act(async () => emit(sourcesFrame([1, 2, 3])));
    expect(container.querySelector("#source-2")).not.toBeNull();

    await act(async () => emit({ type: "delta", text: "结论 [1][3]" }));
    await act(async () => emit({ type: "done", used_refs: [1, 3] }));

    // 未被引用的 2 号候选被裁掉,1/3 号保留原编号
    expect(container.querySelector("#source-2")).toBeNull();
    expect(container.querySelector("#source-1")).not.toBeNull();
    expect(container.querySelector("#source-3")).not.toBeNull();
    expect(screen.getByText("基于 2 个可核验来源")).toBeDefined();
  });

  it("clears accumulated text on restart and re-accumulates", async () => {
    const { container } = render(<Harness query="restart 清空" />);

    await act(async () => emit(sourcesFrame([1])));
    await act(async () => emit({ type: "delta", text: "第一版草稿" }));
    expect(container.textContent).toContain("第一版草稿");

    await act(async () => emit({ type: "restart" }));
    expect(container.textContent).not.toContain("第一版草稿");

    await act(async () => emit({ type: "delta", text: "修正后答案" }));
    await act(async () => emit({ type: "done", used_refs: [1] }));
    expect(container.textContent).toContain("修正后答案");
    expect(container.textContent).not.toContain("第一版草稿");
  });

  it("shows the no-evidence empty state with a one-click switch to sources mode", async () => {
    const onShowSources = vi.fn();
    render(<Harness query="无证据" onShowSources={onShowSources} />);

    await act(async () => emit({ type: "no_evidence" }));

    expect(screen.getByText("现有资料不足以给出可靠答案")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "改用查原文检索" }));
    expect(onShowSources).toHaveBeenCalledTimes(1);
  });

  it("replays the cached answer on remount without calling postSse again", async () => {
    const first = render(<Harness query="缓存命中" />);
    await act(async () => emit(sourcesFrame([1])));
    await act(async () => emit({ type: "delta", text: "缓存的答案 [1]" }));
    await act(async () => emit({ type: "done", used_refs: [1] }));
    expect(postSseMock).toHaveBeenCalledTimes(1);
    first.unmount();

    // 同一 query 重新挂载:直接回放缓存,不再打接口(不重复计费)
    const second = render(<Harness query="缓存命中" />);
    expect(postSseMock).toHaveBeenCalledTimes(1);
    expect(second.container.textContent).toContain("缓存的答案");
    expect(second.container.querySelector("#source-1")).not.toBeNull();
  });

  it("surfaces stream errors and retry re-opens the stream", async () => {
    render(<Harness query="错误重试" />);

    await act(async () => emit(sourcesFrame([1])));
    await act(async () =>
      emit({ type: "error", code: "budget", message: "本月额度已用完" }),
    );
    expect(screen.getByText("本月额度已用完")).toBeDefined();
    expect(postSseMock).toHaveBeenCalledTimes(1);

    // 失败不缓存:重试显式重新建流
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重试" }));
    });
    expect(postSseMock).toHaveBeenCalledTimes(2);
  });
});
