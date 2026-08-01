import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PendingUserInput } from "@/lib/chat";
import { UserInputCard } from "./user-input-card";

const request: PendingUserInput = {
  kind: "user_input",
  request_id: "request-1",
  run_id: "run-1",
  requested_at: "2026-07-30T00:00:00Z",
  questions: [
    {
      id: "departure",
      header: "出发地",
      question: "从哪里出发？",
      multi_select: false,
      options: [
        { label: "上海", description: "上海浦东或虹桥" },
        { label: "北京", description: null },
      ],
    },
    {
      id: "interests",
      header: "偏好",
      question: "更关注哪些体验？",
      multi_select: true,
      options: [
        { label: "美食", description: null },
        { label: "博物馆", description: null },
      ],
    },
  ],
};

describe("UserInputCard", () => {
  it("keeps answers editable across previous/next navigation and submits once", () => {
    const onSubmit = vi.fn();
    render(<UserInputCard request={request} onSubmit={onSubmit} />);

    fireEvent.click(screen.getByRole("radio", { name: /上海/ }));
    expect(onSubmit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /下一题/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /美食/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /其他（自行填写）/ }));
    fireEvent.change(screen.getByRole("textbox", { name: /偏好的其他答案/ }), {
      target: { value: "夜间演出" },
    });

    fireEvent.click(screen.getByRole("button", { name: /上一题/ }));
    fireEvent.click(screen.getByRole("radio", { name: /北京/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一题/ }));
    fireEvent.click(screen.getByRole("button", { name: /提交答案/ }));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({
      request_id: "request-1",
      answers: [
        { question_id: "departure", selected: ["北京"], other: undefined },
        {
          question_id: "interests",
          selected: ["美食"],
          other: "夜间演出",
        },
      ],
    });
  });

  it("does not enable final submission for an empty Other answer", () => {
    render(<UserInputCard request={request} onSubmit={vi.fn()} />);

    fireEvent.click(screen.getByRole("radio", { name: /其他（自行填写）/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一题/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /美食/ }));

    expect(
      screen.getByRole("button", { name: /提交答案/ }).hasAttribute("disabled"),
    ).toBe(true);
  });

  it("keeps a draft when switching directly between question tabs", () => {
    render(<UserInputCard request={request} onSubmit={vi.fn()} />);

    fireEvent.click(screen.getByRole("radio", { name: /上海/ }));
    fireEvent.click(screen.getByRole("tab", { name: /偏好/ }));
    fireEvent.click(screen.getByRole("tab", { name: /出发地/ }));

    expect(
      (screen.getByRole("radio", { name: /上海/ }) as HTMLInputElement).checked,
    ).toBe(true);
  });
});
