"""demo 后端冒烟脚本:登录 → 逐个打通用端点,打印状态码与响应摘要。

用法(后端已跑起来之后;端口非 8000 时用 DEMO_BASE 覆盖):
    uv run --package nicekit-demo-backend python smoke.py
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.getenv("DEMO_BASE", "http://127.0.0.1:8000")
EMAIL = "admin@demo.example.com"
PASSWORD = "demo-admin-2026"


def _brief(payload: object, limit: int = 220) -> str:
    text = json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    failures: list[str] = []
    with httpx.Client(base_url=BASE, timeout=30.0) as client:
        checks: list[tuple[str, str, int]] = []

        r = client.get("/api/v1/health")
        checks.append(("GET /api/v1/health", _brief(r.json()), r.status_code))

        r = client.get("/metrics")
        first = r.text.splitlines()[0] if r.text else ""
        checks.append(("GET /metrics", f"{len(r.text)} bytes; {first}", r.status_code))

        r = client.post(
            "/api/v1/auth/login",
            json={"email": EMAIL, "password": PASSWORD, "org_slug": "platform"},
        )
        checks.append(
            (
                "POST /api/v1/auth/login (platform)",
                _brief({k: ("<token>" if "token" in k else v) for k, v in r.json().items()}),
                r.status_code,
            )
        )
        token = r.json().get("access_token", "") if r.status_code == 200 else ""
        if not token:
            print(f"[FAIL] {r.status_code} 登录失败,后续端点无法验证:{_brief(r.text)}")
            return 1
        headers = {"authorization": f"Bearer {token}"}

        for method, path in (
            ("GET", "/api/v1/kb/bases"),
            ("GET", "/api/v1/chat/sessions"),
            ("GET", "/api/v1/admin/agent-cards"),
            ("GET", "/api/v1/admin/agent-tools"),
            ("GET", "/api/v1/admin/orgs"),
            ("GET", "/api/v1/admin/service-configs"),
            ("GET", "/api/v1/admin/model-route-tasks"),
            ("GET", "/api/v1/kb/entity-types"),
            ("GET", "/api/v1/notifications"),
            ("GET", "/api/v1/ready"),
        ):
            r = client.request(method, path, headers=headers)
            checks.append((f"{method} {path}", _brief(r.json()), r.status_code))

        for label, body, code in checks:
            ok = code in (200, 201)
            if not ok:
                failures.append(f"{label} -> {code}")
            print(f"[{'ok ' if ok else 'FAIL'}] {code} {label}\n      {body}")

    if failures:
        print("\n失败项:", failures)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
