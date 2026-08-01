import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { LifecycleFlow } from "./lifecycle-flow";

/** 按 data-stage 取节点(current 高亮/淡化断言用 data-state) */
function stageNode(container: HTMLElement, stage: string): HTMLElement | null {
  return container.querySelector(`[data-stage="${stage}"]`);
}

describe("LifecycleFlow rendering", () => {
  test("renders the four stage nodes, transition labels and the empty-delete branch", () => {
    const { container } = render(<LifecycleFlow />);

    // 四个状态节点按序渲染
    const stages = Array.from(
      container.querySelectorAll("[data-stage]"),
    ).map((node) => node.getAttribute("data-stage"));
    expect(stages).toEqual(["active", "archived", "purge_pending", "purged"]);
    expect(screen.getByText("活动")).toBeDefined();
    expect(screen.getByText("已归档")).toBeDefined();
    expect(screen.getByText("清理中")).toBeDefined();
    expect(screen.getByText("已清理")).toBeDefined();

    // 转移动作短语:归档 / 恢复(回边)/ 提交清理 / 执行完成
    expect(screen.getByText("归档")).toBeDefined();
    expect(screen.getByText("恢复 ↩")).toBeDefined();
    expect(screen.getByText("提交清理")).toBeDefined();
    expect(screen.getByText("执行完成")).toBeDefined();

    // active 下的空库硬删除虚线支线
    expect(screen.getByText("空库硬删除(仅从未使用)")).toBeDefined();
  });

  test("marks the irreversible segment on both the purge transition and the purged node", () => {
    render(<LifecycleFlow />);

    // 「提交清理」箭头与 purged 节点各带一个「不可逆」微标
    const badges = screen.getAllByTestId("irreversible-badge");
    expect(badges).toHaveLength(2);
    badges.forEach((badge) => expect(badge.textContent).toBe("不可逆"));
  });
});

describe("LifecycleFlow current mode", () => {
  test("highlights the current stage and dims the unreached ones", () => {
    const { container } = render(<LifecycleFlow current="archived" />);

    expect(stageNode(container, "active")?.dataset.state).toBe("past");
    expect(stageNode(container, "archived")?.dataset.state).toBe("current");
    expect(
      stageNode(container, "archived")?.getAttribute("aria-current"),
    ).toBe("step");
    expect(stageNode(container, "purge_pending")?.dataset.state).toBe(
      "future",
    );
    expect(stageNode(container, "purged")?.dataset.state).toBe("future");
    // 未到节点淡化
    expect(
      stageNode(container, "purged")?.className.includes("opacity-50"),
    ).toBe(true);
    // 当前节点主色高亮 ring
    expect(
      stageNode(container, "archived")?.className.includes("ring-primary"),
    ).toBe(true);
  });

  test("exposes the current status and reachable transitions via the group aria-label", () => {
    render(<LifecycleFlow current="archived" />);

    const group = screen.getByRole("group");
    const label = group.getAttribute("aria-label") ?? "";
    expect(label).toContain("当前状态 已归档");
    expect(label).toContain("恢复为活动");
    expect(label).toContain("提交永久清理");
    expect(label).toContain("不可逆");
  });
});

describe("LifecycleFlow counts mode", () => {
  test("renders per-stage count badges and a legend aria-label", () => {
    const { container } = render(
      <LifecycleFlow
        counts={{
          active: 3,
          archived: 2,
          purge_pending: 1,
          purged: 4,
          total: 10,
        }}
      />,
    );

    expect(stageNode(container, "active")?.textContent).toContain("3");
    expect(stageNode(container, "archived")?.textContent).toContain("2");
    expect(stageNode(container, "purge_pending")?.textContent).toContain("1");
    expect(stageNode(container, "purged")?.textContent).toContain("4");
    // counts 模式无当前状态,所有节点常规态
    expect(stageNode(container, "active")?.dataset.state).toBeUndefined();

    const label = screen.getByRole("group").getAttribute("aria-label") ?? "";
    expect(label).toContain("活动 3 个");
    expect(label).toContain("已清理 4 个");
  });

  test("falls back to zero for missing count keys", () => {
    const { container } = render(<LifecycleFlow counts={{ active: 5 }} />);

    expect(stageNode(container, "purged")?.textContent).toContain("0");
  });
});

describe("LifecycleFlow interactivity", () => {
  test("renders plain nodes without onStageClick and buttons with it", () => {
    const { rerender } = render(<LifecycleFlow current="active" />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);

    const onStageClick = vi.fn();
    rerender(<LifecycleFlow current="active" onStageClick={onStageClick} />);
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(4);

    fireEvent.click(buttons[2]);
    expect(onStageClick).toHaveBeenCalledWith("purge_pending");
  });
});
