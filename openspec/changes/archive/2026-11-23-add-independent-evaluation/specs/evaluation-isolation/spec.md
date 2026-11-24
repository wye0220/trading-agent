## ADDED Requirements

### Requirement: 评测身份与摘要清单
评测真实窗口前 SHALL 校验：当前系统用户不在作者名单内；受保护的摘要清单存在，且与评测器代码、引擎代码的摘要一致。任一不满足 SHALL 拒绝运行。写清单的入口 SHALL 拒绝作者身份。

#### Scenario: 作者身份运行
- **WHEN** 作者名单里的用户对真实窗口运行盲筛
- **THEN** 拒绝，原因 `AUTHOR_IDENTITY`，窗口不解封

#### Scenario: 评测器被改过
- **WHEN** 评测器代码的摘要与清单不符
- **THEN** 拒绝，原因 `DIGEST_MISMATCH`

#### Scenario: 没有清单
- **WHEN** 清单文件不存在
- **THEN** 拒绝，原因 `MANIFEST_MISSING`

### Requirement: 隔离验收
CLI SHALL 提供隔离检查：以当前身份尝试列目录、用 Python 打开、用 shell 读取、经符号链接读取保留集目录，逐条报告是否被挡住，不输出任何文件内容。

#### Scenario: 当前身份读得到
- **WHEN** 任一路径读取成功
- **THEN** 检查结果为 `FAIL`，并指出是哪一条
