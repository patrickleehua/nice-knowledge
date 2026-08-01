"""capabilities:与领域无关的外部能力适配层(联网搜索 / 天气 / 图片生成 / 通知)。

各子包架构一致:`base.py` 定义契约 → provider 实现 → `service.py` 编排
(配置解析、故障转移、降级、计量)。任何 provider 不可用一律**如实降级**,
不抛异常阻塞 agent loop。
"""
