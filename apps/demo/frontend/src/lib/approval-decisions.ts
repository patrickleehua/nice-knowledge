import type {
  ApprovalBundle,
  ApprovalItemDecision,
  ConfirmationAction,
  LegacyPendingConfirmation,
  PendingConfirmation,
} from "@/lib/chat";

export type DraftApprovalDecision = Pick<
  ApprovalItemDecision,
  "decision" | "scope"
> & { note?: string };

export function legacyConfirmationAction(
  pending: LegacyPendingConfirmation,
  approved: boolean,
): ConfirmationAction {
  return { tool_call_id: pending.tool_call_id, approved };
}

export function confirmationKey(
  pending: PendingConfirmation | null | undefined,
): string | null {
  if (!pending) return null;
  return "kind" in pending && pending.kind === "approval_bundle"
    ? `bundle:${pending.bundle_id}`
    : `tool:${pending.tool_call_id}`;
}

/** Build only a complete, server-valid decision for the bundle's pending items. */
export function buildBundleConfirmationAction(
  bundle: ApprovalBundle,
  drafts: Readonly<Record<string, DraftApprovalDecision | undefined>>,
): ConfirmationAction | null {
  const pendingItems = bundle.items.filter((item) => item.status === "pending");
  if (pendingItems.length === 0) return null;

  const decisions: ApprovalItemDecision[] = [];
  for (const item of pendingItems) {
    const draft = drafts[item.tool_call_id];
    if (!draft) return null;
    if (draft.decision === "deny" && draft.scope !== "once") return null;
    if (
      draft.decision === "approve" &&
      !item.eligible_scopes.includes(draft.scope)
    )
      return null;
    const note = draft.note?.trim();
    decisions.push({
      tool_call_id: item.tool_call_id,
      decision: draft.decision,
      scope: draft.scope,
      note: note ? note.slice(0, 500) : undefined,
    });
  }

  return { bundle_id: bundle.bundle_id, decisions };
}
