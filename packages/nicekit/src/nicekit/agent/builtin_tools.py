"""SDK 内置通用工具:知识库 / 检索依据 / 联网 / 图片生成 / 技能 / 交互 /
计划 / 会话目标 / 定时任务 / 长期记忆 / 天气。

迁移自 TF backend/app/services/agent/tools.py 中与业务无关的那一部分
(MIGRATION-PLAN §5.4"tools.py 拆分")。TF 的 44 个旅游业务工具、
`kb_retrieve`、`business_media_select` 不搬。

三条搬运纪律:
1. **注册走 `default_registry.register()`**:每个工具在登记时就必须给全七轴
   权限元数据(取自 TF `BUILTIN_TOOL_PERMISSIONS`),坏元数据在导入期即报错;
   `label`/`category` 取自 TF `TOOL_META` 并中性化。
2. **未搬模块一律函数内延迟 import**:`nicekit.kb.search` / `nicekit.kb.eligibility`
   由 KB 检索线单独交付;capabilities 与 icron / session_goal / memory 则是为了
   避免导入环(service → tools → icron → service)。
3. **工具描述去领域化**:schema 与执行体照搬,描述里的行业措辞一律改中性,
   语义(纪律、禁编造、降级如实回报)一字不减。

宿主扩展点:
- `set_retrieval_snapshot_provider()`:`retrieval_get` 读哪份检索快照由宿主决定
  (TF 读的是业务流水线的 kb.retrieve 产物,SDK 不认识那张表)。
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from nicekit.agent.media_inspection import (
    AgentMediaInspectionError,
    inspect_knowledge_image,
)
from nicekit.agent.skills import SkillError, list_skills, read_skill_file

# 复用 tools.py 里已定型的 strict-schema 速记(单一定义,不再抄第二份)
from nicekit.agent.tools import (
    _B,
    _I_NULL,
    _S,
    _S_NULL,
    ToolContext,
    ToolError,
    _schema,
    default_registry,
    tool_permission,
)
from nicekit.agent.user_input import normalize_questions
from nicekit.core.db import get_session_factory
from nicekit.domain.agent_permission import (
    PermissionScope,
    ToolCategory,
    ToolDelegation,
    ToolEffect,
    ToolReversibility,
    ToolRisk,
)
from nicekit.domain.agent_plan import PLAN_STEP_STATUSES
from nicekit.domain.kb_media import project_authenticated_media_reference
from nicekit.models.kb import KnowledgeBase
from nicekit.models.memory import MemoryItem, MemoryScope, MemoryStatus

# 工具分组(admin 工具勾选列表与前端工具卡按此分组)
CATEGORY_KB = "知识库"
CATEGORY_NETWORK = "联网"
CATEGORY_SKILL = "技能"
CATEGORY_INTERACTION = "交互"
CATEGORY_PLAN = "计划"
CATEGORY_MEMORY = "记忆"
CATEGORY_CRON = "定时任务"


# ---------------------------------------------------------------------------
# 扩展点:检索依据快照
# ---------------------------------------------------------------------------

# 宿主实现:给定会话,返回"最近一次成功的知识库检索快照"(与 kb_search 的
# 实时检索不同,这里是重放已经发生过的那一次)。返回 None = 还没有检索记录。
# 快照结构约定:{"empty": bool, "counts": {...}, "retrieval": {分组名: [命中]}},
# 每条命中可带 media_refs(会被重新投影成带鉴权路由的引用)。
RetrievalSnapshotProvider = Callable[
    [AsyncSession, UUID, object], Awaitable[Mapping | None]
]


async def _null_retrieval_snapshot(
    session: AsyncSession, org_id: UUID, chat_session: object
) -> Mapping | None:
    return None


_retrieval_snapshot_provider: RetrievalSnapshotProvider = _null_retrieval_snapshot


def set_retrieval_snapshot_provider(provider: RetrievalSnapshotProvider | None) -> None:
    """注入宿主的检索快照读取(传 None 恢复"永远没有快照"的默认行为)。"""
    global _retrieval_snapshot_provider
    _retrieval_snapshot_provider = provider or _null_retrieval_snapshot


def get_retrieval_snapshot_provider() -> RetrievalSnapshotProvider:
    return _retrieval_snapshot_provider


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def _uuid_or_error(value: str | None, fallback: UUID | None, what: str) -> UUID:
    if value:
        try:
            return UUID(str(value))
        except ValueError as exc:
            raise ToolError(f"{what} 不是合法 UUID:{value}") from exc
    if fallback is not None:
        return fallback
    raise ToolError(f"缺少 {what}:请提供参数,或先绑定作用域")


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


@default_registry.register(
    "kb_list",
    "列出当前组织可见的知识库及其 ID。需要限定 kb_search 范围时先调用本工具。",
    _schema({}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.ORGANIZATION,
    ),
    label="知识库列表",
    category=CATEGORY_KB,
)
async def kb_list(ctx: ToolContext, args: dict) -> dict:
    del args
    rows = list(
        (
            await ctx.session.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.lifecycle_status == "active")
                .order_by(KnowledgeBase.name, KnowledgeBase.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "count": len(rows),
        "items": [
            {
                "id": str(row.id),
                "name": row.name,
                "kb_type": row.kb_type,
                "description": row.description,
                "access": "owned" if row.org_id == ctx.org_id else "shared",
                "active_snapshot_id": (
                    str(row.active_snapshot_id) if row.active_snapshot_id else None
                ),
            }
            for row in rows
        ],
    }


@default_registry.register(
    "kb_search",
    "在组织知识库中检索事实、实体与文档片段,返回带来源与置信度的结果。"
    "kb_ids=null 表示搜索当前组织全部可见库;需限定范围时先用 kb_list 获取 ID。"
    "图片命中会附带已审核描述、image_source citation 和认证 media_refs。"
    "检索为空时如实返回空,严禁编造。",
    _schema(
        {
            "query": _S,
            "top_k": _I_NULL,
            "kb_ids": {"type": ["array", "null"], "items": _S},
        }
    ),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.ORGANIZATION,
        ("kb_ids",),
    ),
    label="知识库检索",
    category=CATEGORY_KB,
)
async def kb_search(ctx: ToolContext, args: dict) -> dict:
    # 延迟 import:KB 检索链是独立交付面,agent 导入期不该把它整条拖进来
    from nicekit.kb.eligibility import (
        active_knowledge_base_lease_is_current,
        capture_active_knowledge_base_leases,
    )
    from nicekit.kb.search import default_embedder, search_kb

    query = (args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空")
    raw_kb_ids = args.get("kb_ids")
    kb_ids = (
        [_uuid_or_error(value, None, "kb_ids") for value in raw_kb_ids]
        if raw_kb_ids is not None
        else None
    )
    scope_leases = await capture_active_knowledge_base_leases(
        ctx.session,
        kb_ids=kb_ids,
    )
    if kb_ids is not None:
        missing = [str(kb_id) for kb_id in kb_ids if kb_id not in scope_leases]
        if missing:
            raise ToolError(f"知识库不可见或不存在:{', '.join(missing)}")
    await ctx.session.commit()
    hits = await search_kb(
        ctx.session,
        ctx.org_id,
        query,
        top_k=min(int(args.get("top_k") or 10), 20),
        kb_ids=kb_ids,
        embedder=default_embedder(),
    )
    hit_kb_ids = {UUID(str(hit.kb_id)) for hit in hits}
    hit_leases = {
        kb_id: lease for kb_id, lease in scope_leases.items() if kb_id in hit_kb_ids
    }
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in hit_leases]
    await ctx.session.commit()
    current_kb_ids = {
        kb_id
        for kb_id, lease in hit_leases.items()
        if await active_knowledge_base_lease_is_current(
            ctx.session,
            lease,
            lock=True,
        )
    }
    hits = [hit for hit in hits if UUID(str(hit.kb_id)) in current_kb_ids]
    await ctx.session.commit()
    return {
        "scope": {
            "mode": "all_visible" if kb_ids is None else "selected",
            "kb_ids": [str(kb_id) for kb_id in kb_ids] if kb_ids is not None else None,
        },
        "count": len(hits),
        "hits": [
            {
                "kind": h.kind,
                "layer": h.layer,
                "source": h.source,
                "confidence": h.confidence,
                "data": h.data,
                "citation": h.citation,
                "media_refs": [media.model_dump(mode="json") for media in h.media_refs],
            }
            for h in hits
        ],
    }


@default_registry.register(
    "kb_image_inspect",
    "经当前生效快照与权限校验后,使用已配置的多模态模型核验一张知识库原图。"
    "仅在已审核的 caption/OCR 不足以回答时调用;会产生外部模型成本并留下审计,"
    "不返回图片字节、对象键或凭证。能力未配置时如实返回不可用。",
    _schema({"asset_id": _S, "question": _S_NULL}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.SENSITIVE,
        {
            ToolCategory.LOCAL_DATA,
            ToolCategory.NETWORK,
            ToolCategory.EXTERNAL_COST,
        },
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.REVIEWABLE,
        # TF 是 PROJECT 档(按业务项目授权);SDK 的等价档是 RESOURCE
        PermissionScope.RESOURCE,
        ("asset_id",),
    ),
    label="核验知识图片",
    category=CATEGORY_KB,
)
async def kb_image_inspect(ctx: ToolContext, args: dict) -> dict:
    asset_id = _uuid_or_error(args.get("asset_id"), None, "asset_id")
    try:
        # kb_scope=None:SDK 只按 org 隔离 + "active 快照 + accepted" 两道硬闸。
        # 需要按宿主作用域再收一层可见范围的,由宿主自建工具包一层传 kb_scope。
        result = await inspect_knowledge_image(
            ctx.session,
            org_id=ctx.org_id,
            user_id=ctx.user_id,
            asset_id=asset_id,
            question=args.get("question"),
        )
    except AgentMediaInspectionError as exc:
        await ctx.session.commit()
        raise ToolError(f"知识图片核验不可用:{exc.code}") from exc
    await ctx.session.commit()
    return result


# 检索快照里参与媒体投影的分组名。TF 另有五个旅游专表分组(destinations/pois/
# hotels/costs/route_templates),随 §5.5 B1 一并删除,统一走 entities。
RETRIEVAL_GROUPS: tuple[str, ...] = ("pages", "chunks", "entities")
# 每组回给模型的条数上限(含 source/layer/confidence)
_RETRIEVAL_GROUP_LIMIT = 5


def project_retrieval_media(output: Mapping) -> dict:
    """把快照里的 media_refs 重新投影成带鉴权路由的引用(URL 不落库,读时现算)。"""
    raw_retrieval = output.get("retrieval")
    if not isinstance(raw_retrieval, Mapping):
        return dict(output)
    projected: dict = {}
    for group, items in raw_retrieval.items():
        if not isinstance(items, list):
            projected[group] = items
            continue
        rows = []
        for item in items:
            if not isinstance(item, Mapping):
                rows.append(item)
                continue
            row = dict(item)
            media = row.get("media_refs")
            if isinstance(media, list):
                row["media_refs"] = [
                    project_authenticated_media_reference(ref).model_dump(mode="json")
                    for ref in media
                ]
            rows.append(row)
        projected[group] = rows
    return {**output, "retrieval": projected}


@default_registry.register(
    "retrieval_get",
    "读取本会话作用域最近一次成功的知识库检索结果(既有产物引用的事实依据),"
    "不重跑检索。重放知识图片时会从稳定 asset/snapshot 标识投影新的认证路由。"
    "empty=true 表示还没有检索记录。",
    _schema({}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.RESOURCE,
    ),
    label="检索依据",
    category=CATEGORY_KB,
)
async def retrieval_get(ctx: ToolContext, args: dict) -> dict:
    del args
    output = await _retrieval_snapshot_provider(
        ctx.session, ctx.org_id, ctx.chat_session
    )
    if not output:
        return {
            "empty": True,
            "message": "当前作用域还没有成功的检索记录,可先执行知识库检索",
        }
    projected = project_retrieval_media(output)
    retrieval = projected.get("retrieval") or {}
    groups = {
        key: (retrieval.get(key) or [])[:_RETRIEVAL_GROUP_LIMIT]
        for key in RETRIEVAL_GROUPS
    }
    return {
        "empty": bool(projected.get("empty")),
        "counts": projected.get("counts") or {},
        "query": retrieval.get("query"),
        "retrieval": groups,
    }


# ---------------------------------------------------------------------------
# 计划模式与会话目标
# ---------------------------------------------------------------------------

PLAN_MAX_STEPS = 20


def validate_plan_steps(steps: object) -> list[dict]:
    """校验并规范化计划步骤(全量覆盖语义);非法输入抛 ToolError。"""
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps 必须是非空数组")
    if len(steps) > PLAN_MAX_STEPS:
        raise ToolError(f"计划最多 {PLAN_MAX_STEPS} 步,请合并粒度")
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise ToolError(f"steps[{i}] 必须是对象")
        step_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        step_status = str(raw.get("status") or "").strip()
        if not step_id or not title:
            raise ToolError(f"steps[{i}] 的 id 与 title 不能为空")
        if step_id in seen_ids:
            raise ToolError(f"steps[{i}] 的 id 重复:{step_id}")
        if step_status not in PLAN_STEP_STATUSES:
            raise ToolError(
                f"steps[{i}].status 必须是 {sorted(PLAN_STEP_STATUSES)} 之一"
            )
        seen_ids.add(step_id)
        note = raw.get("note")
        out.append(
            {
                "id": step_id,
                "title": title,
                "status": step_status,
                "note": str(note) if note else None,
            }
        )
    return out


@default_registry.register(
    "plan_update",
    "创建或更新本会话的执行计划(全量覆盖)。多步任务开始前先列计划,"
    "每完成一步立即更新对应 status(pending/in_progress/done/skipped/failed);"
    "某步执行失败且没有立刻改道成功时,必须把它标成 failed 并在 note 里写明失败原因,"
    "不要留在 in_progress;计划有变(用户改需求、失败改道)先更新计划再继续。",
    _schema(
        {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _S,
                        "title": _S,
                        "status": _S,
                        "note": _S_NULL,
                    },
                    "required": ["id", "title", "status", "note"],
                    "additionalProperties": False,
                },
            }
        }
    ),
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
    ),
    label="更新计划",
    category=CATEGORY_PLAN,
)
async def plan_update(ctx: ToolContext, args: dict) -> dict:
    steps = validate_plan_steps(args.get("steps"))
    ctx.chat_session.plan = steps
    ctx.session.add(ctx.chat_session)
    await ctx.session.commit()
    return {"ok": True, "steps": steps}


@default_registry.register(
    "update_goal",
    "声明当前会话目标已完成。只有在核对过当前系统状态、证据表明目标的每一项要求"
    "都已满足时才调用;不确定、还有剩余工作或遇到阻塞时不要调用——继续推进或"
    "如实说明阻塞即可。status 目前只接受 completed;summary 用中文写清"
    "做了什么、结论是什么、还有什么需要用户知道。",
    _schema({"status": _S, "summary": _S}),
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("status",),
    ),
    label="更新会话目标",
    category=CATEGORY_PLAN,
)
async def update_goal(ctx: ToolContext, args: dict) -> dict:
    # 局部导入:session_goal 的续跑路径要回头用 service,模块级互相导入会成环
    from nicekit.agent.session_goal import complete_goal, goal_event_payload

    status = str(args.get("status") or "").strip()
    if status != "completed":
        raise ToolError("status 只能是 completed;目标未完成时不要调用本工具")
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ToolError("summary 不能为空:请说明完成了什么、结论是什么")
    goal = await complete_goal(ctx.session, ctx.chat_session.id, summary=summary)
    if goal is None:
        raise ToolError("当前会话没有进行中的目标,无需调用 update_goal")
    return {"ok": True, "goal": goal_event_payload(goal)}


# ---------------------------------------------------------------------------
# 定时任务
# ---------------------------------------------------------------------------

CRONJOB_ACTIONS: tuple[str, ...] = ("create", "pause", "resume", "run_now", "list")


def _cronjob_view(task, *, preview: list | None = None) -> dict:
    """回给模型的任务视图(时间一律 ISO8601,带时区)。"""
    from nicekit.agent.icron import schedule_summary

    view = {
        "task_id": str(task.id),
        "name": task.name,
        "schedule": schedule_summary(task),
        "schedule_kind": task.schedule_kind,
        "status": task.status,
        "timezone": task.timezone,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
    }
    if preview is not None:
        view["upcoming_runs"] = [moment.isoformat() for moment in preview]
    return view


@default_registry.register(
    "cronjob",
    "管理自主定时任务:到点系统会自动起一次 agent 运行去执行 instruction,"
    "结果转成通知发给你。action=create 新建(需要 name/instruction/schedule_kind;"
    "cron 传 cron_expr 五段式、interval 传 interval_seconds(≥60)、once 传 run_at "
    "ISO8601、manual 只能手动运行);pause/resume/run_now 需要 task_id;list 列出"
    "当前用户的定时任务。**instruction 必须完全自包含**:到点执行时没有人能回答"
    "追问,写清目标、已知事实(相关对象标识、时间范围等)、约束与期望产出,"
    "只写「继续上次那个」到点什么也做不了。新建前先向用户确认节奏与内容。",
    _schema(
        {
            "action": _S,
            "name": _S_NULL,
            "instruction": _S_NULL,
            "schedule_kind": _S_NULL,
            "cron_expr": _S_NULL,
            "interval_seconds": _I_NULL,
            "run_at": _S_NULL,
            "task_id": _S_NULL,
        }
    ),
    # 建一条定时任务 = 授权 agent 在未来无人值守地反复执行一段指令,影响面远大于
    # "改一行数据"。USER_REQUIRED 让它在任何 profile 下都必须人在环(含 full_access),
    # 同时天然挡住"定时任务里再建定时任务"——无人值守 run 把 ASK_USER 降级为 DENY,
    # cron 炸弹因此建不起来。
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.SENSITIVE,
        {ToolCategory.LOCAL_DATA, ToolCategory.WORKFLOW},
        ToolReversibility.COMPENSATABLE,
        ToolDelegation.USER_REQUIRED,
        PermissionScope.ORGANIZATION,
        (
            "action",
            "name",
            "instruction",
            "schedule_kind",
            "cron_expr",
            "interval_seconds",
            "run_at",
            "task_id",
        ),
    ),
    side_effect="write",
    confirm=True,
    label="定时任务",
    category=CATEGORY_CRON,
)
async def cronjob(ctx: ToolContext, args: dict) -> dict:
    # 局部导入:icron 依赖 service(起 run),service 依赖本模块 → 模块级会成环
    from nicekit.agent import icron

    action = str(args.get("action") or "").strip()
    if action not in CRONJOB_ACTIONS:
        raise ToolError(f"action 只能是 {'/'.join(CRONJOB_ACTIONS)},收到 {action!r}")

    async def _own_task(task_id_text: str | None):
        """按 id 取任务;只允许操作当前用户自己创建的任务。"""
        if not task_id_text:
            raise ToolError(f"action={action} 需要 task_id(可先用 action=list 查)")
        try:
            task_id = UUID(str(task_id_text))
        except ValueError as exc:
            raise ToolError(f"task_id 不是合法 UUID:{task_id_text!r}") from exc
        task = await icron.get_task(ctx.session, task_id)
        if task is None or task.created_by != ctx.user_id:
            raise ToolError("定时任务不存在或不属于当前用户")
        return task

    if action == "list":
        tasks = await icron.list_tasks(ctx.session, created_by=ctx.user_id)
        return {"tasks": [_cronjob_view(task) for task in tasks]}

    if action == "create":
        name = str(args.get("name") or "").strip()
        instruction = str(args.get("instruction") or "").strip()
        schedule_kind = str(args.get("schedule_kind") or "").strip()
        if not name:
            raise ToolError("create 需要 name(任务名,会作为会话标题)")
        if not instruction:
            raise ToolError("create 需要 instruction(自包含的任务描述)")
        raw_interval = args.get("interval_seconds")
        try:
            run_at = icron.parse_run_at(args.get("run_at"))
            task = await icron.create_task(
                ctx.session,
                org_id=ctx.org_id,
                created_by=ctx.user_id,
                name=name[:200],
                instruction=instruction,
                schedule_kind=schedule_kind,
                cron_expr=str(args.get("cron_expr") or "").strip() or None,
                interval_seconds=int(raw_interval) if raw_interval else None,
                run_at=run_at,
                # agent 建的任务不绑定当前会话:每次触发开一条干净的新会话,
                # 与"instruction 必须自包含"是同一条纪律(要延续上下文得由人显式指定);
                # 但作用域跟着当前会话走,任务起的 run 才落在同一批数据上
                scope_type=ctx.chat_session.scope_type,
                scope_id=ctx.chat_session.scope_id,
            )
        except icron.IcronScheduleError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "ok": True,
            **_cronjob_view(task, preview=icron.preview_schedule(task)),
        }

    task = await _own_task(args.get("task_id"))
    if action == "pause":
        task = await icron.set_task_status(
            ctx.session, task, icron.IcronTaskStatus.PAUSED.value
        )
        return {"ok": True, **_cronjob_view(task)}
    if action == "resume":
        if task.status == icron.IcronTaskStatus.ARCHIVED.value:
            raise ToolError("已归档的任务不能恢复,请新建一条")
        task = await icron.set_task_status(
            ctx.session, task, icron.IcronTaskStatus.ACTIVE.value
        )
        return {"ok": True, **_cronjob_view(task, preview=icron.preview_schedule(task))}

    # run_now:后台起 run,不占用当前这一轮(一次 run 可能跑好几分钟)
    icron.schedule_launch(
        get_session_factory(),
        org_id=ctx.org_id,
        task_id=task.id,
        trigger_source=icron.IcronTriggerSource.AGENT.value,
    )
    return {
        "ok": True,
        "started": True,
        "note": "已在后台开始执行,结果会以通知发出;运行历史见定时任务页",
        **_cronjob_view(task),
    }


# ---------------------------------------------------------------------------
# 长期记忆
#
# 三个工具共用 agent/memory.py 的校验管道:memory_write 与后台抽取器走的是
# 同一个 ingest_candidate,没有"agent 主动记的就免检"这条捷径——绕过去重会让
# 同一句话被记十遍,绕过防污染会让模型脑补的偏好变成组织资产。
# ---------------------------------------------------------------------------


async def _memory_scope_refs(ctx: ToolContext) -> dict[str, str]:
    """当前会话可归属的 {记忆范围: ref};未注册 ScopeResolver 时为空。"""
    from nicekit.agent import memory as memory_service

    return await memory_service.resolve_scope_refs(
        ctx.session, ctx.org_id, ctx.chat_session.id
    )


def _memory_public(item) -> dict:
    """回给模型的记忆视图:带 id(供 memory_forget 引用)与来源,不含 embedding。"""
    return {
        "memory_id": str(item.id),
        "type": item.memory_type,
        "scope": item.scope,
        "title": item.title,
        "content": item.content,
        "confidence": round(float(item.confidence or 0), 2),
        "source": item.source,
        "hit_count": item.hit_count,
    }


@default_registry.register(
    "memory_search",
    "检索本组织的长期记忆(用户偏好/硬约束/已确认决策/风险)。系统每轮已自动"
    "召回最相关的几条注入上下文,只有需要查更早或更具体的记忆时才调用本工具。"
    "scope 可限定记忆范围(org 为全组织通用,其余范围由本部署自行定义),"
    "留空表示全部可见范围。",
    _schema({"query": _S, "scope": _S_NULL}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.ORGANIZATION,
        ("query",),
    ),
    label="检索长期记忆",
    category=CATEGORY_MEMORY,
)
async def memory_search(ctx: ToolContext, args: dict) -> dict:
    from nicekit.agent import memory as memory_service
    from nicekit.models.memory import known_memory_scopes

    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query 不能为空:请给出要检索的主题关键词")
    scope = str(args.get("scope") or "").strip() or None
    known = known_memory_scopes()
    if scope is not None and scope not in known:
        raise ToolError(f"scope 非法:{scope};可选 " + "/".join(sorted(known)))
    refs = list((await _memory_scope_refs(ctx)).values())
    result = await memory_service.recall(
        ctx.session,
        ctx.org_id,
        None,
        query,
        # 工具检索面向"我要查点东西",给的余量比自动注入段大,但仍封顶
        budget_chars=4000,
        extra_scope_refs=refs,
        max_items=20,
    )
    items = [item for item in result.selected if scope is None or item.scope == scope]
    return {
        "query": query,
        "count": len(items),
        "memories": [_memory_public(item) for item in items],
    }


@default_registry.register(
    "memory_write",
    "把一条值得跨会话长期保留的用户偏好/硬约束/已确认决策/风险记进长期记忆。"
    "type 取 preference/constraint/decision/fact/risk;scope 取 org(全组织通用规则)"
    "或本部署定义的具体作用域(记忆跟着该作用域走)。只记用户明确说过、有对话原文"
    "支撑的内容——不要记一次性情绪、你自己推测的偏好、无来源的外部事实,也不要记"
    "用户已经否定过的建议。系统会做去重与置信打分,重复记同一件事不会新增记录。",
    _schema({"type": _S, "scope": _S, "title": _S, "content": _S}),
    # 写入本身可逆(写错了可以 memory_forget 掉)且必过校验管道,故按
    # routine/automatic 处理:每记一条偏好都弹确认会把对话打断得没法用
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.ORGANIZATION,
        ("type", "scope", "title"),
    ),
    label="记录长期记忆",
    category=CATEGORY_MEMORY,
)
async def memory_write(ctx: ToolContext, args: dict) -> dict:
    from nicekit.agent import memory as memory_service

    try:
        candidate = memory_service.normalize_candidate(
            {
                "memory_type": args.get("type"),
                "scope": args.get("scope"),
                "title": args.get("title"),
                "content": args.get("content"),
                # agent 主动记的默认给 0.7:高于丢弃阈值、低于提升阈值,
                # 想成为正式 preference 仍要靠后续对话再次印证
                "confidence": 0.7,
            }
        )
    except memory_service.MemoryValidationError as exc:
        raise ToolError(str(exc)) from exc

    scope_refs = await _memory_scope_refs(ctx)
    scope_ref = scope_refs.get(candidate.scope)
    if candidate.scope != MemoryScope.ORG.value and not scope_ref:
        raise ToolError(
            f"当前会话没有可归属的 {candidate.scope} 作用域,"
            f"写不了 scope={candidate.scope} 的记忆;"
            "请先绑定对应作用域,或改用 scope=org 记录全组织通用规则"
        )
    outcome = await memory_service.ingest_candidate(
        ctx.session,
        org_id=ctx.org_id,
        candidate=candidate,
        source=f"{memory_service.SOURCE_AGENT_TOOL}:{ctx.chat_session.id}",
        scope_ref_id=scope_ref,
    )
    await ctx.session.commit()
    if not outcome.written:
        # 不抛错:被管道拒绝是正常结果,把原因如实回给模型让它别再试同一条
        return {"ok": False, "action": "dropped", "reason": outcome.reason}
    return {
        "ok": True,
        "action": outcome.action,
        "reason": outcome.reason,
        "memory": _memory_public(outcome.item),
    }


@default_registry.register(
    "memory_forget",
    "把一条长期记忆标记为失效(不再被召回)。用于记忆过时、被用户当面否定或"
    "确认记错了。这是高影响操作:记忆可能是他人长期沉淀的组织资产,发起前先向"
    "用户说清要作废哪一条、为什么。reason 用中文写明依据。",
    _schema({"memory_id": _S, "reason": _S}),
    # 删记忆是高影响:被否定的记忆永不再召回,而它可能是别人沉淀了很久的组织资产。
    # REVIEWABLE 触发 confirm=True 人在环,由用户拍板。
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.SENSITIVE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.COMPENSATABLE,
        ToolDelegation.REVIEWABLE,
        PermissionScope.ORGANIZATION,
        ("memory_id", "reason"),
    ),
    label="标记记忆失效",
    category=CATEGORY_MEMORY,
)
async def memory_forget(ctx: ToolContext, args: dict) -> dict:
    memory_id = _uuid_or_error(str(args.get("memory_id") or ""), None, "memory_id")
    reason = str(args.get("reason") or "").strip()
    if not reason:
        raise ToolError("reason 不能为空:请说明为什么这条记忆不再成立")
    item = (
        await ctx.session.execute(
            select(MemoryItem).where(
                MemoryItem.id == memory_id, MemoryItem.org_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise ToolError("记忆不存在或不属于本组织")
    if item.status == MemoryStatus.REJECTED.value:
        raise ToolError("这条记忆已经被标记为失效,无需重复操作")
    # 软删:置 rejected 而非物理删。来源与判断痕迹必须留存,否则下一轮抽取
    # 会把同一条脏记忆原样再写一遍(见 memory.ingest_candidate 的 rejected 拦截)。
    item.status = MemoryStatus.REJECTED.value
    item.content = f"{item.content}\n[已失效] {reason[:200]}"
    item.updated_at = datetime.now(UTC)
    ctx.session.add(item)
    await ctx.session.commit()
    return {"ok": True, "memory_id": str(item.id), "status": item.status}


# ---------------------------------------------------------------------------
# 人在环
# ---------------------------------------------------------------------------


@default_registry.register(
    "ask_user",
    "向用户提出结构化澄清问题。每次提供 1-4 个问题,每题提供 2-4 个互斥候选;"
    "multi_select=true 时用户可多选。不要自行提供 Other/其他选项,调用端会统一添加"
    "可填写的其他答案。调用后本轮暂停,等待用户一次性提交全部答案。",
    _schema(
        {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _S,
                        "header": _S,
                        "question": _S,
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": _S,
                                    "description": _S_NULL,
                                },
                                "required": ["label", "description"],
                                "additionalProperties": False,
                            },
                        },
                        "multi_select": _B,
                    },
                    "required": [
                        "id",
                        "header",
                        "question",
                        "options",
                        "multi_select",
                    ],
                    "additionalProperties": False,
                },
            }
        }
    ),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
    ),
    label="向用户确认",
    category=CATEGORY_INTERACTION,
    # ask_user 是循环的交互协议(会让 run 停下来等人),卡片把它排除在外通常是
    # 刻意的自治设计,不该被"本轮临时工具"开关改掉
    runtime_grantable=False,
)
async def ask_user(ctx: ToolContext, args: dict) -> dict:
    del ctx
    # loop 对本工具特殊处理:合法调用会在执行器之前持久化请求并截停。
    return {"questions": normalize_questions(args.get("questions"))}


# ---------------------------------------------------------------------------
# Skill 渐进披露(只读,不执行服务端脚本)
# ---------------------------------------------------------------------------


@default_registry.register(
    "skills_list",
    "列出当前 agent 已绑定的只读技能包及其用途。",
    _schema({}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
    ),
    label="技能列表",
    category=CATEGORY_SKILL,
    # 可用性取决于卡片绑了哪些技能包,临时放开只会拿到空表,真正的开关在卡片上
    runtime_grantable=False,
)
async def skills_list_tool(ctx: ToolContext, args: dict) -> dict:
    del args
    bound = set(ctx.skills)
    return {"skills": [asdict(skill) for skill in list_skills() if skill.slug in bound]}


@default_registry.register(
    "skill_view",
    "读取当前 agent 已绑定技能包中的 SKILL.md 或静态参考文件。",
    _schema({"slug": _S, "path": _S_NULL}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.ROUTINE,
        {ToolCategory.LOCAL_DATA},
        ToolReversibility.REVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("slug", "path"),
    ),
    label="读取技能",
    category=CATEGORY_SKILL,
    runtime_grantable=False,
)
async def skill_view_tool(ctx: ToolContext, args: dict) -> dict:
    slug = str(args.get("slug") or "")
    if slug not in ctx.skills:
        return {"error": f"skill {slug} 未绑定到当前 agent"}
    path = str(args.get("path") or "SKILL.md")
    try:
        content = read_skill_file(slug, path)
    except SkillError as exc:
        return {"error": str(exc)}
    return {"slug": slug, "path": path, "content": content}


# ---------------------------------------------------------------------------
# 联网能力(薄包装 capabilities/websearch 与 capabilities/imagegen,
# 可降级不炸 loop)
# ---------------------------------------------------------------------------


@default_registry.register(
    "web_search",
    "联网搜索公开网页(时效性信息:政策规定、开放时间、近期动态等),返回带来源链接的结果。"
    'query 是主检索式;比较类问题(如"A 和 B 哪个更便宜")把其余检索式放进 queries 数组'
    "一次并发搜,比分多轮搜索快(含 query 共最多 3 条,超出忽略)。"
    "结果按来源可信度排序,每条带 ref 编号与 source_tier(official 官方/media 媒体/"
    "ugc 社区)。回答时用 [ref] 标注出处,官方来源优先于社区内容。"
    "只给摘要;需要原文细节(如完整条款、清单)时再用 web_fetch 读正文。"
    "搜不到或未配置时如实返回,严禁编造。freshness 可选 day/week/month/year 限定时效。",
    _schema(
        {
            "query": _S,
            "queries": {"type": ["array", "null"], "items": _S},
            "max_results": _I_NULL,
            "freshness": _S_NULL,
        }
    ),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.SENSITIVE,
        {ToolCategory.NETWORK},
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("query",),
    ),
    label="联网搜索",
    category=CATEGORY_NETWORK,
)
async def web_search(ctx: ToolContext, args: dict) -> dict:
    from nicekit.capabilities.websearch import service as websearch_service

    extra = args.get("queries")
    raw_queries = [args.get("query"), *(extra if isinstance(extra, list) else [])]
    queries = websearch_service.normalize_queries(
        raw_queries, args.get("max_results"), args.get("freshness")
    )
    if not queries:
        raise ToolError("query 不能为空")
    result = await websearch_service.run_multi_search(
        ctx.session,
        org_id=ctx.org_id,
        queries=queries,
        provider_override=ctx.websearch_provider,
    )
    await ctx.session.commit()  # 计量随本次调用落库
    if result.status == "unavailable":
        raise ToolError(f"联网搜索不可用:{result.error}")
    # 编号在 run 内累加:同一轮里第二次搜索接着上次往后编,避免 [1] 指代不明
    offset = ctx.citations.take(len(result.hits))
    return {
        "status": result.status,
        "provider": result.provider,
        "queries": result.queries,
        "cached": result.cached,
        "count": len(result.hits),
        # rank_score 是内部排序分,不喂模型——口径解释不清反而干扰判断
        "results": [
            hit.model_dump(exclude={"rank_score"}) | {"ref": offset + index}
            for index, hit in enumerate(result.hits, start=1)
        ],
    }


@default_registry.register(
    "web_fetch",
    "读取指定网页的正文内容(markdown),用于 web_search 摘要不够时深入原文——"
    "例如条款全文、材料清单、公告正文。urls 传 1-5 个 http(s) 链接,"
    "通常来自 web_search 结果。query 传本次要从正文里找什么"
    "(用于长正文压缩时保留相关段落)。"
    "内网地址与非网页资源会被安全策略拒绝;单页失败不影响其它页。"
    "正文可能被截断(truncated=true),引用时用 [ref] 标注出处,严禁编造未出现的内容。",
    _schema(
        {
            "urls": {"type": "array", "items": {"type": "string"}},
            "query": _S_NULL,
        }
    ),
    # 与 web_search 同档:只读外网、无副作用,但 URL 来自模型,
    # 因此 SSRF 防护在 capabilities/websearch/fetch.py 里做硬拦截,不靠权限层兜底
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.SENSITIVE,
        {ToolCategory.NETWORK},
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("urls",),
    ),
    label="网页读取",
    category=CATEGORY_NETWORK,
)
async def web_fetch(ctx: ToolContext, args: dict) -> dict:
    from nicekit.capabilities.websearch import service as websearch_service

    raw = args.get("urls")
    urls = [u for u in (raw or []) if isinstance(u, str) and u.strip()]
    if not urls:
        raise ToolError("urls 不能为空")
    pages = await websearch_service.run_fetch(
        ctx.session,
        org_id=ctx.org_id,
        urls=urls,
        query=(args.get("query") or "").strip(),
        provider_override=ctx.websearch_provider,
    )
    await ctx.session.commit()  # 计量随本次调用落库
    ok_count = sum(1 for page in pages if page.status == "ok")
    if not pages or ok_count == 0:
        status = "empty"
    elif ok_count < len(pages):
        status = "partial"
    else:
        status = "ok"
    offset = ctx.citations.take(len(pages))
    return {
        "status": status,
        "count": ok_count,
        "pages": [
            page.model_dump() | {"ref": offset + index}
            for index, page in enumerate(pages, start=1)
        ],
    }


@default_registry.register(
    "image_generate",
    "按文字描述生成 AI 创意图片(封面/配图/示意视觉,消耗外部 API 配额)。"
    "prompt 用具体的画面描述(主体/场景/风格/构图);"
    "size 可选 1024x1024/1024x1536/1536x1024;"
    "n 为张数(1-4)。结果由对话界面直接展示,最终回复不要重复输出或嵌入内部 /genimg URL。"
    "工具不返回图片内容本身。生成内容不得冒充实拍照片、"
    "已核验的价格或库存、权威图示、证件、许可、票据或二维码等事实证据。",
    _schema({"prompt": _S, "n": _I_NULL, "size": _S_NULL}),
    permission=tool_permission(
        ToolEffect.WRITE,
        ToolRisk.SENSITIVE,
        {ToolCategory.NETWORK, ToolCategory.EXTERNAL_COST},
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.REVIEWABLE,
        PermissionScope.SESSION,
        ("n", "size"),
    ),
    label="生成图片",
    category=CATEGORY_NETWORK,
    # 生图自己会持续 report progress,loop 不再发"开始执行"避免两条进度打架
    # (替代 TF loop.py 对 image_generate 的工具名硬编码特判)
    emit_start_progress=False,
)
async def image_generate(ctx: ToolContext, args: dict) -> dict:
    from nicekit.capabilities.imagegen import service as imagegen_service

    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        raise ToolError("prompt 不能为空")
    query = imagegen_service.normalize_query(prompt, args.get("n"), args.get("size"))
    output = await imagegen_service.generate_images(
        ctx.session,
        org_id=ctx.org_id,
        query=query,
        on_progress=ctx.progress,
    )
    await ctx.session.commit()  # 计量随本次调用落库
    if output["status"] == "unavailable":
        raise ToolError(f"图片生成不可用:{output['error']}")
    return output


# ---------------------------------------------------------------------------
# 天气
# ---------------------------------------------------------------------------


@default_registry.register(
    "weather_get",
    "查询指定城市未来几天的天气预报(多天气源自动降级)。用于需要考虑天气条件的"
    "安排(温度/降水/风力)。数据来自外部天气源,可能不可用:unavailable=true 时"
    "如实告知用户拿不到预报,严禁编造天气。",
    _schema({"location": _S, "days": _I_NULL, "start_date": _S_NULL}),
    permission=tool_permission(
        ToolEffect.READ,
        ToolRisk.SENSITIVE,
        {ToolCategory.NETWORK},
        ToolReversibility.IRREVERSIBLE,
        ToolDelegation.AUTOMATIC,
        PermissionScope.SESSION,
        ("location", "start_date"),
    ),
    label="天气预报",
    category=CATEGORY_NETWORK,
)
async def weather_get(ctx: ToolContext, args: dict) -> dict:
    del ctx
    from nicekit.capabilities.weather.service import get_weather_service

    location = str(args.get("location") or "").strip()
    if not location:
        raise ToolError("location 不能为空")
    days = int(args.get("days") or 7)
    if not 1 <= days <= 14:
        raise ToolError("days 必须在 1-14 之间")
    start: date | None = None
    if args.get("start_date"):
        try:
            start = date.fromisoformat(str(args["start_date"]))
        except ValueError as exc:
            raise ToolError(
                f"start_date 必须是 ISO 日期(YYYY-MM-DD):{args['start_date']}"
            ) from exc
    result = await get_weather_service().forecast(location, start=start, days=days)
    return asdict(result)


# SDK 内置通用工具全集(汇总测试与宿主装配按此核对)
BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "kb_list",
    "kb_search",
    "kb_image_inspect",
    "retrieval_get",
    "plan_update",
    "update_goal",
    "cronjob",
    "memory_search",
    "memory_write",
    "memory_forget",
    "ask_user",
    "skills_list",
    "skill_view",
    "web_search",
    "web_fetch",
    "image_generate",
    "weather_get",
)

__all__ = [
    "BUILTIN_TOOL_NAMES",
    "CRONJOB_ACTIONS",
    "PLAN_MAX_STEPS",
    "RETRIEVAL_GROUPS",
    "RetrievalSnapshotProvider",
    "get_retrieval_snapshot_provider",
    "project_retrieval_media",
    "set_retrieval_snapshot_provider",
    "validate_plan_steps",
]
