import { describe, expect, test } from "vitest";
import type { RouteKnowledgeDiagnostics, SourceDocument } from "@/lib/types";
import {
  canRequestRouteReclassification,
  routeDiagnosticPresentation,
  routeReclassificationStatus,
  shouldPollReclassification,
} from "./route-ingestion";

function diagnostics(
  overrides: Partial<RouteKnowledgeDiagnostics> = {},
): RouteKnowledgeDiagnostics {
  return {
    kb_id: "kb-1",
    source_counts: {
      total: 1,
      active: 1,
      terminal: 1,
      by_doc_type: { general: 1 },
      by_status: { completed: 1 },
    },
    eligible_general_document_ids: ["doc-1"],
    route_claim_counts: {
      total: 0,
      suggested: 0,
      confirmed: 0,
      rejected: 0,
      orphaned: 0,
    },
    active_snapshot_id: null,
    ready_snapshot_id: null,
    active_route_count: 0,
    ready_route_count: 0,
    business_asset_count: 0,
    reason_code: "wrong_extraction_type",
    next_action: "reclassify",
    ...overrides,
  };
}

describe("route ingestion guidance", () => {
  test("maps diagnostics to lifecycle-specific actions", () => {
    expect(
      routeDiagnosticPresentation(
        diagnostics({
          reason_code: "classification_required",
          next_action: "classify_and_enqueue",
          source_counts: {
            total: 1,
            active: 1,
            terminal: 0,
            by_doc_type: { unclassified: 1 },
            by_status: { staged: 1 },
          },
        }),
      ),
    ).toMatchObject({
      destination: "sources",
      actionLabel: "去分类并排队",
    });
    expect(routeDiagnosticPresentation(diagnostics())).toMatchObject({
      destination: "sources",
      actionLabel: "去重新抽取",
    });
    expect(
      routeDiagnosticPresentation(
        diagnostics({
          reason_code: "review_required",
          next_action: "review_claims",
          route_claim_counts: {
            total: 2,
            suggested: 2,
            confirmed: 0,
            rejected: 0,
            orphaned: 0,
          },
        }),
      ),
    ).toMatchObject({
      destination: "review",
      actionLabel: "去审核",
    });
    expect(
      routeDiagnosticPresentation(
        diagnostics({
          reason_code: "snapshot_activation_required",
          next_action: "activate_snapshot",
          ready_route_count: 3,
        }),
      ),
    ).toMatchObject({
      destination: "release",
      actionLabel: "去发布快照",
      description: expect.stringContaining("3 条线路知识"),
    });
  });

  test("polls only active route reclassification runs", () => {
    const document = {
      latest_reclassification: {
        target_doc_type: "route_template",
        status: "queued",
      },
    } as SourceDocument;
    expect(shouldPollReclassification(document)).toBe(true);
    expect(routeReclassificationStatus(document)?.text).toBe(
      "线路知识抽取已排队",
    );

    document.latest_reclassification!.status = "failed";
    document.latest_reclassification!.error = "抽取失败";
    expect(shouldPollReclassification(document)).toBe(false);
    expect(routeReclassificationStatus(document)).toMatchObject({
      failed: true,
      text: "线路知识抽取失败：抽取失败",
    });
  });

  test("offers reclassification only for active terminal general documents or failed retries", () => {
    const document = {
      lifecycle_status: "active",
      status: "awaiting_review",
      doc_type: "general",
      latest_reclassification: null,
    } as SourceDocument;
    expect(canRequestRouteReclassification(document)).toBe(true);

    document.lifecycle_status = "withdrawn";
    expect(canRequestRouteReclassification(document)).toBe(false);

    document.lifecycle_status = "active";
    document.status = "parsing";
    expect(canRequestRouteReclassification(document)).toBe(false);
  });
});
