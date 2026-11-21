# -*- coding: utf-8 -*-
"""共存主站模拟：运行总线上按主站空窗注入读写/控制/原始帧。"""
from __future__ import annotations

import argparse
import sys

from dds1366 import protocol as p
from dds1366 import ui
from dds1366.actions import (baud_menu, raw_menu, read_menu, relay_menu,
                             write_menu)
from dds1366.client import Client
from dds1366.link import Link, SerialConfig
from dds1366.monitor import run_monitor
from dds1366.scheduler import (CoexistConfig, CoexistScheduler, Request,
                               RequestQueue)


class CoexistSession:
    def __init__(self, cfg: dict, yes: bool = False):
        self.cfg = cfg
        self.yes = yes
        self.link = None
        self.scheduler = None
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
        config = CoexistConfig(
            per_window=max(1, min(3, ui.env_int(
                self.cfg, "COEXIST_PER_WINDOW", 3))),
            txn_wait=ui.env_float(self.cfg, "COEXIST_TXN_WAIT", 30.0),
            full_access=True,
        )
        scheduler = CoexistScheduler(
            link, p.wire_addr_of(validated), config=config, log=print)
        self.client = Client(scheduler, validated,
                             retries=ui.env_int(self.cfg, "RETRIES", 2))
        self.link = link
        self.scheduler = scheduler
        ui.save_env(self.cfg)
        print(f"已连接 {port} {baud} 8{parity}1，目标 {validated}，"
              f"每窗最多 {config.per_window} 次")
        return True

    def reopen(self):
        self.close()
        if not self.connect():
            print("重连失败，可修改串口参数后重试。")

    def close(self):
        if self.link is not None:
            self.link.close()
        self.link = None
        self.scheduler = None
        self.client = None

    def gate(self, title: str, lines) -> bool:
        if self.yes:
            print(f"!! --yes 已授权高风险操作: {title}")
            return True
        return ui.confirm_risky(title, lines)

    def show_config(self):
        print(f"  目标地址: {self.cfg.get('METER_ADDRESS', '(未设置)')}")
        print(f"  串口: {self.cfg.get('PORT') or '(自动)'}  "
              f"{ui.env_int(self.cfg, 'BAUD', 2400)} 8"
              f"{self.cfg.get('PARITY', 'E')}1  "
              f"方向脚={self.cfg.get('TX_KEY', 'rts')}")
        print(f"  重试: {ui.env_int(self.cfg, 'RETRIES', 2)}  "
              f"模式: 全功能窗口注入")
        if self.scheduler is not None:
            print(f"  每窗上限: {self.scheduler.cfg.per_window}  "
                  f"等窗超时: {self.scheduler.cfg.txn_wait:g}s  "
                  f"最小样本: {self.scheduler.cfg.min_samples}")

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
        request = Request(data, f"原始帧 C={ctrl:02X}H",
                          (ctrl | 0x80, ctrl | 0xC0),
                          expect_response=expect, retries=0,
                          meta={"any_addr": True, "raw": True})
        result = self.client.scheduler.consume(RequestQueue([request]))[0]
        ui.show_result(result)


def bus_monitor(session: CoexistSession):
    seconds = ui.ask_int("监听秒数", 20, lo=1, hi=600)
    observer = session.scheduler.observer

    def show(frame):
        mark = "表" if frame.is_response else "主站"
        first = p.describe_frame(frame).splitlines()[0]
        print(f"  [{mark}] {first}")

    observer.on_frame = show
    print(f"被动监听 {seconds}s（不发送任何内容）...")
    observer.stats.clear()
    try:
        observer.pump(seconds)
    finally:
        observer.on_frame = None
    interval = observer.est_interval()
    print(f"\n主站请求 {observer.stats['master']} 帧，"
          f"应答 {observer.stats['responses']} 帧，"
          f"校验存疑 {observer.stats['suspect']} 帧")
    if interval is None:
        print("尚未收集到主站请求间隔样本。")
    else:
        print(f"主站间隔中位数 {interval:.2f}s，样本 {len(observer.gaps)}，"
              f"窗口就绪={observer.ready(session.scheduler.cfg.min_samples)}")
    ui.pause()


def settings_menu(session: CoexistSession):
    while True:
        ui.banner("设置与重连")
        session.show_config()
        choice = ui.menu("修改配置", [
            ("1", "目标表地址"),
            ("2", "串口与串口参数（只改本机）"),
            ("3", "每窗次数 / 等窗超时"),
            ("4", "重试次数"),
            ("5", "显示配置"),
            ("6", "重连串口"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            new = ui.address_prompt(session.cfg.get("METER_ADDRESS", ""))
            session.cfg["METER_ADDRESS"] = new
            if session.client is not None:
                session.client.set_address(new)
            ui.save_env(session.cfg)
            print("目标地址已更新。")
        elif choice == "2":
            session.cfg["PORT"] = ui.port_prompt(session.cfg.get("PORT", ""))
            session.cfg["BAUD"] = str(ui.baud_prompt(
                ui.env_int(session.cfg, "BAUD", 2400)))
            session.cfg["PARITY"] = ui.parity_prompt(
                session.cfg.get("PARITY", "E"))
            session.cfg["TX_KEY"] = ui.tx_key_prompt(
                session.cfg.get("TX_KEY", "rts"))
            ui.save_env(session.cfg)
            session.reopen()
        elif choice == "3":
            if session.scheduler is None:
                print("当前未连接，先重连串口。")
                continue
            per_window = ui.ask_int(
                "每窗最多通信次数", session.scheduler.cfg.per_window,
                lo=1, hi=3)
            txn_wait = ui.ask_float(
                "等待主站窗口超时秒", session.scheduler.cfg.txn_wait,
                lo=3)
            session.scheduler.cfg.per_window = per_window
            session.scheduler.cfg.txn_wait = txn_wait
            session.cfg["COEXIST_PER_WINDOW"] = str(per_window)
            session.cfg["COEXIST_TXN_WAIT"] = f"{txn_wait:g}"
            ui.save_env(session.cfg)
            print("已更新，下一轮请求生效。")
        elif choice == "4":
            retries = ui.ask_int(
                "重试次数", ui.env_int(session.cfg, "RETRIES", 2),
                lo=0, hi=9)
            session.cfg["RETRIES"] = str(retries)
            if session.client is not None:
                session.client.retries = retries
            ui.save_env(session.cfg)
            print("重试次数已更新。")
        elif choice == "5":
            session.show_config()
            ui.pause()
        elif choice == "6":
            session.reopen()
            ui.pause()


def main() -> int:
    ui.init_console()
    parser = argparse.ArgumentParser(description="DDS1366 共存主站（窗口注入）")
    parser.add_argument("--address", help="覆盖 .env 中的目标表地址")
    parser.add_argument("--port", help="覆盖 .env 中的串口")
    parser.add_argument("--yes", action="store_true",
                        help="跳过高风险操作短语确认")
    args = parser.parse_args()

    cfg = ui.load_env()
    if args.address:
        cfg["METER_ADDRESS"] = args.address
    if args.port:
        cfg["PORT"] = args.port

    ui.banner("共存主站模拟（窗口注入）")
    print("所有帧只在主站事务结束后的空窗发送；读、写、控制、广播均已开放。")
    print("广播和原始帧仍可能影响全总线，每项操作需要单独确认。")
    print("每窗最多 3 次，按实测间隔自动降级/回滚；超时按重试次数移到后续窗。")

    session = CoexistSession(cfg, args.yes)
    if not session.connect():
        print("首次连接失败，进入设置修正串口后重试。")
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
                ("5", "通信速率"),
                ("6", "发送原始帧"),
                ("7", "设置与重连"),
                ("8", "监听总线并评估窗口"),
                ("0", "退出"),
            ])
            if choice == "0":
                break
            try:
                if choice == "1":
                    read_menu(session)
                elif choice == "2":
                    run_monitor(session, coexist=True)
                elif choice == "3":
                    write_menu(session)
                elif choice == "4":
                    relay_menu(session)
                elif choice == "5":
                    baud_menu(session)
                elif choice == "6":
                    raw_menu(session)
                elif choice == "7":
                    settings_menu(session)
                elif choice == "8":
                    bus_monitor(session)
            except KeyboardInterrupt:
                print("\n已取消本次等待，返回菜单。")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
