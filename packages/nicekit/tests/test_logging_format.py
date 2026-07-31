"""结构化日志单测(迁移自 TF):JSON/console formatter + request_id contextvar。"""

import json
import logging

from nicekit.core.logging import (
    ConsoleFormatter,
    JsonFormatter,
    request_id_var,
)


def _record(msg: str, **extra) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="nicekit.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_emits_single_line_json() -> None:
    out = JsonFormatter().format(_record("你好", org_id="org-1", count=3))
    assert "\n" not in out
    assert "你好" in out  # 中文不转义为 \uXXXX
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "nicekit.test"
    assert payload["message"] == "你好"
    assert payload["org_id"] == "org-1"
    assert payload["count"] == 3


def test_json_formatter_includes_request_id() -> None:
    token = request_id_var.set("req-abc")
    try:
        payload = json.loads(JsonFormatter().format(_record("m")))
        assert payload["request_id"] == "req-abc"
    finally:
        request_id_var.reset(token)


def test_json_formatter_no_request_id_when_unset() -> None:
    # 确保上下文干净(其他测试可能设过)
    request_id_var.set(None)
    payload = json.loads(JsonFormatter().format(_record("m")))
    assert "request_id" not in payload


def test_console_formatter_readable_with_extras() -> None:
    request_id_var.set(None)
    out = ConsoleFormatter().format(_record("渲染完成", org_id="o1"))
    assert "INFO" in out
    assert "nicekit.test" in out
    assert "渲染完成" in out
    assert "org_id=o1" in out


def test_json_formatter_serializes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            name="nicekit.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="失败", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(rec))
    assert "boom" in payload["exc"]
