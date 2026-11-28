# 多因素认证异常的检测与限权处置

各接入端返回已核验的身份尝试，支持台维护换机事实，身份管理端跟踪实际处置。资料只有观测与治理回执，不生成确认请求或真实登录。

资料中的对象、身份及记录均为虚构。各来源使用 UTF-8 JSON Lines，一行是一个原始对象。`seq` 是全局到达序号，按演练资料的可知顺序排列。事实发生时刻、确认时刻与适用时段以同行中的独立字段为准，用相对演练起点的整数分钟表达。JSON `null` 保留来源的缺省或未确认值。除非来源明确使用相同标识，编号只在所属来源和实体域内有意义。

## 来源

- `sources/attempts.jsonl`　source 与 attempt 联合标识实际尝试，account 使用账号域加账号号。observed_at 是认证端实际时间，factor 和 device 是已核引用。
- `sources/reports.jsonl`　通过可靠渠道已核的报告，source/attempt 可以为空，supersedes 是用户明确更正的报告号。
- `sources/enrollment.jsonl`　支持台的预约与实际办理记录。两设备可以同用 display，但以 credential 和 device_identity 识别。
- `sources/policy.jsonl`　值班配置，horizon 为观测时间跨度，basis 说明须核对的独立信息。retained 指独立可用的验证入口。
- `sources/responses.jsonl`　管理端回执。operation 标识处置来源，target 由账号、尝试来源和对象标识共同定位。

## 查看记录

使用 Python 3.11.2 标准库，无需安装第三方包。以下命令列出可读取的来源、查看单个来源记录并检查资料格式。读取命令按输入顺序输出 JSON，可通过 `--through` 限制已到达的记录。

```bash
python3 source_records.py
python3 source_records.py attempts
python3 -m unittest discover -s tests -v
```

## 检测与缓解台

`duty_desk.py` 在本地按全局到达序号（`seq`）持续接收五个来源的记录，输出三类视图：

- 检测结论（`accounts`）：按账号与账号域策略给出每次尝试的风险结论、可核对窗口与逐条依据。
  - `quarantine` 待隔离：可靠渠道的明确否认与确切认证记录对应；
  - `cleared` 风险项消除：证据证明操作来自已登记且未被否认的合法换机（仅当策略 `basis` 含 `enrollment`）；
  - `pending` 身份待核：报告未指向具体尝试、设备观测缺失或登记状态不满足，保留可核对范围；
  - `normal` 无不利证据；缺少值班策略的账号域不下结论。
- 缓解处置（`mitigations`）：按账号、尝试来源和对象定位管理端回执，区分 `queued`/`applied`/`target_unverified`，并对每条解除决定说明是否可恢复及阻塞原因（如 `independent-revocation`、`partially-observed`）。
- 用户告知（`notices`）：仅列出真正仍受限的访问，指明可继续使用的保留入口与重新验证要求，不暴露因子秘密。

关键判定规则：

- 同一 `account` + `source` + `attempt` 的重复投递合并为一条尝试，重复到达不增加风险依据；不同接入端的同号尝试、同显示名（`display`）的不同设备互不混淆，设备以 `credential` 与 `device_identity` 识别。
- 换机预约（`scheduled`）不是登记；仅凭相同显示名、一次成功批准或后来完成的换机，不能为在先仍有风险的会话开脱。
- 策略按修订号取最新生效版本；`horizon` 界定可核对区间 `(at-horizon, at]`，窗口外的尝试保留结论但不再产生新处置建议。
- 处置涵盖因子时，保留入口（`retained`）与其他因子的会话不进入处置范围；解除只能恢复仍满足身份条件、确属本次处置限制且未被独立撤销的会话。

```bash
python3 duty_desk.py                 # 截至全部已到达记录的完整视图
python3 duty_desk.py --through 10    # 重放到 seq 10 的观测状态
python3 duty_desk.py dept-n:23       # 只看单个账号
```
