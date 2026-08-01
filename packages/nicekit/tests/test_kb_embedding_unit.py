"""KB-5B embedding oversize 折半重试单测(mock OpenAI 客户端,不出网)。"""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import openai
import pytest

import nicekit.kb.embedding as embedding_module
from nicekit.kb.embedding import EmbeddingService, EmbeddingUnavailableError
from nicekit.llm.capability_routes import ModelEndpoint
from nicekit.llm.service import LlmBudgetExceededError
from nicekit.models.kb import EMBEDDING_DIM


@pytest.fixture(autouse=True)
def embedding_settings(monkeypatch) -> None:
    monkeypatch.setattr(
        embedding_module,
        "get_settings",
        lambda: SimpleNamespace(
            embedding_provider="siliconflow",
            embedding_model="BAAI/bge-m3",
            embedding_batch_max_items=32,
            embedding_batch_max_estimated_tokens=8192,
        ),
    )


def _status_error(status_code: int, message: str) -> openai.APIStatusError:
    request = httpx.Request("POST", "http://test/v1/embeddings")
    response = httpx.Response(status_code, request=request)
    return openai.APIStatusError(message, response=response, body=None)


class _FakeEmbeddings:
    """前 failures 次调用抛指定异常,之后返回合法向量;记录每次输入。"""

    def __init__(self, failures: int, exc: Exception):
        self.calls: list[list[str]] = []
        self._failures = failures
        self._exc = exc

    async def create(self, model: str, input: list[str]):  # noqa: A002 - 对齐 SDK 签名
        self.calls.append(list(input))
        if len(self.calls) <= self._failures:
            raise self._exc
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index)] * EMBEDDING_DIM)
                for index, _ in enumerate(input)
            ],
            usage=SimpleNamespace(prompt_tokens=sum(len(text) for text in input)),
        )


class _FakeGovernance:
    def __init__(self):
        self.calls: list[dict] = []

    async def run_governed_call(self, **kwargs):
        result = await kwargs["call"]()
        tokens_in, tokens_out = kwargs["token_usage"](result)
        self.calls.append(
            {
                key: value
                for key, value in kwargs.items()
                if key not in {"call", "token_usage"}
            }
            | {"tokens_in": tokens_in, "tokens_out": tokens_out}
        )
        return result


class _BudgetGovernance(_FakeGovernance):
    def __init__(self, *, allowed_calls: int):
        super().__init__()
        self.allowed_calls = allowed_calls
        self.attempts: list[dict] = []

    async def run_governed_call(self, **kwargs):
        self.attempts.append(
            {
                key: value
                for key, value in kwargs.items()
                if key not in {"call", "token_usage"}
            }
        )
        if len(self.attempts) > self.allowed_calls:
            raise LlmBudgetExceededError(
                kwargs["org_id"],
                used=9_000,
                budget=10_000,
                estimated_tokens=kwargs["estimated_tokens"],
            )
        return await super().run_governed_call(**kwargs)


def _service(
    fake: _FakeEmbeddings,
    *,
    governance: _FakeGovernance | None = None,
    max_items: int = 32,
    max_tokens: int = 8192,
) -> EmbeddingService:
    svc = EmbeddingService(
        api_key="test-key",
        llm_service=governance or _FakeGovernance(),  # type: ignore[arg-type]
        max_batch_items=max_items,
        max_batch_estimated_tokens=max_tokens,
    )
    svc._client = SimpleNamespace(embeddings=fake)
    return svc


async def test_oversize_batch_splits_before_single_input_is_halved() -> None:
    fake = _FakeEmbeddings(2, _status_error(400, "input is too long for this model"))
    vectors = await _service(fake).embed(["x" * 1000, "y" * 10], org_id=uuid4())

    assert len(vectors) == 2 and len(vectors[0]) == EMBEDDING_DIM
    # 首次多输入 oversize 先二分；仅仍超长的单输入 1000 → 500，短文本保持原样。
    assert [[len(text) for text in call] for call in fake.calls] == [
        [1000, 10],
        [1000],
        [500],
        [10],
    ]


async def test_oversize_413_detected() -> None:
    fake = _FakeEmbeddings(1, _status_error(413, "payload"))
    await _service(fake).embed(["z" * 100], org_id=uuid4())
    assert len(fake.calls) == 2 and len(fake.calls[1][0]) == 50


async def test_oversize_exhausts_retries_raises_unavailable() -> None:
    """截半 3 次后仍超长 → 走既有 EmbeddingUnavailableError 路径(共 4 次调用)。"""
    fake = _FakeEmbeddings(99, _status_error(400, "maximum context length exceeded"))
    with pytest.raises(EmbeddingUnavailableError):
        await _service(fake).embed(["x" * 800], org_id=uuid4())
    assert len(fake.calls) == 4
    assert [len(c[0]) for c in fake.calls] == [800, 400, 200, 100]


async def test_non_oversize_error_fails_fast() -> None:
    """普通 5xx 不属于超长类,不折半重试。"""
    fake = _FakeEmbeddings(99, _status_error(500, "internal error"))
    with pytest.raises(EmbeddingUnavailableError):
        await _service(fake).embed(["hello"], org_id=uuid4())
    assert len(fake.calls) == 1


async def test_route_falls_back_to_second_embedding_endpoint() -> None:
    primary = _FakeEmbeddings(99, _status_error(503, "primary unavailable"))
    fallback = _FakeEmbeddings(0, RuntimeError("unused"))
    governance = _FakeGovernance()
    service = EmbeddingService(
        api_key="primary-key",
        provider="primary",
        model="primary-embedding",
        llm_service=governance,  # type: ignore[arg-type]
        fallback_endpoints=[
            ModelEndpoint(
                provider="backup",
                model="backup-embedding",
                api_key="backup-key",
                base_url="https://backup.test/v1",
            )
        ],
    )
    service._client = SimpleNamespace(embeddings=primary)
    service._fallback_attempts = [
        embedding_module._EmbeddingAttempt(  # noqa: SLF001
            provider="backup",
            model="backup-embedding",
            client=SimpleNamespace(embeddings=fallback),
        )
    ]

    vectors = await service.embed(["hello"], org_id=uuid4())

    assert len(vectors) == 1
    assert primary.calls == [["hello"]]
    assert fallback.calls == [["hello"]]
    assert governance.calls[0]["provider"] == "backup"
    assert governance.calls[0]["model"] == "backup-embedding"
    assert governance.calls[0]["attempt"] == 2
    assert governance.calls[0]["fallback_from"] == "primary:primary-embedding"


async def test_empty_list_returns_without_api_or_governance_call() -> None:
    fake = _FakeEmbeddings(0, RuntimeError("unused"))
    governance = _FakeGovernance()

    assert await _service(fake, governance=governance).embed([], org_id=uuid4()) == []
    assert fake.calls == []
    assert governance.calls == []


@pytest.mark.parametrize("text", ["", " ", "\t\r\n"])
async def test_empty_or_whitespace_text_is_rejected(text: str) -> None:
    fake = _FakeEmbeddings(0, RuntimeError("unused"))
    governance = _FakeGovernance()

    with pytest.raises(ValueError, match="empty or whitespace-only"):
        await _service(fake, governance=governance).embed([text], org_id=uuid4())
    assert fake.calls == []
    assert governance.calls == []


async def test_batches_by_item_limit_and_records_governance_parameters() -> None:
    fake = _FakeEmbeddings(0, RuntimeError("unused"))
    governance = _FakeGovernance()
    org_id = uuid4()
    vectors = await _service(fake, governance=governance, max_items=2).embed(
        ["a", "b", "c", "d", "e"], org_id=org_id, task="kb.reembed"
    )

    assert [len(call) for call in fake.calls] == [2, 2, 1]
    assert len(vectors) == 5
    assert [call["quantity"] for call in governance.calls] == [2, 2, 1]
    assert all(call["org_id"] == org_id for call in governance.calls)
    assert all(call["task"] == "kb.reembed" for call in governance.calls)
    assert all(call["provider"] == "siliconflow" for call in governance.calls)
    assert [call["tokens_in"] for call in governance.calls] == [2, 2, 1]


async def test_batches_by_estimated_token_limit_and_keeps_oversize_single() -> None:
    fake = _FakeEmbeddings(0, RuntimeError("unused"))
    await _service(fake, max_items=10, max_tokens=2).embed(
        ["a" * 4, "b" * 8, "c" * 20, "d" * 4], org_id=uuid4()
    )

    # Estimates are 1, 2, 5, 1. A single oversize input remains one API item.
    assert fake.calls == [["a" * 4], ["b" * 8], ["c" * 20], ["d" * 4]]


async def test_500_chunk_document_is_governed_in_batches_and_stops_at_budget() -> None:
    texts = [f"chunk-{index:03d}" for index in range(500)]
    org_id = uuid4()
    first_provider = _FakeEmbeddings(0, RuntimeError("unused"))
    first_governance = _BudgetGovernance(allowed_calls=2)

    with pytest.raises(LlmBudgetExceededError):
        await _service(
            first_provider,
            governance=first_governance,
            max_items=128,
            max_tokens=100_000,
        ).embed(texts, org_id=org_id, task="kb.ingestion.chunk")

    assert [len(call) for call in first_provider.calls] == [128, 128]
    assert [attempt["quantity"] for attempt in first_governance.attempts] == [
        128,
        128,
        128,
    ]
    assert sum(call["quantity"] for call in first_governance.calls) == 256
    assert sum(call["tokens_in"] for call in first_governance.calls) == sum(
        len(text) for text in texts[:256]
    )

    # The ingestion dispatcher requeues budget-paused work; a later retry can
    # process the remaining chunks through the same governed batching path.
    retry_provider = _FakeEmbeddings(0, RuntimeError("unused"))
    retry_governance = _BudgetGovernance(allowed_calls=2)
    vectors = await _service(
        retry_provider,
        governance=retry_governance,
        max_items=128,
        max_tokens=100_000,
    ).embed(texts[256:], org_id=org_id, task="kb.ingestion.chunk")

    assert [len(call) for call in retry_provider.calls] == [128, 116]
    assert len(vectors) == 244
    assert sum(call["quantity"] for call in retry_governance.calls) == 244
    assert sum(call["quantity"] for call in first_governance.calls) + sum(
        call["quantity"] for call in retry_governance.calls
    ) == 500


async def test_response_is_reordered_by_index() -> None:
    class ReversedEmbeddings:
        async def create(self, model: str, input: list[str]):  # noqa: A002
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=1, embedding=[2.0] * EMBEDDING_DIM),
                    SimpleNamespace(index=0, embedding=[1.0] * EMBEDDING_DIM),
                ],
                usage=SimpleNamespace(prompt_tokens=7),
            )

    svc = _service(_FakeEmbeddings(0, RuntimeError("unused")))
    svc._client = SimpleNamespace(embeddings=ReversedEmbeddings())
    vectors = await svc.embed(["first", "second"], org_id=uuid4())

    assert vectors[0][0] == 1.0
    assert vectors[1][0] == 2.0


@pytest.mark.parametrize(
    "data,usage,error",
    [
        ([], SimpleNamespace(prompt_tokens=1), "返回数量不符"),
        (
            [SimpleNamespace(index=1, embedding=[0.0] * EMBEDDING_DIM)],
            SimpleNamespace(prompt_tokens=1),
            "index 无效",
        ),
        (
            [SimpleNamespace(index=0, embedding=[0.0])],
            SimpleNamespace(prompt_tokens=1),
            "维度不符",
        ),
        (
            [SimpleNamespace(index=0, embedding=[0.0] * EMBEDDING_DIM)],
            SimpleNamespace(),
            "usage.prompt_tokens",
        ),
    ],
)
async def test_invalid_response_is_rejected(data, usage, error: str) -> None:
    class InvalidEmbeddings:
        async def create(self, model: str, input: list[str]):  # noqa: A002
            return SimpleNamespace(data=data, usage=usage)

    svc = _service(_FakeEmbeddings(0, RuntimeError("unused")))
    svc._client = SimpleNamespace(embeddings=InvalidEmbeddings())
    with pytest.raises(EmbeddingUnavailableError, match=error):
        await svc.embed(["one"], org_id=uuid4())
