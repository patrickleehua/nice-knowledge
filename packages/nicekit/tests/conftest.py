"""最小 conftest:P0 core 单测全部离线(不碰真实数据库 / redis / minio)。

Windows 下先切 selector event loop(psycopg async 不支持 Proactor),
与运行时入口保持同一姿态。
"""

from nicekit.core.event_loop import use_selector_event_loop_on_windows

use_selector_event_loop_on_windows()
