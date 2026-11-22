# -*- coding: utf-8 -*-
"""DL/T 645-2007 帧编解码与 DDS1366 实测数据项。

只依赖标准库。功率类缩放为标准的 XX.XXXX（0.0001 kW/kvar/kVA），
已经与表显实时功率核对。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

START = 0x68
END = 0x16
OFFSET = 0x33
MAX_L = 200
BCAST_DISPLAY = "999999999999"
BCAST_WIRE = b"\x99" * 6
SHORT_WIRE = b"\xAA" * 6


# ------------------------------------------------------------------ 基础编码
def cs_of(body: bytes) -> int:
    return sum(body) & 0xFF


def add33(data: bytes) -> bytes:
    return bytes((b + OFFSET) & 0xFF for b in data)


def sub33(data: bytes) -> bytes:
    return bytes((b - OFFSET) & 0xFF for b in data)


def wire_addr_of(display: str) -> bytes:
    s = "".join(str(display).split())
    if not s.isdigit() or len(s) > 12:
        raise ValueError("表地址应为 0-9 数字，最长 12 位")
    return bytes.fromhex(s.zfill(12))[::-1]


def display_addr(wire: bytes) -> str:
    if len(wire) != 6:
        raise ValueError("地址域必须为 6 字节")
    return "".join(f"{b:02X}" for b in reversed(wire))


def validate_address(value: str, *, allow_broadcast: bool = False):
    s = "".join(str(value).split())
    if not s.isdigit() or not (1 <= len(s) <= 12):
        return None
    s = s.zfill(12)
    if s == BCAST_DISPLAY and not allow_broadcast:
        return None
    return s


def di_le_bytes(di: int) -> bytes:
    return int(di).to_bytes(4, "big")[::-1]


def parse_hex_bytes(text: str) -> bytes:
    s = "".join(text.replace("0x", " ").replace("0X", " ").split())
    if len(s) % 2:
        raise ValueError("HEX 字节数必须为偶数")
    return bytes.fromhex(s)


# ------------------------------------------------------------------ 帧构造
def build_frame(addr_wire: bytes, ctrl: int, data: bytes = b"") -> bytes:
    if len(addr_wire) != 6:
        raise ValueError("addr_wire 必须为 6 字节")
    if len(data) > MAX_L:
        raise ValueError("数据域过长")
    body = bytes([START, *addr_wire, START, ctrl & 0xFF, len(data)]) + data
    return body + bytes([cs_of(body), END])


def build_read(addr_wire: bytes, di: int) -> bytes:
    return build_frame(addr_wire, 0x11, add33(di_le_bytes(di)))


def build_read_address() -> bytes:
    return build_frame(SHORT_WIRE, 0x13)


def build_write(addr_wire: bytes, di: int, pa: int, pwd_hex: str,
                content: bytes, operator_hex: str = "00000000") -> bytes:
    pwd = bytes.fromhex(pwd_hex)
    op = bytes.fromhex(operator_hex)
    if len(pwd) != 3 or len(op) != 4:
        raise ValueError("密码须为 3 字节、操作者代码须为 4 字节")
    raw = di_le_bytes(di) + bytes([pa & 0xFF]) + pwd + op + content
    return build_frame(addr_wire, 0x14, add33(raw))


def build_control(addr_wire: bytes, pa: int, pwd_hex: str, n1: int,
                  n2: int = 0x00, operator_hex: str = "00000000",
                  tail: bytes = b"\x00" * 6) -> bytes:
    pwd = bytes.fromhex(pwd_hex)
    op = bytes.fromhex(operator_hex)
    if len(pwd) != 3 or len(op) != 4:
        raise ValueError("密码须为 3 字节、操作者代码须为 4 字节")
    raw = bytes([pa & 0xFF]) + pwd + op + bytes([n1 & 0xFF, n2 & 0xFF]) + tail
    return build_frame(addr_wire, 0x1C, add33(raw))


RATE_FEATURE = {600: 0x02, 1200: 0x04, 2400: 0x08,
                4800: 0x10, 9600: 0x20, 19200: 0x40}
FEATURE_RATE = {v: k for k, v in RATE_FEATURE.items()}


def build_baud(addr_wire: bytes, rate: int) -> bytes:
    if rate not in RATE_FEATURE:
        raise ValueError("速率须为 600/1200/2400/4800/9600/19200")
    return build_frame(addr_wire, 0x17, add33(bytes([RATE_FEATURE[rate]])))


def build_write_address(new_address: str) -> bytes:
    new_wire = wire_addr_of(new_address)
    if new_wire in (BCAST_WIRE, SHORT_WIRE):
        raise ValueError("新地址不能是广播或缩位地址")
    return build_frame(SHORT_WIRE, 0x15, add33(new_wire))


# ------------------------------------------------------------------ 帧解析
@dataclass
class Frame:
    start: int
    raw: bytes
    addr_wire: bytes
    ctrl: int
    data: bytes
    cs: int
    cs_ok: bool
    ts: float | None = None

    @property
    def addr_display(self) -> str:
        return display_addr(self.addr_wire)

    @property
    def is_response(self) -> bool:
        return bool(self.ctrl & 0x80)

    @property
    def is_exception(self) -> bool:
        return bool(self.ctrl & 0x40)

    @property
    def command(self) -> int:
        return self.ctrl & 0x1F


def _ts_at(markers, off):
    if not markers:
        return None
    idx = bisect.bisect_right([m[0] for m in markers], off) - 1
    return markers[idx][1] if idx >= 0 else None


def find_frames(buf: bytes, markers=None):
    valid, suspects = [], []
    n = len(buf)
    i = 0
    while i < n - 1:
        if buf[i] != START:
            i += 1
            continue
        if i + 10 > n:
            break
        if buf[i + 7] != START:
            i += 1
            continue
        length = buf[i + 9]
        if length > MAX_L:
            i += 1
            continue
        end = i + 12 + length
        if end > n:
            break
        if buf[end - 1] != END:
            i += 1
            continue
        raw = bytes(buf[i:end])
        cs_ok = cs_of(raw[:-2]) == raw[-2]
        frame = Frame(i, raw, raw[1:7], raw[8], raw[10:10 + length],
                      raw[-2], cs_ok, _ts_at(markers, i))
        (valid if cs_ok else suspects).append(frame)
        i = end
    return valid, suspects


# ------------------------------------------------------------------ 数据项目录
@dataclass(frozen=True)
class DiInfo:
    di: int
    name: str
    length: int
    unit: str = ""
    writable: bool = False
    note: str = ""


DI_TABLE = (
    DiInfo(0x00000000, "组合有功总电能", 4, "kWh"),
    DiInfo(0x00000100, "组合有功费率1电能", 4, "kWh"),
    DiInfo(0x00000200, "组合有功费率2电能", 4, "kWh"),
    DiInfo(0x00000300, "组合有功费率3电能", 4, "kWh"),
    DiInfo(0x00000400, "组合有功费率4电能", 4, "kWh"),
    DiInfo(0x0000FF00, "电能数据块(总+费率1~4)", 20, "kWh"),
    DiInfo(0x02010100, "A相电压", 2, "V"),
    DiInfo(0x02020100, "需量/平均有功功率(实测推断)", 3, "kW",
           note="标准此 DI 为 A 相电流，本表不是电流"),
    DiInfo(0x02030000, "瞬时总有功功率", 3, "kW"),
    DiInfo(0x02030100, "瞬时A相有功功率", 3, "kW"),
    DiInfo(0x02040000, "瞬时总无功功率", 3, "kvar"),
    DiInfo(0x02040100, "瞬时A相无功功率", 3, "kvar"),
    DiInfo(0x02050000, "瞬时总视在功率", 3, "kVA"),
    DiInfo(0x02050100, "瞬时A相视在功率", 3, "kVA"),
    DiInfo(0x02060000, "总功率因数", 2, ""),
    DiInfo(0x02060100, "A相功率因数", 2, ""),
    DiInfo(0x04000501, "运行状态字1", 2, ""),
    DiInfo(0x04000502, "运行状态字2", 2, ""),
    DiInfo(0x04000503, "运行状态字3", 2, ""),
    DiInfo(0xF9010100, "厂家组合读: 电能+继电器状态", 6, "kWh"),
    DiInfo(0xF9030100, "厂家参数块(30B)", 30, "", writable=True,
           note="byte[2] 为保电标志"),
    DiInfo(0xF9030200, "厂家保留块(10B)", 10, ""),
    DiInfo(0xF9AAAA01, "厂家数值参数(4B)", 4, "", writable=True,
           note="物理意义未确定"),
    DiInfo(0xF9AAAA02, "厂家块读(零值+电能+状态)", 14, "kWh"),
)
DI_BY_CODE = {item.di: item for item in DI_TABLE}

ENERGY_DIS = {0x00000000, 0x00000100, 0x00000200, 0x00000300, 0x00000400}


def di_name(di: int) -> str:
    item = DI_BY_CODE.get(di)
    return item.name if item else f"未知数据项 {di:08X}"


# ------------------------------------------------------------------ 数值解码
def bcd_digits(data: bytes):
    out = []
    for b in data:
        hi, lo = b >> 4, b & 0xF
        if hi > 9 or lo > 9:
            return None
        out.append(f"{hi}{lo}")
    return "".join(out)


def bcd_int_le(data: bytes):
    s = bcd_digits(data[::-1])
    return int(s) if s is not None else None


def fmt_scaled(value, decimals: int):
    if value is None:
        return None
    scale = 10 ** decimals
    return f"{value // scale}.{value % scale:0{decimals}d}"


def bcd_bytes_le(value: int, width: int) -> bytes:
    text = f"{int(value):0{width * 2}d}"
    if len(text) > width * 2:
        raise ValueError("数值超出 BCD 宽度")
    return bytes.fromhex(text)[::-1]


_KEEP_FLAG_NAMES = {
    0x00: "分组00(广播N2=00才执行,未实测勿试)",
    0xAA: "分组AA(广播N2=AA才执行)",
    0xBB: "分组BB(广播N2=BB才执行,11-22实测)",
}


def keep_flag_desc(value: int) -> str:
    return _KEEP_FLAG_NAMES.get(value, f"未知值 {value:02X}")


_RELAY_STATUS = {
    b"\x00\x00": "合闸正常",
    b"\x00\x02": "过渡态: 跳闸已接受/继电器未断开",
    b"\x08\x00": "过渡态: 合闸清除中",
    b"\x08\x02": "远程拉闸(单播 1A 所致)",
    b"\x08\x06": "定时拉闸(主站广播 1A 所致)",
}


def relay_status_desc(status: bytes) -> str:
    if len(status) != 2:
        return f"状态字={status.hex(' ').upper()}"
    text = _RELAY_STATUS.get(bytes(status))
    if text:
        return f"{text} ({status.hex(' ').upper()})"
    if status[0] == 0x08:
        return f"拉闸(原因 {status[1]:02X}H) ({status.hex(' ').upper()})"
    return f"状态字={status.hex(' ').upper()}"


def decode_value(di: int, payload: bytes) -> str:
    """payload 为减 33H 后、去掉 DI 回显的数据。"""
    n = len(payload)

    if di == 0x0000FF00:
        if n == 0 or n % 4:
            return f"数据={payload.hex(' ').upper()}"
        values = [fmt_scaled(bcd_int_le(payload[i:i + 4]), 2)
                  for i in range(0, n, 4)]
        return "块[" + ", ".join(v if v is not None else "??" for v in values) + "] kWh"

    if di in ENERGY_DIS and n == 4:
        value = fmt_scaled(bcd_int_le(payload), 2)
        return f"{value} kWh" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di == 0xF9010100 and n >= 6:
        energy = fmt_scaled(bcd_int_le(payload[:4]), 2)
        return (f"电能={energy if energy is not None else '??'} kWh | "
                f"{relay_status_desc(payload[4:6])}")

    if di == 0xF9030100 and n >= 30:
        return (f"byte[2]={payload[2]:02X} ({keep_flag_desc(payload[2])}) | "
                f"块={payload.hex(' ').upper()}")

    if di == 0xF9030200:
        return f"块={payload.hex(' ').upper()}"

    if di == 0xF9AAAA01 and n == 4:
        value = fmt_scaled(bcd_int_le(payload), 2)
        return f"值={value if value is not None else payload.hex(' ').upper()} (语义未定)"

    if di == 0xF9AAAA02 and n == 14:
        energy = fmt_scaled(bcd_int_le(payload[8:12]), 2)
        return (f"零值={payload[:8].hex(' ').upper()} | "
                f"电能={energy if energy is not None else '??'} kWh | "
                f"{relay_status_desc(payload[12:14])}")

    if di == 0x02010100 and n == 2:
        value = fmt_scaled(bcd_int_le(payload), 1)
        return f"{value} V" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di == 0x02020100 and n == 3:
        value = fmt_scaled(bcd_int_le(payload), 4)
        return (f"{value} kW (功率量，非电流)" if value is not None
                else f"数据={payload.hex(' ').upper()}")

    if di in (0x02030000, 0x02030100) and n == 3:
        value = fmt_scaled(bcd_int_le(payload), 4)
        return f"{value} kW" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di in (0x02040000, 0x02040100) and n == 3:
        value = fmt_scaled(bcd_int_le(payload), 4)
        return f"{value} kvar" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di in (0x02050000, 0x02050100) and n == 3:
        value = fmt_scaled(bcd_int_le(payload), 4)
        return f"{value} kVA" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di in (0x02060000, 0x02060100) and n == 2:
        value = fmt_scaled(bcd_int_le(payload), 3)
        return f"{value}" if value is not None else f"数据={payload.hex(' ').upper()}"

    if di in (0x04000501, 0x04000502, 0x04000503) and n == 2:
        return f"状态字={payload.hex(' ').upper()}"

    return f"数据={payload.hex(' ').upper()}"


# ------------------------------------------------------------------ 描述
_CTRL_NAMES = {
    0x08: "广播校时", 0x11: "读数据", 0x12: "读后续数据",
    0x13: "读通信地址", 0x14: "写数据", 0x15: "写通信地址",
    0x16: "冻结", 0x17: "更改通信速率", 0x18: "修改密码",
    0x19: "最大需量清零", 0x1A: "电表清零", 0x1B: "事件清零",
    0x1C: "跳合闸/报警/保电", 0x1D: "多功能端子输出",
}

_ERR_NAMES = {
    0x01: "其他错误", 0x02: "无请求数据", 0x03: "密码错/未授权",
    0x04: "密码错/未授权", 0x05: "通信速率不能修改",
}


def ctrl_desc(ctrl: int) -> str:
    direction = "表→主站" if ctrl & 0x80 else "主站→表"
    kind = "异常应答" if ctrl & 0x40 else ("正常应答" if ctrl & 0x80 else "请求")
    name = _CTRL_NAMES.get(ctrl & 0x1F, f"命令{ctrl & 0x1F:02X}H")
    return f"{name}·{kind}({direction}, C={ctrl:02X}H)"


def err_code(frame: Frame):
    if len(frame.data) != 1:
        return None
    return (frame.data[0] - OFFSET) & 0xFF


def err_desc(frame: Frame) -> str:
    err = err_code(frame)
    if err is None:
        return f"异常应答 L={len(frame.data)} 数据={frame.data.hex(' ').upper()}"
    return f"ERR={err:02X}H ({_ERR_NAMES.get(err, '未知错误')})"


def describe_frame(frame: Frame) -> str:
    head = (f"addr={frame.addr_display} {ctrl_desc(frame.ctrl)} "
            f"L={len(frame.data)} CS-{'OK' if frame.cs_ok else 'BAD'}")
    if not frame.data:
        return head
    if frame.ctrl & 0x80 and frame.command in (0x11, 0x12) and len(frame.data) >= 4:
        raw = sub33(frame.data)
        di = int.from_bytes(raw[:4][::-1], "big")
        return f"{head}\n  DI={di:08X} ({di_name(di)}) {decode_value(di, raw[4:])}"
    if frame.ctrl == 0x93 and len(frame.data) == 6:
        raw = sub33(frame.data)
        return f"{head}\n  地址={display_addr(raw)}"
    if frame.ctrl & 0x40:
        return f"{head}\n  {err_desc(frame)}"
    return f"{head}\n  数据={frame.data.hex(' ').upper()}"


def decode_address_reply(frame: Frame):
    if frame.ctrl != 0x93 or len(frame.data) != 6:
        return None
    return display_addr(sub33(frame.data))


# ------------------------------------------------------------------ 自检
def selftest():
    aw = wire_addr_of("123456789012")
    assert aw == bytes.fromhex("129078563412")

    f = build_read(aw, 0x00000000)
    assert f.hex(" ").upper() == "68 12 90 78 56 34 12 68 11 04 33 33 33 33 67 16", f.hex()
    resp = bytes.fromhex("68 12 90 78 56 34 12 68 91 08 33 33 33 33 66 38 33 33 EF 16")
    valid, suspect = find_frames(resp)
    assert len(valid) == 1 and not suspect
    p = sub33(valid[0].data)
    assert int.from_bytes(p[:4][::-1], "big") == 0 and decode_value(0, p[4:]) == "5.33 kWh"

    f = build_read(aw, 0xF9010100)
    assert f.hex(" ").upper() == "68 12 90 78 56 34 12 68 11 04 33 34 34 2C 62 16", f.hex()
    resp = bytes.fromhex(
        "68 12 90 78 56 34 12 68 91 0A 33 34 34 2C 66 38 33 33 33 33 52 16")
    valid, _ = find_frames(resp)
    p = sub33(valid[0].data)
    assert "5.33 kWh" in decode_value(0xF9010100, p[4:])
    assert "合闸正常" in decode_value(0xF9010100, p[4:])

    assert build_read_address().hex(" ").upper() == \
        "68 AA AA AA AA AA AA 68 13 00 DF 16"
    resp = bytes.fromhex(
        "68 12 90 78 56 34 12 68 93 06 45 C3 AB 89 67 45 07 16")
    valid, _ = find_frames(resp)
    assert decode_address_reply(valid[0]) == "123456789012"

    f = build_write(aw, 0xF9030100, 0, "000000", b"\x03\x63\xAA" + b"\x00" * 27)
    assert f[8] == 0x14 and f[9] == 0x2A
    raw = sub33(f[10:10 + 42])
    assert raw[:4].hex() == "000103f9" and raw[4:8].hex() == "00000000"
    assert raw[8:12].hex() == "00000000"

    f = build_write_address("123456789012")
    assert f.hex(" ").upper() == \
        "68 AA AA AA AA AA AA 68 15 06 45 C3 AB 89 67 45 CF 16"
    assert sub33(f[10:16]) == aw

    f = build_baud(aw, 2400)
    assert f.hex(" ").upper() == "68 12 90 78 56 34 12 68 17 01 3B D9 16"

    f = build_control(aw, 0, "000000", 0x1C, 0x00)
    assert f.hex(" ").upper() == (
        "68 12 90 78 56 34 12 68 1C 10 33 33 33 33 33 33 33 33 "
        "4F 33 33 33 33 33 33 33 FE 16")
    f = build_control(BCAST_WIRE, 0, "000000", 0x1A, 0xAA)
    assert f.hex(" ").upper() == (
        "68 99 99 99 99 99 99 68 1C 10 33 33 33 33 33 33 33 33 "
        "4D DD 33 33 33 33 33 33 86 16")

    assert validate_address("123456") == "000000123456"
    assert validate_address("999999999999") is None
    assert display_addr(b"\x23\x01\x00\x00\x00\x00") == "000000000123"
    assert fmt_scaled(533, 2) == "5.33"
    # 功率类按标准 XX.XXXX（0.0001 kW）解读；93 28 00（C6 5B 33 减 33H）
    # 即 0.2893 kW，已与表显实时功率核对。
    assert decode_value(0x02020100, bytes.fromhex("932800")) == \
        "0.2893 kW (功率量，非电流)"
    assert decode_value(0x02030000, bytes.fromhex("001200")) == "0.1200 kW"
    assert decode_value(0x02030100, bytes.fromhex("932800")) == "0.2893 kW"
    assert decode_value(0x02040000, bytes.fromhex("932800")) == "0.2893 kvar"
    assert decode_value(0x02050000, bytes.fromhex("932800")) == "0.2893 kVA"
    assert decode_value(0x02060000, bytes.fromhex("9308")) == "0.893"
    assert bcd_bytes_le(533, 4) == bytes.fromhex("33050000")
    assert "定时拉闸" in relay_status_desc(b"\x08\x06")
    assert "分组BB" in keep_flag_desc(0xBB)

    bad = bytearray(build_read(aw, 0))
    bad[-2] ^= 1
    valid, suspect = find_frames(bytes(bad))
    assert not valid and len(suspect) == 1

    print("protocol selftest OK")


if __name__ == "__main__":
    selftest()
