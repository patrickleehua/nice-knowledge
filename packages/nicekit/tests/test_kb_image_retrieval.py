"""图片检索投影与命中契约(媒体波次;不碰 DB/MinIO)。

TF 的 `test_project_retrieval_persists_stable_media_without_response_urls`
断言业务 pipeline 的 `_retrieved_item` 投影,属不搬运的业务层,已剔除;
媒体引用"落库不带 URL"的契约由 KnowledgeMediaReferenceSnapshot 自身的
模型测试(test_kb_media_contracts.py)覆盖。
"""

from uuid import uuid4

from nicekit.kb.retrieval_projection import (
    IMAGE_SNAPSHOT_META_KEY,
    image_chunk_content,
    image_chunk_snapshot_meta,
    image_retrieval_chunk_id,
)
from nicekit.kb.search import (
    SearchHit,
    _image_chunk_matches,
    _image_source_media,
)
from nicekit.models.kb import (
    DocumentRevision,
    KbChunk,
    KbImageAsset,
    KnowledgeBase,
    SourceDocument,
)


def _accepted_asset(**overrides: object) -> KbImageAsset:
    values: dict[str, object] = {
        "id": uuid4(),
        "org_id": uuid4(),
        "kb_id": uuid4(),
        "doc_id": uuid4(),
        "revision_id": uuid4(),
        "source_occurrence": "page:2/picture:1",
        "parser_name": "test",
        "page": 2,
        "bbox": {"left": 0.1, "top": 0.2, "right": 0.8, "bottom": 0.9},
        "image_sha256": "b" * 64,
        "size_bytes": 100,
        "content_type": "image/png",
        "width": 640,
        "height": 480,
        "original_object_key": "private/original.png",
        "thumbnail_object_key": "private/thumbnail.webp",
        "thumbnail_sha256": "c" * 64,
        "thumbnail_content_type": "image/webp",
        "thumbnail_size_bytes": 50,
        "thumbnail_width": 320,
        "thumbnail_height": 240,
        "caption": "设备铭牌",
        "ocr_text": "编号 SN-0912",
        "surrounding_text": "图片附近的原文",
        "extraction_fingerprint": "d" * 64,
        "enrichment_status": "succeeded",
        "review_status": "accepted",
    }
    values.update(overrides)
    return KbImageAsset(**values)


def test_image_chunk_text_is_labeled_bounded_and_deterministic() -> None:
    asset = _accepted_asset(
        caption="  设备铭牌  ",
        ocr_text="编号 SN-0912",
        surrounding_text="近邻" * 3_000,
    )

    content = image_chunk_content(asset)

    assert content.startswith("[Caption]\n设备铭牌\n\n[OCR]\n编号 SN-0912")
    assert "\n\n[Surrounding text]\n" in content
    assert len(content.split("[Surrounding text]\n", 1)[1]) == 4_000
    snapshot_id = uuid4()
    assert image_retrieval_chunk_id(
        snapshot_id, asset.id
    ) == image_retrieval_chunk_id(snapshot_id, asset.id)
    assert image_retrieval_chunk_id(
        snapshot_id, asset.id
    ) != image_retrieval_chunk_id(snapshot_id, uuid4())


def test_image_hit_requires_active_accepted_asset_and_projects_bounded_media() -> None:
    snapshot_id = uuid4()
    asset = _accepted_asset()
    revision = DocumentRevision(
        id=asset.revision_id,
        org_id=asset.org_id,
        kb_id=asset.kb_id,
        doc_id=asset.doc_id,
        revision_no=1,
        sha256="a" * 64,
        original_object_key="private/source.pdf",
    )
    document = SourceDocument(
        id=asset.doc_id,
        org_id=asset.org_id,
        kb_id=asset.kb_id,
        filename="source.pdf",
        object_key="private/source.pdf",
        sha256="a" * 64,
        doc_type="general",
    )
    chunk = KbChunk(
        id=image_retrieval_chunk_id(snapshot_id, asset.id),
        org_id=asset.org_id,
        kb_id=asset.kb_id,
        revision_id=asset.revision_id,
        snapshot_id=snapshot_id,
        source_doc_id=asset.doc_id,
        content_kind="image",
        image_asset_id=asset.id,
        content=image_chunk_content(asset),
        quarantined=False,
        meta={IMAGE_SNAPSHOT_META_KEY: image_chunk_snapshot_meta(asset)},
    )
    kb = KnowledgeBase(
        id=asset.kb_id,
        org_id=asset.org_id,
        name="media",
        active_snapshot_id=snapshot_id,
    )
    hit = SearchHit(
        kind="chunk",
        layer="tenant",
        kb_id=str(asset.kb_id),
        source=f"kb-image/{asset.id}",
        confidence=0.5,
        data={
            "id": str(chunk.id),
            "content_kind": "image",
            "image_asset_id": str(asset.id),
            "source_doc_id": str(asset.doc_id),
            "revision_id": str(asset.revision_id),
            "snapshot_id": str(snapshot_id),
        },
    )

    assert _image_chunk_matches(hit, chunk, revision, document, asset, kb)
    citation, media = _image_source_media(chunk, revision, asset)
    assert citation["kind"] == "image_source"
    assert citation["asset_id"] == str(asset.id)
    assert citation["source_sha256"] == "a" * 64
    assert citation["image_sha256"] == "b" * 64
    assert citation["page"] == 2
    assert citation["slide"] is None
    assert citation["bbox"] == {
        "left": 0.1,
        "top": 0.2,
        "right": 0.8,
        "bottom": 0.9,
    }
    assert media.thumbnail_url.endswith(f"?snapshot_id={snapshot_id}")
    assert media.content_url.endswith(f"?snapshot_id={snapshot_id}")
    assert "object_key" not in media.model_dump_json()
    unanchored_citation, _unanchored_media = _image_source_media(
        chunk,
        revision,
        asset.model_copy(update={"page": None, "slide": None, "bbox": None}),
    )
    assert unanchored_citation["page"] is None
    assert unanchored_citation["slide"] is None
    assert unanchored_citation["bbox"] is None
    assert not _image_chunk_matches(
        hit,
        chunk,
        revision,
        document,
        asset,
        kb.model_copy(update={"active_snapshot_id": uuid4()}),
    )
    assert not _image_chunk_matches(
        hit,
        chunk,
        revision,
        document.model_copy(update={"lifecycle_status": "withdrawal_pending"}),
        asset,
        kb,
    )
    assert not _image_chunk_matches(
        hit,
        chunk,
        revision.model_copy(update={"status": "tombstoned"}),
        document,
        asset,
        kb,
    )
    changed_asset = asset.model_copy(
        update={"caption": "后来修改的描述", "review_status": "rejected"}
    )
    assert _image_chunk_matches(
        hit,
        chunk,
        revision,
        document,
        changed_asset,
        kb,
    )
    assert _image_source_media(chunk, revision, changed_asset) == (citation, media)
