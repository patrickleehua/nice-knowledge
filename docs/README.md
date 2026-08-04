# NiceKit 文档索引

全部文档都在本目录。下表按**你想干什么**分类,不按文档大小排。

## 我第一次来

| 顺序 | 文档 | 读完你会 |
|---|---|---|
| 1 | [../README.md](../README.md) | 知道 NiceKit 是什么、能力边界在哪、要不要用它 |
| 2 | [SDK-GUIDE.md](SDK-GUIDE.md) | 三十分钟起步:装依赖 → 起基础设施 → 建表 → 最小宿主 → 启动 |
| 3 | [../apps/demo/backend/README.md](../apps/demo/backend/README.md) | 把示例宿主真的跑起来,看四个扩展点的最小实现 |

## 我要把它接进现有系统

| 文档 | 用途 |
|---|---|
| [INTEGRATION.md](INTEGRATION.md) | **租户接入完整方案**。三种模式(全托管 / 桥接 / 单租户)的判据、可复制装配代码、RLS 边界、常见坑、上线前自检 |
| [AGENT-BRIEF.md](AGENT-BRIEF.md) | 上文的**结构化速查版**:只给签名、契约、执行步骤表、"不要这样做"清单。给 AI 编码助手照抄用,人也能查 |
| [DATA-MODEL.md](DATA-MODEL.md) | 64 张表逐表详解、RLS 机制、三种接入模式下的表使用矩阵、已知设计疑点 |

## 我要扩展它

| 我想… | 去哪 |
|---|---|
| 加一个 agent 能调的业务动作 | [SDK-GUIDE.md §4.1](SDK-GUIDE.md#41-让-agent-会做你的业务自定义工具) 自定义工具 + [§4.2](SDK-GUIDE.md#42-让权限层看懂你的业务对象resourceresolver) ResourceResolver |
| 让知识库认识我的领域对象 | [SDK-GUIDE.md §4.5](SDK-GUIDE.md#45-kb-认得你的领域实体实体类型注册) 实体类型注册(不用建表) |
| 给 agent 注入业务上下文 | [SDK-GUIDE.md §4.3](SDK-GUIDE.md#43-给-agent-注入业务上下文contextprovider) ContextProvider |
| 换通知渠道 / 换 trace 落账 / 换检索 port | [SDK-GUIDE.md §4.6](SDK-GUIDE.md#46-其余扩展点速查) 扩展点速查表 |
| 加自己的业务表 | [SDK-GUIDE.md §5](SDK-GUIDE.md#5-数据库与迁移) 数据库与迁移 |

## 我要维护这个仓库

| 文档 | 用途 |
|---|---|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 开发环境、代码规范、测试、PR 流程 |
| [RELEASING.md](RELEASING.md) | 版本号规范、PyPI Trusted Publishing 配置、发版步骤、发布后验证 |
| [../CHANGELOG.md](../CHANGELOG.md) | 版本变更记录 |
| [MIGRATION-PLAN.md](MIGRATION-PLAN.md) | 历史存档:这套代码从 TravelFlow AI 抽取而来的迁移蓝图与逐模块处置。**不是使用文档**,只在追溯设计动机时看 |

## 我是 AI 编码助手

按这个顺序读,不要通读全部:

1. [../AGENTS.md](../AGENTS.md) —— 仓库约定、命令、红线。**先读这个。**
2. [AGENT-BRIEF.md](AGENT-BRIEF.md) —— 接入 SDK 的全部结构化事实:import 路径、函数签名、
   执行步骤表、反模式清单。**照抄这里的代码,不要凭记忆写。**
3. 只在需要时按主题跳读:
   - 要建表 / 查字段 / 判断某张表用不用得到 → [DATA-MODEL.md](DATA-MODEL.md)
   - 要写扩展点 → [SDK-GUIDE.md §4](SDK-GUIDE.md#4-扩展点全清单)
   - 要判断该选哪种租户模式 → [INTEGRATION.md §0](INTEGRATION.md#0-我该选哪种模式)

> **文档里的数字都是核实过的,不是估算。** 端点数、表数、RLS 策略数取自实际代码与
> `psql` 系统目录查询。如果你的改动让某个数字变了,请连带改文档 ——
> 对照 [../CONTRIBUTING.md](../CONTRIBUTING.md#文档) 的同步表。

## 其他目录

| 路径 | 内容 |
|---|---|
| [graph-eval-corpus/](graph-eval-corpus/) | 为验证「图谱检索通道该不该默认开」专门构造的评测语料:六篇虚构技术文档 + `cases.json` 用例集。设计动机与实测结论见该目录的 README |
