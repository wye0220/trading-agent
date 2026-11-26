# 调度、告警、控制通道与恢复（阶段 10）

阶段 1–9 的每一步都能手动运行。这一阶段把它们串成可调度的流水：launchd 只调用 `ops` 和 `control` 两个子命令，手工运行完全等价。

**已安装（2026-11-23，你授权）**：每日采集 `com.wenye.trading-agent-collect`（17:12）、盘后 `com.wenye.trading-agent-daily`（17:45）、周度 `com.wenye.trading-agent-weekly`（18:05）、watch（09:25–16:05）、watch 检查（每 2 分钟）、盘中采集与节拍（每 5 分钟准点）。其余是样例（`ops/launchd/*.plist.example`），没有安装。见第 9 节。

配置：`config/ops.yaml`（scope、节拍、盘后、告警、模型调用）和 `config/control.yaml`（控制通道）。数值是预设，需要你确认。

## 1. 一天的流水

| 时刻（ET） | 动作 | 等价的手工命令 | 样例 |
| --- | --- | --- | --- |
| 09:25 | 启动常驻 watch，16:05 退出 | `python -m trading_agent watch` | `com.wenye.trading-agent-watch`（已安装） |
| 每 5 分钟准点（交易日 RTH 开盘到收盘后 10 分钟） | 读本地线（必要时取 Webull），然后节拍 | `ops/launchd/run-intraday.sh`，或只跑 `python -m trading_agent ops --tick` | `com.wenye.trading-agent-intraday`（已安装） |
| 每 2 分钟 | 检查 watch 心跳 | `python -m trading_agent ops --watch-check` | `com.wenye.trading-agent-watch-check`（已安装） |
| 每 30 秒 | 拉取 Telegram 控制通道 | `python -m trading_agent control --poll` | `com.wenye.trading-agent-control`（已安装） |
| 17:45（每日采集之后） | 盘后流水 | `python -m trading_agent ops --daily` | `com.wenye.trading-agent-daily`（已安装） |
| 16:30 | 模型解释 | `python -m trading_agent ops --explain` | `com.wenye.trading-agent-explain` |
| 18:05 | 周度聚合（只在每周最后一个交易日做事） | `python -m trading_agent ops --weekly` | `com.wenye.trading-agent-weekly`（已安装） |
| 17:12 | 每日采集 | `ops/launchd/run-collect-mcp.sh` | `com.wenye.trading-agent-collect`（已安装） |

每个 `ops` 动作结束时都会分发一次告警。日志在 `~/trading-agent-data/logs/ops/<日期>-<动作>.log`。

安装一个样例（要你授权）：

```bash
cp ops/launchd/com.wenye.trading-agent-daily.plist.example ~/Library/LaunchAgents/com.wenye.trading-agent-daily.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.wenye.trading-agent-daily.plist
launchctl bootout gui/$(id -u)/com.wenye.trading-agent-daily      # 停用
```

## 2. 盘中节拍（`ops --tick`）

1. 拿运行锁 `tick`（有效期 240 秒；锁被别人持有就返回 `LOCKED`，过期的锁可接管）。
2. 对 `config/ops.yaml` 的每个 scope：找当日实际合约、已收线且已可用的最后一根 5 分钟线。
3. 比水位：没有比上次处理更新的线就跳过（`NO_NEW_BAR`）。有新线就运行一次盘中扫描（在发布库的读快照里解析 active 版本）并推进影子两臂和实时前向预演。
4. 写水位 `watermark:<scope>` 和心跳 `heartbeat:tick`。

**补跑**：停机或睡眠后，一次节拍会把落下的线全部交给引擎。落下的信号只进扫描记录（`catch_up=true`），过期的挂单不会恢复成当前入场。纸面运行验收也只数按时处理的交易日。

**断流和迟到**：盘中应已到却没到的 5 分钟线（收线后再等 90 秒）超过 2 根记 CRITICAL `DATA_STALE`，1–2 根记 WARN `LATE_BAR`。

**盘中数据来源**（2026-11-24 起）：每轮先等 price-feed 这一轮的收据，读本地 TradingView 线（`collect --from-tvfeed`，只读，主来源）；只有本地线当天没有任何 RTH 线（price-feed 没跑，或到期周）才起 `claude -p` 会话取 Webull（备用），那天记 WARN `BACKUP_SOURCE_IN_USE`。见 `docs/data-contract.md`。

**开销**：平时不起 `claude -p` 会话；只有用 Webull 备用时，每 5 分钟一个。取不到数时节拍照样运行，只会多出迟到告警，不会产生错误信号。

## 3. 盘后与周度

**`ops --daily`**（收盘后 15 分钟起才做事，提前收盘随日历；`--day` 补跑某一天）：

0. 截止日之后、放行首日之前的封存日：直接跳过（`SEALED_DAY`），不告警。
1. 先补扫当天（跑一次节拍）：没有盘中采集时，纸面运行要到这里才看到当天的线。补扫的信号已过期，不挂单，也不计入纸面运行验收。
2. 当日 RTH 线没到齐：不封存，记 WARN `INCOMPLETE_DAY`。
3. 常驻纸面运行：封存并复盘该 session（放行的实时行情日可以封存；聚合仍按截止日把它们排除在外）。落在进行中的前向保留窗口里时跳过复盘（复盘产物属于评测侧），只记运行健康。
4. 账本检查：不一致记 CRITICAL `LEDGER_INCONSISTENT`。
5. 发布后监控：触发时自动冻结并记 CRITICAL `MONITOR_FREEZE`。
6. 人工轨：封存并复盘 `config/ops.yaml` 的 `daily.account`（缺省是 `config/manual.yaml` 的默认账户）。

**`ops --weekly`**：只在每周最后一个交易日按 scope 运行聚合，有新候选时记 INFO。各队列前三名的实时前向预演挂在每次节拍上。

评测按候选状态由你启动（`evaluate`）；发布等待独立批准（`release --approve` 或控制通道的 `approve`）。演练通过不能跳过正式评测和发布门槛。

## 4. 告警

所有告警都写进 `alerts` 表；分发结果另记在 `alert_deliveries`。

| 级别 | 例子 |
| --- | --- |
| CRITICAL | 行情断流 `DATA_STALE`、账本不一致、监控自动冻结、watch 停止 `WATCH_STOPPED`、`NAKED_POSITION` |
| WARN | 迟到 `LATE_BAR`、日损达 80%、`STOP_MOVED_AWAY`、`REENTRY_IN_COOLDOWN`、`UNPLANNED_ENTRY`、`COUNTERTREND_ADD`、券商数据过期、模型失败、schema 校验失败进死信 |
| INFO | 新候选、评测完成、待审批 |

- **去重键** = 级别 + 类型 + scope + 持仓键（或账户、请求）。同一去重键在静默窗口内只推送一次，其余记 `SUPPRESSED`。静默窗口按类型配置（缺省 30 分钟，`DATA_STALE` 15 分钟，`WATCH_STOPPED` 10 分钟，`NAKED_POSITION` 5 分钟）。纪律提醒的静默另按持仓和规则在 watch 里计算。
- **纪律提醒即时推送**：watch 每轮之后立刻分发，不进日终摘要。
- **通知适配器**：`telegram`（`ops/notify-telegram.sh`）或 `none`。
- **第一次分发**时，之前积压的告警标为 `BACKLOG`、不推送。

## 5. 控制通道

只执行适配器校验过来源、发送者已在 `config/control.yaml` 绑定的消息；只认下面的命令，其余文字只当数据。每条消息（包括被拒绝的）都写进只追加的 `control_commands`。

| 命令 | 作用 | 谁能用 |
| --- | --- | --- |
| `plan ES L 5000 4995 5010 1 理由…` | 交易前预检（阶段 4 的快捷语法） | 绑定用户 |
| `activate <plan_id>` | 激活已预检的计划 | 绑定用户 |
| `watch-interval 60\|300\|1m\|5m` | watch 下一轮起改用这个间隔 | 绑定用户 |
| `note [事件标识] 纠正…` | 纠正 | 绑定用户 |
| `context no-entry [until HH:MM] [scope\|ALL]` | 当日禁止新入场（缺省到收盘、全部 scope） | 绑定用户 |
| `context max-qty N [until HH:MM] [scope\|ALL]` | 当日手数上限（只能比已批准的更紧） | 绑定用户 |
| `freeze <scope\|ALL> [原因]` | 冻结新入场 | 绑定用户 |
| `unfreeze <scope> <freeze_event_id>` | 第一步：返回 120 秒有效的确认令牌 | 批准人 |
| `confirm <令牌>` | 第二步：令牌绑定原请求、通道和发送者；过期、用过、换人都不改变状态 | 批准人 |
| `approve <候选或baseline_id> <完整 manifest_hash> [scope]` | 批准发布（照常过发布守卫和全部核验） | 批准人 |
| `reject <release_id\|memc_…> [原因]` | 放弃待批准的清单，或拒绝 Memory 候选 | 批准人 |
| `confirm-memory <memc_…>` | 确认 Memory 候选 | 批准人 |

**context** 只能收紧：禁止入场会冻结纸面入场（原因 `CONTEXT`，到期由政策自动解除），并让人工计划预检给 `BLOCKED_BY_RISK`（`CONTEXT_NO_ENTRY`）；手数上限让超过它的计划 `BLOCKED_BY_RISK`（`CONTEXT_MAX_QTY:N`）。"14:00 FOMC 前不开新仓"这样的自由文本不执行，要写成 `context no-entry until 14:00`。

**适配器**：

- 本机命令行：`python -m trading_agent control --exec "freeze ALL 盘前演练"`。发送者是当前系统用户，要在 `adapters.cli.users` 里绑定（现在是空的，所以全部拒绝）。
- Telegram（2026-11-24 启用）：用告警的同一个机器人和群（trading-desk 的机器人只发不收，不会和这里抢消息；凭证与 `ops/notify-telegram.sh` 相同，只解析 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`）。只认那个群里**绑定用户**发的消息；绑定的用户 ID 存在 `~/trading-agent-data/secrets/control-telegram.env`（权限 600，不进仓库），用 `control --telegram-bind` 一次性绑定（在群里发 `/bind@ttradewoa_bot <码>`）。
  - 机器人在群里开着隐私模式，命令要以 `/` 开头并带机器人名：`/freeze@ttradewoa_bot ALL`、`/watch_interval@ttradewoa_bot 5m`、`/approve@ttradewoa_bot cand_… <manifest_hash>`。命令名里的 `-` 写成 `_`。
  - 第一次拉取只记下位置，启用之前积压的消息不当命令。
  - 定时任务 `com.wenye.trading-agent-control` 每 30 秒拉一次（已安装）；结果回复到群里。
- `python -m trading_agent control` 不带参数：最近 20 条审计。

## 6. 模型调用与死信

`ops --explain` 用 `claude -p --tools "" --output-format json` 处理解释请求队列：模型拿不到任何工具，提示词里只有入队时封存的上下文和输出 schema，输出必须通过 JSON schema。模型不可用、超时或输出不合格都算一次失败；连续 3 次（`config/engine.yaml` 的 `llm.max_retries`）转入死信队列并记 WARN。确定性流程（信号、订单、成交、盈亏、复盘）不受影响。完成的请求在 `explanation_requests.model_id` 里记下实际生成它的模型（取自 JSON 输出的 `modelUsage`，不是配置里的别名 `ops.explain.model`）；migration 0011 之前完成的请求为 NULL。

```bash
python -m trading_agent ops --dlq                  # 列出死信
python -m trading_agent ops --dlq-replay ALL       # 放回队列（重试计数清零）
python -m trading_agent ops --explain              # 重新处理
```

## 7. 恢复

`python -m trading_agent ops --health` 显示各心跳和水位、运行锁、死信数、待解释请求、待分发告警，以及下面的恢复命令。

| 情形 | 恢复 |
| --- | --- |
| 失去行情、迟到 | `collect --check-day <日期>` 看缺哪几根；`ops/launchd/run-collect-mcp.sh <日期>` 收盘后重新采集；然后 `ops --tick`（落下的信号只进历史） |
| 停机、睡眠之后 | `ops --health`；`ops --tick` 补算；`ops --daily --day <日期>` 补跑漏掉的盘后流水 |
| 重复运行 | 节拍有运行锁和水位，重复运行返回 `LOCKED` 或 `NO_NEW_BAR`；锁卡住时等它过期 |
| 控制通道离线 | 本机命令行 `control --exec "freeze ALL …"`，或直接 `freeze --reason …` |
| 券商只读接口失效 | `watch --once` 看报错；恢复后 `ingest --alpaca` 补导；停止期间 `ops --watch-check` 会报 `WATCH_STOPPED` |
| 死信队列 | `ops --dlq`、`ops --dlq-replay <task_id\|ALL>`、`ops --explain` |

## 8. 全链路演示

在临时库上，用跨交易日的合成 session，发布走独立的测试指针（`tests/test_ops.py`、`tests/test_release.py`）。

**一期**（`test_demo_phase_one`）：ingest → 节拍扫描 → 计划预检与激活 → watch（控制通道切换 1 分钟和 5 分钟间隔；合成券商快照触发 `NAKED_POSITION`，即时推送）→ 盘后封存与复盘 → 纪律 KPI。watch 的其余五条提醒各自的触发在 `tests/test_watch.py`。

**二期**：

| 段 | 演练 |
| --- | --- |
| ingest → scan → plan → close session → review | `tests/test_review.py`（复盘）、`test_daily_and_weekly`（节拍和盘后流水） |
| evolve（Track A／B 分流、前向预演） | `tests/test_evolve.py` |
| evaluate（盲筛胶囊、Delta Trades 检验） | `tests/test_evaluation.py` 的 `test_full_protocol_flow`、`test_delta_trades_is_auxiliary` |
| shadow → 测试批准 → 测试激活 → 监控 → 下一个 session → 回滚 → 双策略并行隔离 | `test_demo_phase_two_release_chain`（批准经控制通道，回显 manifest_hash）；`tests/test_release.py` 的晋级、回滚和并行 scope 演练 |

**仍待真实行情验证**：盘中采集（`run-intraday.sh`）在真实市场上的到达延迟和失败率；节拍的迟到阈值是否合适；Telegram 控制通道的端到端收发；watch 在真实 Alpaca 接口上整天常驻的稳定性；首次纸面基线的纸面运行验收（要按时处理满 5 个交易日）。

## 假设

以下是我定的保守解，需要你确认。

1. **样例不安装**。只有阶段 1 的每日采集是装好的；其余定时任务等你授权。
2. **盘中采集沿用 `claude -p` 调 Webull 连接器**，每 5 分钟一次；这是现在唯一有期货行情的来源。
3. **节拍遍历 `config/ops.yaml` 登记的 scope**（现在只有 ES），不从发布库自动发现。
4. **告警去重键和静默窗口**（缺省 30 分钟等）是预设；第一次分发时积压的告警不推送。
5. **Telegram 控制通道用告警的同一个机器人和群**（2026-11-24 你的决定；trading-desk 只发不收，不冲突）；绑定的用户是批准人。本机命令行通道的绑定用户现在是空的。
6. **本机命令行通道的发送者就是系统用户**，和批准人是同一个人；它只防误操作，不是安全边界。
7. **`unfreeze` 的令牌 120 秒有效**；`approve` 只要求回显完整摘要，不再要令牌（摘要本身就是二次确认）。
8. **context 只认固定文法**：`no-entry` 和 `max-qty`；自由文本不执行。禁止入场同时作用于纸面入场和人工计划预检。
9. **模型调用不给任何工具**，比"只读封存产物"更严：上下文由程序在入队时封存，放进提示词。
10. **盘后流水在 17:45 跑**（每日采集 17:12 之后），提前收盘日也一样；周度在 18:05 跑，不是周末交易日就直接返回。当天没有成交也没有提醒的人工轨记 `NO_ACTIVITY`，不算失败。

## 9. 上线状态（2026-11-23）

| 任务 | 状态 | 说明 |
| --- | --- | --- |
| 每日采集 17:12 | 已安装 | 2026-11-24 起的 Webull 线放行进运行库 |
| 盘后 17:45 | 已安装 | 11-23 是封存日，试跑结果 `SEALED_DAY`；人工轨 `NO_ACTIVITY` |
| 周度 18:05 | 已安装 | 只有每周最后一个交易日做事 |
| watch（09:25–16:05）、watch 检查（每 2 分钟） | 已安装（2026-11-23） | Alpaca 模拟盘只读密钥在 `~/trading-agent-data/secrets/alpaca.env`；实测 `watch --once` 状态 OK |
| 盘中采集与节拍 | 已安装（2026-11-23） | 每小时 :00、:05 … :55 准点触发，等本地这一轮的数据就位后读本地线，必要时取 Webull，再跑节拍；按交易日历只在 RTH 开盘到收盘后 10 分钟内做事。平时不起 `claude -p` 会话；日志 `~/trading-agent-data/logs/ops/<日期>-intraday.log` |
| 控制通道（Telegram） | 已安装（2026-11-24） | 用告警的同一个机器人和群；绑定了你的用户 ID；每 30 秒拉一次。本机命令行通道仍没有绑定用户 |
| 模型解释 | 没装 | 积压 1362 个请求，等你决定 |

**注意**：launchd 运行的是项目目录里**当前检出的分支**。切换分支会改变定时任务执行的代码；上线期间请保持检出 `ops-go-live`（或合并后的 `main`）。

停用：`launchctl bootout gui/$(id -u)/com.wenye.trading-agent-daily`（weekly 同理）。

