## ADDED Requirements

### Requirement: 轮询间隔可来自已生效的偏好
`watch` 的轮询间隔 SHALL 依次取命令行 `--interval`、已生效的偏好 `watch_poll_interval_seconds`、`config/watch.yaml`。

#### Scenario: 有已生效的偏好
- **WHEN** 没给 `--interval`，Memory 里有生效的 `watch_poll_interval_seconds=300`
- **THEN** `watch` 用 300 秒

#### Scenario: 命令行优先
- **WHEN** 同时给了 `--interval 60`
- **THEN** `watch` 用 60 秒
