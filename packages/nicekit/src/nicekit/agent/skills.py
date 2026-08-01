"""SKILL.md 技能包:运行期只读渐进披露 + admin 侧文件系统维护(均防路径穿越)。"""

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from nicekit.core.config import get_settings

MAX_SKILL_FILE_BYTES = 32 * 1024
MAX_SKILL_MD_BYTES = 64 * 1024
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_CACHE_TTL = 60
_cache: tuple[float, tuple["SkillMeta", ...]] | None = None


class SkillError(ValueError):
    pass


@dataclass(frozen=True)
class SkillMeta:
    slug: str
    name: str
    description: str
    version: str
    category: str


@dataclass(frozen=True)
class SkillPackageIssue:
    slug: str
    code: str


def _skills_root() -> Path:
    return Path(get_settings().skills_dir).resolve()


def _parse_frontmatter(text: str, where: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{where} 缺少 YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise SkillError(f"{where} frontmatter 未闭合") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{where} frontmatter 不是合法 YAML:{exc}") from exc
    if not isinstance(data, dict):
        raise SkillError(f"{where} frontmatter 必须是对象")
    return data


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SkillError(f"无法读取 {path.name}:{exc}") from exc
    return _parse_frontmatter(text, f"{path.parent.name}/SKILL.md")


def list_skills() -> list[SkillMeta]:
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return list(_cache[1])
    root = _skills_root()
    skills: list[SkillMeta] = []
    if root.exists():
        for skill_file in sorted(root.glob("*/SKILL.md")):
            data = _frontmatter(skill_file)
            slug = skill_file.parent.name
            skills.append(
                SkillMeta(
                    slug=slug,
                    name=str(data.get("name") or slug),
                    description=str(data.get("description") or ""),
                    version=str(data.get("version") or "1.0.0"),
                    category=str(data.get("category") or "general"),
                )
            )
    _cache = (now + _CACHE_TTL, tuple(skills))
    return skills


def inspect_skill_packages() -> tuple[list[SkillMeta], list[SkillPackageIssue]]:
    """Return every readable package plus bounded diagnostics for broken ones."""
    root = _skills_root()
    skills: list[SkillMeta] = []
    issues: list[SkillPackageIssue] = []
    if not root.exists():
        return skills, issues
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        slug = skill_dir.name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            issues.append(SkillPackageIssue(slug=slug, code="skill_manifest_missing"))
            continue
        try:
            data = _frontmatter(skill_file)
        except SkillError:
            issues.append(SkillPackageIssue(slug=slug, code="skill_manifest_invalid"))
            continue
        skills.append(
            SkillMeta(
                slug=slug,
                name=str(data.get("name") or slug),
                description=str(data.get("description") or ""),
                version=str(data.get("version") or "1.0.0"),
                category=str(data.get("category") or "general"),
            )
        )
    return skills, issues


def read_skill_file(slug: str, relpath: str = "SKILL.md") -> str:
    root = _skills_root()
    skill_root = (root / slug).resolve()
    if skill_root.parent != root or not skill_root.is_dir():
        raise SkillError("skill 不存在")
    requested = (skill_root / relpath).resolve()
    if not requested.is_relative_to(skill_root) or not requested.is_file():
        raise SkillError("非法或不存在的 skill 文件路径")
    try:
        raw = requested.read_bytes()
    except OSError as exc:
        raise SkillError(f"无法读取 skill 文件:{exc}") from exc
    if b"\x00" in raw:
        raise SkillError("不支持读取二进制 skill 文件")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillError("skill 文件必须是 UTF-8 文本") from exc
    truncated = len(text) > MAX_SKILL_FILE_BYTES
    content = text[:MAX_SKILL_FILE_BYTES]
    if truncated:
        content += "\n\n...(文件已截断至 32KB)"
    return content


def clear_skill_cache() -> None:
    global _cache
    _cache = None


# ---- admin 维护(platform_admin 专属;运行期工具不走以下函数)


def _skill_dir(slug: str, *, must_exist: bool) -> Path:
    """slug 校验 + resolve 后确认仍在技能根目录内,防路径穿越/符号链接逃逸。"""
    if not SLUG_RE.fullmatch(slug):
        raise SkillError("slug 只能是小写字母/数字/中划线,长度 2-64")
    root = _skills_root()
    path = (root / slug).resolve()
    if path.parent != root:
        raise SkillError("非法 slug")
    if must_exist and not path.is_dir():
        raise SkillError("skill 不存在")
    return path


def list_skill_files(slug: str) -> list[str]:
    skill_root = _skill_dir(slug, must_exist=True)
    return sorted(
        p.relative_to(skill_root).as_posix()
        for p in skill_root.rglob("*")
        if p.is_file()
    )


def write_skill(slug: str, content: str) -> None:
    """写入(或新建)技能包的 SKILL.md;frontmatter 必须含 name 与 description。"""
    skill_root = _skill_dir(slug, must_exist=False)
    if len(content.encode("utf-8")) > MAX_SKILL_MD_BYTES:
        raise SkillError("SKILL.md 不能超过 64KB")
    data = _parse_frontmatter(content, f"{slug}/SKILL.md")
    if not str(data.get("name") or "").strip() or not str(data.get("description") or "").strip():
        raise SkillError("frontmatter 必须包含非空的 name 与 description")
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(content, encoding="utf-8", newline="\n")
    clear_skill_cache()


def delete_skill(slug: str) -> None:
    skill_root = _skill_dir(slug, must_exist=True)
    shutil.rmtree(skill_root)
    clear_skill_cache()
