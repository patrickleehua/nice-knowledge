"""SQLModel 表聚合器:仅供 alembic autogenerate 与测试建表使用。

运行时代码一律从具体模块导入(nicekit.models.tenancy / .llm / ...),
不要 import 本包顶层——避免"触碰任何模型即拖动全库 schema"的 TF 旧问题
(TF backend/app/models/__init__.py 捆绑 99 张表的教训)。

新增表模型文件后必须在此登记,否则 alembic autogenerate 看不到。
chat 与 agent_permission 有交叉引用,必须成对登记。
"""

from sqlmodel import SQLModel

from nicekit.models import agent_permission as agent_permission  # noqa: F401
from nicekit.models import chat as chat  # noqa: F401
from nicekit.models import icron as icron  # noqa: F401
from nicekit.models import kb as kb  # noqa: F401
from nicekit.models import kb_feedback as kb_feedback  # noqa: F401
from nicekit.models import llm as llm  # noqa: F401
from nicekit.models import llm_provider as llm_provider  # noqa: F401
from nicekit.models import mcp as mcp  # noqa: F401
from nicekit.models import memory as memory  # noqa: F401
from nicekit.models import service_config as service_config  # noqa: F401
from nicekit.models import tenancy as tenancy  # noqa: F401

metadata = SQLModel.metadata
