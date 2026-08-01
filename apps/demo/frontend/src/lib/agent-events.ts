import type {
  AgentEvent,
  ApprovalBundle,
  ApprovalBundleItem,
  ChatMessageOut,
  LegacyPendingConfirmation,
  PendingConfirmation,
  ReviewerOverride,
} from "@/lib/chat";

export interface ReviewerOverrideTrace {
  candidateId: string;
  status: "available" | "consumed" | "completed";
  actionHash: string;
  override?: ReviewerOverride;
  ok?: boolean;
  reasonCode?: string | null;
}

export interface ReviewerTrace {
  toolCallId: string;
  name: string;
  requested: boolean;
  decision?: "approve" | "deny" | "escalate";
  reasonCode?: string;
  rationale?: string | null;
  riskFlags: string[];
  overrideEligible: boolean;
  circuitBreaker: boolean;
  denialCount?: number;
  override?: ReviewerOverrideTrace;
}

export interface ToolApprovalTrace {
  bundleId: string;
  bundleStatus: ApprovalBundle["status"];
  itemStatus: ApprovalBundleItem["status"];
  decision: ApprovalBundleItem["decision"];
  decisionScope: ApprovalBundleItem["decision_scope"];
}

export interface ToolPermissionTrace {
  reviewer?: ReviewerTrace;
  approval?: ToolApprovalTrace;
}

export interface ToolRun {
  id: string;
  name: string;
  input?: Record<string, unknown>;
  output?: unknown;
  ok?: boolean;
  progress: string[];
  status: "running" | "waiting" | "ok" | "failed";
  permission?: ToolPermissionTrace;
}

export interface GeneratedImageArtifactItem {
  filename: string;
  url: string;
  content_type: "image/png" | "image/jpeg" | "image/webp";
  size_bytes: number;
  width: number;
  height: number;
}

export interface ImageArtifact {
  id: string;
  seq: number;
  status: "running" | "success" | "rejected" | "failed";
  prompt: string;
  requestedCount: number;
  requestedWidth: number;
  requestedHeight: number;
  images: GeneratedImageArtifactItem[];
}

export interface BuiltinWebSearchSource {
  title: string;
  url: string;
  page_age?: string | null;
}

export type ActivityTimelineItem =
  | {
      type: "thought";
      id: string;
      seq: number;
      text: string;
    }
  | {
      type: "builtin-search";
      id: string;
      seq: number;
      query: string;
      results: BuiltinWebSearchSource[];
    }
  | {
      type: "message";
      id: string;
      seq: number;
      text: string;
    }
  | {
      type: "tool";
      id: string;
      seq: number;
      run: ToolRun;
    }
  | {
      type: "stage";
      id: string;
      seq: number;
      projectId: string;
      projectStatus: string;
      stage: "demand" | "itinerary" | "quote" | "docs";
    }
  | {
      type: "error";
      id: string;
      seq: number;
      message: string;
    }
  | {
      type: "approval";
      id: string;
      seq: number;
      pending: PendingConfirmation;
    };

export interface ActivityTimeline {
  items: ActivityTimelineItem[];
  finalText: string;
}

export type SemanticAgentEvent = Exclude<
  AgentEvent,
  { type: "text.delta" | "thought.delta" }
>;

const PERMISSION_EVENT_TYPES = new Set([
  "policy.snapshot",
  "policy.decision",
  "policy.shadow_evaluation",
  "reviewer.requested",
  "reviewer.decision",
  "reviewer.circuit_breaker",
  "reviewer.override.available",
  "reviewer.override.consumed",
  "reviewer.override.completed",
  "approval.bundle.requested",
  "approval.bundle.updated",
  "approval.bundle.decision",
  "approval.bundle.completed",
  "approval.legacy.decision",
]);

export type PermissionAgentEvent = Extract<
  AgentEvent,
  { type: `policy.${string}` | `reviewer.${string}` | `approval.${string}` }
>;

export interface PermissionReplay {
  events: PermissionAgentEvent[];
  pendingBundles: ApprovalBundle[];
  availableOverrides: string[];
  completedBundleIds: string[];
  completedOverrideIds: string[];
}

export interface PermissionTimelineProjection {
  bundles: Map<string, ApprovalBundle>;
  reviewerByToolCall: Map<string, ReviewerTrace>;
  approvalByToolCall: Map<string, ToolApprovalTrace>;
}

/**
 * Token deltas are transport details. The primary Agent workspace deliberately
 * projects only complete loop events so new content arrives as semantic blocks.
 */
export function isSemanticAgentEvent(
  event: AgentEvent,
): event is SemanticAgentEvent {
  return event.type !== "text.delta" && event.type !== "thought.delta";
}

export function buildToolRuns(events: AgentEvent[]): ToolRun[] {
  const runs = new Map<string, ToolRun>();
  for (const event of events) {
    if (event.type === "tool.call") {
      runs.set(event.tool_call_id, {
        id: event.tool_call_id,
        name: event.name,
        input: event.input,
        progress: [],
        status: "running",
      });
    } else if (event.type === "tool.progress") {
      const run = runs.get(event.parent_tool_call_id);
      if (run) run.progress.push(event.text);
    } else if (event.type === "tool.result") {
      const run = runs.get(event.tool_call_id) ?? {
        id: event.tool_call_id,
        name: event.name,
        progress: [],
        status: "running" as const,
      };
      const succeeded = toolResultSucceeded(event.ok, event.output);
      run.output = event.output;
      run.ok = succeeded;
      run.status = succeeded ? "ok" : "failed";
      runs.set(run.id, run);
    }
  }
  return [...runs.values()];
}

function orderedEvents(events: AgentEvent[]): AgentEvent[] {
  return events
    .map((event, index) => ({ event, index }))
    .sort((left, right) =>
      left.event.seq === right.event.seq
        ? left.index - right.index
        : left.event.seq - right.event.seq,
    )
    .map(({ event }) => event);
}

/** Rebuild permission state from durable semantic events without reopening work. */
export function buildPermissionReplay(events: AgentEvent[]): PermissionReplay {
  const permissionEvents = orderedEvents(events).filter(
    (event): event is PermissionAgentEvent =>
      PERMISSION_EVENT_TYPES.has(event.type),
  );
  const pendingBundles = new Map<string, ApprovalBundle>();
  const availableOverrides = new Set<string>();
  const completedBundleIds = new Set<string>();
  const completedOverrideIds = new Set<string>();

  for (const event of permissionEvents) {
    if (
      event.type === "approval.bundle.requested" ||
      event.type === "approval.bundle.updated"
    ) {
      if (
        event.bundle.status === "pending" &&
        event.bundle.items.some((item) => item.status === "pending")
      )
        pendingBundles.set(event.bundle.bundle_id, event.bundle);
      else pendingBundles.delete(event.bundle.bundle_id);
      continue;
    }
    if (event.type === "approval.bundle.completed") {
      pendingBundles.delete(event.bundle.bundle_id);
      completedBundleIds.add(event.bundle.bundle_id);
      continue;
    }
    if (event.type === "reviewer.override.available") {
      availableOverrides.add(event.override.candidate_id);
      continue;
    }
    if (event.type === "reviewer.override.consumed") {
      availableOverrides.delete(event.override.candidate_id);
      continue;
    }
    if (event.type === "reviewer.override.completed") {
      availableOverrides.delete(event.candidate_id);
      completedOverrideIds.add(event.candidate_id);
    }
  }

  return {
    events: permissionEvents,
    pendingBundles: [...pendingBundles.values()],
    availableOverrides: [...availableOverrides],
    completedBundleIds: [...completedBundleIds],
    completedOverrideIds: [...completedOverrideIds],
  };
}

function reviewerTrace(
  traces: Map<string, ReviewerTrace>,
  toolCallId: string,
  name: string,
): ReviewerTrace {
  const existing = traces.get(toolCallId);
  if (existing) return existing;
  const created: ReviewerTrace = {
    toolCallId,
    name,
    requested: false,
    riskFlags: [],
    overrideEligible: false,
    circuitBreaker: false,
  };
  traces.set(toolCallId, created);
  return created;
}

/**
 * Coalesce permission events across adjacent runs in one visible Agent turn.
 * Each run owns its own sequence counter, so callers pass run event groups and
 * this helper orders only inside each group before preserving group order.
 */
export function buildPermissionTimelineProjection(
  eventGroups: readonly AgentEvent[][],
): PermissionTimelineProjection {
  const bundles = new Map<string, ApprovalBundle>();
  const reviewerByToolCall = new Map<string, ReviewerTrace>();

  for (const group of eventGroups) {
    for (const event of orderedEvents(group)) {
      if (
        event.type === "approval.bundle.requested" ||
        event.type === "approval.bundle.updated" ||
        event.type === "approval.bundle.completed"
      ) {
        bundles.set(event.bundle.bundle_id, event.bundle);
        continue;
      }

      if (event.type === "approval.bundle.decision") {
        const bundle = bundles.get(event.bundle_id);
        if (bundle) {
          const items = bundle.items.map((item) =>
            item.tool_call_id === event.tool_call_id
              ? {
                  ...item,
                  status:
                    event.decision === "approve"
                      ? ("approved" as const)
                      : ("denied" as const),
                  decision: event.decision,
                  decision_scope: event.scope,
                  user_note: event.note ?? null,
                }
              : item,
          );
          bundles.set(event.bundle_id, {
            ...bundle,
            status: items.some((item) => item.status === "pending")
              ? "pending"
              : "ready",
            items,
          });
        }
        continue;
      }

      if (event.type === "reviewer.requested") {
        const trace = reviewerTrace(
          reviewerByToolCall,
          event.tool_call_id,
          event.name,
        );
        trace.requested = true;
        trace.reasonCode = event.reason_code;
        continue;
      }

      if (event.type === "reviewer.decision") {
        const trace = reviewerTrace(
          reviewerByToolCall,
          event.tool_call_id,
          event.name,
        );
        trace.requested = true;
        trace.decision = event.decision;
        trace.reasonCode = event.reason_code;
        trace.rationale = event.rationale;
        trace.riskFlags = [...event.risk_flags];
        trace.overrideEligible = event.override_eligible;
        trace.circuitBreaker = event.circuit_breaker;
        continue;
      }

      if (event.type === "reviewer.circuit_breaker") {
        const trace = reviewerTrace(
          reviewerByToolCall,
          event.tool_call_id,
          event.name,
        );
        trace.requested = true;
        trace.decision = "deny";
        trace.reasonCode = "reviewer_circuit_breaker";
        trace.circuitBreaker = true;
        trace.denialCount = event.denial_count;
        continue;
      }

      if (
        event.type === "reviewer.override.available" ||
        event.type === "reviewer.override.consumed"
      ) {
        const candidate = event.override;
        const trace = reviewerTrace(
          reviewerByToolCall,
          candidate.tool_call_id,
          candidate.name,
        );
        trace.override = {
          candidateId: candidate.candidate_id,
          status:
            event.type === "reviewer.override.available"
              ? "available"
              : "consumed",
          actionHash: candidate.action_hash,
          override: candidate,
        };
        continue;
      }

      if (event.type === "reviewer.override.completed") {
        const trace = reviewerTrace(
          reviewerByToolCall,
          event.override_of,
          event.name,
        );
        trace.override = {
          candidateId: event.candidate_id,
          status: "completed",
          actionHash: event.action_hash,
          ok: event.ok,
          reasonCode: event.reason_code,
        };
      }
    }
  }

  const approvalByToolCall = new Map<string, ToolApprovalTrace>();
  for (const bundle of bundles.values()) {
    for (const item of bundle.items) {
      approvalByToolCall.set(item.tool_call_id, {
        bundleId: bundle.bundle_id,
        bundleStatus: bundle.status,
        itemStatus: item.status,
        decision: item.decision,
        decisionScope: item.decision_scope,
      });
    }
  }

  return { bundles, reviewerByToolCall, approvalByToolCall };
}

function imageRequest(input: Record<string, unknown> | undefined) {
  const count =
    typeof input?.n === "number" && Number.isFinite(input.n)
      ? Math.min(4, Math.max(1, Math.floor(input.n)))
      : 1;
  const match =
    typeof input?.size === "string"
      ? /^(\d+)x(\d+)$/.exec(input.size.trim())
      : null;
  return {
    prompt: typeof input?.prompt === "string" ? input.prompt.trim() : "",
    count,
    width: match ? Number(match[1]) : 1,
    height: match ? Number(match[2]) : 1,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Reconcile the transport flag with the structured result contract.
 *
 * Older long-running backend processes emitted `ok: false` for successful
 * results that contained `error: null`. A concrete `status: "ok"` result with
 * no real error remains authoritative and must replay like the persisted tool
 * message. Explicit errors and unavailable results always fail closed.
 */
export function toolResultSucceeded(reportedOk: boolean, output: unknown) {
  if (!isRecord(output)) return reportedOk;
  if (output.status === "unavailable" || Boolean(output.error)) return false;
  return reportedOk || output.status === "ok";
}

function imageItems(
  output: unknown,
  fallback: ReturnType<typeof imageRequest>,
): GeneratedImageArtifactItem[] {
  if (!isRecord(output) || !Array.isArray(output.images)) return [];
  return output.images.flatMap((value): GeneratedImageArtifactItem[] => {
    if (!isRecord(value)) return [];
    const contentType = value.content_type;
    if (
      typeof value.filename !== "string" ||
      typeof value.url !== "string" ||
      (contentType !== "image/png" &&
        contentType !== "image/jpeg" &&
        contentType !== "image/webp")
    )
      return [];
    const width =
      typeof value.width === "number" && value.width > 0
        ? value.width
        : fallback.width;
    const height =
      typeof value.height === "number" && value.height > 0
        ? value.height
        : fallback.height;
    return [
      {
        filename: value.filename,
        url: value.url,
        content_type: contentType,
        size_bytes:
          typeof value.size_bytes === "number" && value.size_bytes >= 0
            ? value.size_bytes
            : 0,
        width,
        height,
      },
    ];
  });
}

/** Project image tool events into stable contextual artifact positions. */
export function buildImageArtifacts(events: AgentEvent[]): ImageArtifact[] {
  const artifacts = new Map<string, ImageArtifact>();
  const ordered = orderedEvents(events);
  const permissionProjection = buildPermissionTimelineProjection([ordered]);
  const approvalGrantedAt = new Map<string, number>();
  const executionStartedAfterApproval = new Set<string>();
  const legacyPending = new Set(
    ordered.flatMap((event) =>
      event.type === "tool.confirm" ? [event.tool_call_id] : [],
    ),
  );
  const legacyDenied = new Set<string>();

  ordered.forEach((event, index) => {
    if (
      event.type === "approval.bundle.requested" ||
      event.type === "approval.bundle.updated" ||
      event.type === "approval.bundle.completed"
    ) {
      for (const item of event.bundle.items) {
        if (
          (item.status === "approved" || item.status === "allowed") &&
          !approvalGrantedAt.has(item.tool_call_id)
        )
          approvalGrantedAt.set(item.tool_call_id, index);
      }
      return;
    }
    if (event.type === "approval.bundle.decision") {
      if (event.decision === "approve")
        approvalGrantedAt.set(event.tool_call_id, index);
      return;
    }
    if (event.type === "approval.legacy.decision") {
      if (event.decision === "approve")
        approvalGrantedAt.set(event.tool_call_id, index);
      else legacyDenied.add(event.tool_call_id);
      return;
    }
    const toolCallId =
      event.type === "tool.call"
        ? event.tool_call_id
        : event.type === "tool.progress"
          ? event.parent_tool_call_id
          : null;
    const grantedAt = toolCallId
      ? approvalGrantedAt.get(toolCallId)
      : undefined;
    if (toolCallId && grantedAt !== undefined && index > grantedAt)
      executionStartedAfterApproval.add(toolCallId);
  });

  for (const event of ordered) {
    if (event.type === "tool.call" && event.name === "image_generate") {
      const request = imageRequest(event.input);
      const existing = artifacts.get(event.tool_call_id);
      artifacts.set(event.tool_call_id, {
        id: event.tool_call_id,
        seq: existing?.seq ?? event.seq,
        status: "running",
        prompt: existing?.prompt || request.prompt,
        requestedCount: existing?.requestedCount ?? request.count,
        requestedWidth: existing?.requestedWidth ?? request.width,
        requestedHeight: existing?.requestedHeight ?? request.height,
        images: [],
      });
      continue;
    }
    if (event.type !== "tool.result" || event.name !== "image_generate")
      continue;
    const existing = artifacts.get(event.tool_call_id);
    const request = imageRequest(
      existing
        ? {
            prompt: existing.prompt,
            n: existing.requestedCount,
            size: `${existing.requestedWidth}x${existing.requestedHeight}`,
          }
        : undefined,
    );
    const images = imageItems(event.output, request).slice(0, 4);
    const output = isRecord(event.output) ? event.output : undefined;
    const error =
      typeof output?.error === "string" ? output.error.toLowerCase() : "";
    const rejected = error.includes("拒绝") || error.includes("未批准");
    artifacts.set(event.tool_call_id, {
      id: event.tool_call_id,
      seq: existing?.seq ?? event.seq,
      status:
        toolResultSucceeded(event.ok, event.output) && images.length
          ? "success"
          : rejected
            ? "rejected"
            : "failed",
      prompt: existing?.prompt ?? request.prompt,
      requestedCount: existing?.requestedCount ?? Math.max(images.length, 1),
      requestedWidth: existing?.requestedWidth ?? request.width,
      requestedHeight: existing?.requestedHeight ?? request.height,
      images,
    });
  }
  return [...artifacts.values()]
    .filter((artifact) => {
      if (artifact.status !== "running") return true;
      const approval = permissionProjection.approvalByToolCall.get(artifact.id);
      if (
        approval &&
        (approval.bundleStatus === "pending" ||
          approval.itemStatus === "pending" ||
          approval.itemStatus === "denied")
      )
        return false;
      if (approval && !executionStartedAfterApproval.has(artifact.id))
        return false;
      if (legacyDenied.has(artifact.id)) return false;
      return (
        !legacyPending.has(artifact.id) ||
        executionStartedAfterApproval.has(artifact.id)
      );
    })
    .sort((left, right) => left.seq - right.seq);
}

/**
 * Keep one contextual image position when an approval pause and its resume are
 * stored as separate runs. The first run owns the position; a later terminal
 * event for the same tool call replaces that placeholder instead of adding a
 * second gallery.
 */
export function buildImageArtifactsByRun(
  eventGroups: readonly AgentEvent[][],
): ImageArtifact[][] {
  const grouped = eventGroups.map((): ImageArtifact[] => []);
  const initialOwners = new Map<string, number>();
  const resolved = new Map<
    string,
    { owner: number; artifact: ImageArtifact }
  >();

  eventGroups.forEach((events, owner) => {
    for (const event of orderedEvents(events)) {
      if (
        event.type === "tool.call" &&
        event.name === "image_generate" &&
        !initialOwners.has(event.tool_call_id)
      )
        initialOwners.set(event.tool_call_id, owner);
    }
    for (const artifact of buildImageArtifacts(events)) {
      const existing = resolved.get(artifact.id);
      if (!existing) {
        resolved.set(artifact.id, {
          owner: initialOwners.get(artifact.id) ?? owner,
          artifact,
        });
        continue;
      }

      if (
        existing.artifact.status !== "running" &&
        artifact.status === "running"
      )
        continue;

      const next = artifact.status === "running" ? existing.artifact : artifact;
      resolved.set(artifact.id, {
        owner: existing.owner,
        artifact: {
          ...next,
          seq: existing.artifact.seq,
          prompt: existing.artifact.prompt || artifact.prompt,
          requestedCount: existing.artifact.requestedCount,
          requestedWidth: existing.artifact.requestedWidth,
          requestedHeight: existing.artifact.requestedHeight,
        },
      });
    }
  });

  for (const { owner, artifact } of resolved.values())
    grouped[owner]?.push(artifact);
  for (const artifacts of grouped)
    artifacts.sort((left, right) => left.seq - right.seq);
  return grouped;
}

function toolPermissionTrace(
  projection: PermissionTimelineProjection,
  toolCallId: string,
): ToolPermissionTrace | undefined {
  const reviewer = projection.reviewerByToolCall.get(toolCallId);
  const approval = projection.approvalByToolCall.get(toolCallId);
  if (!reviewer && !approval) return undefined;
  return {
    reviewer: reviewer
      ? {
          ...reviewer,
          riskFlags: [...reviewer.riskFlags],
          override: reviewer.override ? { ...reviewer.override } : undefined,
        }
      : undefined,
    approval: approval ? { ...approval } : undefined,
  };
}

function initialToolStatus(
  permission: ToolPermissionTrace | undefined,
): ToolRun["status"] {
  const approval = permission?.approval;
  if (approval?.bundleStatus === "pending" && approval.itemStatus === "pending")
    return "waiting";
  if (approval?.itemStatus === "denied") return "failed";
  return "running";
}

/** Convert one SSE run into semantic blocks in authoritative event order. */
export function buildActivityTimeline(
  events: AgentEvent[],
  options?: { permissionProjection?: PermissionTimelineProjection },
): ActivityTimeline {
  const ordered = orderedEvents(events);
  const permissionProjection =
    options?.permissionProjection ??
    buildPermissionTimelineProjection([events]);
  const items: ActivityTimelineItem[] = [];
  const toolItems = new Map<
    string,
    Extract<ActivityTimelineItem, { type: "tool" }>
  >();
  const approvalItems = new Set<string>();
  const bundledToolCallIds = new Set(
    ordered.flatMap((event) =>
      event.type === "approval.bundle.requested"
        ? event.bundle.items.map((item) => item.tool_call_id)
        : [],
    ),
  );
  const finalTexts: string[] = [];

  for (const event of ordered) {
    if (event.type === "thought") {
      if (event.text.trim())
        items.push({
          type: "thought",
          id: `thought-${event.seq}`,
          seq: event.seq,
          text: event.text,
        });
      continue;
    }

    if (event.type === "websearch.builtin") {
      items.push({
        type: "builtin-search",
        id: `builtin-search-${event.seq}`,
        seq: event.seq,
        query: event.query ?? "",
        results: Array.isArray(event.results) ? event.results : [],
      });
      continue;
    }

    if (event.type === "assistant.message") {
      if (event.text.trim())
        items.push({
          type: "message",
          id: `message-${event.seq}`,
          seq: event.seq,
          text: event.text,
        });
      continue;
    }

    if (event.type === "thought.delta" || event.type === "text.delta") {
      continue;
    }

    if (event.type === "tool.call") {
      const permission = toolPermissionTrace(
        permissionProjection,
        event.tool_call_id,
      );
      const existing = toolItems.get(event.tool_call_id);
      if (existing) {
        existing.run.input = existing.run.input ?? event.input;
        existing.run.permission = permission ?? existing.run.permission;
        if (existing.run.status === "waiting") existing.run.status = "running";
        continue;
      }
      const item: Extract<ActivityTimelineItem, { type: "tool" }> = {
        type: "tool",
        id: `tool-${event.tool_call_id}`,
        seq: event.seq,
        run: {
          id: event.tool_call_id,
          name: event.name,
          input: event.input,
          progress: [],
          status: initialToolStatus(permission),
          permission,
        },
      };
      toolItems.set(event.tool_call_id, item);
      items.push(item);
      continue;
    }

    if (event.type === "tool.progress") {
      toolItems.get(event.parent_tool_call_id)?.run.progress.push(event.text);
      continue;
    }

    if (event.type === "tool.result") {
      let item = toolItems.get(event.tool_call_id);
      if (!item) {
        item = {
          type: "tool",
          id: `tool-${event.tool_call_id}`,
          seq: event.seq,
          run: {
            id: event.tool_call_id,
            name: event.name,
            progress: [],
            status: "running",
            permission: toolPermissionTrace(
              permissionProjection,
              event.tool_call_id,
            ),
          },
        };
        toolItems.set(event.tool_call_id, item);
        items.push(item);
      }
      const succeeded = toolResultSucceeded(event.ok, event.output);
      item.run.output = event.output;
      item.run.ok = succeeded;
      item.run.status = succeeded ? "ok" : "failed";
      continue;
    }

    if (event.type === "approval.bundle.requested") {
      if (!approvalItems.has(event.bundle.bundle_id)) {
        approvalItems.add(event.bundle.bundle_id);
        items.push({
          type: "approval",
          id: `approval-${event.bundle.bundle_id}`,
          seq: event.seq,
          pending:
            permissionProjection.bundles.get(event.bundle.bundle_id) ??
            event.bundle,
        });
      }
      continue;
    }

    if (
      event.type === "tool.confirm" &&
      !bundledToolCallIds.has(event.tool_call_id)
    ) {
      const legacy: LegacyPendingConfirmation = {
        tool_call_id: event.tool_call_id,
        name: event.name,
        input: event.input,
        summary: event.summary,
      };
      items.push({
        type: "approval",
        id: `approval-legacy-${event.tool_call_id}`,
        seq: event.seq,
        pending: legacy,
      });
      continue;
    }

    if (event.type === "stage.update") {
      items.push({
        type: "stage",
        id: `stage-${event.seq}`,
        seq: event.seq,
        projectId: event.project_id,
        projectStatus: event.project_status,
        stage: event.stage,
      });
      continue;
    }

    if (event.type === "error") {
      items.push({
        type: "error",
        id: `error-${event.seq}`,
        seq: event.seq,
        message: event.message,
      });
      continue;
    }

    if (event.type === "text") {
      finalTexts.push(event.text);
      continue;
    }
  }

  return {
    items: items.filter(
      (item) => item.type !== "thought" || item.text.trim().length > 0,
    ),
    finalText: finalTexts.join("\n\n"),
  };
}

export interface ConversationItem {
  // notice = 非用户撰写、但需要在时间线上留痕的系统小条(如目标续跑)
  key: string;
  role: "user" | "assistant" | "run" | "notice";
  source?: "persisted" | "live";
  runId?: string | null;
  persistedRecordId?: string;
  resultOnly?: boolean;
  content?: string;
  events?: AgentEvent[];
  createdAt?: string | null;
  completedAt?: string | null;
}

export interface ConversationGroup {
  key: string;
  role: "user" | "assistant" | "notice";
  items: ConversationItem[];
}

export interface ConversationAnchor {
  id: string;
  groupIndex: number;
  label: string;
}

export type AssistantTurnSection =
  | { type: "text"; item: ConversationItem }
  | { type: "run"; key: string; items: ConversationItem[] };

/**
 * Persisted tool records are separate chat messages, but visually belong to the
 * same Agent turn as the assistant text around them. User messages always start
 * a new boundary; adjacent assistant/run records stay ordered inside one group.
 */
export function groupConversationItems(
  items: ConversationItem[],
): ConversationGroup[] {
  const groups: ConversationGroup[] = [];
  for (const item of items) {
    if (item.role === "user") {
      groups.push({ key: item.key, role: "user", items: [item] });
      continue;
    }
    if (item.role === "notice") {
      groups.push({ key: item.key, role: "notice", items: [item] });
      continue;
    }
    const last = groups.at(-1);
    if (last?.role === "assistant") {
      last.items.push(item);
      continue;
    }
    groups.push({ key: item.key, role: "assistant", items: [item] });
  }
  return groups;
}

export function buildConversationAnchors(
  groups: ConversationGroup[],
): ConversationAnchor[] {
  return groups.flatMap((group, groupIndex) => {
    // 系统小条不是对话轮次,不进定位器
    if (group.role === "notice") return [];
    if (group.role !== "user" && groupIndex !== 0) return [];
    const content = group.items[0]?.content?.replace(/\s+/g, " ").trim();
    const label =
      group.role === "user"
        ? content
          ? content.length > 18
            ? `${content.slice(0, 18)}…`
            : content
          : "用户消息"
        : "Agent 回复";
    return [{ id: `conversation-turn-${groupIndex}`, groupIndex, label }];
  });
}

/** Keep assistant narration ordered while merging only adjacent run records. */
export function groupAssistantSections(
  items: ConversationItem[],
): AssistantTurnSection[] {
  const sections: AssistantTurnSection[] = [];
  for (const item of items) {
    if (item.role !== "run") {
      sections.push({ type: "text", item });
      continue;
    }
    const previous = sections.at(-1);
    const previousFinished =
      previous?.type === "run" &&
      previous.items
        .at(-1)
        ?.events?.some((event) => event.type === "turn.done");
    if (
      previous?.type === "run" &&
      (!previousFinished ||
        continuesPendingApproval(previous.items, item.events ?? []))
    )
      previous.items.push(item);
    else sections.push({ type: "run", key: item.key, items: [item] });
  }
  return sections;
}

function continuesPendingApproval(
  previousItems: ConversationItem[],
  nextEvents: AgentEvent[],
): boolean {
  const previousEvents = previousItems.flatMap((item) => item.events ?? []);
  const paused = previousEvents.some(
    (event) =>
      event.type === "turn.done" &&
      (event.reason === "confirm" || event.reason === "ask_user"),
  );
  if (!paused) return false;

  const bundleIds = new Set(
    previousEvents.flatMap((event) =>
      event.type === "approval.bundle.requested"
        ? [event.bundle.bundle_id]
        : [],
    ),
  );
  const toolCallIds = new Set(
    previousEvents.flatMap((event) =>
      event.type === "tool.call" || event.type === "tool.confirm"
        ? [event.tool_call_id]
        : [],
    ),
  );
  const userInputRequestIds = new Set(
    previousEvents.flatMap((event) =>
      event.type === "user.input.required" ? [event.request.request_id] : [],
    ),
  );
  return nextEvents.some(
    (event) =>
      (event.type === "approval.bundle.decision" &&
        bundleIds.has(event.bundle_id)) ||
      (event.type === "user.input.resolved" &&
        userInputRequestIds.has(event.request_id)) ||
      ((event.type === "tool.call" || event.type === "tool.result") &&
        toolCallIds.has(event.tool_call_id)),
  );
}

/**
 * Run sequence numbers restart at one after an approval resume. Normalize a
 * logical run's event groups without sorting one run through another.
 */
export function flattenAgentEventGroups(
  eventGroups: readonly AgentEvent[][],
): AgentEvent[] {
  let seq = 0;
  return eventGroups.flatMap((events) =>
    orderedEvents(events).map((event) => {
      seq += 1;
      return { ...event, seq } as AgentEvent;
    }),
  );
}

const INTERNAL_CONTINUATION_MESSAGES = new Set([
  "(已处理审批决定)",
  "(已批准执行)",
  "(已拒绝执行)",
  "(已处理独立审批覆盖)",
  "(用户已回答澄清问题)",
]);

/**
 * 会话目标续跑的合成输入前缀(后端 services/agent/session_goal.py 同名常量)。
 * chat_messages 没有 metadata 列,该消息只能靠内容前缀自标识;它不是用户写的,
 * 因此渲染成系统小条而不是用户气泡。
 */
export const GOAL_CONTINUATION_PREFIX = "[目标续跑]";
const GOAL_CONTINUATION_NOTICE = "目标续跑 · Agent 自动继续推进会话目标";

export function isGoalContinuationMessage(content: string | null): boolean {
  return !!content && content.trimStart().startsWith(GOAL_CONTINUATION_PREFIX);
}

/**
 * 定时任务合成输入的前缀(后端 services/agent/icron.py 同名常量)。
 * 与目标续跑同一处理:它不是用户写的,渲染成系统小条而不是用户气泡。
 */
export const ICRON_MESSAGE_PREFIX = "[定时任务]";
const ICRON_NOTICE = "定时任务 · 到点自动执行(无人值守)";

export function isIcronMessage(content: string | null): boolean {
  return !!content && content.trimStart().startsWith(ICRON_MESSAGE_PREFIX);
}

export function historyToItems(messages: ChatMessageOut[]): ConversationItem[] {
  return [...messages]
    .sort((left, right) => left.sequence - right.sequence)
    .flatMap((message, index): ConversationItem[] => {
      if (message.role === "tool") {
        const callId = message.tool_call_id ?? message.id;
        const events: AgentEvent[] = [
          {
            type: "tool.call",
            seq: index * 2 + 1,
            tool_call_id: callId,
            name: message.tool_name ?? "unknown",
            input: message.tool_input ?? {},
          },
          {
            type: "tool.result",
            seq: index * 2 + 2,
            tool_call_id: callId,
            name: message.tool_name ?? "unknown",
            ok: !toolOutputHasError(message.tool_output),
            output: message.tool_output,
          },
        ];
        return [
          {
            key: message.id,
            role: "run",
            source: "persisted",
            runId: message.run_id,
            persistedRecordId: message.id,
            events,
            createdAt: message.created_at,
          },
        ];
      }
      if (
        !message.content ||
        (message.role === "user" &&
          INTERNAL_CONTINUATION_MESSAGES.has(message.content.trim()))
      )
        return [];
      if (message.role === "user" && isGoalContinuationMessage(message.content))
        return [
          {
            key: message.id,
            role: "notice",
            content: GOAL_CONTINUATION_NOTICE,
            createdAt: message.created_at,
          },
        ];
      if (message.role === "user" && isIcronMessage(message.content))
        return [
          {
            key: message.id,
            role: "notice",
            content: ICRON_NOTICE,
            createdAt: message.created_at,
          },
        ];
      return [
        {
          key: message.id,
          role: message.role,
          content: message.content,
          createdAt: message.created_at,
        },
      ];
    });
}

function toolOutputHasError(output: unknown): boolean {
  if (!isRecord(output) || !("error" in output)) return false;
  const error = output.error;
  if (error === null || error === undefined || error === false) return false;
  if (typeof error === "string") return error.trim().length > 0;
  return true;
}
