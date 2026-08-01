from pathlib import Path

import pytest

from nicekit.agent import skills


def _skill(root: Path, slug: str = "demo") -> Path:
    folder = root / slug
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(
        "---\nname: Demo\ndescription: Test skill\nversion: 1.2.0\n"
        "category: test\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    return folder


def test_list_skills_parses_frontmatter(tmp_path: Path, monkeypatch) -> None:
    _skill(tmp_path)
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    skills.clear_skill_cache()
    found = skills.list_skills()
    assert found[0].slug == "demo"
    assert found[0].version == "1.2.0"


@pytest.mark.parametrize("path", ["../secret.txt", "../../secret.txt"])
def test_read_skill_rejects_traversal(
    tmp_path: Path, monkeypatch, path: str
) -> None:
    _skill(tmp_path)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    with pytest.raises(skills.SkillError):
        skills.read_skill_file("demo", path)


def test_read_skill_truncates_large_text(tmp_path: Path, monkeypatch) -> None:
    folder = _skill(tmp_path)
    (folder / "large.txt").write_text("x" * 40000, encoding="utf-8")
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    content = skills.read_skill_file("demo", "large.txt")
    assert "文件已截断" in content
    assert len(content) < 33000


def test_read_skill_rejects_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    folder = _skill(tmp_path)
    outside = tmp_path.parent / "outside-skill.txt"
    outside.write_text("secret", encoding="utf-8")
    link = folder / "outside.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 配置不允许创建符号链接")
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    with pytest.raises(skills.SkillError):
        skills.read_skill_file("demo", "outside.txt")


VALID_MD = (
    "---\nname: 新技能\ndescription: 用途说明\nversion: 1.0.0\n"
    "category: general\n---\n\n# 使用说明\n"
)


def test_write_skill_creates_and_updates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    skills.clear_skill_cache()
    skills.write_skill("new-skill", VALID_MD)
    assert [s.slug for s in skills.list_skills()] == ["new-skill"]
    skills.write_skill("new-skill", VALID_MD.replace("1.0.0", "1.1.0"))
    assert skills.list_skills()[0].version == "1.1.0"


@pytest.mark.parametrize("slug", ["../x", "A-Upper", "a", "含中文", "a/b", "a" * 65])
def test_write_skill_rejects_bad_slug(tmp_path: Path, monkeypatch, slug: str) -> None:
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    with pytest.raises(skills.SkillError):
        skills.write_skill(slug, VALID_MD)


def test_write_skill_requires_frontmatter_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    with pytest.raises(skills.SkillError):
        skills.write_skill("no-front", "# 没有 frontmatter\n")
    with pytest.raises(skills.SkillError):
        skills.write_skill("no-name", "---\ndescription: x\n---\n正文")
    with pytest.raises(skills.SkillError):
        skills.write_skill("big", VALID_MD + "x" * (64 * 1024))


def test_delete_skill_removes_directory(tmp_path: Path, monkeypatch) -> None:
    _skill(tmp_path)
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    skills.clear_skill_cache()
    skills.delete_skill("demo")
    assert not (tmp_path / "demo").exists()
    assert skills.list_skills() == []
    with pytest.raises(skills.SkillError):
        skills.delete_skill("demo")


def test_list_skill_files(tmp_path: Path, monkeypatch) -> None:
    folder = _skill(tmp_path)
    (folder / "references").mkdir()
    (folder / "references" / "guide.md").write_text("ref", encoding="utf-8")
    monkeypatch.setattr(skills, "_skills_root", lambda: tmp_path.resolve())
    assert skills.list_skill_files("demo") == ["SKILL.md", "references/guide.md"]
    with pytest.raises(skills.SkillError):
        skills.list_skill_files("missing")
