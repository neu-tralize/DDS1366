# DDS1366 调试脚本

面向 DDS1366 单相 485 电能表的 DL/T 645-2007 工具集：普通主站、共存主站、电表模拟器和命令行调试工具。

## 文件

| 文件 | 用途 |
| --- | --- |
| `master_cli.py` | 普通主站模拟，交互菜单，用于隔离台架或点对点测试 |
| `coexist_cli.py` | 共存主站模拟，在运行总线的主站空窗内注入读、写、控制、广播和原始帧 |
| `meter_sim.py` | 电表模拟器（645-2007 从站），交互菜单 |
| `dds_debug.py` | 调试命令行，按“命令 + 参数”调用，便于脚本或代理使用 |
| `dds1366/actions.py` | 主站公共交互菜单：读、写、继电器、地址、速率、原始帧 |
| `dds1366/monitor.py` | 实时监测：按固定顺序轮询实时参数并原位刷新面板 |
| `dds1366/protocol.py` | 帧构造、解析、DI 与数值解码 |
| `dds1366/link.py` | 串口、RS-485 方向控制、帧流与总线观测 |
| `dds1366/scheduler.py` | 请求队列、直连调度与共存窗口调度 |
| `dds1366/client.py` | 读、写、继电器、地址、速率、广播等高层操作 |
| `dds1366/meter.py` | 电表从站行为与模拟状态 |
| `dds1366/ui.py` | 菜单、输入校验、风险确认、`.env` 配置 |

## 安全边界

- `master_cli.py` 会主动发送读写、跳合闸和广播帧，**只能用于隔离台架或已获授权的点对点测试**。运行总线上还有采集主站时，禁止使用。
- `coexist_cli.py` 在观测到的现有主站事务空窗内发送，功能菜单与普通主站一致；窗口对齐只降低冲突概率，不能消除电学、协议和表计状态风险。仍需先确认总线有持续轮询的主站，并取得目标表及高风险操作的授权。
- 共存入口开启全功能帧白名单：目标表单播 `11H/14H/17H/1CH`、广播 `99×6` 的 `14H/1CH/15H`，以及经确认的原始帧；其他地址/命令组合由调度器拒绝。
- 共存总线上通常有多块表，入口不提供 `AA×6` 读/改通信地址；需要改址时请使用隔离台架下的普通主站。
- 高风险操作（跳合闸、广播、改地址、改速率、写参数、原始帧）需要 (y/N) 确认，默认不执行；`--yes` 会跳过确认，只应在已授权的自动化场景使用。
- 广播连发 2 遍可能落在不同主站窗口内，按两次独立的静默帧处理；不会为了凑在同一窗而挤压其他请求。
- 广播地址 `999999999999` 会影响总线上所有表。广播拉闸只对保电标志 `byte[2]=AAH` 的表生效；广播合闸只把 `08 06` 恢复为 `00 00`。
- 实测中，广播写 `F9AAAA01` 会无条件清零电能且不可恢复。本工具没有为它提供专用菜单命令；模拟器提供了“是否清电能”的开关用于演示。
- 仓库不包含现场表地址。`.env` 和 `meter_sim.json` 已加入 `.gitignore`。

## 安装

需要 Python 3.10+：

```bash
cd tools
python -m pip install -r requirements.txt
cp .env.example .env      # Windows: copy .env.example .env
```

`.env` 常用项：

| 键 | 说明 |
| --- | --- |
| `METER_ADDRESS` | 目标表地址，12 位以内数字 |
| `PORT` | 串口，如 `COM5`；留空时自动找 CH340 |
| `BAUD` / `PARITY` | 默认 `2400` / `E`，即 2400 8E1 |
| `TX_KEY` | RS-485 方向脚：`rts` / `dtr` / `none` |
| `RETRIES` | 超时重试次数 |
| `RESPONSE_TIMEOUT` | 普通主站等待应答秒数 |
| `COEXIST_PER_WINDOW` | 共存每窗最多通信次数，1~3 |
| `COEXIST_TXN_WAIT` | 共存等待主站窗口的秒数 |

## 交互工具

### 普通主站（隔离台架）

```bash
python master_cli.py
```

菜单覆盖：按组读取 DI、实时监测、写保电标志（读改写并回读校验）、写 `F9AAAA01`、自定义写、单播跳闸/合闸/跳闸后恢复、广播拉闸/合闸、读/改通信地址、更改速率、原始帧、串口与地址设置。

连续操作由请求队列交给直连调度器逐条发送；每条请求可配置重试。

实时监测按固定顺序（电压 → 功率 → 总电能 → 继电器状态 → 功率因数 → 无功功率）逐项轮询，每取得一项立即刷新面板中该参数的数值；间隔默认 3 秒（进入时可修改）。面板显示在终端备用屏幕内；按 `Q`/`Esc`（或 `Ctrl+C`）退出时在面板下方显示统计摘要，按回车确认后还原原界面并返回主菜单。

### 共存主站（运行总线）

```bash
python coexist_cli.py --address 123456789012 --port COM5
```

- 主菜单覆盖：读取数据、实时监测、写参数、继电器/跳合闸、通信速率、原始帧、设置与重连、被动监听并评估窗口。
- 实时监测与普通主站共用同一套数据项顺序；不引入额外间隔，只在观测到一个新的主站请求帧后才在空窗内注入一次查询，即每窗 1 次，轮询节奏与主站保持一致。同样按 `Q`/`Esc`（或 `Ctrl+C`）退出。
- 与普通主站共用 `dds1366.actions` 菜单；单播拉合闸、保电标志写入、广播、改速率和原始帧均可使用，但均需单独确认。通信地址读写仅在普通主站保留。
- 先采样现有主站请求间隔（只统计 1~6s 的帧间隔，取中位数）。等窗期间按状态变化输出提示（等待主站帧、采样进度 n/3、等待空窗等），首次通讯前的采样阶段也有回显。
- 天窗指主站一次通讯结束到下一次通讯之间的空窗：观测到新的主站请求帧才开新窗，每个天窗最多 3 次，按剩余空闲时间自动计算本窗可发次数。
- 发送时刻在现有主站事务结束后的空闲窗内；若下一次发送点与当前时刻冲突，则回滚为更少次数并重排。
- 应答超时：继续等待到本窗结束，仍无应答则按重试次数重试；本窗剩余的请求全部移到后续窗。
- 超出应答预算但在本窗结束前到达的应答记为“迟到成功”，仍会解码数据、回执并参与写入后的回读校验。
- 观测到主站新帧、他表应答或校验存疑帧时中止本窗，避免冲突。
- 没有主站活动时不会发送，超时后返回“无主站窗口”。
- `--yes` 仅用于已授权的自动化，会跳过隔离/短语确认，但不会改变窗口调度和帧白名单。

### 电表模拟器

```bash
python meter_sim.py
```

默认 2400 8E1，从站行为按实测记录实现：

- `11H` 读已实现的 DI：总电能、费率电能、电能块、电压、功率、功率因数、运行状态字、`F9010100`、`F9030100`、`F9030200`、`F9AAAA01`、`F9AAAA02`。
- 未实现的 DI 返回 `D1H ERR=02`；`12H` 返回 `D2H ERR=01`；`13H`/`15H` 只接受 `AA×6`。
- `1CH` 支持单播 `1A` 跳闸、`1C` 合闸，以及广播 `1A·AA`/`1C·AA`。
- 广播拉闸是否执行由参数块 `byte[2]` 决定：`AA` 执行，`00`/`BB` 忽略。
- 单播合闸清除任何拉闸状态；广播合闸只恢复 `08 06`。
- 可配置唤醒 `FE` 个数、继电器状态、电能、测量值、保电标志、广播写 `F9AAAA01` 是否清电能。

菜单中“开始模拟”进入串口循环，`Ctrl+C` 返回菜单。状态保存在 `meter_sim.json`。

自动运行（便于联调）：

```bash
python meter_sim.py --port COM5 --run --seconds 60
```

## 调试命令行

连接类参数放在子命令之后：`--port`、`--address`、`--baud`、`--parity`、`--tx-key`、`--timeout`、`--retries`。未给出的项从 `.env` 读取。

```bash
python dds_debug.py list-ports
python dds_debug.py decode "FE FE 68 12 90 78 56 34 12 68 91 08 33 33 33 33 66 38 33 33 EF 16"
python dds_debug.py listen --port COM5 --seconds 30
python dds_debug.py windows --port COM5 --seconds 30
python dds_debug.py read --port COM5 00000000 F9010100
python dds_debug.py read-address --port COM5
python dds_debug.py write-param --port COM5 --flag 00 --yes
python dds_debug.py write-value --port COM5 12.34 --yes
python dds_debug.py relay --port COM5 trip --yes
python dds_debug.py relay --port COM5 trip-restore --pause 5 --yes
python dds_debug.py set-baud --port COM5 4800 --yes
python dds_debug.py set-address --port COM5 000000000123 --yes
python dds_debug.py broadcast --port COM5 trip --yes
python dds_debug.py raw --port COM5 "68 AA AA AA AA AA AA 68 13 00 DF 16" --yes
python dds_debug.py selftest
```

- `listen` 和 `windows` 只被动接收，不发送。
- `raw` 只接受完整且校验正确的 645 帧，帧前可附加 `FE` 唤醒字节；危险操作需 `--yes` 或交互确认。
- `set-address --save` 会把新地址写回 `.env`；`set-baud --switch-local` 会在等待后把本机串口切到新速率并写回 `.env`。
- 命令返回码：`0` 成功，`1` 通信失败或表拒绝，`2` 参数错误。

## 联调示例

台架上有两个 RS-485 适配器（或一台回环设备）时：

1. 终端 A 运行 `python meter_sim.py --port COM5 --run`
2. 终端 B 运行 `python master_cli.py --port COM6 --address 123456789012 --yes`
3. 主站读取到的电能、继电器状态应与模拟器菜单中的配置一致。

运行总线上：

1. 先 `python dds_debug.py listen --port COM5 --seconds 30` 确认有主站轮询。
2. 再 `python coexist_cli.py --port COM5`，先做单项读取观察窗口日志，再开实时监测。

## 自检

```bash
python dds_debug.py selftest
# 或逐个模块
python -m dds1366.protocol
python -m dds1366.link
python -m dds1366.scheduler
python -m dds1366.client
python -m dds1366.meter
```

自检全部离线运行，不需要串口。

## 已知边界

- 协议层按 DL/T 645-2007 的 `+33H` 编码；1997 版不在支持范围内。
- `12H` 读后续数据未实现，模拟器按实测返回异常应答。
- 共存调度器默认只读；共存入口显式开启 `full_access` 白名单后才允许写入、控制、广播和原始帧。白名单只限制地址/命令组合，不代表操作安全。
- 数据项只覆盖仓库规范中实测过的子集；功率按 DDS1366 实测使用 3 位小数，不套用标准标称的 4 位。
- 本工具不替代现场安全规程；跳合闸、广播、改址、改速率前必须确认授权与物理隔离。
