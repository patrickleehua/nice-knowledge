"""providers 纯函数单元测试:strict 兼容性判定、思考等级映射与多模态内容块归一化(不碰网络)。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import openai
import pytest
from pydantic import BaseModel

from nicekit.llm.providers import (
    AnthropicProvider,
    OpenAIProvider,
    ProviderError,
    anthropic_thinking_budget,
    classify_provider_exception,
    extract_json_object,
    openai_reasoning_params,
    openai_strict_compatible,
    to_anthropic_content,
    to_anthropic_messages,
    to_openai_content,
)


class _StructuredOutput(BaseModel):
    answer: str


def _usage() -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        input_tokens_details=None,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


async def _generate_structured(provider):
    return await provider.generate_structured(
        model="test-model",
        system="test",
        messages=[{"role": "user", "content": "hello"}],
        output_model=_StructuredOutput,
        max_tokens=100,
        timeout_seconds=1,
    )


class _FakeStream:
    """替身:SDK 的 stream() 同步返回异步上下文管理器,请求在 __aenter__ 发出。"""

    def __init__(self, *, final=None, error: Exception | None = None, events=()) -> None:
        self._final = final
        self._error = error
        self._events = list(events)

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def __aiter__(self):
        self._pending = list(self._events)
        return self

    async def __anext__(self):
        if not self._pending:
            raise StopAsyncIteration
        return self._pending.pop(0)

    async def get_final_response(self):
        return self._final

    async def get_final_message(self):
        return self._final


def _stream(*, final=None, error: Exception | None = None, events=()) -> MagicMock:
    return MagicMock(return_value=_FakeStream(final=final, error=error, events=events))


def _response(status_code: int, *, request_id_header: str) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    return httpx.Response(
        status_code,
        request=request,
        headers={request_id_header: "req_safe_123"},
    )


async def test_openai_rate_limit_error_never_exposes_body() -> None:
    upstream = openai.RateLimitError(
        "SECRET_API_KEY provider response body",
        response=_response(429, request_id_header="x-request-id"),
        body={"error": "SECRET_API_KEY provider response body"},
    )
    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(stream=_stream(error=upstream))
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)

    assert caught.value.code == "rate_limit"
    assert caught.value.status_code == 429
    assert caught.value.request_id == "req_safe_123"
    assert caught.value.diagnostic == "rate_limit;status=429;request_id=req_safe_123"
    assert "SECRET_API_KEY" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_anthropic_status_error_never_exposes_body() -> None:
    upstream = anthropic.APIStatusError(
        "SECRET_TOKEN provider response body",
        response=_response(503, request_id_header="request-id"),
        body={"error": "SECRET_TOKEN provider response body"},
    )
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(stream=_stream(error=upstream))
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)

    assert caught.value.code == "http_error"
    assert caught.value.retryable is True
    assert caught.value.status_code == 503
    assert caught.value.request_id == "req_safe_123"
    assert "SECRET_TOKEN" not in caught.value.diagnostic
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_openai_schema_error_never_exposes_validation_input() -> None:
    # 正文 JSON 不符合 schema → 本地校验失败归类 schema_error,内容不外泄
    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(
            stream=_stream(
                final=SimpleNamespace(
                    output_text='{"secret": "SECRET_SCHEMA_VALUE"}', usage=_usage()
                )
            )
        )
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)

    assert str(caught.value) == "schema_error"
    assert caught.value.retryable is True
    assert "SECRET_SCHEMA_VALUE" not in caught.value.diagnostic
    assert caught.value.__suppress_context__ is True


async def test_openai_structured_parses_plain_text_json() -> None:
    # 结构化唯一路径:普通对话正文 → 本地解析(容忍围栏与前后说明文字)
    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(
            stream=_stream(
                final=SimpleNamespace(
                    output_text='好的,结果如下:\n```json\n{"answer": "墨尔本"}\n```',
                    usage=_usage(),
                )
            )
        )
    )

    result = await _generate_structured(provider)
    assert result.parsed == _StructuredOutput(answer="墨尔本")
    assert (result.tokens_in, result.tokens_out) == (10, 5)


async def test_anthropic_structured_parses_text_blocks() -> None:
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(
            stream=_stream(
                final=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text='{"answer": "ok"}')],
                    usage=_usage(),
                )
            )
        )
    )

    result = await _generate_structured(provider)
    assert result.parsed == _StructuredOutput(answer="ok")


async def test_anthropic_structured_no_json_is_empty_output() -> None:
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(
            stream=_stream(
                final=SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="三日轻松之旅")],
                    usage=_usage(),
                )
            )
        )
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)
    assert caught.value.code == "empty_output"
    assert caught.value.retryable is True


async def test_gateway_upstream_failure_400_is_retryable() -> None:
    # 网关把自己的回源失败包装成 400:必须判可重试,否则一次抖动打穿整条降级链
    request = httpx.Request("POST", "https://gw.example/v1/responses")
    response = httpx.Response(
        400,
        request=request,
        headers={"x-request-id": "req_safe_123"},
        json={"error": {"message": "Upstream request failed", "type": "upstream_error"}},
    )
    upstream = openai.APIStatusError("bad", response=response, body=None)
    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(stream=_stream(error=upstream))
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)
    assert caught.value.code == "http_error"
    assert caught.value.status_code == 400
    assert caught.value.retryable is True  # 真实非法请求的 400 仍不可重试


async def test_plain_400_stays_non_retryable() -> None:
    request = httpx.Request("POST", "https://gw.example/v1/responses")
    response = httpx.Response(
        400,
        request=request,
        headers={"x-request-id": "req_safe_123"},
        json={"error": {"message": "Invalid schema", "type": "invalid_request_error"}},
    )
    upstream = openai.APIStatusError("bad", response=response, body=None)
    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(stream=_stream(error=upstream))
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)
    assert caught.value.code == "http_error"
    assert caught.value.retryable is False


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('前言```json\n{"a": {"b": "x}y"}}\n```后记') == {
        "a": {"b": "x}y"}
    }
    assert extract_json_object('转义 {"a": "he said \\"}\\""} 收尾') == {"a": 'he said "}"'}
    assert extract_json_object("纯文本,没有对象") is None
    assert extract_json_object('{"未闭合": ') is None


@pytest.mark.parametrize(
    ("upstream", "expected_code"),
    [
        (
            openai.APITimeoutError(
                httpx.Request("POST", "https://provider.example/v1/responses")
            ),
            "timeout",
        ),
        (
            anthropic.APIConnectionError(
                message="SECRET_CONNECTION_DETAIL",
                request=httpx.Request(
                    "POST",
                    "https://provider.example/v1/messages",
                ),
            ),
            "connection_error",
        ),
    ],
)
def test_sdk_transport_errors_are_classified_without_message(
    upstream: Exception,
    expected_code: str,
) -> None:
    error = classify_provider_exception(upstream)

    assert error.code == expected_code
    assert error.retryable is True
    assert "SECRET_CONNECTION_DETAIL" not in error.diagnostic


async def test_openai_stream_error_never_exposes_event_body() -> None:
    class FailedStream:
        def __init__(self) -> None:
            self._sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._sent:
                raise StopAsyncIteration
            self._sent = True
            return SimpleNamespace(
                type="response.failed",
                response=SimpleNamespace(error="SECRET_STREAM_BODY"),
            )

    provider = OpenAIProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(create=AsyncMock(return_value=FailedStream()))
    )

    with pytest.raises(ProviderError) as caught:
        await provider.generate_with_tools(
            model="test-model",
            system="test",
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            max_tokens=100,
            timeout_seconds=1,
            on_delta=AsyncMock(),
        )

    assert str(caught.value) == "stream_error"
    assert "SECRET_STREAM_BODY" not in caught.value.diagnostic


def test_provider_error_rejects_untrusted_message_and_metadata() -> None:
    error = ProviderError(
        "SECRET_PROVIDER_BODY",
        retryable=True,
        status_code=999,
        request_id="req_safe;SECRET_TOKEN",
    )

    assert str(error) == "provider_error"
    assert error.diagnostic == "provider_error"


def _strict_schema(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def test_strict_schema_compatible():
    assert openai_strict_compatible(
        _strict_schema({"query": {"type": "string"}, "top_k": {"type": ["integer", "null"]}})
    )


def test_free_object_param_incompatible():
    # demand_patch 类工具:fields 为自由对象 → 必须降为非 strict
    schema = _strict_schema(
        {"project_id": {"type": ["string", "null"]},
         "fields": {"type": "object", "additionalProperties": True}}
    )
    assert not openai_strict_compatible(schema)


def test_missing_additional_properties_incompatible():
    # MCP 工具常见:未显式声明 additionalProperties → strict 拒绝
    assert not openai_strict_compatible(
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}
    )


def test_optional_property_incompatible():
    # strict 要求全部属性进 required
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert not openai_strict_compatible(schema)


def test_nested_free_object_incompatible():
    schema = _strict_schema(
        {"detail": _strict_schema({"inner": {"type": "object", "additionalProperties": True}})}
    )
    assert not openai_strict_compatible(schema)


_BUDGETS = {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "xhigh": 16384}


def test_anthropic_budget_levels():
    assert anthropic_thinking_budget("off", 8192, 2048, _BUDGETS) == 0  # 未列出 = 不开启
    assert anthropic_thinking_budget("low", 8192, 2048, _BUDGETS) == 2048
    assert anthropic_thinking_budget("medium", 8192, 2048, _BUDGETS) == 4096
    # clamp 到 max_tokens-1024
    assert anthropic_thinking_budget("high", 8192, 2048, _BUDGETS) == 7168
    assert anthropic_thinking_budget("xhigh", 32768, 2048, _BUDGETS) == 16384


def test_anthropic_budget_default_and_floor():
    # None = 维持 env 默认;小 max_tokens 下低于 1024 直接不开启
    assert anthropic_thinking_budget(None, 8192, 2048, _BUDGETS) == 2048
    assert anthropic_thinking_budget(None, 8192, 0, _BUDGETS) == 0
    assert anthropic_thinking_budget("low", 2000, 0, _BUDGETS) == 0


_VISION_BLOCKS = [
    {"type": "text", "text": "文件名:a.png"},
    {"type": "image", "media_type": "image/png", "data": "QUJD"},
]


def test_content_passthrough_for_plain_text():
    # 纯文本 content 原样透传,既有文本调用零变化
    assert to_openai_content("你好") == "你好"
    assert to_anthropic_content("你好") == "你好"


def test_to_openai_content_image_blocks():
    assert to_openai_content(_VISION_BLOCKS) == [
        {"type": "input_text", "text": "文件名:a.png"},
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
    ]


def test_to_anthropic_content_image_blocks():
    assert to_anthropic_content(_VISION_BLOCKS) == [
        {"type": "text", "text": "文件名:a.png"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
        },
    ]


def test_to_anthropic_messages_skips_orphan_empty_assistant() -> None:
    assert to_anthropic_messages(
        [
            {"role": "user", "content": "开始"},
            {"role": "assistant", "content": None, "tool_calls": []},
            {"role": "assistant", "content": "完成"},
        ]
    ) == [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "完成"},
    ]


def test_to_anthropic_messages_keeps_tool_pairs_without_empty_text() -> None:
    output = to_anthropic_messages(
        [
            {"role": "user", "content": "生成"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "name": "one", "arguments": {}},
                    {"id": "c2", "name": "two", "arguments": {"x": 1}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "one", "content": "{}"},
            {"role": "tool", "tool_call_id": "c2", "name": "two", "content": "{}"},
        ]
    )
    assert output[1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "c1", "name": "one", "input": {}},
            {"type": "tool_use", "id": "c2", "name": "two", "input": {"x": 1}},
        ],
    }
    assert [block["tool_use_id"] for block in output[2]["content"]] == ["c1", "c2"]


def test_openai_reasoning_params():
    effort_map = {"off": "none"}
    assert openai_reasoning_params(None, effort_map) is None
    # 映射表命中的改写,未列出的原样透传(新档位只改配置)
    assert openai_reasoning_params("off", effort_map) == {
        "effort": "none",
        "summary": "auto",
    }
    assert openai_reasoning_params("xhigh", effort_map) == {
        "effort": "xhigh",
        "summary": "auto",
    }


class _EventStream:
    """替身:responses.create(stream=True) 返回的事件流。"""

    def __init__(self, events: list) -> None:
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


def _message_item(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="message",
        content=[SimpleNamespace(type="output_text", text=text)],
    )


def _function_item(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=arguments)


def _final(output: list, *, output_text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        output=output, output_text=output_text, usage=_usage(), incomplete_details=None
    )


async def _tools_turn(provider, events: list):
    calls: list[str] = []

    async def on_delta(text: str | None) -> None:
        calls.append(text or "")

    return await provider.generate_with_tools(
        model="test-model",
        system="test",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        max_tokens=100,
        timeout_seconds=1,
        on_delta=on_delta,
    )


async def test_openai_prefers_final_output_over_streamed_items() -> None:
    # 终值响应带 output 时以它为准(官方行为),流式累积项不参与
    provider = OpenAIProvider("test-key")
    final = _final([_message_item("来自终值")], output_text="来自终值")
    events = [
        SimpleNamespace(type="response.output_item.done", item=_message_item("来自流式")),
        SimpleNamespace(type="response.completed", response=final),
    ]
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(create=AsyncMock(return_value=_EventStream(events)))
    )
    result = await _tools_turn(provider, events)
    assert result.text == "来自终值"


async def test_openai_falls_back_to_streamed_items_when_output_empty() -> None:
    # 个别网关的 completed 事件不回填 output:回退到 output_item.done 还原文本与工具调用
    provider = OpenAIProvider("test-key")
    events = [
        SimpleNamespace(type="response.output_item.done", item=_message_item("流式正文")),
        SimpleNamespace(
            type="response.output_item.done",
            item=_function_item("call_1", "get_weather", '{"city":"北京"}'),
        ),
        SimpleNamespace(type="response.completed", response=_final([])),
    ]
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(create=AsyncMock(return_value=_EventStream(events)))
    )
    result = await _tools_turn(provider, events)
    assert result.text == "流式正文"
    assert result.stop_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_1", "get_weather", {"city": "北京"})
    ]


async def test_max_output_tokens_sent_by_default_and_omitted_only_for_named_instance() -> None:
    # 默认必须照发(官方 API 语义不变);只有显式配置的实例才省略
    default_provider = OpenAIProvider("k")
    omitted_provider = OpenAIProvider("k", send_max_output_tokens=False)
    assert default_provider._max_output_tokens(4096) == 4096
    assert omitted_provider._max_output_tokens(4096) is openai.NOT_GIVEN


def test_omit_switch_is_scoped_to_named_instance(monkeypatch) -> None:
    """兼容性开关按 llm_providers.name 生效,同协议的其他实例不受影响。"""
    from nicekit.core.config import get_settings
    from nicekit.llm.service import build_protocol_provider

    get_settings.cache_clear()
    monkeypatch.setenv("LLM_OPENAI_OMIT_MAX_OUTPUT_TOKENS", '["gateway"]')
    try:
        quirky = build_protocol_provider("openai", "k", "http://gw/v1", instance="gateway")
        official = build_protocol_provider("openai", "k", "", instance="openai")
        assert quirky._max_output_tokens(4096) is openai.NOT_GIVEN
        assert official._max_output_tokens(4096) == 4096  # 未列出的实例照常发送
    finally:
        get_settings.cache_clear()


async def test_openai_structured_falls_back_to_streamed_items() -> None:
    # 结构化路径同样要能在 output 未回填时还原正文(否则直接 empty_output)
    provider = OpenAIProvider("test-key")
    events = [
        SimpleNamespace(
            type="response.output_item.done", item=_message_item('{"answer": "墨尔本"}')
        )
    ]
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        responses=SimpleNamespace(
            stream=_stream(final=_final([]), events=events)
        )
    )
    result = await _generate_structured(provider)
    assert result.parsed == _StructuredOutput(answer="墨尔本")


class _BrokenStream:
    """替身:流已开始,迭代到一半对端断开(SDK 不包装这类 httpx 异常)。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self._sent = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._sent:
            raise self._exc
        self._sent = True
        return SimpleNamespace(type="response.in_progress")

    async def get_final_message(self):
        raise self._exc

    async def get_final_response(self):
        raise self._exc


_CHUNKED = "peer closed connection without sending complete message body (incomplete chunked read)"


async def test_anthropic_mid_stream_disconnect_is_classified_not_leaked() -> None:
    # 断流发生在流开始之后:必须归类成可重试 connection_error,
    # 不能让 httpx/h11 的英文原文冒到 agent loop 当作"模型调用失败"展示
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(
            stream=MagicMock(return_value=_BrokenStream(httpx.RemoteProtocolError(_CHUNKED)))
        )
    )

    with pytest.raises(ProviderError) as caught:
        await provider.generate_with_tools(
            model="test-model", system="test",
            messages=[{"role": "user", "content": "hello"}], tools=[],
            max_tokens=100, timeout_seconds=1, on_delta=AsyncMock(),
        )
    assert caught.value.code == "connection_error"
    assert caught.value.retryable is True
    assert "peer closed" not in str(caught.value)
    assert "chunked" not in caught.value.diagnostic


async def test_anthropic_structured_mid_stream_disconnect_is_classified() -> None:
    provider = AnthropicProvider("test-key")
    provider._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(
            stream=MagicMock(return_value=_BrokenStream(httpx.RemoteProtocolError(_CHUNKED)))
        )
    )

    with pytest.raises(ProviderError) as caught:
        await _generate_structured(provider)
    assert caught.value.code == "connection_error"
    assert "peer closed" not in str(caught.value)


def test_httpx_transport_errors_are_classified() -> None:
    # 兜底分类器同样不能把传输层异常落到 provider_error(不可重试)
    error = classify_provider_exception(httpx.RemoteProtocolError(_CHUNKED))
    assert error.code == "connection_error"
    assert error.retryable is True
    assert "peer closed" not in error.diagnostic
