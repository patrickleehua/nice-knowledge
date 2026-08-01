import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import type {
  ImageSourceCitation,
  KnowledgeMediaReference,
  SourceSpanCitation,
} from "@/lib/types";
import { AgentKnowledgeEvidence } from "./knowledge-evidence";

vi.mock("@/components/kb/knowledge-citation-card", () => ({
  KnowledgeCitationCard: ({
    media,
    sourceFilename,
  }: {
    media: KnowledgeMediaReference;
    sourceFilename: string;
  }) => (
    <div
      data-testid="knowledge-citation"
      data-asset-id={media.asset_id}
      data-source={sourceFilename}
    >
      {media.alt_text}
    </div>
  ),
}));

const IMAGE_CITATION: ImageSourceCitation = {
  kind: "image_source",
  asset_id: "123e4567-e89b-42d3-a456-426614174000",
  revision_id: "223e4567-e89b-42d3-a456-426614174001",
  source_doc_id: "323e4567-e89b-42d3-a456-426614174002",
  source_sha256: "a".repeat(64),
  image_sha256: "b".repeat(64),
  quote_text: "蓝色信息牌展示皇宫开放时间。",
  page: 3,
  slide: null,
  bbox: null,
};

const MEDIA: KnowledgeMediaReference = {
  kind: "image",
  asset_id: IMAGE_CITATION.asset_id,
  alt_text: "蓝色信息牌展示皇宫开放时间。",
  content_type: "image/png",
  width: 960,
  height: 540,
  page: 3,
  slide: null,
  bbox: null,
  citation: IMAGE_CITATION,
};

describe("AgentKnowledgeEvidence", () => {
  test("projects retrieved images as bounded citation evidence", () => {
    render(
      <AgentKnowledgeEvidence
        output={{
          hits: [
            {
              kb_id: "423e4567-e89b-42d3-a456-426614174003",
              data: { source_filename: "皇宫开放时间图文手册.pdf" },
              citation: IMAGE_CITATION,
              media_refs: [MEDIA],
            },
          ],
        }}
      />,
    );

    const card = screen.getByTestId("knowledge-citation");
    expect(card.getAttribute("data-asset-id")).toBe(MEDIA.asset_id);
    expect(card.getAttribute("data-source")).toBe(
      "皇宫开放时间图文手册.pdf",
    );
    expect(card.textContent).toBe(MEDIA.alt_text);
  });

  test("preserves text-only retrieval compatibility", () => {
    const citation: SourceSpanCitation = {
      kind: "source_span",
      revision_id: "523e4567-e89b-42d3-a456-426614174004",
      source_doc_id: "623e4567-e89b-42d3-a456-426614174005",
      source_sha256: "c".repeat(64),
      quote_text: "皇宫每天九点开放。",
      chunk_id: "723e4567-e89b-42d3-a456-426614174006",
      page: 3,
      start_line: 18,
      end_line: 18,
      cell_ref: null,
    };
    render(
      <AgentKnowledgeEvidence
        output={{
          hits: [
            {
              kb_id: "823e4567-e89b-42d3-a456-426614174007",
              data: { source_filename: "开放时间说明.pdf" },
              citation,
              media_refs: [],
            },
          ],
        }}
      />,
    );

    expect(screen.queryByTestId("knowledge-citation")).toBeNull();
    expect(screen.getByRole("link", { name: "开放时间说明.pdf" })).toBeDefined();
    expect(screen.getByText(citation.quote_text)).toBeDefined();
  });

  test("shows an honest empty evidence state", () => {
    render(<AgentKnowledgeEvidence output={{ hits: [] }} />);

    expect(
      screen.getByText("本次没有可展示的已授权知识证据。"),
    ).toBeDefined();
  });
});
