# -*- coding: utf-8 -*-
"""DDS1366 调试命令行：命令 + 参数，便于脚本和代理调用。"""
from __future__ import annotations

import argparse
import sys
import time

from dds1366 import link as linkmod
from dds1366 import protocol as p
from dds1366 import ui
from dds1366.client import CREDENTIALS, Client
from dds1366.link import Link, SerialConfig
from dds1366.scheduler import (CoexistConfig, CoexistScheduler,
                               DirectScheduler, Request, RequestQueue)


def hex_int(text: str) -> int:
    return int(text.replace("0x", "").replace("0X", ""), 16)


def add_common(parser: argparse.ArgumentParser, address: bool = True):
    parser.add_argument("--port", help="串口，如 COM5")
    if address:
        parser.add_argument("--address", help="目标表地址（默认 .env）")
    parser.add_argument("--baud", type=int, help="本机波特率（默认 .env）")
    parser.add_argument("--parity", help="N/E/O（默认 .env）")
    parser.add_argument("--tx-key", help="rts/dtr/none（默认 .env）")
    parser.add_argument("--timeout", type=float, help="应答超时秒（默认 .env）")
    parser.add_argument("--retries", type=int, help="重试次数（默认 .env）")


def resolve_address(args, cfg: dict, required: bool = True):
    text = getattr(args, "address", None) or cfg.get("METER_ADDRESS")
    if not text:
        if required:
            raise SystemExit("缺少表地址：--address 或 .env METER_ADDRESS")
        return None
    value = p.validate_address(text)
    if value is None:
        raise SystemExit(f"表地址不合法: {text!r}")
    return value


def open_link(args, cfg: dict) -> Link:
    port = args.port or cfg.get("PORT") or linkmod.suggest_port()
    if not port:
        raise SystemExit("缺少串口：--port 或 .env PORT")
    baud = args.baud if args.baud else ui.env_int(cfg, "BAUD", 2400)
    parity = (args.parity or cfg.get("PARITY", "E")).upper()
    tx_key = (args.tx_key or cfg.get("TX_KEY", "rts")).lower()
    return Link(SerialConfig(port, baud, parity, tx_key))


def open_client(args, cfg: dict):
    address = resolve_address(args, cfg)
    link = open_link(args, cfg)
    timeout = args.timeout if args.timeout is not None else ui.env_float(
        cfg, "RESPONSE_TIMEOUT", 1.0)
    retries = args.retries if args.retries is not None else ui.env_int(
        cfg, "RETRIES", 2)
    scheduler = DirectScheduler(link, p.wire_addr_of(address),
                                response_timeout=timeout, log=print)
    return link, Client(scheduler, address, retries=retries)


def allowed(args, title: str, lines) -> bool:
    if args.yes:
        print(f"!! --yes 已授权: {title}")
        return True
    if not sys.stdin.isatty():
        print("非交互环境执行高风险操作必须加 --yes。")
        return False
    return ui.confirm_risky(title, lines)


def all_ok(results) -> int:
    return 0 if all(x.status in ("ok", "late") for x in results) else 1


def cmd_list_ports(args, cfg):
    print(linkmod.ports_text())
    return 0


def cmd_decode(args, cfg):
    try:
        raw = p.parse_hex_bytes(" ".join(args.hex))
    except ValueError as exc:
        print(f"HEX 解析失败: {exc}")
        return 2
    valid, suspects = p.find_frames(raw)
    if not valid and not suspects:
        print("未找到完整 645 帧。")
        return 1
    for frame in valid + suspects:
        print(p.describe_frame(frame))
    return 0


def cmd_listen(args, cfg):
    try:
        link = open_link(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    observer = linkmod.BusObserver(link)

    def show(frame):
        mark = "表" if frame.is_response else "主站"
        for line in p.describe_frame(frame).splitlines():
            print(f"[{mark}] {line}")

    observer.on_frame = show
    print(f"被动监听 {args.seconds}s（不发送）...")
    try:
        observer.pump(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    print(f"\n主站请求 {observer.stats['master']}，"
          f"应答 {observer.stats['responses']}，"
          f"校验存疑 {observer.stats['suspect']}")
    interval = observer.est_interval()
    if interval is None:
        print("尚无主站请求间隔样本。")
    else:
        print(f"间隔中位数 {interval:.2f}s，样本 {len(observer.gaps)}")
    return 0


def cmd_windows(args, cfg):
    address = resolve_address(args, cfg, required=False) or "123456789012"
    try:
        link = open_link(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    config = CoexistConfig(per_window=max(1, min(3, args.per_window)))
    scheduler = CoexistScheduler(link, p.wire_addr_of(address), config=config)
    observer = scheduler.observer
    print(f"被动监听 {args.seconds}s 后评估窗口（不发送）...")
    try:
        observer.pump(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    interval = observer.est_interval()
    if interval is None:
        print("尚未收集到主站请求间隔，无法评估。")
        return 1
    print(f"主站请求 {observer.stats['master']}，应答 {observer.stats['responses']}，"
          f"间隔中位数 {interval:.2f}s，样本 {len(observer.gaps)}，"
          f"就绪={observer.ready(config.min_samples)}")
    window = scheduler._try_open()
    if window is None:
        print("当前不满足开窗条件（刚有活动、样本不足或剩余空间过小）。")
        return 1
    print(f"当前窗口: 跨度 {window.span:.2f}s，计划 {window.n} 次，"
          f"槽宽 {window.w:.2f}s，应答预算 {window.budget:.2f}s")
    return 0


def cmd_read(args, cfg):
    try:
        dis = [hex_int(x) for x in args.di]
        if not all(0 <= di <= 0xFFFFFFFF for di in dis):
            raise ValueError
    except ValueError:
        print("DI 应为 32 位 HEX。")
        return 2
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        results = client.read_many(dis)
        for result in results:
            ui.show_result(result)
        return all_ok(results)
    finally:
        link.close()


def cmd_read_address(args, cfg):
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        print("注意：AA×6 读地址会被总线上所有表应答。")
        result = client.read_address()
        ui.show_result(result)
        return 0 if result.status in ("ok", "late") else 1
    finally:
        link.close()


def cmd_write_param(args, cfg):
    flag = p.parse_group_flag(args.flag)
    if flag is None:
        print("组名应为两位 HEX，如 00/AA/BB。")
        return 2
    pa = args.pa
    pwd = args.password or CREDENTIALS.get(pa)
    if pwd is None:
        print(f"PA={pa:02X} 无已知默认密码，请用 --password 指定。")
        return 2
    if not allowed(args, "修改广播分组标志", [
        f"byte[2] → {flag:02X}H ({p.group_flag_desc(flag)})",
        "广播分组标志决定本表响应哪个组名的广播拉/合闸。",
    ]):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        ok, detail = client.set_group_flag(flag, pa, pwd)
        print(("成功: " if ok else "失败: ") + detail)
        return 0 if ok else 1
    finally:
        link.close()


def cmd_write_value(args, cfg):
    pa = args.pa
    pwd = args.password or CREDENTIALS.get(pa)
    if pwd is None:
        print(f"PA={pa:02X} 无已知默认密码，请用 --password 指定。")
        return 2
    if args.value < 0:
        print("值不能为负。")
        return 2
    cents = int(round(args.value * 100))
    if cents > 99999999:
        print("数值超出 4 字节 BCD 范围。")
        return 2
    content = p.bcd_bytes_le(cents, 4)
    if not allowed(args, "写 F9AAAA01", [
        f"值 {cents / 100:.2f}，编码 {content.hex(' ').upper()}",
        "该参数物理意义未确定，可能影响计量或结算。",
    ]):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        result = client.write_value_f9aaaa01(cents, pa, pwd)
        ui.show_result(result)
        return 0 if result.status in ("ok", "late") else 1
    finally:
        link.close()


def cmd_relay(args, cfg):
    pa = args.pa
    pwd = args.password or CREDENTIALS.get(pa)
    if pwd is None:
        print(f"PA={pa:02X} 无已知默认密码，请用 --password 指定。")
        return 2
    if args.action == "trip":
        lines = ["单播 1AH，直接断开本表继电器。"]
    elif args.action == "close":
        lines = ["单播 1CH，直接闭合本表继电器。"]
    else:
        lines = [f"跳闸保持 {args.pause:g}s 后自动合闸。",
                 "合闸失败可能造成用户停电，必须人工处理。"]
    if not allowed(args, f"继电器 {args.action}", lines):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        if args.action == "trip":
            result = client.trip_relay(pa=pa, pwd=pwd)
            ui.show_result(result)
            return 0 if result.status in ("ok", "late") else 1
        if args.action == "close":
            result = client.close_relay(pa=pa, pwd=pwd)
            ui.show_result(result)
            return 0 if result.status in ("ok", "late") else 1
        trip = client.trip_relay(pa=pa, pwd=pwd)
        ui.show_result(trip)
        time.sleep(max(1.0, args.pause))
        close = client.close_relay(pa=pa, pwd=pwd)
        ui.show_result(close)
        return 0 if close.status in ("ok", "late") else 1
    finally:
        link.close()


def cmd_set_baud(args, cfg):
    if args.rate not in p.RATE_FEATURE:
        print("速率须为 600/1200/2400/4800/9600/19200。")
        return 2
    if not allowed(args, "更改通信速率", [
        f"表速率 → {args.rate}bps，应答后数秒生效。",
        "切换期间可能失联。",
    ]):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        result = client.set_baud(args.rate)
        ui.show_result(result)
        if result.status not in ("ok", "late"):
            return 1
        if args.switch_local:
            time.sleep(max(0.0, args.settle))
            link.set_baud(args.rate)
            cfg["BAUD"] = str(args.rate)
            ui.save_env(cfg)
            print(f"本机串口已切到 {args.rate}bps，已写入 .env。")
        else:
            print(f"表端将切到 {args.rate}bps；本机需自行改 --baud 或用 --switch-local。")
        return 0
    finally:
        link.close()


def cmd_set_address(args, cfg):
    new = p.validate_address(args.new_address)
    if new is None:
        print("新地址不合法（0-9，最长 12 位，不能是广播地址）。")
        return 2
    if not allowed(args, "修改通信地址", [
        f"旧地址 {cfg.get('METER_ADDRESS', '(未设置)')} → {new}",
        "改址后旧地址失效；未记录新地址可能失去访问。",
        "部分表需重新上电后才按新地址应答。",
    ]):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        result = client.change_address(new)
        ui.show_result(result)
        if result.status not in ("ok", "late"):
            return 1
        if args.save:
            cfg["METER_ADDRESS"] = new
            ui.save_env(cfg)
            print("已把新地址写入 .env METER_ADDRESS。")
        return 0
    finally:
        link.close()


def cmd_broadcast(args, cfg):
    trip = args.action == "trip"
    group = p.parse_group_flag(args.group or ui.env_group(cfg))
    if group is None:
        print("组名应为两位 HEX，如 AA/BB。")
        return 2
    if not allowed(args, "广播" + ("拉闸" if trip else "合闸"), [
        "广播地址 999999999999，总线上所有表都会接收。",
        f"拉闸只对广播分组标志 byte[2]={group:02X}H 的表生效。",
        "合闸只把对应分组的 08 06 定时拉闸恢复为 00 00。",
    ]):
        return 1
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        results = client.broadcast_control(
            0x1A if trip else 0x1C, group, args.times, args.gap)
        for result in results:
            ui.show_result(result)
        if results and all(result.status == "ok" for result in results):
            if not args.no_verify:
                time.sleep(0.5)
                print("目标表状态回读:")
                ui.show_result(client.read(0xF9010100))
            return 0
        return 1
    finally:
        link.close()


def cmd_raw(args, cfg):
    try:
        raw = p.parse_hex_bytes(" ".join(args.hex))
    except ValueError as exc:
        print(f"HEX 解析失败: {exc}")
        return 2
    valid, _ = p.find_frames(raw)
    if len(valid) != 1:
        print("不是完整且校验正确的 645 帧，拒发。")
        return 2
    frame = valid[0]
    wake = raw[:frame.start]
    if frame.raw != raw[len(wake):] or any(b != 0xFE for b in wake):
        print("只允许在帧前附加 FE 唤醒字节，其他多余内容拒发。")
        return 2
    if not allowed(args, "发送原始帧", [
        raw.hex(" ").upper(),
        "原始帧绕过上层语义校验。",
    ]):
        return 1
    expect = not args.no_response and not frame.is_response \
        and frame.addr_wire != p.BCAST_WIRE
    try:
        link, client = open_client(args, cfg)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"连接失败: {exc}")
        return 1
    try:
        if args.wait_ctrl:
            await_ctrl = tuple(hex_int(x) for x in args.wait_ctrl)
        else:
            await_ctrl = (frame.ctrl | 0x80, frame.ctrl | 0xC0)
        request = Request(raw, f"原始帧 C={frame.ctrl:02X}H",
                          await_ctrl, expect_response=expect,
                          retries=0, meta={"any_addr": True})
        result = client.scheduler.consume(RequestQueue([request]))[0]
        ui.show_result(result)
        return 0 if result.status in ("ok", "late") else 1
    finally:
        link.close()


def cmd_selftest(args, cfg):
    from dds1366 import client, link, meter, monitor, protocol, scheduler
    for module in (protocol, link, scheduler, client, meter, monitor):
        module.selftest()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DDS1366 调试命令行（主动发送仅限隔离台架）",
        epilog="示例: python dds_debug.py read --port COM5 00000000 F9010100")
    sub = parser.add_subparsers(dest="command", required=True)

    item = sub.add_parser("list-ports", help="列出串口")
    item.set_defaults(func=cmd_list_ports)

    item = sub.add_parser("decode", help="离线解析帧 HEX")
    item.add_argument("hex", nargs="+", help="完整帧 HEX")
    item.set_defaults(func=cmd_decode)

    item = sub.add_parser("listen", help="被动监听总线（不发送）")
    add_common(item, address=False)
    item.add_argument("--seconds", type=float, default=20.0)
    item.set_defaults(func=cmd_listen)

    item = sub.add_parser("windows", help="监听后评估共存窗口（不发送）")
    add_common(item)
    item.add_argument("--seconds", type=float, default=20.0)
    item.add_argument("--per-window", type=int, default=3,
                      choices=(1, 2, 3))
    item.set_defaults(func=cmd_windows)

    item = sub.add_parser("read", help="读一个或多个 DI")
    add_common(item)
    item.add_argument("di", nargs="+", help="8 位 HEX 数据标识")
    item.set_defaults(func=cmd_read)

    item = sub.add_parser("read-address", help="AA×6 + 13H 读通信地址")
    add_common(item)
    item.set_defaults(func=cmd_read_address)

    item = sub.add_parser("write-param", help="读改写 F9030100 广播分组标志")
    add_common(item)
    item.add_argument("--flag", required=True,
                      help="目标组名，两位 HEX（如 00/AA/BB）")
    item.add_argument("--pa", type=hex_int, default=0)
    item.add_argument("--password", help="6 位 HEX 密码")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_write_param)

    item = sub.add_parser("write-value", help="写 F9AAAA01")
    add_common(item)
    item.add_argument("value", type=float, help="两位小数")
    item.add_argument("--pa", type=hex_int, default=0)
    item.add_argument("--password", help="6 位 HEX 密码")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_write_value)

    item = sub.add_parser("relay", help="单播跳闸/合闸/跳闸后恢复")
    add_common(item)
    item.add_argument("action", choices=("trip", "close", "trip-restore"))
    item.add_argument("--pause", type=float, default=5.0)
    item.add_argument("--pa", type=hex_int, default=0)
    item.add_argument("--password", help="6 位 HEX 密码")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_relay)

    item = sub.add_parser("set-baud", help="17H 更改通信速率")
    add_common(item)
    item.add_argument("rate", type=int)
    item.add_argument("--switch-local", action="store_true",
                      help="等待后把本机串口也切到新速率")
    item.add_argument("--settle", type=float, default=3.0)
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_set_baud)

    item = sub.add_parser("set-address", help="AA×6 + 15H 改通信地址")
    add_common(item)
    item.add_argument("new_address")
    item.add_argument("--save", action="store_true",
                      help="成功后把新地址写入 .env")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_set_address)

    item = sub.add_parser("broadcast", help="广播 1AH/1CH（N2=目标分组）")
    add_common(item)
    item.add_argument("action", choices=("trip", "close"))
    item.add_argument("--group",
                      help="目标组名，两位 HEX（默认 .env BROADCAST_GROUP，缺省 AA）")
    item.add_argument("--times", type=int, default=2)
    item.add_argument("--gap", type=float, default=0.42)
    item.add_argument("--no-verify", action="store_true")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_broadcast)

    item = sub.add_parser("raw", help="发送完整原始帧")
    add_common(item)
    item.add_argument("hex", nargs="+")
    item.add_argument("--no-response", action="store_true")
    item.add_argument("--wait-ctrl", nargs="+",
                      help="期望应答控制码，如 93 D3")
    item.add_argument("--yes", action="store_true")
    item.set_defaults(func=cmd_raw)

    item = sub.add_parser("selftest", help="运行全部离线自检")
    item.set_defaults(func=cmd_selftest)
    return parser


def main() -> int:
    ui.init_console()
    args = build_parser().parse_args()
    cfg = ui.load_env()
    try:
        return args.func(args, cfg)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:
        print(f"失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
