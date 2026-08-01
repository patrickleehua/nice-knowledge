"""会话上下文压缩单测(mock LLM 与 DB 会话,无真实外部服务):
水位触发判断、摘要写入、覆盖序列后的历史注入、连续失败熔断、后台任务吞异常。"""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from nicekit.agent import compression
from nicekit.agent.compression import (
    compress_session,
    rows_after_summary,
    should_compress,
    summary_user_message,
)
from nicekit.models.chat import ChatMessage, ChatSessionSummary

ORG_ID = uuid4()
SESSION_ID = uuid4()


def _row(sequence: int, role: str, content: str, **values) -> ChatMessage:
    return ChatMessage(
        id=uuid4(),
        org_id=ORG_ID,
        session_id=SESSION_ID,
        sequence=sequence,
        role=role,
        content=content,
        **values,
    )


def _summary(
    covered_until: int, status: str = "success", content: str = "早前决定去巴黎"
) -> ChatSessionSummary:
    return ChatSessionSummary(
        org_id=ORG_ID,
        session_id=SESSION_ID,
        content=content,
        covered_until_sequence=covered_until,
        token_estimate=0,
        status=status,
    )


def _heavy_rows(count: int = 30, chunk: int = 10000) -> list[ChatMessage]:
    """交替 user/assistant 的重会话:count 条 × chunk 字符,轻松越过水位。"""
    return [
        _row(i + 1, "user" if i % 2 == 0 else "assistant", "x" * chunk)
        for i in range(count)
    ]


class FakeResult:
    def __init__(self, items: list):
        self._items = items

    def scalars(self):
        return self

    def all(self) -> list:
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class FakeSession:
    """按 compress_session 的查询顺序依次吐结果:
    1) 最近摘要(熔断检查) 2) 最新 success 摘要 3) 消息行。"""

    def __init__(self, results: list[list]):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        return FakeResult(self._results.pop(0))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def close(self) -> None:
        pass


class FakeLLM:
    def __init__(
        self,
        text: str | None = "客户确定 6 月去巴黎,预算 3 万。",
        error: Exception | None = None,
    ):
        self.calls: list[dict] = []
        self._text = text
        self._error = error

    async def generate_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(text=self._text)


def _patch_session(monkeypatch, fake: FakeSession) -> None:
    monkeypatch.setattr(compression, "org_session", lambda factory, org_id: fake)


# ---------- 水位触发判断 ----------


def test_should_compress_requires_min_messages() -> None:
    # 字符量再大,消息数不足 20 也不触发(单条巨型输出交给双层预算)
    assert should_compress(_heavy_rows(count=19, chunk=50000)) is False


def test_should_compress_requires_watermark() -> None:
    assert should_compress(_heavy_rows(count=30, chunk=10)) is False


def test_should_compress_at_watermark() -> None:
    # 20 条 × 9000 字符 = 180000 字符 → 45000 token,恰达水位
    assert should_compress(_heavy_rows(count=20, chunk=9000)) is True


# ---------- 摘要写入 ----------


async def test_compress_writes_success_summary(monkeypatch) -> None:
    rows = _heavy_rows(30)
    fake = FakeSession([[], [], rows])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM()

    summary = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert summary is not None and summary.status == "success"
    assert summary.content == "客户确定 6 月去巴黎,预算 3 万。"
    assert summary.token_estimate == len(summary.content) // 4
    # 边界:候选 30-15=15(assistant 行),向前吸附到 index 14 的 user 行,
    # 段尾是 sequence=14 → 被保留段以 user 行(seq 15)开头,历史重建合法
    assert summary.covered_until_sequence == 14
    assert fake.added == [summary]
    assert fake.commits == 1
    # LLM 调用走 workbench 路由、无工具、系统指令要求保留决策/约束/未决问题
    (call,) = llm.calls
    assert call["task"] == compression.SUMMARY_TASK
    assert call["tools"] == []
    assert "决策" in call["system"] and "约束" in call["system"]


async def test_compress_below_watermark_is_noop(monkeypatch) -> None:
    fake = FakeSession([[], [], _heavy_rows(count=10, chunk=10)])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM()

    result = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert result is None
    assert llm.calls == [] and fake.added == []


async def test_compress_merges_prior_summary_into_prompt(monkeypatch) -> None:
    # 已有 success 摘要时增量压缩:既有摘要喂给模型合并,新摘要整体取代旧摘要
    prior = _summary(covered_until=6, content="客户此前确定目的地巴黎")
    fake = FakeSession([[prior], [prior], _heavy_rows(30)])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM()

    summary = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert summary is not None and summary.status == "success"
    (call,) = llm.calls
    assert "【既有摘要】" in call["messages"][0]["content"]
    assert "客户此前确定目的地巴黎" in call["messages"][0]["content"]


async def test_compress_failure_writes_failed_row(monkeypatch) -> None:
    fake = FakeSession([[], [], _heavy_rows(30)])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM(error=RuntimeError("全 provider 失败"))

    summary = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert summary is not None and summary.status == "failed"
    assert "摘要生成失败" in summary.content
    assert fake.added == [summary] and fake.commits == 1  # 失败痕迹也落库,供熔断计数


async def test_compress_empty_llm_text_counts_as_failure(monkeypatch) -> None:
    fake = FakeSession([[], [], _heavy_rows(30)])
    _patch_session(monkeypatch, fake)

    summary = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=FakeLLM(text="  ")
    )

    assert summary is not None and summary.status == "failed"


# ---------- 连续失败熔断 ----------


async def test_three_consecutive_failures_trip_circuit_breaker(monkeypatch) -> None:
    failed = [_summary(covered_until=10, status="failed", content="(失败)") for _ in range(3)]
    fake = FakeSession([failed])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM()

    result = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert result is None
    assert llm.calls == [] and fake.added == []


async def test_success_within_recent_three_keeps_compressing(monkeypatch) -> None:
    recent = [
        _summary(covered_until=10, status="failed", content="(失败)"),
        _summary(covered_until=10, status="failed", content="(失败)"),
        _summary(covered_until=6),
    ]
    fake = FakeSession([recent, [recent[2]], _heavy_rows(30)])
    _patch_session(monkeypatch, fake)
    llm = FakeLLM()

    summary = await compress_session(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=llm
    )

    assert summary is not None and summary.status == "success"


# ---------- 覆盖序列后的历史注入 ----------


def _rows_to_history(rows) -> list[dict]:
    """service.rows_to_history 的最小替身(service 层随 P2c 交付)。

    本用例只关心摘要覆盖之后的行才进历史这一条,不需要工具行重建。
    """
    return [{"role": row.role, "content": row.content} for row in rows]


def test_history_injection_after_covered_sequence() -> None:
    rows = [
        _row(1, "user", "老历史 1"),
        _row(2, "assistant", "老历史 2"),
        _row(3, "user", "老历史 3"),
        _row(4, "assistant", "老历史 4"),
        _row(5, "user", "新问题"),
        _row(6, "assistant", "新回答"),
    ]
    summary = _summary(covered_until=4, content="客户确定去巴黎,预算 3 万,未定酒店。")

    history = _rows_to_history(rows_after_summary(rows, summary))
    history.insert(0, summary_user_message(summary))

    # 摘要作为 user 角色注入(多条 system 各 provider 兼容性不一),旧行不再出现
    assert history[0]["role"] == "user"
    assert history[0]["content"].startswith("【早前对话摘要】\n")
    assert "客户确定去巴黎" in history[0]["content"]
    assert [m["content"] for m in history[1:]] == ["新问题", "新回答"]


def test_no_summary_keeps_full_history() -> None:
    rows = [_row(1, "user", "你好"), _row(2, "assistant", "在")]
    assert rows_after_summary(rows, None) == rows


# ---------- 后台任务 ----------


async def test_schedule_compression_swallows_exceptions(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("数据库不可用")

    monkeypatch.setattr(compression, "compress_session", boom)
    task = compression.schedule_compression(
        MagicMock(), org_id=ORG_ID, session_id=SESSION_ID, llm=FakeLLM()
    )
    await task  # 不得向外抛异常(响应路径不能被压缩失败拖垮)
    assert task.exception() is None
