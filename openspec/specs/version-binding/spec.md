# version-binding Specification

## Purpose

让每张订单、每个仓位、每笔已完结的交易都记得自己是在哪一组版本（策略、风控、提示词、Memory、引擎、模型）下产生的，使正式版本切换时新旧互不干扰，事后也能按版本分组比较。

## Requirements

### Requirement: 版本元组
引擎 SHALL 为每个 scope 给出版本元组：策略家族、策略版本、风控版本、提示词版本、Memory 版本、引擎版本、模型标识。风控版本 SHALL 随风控配置的内容变化。

#### Scenario: 尚无正式发布
- **WHEN** 该 scope 在发布库里没有 active 指针
- **THEN** 版本元组由当前注册的策略版本、风控配置摘要和引擎版本组成，并标明尚未发布

#### Scenario: 风控配置变化
- **WHEN** 修改了风控配置中的滑点预算
- **THEN** 风控版本随之变化

### Requirement: 每一步加载一次
引擎 SHALL 在每一步开始时加载一次该 scope 的版本元组，这一步内保持不变。

#### Scenario: 步内一致
- **WHEN** 一步之内生成扫描记录和订单
- **THEN** 两者记录的版本元组相同

### Requirement: 新旧版本互不干扰
新信号 SHALL 使用当前的版本元组。已排队的订单和已有的仓位 MUST 保留各自生成时绑定的版本元组。

#### Scenario: 持仓期间版本变化
- **WHEN** 一个仓位在版本 A 下开出，之后当前版本变为 B
- **THEN** 该仓位及其已完结的交易记录的仍是版本 A，之后的新订单记录版本 B
