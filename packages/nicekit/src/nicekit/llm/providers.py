"""双协议 Provider:结构化输出(本地校验)+ 工具调用。

签名已对本地安装版本核实(openai==2.44 / anthropic==0.116):
- OpenAI:  client.responses.create(tools=[{type:"function",name,parameters,strict}])
           -> output 项 type=="function_call"(call_id/name/arguments 为 JSON 串)
- Anthropic: client.messages.create(tools=[{name,description,input_schema}])
           -> content 块 ToolUseBlock(id/name/input),stop_reason=="tool_use"

结构化输出的设计决策(2026-07,勿回退):
1. 不用服务端约束解码端点(responses.parse/text_format、messages.parse/output_format)。
   部署面向 OpenAI 兼容网关,实测这类端点不可靠 —— output_format 被静默忽略
   (返回自由文本)、连接被无响应掐断、/responses 曾整体回源失败。唯一路径:
   schema 写进指令(structured_system)→ 对话 → extract_json_object + pydantic
   本地校验(parse_structured_text)。行为在原生 API 与任意网关上一致,失败归类为
   可重试 schema_error/empty_output,由降级链接管。
2. 一律流式(messages.stream / responses.stream + SDK 的 get_final_*)。结构化任务
   输出长(实测生成 8 日行程 ~6.7k tokens / 123 秒),非流式请求在网关的静默等待
   上限内出不来响应,会被直接断连(实测 60~180 秒);流式有持续增量,同一请求稳定
   成功。generate_with_tools 也一律流式:它曾在 on_delta 为空时退回非流式,
   而 KB 有据问答正是这样的调用方 —— 撞上网关不符合 Response schema 的非流式
   响应体,SDK 解析成 str,直到读 `.output` 才炸 AttributeError(2026-08 修)。
"""

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import anthropic
import httpx
import openai
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# 文本增量回调:str=一段新文本;None=丢弃此前增量(降级链换 provider 重试时发出)
DeltaFn = Callable[[str | None], Awaitable[None]]

# 思考等级:档位表与映射由 settings 配置(llm_thinking_levels 等),provider 只做查表;
# level=None 表示未指定,维持各 provider 原默认行为(openai 不发 reasoning,
# anthropic 用构造器注入的默认 budget)


def anthropic_thinking_budget(
    level: str | None,
    max_tokens: int,
    default_budget: int,
    budget_map: dict[str, int] | None = None,
) -> int:
    """按思考等级查 budget_tokens;0 = 不开启 thinking(被动接收上游返回)。
    SDK 约束:budget >= 1024 且 < max_tokens,超限自动钳制。"""
    budget = default_budget if level is None else (budget_map or {}).get(level, 0)
    budget = min(budget, max_tokens - 1024)  # 必须给正文留空间且 < max_tokens
    return budget if budget >= 1024 else 0


def openai_reasoning_params(
    level: str | None, effort_map: dict[str, str] | None = None
) -> dict | None:
    """按思考等级构造 Responses reasoning 参数;effort_map 未列出的档位原样透传
    (新模型出新档位时只改配置);summary=auto 使思考摘要可流式。"""
    if level is None:
        return None
    effort = (effort_map or {}).get(level, level)
    return {"effort": effort, "summary": "auto"}


PROVIDER_ERROR_CODES = frozenset(
    {
        "connection_error",
        "empty_output",
        "http_error",
        "invalid_tool_arguments",
        "provider_error",
        "rate_limit",
        "schema_error",
        "stream_error",
        "stream_incomplete",
        "timeout",
    }
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIAGNOSTIC_RE = re.compile(
    rf"^(?:{'|'.join(sorted(PROVIDER_ERROR_CODES))})"
    r"(?:;status=[1-5][0-9]{2})?"
    r"(?:;request_id=[A-Za-z0-9][A-Za-z0-9._:-]{0,127})?$"
)


def _safe_error_code(value: object) -> str:
    return value if isinstance(value, str) and value in PROVIDER_ERROR_CODES else "provider_error"


def _safe_status_code(value: object) -> int | None:
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _safe_request_id(value: object) -> str | None:
    return value if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) else None


class ProviderError(Exception):
    """Structured provider failure with no raw upstream response content."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = _safe_error_code(code)
        self.retryable = bool(retryable)
        self.status_code = _safe_status_code(status_code)
        self.request_id = _safe_request_id(request_id)
        super().__init__(self.code)

    @property
    def diagnostic(self) -> str:
        parts = [self.code]
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.request_id is not None:
            parts.append(f"request_id={self.request_id}")
        return ";".join(parts)

    def __str__(self) -> str:
        return self.diagnostic


def sanitize_provider_error(exc: ProviderError) -> ProviderError:
    """Rebuild an error from allowlisted fields at a trust boundary."""
    return ProviderError(
        _safe_error_code(getattr(exc, "code", None)),
        retryable=bool(getattr(exc, "retryable", False)),
        status_code=_safe_status_code(getattr(exc, "status_code", None)),
        request_id=_safe_request_id(getattr(exc, "request_id", None)),
    )


def classify_provider_exception(exc: Exception) -> ProviderError:
    """Classify SDK or custom failures without reading their message/body."""
    if isinstance(exc, ProviderError):
        return sanitize_provider_error(exc)
    if isinstance(exc, (openai.APITimeoutError, anthropic.APITimeoutError)):
        return _sdk_error("timeout", retryable=True, exc=exc)
    if isinstance(exc, (openai.RateLimitError, anthropic.RateLimitError)):
        return _sdk_error(
            "rate_limit",
            retryable=True,
            exc=exc,
            status_code=429,
        )
    if isinstance(exc, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return _sdk_error("connection_error", retryable=True, exc=exc)
    if isinstance(exc, (openai.APIStatusError, anthropic.APIStatusError)):
        return _sdk_status_error(exc)
    if isinstance(exc, httpx.HTTPError):
        # 流已开始后断开等传输层失败:SDK 不包装,不归类就会泄漏库内部英文文案
        return ProviderError("connection_error", retryable=True)
    if isinstance(exc, ValidationError):
        return ProviderError("schema_error", retryable=True)
    return ProviderError("provider_error", retryable=False)


def sanitize_provider_diagnostic(value: object) -> str:
    """Accept only diagnostics previously produced by ``ProviderError``."""
    if isinstance(value, str) and _DIAGNOSTIC_RE.fullmatch(value):
        return value
    return "provider_error"


def transport_error(exc: httpx.HTTPError, shape: str) -> ProviderError:
    """流式传输中途的底层网络异常归一化。

    SDK 只包装"发起请求"阶段的失败;流已经开始后对端断开(如 h11 的
    ``peer closed connection ... incomplete chunked read``)属于 httpx 异常,
    不归类就会原样冒到 agent loop,把库内部英文文案当成"模型调用失败"给用户看。
    """
    log_unfinished_call("connection_error", shape, exc)
    return ProviderError("connection_error", retryable=True)


def log_unfinished_call(code: str, shape: str, exc: Exception) -> None:
    """连接/超时类失败也要留痕:否则线上只看得到 connection_error 四个字。

    SDK 的 APIConnectionError 文案恒为 "Connection error.",真正的原因(DNS、
    连接被重置、读超时)只在 __cause__ 里。
    """
    cause = exc.__cause__
    detail = f"{type(cause).__name__}: {cause}" if cause is not None else str(exc)
    logger.warning("provider 调用未完成 code=%s %s detail=%s", code, shape, detail[:300])


def _sdk_error(
    code: str,
    *,
    retryable: bool,
    exc: Exception,
    status_code: int | None = None,
) -> ProviderError:
    return ProviderError(
        code,
        retryable=retryable,
        status_code=status_code,
        request_id=getattr(exc, "request_id", None),
    )


def _upstream_detail(exc: Exception) -> str:
    """上游返回的原始错误说明,仅用于服务端日志(绝不进 ProviderError / API / 对话)。"""
    response = getattr(exc, "response", None)
    text = getattr(response, "text", None) if response is not None else None
    if not text:
        body = getattr(exc, "body", None)
        text = json.dumps(body, ensure_ascii=False) if body is not None else str(exc)
    return str(text)[:800].replace("\n", " ")



def request_shape(
    op: str, model: str, messages: list[dict], tools: list[dict] | None = None
) -> str:
    """请求的结构指纹(不含正文):没有它,网关 400 只能靠猜。"""
    calls = sum(len(m.get("tool_calls") or []) for m in messages)
    results = sum(1 for m in messages if m.get("role") == "tool")
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return (
        f"op={op} model={model} msgs={len(messages)} tools={len(tools or [])} "
        f"tool_calls={calls} tool_results={results} content_chars={chars}"
    )


_UPSTREAM_FAILURE_HINTS = ("upstream_error", "upstream request failed", "bad gateway")


def _is_gateway_upstream_failure(detail: str) -> bool:
    """兼容网关会把自己的回源失败包装成 400,而 400 默认被判定为不可重试。

    这类"伪 400"实际是瞬时故障:不放行重试的话,网关抖一下就打穿整条降级链,
    用户侧直接看到任务失败(现场即为此)。请求真的非法时不会带这些标记。
    """
    lowered = detail.lower()
    return any(hint in lowered for hint in _UPSTREAM_FAILURE_HINTS)


def _sdk_status_error(exc: Exception, *, shape: str = "") -> ProviderError:
    status_code = _safe_status_code(getattr(exc, "status_code", None))
    is_rate_limit = status_code == 429 or getattr(exc, "type", None) == "rate_limit_error"
    detail = _upstream_detail(exc)
    # 对外仍然只有 code/status/request_id;原文与请求形态留在服务端日志里,
    # 否则这类 400 无法定位(线上只能看到 http_error;status=400)。
    logger.warning("上游拒绝请求 status=%s %s upstream=%s", status_code, shape, detail)
    return _sdk_error(
        "rate_limit" if is_rate_limit else "http_error",
        retryable=(
            is_rate_limit
            or bool(status_code and status_code >= 500)
            or _is_gateway_upstream_failure(detail)
        ),
        exc=exc,
        status_code=status_code,
    )


class EmptyOutputError(ProviderError):
    def __init__(self, provider: str):
        super().__init__("empty_output", retryable=True)


@dataclass(frozen=True)
class ProviderResult:
    parsed: BaseModel
    tokens_in: int
    tokens_out: int
    # 缓存计量(billing):tokens_in 口径为"非缓存新输入",双协议归一化见 _openai_usage/_anthropic_usage
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class ToolCallRequest:
    """模型请求的一次工具调用(双 SDK 归一化)。"""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolLoopResult:
    """generate_with_tools 的归一化返回:text / tool_calls / stop_reason / tokens。

    stop_reason ∈ {"end_turn", "tool_use", "max_tokens"}。
    thinking:模型思考过程(Anthropic thinking block / OpenAI reasoning summary),
    被动接收——provider 不主动开启,上游返回才有;仅用于展示,不回传模型。
    """

    text: str | None
    tool_calls: list[ToolCallRequest]
    stop_reason: str
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking: str | None = None
    # 模型内置联网搜索(provider 服务端执行,不经过本地工具循环)的过程与来源:
    # [{"query": str, "results": [{"title","url","published_at"}]}]。
    # 仅供展示与引用,不回传模型——模型已在自己的上下文里看过这些结果。
    web_searches: tuple[dict, ...] = ()
    # provider 侧实际发起的搜索次数(计量用;拿不到就按 web_searches 条数兜底)
    web_search_requests: int = 0


_BUILTIN_DOMAIN_CAP = 100  # 两家对域名列表都有长度限制,超了整个请求会被拒


def anthropic_web_search_tool(config: dict, version: str) -> dict:
    """构造 Anthropic 服务端搜索工具(server tool,不进本地工具循环)。

    blocked_domains 由自建搜索的来源黑名单映射而来——内置搜索拿不到我们的
    三级分级与权重重排,但黑名单是能透传的,别把这点治理能力也丢掉。
    allowed/blocked 两者互斥,同时给会被上游拒绝,因此 allowed 优先。
    """
    tool: dict = {"type": version, "name": "web_search"}
    max_uses = config.get("max_uses")
    if isinstance(max_uses, int) and max_uses > 0:
        tool["max_uses"] = max_uses
    allowed = [d for d in (config.get("allowed_domains") or []) if d][:_BUILTIN_DOMAIN_CAP]
    blocked = [d for d in (config.get("blocked_domains") or []) if d][:_BUILTIN_DOMAIN_CAP]
    if allowed:
        tool["allowed_domains"] = allowed
    elif blocked:
        tool["blocked_domains"] = blocked
    return tool


def parse_anthropic_web_searches(content: list) -> list[dict]:
    """从 content 块里还原内置搜索的过程:server_tool_use 出 query,结果块出来源。

    结果块的 content 在失败时是错误对象而非列表(如 max_uses_exceeded),
    因此逐层用 getattr 取值,拿不到就当空结果——绝不能让展示层的解析
    把一次正常回答变成异常。
    """
    queries: dict[str, str] = {}
    searches: list[dict] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", None) == "web_search":
            raw = getattr(block, "input", None) or {}
            queries[getattr(block, "id", "")] = str(raw.get("query") or "")
        elif btype == "web_search_tool_result":
            items = getattr(block, "content", None)
            results: list[dict] = []
            if isinstance(items, list):
                for item in items:
                    url = getattr(item, "url", None)
                    if not url:
                        continue
                    results.append(
                        {
                            "title": getattr(item, "title", None) or url,
                            "url": url,
                            # page_age 是"多久以前"的自然语言,不是日期,原样透传给前端
                            "page_age": getattr(item, "page_age", None),
                        }
                    )
            searches.append(
                {
                    "query": queries.get(getattr(block, "tool_use_id", ""), ""),
                    "results": results,
                }
            )
    return searches


def openai_web_search_tool(config: dict) -> dict:
    """构造 OpenAI Responses 内置搜索工具。

    与 Anthropic 的一处实质差异:Responses 的 filters **只支持 allowed_domains**,
    没有黑名单。因此我们的 deny 名单在这条路径上落不了地——只有配置了白名单
    才谈得上域名治理。这一点在运维文档里要写明,别以为选内置也自动挡住了。
    """
    tool: dict = {"type": "web_search"}
    allowed = [d for d in (config.get("allowed_domains") or []) if d][:_BUILTIN_DOMAIN_CAP]
    if allowed:
        tool["filters"] = {"allowed_domains": allowed}
    return tool


def parse_openai_web_searches(
    output: list,
    streamed_annotations: list | None = None,
    streamed_items: list | None = None,
) -> list[dict]:
    """从 Responses 的多路信号还原内置搜索:web_search_call 出 query,注解出来源。

    Responses 不像 Anthropic 那样把结果按检索式分组——引用统一挂在 message 的
    annotations 上,所以这里归并成一条记录,query 用分隔符拼接。

    引用会从三条通道到达,兼容网关裁哪条都有可能,因此全部取并集(按 URL 去重):
    1. 终值响应 output 里 message 的 annotations;
    2. 流式 response.output_text.annotation.added 事件(AI SDK 就是从这里产生
       source part 的,cherry-studio 能拿到引用而我们拿不到,差别曾在此);
    3. 流式 output_item.done 收到的 message —— 终值 output 非空但 annotations
       被裁空时,只有这份是全的,不能像取 output 那样二选一。
    """
    queries: list[str] = []
    results: list[dict] = []
    seen: set[str] = set()
    seen_calls: set[str] = set()
    used = False

    def collect(note: object) -> None:
        if getattr(note, "type", None) != "url_citation":
            return
        url = getattr(note, "url", None)
        if not url or url in seen:
            return
        seen.add(url)
        results.append(
            {
                "title": getattr(note, "title", None) or url,
                "url": url,
                "page_age": None,
            }
        )

    # 终值与流式 item 合并遍历:同一次调用可能两边都出现,按 item id 去重,
    # 没有 id 的退回按 (类型, 序号) 区分,避免把同一次搜索的 query 记两遍
    for index, item in enumerate(list(output or []) + list(streamed_items or [])):
        itype = getattr(item, "type", None)
        key = str(getattr(item, "id", None) or f"{itype}-{index}")
        if itype == "web_search_call":
            used = True
            if key in seen_calls:
                continue
            seen_calls.add(key)
            action = getattr(item, "action", None)
            query = getattr(action, "query", None) if action is not None else None
            if query:
                queries.append(str(query))
        elif itype == "message":
            for part in getattr(item, "content", None) or []:
                for note in getattr(part, "annotations", None) or []:
                    collect(note)
    for note in streamed_annotations or []:
        collect(note)
    if not used and not results:
        return []
    return [{"query": " | ".join(queries), "results": results}]


def _openai_usage(usage) -> tuple[int, int, int, int]:
    """归一化 (tokens_in, tokens_out, cache_read, cache_write)。

    OpenAI 的 input_tokens 含缓存命中部分(input_tokens_details.cached_tokens),
    这里扣除,使 tokens_in 统一为"非缓存新输入";OpenAI 自动缓存无创建计量,恒 0。
    网关不透传 details 时按无缓存处理。
    """
    if usage is None:
        return 0, 0, 0, 0
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    return max(usage.input_tokens - cached, 0), usage.output_tokens, cached, 0


def _anthropic_usage(usage) -> tuple[int, int, int, int]:
    """Anthropic 的 input_tokens 本就不含缓存读/写,三桶直取。"""
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return usage.input_tokens, usage.output_tokens, cache_read, cache_write


# ---- 多模态内容块(provider 无关,generate_structured 的消息 content 可为
# 纯文本 str 或块列表,由各 provider 归一化为自家 wire 格式):
# {"type": "text", "text": str}
# {"type": "image", "media_type": "image/jpeg"|"image/png"|..., "data": "<base64>"}


def to_openai_content(content: str | list[dict]) -> str | list[dict]:
    """内部内容块 → OpenAI Responses content(input_text / input_image data URL)。"""
    if isinstance(content, str):
        return content
    parts: list[dict] = []
    for block in content:
        if block["type"] == "image":
            parts.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{block['media_type']};base64,{block['data']}",
                }
            )
        else:
            parts.append({"type": "input_text", "text": block["text"]})
    return parts


def to_anthropic_content(content: str | list[dict]) -> str | list[dict]:
    """内部内容块 → Anthropic content(text / image base64 source)。"""
    if isinstance(content, str):
        return content
    parts: list[dict] = []
    for block in content:
        if block["type"] == "image":
            parts.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block["media_type"],
                        "data": block["data"],
                    },
                }
            )
        else:
            parts.append({"type": "text", "text": block["text"]})
    return parts


# ---- agent loop 内部消息格式(provider 无关,由各 provider 转 wire 格式):
# {"role": "user"|"assistant", "content": str}
# {"role": "assistant", "content": str|None, "tool_calls": [{"id","name","arguments":dict}]}
# {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# 工具定义格式:{"name": str, "description": str, "schema": <JSON Schema object>}
# schema 遵守双协议 strict 交集(additionalProperties:false、全字段 required,见 domain 注释)。


class LLMProvider(Protocol):
    name: str

    async def generate_structured(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        output_model: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult: ...

    async def generate_with_tools(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        timeout_seconds: float,
        on_delta: DeltaFn | None = None,
        on_thinking: DeltaFn | None = None,
        thinking_level: str | None = None,
        builtin_web_search: dict | None = None,
    ) -> ToolLoopResult: ...


def openai_strict_compatible(schema: object) -> bool:
    """OpenAI strict 模式要求所有 object 节点 additionalProperties 显式为 false
    且全部属性进 required;含自由对象(additionalProperties: true / 缺省)或可选
    属性的 schema(本地 patch 类工具、任意 MCP schema)必须降为非 strict,
    否则上游整个请求被拒(经网关表现为 502,而非单工具报错)。"""
    if isinstance(schema, dict):
        is_object = schema.get("type") == "object" or "properties" in schema
        if is_object:
            if schema.get("additionalProperties") is not False:
                return False
            props = schema.get("properties") or {}
            if set(schema.get("required") or []) != set(props):
                return False
        return all(openai_strict_compatible(v) for v in schema.values())
    if isinstance(schema, list):
        return all(openai_strict_compatible(v) for v in schema)
    return True


def structured_system(system: str, output_model: type[BaseModel]) -> str:
    """结构化任务的 system:schema 直接写进指令,双协议、原生/网关行为一致。"""
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    return (
        f"{system}\n\n输出必须是且仅是一个符合以下 JSON Schema 的 JSON 对象,"
        f"字段名必须与 schema 完全一致,不要输出任何其他文本:\n{schema}"
    )


def extract_json_object(text: str) -> object | None:
    """从模型文本里取出第一个完整 JSON 对象(容忍 ``` 包裹与前后说明文字)。"""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_structured_text[M: BaseModel](
    text: str, output_model: type[M], provider: str
) -> M:
    """正文 → JSON 对象 → pydantic 校验;失败一律归类为可重试(交给降级链)。"""
    payload = extract_json_object(text or "")
    if payload is None:
        raise EmptyOutputError(provider)
    try:
        return output_model.model_validate(payload)
    except ValidationError as exc:
        # 只记字段路径,不记模型输出内容
        fields = ",".join(
            ".".join(str(part) for part in err["loc"]) for err in exc.errors()[:5]
        )
        logger.warning(
            "结构化输出本地校验失败 provider=%s schema=%s fields=%s",
            provider,
            output_model.__name__,
            fields,
        )
        raise ProviderError("schema_error", retryable=True) from None


def _openai_text_from_items(items: list) -> str:
    """从 Responses output item 里拼回助手正文(message item 的 output_text 块)。"""
    parts: list[str] = []
    for item in items:
        if getattr(item, "type", None) != "message":
            continue
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text" and getattr(block, "text", None):
                parts.append(block.text)
    return "".join(parts)


def to_openai_input(messages: list[dict]) -> list[dict]:
    """内部消息 → Responses API input 项(function_call / function_call_output 平铺)。"""
    items: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m["tool_call_id"],
                    "output": m["content"],
                }
            )
            continue
        if m.get("content"):
            items.append({"role": m["role"], "content": m["content"]})
        for call in m.get("tool_calls") or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                }
            )
    return items


def to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """内部消息 → Anthropic messages(tool_result 必须紧跟在 assistant tool_use 之后的
    user 消息里,连续 tool 结果合并进同一条 user 消息)。"""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
            last = out[-1] if out else None
            if (
                last is not None
                and last["role"] == "user"
                and isinstance(last["content"], list)
            ):
                last["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m.get("tool_calls"):
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": call["arguments"],
                }
                for call in m["tool_calls"]
            )
            out.append({"role": "assistant", "content": blocks})
            continue
        content = m.get("content")
        # Anthropic rejects empty text blocks. Tool-bearing assistant messages
        # were handled above; historical orphan placeholders are discarded.
        if not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": m["role"], "content": content})
    return out


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        effort_map: dict[str, str] | None = None,
        send_max_output_tokens: bool = True,
    ):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        # 思考等级 → reasoning.effort 的改写表(settings 注入,未列出原样透传)
        self._effort_map = effort_map or {}
        # 该实例是否接受 max_output_tokens。默认发送(官方 API 语义不变);仅对
        # 明确配置的实例关闭 —— 某些聚合网关的上游是 ChatGPT/Codex 协议代理,
        # 不认这个参数,带上就整个请求 400(实测 16~32768 任何值都一样)。
        self._send_max_output_tokens = send_max_output_tokens

    def _max_output_tokens(self, max_tokens: int) -> int | openai.NotGiven:
        return max_tokens if self._send_max_output_tokens else openai.NOT_GIVEN

    async def generate_structured(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        output_model: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult:
        # 结构化输出唯一路径:schema 进指令 + 流式对话 + 本地校验。
        # 不用服务端解码端点(responses.parse/text_format):部署面向兼容网关,
        # 实测该类端点不可靠(约束被忽略/整体回源失败),本地校验行为处处一致。
        shape = request_shape("openai.responses.stream[structured]", model, messages)
        try:
            # 必须流式:结构化任务输出长(行程/报价可达数千 token,实测需 2 分钟以上),
            # 非流式请求在网关静默等待期内会被直接断连(见文件头决策)。
            async with self._client.responses.stream(
                model=model,
                instructions=structured_system(system, output_model),
                input=[
                    {"role": m["role"], "content": to_openai_content(m["content"])}
                    for m in messages
                ],
                max_output_tokens=self._max_output_tokens(max_tokens),
                timeout=timeout_seconds,
            ) as stream:
                streamed_items: list = []
                async for event in stream:
                    if event.type == "response.output_item.done":
                        streamed_items.append(event.item)
                response = await stream.get_final_response()
        except openai.APITimeoutError as exc:
            log_unfinished_call("timeout", shape, exc)
            raise _sdk_error("timeout", retryable=True, exc=exc) from None
        except openai.RateLimitError as exc:
            raise _sdk_error(
                "rate_limit",
                retryable=True,
                exc=exc,
                status_code=429,
            ) from None
        except openai.APIConnectionError as exc:
            log_unfinished_call("connection_error", shape, exc)
            raise _sdk_error("connection_error", retryable=True, exc=exc) from None
        except openai.APIStatusError as exc:
            raise _sdk_status_error(exc, shape=shape) from None
        except httpx.HTTPError as exc:  # 流已开始后断流,SDK 不包装
            raise transport_error(exc, shape) from None

        # 与 generate_with_tools 同口径:终值 output_text 为准,个别网关不回填时
        # 回退到流式期间 output_item.done 还原的正文。
        text = response.output_text or _openai_text_from_items(
            list(response.output or []) or streamed_items
        )
        parsed = parse_structured_text(text, output_model, self.name)
        tokens_in, tokens_out, cache_read, cache_write = _openai_usage(response.usage)
        return ProviderResult(
            parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    async def generate_with_tools(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        timeout_seconds: float,
        on_delta: DeltaFn | None = None,
        on_thinking: DeltaFn | None = None,
        thinking_level: str | None = None,
        builtin_web_search: dict | None = None,
    ) -> ToolLoopResult:
        request = dict(
            model=model,
            instructions=system,
            input=to_openai_input(messages),
            tools=[
                {
                    "type": "function",
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["schema"],
                    # strict 与非 strict 工具可混用;不兼容的(自由对象参数、
                    # 任意 MCP schema)单独降级,不拖垮整个请求
                    "strict": openai_strict_compatible(t["schema"]),
                }
                for t in tools
            ],
            max_output_tokens=self._max_output_tokens(max_tokens),
            timeout=timeout_seconds,
        )
        if builtin_web_search:
            # 内置搜索由 Responses 服务端执行,不回到本地工具循环
            request["tools"].append(openai_web_search_tool(builtin_web_search))
        reasoning = openai_reasoning_params(thinking_level, self._effort_map)
        if reasoning is not None:
            request["reasoning"] = reasoning
        try:
            streamed_thinking: list[str] = []
            # 流式过程中逐项收尾的 output item;仅在终值响应没带 output 时启用(见下)
            streamed_items: list = []
            # 引用注解同样走增量:网关裁剪 output 时,这是还原内置搜索来源的唯一途径
            streamed_annotations: list = []
            # 一律流式(见文件头设计裁决 2)。没有 on_delta 时也走流式:非流式
            # 请求在兼容网关的静默等待上限内出不来响应会被断连,且实测有网关的
            # 非流式响应体不符合 Response schema,SDK 会把它解析成 str,直到
            # `response.output` 才炸成 AttributeError —— 报错点离病因十万八千里。
            # KB 有据问答就是不带 on_delta 的调用方,曾因此 500。
            stream = await self._client.responses.create(**request, stream=True)
            response = None
            async for event in stream:
                if event.type == "response.output_text.delta":
                    if on_delta is not None:
                        await on_delta(event.delta)
                elif (
                    event.type == "response.reasoning_summary_text.delta"
                    and event.delta
                ):
                    streamed_thinking.append(event.delta)
                    if on_thinking is not None:
                        await on_thinking(event.delta)
                elif event.type == "response.output_item.done":
                    streamed_items.append(event.item)
                elif event.type == "response.output_text.annotation.added":
                    # 引用注解也走增量:网关裁剪终值 output 时,这里是唯一还原
                    # 来源的途径(同 thinking 的兜底思路)。没有它,内置搜索
                    # 就只剩"0 条来源"却带着结论,用户无从核实。
                    streamed_annotations.append(event.annotation)
                elif event.type in ("response.completed", "response.incomplete"):
                    response = event.response
                elif event.type == "response.failed":
                    raise ProviderError("stream_error", retryable=True)
            if response is None:
                raise ProviderError("stream_incomplete", retryable=True)
        except openai.APITimeoutError as exc:
            raise _sdk_error("timeout", retryable=True, exc=exc) from None
        except openai.RateLimitError as exc:
            raise _sdk_error(
                "rate_limit",
                retryable=True,
                exc=exc,
                status_code=429,
            ) from None
        except openai.APIConnectionError as exc:
            raise _sdk_error("connection_error", retryable=True, exc=exc) from None
        except openai.APIStatusError as exc:
            raise _sdk_status_error(
                exc, shape=request_shape("openai.responses.create", model, messages, tools)
            ) from None
        except httpx.HTTPError as exc:  # 流已开始后断流,SDK 不包装
            raise transport_error(
                exc, request_shape("openai.responses.create", model, messages, tools)
            ) from None

        # 终值响应的 output 为准;个别兼容网关的 completed 事件不回填它(实测恒为空),
        # 这时回退到流式期间 output_item.done 逐项收到的同一批 item。
        output_items = list(response.output or []) or streamed_items
        if not response.output and streamed_items:
            logger.warning(
                "终值响应未回填 output,改用流式 item 还原 model=%s items=%d",
                model,
                len(streamed_items),
            )
        tool_calls: list[ToolCallRequest] = []
        thinking_parts: list[str] = []
        for item in output_items:
            if item.type == "reasoning":
                # 被动接收 reasoning summary(上游/网关返回才有,不主动请求)
                for part in getattr(item, "summary", None) or []:
                    if getattr(part, "text", None):
                        thinking_parts.append(part.text)
                continue
            if item.type != "function_call":
                continue
            try:
                arguments = json.loads(item.arguments or "{}")
            except json.JSONDecodeError:
                raise ProviderError("invalid_tool_arguments", retryable=True) from None
            tool_calls.append(
                ToolCallRequest(id=item.call_id, name=item.name, arguments=arguments)
            )
        # output_text 由 SDK 从 output 聚合,output 为空时同样取不到 → 用同一份 items 还原
        text = response.output_text or _openai_text_from_items(output_items) or None
        if tool_calls:
            stop_reason = "tool_use"
        elif (
            response.incomplete_details is not None
            and response.incomplete_details.reason == "max_output_tokens"
        ):
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        tokens_in, tokens_out, cache_read, cache_write = _openai_usage(response.usage)
        web_searches = parse_openai_web_searches(
            output_items, streamed_annotations, streamed_items
        )
        if web_searches and not web_searches[0]["results"]:
            # 搜了却一条来源都没有:要么上游真的没搜到,要么网关把注解裁掉了。
            # 两者在前端都表现为"0 条来源",只有日志能区分,不记就无从排查。
            logger.warning(
                "内置搜索无来源 model=%s final=%s streamed=%s annotations=%d",
                model,
                [getattr(i, "type", "?") for i in output_items][:12],
                [getattr(i, "type", "?") for i in streamed_items][:12],
                len(streamed_annotations),
            )
        return ToolLoopResult(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            thinking="\n\n".join(thinking_parts) or "".join(streamed_thinking) or None,
            web_searches=tuple(web_searches),
            # Responses 不单独报搜索次数,按解析出的检索轮次兜底
            web_search_requests=len(web_searches),
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        thinking_budget: int = 0,
        budget_map: dict[str, int] | None = None,
    ):
        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url or None)
        # >0 时 generate_with_tools 主动开启 extended thinking(思考稳定输出);
        # 0 = 被动接收上游自主返回的 thinking block
        self._thinking_budget = thinking_budget
        # 思考等级 → budget_tokens 查表(settings 注入,未列出 = 不开启)
        self._budget_map = budget_map or {}

    async def generate_structured(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        output_model: type[T],
        max_tokens: int,
        timeout_seconds: float,
    ) -> ProviderResult:
        # 结构化输出唯一路径:schema 进 system + 流式对话 + 本地校验。
        # 不用服务端解码端点(messages.parse/output_format):部署面向兼容网关,
        # 实测该端点约束被忽略、且会无响应断连;本地校验在原生 API 与网关行为一致。
        shape = request_shape("anthropic.messages.stream[structured]", model, messages)
        try:
            # 必须流式:结构化任务输出长(行程/报价可达数千 token,实测需 2 分钟以上),
            # 非流式请求在网关静默等待期内会被直接断连(见文件头决策)。
            async with self._client.messages.stream(
                model=model,
                system=structured_system(system, output_model),
                messages=[
                    {"role": m["role"], "content": to_anthropic_content(m["content"])}
                    for m in messages
                ],
                max_tokens=max_tokens,
                timeout=timeout_seconds,
            ) as stream:
                response = await stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            log_unfinished_call("timeout", shape, exc)
            raise _sdk_error("timeout", retryable=True, exc=exc) from None
        except anthropic.RateLimitError as exc:
            raise _sdk_error(
                "rate_limit",
                retryable=True,
                exc=exc,
                status_code=429,
            ) from None
        except anthropic.APIConnectionError as exc:
            log_unfinished_call("connection_error", shape, exc)
            raise _sdk_error("connection_error", retryable=True, exc=exc) from None
        except anthropic.APIStatusError as exc:
            raise _sdk_status_error(exc, shape=shape) from None
        except httpx.HTTPError as exc:  # 流已开始后断流,SDK 不包装
            raise transport_error(exc, shape) from None

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        parsed = parse_structured_text(text, output_model, self.name)
        tokens_in, tokens_out, cache_read, cache_write = _anthropic_usage(response.usage)
        return ProviderResult(
            parsed=parsed,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

    async def generate_with_tools(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        timeout_seconds: float,
        on_delta: DeltaFn | None = None,
        on_thinking: DeltaFn | None = None,
        thinking_level: str | None = None,
        builtin_web_search: dict | None = None,
    ) -> ToolLoopResult:
        request = dict(
            model=model,
            system=system,
            messages=to_anthropic_messages(messages),
            tools=[
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["schema"],
                }
                for t in tools
            ],
            max_tokens=max_tokens,
            timeout=timeout_seconds,
        )
        if builtin_web_search:
            # server tool 与普通 function tool 同列在 tools 里,但由 provider 服务端
            # 执行,不会回到本地工具循环
            request["tools"].append(
                anthropic_web_search_tool(
                    builtin_web_search,
                    builtin_web_search.get("tool_version") or "web_search_20250305",
                )
            )
        budget = anthropic_thinking_budget(
            thinking_level, max_tokens, self._thinking_budget, self._budget_map
        )
        if budget > 0:
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
        try:
            streamed_thinking: list[str] = []
            if on_delta is None:
                response = await self._client.messages.create(**request)
            else:
                # 原始事件流:text_delta 走正文通道,thinking_delta 走思考通道
                # (上游默认返回 thinking block 时前端可实时看到思考过程)
                async with self._client.messages.stream(**request) as stream:
                    async for event in stream:
                        if event.type != "content_block_delta":
                            continue
                        delta_type = getattr(event.delta, "type", None)
                        if delta_type == "text_delta":
                            await on_delta(event.delta.text)
                        elif delta_type == "thinking_delta" and event.delta.thinking:
                            # 累积兜底:部分网关只给 delta、终值消息缺 thinking block
                            streamed_thinking.append(event.delta.thinking)
                            if on_thinking is not None:
                                await on_thinking(event.delta.thinking)
                    response = await stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            raise _sdk_error("timeout", retryable=True, exc=exc) from None
        except anthropic.RateLimitError as exc:
            raise _sdk_error(
                "rate_limit",
                retryable=True,
                exc=exc,
                status_code=429,
            ) from None
        except anthropic.APIConnectionError as exc:
            raise _sdk_error("connection_error", retryable=True, exc=exc) from None
        except anthropic.APIStatusError as exc:
            raise _sdk_status_error(
                exc, shape=request_shape("anthropic.messages.create", model, messages, tools)
            ) from None
        except httpx.HTTPError as exc:  # 流已开始后断流,SDK 不包装
            raise transport_error(
                exc, request_shape("anthropic.messages.create", model, messages, tools)
            ) from None

        texts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "thinking":
                # 被动接收(不主动开 extended thinking);redacted_thinking 无明文,跳过
                if getattr(block, "thinking", None):
                    thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallRequest(id=block.id, name=block.name, arguments=dict(block.input))
                )
        if response.stop_reason == "tool_use" or tool_calls:
            stop_reason = "tool_use"
        elif response.stop_reason == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"
        tokens_in, tokens_out, cache_read, cache_write = _anthropic_usage(response.usage)
        web_searches = parse_anthropic_web_searches(response.content)
        server_usage = getattr(response.usage, "server_tool_use", None)
        requests = getattr(server_usage, "web_search_requests", None)
        return ToolLoopResult(
            text="\n".join(texts) or None,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            thinking="\n\n".join(thinking_parts) or "".join(streamed_thinking) or None,
            web_searches=tuple(web_searches),
            web_search_requests=(requests if isinstance(requests, int) else len(web_searches)),
        )
