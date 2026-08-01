import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import type { KnowledgeBaseBoardItem } from "@/lib/kb-lifecycle";
import { PurgeDueBanner, WorkerDisabledBanner } from "./lifecycle-console";

const DAY_MS = 86_400_000;

function makeDueBoardItems(): KnowledgeBaseBoardItem[] {
  return [
    {
      kb_id: "kb-due",
      name: "供应商知识库",
      lifecycle_status: "archived",
      archived_at: new Date(Date.now() - 40 * DAY_MS).toISOString(),
      purged_at: null,
      retention_due_at: new Date(Date.now() - 10 * DAY_MS).toISOString(),
      purge_due: true,
      latest_operation: null,
    },
    {
      kb_id: "kb-due-2",
      name: "被阻塞知识库",
      lifecycle_status: "archived",
      archived_at: new Date(Date.now() - 35 * DAY_MS).toISOString(),
      purged_at: null,
      retention_due_at: new Date(Date.now() - 5 * DAY_MS).toISOString(),
      purge_due: true,
      latest_operation: null,
    },
  ];
}

describe("WorkerDisabledBanner", () => {
  test("explains the disabled worker and shows the pending count", () => {
    render(<WorkerDisabledBanner pendingPurges={2} />);

    const banner = screen.getByRole("alert");
    expect(banner.textContent).toContain("永久清理 worker 未启用");
    expect(banner.textContent).toContain("KB_LIFECYCLE_PURGE_WORKER_ENABLED");
    expect(banner.textContent).toContain("2");
    expect(banner.textContent).toContain("个库清理操作在等待执行");
  });

  test("omits the pending clause when nothing is queued", () => {
    render(<WorkerDisabledBanner pendingPurges={0} />);

    expect(screen.getByRole("alert").textContent).not.toContain(
      "个库清理操作在等待执行",
    );
  });
});

describe("PurgeDueBanner", () => {
  test("lists due bases with danger-zone links", () => {
    render(<PurgeDueBanner items={makeDueBoardItems()} />);

    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("2");
    expect(banner.textContent).toContain("保留期已到期");
    const link = screen.getByRole("link", { name: "供应商知识库" });
    expect(link.getAttribute("href")).toBe(
      "/org/kb/kb-due?view=settings&tab=danger",
    );
    expect(screen.getByRole("link", { name: "被阻塞知识库" })).toBeDefined();
  });

  test("renders nothing when no base is due", () => {
    const { container } = render(<PurgeDueBanner items={[]} />);
    expect(container.textContent).toBe("");
  });
});
