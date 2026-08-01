"""运行中输入队列纯逻辑测试(无 DB,与 test_agent_loop.py 同口径):
入队位置分配、编辑/删除资格、重排校验与重编号、消费幂等判定、
终态跳过资格、input.* 事件载荷。
DB 读写是 run_inputs.py 里的薄封装,行为由这些纯函数决定。
API schema 用例随 api/v1/chat.py 在后续波次搬运。
"""

from uuid import uuid4

import pytest

from nicekit.agent.run_inputs import (
    QUEUE_EVENT_SEQ_BASE,
    is_editable,
    needs_consumption,
    next_position,
    queue_event_payload,
    reorder_positions,
)
from nicekit.models.chat import ChatRunInput, ChatRunInputStatus

ORG_ID = uuid4()
SESSION_ID = uuid4()
RUN_ID = uuid4()
USER_ID = uuid4()


def _item(
    position: int,
    *,
    status: str = ChatRunInputStatus.QUEUED.value,
    content: str = "补充说明",
    consumed_message_id=None,
) -> ChatRunInput:
    return ChatRunInput(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        user_id=USER_ID,
        content=content,
        position=position,
        status=status,
        consumed_message_id=consumed_message_id,
    )


# ---------------------------------------------------------------------------
# 入队:position = 当前队列 max+1
# ---------------------------------------------------------------------------


def test_next_position_starts_at_one_for_empty_queue() -> None:
    assert next_position([]) == 1


def test_next_position_appends_after_current_max() -> None:
    assert next_position([1, 2, 3]) == 4
    # 中间被删过的队列也只看 max,不回填空洞(保持先来后到直觉)
    assert next_position([1, 5]) == 6


# ---------------------------------------------------------------------------
# 编辑/删除资格:仅 queued;consumed/skipped 是终态
# ---------------------------------------------------------------------------


def test_only_queued_inputs_are_editable() -> None:
    assert is_editable(_item(1)) is True
    assert is_editable(_item(1, status=ChatRunInputStatus.CONSUMED.value)) is False
    assert is_editable(_item(1, status=ChatRunInputStatus.SKIPPED.value)) is False


# ---------------------------------------------------------------------------
# 重排:ids 必须恰好是当前全部 queued 项的一个排列
# ---------------------------------------------------------------------------


def test_reorder_renumbers_positions_in_given_order() -> None:
    first, second, third = _item(1), _item(2), _item(3)
    assignments = reorder_positions(
        [first, second, third], [third.id, first.id, second.id]
    )
    assert assignments == [(third.id, 1), (first.id, 2), (second.id, 3)]


def test_reorder_ignores_terminal_items_in_current_snapshot() -> None:
    queued = _item(2)
    consumed = _item(1, status=ChatRunInputStatus.CONSUMED.value)
    # 已消费项不参与重排,也不要求出现在 ids 里
    assert reorder_positions([consumed, queued], [queued.id]) == [(queued.id, 1)]


@pytest.mark.parametrize(
    "build_ids",
    [
        lambda items: [items[0].id],  # 少给
        lambda items: [items[0].id, items[1].id, uuid4()],  # 混入未知 id
        lambda items: [items[0].id, items[0].id],  # 重复
        lambda items: [],  # 空
    ],
)
def test_reorder_rejects_partial_or_stale_id_sets(build_ids) -> None:
    items = [_item(1), _item(2)]
    with pytest.raises(ValueError):
        reorder_positions(items, build_ids(items))


# ---------------------------------------------------------------------------
# 消费幂等:已带 consumed_message_id 的项不再重复转正式消息
# ---------------------------------------------------------------------------


def test_queued_input_needs_consumption() -> None:
    assert needs_consumption(_item(1)) is True


def test_consumed_input_with_message_is_idempotent() -> None:
    done = _item(
        1,
        status=ChatRunInputStatus.CONSUMED.value,
        consumed_message_id=uuid4(),
    )
    assert needs_consumption(done) is False


def test_half_consumed_input_without_message_is_retried() -> None:
    # 状态翻了但消息行缺失(历史脏数据)时允许重写,勿把内容丢掉
    broken = _item(1, status=ChatRunInputStatus.CONSUMED.value)
    assert needs_consumption(broken) is True


# ---------------------------------------------------------------------------
# input.* 事件载荷
# ---------------------------------------------------------------------------


def test_queue_event_payload_carries_queue_card_fields() -> None:
    item = _item(2, content="顺便查一下天气")
    payload = queue_event_payload("input.queued", item)
    assert payload == {
        "type": "input.queued",
        "queued_input_id": str(item.id),
        "session_id": str(SESSION_ID),
        "run_id": str(RUN_ID),
        "position": 2,
        "status": "queued",
        "content": "顺便查一下天气",
        # 该条输入自带的"本轮临时工具";没勾时是空列表
        "runtime_tools": [],
    }


def test_queue_event_payload_optional_message_and_reason() -> None:
    message_id = uuid4()
    consumed = _item(
        1,
        status=ChatRunInputStatus.CONSUMED.value,
        consumed_message_id=message_id,
    )
    payload = queue_event_payload("input.consumed", consumed, message_id=message_id)
    assert payload["message_id"] == str(message_id)

    skipped = _item(1, status=ChatRunInputStatus.SKIPPED.value)
    payload = queue_event_payload("input.skipped", skipped, reason="run_cancelled")
    assert payload["reason"] == "run_cancelled"
    assert payload["status"] == "skipped"


def test_api_side_queue_events_use_reserved_seq_range() -> None:
    # API 侧落 chat_events 的 seq 独立高位段,不与在跑 run 的实时 seq 撞唯一键
    assert QUEUE_EVENT_SEQ_BASE >= 1_000_000
