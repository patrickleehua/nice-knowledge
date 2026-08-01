"""Pure approval-bundle decision and idempotency helpers."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime


class ApprovalDecisionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalBundleUpdate:
    bundle: dict
    changed_items: tuple[dict, ...]

    @property
    def resolved(self) -> bool:
        return not any(item.get("status") == "pending" for item in self.bundle["items"])


def is_approval_bundle(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("kind") == "approval_bundle"
        and isinstance(value.get("items"), list)
    )


def _requested_decisions(confirm_action: dict) -> list[dict]:
    decisions = confirm_action.get("decisions")
    if isinstance(decisions, list):
        return [dict(item) for item in decisions]
    tool_call_id = confirm_action.get("tool_call_id")
    approved = confirm_action.get("approved")
    if tool_call_id is None or not isinstance(approved, bool):
        raise ApprovalDecisionError("invalid approval decision payload")
    return [
        {
            "tool_call_id": str(tool_call_id),
            "decision": "approve" if approved else "deny",
            "scope": "once",
            "note": None,
        }
    ]


def apply_approval_decisions(bundle: dict, confirm_action: dict) -> ApprovalBundleUpdate:
    updated = deepcopy(bundle)
    requested_bundle_id = confirm_action.get("bundle_id")
    if requested_bundle_id is not None and str(requested_bundle_id) != str(
        updated.get("bundle_id")
    ):
        raise ApprovalDecisionError("approval bundle does not match")
    by_call_id = {str(item.get("tool_call_id")): item for item in updated["items"]}
    changed: list[dict] = []
    seen: set[str] = set()
    decided_at = datetime.now(UTC).isoformat()
    for requested in _requested_decisions(confirm_action):
        call_id = str(requested.get("tool_call_id") or "")
        if not call_id or call_id in seen:
            raise ApprovalDecisionError("approval decision tool_call_id is missing or duplicated")
        seen.add(call_id)
        item = by_call_id.get(call_id)
        if item is None or item.get("status") != "pending":
            raise ApprovalDecisionError("approval item is not pending")
        decision = requested.get("decision")
        if decision not in {"approve", "deny"}:
            raise ApprovalDecisionError("approval decision must be approve or deny")
        decision_scope = requested.get("scope") or "once"
        if decision == "deny":
            decision_scope = "once"
        elif decision_scope not in item.get("eligible_scopes", ["once"]):
            raise ApprovalDecisionError("approval scope is not eligible for this item")
        item["decision"] = decision
        item["decision_scope"] = decision_scope
        item["user_note"] = requested.get("note")
        item["decision_reason_code"] = (
            "user_approved" if decision == "approve" else "user_denied"
        )
        item["decided_at"] = decided_at
        item["status"] = "approved" if decision == "approve" else "denied"
        changed.append(deepcopy(item))
    updated["status"] = (
        "ready"
        if not any(item.get("status") == "pending" for item in updated["items"])
        else "pending"
    )
    return ApprovalBundleUpdate(updated, tuple(changed))


def supersede_pending_approval_bundle(
    bundle: dict,
    *,
    note: str = "用户发送了新消息，未批准待执行操作",
) -> ApprovalBundleUpdate:
    updated = deepcopy(bundle)
    changed: list[dict] = []
    decided_at = datetime.now(UTC).isoformat()
    for item in updated["items"]:
        if item.get("status") != "pending":
            continue
        item["decision"] = "deny"
        item["decision_scope"] = "once"
        item["user_note"] = note
        item["decision_reason_code"] = "user_superseded"
        item["decided_at"] = decided_at
        item["status"] = "denied"
        changed.append(deepcopy(item))
    updated["status"] = "ready"
    return ApprovalBundleUpdate(updated, tuple(changed))
