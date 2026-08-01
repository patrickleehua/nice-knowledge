import {
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { KnowledgeMedia } from "./knowledge-media";

const { fetchBlobMock } = vi.hoisted(() => ({
  fetchBlobMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  fetchBlob: fetchBlobMock,
}));

const ASSET_ID = "123e4567-e89b-42d3-a456-426614174000";

describe("KnowledgeMedia", () => {
  const createObjectURL = vi.fn();
  const revokeObjectURL = vi.fn();

  beforeEach(() => {
    fetchBlobMock.mockReset();
    createObjectURL.mockReset();
    revokeObjectURL.mockReset();
    createObjectURL
      .mockReturnValueOnce("blob:authenticated-thumbnail")
      .mockReturnValueOnce("blob:authenticated-original");
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: revokeObjectURL,
    });
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
      configurable: true,
      value: vi.fn(),
    });
    Object.defineProperty(HTMLElement.prototype, "hasPointerCapture", {
      configurable: true,
      value: vi.fn(() => true),
    });
    Object.defineProperty(HTMLElement.prototype, "releasePointerCapture", {
      configurable: true,
      value: vi.fn(),
    });
  });

  test("loads only authenticated routes, reserves aspect ratio, and cleans blob URLs", async () => {
    fetchBlobMock.mockImplementation(async (path: string) => new Blob([path]));
    const { unmount } = render(
      <KnowledgeMedia
        assetId={ASSET_ID}
        alt="皇宫开放时间"
        width={960}
        height={540}
        thumbnailUrl="org/private/raw-thumbnail.png"
        contentUrl="org/private/raw-original.png"
        sourceLabel="皇宫开放时间图文手册.pdf · 第 3 页"
      />,
    );

    const thumbnail = await screen.findByRole("img", {
      name: "皇宫开放时间",
    });
    const openButton = screen.getByRole("button", {
      name: "查看完整图片：皇宫开放时间",
    }) as HTMLButtonElement;
    expect(fetchBlobMock.mock.calls[0]?.[0]).toBe(
      `/kb/image-assets/${ASSET_ID}/thumbnail`,
    );
    expect(document.body.textContent).not.toContain("org/private");
    expect(openButton.style.aspectRatio).toBe("960 / 540");
    expect(openButton.disabled).toBe(false);
    expect(thumbnail.getAttribute("src")).toBe("blob:authenticated-thumbnail");

    openButton.focus();
    fireEvent.click(openButton);
    const original = await screen.findByRole("img", {
      name: "皇宫开放时间（完整图片）",
    });
    const viewer = screen.getByRole("dialog");
    const imageFrame = original.parentElement as HTMLDivElement;
    expect(fetchBlobMock.mock.calls[1]?.[0]).toBe(
      `/kb/image-assets/${ASSET_ID}/content`,
    );
    expect(original.getAttribute("src")).toBe("blob:authenticated-original");
    expect(viewer.className).toContain("grid-rows-[auto_minmax(0,1fr)]");
    expect(imageFrame.className).toContain("min-h-0");
    expect(imageFrame.style.aspectRatio).toBe("");
    expect(original.className).toContain("size-full");
    expect(original.getAttribute("width")).toBe("960");
    expect(original.getAttribute("height")).toBe("540");

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => {
      expect(
        screen.queryByRole("img", {
          name: "皇宫开放时间（完整图片）",
        }),
      ).toBeNull();
    });
    expect(document.activeElement).toBe(openButton);
    expect(revokeObjectURL).toHaveBeenCalledWith(
      "blob:authenticated-original",
    );

    unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith(
      "blob:authenticated-thumbnail",
    );
  });

  test("supports toolbar, ctrl-wheel zoom, bounded drag, and reset", async () => {
    fetchBlobMock.mockImplementation(async (path: string) => new Blob([path]));
    render(
      <KnowledgeMedia
        assetId={ASSET_ID}
        alt="可缩放图片"
        width={800}
        height={600}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "查看完整图片：可缩放图片",
      }),
    );
    const original = await screen.findByRole("img", {
      name: "可缩放图片（完整图片）",
    });
    const imageFrame = screen.getByRole("group", {
      name: "图片查看区域",
    });
    Object.defineProperty(imageFrame, "clientWidth", {
      configurable: true,
      value: 800,
    });
    Object.defineProperty(imageFrame, "clientHeight", {
      configurable: true,
      value: 600,
    });
    imageFrame.getBoundingClientRect = () =>
      ({
        bottom: 600,
        height: 600,
        left: 0,
        right: 800,
        top: 0,
        width: 800,
        x: 0,
        y: 0,
        toJSON: () => undefined,
      }) as DOMRect;

    const zoomIn = screen.getByRole("button", { name: "放大图片" });
    const reset = screen.getByRole("button", { name: "重置图片缩放" });
    expect(
      screen.getByRole("button", { name: "缩小图片" }),
    ).toHaveProperty("disabled", true);
    expect(reset.textContent).toBe("100%");

    fireEvent.click(zoomIn);
    expect(imageFrame.dataset.zoom).toBe("1.25");
    expect(reset.textContent).toBe("125%");

    fireEvent.wheel(imageFrame, { deltaY: -100 });
    expect(imageFrame.dataset.zoom).toBe("1.25");
    fireEvent.wheel(imageFrame, {
      clientX: 400,
      clientY: 300,
      ctrlKey: true,
      deltaY: -100,
    });
    expect(Number(imageFrame.dataset.zoom)).toBeGreaterThan(1.25);

    fireEvent.pointerDown(imageFrame, {
      button: 0,
      clientX: 100,
      clientY: 100,
      pointerId: 1,
      pointerType: "mouse",
    });
    fireEvent.pointerMove(imageFrame, {
      clientX: 160,
      clientY: 130,
      pointerId: 1,
      pointerType: "mouse",
    });
    expect(original.style.transform).toContain("translate3d(60px, 30px, 0)");
    const activeScale = Number(imageFrame.dataset.zoom);
    const maxX = (800 * activeScale - 800) / 2;
    const maxY = (600 * activeScale - 600) / 2;
    fireEvent.pointerMove(imageFrame, {
      clientX: 2100,
      clientY: 2100,
      pointerId: 1,
      pointerType: "mouse",
    });
    expect(original.style.transform).toBe(
      `translate3d(${maxX}px, ${maxY}px, 0) scale(${activeScale})`,
    );
    fireEvent.pointerUp(imageFrame, {
      clientX: 2100,
      clientY: 2100,
      pointerId: 1,
      pointerType: "mouse",
    });

    fireEvent.click(reset);
    expect(imageFrame.dataset.zoom).toBe("1");
    expect(original.style.transform).toBe(
      "translate3d(0px, 0px, 0) scale(1)",
    );

    fireEvent.doubleClick(imageFrame, { clientX: 400, clientY: 300 });
    expect(imageFrame.dataset.zoom).toBe("2");
    fireEvent.keyDown(imageFrame, { key: "0" });
    expect(imageFrame.dataset.zoom).toBe("1");
  });

  test("fails closed when authenticated media is unavailable", async () => {
    fetchBlobMock.mockRejectedValue(new Error("private locator must stay hidden"));
    render(
      <KnowledgeMedia
        assetId={ASSET_ID}
        alt="受限图片"
        thumbnailUrl="/api/v1/kb/image-assets/raw-object-key/thumbnail"
      />,
    );

    await screen.findByText("证据图片当前不可用");
    const openButton = screen.getByRole("button", {
      name: "查看完整图片：受限图片",
    }) as HTMLButtonElement;
    expect(openButton.disabled).toBe(true);
    expect(fetchBlobMock.mock.calls[0]?.[0]).toBe(
      `/kb/image-assets/${ASSET_ID}/thumbnail`,
    );
    expect(document.body.textContent).not.toContain("raw-object-key");
    expect(document.body.textContent).not.toContain("private locator");
  });

  test("rejects non-asset identifiers without issuing a request", () => {
    render(<KnowledgeMedia assetId="org/private/image.png" alt="" />);

    expect(fetchBlobMock).not.toHaveBeenCalled();
    expect(screen.getByText("证据图片当前不可用")).toBeDefined();
    expect(
      screen.getByRole("button", {
        name: "查看完整图片：知识来源图片",
      }),
    ).toHaveProperty("disabled", true);
  });
});
