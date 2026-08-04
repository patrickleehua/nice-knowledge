"""NiceKit:多租户 Agent + 知识库平台 SDK。

版本号只有一个真源 —— `pyproject.toml` 的 `[project].version`。
这里通过安装元数据动态读取,避免两处手写版本号漂移;
源码树里未安装(例如直接把 src/ 塞进 sys.path)时回落到 "0.0.0.dev0"。
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("nicekit")
except PackageNotFoundError:  # pragma: no cover - 仅未安装的源码树会走到
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
