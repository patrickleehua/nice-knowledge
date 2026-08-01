"""聊天工作台 API:会话 CRUD + 发消息(SSE 流 / 聚合 JSON 双模)+ 队列 / 目标 / 重放。

搬自 TF backend/app/api/v1/chat.py。SDK 化改造(MIGRATION-PLAN §5.4):
- 会话与宿主业务对象的联系从 `project_id`/`customer_id` 两个硬 FK 泛化为
  `scope_type` + `scope_id`(无 FK);`origin_mode` 取值 `general`/`scoped`。
  列表过滤参数同步改成 `scope_type`/`scope_id`,`scope=general` 语义保留。
- 切换会话绑定的作用域(TF 的 `context.project_id`)不再校验业务表是否存在——
  SDK 不认识宿主的表,合法性由宿主的 ResourceResolver 在工具调用时把关。
- 工具目录从模块级 TOOLS dict 换成 `ToolRegistry`(默认 `default_registry`)。
- `ctx.role` 是字符串(内置三角色或宿主注册角色)。

org 内全员可用;工具内部的角色约束按 JWT role 生效。
"""

import json
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent import run_inputs, session_goal
from nicekit.agent.loop import public_pending_confirmation
from nicekit.agent.permissions import (
    ReviewerOverrideError,
    consume_reviewer_override,
    is_approval_bundle,
)
from nicekit.agent.permissions.management import new_session_permission_defaults
from nicekit.agent.permissions.policy import (
    load_effective_policy,
    policy_snapshot_payload,
)
from nicekit.agent.runtime_tools import available_runtime_tools
from nicekit.agent.service import resolve_card, run_turn_events
from nicekit.agent.tools import default_registry
from nicekit.agent.user_input import (
    UserInputValidationError,
    normalize_answers,
    public_user_input_request,
)
from nicekit.api.deps import OrgContext, get_org_context, get_org_session
from nicekit.capabilities.websearch.service import resolve_websearch_readiness
from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory
from nicekit.domain.agent_plan import PlanContinuationError, continue_failed_plan_step
from nicekit.llm.registry import resolve_route
from nicekit.llm.service import env_provider_credentials
from nicekit.models.chat import (
    AgentCard,
    AgentCardVersion,
    ChatEvent,
    ChatMessage,
    ChatRunInput,
    ChatSession,
    ChatSessionOriginMode,
)
from nicekit.models.llm_provider import LlmProvider

router = APIRouter()

Session = Annotated[AsyncSession, Depends(get_org_session)]
Ctx = Annotated[OrgContext, Depends(get_org_context)]

# scope_type 的落库长度上限(见 models/chat.py)
_SCOPE_TYPE_MAX = 50


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_card_id: UUID | None = None
    # 绑定宿主业务作用域:两者要么同时给,要么都不给(不给 = general 会话)
    scope_type: str | None = Field(default=None, max_length=_SCOPE_TYPE_MAX)
    scope_id: UUID | None = None
    title: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (self.scope_type is None) != (self.scope_id is None):
            raise ValueError("scope_type 与 scope_id 必须同时提供")
        return self


class ApprovalItemDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "deny"]
    scope: Literal["once", "session_tool"] = "once"
    note: str | None = Field(default=None, max_length=500)


class ConfirmIn(BaseModel):
    """Legacy exact boolean decision or a new per-item approval bundle decision."""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str | None = Field(default=None, min_length=1, max_length=100)
    approved: bool | None = None
    bundle_id: UUID | None = None
    decisions: list[ApprovalItemDecisionIn] | None = Field(
        default=None, min_length=1, max_length=50
    )

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        legacy = self.tool_call_id is not None or self.approved is not None
        bundled = self.bundle_id is not None or self.decisions is not None
        if legacy == bundled:
            raise ValueError("confirm 必须且只能使用 legacy 或 bundle 决策格式")
        if legacy and (self.tool_call_id is None or self.approved is None):
            raise ValueError("legacy confirm 需要 tool_call_id 和 approved")
        if bundled:
            if self.bundle_id is None or not self.decisions:
                raise ValueError("bundle confirm 需要 bundle_id 和 decisions")
            call_ids = [item.tool_call_id for item in self.decisions]
            if len(call_ids) != len(set(call_ids)):
                raise ValueError("decisions.tool_call_id 不可重复")
        return self


class ReviewerOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    action_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class UserInputAnswerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1, max_length=64)
    selected: list[str] = Field(default_factory=list, max_length=4)
    other: str | None = Field(default=None, max_length=1000)


class UserInputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    answers: list[UserInputAnswerIn] = Field(min_length=1, max_length=4)


class ContinuePlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=100)


class RuntimeModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=100)


class MessageContextIn(BaseModel):
    """切换会话当前绑定的宿主作用域(TF 的 context.project_id 泛化)。"""

    model_config = ConfigDict(extra="forbid")

    scope_type: str | None = Field(default=None, max_length=_SCOPE_TYPE_MAX)
    scope_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if (self.scope_type is None) != (self.scope_id is None):
            raise ValueError("scope_type 与 scope_id 必须同时提供")
        return self


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    context: MessageContextIn | None = None
    # 对 tool.confirm 截停的答复;可与 content 并存(批准/拒绝并补充说明)
    confirm: ConfirmIn | None = None
    override: ReviewerOverrideIn | None = None
    user_input: UserInputIn | None = None
    continue_plan: ContinuePlanIn | None = None
    # 思考等级(合法值 = settings.llm_thinking_levels,发消息时校验);
    # None = provider 默认行为
    thinking: str | None = None
    # None = 使用 agent model_task 的自动路由与降级链；显式选择只运行该模型
    model: RuntimeModelIn | None = None
    # 本轮联网方式:off=不向模型暴露联网工具;provider id=只用这家;
    # None = 跟随管理端配置(auto 故障转移链)
    web_search: str | None = None
    # 本轮临时开启的工具(见 agent/runtime_tools.py):不写 agent 卡白名单,
    # 只对这一次发送(或这条排队输入)生效。这里不校验工具是否存在/是否只读——
    # 判定与拒绝原因由 resolve_overrides 统一给出并进事件流与审计快照,
    # 报 422 反而会把"这个工具不能临时开"这条信息藏起来。
    runtime_tools: list[str] | None = Field(default=None, max_length=20)

    @field_validator("runtime_tools")
    @classmethod
    def normalize_runtime_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        names: list[str] = []
        for raw in value:
            name = raw.strip()
            if len(name) > 100:
                raise ValueError("runtime_tools 项过长")
            if name and name not in names:
                names.append(name)
        return names

    @model_validator(mode="after")
    def validate_decision_shape(self) -> Self:
        actions = [
            self.confirm,
            self.override,
            self.user_input,
            self.continue_plan,
        ]
        if sum(action is not None for action in actions) > 1:
            raise ValueError("每次只能提交一种交互操作")
        if (self.user_input is not None or self.continue_plan is not None) and (
            self.content.strip()
        ):
            raise ValueError("结构化交互操作不可同时提交普通消息")
        return self


class RuntimeModelOptionOut(BaseModel):
    provider: str
    model: str
    is_default: bool


class RuntimeModelsOut(BaseModel):
    default: RuntimeModelIn
    models: list[RuntimeModelOptionOut]


class SessionOut(BaseModel):
    id: UUID
    agent_card_id: UUID
    scope_type: str | None
    scope_id: UUID | None
    origin_mode: ChatSessionOriginMode
    title: str
    status: str
    # 待批的高危工具调用(重进会话时前端据此恢复确认卡片)
    pending_confirmation: dict | None = None
    # 待答结构化澄清请求(重进会话时恢复问题卡片)
    pending_user_input: dict | None = None
    # agent 执行计划(重进会话时前端据此恢复计划面板)
    plan: list | None = None
    created_at: datetime | None
    updated_at: datetime | None

    @field_validator("pending_confirmation", mode="before")
    @classmethod
    def redact_pending_confirmation(cls, value: dict | None) -> dict | None:
        return public_pending_confirmation(value)

    @field_validator("pending_user_input", mode="before")
    @classmethod
    def project_pending_user_input(cls, value: dict | None) -> dict | None:
        return public_user_input_request(value)


class QueuedInputOut(BaseModel):
    """排队中的运行输入(status=queued 可编辑/删除/重排,终态只读)。"""

    id: UUID
    run_id: UUID
    position: int
    status: str
    content: str
    # 这条输入自带的"本轮临时工具";消费它时才解析放行,不改 agent 配置
    runtime_tools: list[str] = Field(default_factory=list)
    consumed_message_id: UUID | None
    created_at: datetime | None
    updated_at: datetime | None


class QueuedInputPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=20000)

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content 不能为空")
        return value


class QueuedInputOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[UUID] = Field(min_length=1, max_length=100)


class SessionGoalIn(BaseModel):
    """设定会话目标。token_budget 省略时用后端默认(见 session_goal 模块常量)。"""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    token_budget: int | None = Field(default=None, ge=1000, le=5_000_000)
    max_auto_runs: int | None = Field(default=None, ge=1, le=50)

    @field_validator("objective")
    @classmethod
    def objective_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("objective 不能为空")
        return value.strip()


class SessionGoalOut(BaseModel):
    id: UUID
    session_id: UUID
    objective: str
    status: str
    token_budget: int
    tokens_used: int
    auto_runs: int
    max_auto_runs: int
    summary: str | None
    created_at: datetime | None
    updated_at: datetime | None


class SessionGoalDetailOut(BaseModel):
    """没有目标是常态(大多数会话不设目标),因此用可空包装而不是 404。"""

    goal: SessionGoalOut | None


class SessionGoalCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, max_length=2000)


class MessageOut(BaseModel):
    id: UUID
    sequence: int
    role: str
    content: str | None
    tool_name: str | None
    tool_call_id: str | None
    run_id: UUID | None
    tool_input: dict | None
    tool_output: dict | None
    trace_id: UUID | None
    created_at: datetime | None


async def _get_session_or_404(
    session: AsyncSession, session_id: UUID, user_id: UUID
) -> ChatSession:
    """会话按属主隔离:非本人会话一律 404(不泄露存在性)。"""
    chat = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if chat is None or chat.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return chat


# 档位内置中文名(settings.llm_thinking_level_labels 可覆盖,再缺省用原值)
_THINKING_LABEL_DEFAULTS = {
    "off": "关闭",
    "none": "关闭",
    "minimal": "最简",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
}


async def _runtime_models_for_task(
    session: AsyncSession, org_id: UUID, model_task: str
) -> RuntimeModelsOut:
    """Return only provider/model pairs that the current runtime can construct.

    Only the selected Agent task's active primary/fallback entries are selectable.
    This preserves admin routing policy and avoids exposing unrelated embedding,
    image, or speech models from a provider inventory. The provider must also be
    enabled and have credentials that the runtime can use.
    """
    route = await resolve_route(session, model_task, org_id)
    provider_rows = (await session.execute(select(LlmProvider))).scalars().all()
    env_credentials = env_provider_credentials()
    usable_providers: dict[str, LlmProvider] = {}
    for row in provider_rows:
        if not row.enabled:
            continue
        env = env_credentials.get(row.name)
        if row.api_key or (env is not None and env[1]):
            usable_providers[row.name] = row

    default = RuntimeModelIn(
        provider=route.primary_provider,
        model=route.primary_model,
    )
    options: dict[tuple[str, str], RuntimeModelOptionOut] = {}

    def add(provider: str, model: str) -> None:
        if provider not in usable_providers or not model:
            return
        key = (provider, model)
        options[key] = RuntimeModelOptionOut(
            provider=provider,
            model=model,
            is_default=(provider == default.provider and model == default.model),
        )

    for hop in [
        {"provider": route.primary_provider, "model": route.primary_model},
        *route.fallback_chain,
    ]:
        add(str(hop.get("provider") or ""), str(hop.get("model") or ""))

    ordered = sorted(
        options.values(),
        key=lambda item: (
            not item.is_default,
            item.provider.casefold(),
            item.model.casefold(),
        ),
    )
    return RuntimeModelsOut(default=default, models=ordered)


@router.get("/chat/thinking-levels")
async def thinking_levels() -> dict:
    """思考等级档位表(settings 驱动,前端下拉按此渲染)。"""
    settings = get_settings()
    return {
        "levels": [
            {
                "value": level,
                "label": settings.llm_thinking_level_labels.get(level)
                or _THINKING_LABEL_DEFAULTS.get(level, level),
            }
            for level in settings.llm_thinking_levels
        ],
        "default": settings.llm_thinking_default_level,
    }


@router.get("/chat/websearch-options")
async def websearch_options(session: Session, ctx: Ctx) -> dict:
    """联网方式可选项(输入框面板按此渲染)。

    可用性由管理端「外部服务」配置决定,前端不许硬编码服务商列表——否则新增
    一家搜索源要改两处,而且用户会看到一个配了 key 却选不了的选项。
    """
    readiness = await resolve_websearch_readiness(session)
    return {
        "options": [
            {"value": o.value, "label": o.label, "ready": o.ready, "hint": o.hint}
            for o in readiness.options
        ],
        # None = 跟随管理端配置(auto 故障转移链);前端据此显示"默认"态
        "default_provider": readiness.default_provider,
        "ready": readiness.ready,
    }


@router.get("/chat/runtime-tools")
async def list_runtime_tools(
    session: Session,
    ctx: Ctx,
    agent_card_id: UUID | None = None,
) -> dict:
    """当前 agent 卡可"本轮临时开启"的工具候选(输入框工具选择器按此渲染)。

    候选只含只读工具且不在卡白名单里——写工具与需确认的工具永远不出现在这里,
    临时开关不是权限提升通道(硬判定在 runtime_tools.resolve_overrides,
    这里只是同一规则的展示面)。取卡方式与 /chat/models 同构:前端选了哪张卡
    就问哪张卡,免得为一个下拉再造一条"按会话取卡"的路径。
    """
    try:
        card = await resolve_card(session, ctx.org_id, agent_card_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {
        "tools": available_runtime_tools(card.tools, default_registry.as_dict()),
        # 提示语由后端给,保证"仅本轮生效"的口径在各端一致
        "notice": "仅本轮生效，不会改变 Agent 配置",
    }


@router.get("/chat/models", response_model=RuntimeModelsOut)
async def list_runtime_models(
    session: Session,
    ctx: Ctx,
    agent_card_id: UUID | None = None,
) -> RuntimeModelsOut:
    """Models that can authoritatively override the selected Agent for one turn."""
    try:
        card = await resolve_card(session, ctx.org_id, agent_card_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        return await _runtime_models_for_task(session, ctx.org_id, card.model_task)
    except LookupError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc


@router.get("/chat/agent-cards")
async def list_agent_cards(session: Session, ctx: Ctx) -> list[dict]:
    """可用 agent 卡(本 org 自定义 + 平台默认;RLS 已限定可见范围)。

    委派专员卡(delegatable)不进这个列表:它们只被主 agent 通过 Agent 工具调用,
    不直接面对用户,出现在会话选择器里只会让人误开一个没有全流程能力的对话。
    """
    rows = (
        await session.execute(
            select(AgentCard, AgentCardVersion)
            .join(
                AgentCardVersion,
                (AgentCardVersion.card_id == AgentCard.id)
                & AgentCardVersion.is_active
                & (AgentCardVersion.status == "active"),
            )
            .where(AgentCard.is_active, AgentCard.delegatable.is_(False))  # type: ignore[union-attr]
            .order_by(AgentCard.sort_order, AgentCard.created_at)
        )
    ).all()
    return [
        {
            "id": str(card.id),
            "name": card.name,
            "description": card.description,
            "icon": card.icon,
            "active_version": version.version,
            "tools": version.tools,
            "max_turns": version.max_turns,
            "is_platform": card.org_id != ctx.org_id,
        }
        for card, version in rows
    ]


@router.post("/chat/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, session: Session, ctx: Ctx) -> SessionOut:
    """新建会话。绑定作用域(scope_type + scope_id)是创建事实,决定 origin_mode。

    SDK 不校验 scope_id 指向的宿主对象是否存在:它没有那张表。真正的越权拦截
    发生在工具调用时——ResourceResolver 解析不出资源,权限引擎直接拒。
    """
    try:
        card = await resolve_card(session, ctx.org_id, body.agent_card_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    profile, permission_scope, custom_rules, _policy = (
        await new_session_permission_defaults(
            session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            scope_id=body.scope_id,
        )
    )
    chat = ChatSession(
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        agent_card_id=card.card_id,
        scope_type=body.scope_type,
        scope_id=body.scope_id,
        origin_mode=(
            ChatSessionOriginMode.SCOPED
            if body.scope_id is not None
            else ChatSessionOriginMode.GENERAL
        ),
        title=body.title or "新对话",
        permission_profile=profile,
        permission_scope=permission_scope,
        permission_custom_rules=custom_rules,
    )
    session.add(chat)
    await session.flush()
    effective = await load_effective_policy(
        session,
        org_id=ctx.org_id,
        user_id=ctx.user_id,
        chat_session=chat,
    )
    chat.permission_policy_id = effective.policy_id
    chat.permission_policy_version = effective.policy_version
    chat.permission_policy_snapshot = policy_snapshot_payload(effective)
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return SessionOut.model_validate(chat, from_attributes=True)


@router.get("/chat/sessions")
async def list_sessions(
    session: Session,
    ctx: Ctx,
    page: int = 1,
    page_size: int = 20,
    scope_type: str | None = None,
    scope_id: UUID | None = None,
    scope: str | None = None,
) -> dict:
    """会话列表。来源模式固定,scope_type/scope_id 表示当前绑定的宿主作用域。

    `scope=general` 只列未绑定作用域的会话;`scope_id`(可选配 `scope_type`)
    只列绑到该作用域的会话。两者同时给时以 scope_id 为准(更具体的条件优先)。
    """
    query = select(ChatSession).where(ChatSession.user_id == ctx.user_id)
    if scope_id is not None:
        query = query.where(
            ChatSession.origin_mode == ChatSessionOriginMode.SCOPED,
            ChatSession.scope_id == scope_id,
        )
        if scope_type is not None:
            query = query.where(ChatSession.scope_type == scope_type)
    elif scope == "general":
        query = query.where(
            ChatSession.origin_mode == ChatSessionOriginMode.GENERAL
        )
    elif scope_type is not None:
        query = query.where(ChatSession.scope_type == scope_type)
    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                query.order_by(ChatSession.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [SessionOut.model_validate(r, from_attributes=True) for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@router.delete("/chat/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session_id: UUID, session: Session, ctx: Ctx) -> None:
    """删除会话及其消息与事件。有 run 进行中时 409。"""
    chat = (
        await session.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if chat.active_run_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent_busy")
    # chat_messages 外键无级联(chat_events 有),显式清理
    await session.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    await session.delete(chat)
    await session.commit()


@router.get("/chat/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: UUID, session: Session, ctx: Ctx) -> list[MessageOut]:
    await _get_session_or_404(session, session_id, ctx.user_id)
    rows = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.sequence)
            )
        )
        .scalars()
        .all()
    )
    return [MessageOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/chat/sessions/{session_id}/messages")
async def send_message(
    session_id: UUID,
    body: MessageIn,
    session: Session,
    ctx: Ctx,
    stream: bool = True,
):
    """发消息并跑一轮 agent。默认 SSE 流(text/event-stream,data 为事件 JSON);
    ?stream=false 时等待完成后返回聚合 JSON {events, reply, stop}。
    body.confirm 用于答复 tool.confirm 截停(此时 content 可为空)。"""
    if (
        not body.content.strip()
        and body.confirm is None
        and body.override is None
        and body.user_input is None
        and body.continue_plan is None
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "content 不能为空")
    if (
        body.thinking is not None
        and body.thinking not in get_settings().llm_thinking_levels
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "thinking 档位非法")
    if body.web_search is not None:
        # 只校验取值合法,不校验是否可用:配置可能在用户选完之后被管理端改掉,
        # 那种情况按"不可用就当没开"处理(见 _build_tool_snapshot),不该报错打断对话
        readiness = await resolve_websearch_readiness(session)
        if body.web_search not in {o.value for o in readiness.options}:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "web_search 取值非法"
            )
    chat = (
        await session.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if chat.active_run_id is not None:
        # run 正在跑:普通消息不再 409,入队等待 loop 在安全检查点消费。
        # confirm/override 决策仍 409——待批确认只存在于"run 已停"的会话上,
        # active run 意味着没有可答复的截停,排队决策没有意义。
        # (pending_confirmation 存在但 run 未跑时不走这里,保持原 superseded 语义。)
        if (
            body.confirm is not None
            or body.override is not None
            or body.user_input is not None
            or body.continue_plan is not None
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "agent_busy")
        item = await run_inputs.enqueue_input(
            session,
            chat=chat,
            run_id=chat.active_run_id,
            user_id=ctx.user_id,
            content=body.content,
            runtime_tools=body.runtime_tools,
        )
        await session.commit()
        # 无活跃 SSE emit 通道,input.queued 直接落 chat_events(best-effort)
        await run_inputs.record_queue_event(
            get_session_factory(),
            org_id=ctx.org_id,
            payload=run_inputs.queue_event_payload("input.queued", item),
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "queued": True,
                "queued_input_id": str(item.id),
                "run_id": str(item.run_id),
                "position": item.position,
            },
        )
    if body.model is not None:
        try:
            card = await resolve_card(session, ctx.org_id, chat.agent_card_id)
            catalog = await _runtime_models_for_task(
                session, ctx.org_id, card.model_task
            )
        except LookupError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        if not any(
            item.provider == body.model.provider and item.model == body.model.model
            for item in catalog.models
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "所选模型不在当前 Agent 的可用目录中",
            )
    pending_user_input = chat.pending_user_input or {}
    if pending_user_input:
        if body.user_input is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "pending_user_input_requires_answer",
            )
        if str(body.user_input.request_id) != str(
            pending_user_input.get("request_id")
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "no_pending_user_input",
            )
        try:
            normalize_answers(
                pending_user_input,
                body.user_input.model_dump(mode="json"),
            )
        except UserInputValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                str(exc),
            ) from exc
    elif body.user_input is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no_pending_user_input")
    if body.confirm is not None:
        pending = chat.pending_confirmation or {}
        if is_approval_bundle(pending):
            pending_items = {
                str(item.get("tool_call_id")): item
                for item in pending["items"]
                if item.get("status") == "pending"
            }
            if body.confirm.bundle_id is not None:
                decision_ids = {
                    item.tool_call_id for item in body.confirm.decisions or []
                }
                valid = (
                    str(body.confirm.bundle_id) == str(pending.get("bundle_id"))
                    and decision_ids
                    and decision_ids.issubset(pending_items)
                )
            else:
                valid = bool(
                    body.confirm.tool_call_id
                    and body.confirm.tool_call_id in pending_items
                )
            if not valid:
                raise HTTPException(status.HTTP_409_CONFLICT, "no_pending_confirmation")
        elif (
            body.confirm.tool_call_id is None
            or pending.get("tool_call_id") != body.confirm.tool_call_id
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "no_pending_confirmation")
    if body.override is not None:
        if chat.pending_confirmation or chat.pending_user_input:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "pending_interaction_requires_decision",
            )
        try:
            consume_reviewer_override(
                chat.reviewer_override_candidates or [],
                body.override.model_dump(mode="json"),
            )
        except ReviewerOverrideError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "no_reviewer_override",
            ) from exc
    if body.continue_plan is not None:
        if chat.pending_confirmation or chat.pending_user_input:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "pending_interaction_requires_decision",
            )
        try:
            continue_failed_plan_step(
                chat.plan,
                step_id=body.continue_plan.step_id,
            )
        except PlanContinuationError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "plan_step_not_continuable",
            ) from exc
    if body.context is not None and body.context.scope_id is not None:
        # 切换当前工作作用域。origin_mode 是创建事实,永不回写(见 models/chat.py)。
        chat.scope_type = body.context.scope_type
        chat.scope_id = body.context.scope_id
    run_id = uuid4()
    chat.active_run_id = run_id
    chat.cancel_requested = False
    session.add(chat)
    await session.commit()

    events = run_turn_events(
        get_session_factory(),
        org_id=ctx.org_id, user_id=ctx.user_id, role=ctx.role,
        session_id=session_id, run_id=run_id, user_text=body.content,
        confirm_action=(
            body.confirm.model_dump(exclude_none=True) if body.confirm else None
        ),
        override_action=(
            body.override.model_dump(mode="json") if body.override else None
        ),
        user_input_action=(
            body.user_input.model_dump(mode="json") if body.user_input else None
        ),
        continue_plan_action=(
            body.continue_plan.model_dump() if body.continue_plan else None
        ),
        thinking_level=body.thinking,
        model_override=body.model.model_dump() if body.model else None,
        web_search_choice=body.web_search,
        runtime_tool_requests=body.runtime_tools,
    )

    if not stream:
        collected: list[dict] = []
        async for ev in events:
            collected.append(ev)
        reply = next(
            (e["text"] for e in reversed(collected) if e["type"] == "text"), None
        )
        done = next((e for e in reversed(collected) if e["type"] == "turn.done"), {})
        return {"events": collected, "reply": reply, "stop": done.get("reason")}

    async def sse() -> object:
        async for ev in events:
            yield f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Agent-Run-Id": str(run_id),
        },
    )


@router.get("/chat/sessions/{session_id}/queued-inputs")
async def list_queued_inputs(session_id: UUID, session: Session, ctx: Ctx) -> dict:
    """会话当前排队中的运行输入(按 position 升序)。"""
    await _get_session_or_404(session, session_id, ctx.user_id)
    items = await run_inputs.list_queued_inputs(session, session_id)
    return {
        "items": [
            QueuedInputOut.model_validate(item, from_attributes=True) for item in items
        ]
    }


async def _get_queued_input_or_404(
    session: AsyncSession, session_id: UUID, input_id: UUID
) -> ChatRunInput:
    item = await run_inputs.get_input(session, session_id, input_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "排队输入不存在")
    return item


@router.patch(
    "/chat/sessions/{session_id}/queued-inputs/{input_id}",
    response_model=QueuedInputOut,
)
async def update_queued_input(
    session_id: UUID,
    input_id: UUID,
    body: QueuedInputPatch,
    session: Session,
    ctx: Ctx,
) -> QueuedInputOut:
    """编辑排队中的输入内容;已消费/已跳过的终态项拒绝(409)。"""
    await _get_session_or_404(session, session_id, ctx.user_id)
    item = await _get_queued_input_or_404(session, session_id, input_id)
    if not run_inputs.is_editable(item):
        raise HTTPException(status.HTTP_409_CONFLICT, "input_not_queued")
    item.content = body.content
    session.add(item)
    await session.commit()
    await run_inputs.record_queue_event(
        get_session_factory(),
        org_id=ctx.org_id,
        payload=run_inputs.queue_event_payload("input.updated", item),
    )
    return QueuedInputOut.model_validate(item, from_attributes=True)


@router.delete(
    "/chat/sessions/{session_id}/queued-inputs/{input_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_queued_input(
    session_id: UUID, input_id: UUID, session: Session, ctx: Ctx
) -> None:
    """删除排队中的输入;已消费/已跳过的终态项拒绝(409)。"""
    await _get_session_or_404(session, session_id, ctx.user_id)
    item = await _get_queued_input_or_404(session, session_id, input_id)
    if not run_inputs.is_editable(item):
        raise HTTPException(status.HTTP_409_CONFLICT, "input_not_queued")
    deleted_payload = run_inputs.queue_event_payload("input.deleted", item)
    await session.delete(item)
    await session.commit()
    await run_inputs.record_queue_event(
        get_session_factory(), org_id=ctx.org_id, payload=deleted_payload
    )


@router.put("/chat/sessions/{session_id}/queued-inputs/order")
async def reorder_queued_inputs(
    session_id: UUID, body: QueuedInputOrderIn, session: Session, ctx: Ctx
) -> dict:
    """重排排队中的输入;ids 必须恰好是当前全部 queued 项的一个排列。"""
    await _get_session_or_404(session, session_id, ctx.user_id)
    items = await run_inputs.list_queued_inputs(session, session_id)
    try:
        assignments = run_inputs.reorder_positions(items, body.ids)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    by_id = {item.id: item for item in items}
    for input_id, position in assignments:
        by_id[input_id].position = position
        session.add(by_id[input_id])
    await session.commit()
    ordered = await run_inputs.list_queued_inputs(session, session_id)
    return {
        "items": [
            QueuedInputOut.model_validate(item, from_attributes=True)
            for item in ordered
        ]
    }


async def _publish_goal_event(
    ctx: OrgContext, chat: ChatSession, payload: dict
) -> None:
    """goal.updated 落 chat_events(best-effort)。

    API 侧没有活跃 SSE emit 通道,与 input.* 队列事件同一处理:直接写表。
    run_id 取会话当前活跃 run;空闲会话用 payload 里的 goal_id 兜底
    (chat_events.run_id 非空,而"设/取消目标"本就不属于任何一次 run)。
    """
    run_id = chat.active_run_id or UUID(str(payload["goal_id"]))
    await session_goal.record_goal_event(
        get_session_factory(),
        org_id=ctx.org_id,
        session_id=chat.id,
        run_id=run_id,
        payload=payload,
    )


@router.get("/chat/sessions/{session_id}/goal", response_model=SessionGoalDetailOut)
async def get_session_goal(
    session_id: UUID, session: Session, ctx: Ctx
) -> SessionGoalDetailOut:
    """会话目标详情(含预算进度与自动续跑轮次);未设目标时 goal=null。"""
    await _get_session_or_404(session, session_id, ctx.user_id)
    goal = await session_goal.get_goal(session, session_id)
    return SessionGoalDetailOut(
        goal=(
            SessionGoalOut.model_validate(goal, from_attributes=True)
            if goal is not None
            else None
        )
    )


@router.put("/chat/sessions/{session_id}/goal", response_model=SessionGoalOut)
async def set_session_goal(
    session_id: UUID, body: SessionGoalIn, session: Session, ctx: Ctx
) -> SessionGoalOut:
    """设定或替换会话目标(替换会先终结旧目标,预算与续跑计数清零)。"""
    chat = await _get_session_or_404(session, session_id, ctx.user_id)
    goal, cancelled = await session_goal.set_goal(
        session,
        chat=chat,
        objective=body.objective,
        token_budget=body.token_budget,
        max_auto_runs=body.max_auto_runs,
    )
    if cancelled is not None:
        await _publish_goal_event(ctx, chat, cancelled)
    await _publish_goal_event(ctx, chat, session_goal.goal_event_payload(goal))
    return SessionGoalOut.model_validate(goal, from_attributes=True)


@router.post("/chat/sessions/{session_id}/goal/cancel", response_model=SessionGoalOut)
async def cancel_session_goal(
    session_id: UUID, session: Session, ctx: Ctx
) -> SessionGoalOut:
    """取消会话目标(终态,不再自动续跑);未设目标时 404。"""
    chat = await _get_session_or_404(session, session_id, ctx.user_id)
    goal = await session_goal.cancel_goal(session, session_id)
    if goal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话未设置目标")
    await _publish_goal_event(ctx, chat, session_goal.goal_event_payload(goal))
    return SessionGoalOut.model_validate(goal, from_attributes=True)


@router.post("/chat/sessions/{session_id}/goal/complete", response_model=SessionGoalOut)
async def complete_session_goal(
    session_id: UUID, body: SessionGoalCompleteIn, session: Session, ctx: Ctx
) -> SessionGoalOut:
    """用户手动把目标标记为完成(终态,不再自动续跑);未设目标时 404。

    与 update_goal 工具走同一个 complete_goal:模型判断完成和用户判断完成
    应当落到同一种终态,只是 summary 来源不同(用户可不填,默认注明是人工判定)。
    目标已是 cancelled/completed 时 complete_goal 返回 None,这里转 409——
    重复完成是前端状态过期,不该静默成功让用户以为自己刚做了什么。
    """
    chat = await _get_session_or_404(session, session_id, ctx.user_id)
    summary = (body.summary or "").strip() or "由用户手动标记完成"
    goal = await session_goal.complete_goal(session, session_id, summary=summary)
    if goal is None:
        existing = await session_goal.get_goal(session, session_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "会话未设置目标")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"目标当前状态为 {existing.status},不能标记完成",
        )
    await _publish_goal_event(ctx, chat, session_goal.goal_event_payload(goal))
    return SessionGoalOut.model_validate(goal, from_attributes=True)


@router.post("/chat/sessions/{session_id}/cancel")
async def cancel_run(session_id: UUID, session: Session, ctx: Ctx) -> dict:
    """请求取消当前 run；进行中的单次 LLM HTTP 调用会在返回后才观察到取消。"""
    chat = (
        await session.execute(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    chat.cancel_requested = chat.active_run_id is not None
    session.add(chat)
    await session.commit()
    return {
        "run_id": str(chat.active_run_id) if chat.active_run_id else None,
        "cancel_requested": chat.cancel_requested,
    }


@router.get("/chat/sessions/{session_id}/events")
async def replay_events(
    session_id: UUID,
    session: Session,
    ctx: Ctx,
    run_id: UUID,
    after_seq: int = 0,
) -> list[dict]:
    await _get_session_or_404(session, session_id, ctx.user_id)
    events = (
        (
            await session.execute(
                select(ChatEvent)
                .where(
                    ChatEvent.session_id == session_id,
                    ChatEvent.run_id == run_id,
                    ChatEvent.seq > max(0, after_seq),
                )
                .order_by(ChatEvent.seq)
            )
        )
        .scalars()
        .all()
    )
    return [event.payload for event in events]
