# -*- coding: utf-8 -*-
"""交互层：菜单、输入校验、风险确认与 .env 配置。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from . import protocol as p

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
ADDRESSES_KEY = "METER_ADDRESSES"
_INPUT_CLOSED = False

ENV_ORDER = (
    "METER_ADDRESS", "METER_ADDRESSES", "PORT", "BAUD", "PARITY", "TX_KEY",
    "BROADCAST_GROUP", "RETRIES", "RESPONSE_TIMEOUT", "COEXIST_PER_WINDOW",
    "COEXIST_TXN_WAIT",
)

DEFAULT_GROUP = "AA"


def init_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def banner(title: str):
    print("\n" + "=" * 58)
    print(" " + title)
    print("=" * 58)


def menu(title: str, options) -> str:
    global _INPUT_CLOSED
    banner(title)
    for key, text in options:
        print(f"  {key}. {text}")
    try:
        return input("请选择: ").strip()
    except (EOFError, KeyboardInterrupt):
        _INPUT_CLOSED = True
        print()
        return "0"


def ask(prompt: str, default: str = "") -> str:
    global _INPUT_CLOSED
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        _INPUT_CLOSED = True
        print()
        return default
    return value or default


def ask_int(prompt: str, default: int, lo: int | None = None,
            hi: int | None = None):
    while True:
        text = ask(prompt, str(default))
        if _INPUT_CLOSED:
            return default
        try:
            value = int(text)
        except ValueError:
            print("  请输入整数。")
            continue
        if lo is not None and value < lo:
            print(f"  不能小于 {lo}。")
            continue
        if hi is not None and value > hi:
            print(f"  不能大于 {hi}。")
            continue
        return value


def ask_float(prompt: str, default: float, lo: float | None = None,
              hi: float | None = None):
    while True:
        text = ask(prompt, f"{default:g}")
        if _INPUT_CLOSED:
            return default
        try:
            value = float(text)
        except ValueError:
            print("  请输入数字。")
            continue
        if lo is not None and value < lo:
            print(f"  不能小于 {lo}。")
            continue
        if hi is not None and value > hi:
            print(f"  不能大于 {hi}。")
            continue
        return value


def ask_hex_bytes(prompt: str, default: str = "") -> bytes:
    while True:
        text = ask(prompt, default)
        if _INPUT_CLOSED:
            try:
                return p.parse_hex_bytes(text)
            except ValueError:
                return b""
        try:
            data = p.parse_hex_bytes(text)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        if not data:
            print("  不能为空。")
            continue
        return data


def confirm(prompt: str, default: bool = False) -> bool:
    global _INPUT_CLOSED
    suffix = " [Y/n]" if default else " [y/N]"
    try:
        text = input(prompt + suffix + ": ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _INPUT_CLOSED = True
        print()
        return False
    if not text:
        return default
    return text in ("y", "yes", "是", "1")


def confirm_risky(title: str, lines) -> bool:
    global _INPUT_CLOSED
    print("\n" + "!" * 58)
    print("高风险操作: " + title)
    for line in lines:
        print("  " + line)
    print("!" * 58)
    try:
        text = input("确认执行? (y/N): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        _INPUT_CLOSED = True
        print()
        return False
    return text == "y"


def pause(prompt: str = "按回车继续"):
    global _INPUT_CLOSED
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        _INPUT_CLOSED = True
        print()


def load_env(path: Path | str | None = None) -> dict:
    path = Path(path or ENV_PATH)
    cfg = {}
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                cfg[key.strip()] = value
    except OSError:
        pass
    return cfg


def save_env(cfg: dict, path: Path | str | None = None):
    path = Path(path or ENV_PATH)
    set_env_addresses(cfg, env_addresses(cfg))
    keys = list(ENV_ORDER) + sorted(k for k in cfg if k not in ENV_ORDER)
    lines = ["# DDS1366 工具默认配置；本文件含现场地址，请勿提交。"]
    for key in keys:
        if key in cfg:
            lines.append(f"{key}={cfg[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def env_addresses(cfg: dict) -> list[str]:
    """已储存的地址集合（去重保序），当前目标始终包含在内。"""
    items = []
    for chunk in re.split(r"[,;\s]+", cfg.get(ADDRESSES_KEY, "")):
        value = p.validate_address(chunk)
        if value and value not in items:
            items.append(value)
    current = p.validate_address(cfg.get("METER_ADDRESS", ""))
    if current and current not in items:
        items.append(current)
    return items


def set_env_addresses(cfg: dict, addresses) -> None:
    values = []
    for item in addresses:
        value = p.validate_address(item)
        if value and value not in values:
            values.append(value)
    if values:
        cfg[ADDRESSES_KEY] = ",".join(values)
    else:
        cfg.pop(ADDRESSES_KEY, None)


def resolve_address_text(text: str, addresses):
    """12 位输入视为完整地址，不足 12 位按后缀匹配已存集合。

    返回 (地址, 是否新地址, 失败原因)；地址为 None 时第三项说明原因。
    """
    raw = "".join(str(text).split())
    if not raw or not (raw.isascii() and raw.isdigit()):
        return None, False, "地址只能包含数字 0-9。"
    if len(raw) > 12:
        return None, False, "地址不能超过 12 位。"
    if len(raw) == 12:
        value = p.validate_address(raw)
        if value is None:
            return None, False, "不能使用广播地址 999999999999。"
        return value, value not in addresses, ""
    matches = [item for item in addresses if item.endswith(raw)]
    if not matches:
        return None, False, f"没有已储存地址以 {raw} 结尾。"
    if len(matches) > 1:
        return None, False, (f"后缀 {raw} 匹配到多个地址："
                             + "，".join(matches) + "；请多输入几位。")
    return matches[0], False, ""


def address_prompt(current: str = "") -> str:
    while True:
        text = ask("目标表地址(12 位以内数字，不足补零)", current)
        if _INPUT_CLOSED:
            return text
        value = p.validate_address(text)
        if value is None:
            print("  地址不合法：只能是 0-9，且不能是广播地址 999999999999。")
            continue
        return value


def port_prompt(current: str = "") -> str:
    from . import link
    print(link.ports_text())
    suggest = link.suggest_port() or current
    while True:
        text = ask("串口(留空=自动选择)", suggest)
        if _INPUT_CLOSED:
            return text
        if text:
            return text
        if suggest:
            return suggest


def baud_prompt(current: int = 2400) -> int:
    rates = [1200, 2400, 4800, 9600]
    print("可选速率: " + " / ".join(str(x) for x in rates))
    while True:
        value = ask_int("波特率", current)
        if _INPUT_CLOSED:
            return current if current in rates else 2400
        if value in rates:
            return value
        print("  本表实测只支持 1200/2400/4800/9600。")


def parity_prompt(current: str = "E") -> str:
    value = ask("校验位 N/E/O", current).upper()
    return value if value in ("N", "E", "O") else current


def tx_key_prompt(current: str = "rts") -> str:
    value = ask("RS485 方向脚 rts/dtr/none", current).lower()
    return value if value in ("rts", "dtr", "none") else current


def env_group(cfg: dict) -> str:
    """广播拉/合闸帧默认组名（两位 HEX）；未设置或非法时回落 AA。"""
    value = p.parse_group_flag(cfg.get("BROADCAST_GROUP", ""))
    return f"{value:02X}" if value is not None else DEFAULT_GROUP


def env_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def env_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def describe_result(result) -> str:
    names = {"ok": "成功", "late": "迟到成功", "err": "表拒绝",
             "timeout": "超时", "failed": "失败", "aborted": "中断",
             "no_master": "无主站窗口"}
    line = f"[{result.request.label}] {names.get(result.status, result.status)}"
    if result.status in ("ok", "late") and result.value:
        line += f" → {result.value}"
    elif result.status == "err" and result.frame is not None:
        line += f" → {p.err_desc(result.frame)}"
    elif result.detail:
        line += f" → {result.detail}"
    return line


def show_result(result):
    print(describe_result(result))


def selftest():
    import tempfile
    addrs = ["000000123456", "000000123457"]
    merged = env_addresses({"METER_ADDRESS": "123456",
                            ADDRESSES_KEY: "123457,000000123456"})
    assert sorted(merged) == addrs, merged
    assert resolve_address_text("123456", addrs) == \
        ("000000123456", False, "")
    assert resolve_address_text(" 3456 ", addrs) == \
        ("000000123456", False, "")
    pair = ["000000123456", "000000223456"]
    assert resolve_address_text("3456", pair)[0] is None
    assert resolve_address_text("123456", pair) == \
        ("000000123456", False, "")
    assert resolve_address_text("１２３", addrs)[0] is None
    assert resolve_address_text("123458", addrs)[0] is None
    assert resolve_address_text("000000123458", addrs) == \
        ("000000123458", True, "")
    assert resolve_address_text("12345", addrs)[0] is None
    assert resolve_address_text("9999", addrs)[0] is None
    assert resolve_address_text("999999999999", addrs)[0] is None
    assert resolve_address_text("1234567890123", addrs)[0] is None
    assert resolve_address_text("12x", addrs)[0] is None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".env"
        cfg = {"METER_ADDRESS": "123456", ADDRESSES_KEY: "123457"}
        save_env(cfg, path)
        loaded = load_env(path)
        assert loaded["METER_ADDRESS"] == "123456"
        assert loaded["METER_ADDRESSES"] == \
            "000000123457,000000123456", loaded["METER_ADDRESSES"]


def _main():
    # 便于直接运行 python -m dds1366.ui 检查配置解析
    cfg = load_env()
    print(f"配置文件: {ENV_PATH}")
    for key in ENV_ORDER:
        print(f"  {key}={cfg.get(key, '(未设置)')}")


if __name__ == "__main__":
    init_console()
    _main()
