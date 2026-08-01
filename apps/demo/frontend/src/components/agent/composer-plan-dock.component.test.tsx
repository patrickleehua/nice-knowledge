import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CommandInput } from "./command-input";
import { PlanChecklist } from "./plan-checklist";

const steps = [
  { id: "done", title: "已完成", status: "done" as const, note: null },
  {
    id: "failed",
    title: "生成行程",
    status: "failed" as const,
    note: "执行中断",
  },
];

describe("composer plan dock", () => {
  it("renders a compact plan above the composer with neutral continuation", () => {
    const onContinueStep = vi.fn();
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <CommandInput
          value=""
          onChange={vi.fn()}
          onProject={vi.fn()}
          placeholder="输入消息"
          above={
            <PlanChecklist steps={steps} onContinueStep={onContinueStep} />
          }
          queuedTurns={[]}
          running={false}
          onSubmit={vi.fn()}
          onStop={vi.fn()}
          onQueuedEdit={vi.fn()}
          onQueuedMove={vi.fn()}
          onQueuedRemove={vi.fn()}
          onQueuedRun={vi.fn()}
        />
      </QueryClientProvider>,
    );

    const details = screen.getByText("执行计划").closest("details");
    const composer = screen.getByPlaceholderText("输入消息");
    if (!details) throw new Error("plan dock details element was not rendered");
    expect(details.hasAttribute("open")).toBe(false);
    expect(
      details.compareDocumentPosition(composer) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    fireEvent.click(screen.getByText("执行计划"));
    fireEvent.click(screen.getByRole("button", { name: /继续处理/ }));

    expect(screen.queryByText("重试")).toBeNull();
    expect(onContinueStep).toHaveBeenCalledWith(steps[1]);
  });
});
