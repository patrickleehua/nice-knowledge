<div align="center">

<img src="images/nicekit.png" alt="NiceKit" width="120" />

# NiceKit

**A multi-tenant Agent + Knowledge Base platform SDK for Python**

Tenant isolation, LLM routing, an agent runtime and hybrid retrieval — packaged so your
product starts with a production-grade AI foundation on day one.

[![PyPI](https://img.shields.io/pypi/v/nicekit.svg)](https://pypi.org/project/nicekit/)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![CI](https://github.com/patrickleehua/nice-knowledge/actions/workflows/ci.yml/badge.svg)](https://github.com/patrickleehua/nice-knowledge/actions/workflows/ci.yml)

[Quick start](#quick-start) · [Docs](docs/README.md) · [中文](README.md)

</div>

> **Note on language.** The full documentation set under [`docs/`](docs/) is written in Chinese.
> This page is a complete overview in English; deeper guides have not been translated yet.

---

## What it is

**NiceKit** is a **platform framework package**, not a utility library. It was extracted from a
production enterprise AI application. Install it, wire it up, and your project gets a multi-tenant
foundation, an LLM routing layer, an agent runtime, a knowledge base and operational
observability — **228 REST endpoints** across 182 paths, **64 tables**, **46 of them under
PostgreSQL `FORCE ROW LEVEL SECURITY`**.

**Nice Knowledge** is the demo host shipped in this repository: a knowledge workbench built on
NiceKit. It runs as a real product and doubles as living documentation for how to assemble the SDK.

### The problems it solves

When you build an AI product with a knowledge base, the time sink is never the LLM API. It's:

- Making multi-tenant data isolation **enforced by the database**, not by remembering
  `WHERE org_id = ?` in every query
- Deciding **who approves** an agent's side-effecting tool call, at what granularity, and whether
  it can be undone
- Understanding **why retrieval missed**, and refusing to answer when semantic neighbors don't
  constitute evidence
- Getting the **degradation paths** right — model swaps, embedding migrations, context budget
  overruns, provider outages

NiceKit has all four solved, and exposes each of them as an extension point you can replace.

### What it deliberately does not do

- **No domain models.** No orders, tickets, projects or customers — you define those and plug
  them in through extension points.
- **No database-agnostic abstraction.** PostgreSQL is a hard dependency, on purpose: RLS,
  pgvector and zhparser have no substitutes here.
- **No industry vocabularies.** Entity types, domain prompt copy and retrieval filter fields are
  all registered by the host. The SDK ships only domain-neutral fallbacks.
- **No frontend for you.** The demo frontend is a reference implementation to cut down, not a
  published npm package.

## Capabilities

| Area | What you get |
|---|---|
| **Multi-tenancy** | Orgs / users / memberships / invitations, JWT + rotating refresh tokens, PostgreSQL `FORCE ROW LEVEL SECURITY` (binds even the table owner; policies evaluate false when no context is set), three integration modes |
| **LLM layer** | OpenAI / Anthropic dual-protocol normalization, model capability registry, model-ID normalization (aggregator prefixes, Bedrock ARNs, quantization tags, date stamps), routing and fallback chains, provider cooldown, per-org budget and concurrency gates, four-bucket token metering |
| **Agent runtime** | Multi-turn tool loop, **seven-axis permission approval**, AI reviewer, MCP client, `SKILL.md` skills, sub-agent delegation, long-term memory, session-goal continuation, context compression with two-tier tool-result budgeting, scheduled tasks that start a real agent run |
| **Knowledge base** | Ingestion (Docling layout understanding, structure-aware chunking, contextualization, embedding), generic entity extraction and review, snapshot publish/rollback, **four-channel hybrid retrieval**, knowledge graph, wiki generation, media ingestion and review, prompt-injection guardrails |
| **Operations** | Health/readiness probes, Prometheus metrics, service heartbeats, provider connectivity probes, orphan task recovery, stale sweeps, transactional outbox |
| **API** | 18 routers and 228 endpoints under `/api/v1` (auth, members, admin, chat SSE, the whole KB family, memory, cron, notifications, media) |

<details>
<summary><b>Which four retrieval channels</b></summary>

`structured` (JSONB filtering over registered entity types), `sparse` (PostgreSQL FTS with
zhparser Chinese segmentation), `dense` (pgvector cosine, distance capped at 0.62) and `graph`
(1–2 hop recall over the active graph projection). The four are fused with RRF (`k=60`) and handed
to a cross-encoder reranker; if reranking is unavailable it degrades to plain RRF.

**Refusal gate:** when structured / sparse / must_include / graph are all empty and **only dense
hit**, the search returns nothing. Semantic proximity is not evidence, and the fix is not to loosen
the distance threshold.

</details>

<details>
<summary><b>Which seven permission axes</b></summary>

Tools declare seven dimensions via `tool_permission()`. The permission layer uses them to decide
whether a call runs automatically, goes to the AI reviewer, requires a human click, or is forbidden
to the agent entirely:

| Axis | Values |
|---|---|
| `effect` | `read` / `write` / `delete` / `transition` |
| `risk` | `routine` / `sensitive` / `critical` |
| `categories` | `local_data` / `network` / `external_cost` / `financial` / `destructive` / `workflow` / `export` |
| `reversibility` | `reversible` / `compensatable` / `irreversible` |
| `delegation` | `automatic` / `reviewable` / `user_required` / `agent_forbidden` |
| `scope` | `session` / `resource` / `organization` |
| `material_arguments` | Arguments that feed the authorization fingerprint and the approval card |

Bad metadata fails at **import time**, not at runtime.

</details>

## Install

```bash
pip install nicekit
# or
uv add nicekit
```

**Hard requirements:** Python 3.13+, PostgreSQL (with `pgvector`, `pg_trgm`, `zhparser`,
`pgcrypto`), Redis, S3-compatible object storage. Celery is optional (there's an inline fallback).

> `pip install` alone won't get you a running system — the four pieces of infrastructure above must
> be in place. The `docker compose` setup in this repository is the fastest path.

## Quick start

### Run the bundled demo

```bash
git clone https://github.com/patrickleehua/nice-knowledge.git
cd nice-knowledge

# 1) Infrastructure: postgres 5433 / redis 6380 / minio 9010
docker compose up -d

# 2) Migrate (64 tables, 46 with FORCE RLS, plus the Chinese text-search config)
cd packages/nicekit && uv run --package nicekit alembic upgrade head && cd ../..

# 3) Seed and run
cp apps/demo/backend/.env.example apps/demo/backend/.env
cd apps/demo/backend
uv run --package nicekit-demo-backend python -m demo_backend.seed
uv run --package nicekit-demo-backend python run.py
```

Default account `admin@demo.example.com` / `demo-admin-2026`; the login form requires an
`org_slug` (`platform` or `demo`). For the frontend, in another terminal:
`cd apps/demo/frontend && pnpm install && pnpm dev`.

> **Use the custom PostgreSQL image in `deploy/`.** The official `postgres` image lacks the three
> required extensions and migrations will fail outright.
>
> **On Windows, start through `run.py`**, never `uvicorn` directly. uvicorn hardcodes
> `ProactorEventLoop` on win32, which psycopg's async driver does not support. The symptom is
> HTTP responding normally while every database background task silently fails.

### Use it in your own project

```python
# main.py — a platform with auth, model configuration, knowledge bases and agent chat
from nicekit.api.v1.router import default_routers
from nicekit.runtime.app_factory import create_app
from nicekit.runtime.bootstrap import install_default_ports

install_default_ports()
app = create_app(routers=default_routers())
```

```python
# seed.py — idempotent, run once per deployment
from nicekit.core.db import get_session_factory
from nicekit.runtime.bootstrap import bootstrap_platform

async with get_session_factory()() as session:
    report = await bootstrap_platform(session)
    await session.commit()
```

## Bridging your existing identity system

Authentication converges on a **single dependency**, `get_org_context`. Replace the
`PrincipalResolver` behind it and the SDK plugs into whatever account system you already have —
you change assembly code, not the data model.

| | A. Managed | B. Bridged | C. Single-tenant |
|---|---|---|---|
| Fits | A new product using the SDK's tenancy | You already have tenants and login | No tenant concept, or auth happens at the gateway |
| Issues credentials | SDK (JWT + rotating refresh) | Your app | Gateway / none |
| `set_principal_resolver` | Not called | Required | Required |
| Mount `auth` / `members` routers | Yes | No | No |
| RLS | On | On | On |

```python
# Bridged mode: translate your token into a Principal
from nicekit.api.deps import Principal, set_principal_resolver
from nicekit.api.v1.router import AUTH_ROUTER_NAMES, default_routers
from nicekit.tenancy import subject_uuid, tenant_uuid

async def resolver(request: Request) -> Principal:
    claims = verify_host_token(request.headers["authorization"])   # verify locally
    return Principal(
        org_id=tenant_uuid(claims["company_id"]),   # external key -> UUID, derived deterministically
        user_id=subject_uuid(claims["user_id"]),
        role=ROLE_MAP[claims["role"]],
    )

set_principal_resolver(resolver)                    # must happen before create_app
app = create_app(routers=default_routers(exclude=AUTH_ROUTER_NAMES))
```

Full copy-pasteable recipes for all three modes, RLS boundaries, known pitfalls and a pre-launch
self-check script live in [docs/INTEGRATION.md](docs/INTEGRATION.md) (Chinese).

## Extension points

The SDK never imports host code. Everything domain-specific goes through these interfaces:

| I want to… | Extension point |
|---|---|
| Let the agent perform my business actions | `ToolRegistry.register(...)` with a seven-axis permission declaration |
| Make the permission layer understand my objects | `register_resource_resolver(...)` — resolve tool arguments into a scope root |
| Inject live business context into the agent | `register_context_provider(...)` — a block injected into the system prompt each turn (never persisted; dropped whole if over budget) |
| Teach the knowledge base my domain objects | `bootstrap_platform(entity_type_specs=[...])` — a spec is enough, **no tables to create** |
| Swap notifications / trace sinks / KB reference scanning | `set_notifier` / `TraceSink`, `UsageSink` / `register_reference_scanner` |
| Add a business role | `register_roles("editor")` + `register_write_roles("editor")` |
| Add my own tables | A separate alembic branch plus `op_enable_org_rls(op, "your_table")` |

All 44 registration interfaces are documented in
[docs/SDK-GUIDE.md §4](docs/SDK-GUIDE.md#4-扩展点全清单) (Chinese).

## Architecture

```text
core        config (per-domain) / db (RLS) / security + secretbox / cache / ratelimit / logging / metrics
  ↑
tenancy     orgs, users, memberships, invitations / JWT / role registry / usage metering / RLS migration helpers
  ↑
llm         dual-protocol providers / capability registry / ID normalization / routing & fallback / sinks
  ↑
kb ── agent ── capabilities        mutually independent; each depends only downward
  ↑
operations / runtime               observability / assembly, dispatch, reliability, celery
  ↑
api                                REST routers
```

```text
packages/nicekit/     The SDK package — this is what ships to PyPI
apps/demo/backend/    Nice Knowledge demo host + minimal implementations of four extension points
apps/demo/frontend/   Nice Knowledge demo frontend (Next.js)
deploy/               Custom PostgreSQL image (pgvector + pg_trgm + zhparser)
docs/                 All documentation
```

## Documentation

| Document | Read it when you want to |
|---|---|
| [docs/README.md](docs/README.md) | Find the right doc for your task — **start here** |
| [docs/SDK-GUIDE.md](docs/SDK-GUIDE.md) | Build a new project on NiceKit: 30-minute start, all extension points, common recipes |
| [docs/INTEGRATION.md](docs/INTEGRATION.md) | Bridge NiceKit into an existing tenancy and auth system |
| [docs/AGENT-BRIEF.md](docs/AGENT-BRIEF.md) | The structured, copy-pasteable version of the above — **written for AI coding assistants** |
| [docs/DATA-MODEL.md](docs/DATA-MODEL.md) | All 64 tables, the RLS mechanism, and which tables each mode actually uses |
| [AGENTS.md](AGENTS.md) | Work on **this repository** as an AI assistant: commands, constraints, hard limits |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, code standards, testing, PR flow |
| [docs/RELEASING.md](docs/RELEASING.md) | Release process |

## Project status

`0.1.0`. **The public API is not frozen.** During 0.x, minor versions may carry breaking changes —
read [CHANGELOG.md](CHANGELOG.md) before upgrading.

Tests: 135 test files. `cd packages/nicekit && uv run --package nicekit pytest` gives
**1967 passed / 4 skipped / 53 deselected in ~45s with no external services**. The 53 deselected
are marked `live` (they need a real PostgreSQL or LLM API); run them with `-m live`.

## Contributing

Issues and PRs are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first — especially
"the SDK never imports host code" and "every table with `org_id` must have RLS enabled".
Those two are hard constraints.

## License

[Apache License 2.0](LICENSE) © 2026 patrickleehua
