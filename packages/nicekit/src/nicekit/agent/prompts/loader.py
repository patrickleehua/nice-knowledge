"""系统 Prompt 资源加载器(参照 TripBoss promptres 的分层思想)。

prompt 不再散落在代码字符串里,而是每段一个带 YAML frontmatter 的 .md
资源文件放在 resources/ 下,随源码发布、随代码 review;业务代码按
PromptBlock 显式声明加载顺序,避免隐式配置导致链路不可控。核心约定:
全局段(global_*)每次 Agent run 必定加载,Agent 卡的 system_prompt 只
作为角色段注入,从机制上杜绝"改卡 prompt 就能覆盖全局工具纪律/输出
规则"。

与 TF(backend/app/services/agent/prompts/loader.py)的差异(§4 "Prompt 多 root"):
- TF 的 `_RESOURCE_DIR` 是固定的包内 resources/;SDK 改为**多 root 搜索**:
  SDK 内置目录 + 宿主经 `register_prompt_root()` 注册的目录。同 id 时后
  注册的 root 覆盖先注册的(即宿主覆盖 SDK),让宿主能替换任一段文案而不必
  fork SDK。
- 资源仍是一次性读入内存(首次访问时构建缓存),运行期零磁盘 IO;
  注册新 root 会使缓存失效并在下次访问时重建。装配期注册完 root 后
  应调用 `validate_prompt_resources()` 做启动期校验(变量声明与 {{var}}
  占位符一致性、frontmatter 完整性),让坏资源在启动期而不是对话中暴露。
"""

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# SDK 自带资源目录:永远排在搜索链最前(优先级最低,可被宿主覆盖)
BUILTIN_RESOURCE_DIR = Path(__file__).resolve().parent / "resources"

# 变量占位符统一 {{var}} 形式,var 限定标识符字符,避免误伤正文里的花括号
_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

_lock = threading.RLock()
_roots: list[Path] = [BUILTIN_RESOURCE_DIR]
_cache: dict[str, "PromptResource"] | None = None


class PromptResourceError(ValueError):
    """资源缺失或格式非法(frontmatter 不完整、id 与文件名不一致等)。"""


@dataclass(frozen=True)
class PromptResource:
    """单个 prompt 资源:frontmatter 元信息 + 正文。"""

    id: str
    title: str
    category: str
    description: str
    # 加载入口源码路径:排查"这段 prompt 是谁在什么链路注入的"时,不用全局
    # 搜代码,看 source 直接跳到消费方(对齐 TripBoss frontmatter 约定)
    source: str
    variables: tuple[str, ...]
    content: str
    # 该资源来自哪个 root(宿主覆盖 SDK 内置时,admin 预览要能看出是哪一份)
    origin: str = ""


@dataclass(frozen=True)
class PromptBlock:
    """组装用的资源块声明:引用资源 id,可携带变量值。"""

    id: str
    values: dict[str, str] | None = None


def register_prompt_root(path: Path | str) -> None:
    """追加一个宿主资源目录;同 id 时后注册的覆盖先注册的(宿主覆盖 SDK)。

    目录不存在直接抛错:注册了一个空路径而资源静默沿用 SDK 默认,是最难
    排查的一类问题(admin 里看到的还是旧文案,却查不出为什么)。
    """
    root = Path(path).resolve()
    if not root.is_dir():
        raise PromptResourceError(f"prompt 资源目录不存在:{root}")
    with _lock:
        global _cache
        if root in _roots:
            return
        _roots.append(root)
        _cache = None  # 下次访问重建


def prompt_roots() -> tuple[Path, ...]:
    """当前搜索链(按优先级从低到高)。"""
    with _lock:
        return tuple(_roots)


def reset_prompt_roots() -> None:
    """恢复到只剩 SDK 内置目录(测试 / 重新装配用)。"""
    with _lock:
        global _cache
        _roots.clear()
        _roots.append(BUILTIN_RESOURCE_DIR)
        _cache = None


def _parse_resource(path: Path) -> PromptResource:
    """解析单个资源文件;格式问题直接抛错让进程启动失败,不做静默兜底。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PromptResourceError(f"prompt 资源 {path.name} 缺少 YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise PromptResourceError(f"prompt 资源 {path.name} frontmatter 未闭合") from exc
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise PromptResourceError(
            f"prompt 资源 {path.name} frontmatter 不是合法 YAML:{exc}"
        ) from exc
    if not isinstance(meta, dict):
        raise PromptResourceError(f"prompt 资源 {path.name} frontmatter 必须是对象")

    resource_id = str(meta.get("id") or "")
    # 文件名与 id 强一致,排查时看文件名即知资源 id
    if resource_id != path.stem:
        raise PromptResourceError(
            f"prompt 资源 {path.name} 的 frontmatter id({resource_id!r})必须与文件名一致"
        )
    title = str(meta.get("title") or "").strip()
    category = str(meta.get("category") or "").strip()
    if not title or not category:
        raise PromptResourceError(f"prompt 资源 {path.name} frontmatter 缺少 title/category")
    # source 与 title/category 同级必填:没有加载入口的资源无法追溯消费链路,
    # 宁可启动失败也不允许"来历不明"的 prompt 段进入组装
    source = str(meta.get("source") or "").strip()
    if not source:
        raise PromptResourceError(f"prompt 资源 {path.name} frontmatter 缺少 source")

    content = "\n".join(lines[end + 1 :]).strip()
    declared = tuple(str(v) for v in (meta.get("variables") or []))
    # 声明变量与正文占位符必须一致:写了 {{var}} 忘了声明(或反之)都在启动期暴露
    used = set(_VAR_RE.findall(content))
    if set(declared) != used:
        raise PromptResourceError(
            f"prompt 资源 {path.name} variables 声明({sorted(declared)})"
            f"与正文占位符({sorted(used)})不一致"
        )
    return PromptResource(
        id=resource_id,
        title=title,
        category=category,
        description=str(meta.get("description") or "").strip(),
        source=source,
        variables=declared,
        content=content,
        origin=str(path.parent),
    )


def _load_all() -> dict[str, PromptResource]:
    resources: dict[str, PromptResource] = {}
    for root in _roots:
        if not root.is_dir():  # pragma: no cover - register 已校验,防运行期被删
            logger.warning("prompt 资源目录已消失,跳过:%s", root)
            continue
        for path in sorted(root.glob("*.md")):
            resource = _parse_resource(path)
            # 后注册的 root 覆盖先注册的:宿主想换掉某一段就放一个同名文件
            if resource.id in resources:
                logger.info(
                    "prompt 资源 %s 被 %s 覆盖(原 %s)",
                    resource.id,
                    resource.origin,
                    resources[resource.id].origin,
                )
            resources[resource.id] = resource
    if not resources:
        raise PromptResourceError(f"prompt 资源目录为空:{[str(r) for r in _roots]}")
    return resources


def _resources() -> dict[str, PromptResource]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_all()
        return _cache


def validate_prompt_resources() -> int:
    """启动期校验:强制解析全部 root 下的资源,返回资源条数。

    装配期(注册完宿主 root 后)调一次;坏 frontmatter / 变量声明与占位符
    不一致会在这里抛 PromptResourceError,而不是等到第一次对话才炸。
    """
    with _lock:
        global _cache
        _cache = None
    return len(_resources())


def load_prompt(resource_id: str) -> PromptResource:
    """按 id 取资源(元信息 + 正文);不存在即抛错,不返回隐藏 fallback。"""
    resource = _resources().get(resource_id)
    if resource is None:
        raise PromptResourceError(f"prompt 资源不存在:{resource_id}")
    return resource


def render(resource_id: str, values: dict[str, str] | None = None) -> str:
    """渲染资源正文,{{var}} 按 values 替换。

    缺变量时先看资源默认值(见 defaults.py 的 prompt_variable_defaults),
    仍缺就保留 {{var}} 原样并记 warning:预览/日志里肉眼可见哪里没注入,
    比静默替换成空串更容易排查。
    """
    resource = load_prompt(resource_id)
    supplied = {**prompt_variable_defaults(), **(values or {})}
    missing: set[str] = set()

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in supplied:
            return str(supplied[key])
        missing.add(key)
        return match.group(0)

    text = _VAR_RE.sub(_substitute, resource.content)
    if missing:
        logger.warning("prompt 资源 %s 渲染缺少变量:%s", resource_id, sorted(missing))
    return text


def compose(blocks: list[PromptBlock]) -> str:
    """按调用方给定顺序渲染并拼接资源块,块间空行分隔;空块自动跳过。"""
    parts = (render(block.id, block.values).strip() for block in blocks)
    return "\n\n".join(part for part in parts if part)


def list_prompts() -> list[PromptResource]:
    """全部资源清单(category、id 排序),供 admin 只读预览 API。"""
    return sorted(_resources().values(), key=lambda r: (r.category, r.id))


# ---------------------------------------------------------------------------
# 全局变量默认值(SDK 中性文案;宿主装配期覆盖)
# ---------------------------------------------------------------------------

# SDK 默认的产品身份:刻意不提任何行业。宿主用 set_prompt_variable
# ("product_identity", ...) 换成自己的产品定位,不必替换整个资源文件。
# memory_scope_guide 同理:SDK 只内置 org 一个记忆范围(见 models/memory.py),
# 宿主注册了自己的范围后应把这段词表一并换掉,否则模型不知道能写哪些 scope。
_DEFAULT_VARIABLES: dict[str, str] = {
    "product_identity": "你是一个多租户 AI 工作台助手",
    "memory_scope_guide": (
        "- `scope` 取值:\n"
        "  - `org` —— 与具体对象无关的组织通用规则(当前唯一可用范围)"
    ),
}

_variables: dict[str, str] = dict(_DEFAULT_VARIABLES)


def set_prompt_variable(name: str, value: str | None) -> None:
    """设置(或用 None 清除)一个全局 prompt 变量默认值。"""
    with _lock:
        if value is None:
            _variables.pop(name, None)
        else:
            _variables[name] = str(value)


def prompt_variable_defaults() -> dict[str, str]:
    with _lock:
        return dict(_variables)


def reset_prompt_variables() -> None:
    """恢复 SDK 默认变量值(测试 / 重新装配用)。"""
    with _lock:
        _variables.clear()
        _variables.update(_DEFAULT_VARIABLES)
