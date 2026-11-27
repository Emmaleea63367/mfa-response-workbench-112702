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
