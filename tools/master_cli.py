# -*- coding: utf-8 -*-
"""普通主站模拟（隔离台架）：交互菜单 + 请求队列 + 直连调度。"""
from __future__ import annotations

import argparse
import sys

from dds1366 import protocol as p
from dds1366 import ui
from dds1366.actions import (address_menu, baud_menu, forget_stored_address,
                             raw_menu, read_menu, relay_menu,
                             target_address_setup, write_menu)
from dds1366.client import Client
from dds1366.link import Link, SerialConfig
from dds1366.monitor import run_monitor
from dds1366.scheduler import DirectScheduler, Request, RequestQueue


class Session:
    def __init__(self, cfg: dict, yes: bool = False):
        self.cfg = cfg
        self.yes = yes
        self.link = None
        self.client = None

    def connect(self) -> bool:
        self.close()
        address = self.cfg.get("METER_ADDRESS") or ui.address_prompt()
        validated = p.validate_address(address)
        if validated is None:
            print(f"配置中的地址 {address!r} 不合法，请重新输入。")
            validated = p.validate_address(ui.address_prompt())
        if validated is None:
            print("未提供有效地址，连接取消。")
            return False
        self.cfg["METER_ADDRESS"] = validated
        port = self.cfg.get("PORT") or ui.port_prompt()
        self.cfg["PORT"] = port
        baud = ui.env_int(self.cfg, "BAUD", 2400)
        parity = self.cfg.get("PARITY", "E").upper()
        tx_key = self.cfg.get("TX_KEY", "rts").lower()
        try:
            link = Link(SerialConfig(port, baud, parity, tx_key))
        except Exception as exc:
            print(f"打开串口失败: {exc}")
            return False
        timeout = ui.env_float(self.cfg, "RESPONSE_TIMEOUT", 1.0)
        scheduler = DirectScheduler(link, p.wire_addr_of(validated),
                                    response_timeout=timeout, log=print)
        self.client = Client(scheduler, validated,
                             retries=ui.env_int(self.cfg, "RETRIES", 2))
        self.link = link
        ui.save_env(self.cfg)
        print(f"已连接 {port} {baud} 8{parity}1，目标 {validated}")
        return True

    def reopen(self):
        self.close()
        if not self.connect():
            print("重连失败，可进入“设置与重连”修正参数。")

    def close(self):
        if self.link is not None:
            self.link.close()
        self.link = None
        self.client = None

    def gate(self, title: str, lines) -> bool:
        if self.yes:
            print(f"!! --yes 已授权高风险操作: {title}")
            return True
        return ui.confirm_risky(title, lines)

    def show_config(self):
        print(f"  目标地址: {self.cfg.get('METER_ADDRESS', '(未设置)')}")
        print(f"  已存地址: {' '.join(ui.env_addresses(self.cfg)) or '(无)'}")
        print(f"  串口: {self.cfg.get('PORT') or '(自动)'}  "
              f"{ui.env_int(self.cfg, 'BAUD', 2400)} 8"
              f"{self.cfg.get('PARITY', 'E')}1  "
              f"方向脚={self.cfg.get('TX_KEY', 'rts')}")
        print(f"  重试: {ui.env_int(self.cfg, 'RETRIES', 2)}  "
              f"应答超时: {ui.env_float(self.cfg, 'RESPONSE_TIMEOUT', 1.0):.1f}s")

    def raw(self, data: bytes):
        valid, _ = p.find_frames(data)
        if len(valid) != 1 or valid[0].raw != data:
            print("不是完整且校验正确的 645 帧，已取消。")
            return
        frame = valid[0]
        ctrl = frame.ctrl
        if frame.addr_wire == p.BCAST_WIRE or frame.is_response:
            expect = False
        else:
            expect = ui.confirm("等待应答?", default=True)
        await_ctrl = (ctrl | 0x80, ctrl | 0xC0)
        request = Request(data, f"原始帧 C={ctrl:02X}H", await_ctrl,
                          expect_response=expect, retries=0,
                          meta={"any_addr": True})
        result = self.client.scheduler.consume(RequestQueue([request]))[0]
        ui.show_result(result)


def settings_menu(session: Session):
    while True:
        ui.banner("设置与重连")
        session.show_config()
        choice = ui.menu("修改配置", [
            ("1", "目标表地址（12 位新增/不足 12 位按后缀匹配）"),
            ("2", "删除已储存地址"),
            ("3", "串口与串口参数"),
            ("4", "重试次数 / 应答超时"),
            ("5", "显示配置"),
            ("6", "重连串口"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            target_address_setup(session)
        elif choice == "2":
            forget_stored_address(session)
        elif choice == "3":
            session.cfg["PORT"] = ui.port_prompt(session.cfg.get("PORT", ""))
            session.cfg["BAUD"] = str(ui.baud_prompt(
                ui.env_int(session.cfg, "BAUD", 2400)))
            session.cfg["PARITY"] = ui.parity_prompt(
                session.cfg.get("PARITY", "E"))
            session.cfg["TX_KEY"] = ui.tx_key_prompt(
                session.cfg.get("TX_KEY", "rts"))
            ui.save_env(session.cfg)
            session.reopen()
        elif choice == "4":
            session.cfg["RETRIES"] = str(ui.ask_int("重试次数", 2, lo=0, hi=9))
            timeout = ui.ask_float(
                "应答超时秒",
                ui.env_float(session.cfg, "RESPONSE_TIMEOUT", 1.0), lo=0.1)
            session.cfg["RESPONSE_TIMEOUT"] = f"{timeout:g}"
            ui.save_env(session.cfg)
            session.reopen()
        elif choice == "5":
            session.show_config()
            ui.pause()
        elif choice == "6":
            session.reopen()
            ui.pause()


def main() -> int:
    ui.init_console()
    parser = argparse.ArgumentParser(description="DDS1366 普通主站模拟（隔离台架）")
    parser.add_argument("--address", help="覆盖 .env 中的目标表地址")
    parser.add_argument("--port", help="覆盖 .env 中的串口")
    parser.add_argument("--yes", action="store_true",
                        help="跳过台架隔离确认和高风险操作短语确认")
    args = parser.parse_args()

    cfg = ui.load_env()
    if args.address:
        cfg["METER_ADDRESS"] = args.address
    if args.port:
        cfg["PORT"] = args.port

    ui.banner("普通主站模拟（隔离台架）")
    print("本工具会主动发送读、写、跳合闸等帧，只能用于隔离台架或点对点测试。")
    print("若总线上还有运行中的采集主站，请改用 coexist_cli.py（窗口注入）。")
    if not args.yes and not ui.confirm("确认已隔离台架，继续?", default=False):
        return 0

    session = Session(cfg, args.yes)
    if not session.connect():
        print("首次连接失败，可在设置中修正参数。")
    try:
        while True:
            if session.client is None:
                if not ui.confirm("串口未连接，进入设置?", default=True):
                    break
                settings_menu(session)
                if session.client is None:
                    continue
            choice = ui.menu("主菜单", [
                ("1", "读取数据"),
                ("2", "实时监测"),
                ("3", "写参数"),
                ("4", "继电器 / 跳合闸"),
                ("5", "通信地址"),
                ("6", "通信速率"),
                ("7", "发送原始帧"),
                ("8", "设置与重连"),
                ("0", "退出"),
            ])
            if choice == "0":
                break
            if choice == "1":
                read_menu(session)
            elif choice == "2":
                run_monitor(session, coexist=False)
            elif choice == "3":
                write_menu(session)
            elif choice == "4":
                relay_menu(session)
            elif choice == "5":
                address_menu(session)
            elif choice == "6":
                baud_menu(session)
            elif choice == "7":
                raw_menu(session)
            elif choice == "8":
                settings_menu(session)
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
