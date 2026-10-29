"""模型接入的公共约定。具体的提供方、按用途指定模型见 avelis.modelcfg（在“模型配置”页面维护）。

LangSmith 等外部追踪默认关闭：导入本模块时若未显式设置，置 LANGSMITH_TRACING=false，
避免把问题、元数据发到第三方。
"""

from __future__ import annotations

import os

os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")

DEMO_ADAPTER = "demo-rules"
