"""引擎核心：事件时钟、成交模型、风控、版本绑定、上下文构建器。

只提供可被 scan 和 replay 共同调用的函数和事务封装，不依赖任何具体策略的内部字段。
"""
from .fills import ENGINE_VERSION  # noqa: F401
