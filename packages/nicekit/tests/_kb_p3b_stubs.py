"""P3b 未搬模块的测试替身(MIGRATION-PLAN §5.5)。

P3a 只搬到模型层 + 摄入链;`nicekit.kb.ingestion` 对实体类型/实体绑定/证据定位/
图片富化/wiki 生成五条支线走**函数内延迟 import**(见 ingestion.py 文件头清单)。
这里把这些模块以最小替身注入 sys.modules,让摄入链的主干路径可以在 P3a 阶段
被单测覆盖;P3b 把真实模块搬进来后,删掉对应 stub 即可。
"""

import sys
import types
from unittest.mock import AsyncMock


class StubEntityValidationError(Exception):
    """替身:nicekit.kb.entity_types.EntityValidationError。"""


def install_entity_types_stub(
    monkeypatch,
    *,
    entity_type: object | None = None,
    validate=None,
) -> types.ModuleType:
    """注入 nicekit.kb.entity_types 替身(get_entity_type / 属性校验)。"""
    module = types.ModuleType("nicekit.kb.entity_types")
    module.EntityValidationError = StubEntityValidationError
    module.get_entity_type = AsyncMock(return_value=entity_type)
    module.validate_entity_attributes = validate or (lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicekit.kb.entity_types", module)
    return module


def install_evidence_locator_stub(monkeypatch, *, locate=None) -> types.ModuleType:
    """注入 nicekit.kb.evidence_locator 替身(证据定位)。"""

    class StubEvidenceNotFoundError(Exception):
        pass

    module = types.ModuleType("nicekit.kb.evidence_locator")
    module.EvidenceNotFoundError = StubEvidenceNotFoundError
    module.locate_evidence = locate or (lambda *_args, **_kwargs: None)
    monkeypatch.setitem(sys.modules, "nicekit.kb.evidence_locator", module)
    return module
