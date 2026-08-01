"""知识库抽取契约:ingestion 管线用 LLM 按这些 schema 从文档抽取实体与关系。

SDK 不内置任何行业专用抽取契约——结构化抽取一律走 `GenericEntityExtraction`
(kb.extract.generic),字段约束由 `KbEntityType.field_schema` 在运行时注入,
落库前用 jsonschema 强校验。行业模型由宿主注册实体类型来表达,不改代码。
每个模型带 low_confidence_notes:抽不准/原文歧义处如实说明,供人工审核参考。
条目级 confidence:模型对每条给 0-1 置信度，摄入后写入
FactClaim.confidence。双协议 strict 交集(test_schema_compat)禁止 default/minimum/
maximum 等 schema 关键字,故 confidence 在 schema 中为必填纯 float,
范围校验与缺省(0.9)由 validator 承担,不进 schema。
条目级 evidence_quote(R3):模型必须为每条事实逐条返回原文引文；页码、
行号与表格单元格等 locator 不属于 LLM 输出契约，只能由服务端依据引文
回查 Docling 结构化主产物后生成。

另含 IngestProfile:per-KB 摄入切片配置(非 LLM 输出,存 JSONB)。
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nicekit.domain.common import LLMOutput

_EVIDENCE_QUOTE_MAX_CHARS = 2000


class ConfidentItem(LLMOutput):
    """抽取条目基类:逐条置信度与独立原文证据。"""

    confidence: float
    evidence_quote: str

    @model_validator(mode="before")
    @classmethod
    def _default_confidence(cls, data: object) -> object:
        # 网关/模型偶发漏字段时兜底 0.9,不让整段抽取失败(schema 仍是必填)
        if isinstance(data, dict) and data.get("confidence") is None:
            data = {**data, "confidence": 0.9}
        return data

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0 <= v <= 1:
            raise ValueError("confidence 必须在 0-1 之间")
        return v

    @field_validator("evidence_quote")
    @classmethod
    def _evidence_quote_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence_quote 不能为空")
        if len(value) > _EVIDENCE_QUOTE_MAX_CHARS:
            raise ValueError(
                f"evidence_quote 不能超过 {_EVIDENCE_QUOTE_MAX_CHARS} 个字符"
            )
        return value


class GenericExtractedItem(ConfidentItem):
    """通用类型抽取条目(M3a)。

    strict 结构化输出禁止自由 object(additionalProperties 必须显式 false),
    因此 attributes 以 JSON 对象字符串承载:LLM 按注入的类型 field_schema 填写,
    解析 + jsonschema 强校验在落库前执行,不合规条目整条丢弃。
    """

    attributes_json: str

    @field_validator("attributes_json")
    @classmethod
    def _must_parse_to_object(cls, value: str) -> str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("attributes_json 必须是合法 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("attributes_json 必须是 JSON 对象")
        return value

    def parsed_attributes(self) -> dict:
        return json.loads(self.attributes_json)


class GenericEntityExtraction(LLMOutput):
    """自定义实体类型的通用抽取包装(kb.extract.generic)。"""

    items: list[GenericExtractedItem]
    low_confidence_notes: list[str]


# ---- 实体图谱抽取(全类型统一;跨文档串联的输入)---------------------------


class ExtractedEntity(ConfidentItem):
    """文档里出现的一个知识实体(名称 + 类型 + 别名)。

    type_key 由服务端按「内置类型 + 本 org 已注册类型」白名单校验,越界的
    条目整条丢弃,避免模型自造类型污染实体库。
    """

    name: str
    type_key: str
    aliases: list[str]
    summary: str | None


class ExtractedRelation(ConfidentItem):
    """两个实体之间的一条有向/无向关系(谓词取自 GraphPredicate)。"""

    source_name: str
    target_name: str
    predicate: str


class EntityGraphExtraction(LLMOutput):
    """kb.extract.entities 的输出:实体 + 实体间关系。"""

    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    low_confidence_notes: list[str]


# ---- Wiki 自动生成契约(KB-5A,借鉴 llm_wiki 两步思维链摄入)----------------


class WikiPlannedPage(LLMOutput):
    """Step 1 分析建议的页面操作:create 新页 / update 已有页。"""

    title: str
    page_type: str
    action: Literal["create", "update"]
    reason: str | None


class WikiRelatedPage(LLMOutput):
    """文档与库内现有页的关联说明(page_title 必须来自清单)。"""

    page_title: str
    relation: str


class WikiAnalysis(LLMOutput):
    """Step 1(kb.wiki_analyze)输出:主题/关联/矛盾/页面操作计划。"""

    key_topics: list[str]
    related_pages: list[WikiRelatedPage]
    contradictions: list[str]
    planned_pages: list[WikiPlannedPage]


class WikiGeneratedPage(LLMOutput):
    """Step 2 生成的单页:content_markdown 鼓励 [[wikilink]] 与来源署名。"""

    title: str
    page_type: str
    content_markdown: str
    evidence_quote: str

    @field_validator("evidence_quote")
    @classmethod
    def _evidence_quote_content(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("evidence_quote 不能为空")
        if len(value) > _EVIDENCE_QUOTE_MAX_CHARS:
            raise ValueError(
                f"evidence_quote 不能超过 {_EVIDENCE_QUOTE_MAX_CHARS} 个字符"
            )
        return value


class WikiGeneration(LLMOutput):
    """Step 2(kb.wiki_generate)输出。"""

    pages: list[WikiGeneratedPage]


class WikiMergeResult(LLMOutput):
    """kb.wiki_merge 输出:旧文+新文 → 合并文(保留旧要点、标注矛盾)。"""

    merged_markdown: str


class WikiChunkDigest(LLMOutput):
    """kb.wiki_analyze_chunk 输出:长文档滚动摘要的单步结果。"""

    key_points: list[str]
    global_summary: str


class WikiOverview(LLMOutput):
    """kb.wiki_overview 输出:知识库总览页正文。"""

    content_markdown: str


# ---- IngestProfile(KB-2):per-KB 摄入切片配置 ------------------------------


class IngestProfile(BaseModel):
    """存 knowledge_bases.ingest_profile(JSONB);API 写入前必须过本模型校验,
    无 profile 的库摄入时用本模型默认值。parent_child 仅存配置,本期不实现。"""

    model_config = ConfigDict(extra="forbid")

    # mineru 占位后端已移除(2026-07-23):存量 profile 若含 "mineru" 会校验失败,
    # 摄入侧 _load_profile 兜底回退默认值(docling),不炸摄入。
    parser: Literal["fast", "docling"] = "docling"
    chunk_strategy: Literal["structure", "fixed"] = "structure"
    chunk_max_chars: int = Field(default=1200, ge=200, le=8000)
    chunk_overlap_chars: int = Field(default=150, ge=0, le=500)
    table_mode: Literal["row", "whole"] = "row"
    parent_child: bool = False
    # 视觉 caption(VLM):默认开启,摄入图片时生成面向检索的中文转写;
    # provider/model 都为空时使用平台默认;库级覆盖必须成对且在 API/执行时校验视觉能力。
    caption_images: bool = True
    caption_provider: str | None = Field(default=None, min_length=1, max_length=50)
    caption_model: str | None = Field(default=None, min_length=1, max_length=200)
    # KB-5A:文档摄入成功后 best-effort 自动生成/维护 wiki 页(失败不影响摄入)
    auto_wiki: bool = True
    # 实体图谱抽取:全类型统一多跑一道实体/关系抽取,AI 审核通过后归一绑定,
    # 快照 graph 投影据此连边(同名实体跨文档归一到同一节点)。失败不影响摄入。
    auto_entities: bool = True

    @model_validator(mode="after")
    def _overlap_lt_max(self) -> "IngestProfile":
        if self.chunk_overlap_chars >= self.chunk_max_chars:
            raise ValueError("chunk_overlap_chars 必须小于 chunk_max_chars")
        if (self.caption_provider is None) != (self.caption_model is None):
            raise ValueError("caption_provider 与 caption_model 必须同时配置或同时留空")
        return self


# 预设模板(API GET /kb/ingest/profiles/presets 原样返回)。
# SDK 只给领域无关的三档示例;宿主可覆盖或追加自己的行业预设。
INGEST_PROFILE_PRESETS: dict[str, dict] = {
    "general": {
        "label": "通用文档(默认:docling 解析 + 结构感知切片)",
        "profile": IngestProfile().model_dump(),
    },
    "tabular_xlsx": {
        "label": "通用表格(xlsx):fast 直转 GFM 表格,超限按行组切、每片带表头",
        "profile": IngestProfile(
            parser="fast", table_mode="row", chunk_max_chars=1600, chunk_overlap_chars=0
        ).model_dump(),
    },
    "long_form_docx": {
        "label": "长文文档(docx):docling 高质量解析,按标题结构切片",
        "profile": IngestProfile(parser="docling", chunk_strategy="structure").model_dump(),
    },
    "illustrated_pdf": {
        "label": "图文混排(pdf):整表不切,保留版面与表格完整性",
        "profile": IngestProfile(
            parser="docling", table_mode="whole", chunk_max_chars=2000
        ).model_dump(),
    },
}
