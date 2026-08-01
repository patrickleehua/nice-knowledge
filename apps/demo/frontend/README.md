# nicekit demo 前端

nicekit(多租户 Agent + 知识库平台 SDK)的可视化测试台。裁剪自 TravelFlow AI
的 Next.js 工作台,删掉全部旅游业务面,只保留 SDK 能力对应的界面。

## 起步

```bash
# 1) 依赖(pnpm;本目录自带 pnpm-workspace.yaml,独立于根的 uv workspace)
pnpm install

# 2) 环境变量(可选,默认就指向 8020)
cp .env.example .env.local

# 3) 起 demo 后端(另开一个终端)
cd ../backend
cp .env.example .env
uv run --package nicekit-demo-backend python -m demo_backend.seed
DEMO_PORT=8020 uv run python run.py

# 4) 起前端
pnpm dev     # http://localhost:3000
```

默认账号 `admin@demo.example.com` / `demo-admin-2026`,**登录必须填 org_slug**
(`platform` 或 `demo`)。

## 页面

| 路由 | 内容 |
|---|---|
| `/login` | 邮箱 + 密码 + org_slug |
| `/app/chat` | Agent 全交互:会话 / 工具卡 / 计划 / 审批 / 反问 / 权限 / 思考等级 / 模型 / 联网 / 输入队列 / 目标 |
| `/app/kb` | 知识检索 + AI 问答流 |
| `/app/icron` | 定时任务 |
| `/app/notifications` | 通知 |
| `/org/kb`, `/org/kb/[id]` | 知识库管理 + 9 视图工作台 |
| `/org/members` | 成员与角色 |
| `/org/settings/agent-permissions` | 组织级 Agent 权限策略 |
| `/org/settings/memory` | 长期记忆治理 |
| `/admin/*` | 平台管理端全区 |

`/app/chat` 支持把会话绑定到宿主的业务作用域:
`?scope_type=workspace&scope_id=<uuid>`(两者必须成对,只给一个后端 422)。

## 脚本

```bash
pnpm dev             # 开发服务器
pnpm build           # 生产构建(会跑完整 tsc,能暴露孤儿 import)
pnpm lint            # eslint(含 React Compiler 规则)
pnpm test            # node --test(*.test.mjs) + vitest(*.component.test.tsx)
```

## 给宿主的扩展点

前端这一层最重要的扩展点是**工具结果渲染器注册表**
(`src/components/agent/result-renderers.tsx`)。SDK 不认识宿主的业务工具,
也不该内置业务卡片,因此按工具名注册渲染器:

```tsx
import {
  registerToolResultRenderers,
  type ToolResultRendererProps,
} from "@/components/agent/result-renderers";

function TicketCard({ output }: ToolResultRendererProps) {
  return <div>工单 {String(output.id)} 已创建</div>;
}

// 模块加载期注册(全局),或用 <ToolResultRendererProvider value={…}> 就近注入
registerToolResultRenderers({ ticket_create: TicketCard });
```

未注册的工具走通用 JSON 渲染(`JsonResultRenderer`),不会白屏。

另外三处可选注册点:

- `components/agent/tool-presentation.ts` — `registerToolSummarizer(name, fn)`
  自定义工具卡的一行摘要;`registerAutoExpandTools(...names)` 让某些工具结果默认展开。
- `components/agent/agent-conversation.tsx` — `presets` / `emptyTitle` /
  `emptyDescription` 替换空态引导。
- `components/agent/command-input.tsx` — `presets` 替换 `/` 快捷指令表。

## 注意

Next.js 16 与训练数据有 breaking changes(Middleware 更名 Proxy、`cookies()` 异步
等)。改动前先查 `node_modules/next/dist/docs/`,见 `AGENTS.md`。
