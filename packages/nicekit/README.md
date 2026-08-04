<div align="center">

<img src="https://raw.githubusercontent.com/patrickleehua/nice-knowledge/main/images/nicekit.png" alt="NiceKit" width="120" />

# NiceKit

**A multi-tenant Agent + Knowledge Base platform SDK for Python**

[![PyPI](https://img.shields.io/pypi/v/nicekit.svg)](https://pypi.org/project/nicekit/)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/patrickleehua/nice-knowledge/blob/main/LICENSE)

</div>

NiceKit is a **platform framework package**, not a utility library. It was extracted from a
production enterprise AI application. Install it, wire it up, and your project gets a multi-tenant
foundation, an LLM routing layer, an agent runtime, a knowledge base and operational
observability — **228 REST endpoints** across 182 paths, **64 tables**, **46 of them under
PostgreSQL `FORCE ROW LEVEL SECURITY`**.

```bash
pip install nicekit
```

```python
from nicekit.api.v1.router import default_routers
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports

install_default_ports()
app = create_app(routers=default_routers())
```

That is a running platform with authentication, model configuration, knowledge bases and agent
chat — provided the infrastructure below is in place.

## Requirements

Hard dependencies, none of them substitutable:

- **Python 3.13+**
- **PostgreSQL** with `pgvector`, `pg_trgm`, `zhparser` and `pgcrypto`
- **Redis**
- **S3-compatible object storage**
- Celery is optional — there is an inline dispatch fallback

> `pip install nicekit` alone will not give you a running system. The
> [repository](https://github.com/patrickleehua/nice-knowledge) ships a `docker compose` setup and a
> custom PostgreSQL image with the three extensions pre-built; the official `postgres` image will
> fail migrations.

## What you get

| Area | What you get |
|---|---|
| **Multi-tenancy** | Orgs / users / memberships / invitations, JWT + rotating refresh tokens, PostgreSQL `FORCE ROW LEVEL SECURITY` (binds even the table owner; policies evaluate false when no context is set), three integration modes |
| **LLM layer** | OpenAI / Anthropic dual-protocol normalization, model capability registry, model-ID normalization, routing and fallback chains, provider cooldown, per-org budget and concurrency gates, four-bucket token metering |
| **Agent runtime** | Multi-turn tool loop, seven-axis permission approval, AI reviewer, MCP client, `SKILL.md` skills, sub-agent delegation, long-term memory, session-goal continuation, context compression, scheduled tasks that start a real agent run |
| **Knowledge base** | Ingestion (Docling layout understanding, structure-aware chunking, contextualization, embedding), generic entity extraction and review, snapshot publish/rollback, four-channel hybrid retrieval (structured + sparse + dense + graph, fused with RRF), knowledge graph, wiki generation, media review, prompt-injection guardrails |
| **Operations** | Health/readiness probes, Prometheus metrics, service heartbeats, provider connectivity probes, orphan task recovery, stale sweeps, transactional outbox |
| **API** | 18 routers and 228 endpoints under `/api/v1` |

**Deliberately not included:** any domain model. No orders, tickets, projects or customers — you
define those and plug them in through extension points. NiceKit also does not abstract over the
database (PostgreSQL is required by design) and ships no industry vocabularies.

## Bridging your existing identity system

Authentication converges on a single dependency. Replace the `PrincipalResolver` behind it and the
SDK plugs into whatever account system you already have — you change assembly code, not the data
model.

```python
from nicekit.api.deps import Principal, set_principal_resolver
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers
from nicekit.tenancy import subject_uuid, tenant_uuid

async def resolver(request) -> Principal:
    claims = verify_host_token(request.headers["authorization"])
    return Principal(
        org_id=tenant_uuid(claims["company_id"]),   # external key -> UUID, deterministic, no mapping table
        user_id=subject_uuid(claims["user_id"]),
        role=ROLE_MAP[claims["role"]],
    )

set_principal_resolver(resolver)                    # must happen before create_app
app = create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES))
```

Three modes are supported: **managed** (SDK owns identity), **bridged** (your app owns it) and
**single-tenant** (no tenant concept, or auth at the gateway).

## Extension points

The SDK never imports host code. Everything domain-specific goes through registration interfaces —
custom tools with seven-axis permissions, resource resolvers, context providers, entity types,
notifiers, trace/usage sinks, roles, scheduled tasks and more.

```python
from nicekit.agent.tools import ToolContext, ToolRegistry, tool_permission
from nicekit.domain.agent_permission import (
    PermissionScope, ToolCategory, ToolDelegation, ToolEffect, ToolReversibility, ToolRisk,
)

my_tools = ToolRegistry("myapp")

@my_tools.register(
    "ticket_close",
    "Close a ticket",
    {"type": "object", "properties": {"ticket_id": {"type": "string"}},
     "required": ["ticket_id"], "additionalProperties": False},
    permission=tool_permission(
        effect=ToolEffect.TRANSITION,
        risk=ToolRisk.SENSITIVE,
        categories=(ToolCategory.WORKFLOW,),
        reversibility=ToolReversibility.COMPENSATABLE,
        delegation=ToolDelegation.REVIEWABLE,     # requires AI review or human approval
        scope=PermissionScope.RESOURCE,
        material_arguments=("ticket_id",),
    ),
)
async def ticket_close(ctx: ToolContext, args: dict) -> dict:
    ...

app = create_app(routers=default_routers(), tool_registry=my_tools)
```

## Documentation

Full documentation lives in the repository. It is written in Chinese; the repository also has an
[English overview](https://github.com/patrickleehua/nice-knowledge/blob/main/README.en.md).

- [Documentation index](https://github.com/patrickleehua/nice-knowledge/blob/main/docs/README.md)
- [SDK guide](https://github.com/patrickleehua/nice-knowledge/blob/main/docs/SDK-GUIDE.md) — 30-minute start, all extension points, common recipes
- [Integration guide](https://github.com/patrickleehua/nice-knowledge/blob/main/docs/INTEGRATION.md) — bridging an existing tenancy and auth system
- [Agent brief](https://github.com/patrickleehua/nice-knowledge/blob/main/docs/AGENT-BRIEF.md) — structured, copy-pasteable facts written for AI coding assistants
- [Data model](https://github.com/patrickleehua/nice-knowledge/blob/main/docs/DATA-MODEL.md) — all 64 tables and the RLS mechanism

## Status

`0.1.0`. **The public API is not frozen.** During 0.x, minor versions may carry breaking changes —
read the [changelog](https://github.com/patrickleehua/nice-knowledge/blob/main/CHANGELOG.md)
before upgrading.

## License

[Apache License 2.0](https://github.com/patrickleehua/nice-knowledge/blob/main/LICENSE)
© 2026 patrickleehua
