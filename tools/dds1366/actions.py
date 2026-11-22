# -*- coding: utf-8 -*-
"""主站公共交互动作：读取、写参数、继电器、地址、速率、原始帧。"""
from __future__ import annotations

import re
import time

from . import protocol as p
from . import ui
from .client import CREDENTIALS, OK_STATUS

READ_GROUPS = {
    "1": ("总电能", [0x00000000]),
    "2": ("电能数据块", [0x0000FF00]),
    "3": ("电压/功率/功率因数", [0x02010100, 0x02020100, 0x02030000,
                             0x02040000, 0x02050000, 0x02060000]),
    "4": ("运行状态字", [0x04000501, 0x04000502, 0x04000503]),
    "5": ("厂家组合读 F9010100", [0xF9010100]),
    "6": ("厂家参数块 F9030100", [0xF9030100]),
    "7": ("全部已知数据项", [item.di for item in p.DI_TABLE]),
}


def show_many(results):
    for result in results:
        ui.show_result(result)


def read_menu(session):
    while True:
        choice = ui.menu("读取数据", [
            (key, value[0]) for key, value in READ_GROUPS.items()
        ] + [("8", "自定义 DI"), ("0", "返回")])
        if choice == "0":
            return
        if choice in READ_GROUPS:
            show_many(session.client.read_many(READ_GROUPS[choice][1]))
            ui.pause()
            continue
        if choice == "8":
            text = ui.ask("数据标识 DI（8 位 HEX，如 00000000）", "00000000")
            try:
                di = int(text.replace("0x", "").replace("0X", ""), 16)
                if not 0 <= di <= 0xFFFFFFFF:
                    raise ValueError
            except ValueError:
                print("DI 应为 32 位 HEX。")
                continue
            ui.show_result(session.client.read(di))
            ui.pause()


def write_menu(session):
    while True:
        choice = ui.menu("写参数", [
            ("1", "广播分组标志（广播拉合闸行为设置，"
                  "读 F9030100 → 改 byte[2] → 回读校验）"),
            ("2", "写 F9AAAA01 数值（物理意义未定）"),
            ("3", "自定义写（DI/PA/密码/内容）"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            result = session.client.read_parameter()
            if result.status not in OK_STATUS:
                ui.show_result(result)
                ui.pause()
                continue
            current = result.payload[2]
            print(f"当前 byte[2]={current:02X}H ({p.group_flag_desc(current)})")
            text = ui.ask("目标组名（两位 HEX，如 00/AA/BB；留空=不修改）", "")
            if not text:
                print("保持现状，不改动。")
                continue
            flag = p.parse_group_flag(text)
            if flag is None:
                print("组名应为两位 HEX，如 00/AA/BB。")
                continue
            if not session.gate("修改广播分组标志", [
                f"目标 byte[2]={flag:02X}H ({p.group_flag_desc(flag)})",
                "广播分组标志决定本表响应哪个组名的广播拉/合闸，"
                "写入后自动回读校验。",
            ]):
                continue
            ok, detail = session.client.set_group_flag(flag)
            print(("成功: " if ok else "失败: ") + detail)
            ui.pause()
            continue
        if choice == "2":
            value = ui.ask_float("写入值（两位小数，BCD 4B）", 0.0, lo=0)
            cents = int(round(value * 100))
            if cents > 99999999:
                print("数值超出 4 字节 BCD 范围。")
                continue
            if not session.gate("写 F9AAAA01", [
                f"写入值 {cents / 100:.2f}"
                f"（编码 {p.bcd_bytes_le(cents, 4).hex(' ').upper()}）",
                "该参数物理意义未确定，可能影响表内计量或结算。",
            ]):
                continue
            ui.show_result(session.client.write_value_f9aaaa01(cents))
            ui.pause()
            continue
        if choice == "3":
            text = ui.ask("数据标识 DI（8 位 HEX）", "F9030100")
            pa_text = ui.ask("密码等级 PA（2 位 HEX）", "00")
            try:
                di = int(text, 16)
                pa = int(pa_text, 16)
            except ValueError:
                print("DI/PA 格式错误。")
                continue
            pwd = ui.ask("密码（6 位 HEX）", CREDENTIALS.get(pa, "000000"))
            operator = ui.ask("操作者代码（8 位 HEX）", "00000000")
            content = ui.ask_hex_bytes("内容 HEX（不含 33H 偏移）", "00")
            try:
                if not (0 <= di <= 0xFFFFFFFF and 0 <= pa <= 0xFF):
                    raise ValueError
                if len(bytes.fromhex(pwd)) != 3 \
                        or len(bytes.fromhex(operator)) != 4:
                    raise ValueError
            except ValueError:
                print("DI/PA/密码/操作者代码格式错误。")
                continue
            if not session.gate("自定义写数据", [
                f"DI={di:08X} PA={pa:02X} 密码={pwd} "
                f"内容={content.hex(' ').upper()}",
                "直接写表内参数，内容错误可能导致表计异常。",
            ]):
                continue
            ui.show_result(
                session.client.write(di, content, pa, pwd, operator))
            ui.pause()


def relay_menu(session):
    while True:
        choice = ui.menu("继电器 / 跳合闸", [
            ("1", "单播跳闸（1AH）"),
            ("2", "单播合闸（1CH）"),
            ("3", "跳闸 → 保持 → 自动恢复"),
            ("4", "广播拉闸（99×6，1AH，连发 2 遍）"),
            ("5", "广播合闸（99×6，1CH，连发 2 遍）"),
            ("6", "读继电器状态（F9010100）"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "6":
            ui.show_result(session.client.read(0xF9010100))
            ui.pause()
            continue
        if choice in ("1", "2"):
            trip = choice == "1"
            if not session.gate("单播跳闸" if trip else "单播合闸", [
                "会直接操作本表继电器，可能造成用户停电或来电。",
                "仅限隔离台架或已获授权的现场。",
            ]):
                continue
            result = session.client.trip_relay() if trip \
                else session.client.close_relay()
            ui.show_result(result)
            ui.pause()
            continue
        if choice == "3":
            seconds = ui.ask_int("跳闸保持秒数", 5, lo=1)
            if not session.gate("跳闸后自动恢复", [
                f"先单播跳闸，保持 {seconds}s，再单播合闸。",
                "跳闸可能导致用户停电；若合闸失败必须人工处理。",
            ]):
                continue
            ui.show_result(session.client.trip_relay())
            print(f"保持 {seconds}s ...")
            time.sleep(seconds)
            ui.show_result(session.client.close_relay())
            ui.pause()
            continue
        if choice in ("4", "5"):
            trip = choice == "4"
            default_group = ui.env_group(session.cfg)
            text = ui.ask(
                f"目标组名（两位 HEX，如 AA/BB；回车=默认 {default_group}）",
                default_group)
            group = p.parse_group_flag(text)
            if group is None:
                print("组名应为两位 HEX，如 AA/BB。")
                continue
            if not session.gate(
                    "广播" + ("拉闸" if trip else "合闸"), [
                        "广播地址 999999999999，总线上所有表都会接收。",
                        f"N2 为目标分组选择器：本帧 N2={group:02X}H，"
                        f"只作用于广播分组标志 byte[2]={group:02X}H 的表。",
                        "广播合闸只恢复对应分组且状态字 08 06 的表。",
                        "禁止在运行总线上执行。",
                    ]):
                continue
            results = session.client.broadcast_control(
                0x1A if trip else 0x1C, group)
            show_many(results)
            time.sleep(0.5)
            print("读目标表状态确认:")
            ui.show_result(session.client.read(0xF9010100))
            ui.pause()


def address_menu(session):
    while True:
        choice = ui.menu("通信地址", [
            ("1", "读通信地址（AA×6 + 13H，总线上所有表都会应答）"),
            ("2", "改通信地址（AA×6 + 15H，高风险）"),
            ("0", "返回"),
        ])
        if choice == "0":
            return
        if choice == "1":
            ui.show_result(session.client.read_address())
            ui.pause()
            continue
        if choice == "2":
            new = ui.address_prompt()
            if new == session.cfg.get("METER_ADDRESS"):
                print("新旧地址相同，已取消。")
                continue
            if not session.gate("修改通信地址", [
                f"当前 {session.cfg.get('METER_ADDRESS')} → 新地址 {new}",
                "改址后旧地址失效；若未记录新地址可能失去对表的访问。",
                "AA×6 改址会作用到总线上所有收到该帧的表。",
                "部分表需重新上电或重新协商后才按新地址应答。",
            ]):
                continue
            result = session.client.change_address(new)
            ui.show_result(result)
            if result.status in OK_STATUS:
                session.cfg["METER_ADDRESS"] = new
                ui.save_env(session.cfg)
                print("配置中的目标地址已同步为新地址。")
            ui.pause()


def baud_menu(session):
    current = ui.env_int(session.cfg, "BAUD", 2400)
    print(f"当前本机速率: {current}bps")
    rate = ui.baud_prompt(current)
    if rate == current:
        print("速率相同，已取消。")
        return
    if not session.gate("更改通信速率", [
        f"从 {current}bps 改为 {rate}bps。",
        "表在应答后数秒切换；切换期间可能失联。",
        "若无法再通信，需要用设置菜单把本机速率改回。",
    ]):
        return
    result = session.client.set_baud(rate)
    ui.show_result(result)
    if result.status not in OK_STATUS:
        return
    print("等待 3s 让表完成切换 ...")
    time.sleep(3.0)
    if ui.confirm(f"把本机串口切到 {rate}bps?", default=True):
        session.link.set_baud(rate)
        session.cfg["BAUD"] = str(rate)
        ui.save_env(session.cfg)
        print(f"本机已切换为 {rate}bps，配置已保存。")
    ui.pause()


def raw_menu(session):
    ui.banner("发送原始帧")
    print("输入完整帧 HEX（含 68 起始、CS 与 16H 结束），不做任何改写。")
    data = ui.ask_hex_bytes(
        "帧 HEX", "68 AA AA AA AA AA AA 68 13 00 DF 16")
    if not session.gate("发送原始帧", [
        f"{data.hex(' ').upper()}",
        "原始帧可绕过上层校验，错误内容可能改变表计状态。",
    ]):
        return
    session.raw(data)
    ui.pause()
