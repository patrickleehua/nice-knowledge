from pydantic import BaseModel, ConfigDict


class LLMOutput(BaseModel):
    """LLM 结构化输出模型基类:强制 additionalProperties=false。"""

    model_config = ConfigDict(extra="forbid")
