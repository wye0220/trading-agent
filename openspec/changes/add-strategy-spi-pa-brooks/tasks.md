# Tasks

## 1. 分支与骨架

- [ ] 1.1 确认在分支 `phase-2-strategy-spi`（从 `phase-1-data-contract` 分出）；验证：`git branch --show-current` 输出该分支名
- [ ] 1.2 建立 `tests/vectors/strategy/pa_brooks/` 和 `config/strategies/`；验证：目录存在并被 Git 跟踪

## 2. 插件接口

- [ ] 2.1 在 `src/trading_agent/strategies/base.py` 定义 `FeatureRequirements`、`FeatureContext`、`StrategyState`、`TargetSpec`、`OrderIntent`、`DecisionMetadata`、`StrategyStepResult`、`TraderPlan`、`AlignmentResult` 和 `StrategyProvider` 协议，全部为不可变类型；验证：单元测试断言这些类型不可修改、`signal_status` 不是 `SETUP_VALID` 时带意图会报错
- [ ] 2.2 实现 `OrderIntent` 与 `TargetSpec` 的构造校验：订单类型和目标类型的枚举、数值为正、止损和限价的方向；验证：合法做多、合法做空、止损错侧、限价错侧、未知目标类型、目标为 0 各一个测试
- [ ] 2.3 实现 `StrategyState` 的序列化与结构版本号；验证：往返测试，含非 JSON 基本类型时报错
- [ ] 2.4 实现 `StrategyRegistry`：注册、查找、列出，同名重复和未注册报错；验证：对应单元测试
- [ ] 2.5 写一个测试用的模拟策略（每根返回 `MARKET_NEXT_OPEN` 加 `INITIAL_RISK_MULTIPLE`），只通过接口逐 bar 调用；验证：测试通过，且 `base.py`、`registry.py` 的源码里不出现 `pa_brooks`、`spike`、`H1` 等 PA 专有字样
- [ ] 2.6 在 `docs/strategy-spec.md` 写"插件接口"一节：各类型的字段、纯函数约束、状态序列化、如何新增一个策略；验证：`base.py` 里每个公开类型在文档中都有说明

## 3. 通用因果特征

- [ ] 3.1 在 `src/trading_agent/features.py` 实现 EMA20（收盘价，不足 20 根不可用）；验证：19 根不可用、20 根等价收盘、与手算序列一致
- [ ] 3.2 实现 ATR14（Wilder，精确有理数，不足 14 个 TR 不可用）；验证：首值为算术平均、递推公式、与手算一致、结果未被四舍五入
- [ ] 3.3 实现 swing 高低点（左右各 2 根严格，等高等低不确认，右侧第 2 根可见后可用）；验证：右侧不足、等高、严格更高、低点镜像各一个测试
- [ ] 3.4 实现 `rolling_20_range` 和 `intraday_rth_range`；验证：不足 20 根不可用、夜盘高点只进滚动区间的测试
- [ ] 3.5 实现 ADR（前 10 个 RTH 日的完整有效日，至少 3 个，不含当天）和 1 小时线背景的组装；验证：2 个有效日不可用、7 个有效日取均值并报 7、当前小时未结束不在其中
- [ ] 3.6 实现从一串可见 K 线和阶段 1 的 as-of 结果组装 `FeatureContext` 的函数，指标只用当前实际合约的 K 线；验证：换月后只有 10 根时 EMA、ATR 不可用；在序列末尾追加 K 线不改变较早时点的特征值
- [ ] 3.7 在 `docs/strategy-spec.md` 写"通用特征"一节：每个特征的公式、可用条件、`known_at`；验证：`FeatureContext` 的每个字段在文档中都有说明

## 4. PA 配置

- [ ] 4.1 写 `config/strategies/pa-esnq-5m-v1.0.yaml`：ES／MES 与 NQ／MNQ 的回撤和根数阈值、Spike 的 2.5×ATR 和 50%／三分之一、质量检查的 20% 和 3×ATR、限价偏移 1 tick、ADR 目标 0.40、尾盘 30 分钟、两次止损、十类形态的优先级和启用开关（只有 Spike 回调为真）、各形态的过滤器豁免表；验证：加载测试断言各数值
- [ ] 4.2 实现配置加载与校验：未知键报错、启用未实现的形态报错、阈值类型和范围检查；验证：启用楔形时加载失败并指出原因，非法阈值各一个测试

## 5. Spike 与回调状态机

- [ ] 5.1 实现强趋势 K 和 Spike 的识别（含用 Spike 末根的 ATR、Spike 延伸、反向 Spike 取代）；验证：向量覆盖恰好 2.5×ATR、略低于、只有一根、三根、延伸、被反向 Spike 取代
- [ ] 5.2 实现回调的开始、回撤比例（整数交叉相乘）、失效和趋势恢复；验证：向量覆盖 ES 恰好 75%、超过 75%、第 20 根有效、第 21 根作废、NQ 60% 和 12 根的边界、收盘越过极值结束
- [ ] 5.3 实现反向腿与 H1–H3 / L1–L3 计数；验证：向量覆盖第一个 H1、腿首根不能当信号 K、没有新腿不升级、新腿后 H2、H3 之后不再发信号
- [ ] 5.4 实现信号 K 的通用质量检查；验证：实体 15% 被拒、恰好 20% 通过、全幅超过 3×ATR 被拒、零全幅被拒
- [ ] 5.5 实现状态的序列化往返；验证：任取一个向量，在每一根之后都把状态序列化再恢复，结果与不中断时逐根相同

## 6. 过滤器、纪律与订单意图

- [ ] 6.1 实现五个过滤器及豁免表，按顺序评估并记录全部证据和第一个拦截原因；验证：每个过滤器各有触发与不触发的单元测试；Spike 回调在 1 小时方向未知时不被过滤器 1、3、4、5 拦截
- [ ] 6.2 实现尾盘窗口拦截（按当日实际收盘时刻）；验证：向量覆盖 15:30 不拦、15:35 拦、半日市 12:40 拦
- [ ] 6.3 实现两次止损停手；验证：向量覆盖两次止损后拦截、中间盈利清零、保本不清零、次日恢复
- [ ] 6.4 实现特征不可用时的 `WAIT`；验证：ATR 不可用时输出 `WAIT` 并指明原因
- [ ] 6.5 实现订单意图：触发价、限价、回调极值止损、ADR 或 2R 目标、`setup_id`；验证：向量覆盖做多和做空的具体数值、ADR 201 tick 得 81、ADR 不可用得 2R、H1 与 H2 的 `setup_id` 不同、同输入两次相同
- [ ] 6.6 实现周期输出（两种 Spike、两种微通道，其余 `UNKNOWN`）和 `htf_direction=UNKNOWN`；验证：对应单元测试

## 7. 输出与一致性评估

- [ ] 7.1 组装 `DecisionMetadata` 的全部字段；验证：测试逐项断言 Prompt 2 第 6 节列出的字段都存在
- [ ] 7.2 实现中文短报渲染；验证：有信号、被拦截、等待三种结果各一个快照测试，末行是免责声明
- [ ] 7.3 实现 `evaluate_pretrade_alignment`；验证：熊 Spike 中做多得 `DEVIATES` 和 `COUNTERTREND_BEFORE_CONFIRMATION`、与有效信号同向得 `ALIGNED` 和 `setup_id`、无结构得 `UNKNOWN`、无止损标 `NO_STOP`
- [ ] 7.4 把 PA 策略注册到注册表；验证：按 `pa_brooks` 查到，`feature_requirements()` 声明了 `ema20`、`atr14`、`adr10`、`htf_1h`、`rolling_20_range`、`intraday_rth_range`

## 8. 向量运行器与镜像

- [ ] 8.1 定义向量的 YAML 格式并实现运行器（K 线、特征覆盖、逐根期望）；验证：一个最小向量跑通，期望不符时报出是哪一根、哪个字段
- [ ] 8.2 实现自动镜像：价格翻转、方向和 H／L 标签互换后再跑；验证：`tests/vectors/strategy/pa_brooks/` 下每个向量的正向和镜像都通过
- [ ] 8.3 断言纯函数性：静态检查 `strategies/` 和 `features.py` 不引用 `sqlite3`、文件写入和网络库；同输入两次结果相同且不修改传入对象；验证：对应测试通过

## 9. 文档与收尾

- [ ] 9.1 在 `docs/strategy-spec.md` 写"PA v1.0 规格"：Spike、回调状态机、H／L 计数、质量检查、过滤器链与豁免、尾盘、两次止损、订单意图、输出字段；验证：pa-brooks-strategy 规格的每条要求在文档中都能找到对应小节
- [ ] 9.2 在 `docs/strategy-spec.md` 写"量化草案（待确认）"：九类未启用的形态、宽通道、交易区间、交易区间日、1 小时方向，各含突破参照、摆动结构、腿的重置、容差、确认时点、失效条件、镜像和边界测试；验证：每一项都有这八个小项，并标明"未启用"
- [ ] 9.3 在 `docs/strategy-spec.md` 写"假设"和"研究排除项"（Prompt 2 列出的五条未采用方案）；验证：design.md 中标为我的解读的每一处都列在假设里
- [ ] 9.4 更新 `docs/glossary.md`（Spike、回调腿、H／L 标签、`setup_id`、目标描述）和 `README.md`（阶段 2 的状态）；验证：新术语都有条目
- [ ] 9.5 运行 `uv run pytest`；验证：全部通过，阶段 1 的测试没有被改动或跳过
- [ ] 9.6 确认没有在真实数据上运行策略：检查本分支的测试和脚本没有把策略接到运行库；验证：`grep` 不到策略模块对 `open_runtime` 或 `context.visible_bars` 的调用（特征组装函数除外，它只在合成数据的测试里被调用）
- [ ] 9.7 按"阶段完成判定"模板写 `docs/phase-2-report.md` 并在分支上提交，不合并；验证：`git log phase-1-data-contract..phase-2-strategy-spi` 有提交
