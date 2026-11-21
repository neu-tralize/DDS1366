# -*- coding: utf-8 -*-
"""DDS1366 电表模拟器（645-2007 从站）交互入口。"""
from __future__ import annotations

import argparse
import sys

from dds1366 import meter as m
from dds1366 import protocol as p
from dds1366 import ui
from dds1366.link import Link, SerialConfig

STATE_PATH = ui.BASE_DIR / "meter_sim.json"


def relay_choices(state: m.MeterState):
    while True:
        choice = ui.menu("继电器状态", [
            ("1", "0000 合闸正常"),
            ("2", "0802 远程拉闸"),
            ("3", "0806 定时拉闸"),
            ("4", "0002 跳闸已接受/继电器未断"),
            ("5", "0800 合闸清除中"),
            ("6", "自定义 2 字节 HEX"),
            ("0", "返回"),
        ])
        fixed = {"1": "0000", "2": "0802", "3": "0806",
                 "4": "0002", "5": "0800"}
        if choice in fixed:
            state.status = bytes.fromhex(fixed[choice])
            return
        if choice == "6":
            raw = ui.ask_hex_bytes("状态字 2 字节 HEX", "0000")
            if len(raw) == 2:
                state.status = raw
                return
            print("必须为 2 字节。")
        elif choice == "0":
            return


def energy_menu(state: m.MeterState):
    total = ui.ask_float("总电能 kWh", state.total_cents() / 100, lo=0)
    state.set_total_cents(int(round(total * 100)))
    if ui.confirm("继续分别设置费率 1~4（总电能将变为四费率之和）?",
                  default=False):
        for index in range(4):
            value = ui.ask_float(f"费率{index + 1} kWh",
                                 state.rates[index] / 100, lo=0)
            state.rates[index] = int(round(value * 100))


def param_menu(state: m.MeterState):
    while True:
        block = state.param_block
        print(f"\nF9030100 ({len(block)}B): {block.hex(' ').upper()}")
        print(f"byte[2]={block[2]:02X}H ({p.keep_flag_desc(block[2])})")
        choice = ui.menu("参数块", [
            ("1", "设置保电标志 byte[2]"),
            ("2", "整块 30 字节写入 HEX"),
            ("3", "设置 F9AAAA01 数值"),
            ("4", "设置 F9030200 10 字节 HEX"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            flag_text = ui.ask("标志 1=保电(00) 2=非保电(AA) 3=BB(行为同 00)", "1")
            flag = {"1": 0x00, "2": 0xAA, "3": 0xBB}.get(flag_text, 0xAA)
            state.param_block = block[:2] + bytes([flag]) + block[3:]
            print(f"byte[2] 已改为 {flag:02X}H")
        elif choice == "2":
            raw = ui.ask_hex_bytes("30 字节 HEX", block.hex())
            if len(raw) == 30:
                state.param_block = raw
            else:
                print("长度必须为 30 字节。")
        elif choice == "3":
            value = ui.ask_float("F9AAAA01 值（两位小数）",
                                 state.f9aaaa01_cents / 100, lo=0)
            state.f9aaaa01_cents = int(round(value * 100))
        elif choice == "4":
            raw = ui.ask_hex_bytes("10 字节 HEX", state.f9030200.hex())
            if len(raw) == 10:
                state.f9030200 = raw
            else:
                print("长度必须为 10 字节。")


def measurement_menu(state: m.MeterState):
    while True:
        print(f"\n电压 {state.voltage_dv / 10:.1f} V  "
              f"有功 {state.power_mw / 1000:.4f} kW  "
              f"无功 {state.reactive_mvar / 1000:.4f} kvar  "
              f"视在 {state.apparent_mva / 1000:.4f} kVA  "
              f"功率因数 {state.pf_milli / 1000:.3f}")
        print(f"运行状态字: " + "  ".join(
            x.hex().upper() for x in state.run_status))
        choice = ui.menu("测量值", [
            ("1", "电压 V（0.1V）"),
            ("2", "有功功率 kW（步进 0.001，线上 XX.XXXX）"),
            ("3", "无功功率 kvar（步进 0.001，线上 XX.XXXX）"),
            ("4", "视在功率 kVA（步进 0.001，线上 XX.XXXX）"),
            ("5", "功率因数（步进 0.001，线上 X.XXX）"),
            ("6", "运行状态字 1~3"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            state.voltage_dv = int(round(
                ui.ask_float("电压 V", state.voltage_dv / 10, lo=0) * 10))
        elif choice == "2":
            state.power_mw = int(round(
                ui.ask_float("有功 kW", state.power_mw / 1000) * 1000))
        elif choice == "3":
            state.reactive_mvar = int(round(
                ui.ask_float("无功 kvar", state.reactive_mvar / 1000) * 1000))
        elif choice == "4":
            state.apparent_mva = int(round(
                ui.ask_float("视在 kVA", state.apparent_mva / 1000) * 1000))
        elif choice == "5":
            state.pf_milli = int(round(
                ui.ask_float("功率因数", state.pf_milli / 1000) * 1000))
        elif choice == "6":
            words = []
            for index in range(3):
                raw = ui.ask_hex_bytes(
                    f"状态字{index + 1} 2 字节 HEX",
                    state.run_status[index].hex())
                words.append(raw if len(raw) == 2 else state.run_status[index])
            state.run_status = words


def serial_menu(session: "SimSession"):
    cfg = session.cfg
    cfg["PORT"] = ui.port_prompt(cfg.get("PORT", ""))
    cfg["BAUD"] = str(ui.baud_prompt(ui.env_int(cfg, "BAUD", 2400)))
    cfg["PARITY"] = ui.parity_prompt(cfg.get("PARITY", "E"))
    cfg["TX_KEY"] = ui.tx_key_prompt(cfg.get("TX_KEY", "rts"))
    ui.save_env(cfg)


def behavior_menu(state: m.MeterState):
    while True:
        print(f"\n唤醒 FE: {state.wake_fe} 个  "
              f"广播写 F9AAAA01 清电能: "
              f"{'无条件执行' if state.broadcast_clear_energy else '忽略'}")
        choice = ui.menu("模拟行为", [
            ("1", "设置唤醒 FE 字节数"),
            ("2", "切换广播写 F9AAAA01 是否清电能"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            state.wake_fe = ui.ask_int("FE 个数", state.wake_fe, lo=0, hi=4)
        elif choice == "2":
            state.broadcast_clear_energy = not state.broadcast_clear_energy


class SimSession:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.state = m.load(str(STATE_PATH))
        if not STATE_PATH.exists():
            address = p.validate_address(cfg.get("METER_ADDRESS", ""))
            if address:
                self.state.address = address
        self.link = None

    def save(self):
        m.save(str(STATE_PATH), self.state)

    def summary(self):
        cfg = self.cfg
        state = self.state
        print(f"  地址 {state.address}  "
              f"电能 {state.total_cents() / 100:.2f} kWh  "
              f"继电器 {state.status.hex().upper()}  "
              f"保电标志 {state.keep_flag:02X}H")
        print(f"  串口 {cfg.get('PORT') or '(自动)'} "
              f"{ui.env_int(cfg, 'BAUD', 2400)} 8"
              f"{cfg.get('PARITY', 'E')}1  "
              f"方向脚={cfg.get('TX_KEY', 'rts')}")

    def run(self, seconds: float | None = None) -> int:
        port = self.cfg.get("PORT") or ui.port_prompt()
        self.cfg["PORT"] = port
        port = self.cfg["PORT"]
        baud = ui.env_int(self.cfg, "BAUD", 2400)
        parity = self.cfg.get("PARITY", "E").upper()
        tx_key = self.cfg.get("TX_KEY", "rts").lower()
        try:
            link = Link(SerialConfig(port, baud, parity, tx_key))
        except Exception as exc:
            print(f"打开串口失败: {exc}")
            return 1
        ui.save_env(self.cfg)
        state = self.state
        state.pending_baud = None
        simulator = m.MeterSimulator(state, log=print)
        print(f"\n模拟开始: {state.address}  {port} {baud} 8{parity}1  "
              f"电能 {state.total_cents() / 100:.2f} kWh  "
              f"继电器 {state.status.hex().upper()}  "
              f"保电标志 {state.keep_flag:02X}H")
        print("Ctrl+C 停止并返回菜单。未实现指令一律静默。\n")
        try:
            m.simulate(link, simulator, log=print, stop_after=seconds)
        except KeyboardInterrupt:
            pass
        finally:
            link.close()
            self.save()
            print(f"\n本表收 {simulator.rx_own}，其他地址 {simulator.rx_other}，"
                  f"应答 {simulator.replies}")
            print(f"结束时电能 {state.total_cents() / 100:.2f} kWh，"
                  f"继电器 {state.status.hex().upper()}")
        return 0


def main() -> int:
    ui.init_console()
    parser = argparse.ArgumentParser(description="DDS1366 电表模拟器（645 从站）")
    parser.add_argument("--port", help="覆盖 .env 中的串口")
    parser.add_argument("--run", action="store_true",
                        help="不显示菜单，直接开始模拟")
    parser.add_argument("--seconds", type=float,
                        help="配合 --run，模拟若干秒后自动停止")
    args = parser.parse_args()

    cfg = ui.load_env()
    if args.port:
        cfg["PORT"] = args.port
    session = SimSession(cfg)
    if args.run:
        return session.run(args.seconds)

    try:
        while True:
            ui.banner("电表模拟器（DL/T 645-2007 从站）")
            session.summary()
            choice = ui.menu("主菜单", [
                ("1", "表地址"),
                ("2", "电能与费率"),
                ("3", "继电器状态"),
                ("4", "参数块与保电标志"),
                ("5", "测量值 / 运行状态"),
                ("6", "串口设置"),
                ("7", "模拟行为"),
                ("8", "开始模拟"),
                ("9", "保存配置"),
                ("0", "保存并退出"),
            ])
            if choice == "0":
                session.save()
                break
            if choice == "1":
                session.state.address = ui.address_prompt(
                    session.state.address)
            elif choice == "2":
                energy_menu(session.state)
            elif choice == "3":
                relay_choices(session.state)
            elif choice == "4":
                param_menu(session.state)
            elif choice == "5":
                measurement_menu(session.state)
            elif choice == "6":
                serial_menu(session)
                continue
            elif choice == "7":
                behavior_menu(session.state)
            elif choice == "8":
                session.save()
                session.run()
                continue
            elif choice == "9":
                session.save()
                print("已保存。")
                continue
            else:
                continue
            session.save()
    except KeyboardInterrupt:
        print("\n已中断。")
        session.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
