# NiceKit 发版手册

本文只讲 **`nicekit` 这一个包怎么发到 PyPI**。

仓库是 uv workspace 单仓,三个成员里只有 `packages/nicekit` 会被发布:

| 包 | 路径 | 是否发布 |
| --- | --- | --- |
| `nicekit` | `packages/nicekit` | ✅ 发到 PyPI |
| `nicekit-demo-backend` | `apps/demo/backend` | ❌ 仅示例宿主,`Private :: Do Not Upload` |
| `nice-knowledge` | 仓库根 | ❌ `package = false`,只做 workspace 聚合 |

---

## 1. 版本号规范

遵循 [SemVer](https://semver.org/lang/zh-CN/),格式 `MAJOR.MINOR.PATCH`。

当前处于 **0.x 阶段**,含义要说清楚:

- `0.x` 表示公共 API **还没有稳定性承诺**。按 SemVer 的定义,`0.y.z` 阶段任何一次 `y` 的递增都可以包含破坏性变更。
- 实际操作约定:
  - **破坏性变更**(删/改公共 API、改数据库迁移语义、改配置项名称)→ 提升 `y`,如 `0.1.0` → `0.2.0`。
  - **新增功能且向后兼容** → 也提升 `y`(0.x 阶段不区分 minor / major)。
  - **纯修复、不改接口** → 提升 `z`,如 `0.1.0` → `0.1.1`。
- 预发布用 `0.2.0rc1` 这类 PEP 440 后缀,只发 TestPyPI 或作为 pre-release 发正式源。
- 什么时候升 `1.0.0`:公共 API(`nicekit.api` 路由契约 + SDK 装配入口 + 配置项)冻结、迁移链稳定、有明确的弃用流程之后。

### 版本号真源

版本号**只在 `packages/nicekit/pyproject.toml` 的 `[project].version` 里写一次**。

`packages/nicekit/src/nicekit/__init__.py` 里的 `__version__` 通过
`importlib.metadata.version("nicekit")` 动态读取安装元数据,不要在代码里再手写一份:

```python
from nicekit import __version__
```

未安装的源码树(直接把 `src/` 塞进 `sys.path`)会回落到 `0.0.0.dev0`。

---

## 2. 发版前检查清单

按顺序过一遍,任何一步不过就不要打 tag。

- [ ] `git status` 干净,当前分支就是要发的那个提交。
- [ ] `packages/nicekit/pyproject.toml` 的 `version` 已经改成目标版本,且**没有**其他地方重复写版本号。
- [ ] `uv sync --all-packages` 能过,`uv.lock` 已随代码提交。
- [ ] `uv run ruff check .` 全绿。
- [ ] `cd packages/nicekit && uv run --package nicekit pytest -q` 全绿(默认已 `-m 'not live'`,不需要外部服务)。
- [ ] `uv build --package nicekit` 成功,`uvx twine check dist/*` 两个包都 PASSED。
- [ ] 抽查 wheel 内容,确认非 `.py` 资源都在(见下方"产物自检")。
- [ ] `packages/nicekit/README.md` 的内容是最新的 —— PyPI 项目页直接渲染它。
- [ ] `LICENSE` / `NOTICE` 在仓库根和 `packages/nicekit/` 下**都存在且内容一致**(打包时取的是包目录下那份)。
- [ ] 目标版本号在 PyPI 上还没被占用(PyPI 不允许覆盖同版本号,发错只能 yank + 换号重发)。

### 产物自检

```bash
uv build --package nicekit
cd dist
unzip -l nicekit-*-py3-none-any.whl | grep -E "prompts/resources|migrations/|py.typed|dist-info"
unzip -p nicekit-*-py3-none-any.whl nicekit-*.dist-info/METADATA | grep -vE "^Requires-Dist"
```

必须看到:

- `nicekit/agent/prompts/resources/*.md`(7 个 prompt 模板)
- `nicekit/migrations/`(含 `script.py.mako`、`versions/*.py`)
- `nicekit/py.typed`
- `nicekit-<ver>.dist-info/licenses/LICENSE` 与 `.../licenses/NOTICE`
- METADATA 里 `License-Expression: Apache-2.0`、5 条 `Project-URL`、`Typing :: Typed` 等 classifier

> 注:`packages/nicekit/alembic.ini` **不会**进 wheel(它在包目录而不在 `src/nicekit/` 里,且
> `script_location`/`prepend_sys_path` 写的是开发仓相对路径,对下游没有意义)。它只随 sdist 分发。
>
> 下游用户自己写一份 `alembic.ini`,用 alembic 的**包资源语法**指向已安装的迁移目录 ——
> 不要写绝对路径:
>
> ```ini
> [alembic]
> script_location = nicekit:migrations
> # 不写 sqlalchemy.url:env.py 从 Settings.migration_database_url 读
> ```
>
> 已实测:干净 venv 里 `pip install nicekit` 后,该配置解析到
> `<venv>/Lib/site-packages/nicekit/migrations`,`alembic heads` 返回 `f4c2a8e19d63`(5 个 revision)。

---

## 3. 在 PyPI 配置 Trusted Publishing(只做一次)

Trusted Publishing 用 GitHub OIDC 换取一次性上传凭据,**仓库里不需要存任何 API token**。

### 3.1 正式 PyPI

1. 登录 https://pypi.org/ → 右上角账号菜单 → **Your projects**。
2. 如果 `nicekit` 还从没发布过,走 **Publishing → Add a new pending publisher**(pending publisher 允许首次发布就用 OIDC,不必先手动传一版)。
3. 表单填:

   | 字段 | 值 |
   | --- | --- |
   | PyPI Project Name | `nicekit` |
   | Owner | `patrickleehua` |
   | Repository name | `nice-knowledge` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

4. 在 GitHub 仓库 **Settings → Environments** 新建环境 `pypi`。建议同时开 **Required reviewers**(把自己加上),这样每次发正式包都要人工点一次确认。

### 3.2 TestPyPI

在 https://test.pypi.org/ 重复上面的步骤,唯一区别是 Environment name 填 `testpypi`,并在 GitHub 里另建一个 `testpypi` 环境(不需要审批)。

### 3.3 校验点

- workflow 文件名必须**恰好**是 `release.yml`(PyPI 是按文件名匹配的,改名就要回 PyPI 改配置)。
- publish job 必须声明 `permissions: id-token: write`,否则拿不到 OIDC token。
- publish job 的 `environment:` 必须和 PyPI 上填的 Environment name 完全一致。

---

## 4. 打 tag 正式发布

```bash
# 1. 改版本号
#    编辑 packages/nicekit/pyproject.toml -> [project] version = "0.2.0"

# 2. 同步 lock 并提交
uv sync --all-packages
git add -A
git commit -m 'update:版本号升到 0.2.0'

# 3. 打 tag(tag 名 = v + 版本号)
git tag -a v0.2.0 -m 'nicekit 0.2.0'

# 4. 推代码 + 推 tag
git push origin main
git push origin v0.2.0
```

推 tag 后 `Release` workflow 自动触发:

1. `build` job:checkout → setup-uv → `uv build --package nicekit` → `uvx twine check dist/*` → 上传 artifact。
2. `publish` job:下载 artifact → `pypa/gh-action-pypi-publish` 用 OIDC 发到 PyPI。
   如果 `pypi` 环境配了 Required reviewers,这一步会卡在等待审批,去 Actions 页面点 **Review deployments → Approve**。

发完去 https://pypi.org/project/nicekit/ 确认版本、README 渲染、License 显示都对。

---

## 5. TestPyPI 演练

正式发之前建议先在 TestPyPI 走一遍完整链路。

1. GitHub → **Actions → Release → Run workflow**。
2. `发布目标` 选 `testpypi`,Run。
3. workflow 会跑 `build` + `publish-testpypi`,把包传到 https://test.pypi.org/project/nicekit/。

演练要注意的点:

- TestPyPI 上的版本号也不能重复。演练建议用 `0.2.0rc1` 这种预发布号,别把正式号烧掉。
  workflow 里已经开了 `skip-existing: true`,重复上传同一文件不会让 job 失败,但也**不会**覆盖。
- 从 TestPyPI 装包时要指定 extra-index,否则依赖装不上(nicekit 的几十个依赖只在正式 PyPI 上):

  ```bash
  uv pip install --index-url https://test.pypi.org/simple/ \
                 --extra-index-url https://pypi.org/simple/ \
                 nicekit==0.2.0rc1
  ```

---

## 6. 手动发布兜底

CI 挂了、或者要在没有 GitHub Actions 的情况下应急发版:

```bash
cd /path/to/nice-knowledge

# 1. 干净构建
rm -rf dist
uv build --package nicekit
uvx twine check dist/*

# 2. 发到 TestPyPI(先演练)
uv publish --publish-url https://test.pypi.org/legacy/ --token pypi-<TestPyPI token>

# 3. 发到正式 PyPI
uv publish --token pypi-<PyPI token>
```

token 在 PyPI **Account settings → API tokens** 生成,scope 尽量限定到 `nicekit` 这一个项目。
token 只在生成时可见一次,不要写进仓库、不要贴进 issue;用完建议直接吊销,常态路径还是走 Trusted Publishing。

`uv publish` 也读环境变量 `UV_PUBLISH_TOKEN`,可以避免 token 出现在 shell history 里:

```bash
export UV_PUBLISH_TOKEN='pypi-...'
uv publish
```

---

## 7. 发布后验证

在一个**干净的临时 venv** 里从 PyPI 装一次,做 import 冒烟:

```bash
cd $(mktemp -d)
uv venv --python 3.13
source .venv/bin/activate      # Windows Git Bash: source .venv/Scripts/activate

uv pip install nicekit

python - <<'PY'
import nicekit
from importlib.resources import files

print("version:", nicekit.__version__)

# 打包资源是否真的进来了
res = files("nicekit.agent.prompts") / "resources"
mds = sorted(p.name for p in res.iterdir() if p.name.endswith(".md"))
print("prompt templates:", len(mds), mds)

mig = files("nicekit") / "migrations" / "versions"
print("migrations:", sorted(p.name for p in mig.iterdir() if p.name.endswith(".py")))

# py.typed
print("py.typed:", (files("nicekit") / "py.typed").is_file())
PY
```

期望输出:版本号与 tag 一致、7 个 `.md` 模板、5 个迁移脚本、`py.typed: True`。

再顺手确认:

- https://pypi.org/project/nicekit/ 的 README 渲染正常(中文不乱码、代码块正常)。
- 项目页左侧有 Homepage / Repository / Documentation / Issues / Changelog 五个链接。
- License 显示 `Apache-2.0`。

---

## 8. 已知边界(装完不能空跑)

`pip install nicekit` 只装了 Python 代码,**跑不起来一个可用系统**。NiceKit 是平台 SDK,强依赖以下基础设施:

| 依赖 | 用途 | 备注 |
| --- | --- | --- |
| **PostgreSQL** | 主库 + 多租户 RLS | 必须装扩展:`pgvector`(向量检索)、`pg_trgm`(模糊匹配)、`zhparser`(中文全文分词)。缺任何一个,`alembic upgrade head` 或检索链路都会失败 |
| **Redis** | 缓存 / Celery broker / 会话状态 | |
| **S3 兼容对象存储** | 文档与媒体原文存储 | MinIO 或任意 S3 兼容服务 |
| **LLM / Embedding provider** | Agent 与知识库能力 | OpenAI / Anthropic 兼容端点,需要 API key |

另外:

- 迁移不会自动执行。装完必须自己写 `alembic.ini`(`script_location = nicekit:migrations`)再跑
  `alembic upgrade head`,详见 §2 产物自检下方的注。
- `zhparser` 不在多数 PostgreSQL 官方镜像里,需要自行编译或用带扩展的镜像;仓库根的 `docker-compose.yml` 给了一套可直接起的开发环境。
- 依赖体积不小(含 `docling`、`onnxruntime` 等),首次安装耗时较长;裸机安装前先确认磁盘和网络。
- 仓库里的 `apps/demo`(Nice Knowledge)是"怎么把 SDK 装配成可运行系统"的参考实现,不随 PyPI 包分发。

因此 PyPI 页面上的定位是 **SDK / 框架**,不是开箱即用的应用。评估者应当先按仓库 README 起 `docker-compose`,再谈 `pip install`。
