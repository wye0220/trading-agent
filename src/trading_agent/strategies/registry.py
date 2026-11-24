"""策略注册表：按策略家族名注册和查找。"""
from __future__ import annotations

from .base import ContractViolation, StrategyProvider


class StrategyRegistry:
    def __init__(self):
        self._items: dict[str, StrategyProvider] = {}
        self._variants: dict[str, object] = {}

    def register(self, provider: StrategyProvider, variant_factory=None) -> StrategyProvider:
        if not isinstance(provider, StrategyProvider):
            raise ContractViolation(f"{provider!r} 没有实现 StrategyProvider 接口")
        family = provider.strategy_family
        if family in self._items:
            raise ContractViolation(f"策略家族已注册：{family}")
        self._items[family] = provider
        if variant_factory is not None:
            self._variants[family] = variant_factory
        return provider

    def variant(self, family: str, diff, label: str) -> StrategyProvider:
        """按结构化配置差异构造该家族的一个变体（研究对比用）。家族没有登记变体工厂时报错。"""
        self.get(family)
        if family not in self._variants:
            raise ContractViolation(f"策略家族 {family} 没有登记配置变体工厂")
        provider = self._variants[family](diff, label)
        if not isinstance(provider, StrategyProvider) or provider.strategy_family != family:
            raise ContractViolation(f"{family} 的变体工厂返回了不合规的对象")
        return provider

    def get(self, family: str) -> StrategyProvider:
        try:
            return self._items[family]
        except KeyError:
            raise ContractViolation(
                f"策略家族未注册：{family}（已注册：{', '.join(sorted(self._items)) or '无'}）") from None

    def families(self) -> list[str]:
        return sorted(self._items)

    def __contains__(self, family: str) -> bool:
        return family in self._items


def default_registry() -> StrategyRegistry:
    """已注册全部内置策略的注册表。新增策略：在 strategies/ 下加模块，并在这里注册。"""
    from .pa_brooks import PaBrooksStrategy

    reg = StrategyRegistry()
    cls = PaBrooksStrategy
    reg.register(cls.from_default_config(), variant_factory=cls.variant)
    return reg
