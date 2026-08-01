import { fireEvent, render, screen } from "@testing-library/react";
import Markdown from "react-markdown";
import { expect, test, vi } from "vitest";
import type { ToolRun } from "@/lib/agent-events";
import {
  agentMarkdownComponents,
  sanitizeAgentMarkdown,
} from "./agent-markdown";
import { ToolRunCard } from "./tool-run-card";

function AnswerWithCitations({ text }: { text: string }) {
  return (
    <Markdown components={agentMarkdownComponents}>
      {sanitizeAgentMarkdown(text)}
    </Markdown>
  );
}

function searchRun(output: Record<string, unknown>): ToolRun {
  return {
    id: "run-1",
    name: "web_search",
    progress: [],
    status: "ok",
    output,
  };
}

function daysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
}

function openCard(label: string) {
  fireEvent.click(screen.getByRole("button", { name: new RegExp(label) }));
}

test("renders web_search sources with refs, tiers, freshness and folding", () => {
  const results = Array.from({ length: 9 }, (_, index) => ({
    ref: index + 1,
    title: `来源标题 ${index + 1}`,
    url: `https://a${index + 1}.example.com/page`,
    domain: `a${index + 1}.example.com`,
    snippet: "摘要内容",
    published_at: index === 0 ? daysAgo(500) : daysAgo(2),
    source_tier: index === 0 ? "official" : index === 1 ? "media" : "unknown",
  }));
  render(
    <ToolRunCard
      run={searchRun({
        status: "ok",
        provider: "tavily",
        cached: true,
        count: results.length,
        queries: ["泰国落地签材料", "泰国入境新规"],
        results,
      })}
    />,
  );

  expect(screen.getByText("缓存 · 9 条来源 · 1 官方")).toBeTruthy();
  openCard("联网搜索");

  expect(screen.getByText(/tavily · 9 条来源/)).toBeTruthy();
  expect(screen.getByText("检索式 2：泰国入境新规")).toBeTruthy();
  expect(screen.getByText("官方")).toBeTruthy();
  expect(screen.getByText("媒体")).toBeTruthy();
  expect(screen.getByText("[1]")).toBeTruthy();
  expect(screen.getByText(/信息可能过时/)).toBeTruthy();
  expect(document.getElementById("cite-1")?.dataset.ref).toBe("1");

  // 默认只展示 8 条,展开后补齐第 9 条。
  expect(screen.queryByText("来源标题 9")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "显示全部 9 条" }));
  expect(screen.getByText("来源标题 9")).toBeTruthy();
});

test("renders web_fetch pages per status", () => {
  render(
    <ToolRunCard
      run={{
        id: "run-2",
        name: "web_fetch",
        progress: [],
        status: "ok",
        output: {
          status: "partial",
          count: 3,
          pages: [
            {
              ref: 1,
              url: "https://gov.example.cn/a",
              final_url: "https://gov.example.cn/a?v=2",
              title: "签证须知",
              content: "第一行\n第二行\n第三行\n第四行",
              status: "ok",
              truncated: true,
              domain: "gov.example.cn",
              source_tier: "official",
              published_at: daysAgo(3),
            },
            {
              ref: 2,
              url: "https://blocked.example.com/b",
              title: "被拦截页面",
              status: "blocked",
              error: "robots 不允许",
              truncated: false,
              domain: "blocked.example.com",
            },
            {
              ref: 3,
              url: "https://down.example.com/c",
              title: "读取失败页面",
              status: "unavailable",
              error: "连接超时",
              truncated: false,
              domain: "down.example.com",
            },
          ],
        },
      }}
    />,
  );

  expect(screen.getByText("已读取 1 个网页 · 2 个失败")).toBeTruthy();
  openCard("网页读取");

  expect(screen.getByText("正文已截断")).toBeTruthy();
  expect(screen.getByText("已拦截：robots 不允许")).toBeTruthy();
  expect(screen.getByText("读取失败：连接超时")).toBeTruthy();
  expect(screen.getByText("签证须知").getAttribute("href")).toBe(
    "https://gov.example.cn/a?v=2",
  );
  // 正文默认只渲染前 3 行,展开后才补全(高级详情里的原始 JSON 不参与断言)。
  expect(document.getElementById("cite-1")?.textContent).not.toContain("第四行");
  fireEvent.click(screen.getByRole("button", { name: "展开全文" }));
  expect(document.getElementById("cite-1")?.textContent).toContain("第四行");
});

test("citation markers jump to the matching source card", () => {
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  render(
    <div>
      <AnswerWithCitations text="落地签需 4 张照片[1]，另见新规[2]。" />
      <ToolRunCard
        run={searchRun({
          results: [
            {
              ref: 1,
              title: "签证指南",
              url: "https://gov.example.cn/a",
              domain: "gov.example.cn",
              source_tier: "official",
            },
          ],
        })}
      />
    </div>,
  );
  openCard("联网搜索");

  const markers = screen.getAllByRole("button", { name: /跳转到来源/ });
  expect(markers.map((marker) => marker.textContent)).toEqual(["1", "2"]);

  fireEvent.click(markers[0]);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
  expect(document.getElementById("cite-1")?.className).toContain("cite-flash");

  // 没有对应来源卡片时角标仍然渲染,点击不报错也不滚动。
  fireEvent.click(markers[1]);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
});

test("citation markers resolve to the source card of their own run", () => {
  // ref 只在一次 agent run 内唯一,同一会话的下一轮又从 1 开始。工具卡片渲染在
  // 它所支撑的正文之前,因此第二轮的 [1] 必须命中第二轮的卡片,而不是页面上
  // 第一个 cite-1。
  const scrollIntoView = vi.fn();
  Element.prototype.scrollIntoView = scrollIntoView;
  const card = (id: string, title: string) => (
    <ToolRunCard
      run={{
        ...searchRun({
          results: [
            {
              ref: 1,
              title,
              url: `https://${id}.example.cn/a`,
              domain: `${id}.example.cn`,
              source_tier: "official",
            },
          ],
        }),
        id,
      }}
    />
  );

  render(
    <div>
      {card("run-a", "第一轮来源")}
      <AnswerWithCitations text="第一轮结论[1]。" />
      {card("run-b", "第二轮来源")}
      <AnswerWithCitations text="第二轮结论[1]。" />
    </div>,
  );
  for (const toggle of screen.getAllByRole("button", { name: /联网搜索/ })) {
    fireEvent.click(toggle);
  }

  const anchors = document.querySelectorAll('[data-ref="1"]');
  expect(anchors).toHaveLength(2);

  const markers = screen.getAllByRole("button", { name: /跳转到来源/ });
  fireEvent.click(markers[1]);

  expect(anchors[1].className).toContain("cite-flash");
  expect(anchors[0].className).not.toContain("cite-flash");
});
