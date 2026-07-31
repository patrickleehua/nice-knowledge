"""TraceSink / UsageSink 协议(MIGRATION-PLAN §4):LLM 调用落账的扩展点。

TF 的 llm/service.py:501 `_record` 直落 LlmTrace + UsageDaily;SDK 化后把
"往哪记、记什么"抽成协议,宿主可替换为自己的观测/计费管道(OTLP、
ClickHouse 等),默认实现仍落 SQL 表保持行为不变。

会话与事务边界:两个 sink 都使用调用方传入的 session、不 commit——
LLMService._record 负责建会话、绑定 org 上下文(RLS)与最终 commit,
trace 与 usage 同事务落库(与 TF 行为一致)。
"""

import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nicekit.models.llm import LlmTrace

logger = logging.getLogger(__name__)


class TraceSink(Protocol):
    """单次 provider 调用的 trace 落账(成功与失败每跳各一条)。"""

    async def record_trace(
        self, session: AsyncSession, trace_payload: dict
    ) -> UUID | None:
        """记录一条调用 trace,返回 trace id(无持久化 id 的实现可返回 None)。

        trace_payload 字段与 LlmTrace 列一致:org_id/task/provider/model/
        prompt_version/attempt/fallback_from/status/error/tokens_in/tokens_out/
        latency_ms/cache_read_tokens/cache_write_tokens。
        """
        ...


class UsageSink(Protocol):
    """四桶计量(tokens_in/out + cache read/write)按日聚合落账,仅成功调用。"""

    async def record_usage(self, session: AsyncSession, **usage_fields) -> None:
        """usage_fields 与 usage_upsert_stmt 签名一致:org_id/task/provider/
        model/calls/tokens_in/tokens_out/cache_read_tokens/cache_write_tokens/quantity。
        """
        ...


class SqlTraceSink:
    """默认实现:落 llm_traces 表(逻辑取自 TF llm/service.py `_record`)。"""

    async def record_trace(
        self, session: AsyncSession, trace_payload: dict
    ) -> UUID | None:
        trace = LlmTrace(**trace_payload)
        session.add(trace)
        # id 由列级 default=uuid4 在 flush 时生成,flush 后即可返回给调用方
        await session.flush()
        return trace.id


class SqlUsageSink:
    """默认实现:usage_daily 四桶 upsert(全库唯一 upsert 写法在 tenancy.usage)。

    延迟 import:tenancy 子包与 llm 子包平级,llm 不在包级依赖它——
    仅当宿主真的用 SQL 计量(默认)时才需要 tenancy.usage 存在。
    """

    async def record_usage(self, session: AsyncSession, **usage_fields) -> None:
        from nicekit.tenancy.usage import usage_upsert_stmt

        await session.execute(usage_upsert_stmt(**usage_fields))
