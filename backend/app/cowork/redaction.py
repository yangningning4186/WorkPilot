"""兼容 Cowork 旧导入路径；实现位于不依赖 service 层的 security 模块。"""

from app.security.redaction import redact_persisted_tool_value

__all__ = ["redact_persisted_tool_value"]
