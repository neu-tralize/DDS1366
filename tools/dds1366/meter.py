# -*- coding: utf-8 -*-
"""DDS1366 从站模拟：按实测行为应答 645-2007 请求。"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import protocol as p
from .link import FrameStream, Link

DEFAULT_PARAM = bytes.fromhex(
    "03 63 AA 00 00 50 00 00 00 08 00 00 0F 00 64 00 32 00 "
    "D0 07 10 27 30 75 83 03 60 00 00 00")

CLOSE_TRANSITION_S = 1.0   # 实测：单播合闸后 08 00 过渡态约 1s 后回 00 00


def _default_rates():
    return [0, 0, 0, 0]


@dataclass
class MeterState:
    address: str = "123456789012"
    energy_cents: int = 0
    rates: list = field(default_factory=_default_rates)
    status: bytes = b"\x00\x00"
    param_block: bytes = DEFAULT_PARAM
    run_status: list = field(default_factory=lambda: [b"\x00\x00"] * 3)
    voltage_dv: int = 2200
    power_mw: int = 0        # 有功 0.001 kW（线上编码为 0.0001 kW/字）
    reactive_mvar: int = 0   # 无功 0.001 kvar（同上）
    apparent_mva: int = 0    # 视在 0.001 kVA（同上）
    pf_milli: int = 1000
    f9aaaa01_cents: int = 0
    f9030200: bytes = b"\x00" * 10
    wake_fe: int = 3
    broadcast_clear_energy: bool = True
    pending_baud: int | None = None
    pending_baud_at: float = 0.0
    pending_close_at: float | None = None   # 08 00 合闸清除过渡的完成时刻

    @property
    def aw(self) -> bytes:
        return p.wire_addr_of(self.address)

    @property
    def keep_flag(self) -> int:
        return self.param_block[2] if len(self.param_block) > 2 else 0xAA

    def total_cents(self) -> int:
        return sum(self.rates)

    def set_total_cents(self, cents: int):
        cents = max(0, min(int(cents), 99999999))
        self.energy_cents = cents
        self.rates = [0, 0, cents, 0]

    def total_cents_to_zero(self):
        self.energy_cents = 0
        self.rates = [0, 0, 0, 0]

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "energy_cents": self.energy_cents,
            "rates": list(self.rates),
            "status_hex": self.status.hex(),
            "param_block_hex": self.param_block.hex(),
            "run_status_hex": [x.hex() for x in self.run_status],
            "voltage_dv": self.voltage_dv,
            "power_mw": self.power_mw,
            "reactive_mvar": self.reactive_mvar,
            "apparent_mva": self.apparent_mva,
            "pf_milli": self.pf_milli,
            "f9aaaa01_cents": self.f9aaaa01_cents,
            "f9030200_hex": self.f9030200.hex(),
            "wake_fe": self.wake_fe,
            "broadcast_clear_energy": self.broadcast_clear_energy,
        }

    @classmethod
    def from_dict(cls, data: dict):
        state = cls()
        state.address = p.validate_address(str(data.get("address", state.address))) \
            or state.address
        state.energy_cents = int(data.get("energy_cents", 0))
        rates = data.get("rates")
        if isinstance(rates, list) and len(rates) == 4:
            state.rates = [max(0, min(int(x), 99999999)) for x in rates]
        else:
            state.set_total_cents(state.energy_cents)
        status = bytes.fromhex(data.get("status_hex", "0000"))
        state.status = status if len(status) == 2 else b"\x00\x00"
        param = bytes.fromhex(data.get("param_block_hex", DEFAULT_PARAM.hex()))
        state.param_block = param if len(param) == 30 else DEFAULT_PARAM
        run = []
        for item in data.get("run_status_hex", ["0000", "0000", "0000"]):
            raw = bytes.fromhex(item)
            run.append(raw if len(raw) == 2 else b"\x00\x00")
        state.run_status = (run + [b"\x00\x00"] * 3)[:3]
        state.voltage_dv = int(data.get("voltage_dv", 2200))
        state.power_mw = int(data.get("power_mw", 0))
        state.reactive_mvar = int(data.get("reactive_mvar", 0))
        state.apparent_mva = int(data.get("apparent_mva", 0))
        state.pf_milli = int(data.get("pf_milli", 1000))
        state.f9aaaa01_cents = int(data.get("f9aaaa01_cents", 0))
        block = bytes.fromhex(data.get("f9030200_hex", "00" * 10))
        state.f9030200 = block if len(block) == 10 else b"\x00" * 10
        state.wake_fe = max(0, min(4, int(data.get("wake_fe", 3))))
        state.broadcast_clear_energy = bool(
            data.get("broadcast_clear_energy", True))
        return state


class MeterSimulator:
    def __init__(self, state: MeterState | None = None, log=None):
        self.state = state or MeterState()
        self.log = log or (lambda msg: None)
        self.rx_own = 0
        self.rx_other = 0
        self.replies = 0

    def _respond(self, ctrl: int, raw: bytes) -> bytes:
        self.replies += 1
        frame = p.build_frame(self.state.aw, ctrl, p.add33(raw))
        return b"\xFE" * self.state.wake_fe + frame

    def _error(self, ctrl: int, err: int) -> bytes:
        return self._respond(ctrl, bytes([err]))

    def _energy_bytes(self, cents: int) -> bytes:
        return p.bcd_bytes_le(max(0, min(cents, 99999999)), 4)

    def _value_bytes(self, value: int, width: int) -> bytes:
        limit = 10 ** (width * 2) - 1
        return p.bcd_bytes_le(max(0, min(int(value), limit)), width)

    # ---------------------------------------------------------- 读
    def _read(self, raw: bytes):
        if len(raw) != 4:
            return [self._error(0xD1, 0x02)]
        di = int.from_bytes(raw[::-1], "big")
        total = self.state.total_cents()

        if di == 0x00000000:
            return [self._respond(0x91, raw + self._energy_bytes(total))]
        if 0x00000100 <= di <= 0x00000400:
            index = (di >> 8) - 1
            if 0 <= index < 4:
                return [self._respond(0x91, raw + self._energy_bytes(
                    self.state.rates[index]))]
        if di == 0x0000FF00:
            payload = self._energy_bytes(total) + b"".join(
                self._energy_bytes(x) for x in self.state.rates)
            return [self._respond(0x91, raw + payload)]
        if di == 0x02010100:
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.voltage_dv, 2))]
        if di == 0x02020100:
            # 功率线上格式为标准 XX.XXXX（0.0001 kW/字，实测与表显一致）；
            # 状态字段单位 0.001 kW，编码时 ×10。
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.power_mw * 10, 3))]
        if di in (0x02030000, 0x02030100):
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.power_mw * 10, 3))]
        if di in (0x02040000, 0x02040100):
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.reactive_mvar * 10, 3))]
        if di in (0x02050000, 0x02050100):
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.apparent_mva * 10, 3))]
        if di in (0x02060000, 0x02060100):
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.pf_milli, 2))]
        if di in (0x04000501, 0x04000502, 0x04000503):
            index = (di & 0x0F) - 1
            status = self.state.run_status[index] \
                if 0 <= index < 3 else b"\x00\x00"
            return [self._respond(0x91, raw + status)]
        if di == 0xF9010100:
            return [self._respond(0x91, raw + self._energy_bytes(total)
                                  + self.state.status)]
        if di == 0xF9030100:
            return [self._respond(0x91, raw + self.state.param_block)]
        if di == 0xF9030200:
            return [self._respond(0x91, raw + self.state.f9030200)]
        if di == 0xF9AAAA01:
            return [self._respond(0x91, raw + self._value_bytes(
                self.state.f9aaaa01_cents, 4))]
        if di == 0xF9AAAA02:
            return [self._respond(0x91, raw + b"\x00" * 8
                                  + self._energy_bytes(total)
                                  + self.state.status)]
        return [self._error(0xD1, 0x02)]

    # ---------------------------------------------------------- 写
    def _password_ok(self, pa: int, pwd: bytes) -> bool:
        if pa in (0x00, 0x01, 0x02):
            return pwd == bytes.fromhex("000000")
        if pa == 0x04:
            return pwd == bytes.fromhex("111111")
        return False

    def _write_unicast(self, raw: bytes):
        if len(raw) < 12:
            return [self._error(0xD4, 0x02)]
        di = int.from_bytes(raw[:4][::-1], "big")
        pa = raw[4]
        pwd = raw[5:8]
        content = raw[12:]
        if di not in (0xF9030100, 0xF9AAAA01):
            return [self._error(0xD4, 0x02)]
        if not self._password_ok(pa, pwd):
            return [self._error(0xD4, 0x04)]
        if di == 0xF9030100:
            if len(content) != 30:
                return [self._error(0xD4, 0x01)]
            self.state.param_block = content
            self.log(f"参数块已写入，保电标志={content[2]:02X}")
            return [self._respond(0x94, b"")]
        if len(content) != 4:
            return [self._error(0xD4, 0x01)]
        self.state.f9aaaa01_cents = p.bcd_int_le(content) or 0
        self.log(f"F9AAAA01 已写入 {self.state.f9aaaa01_cents / 100:.2f}")
        return [self._respond(0x94, b"")]

    def _write_broadcast(self, raw: bytes):
        if len(raw) < 12:
            return []
        di = int.from_bytes(raw[:4][::-1], "big")
        if di != 0xF9AAAA01:
            return []
        content = raw[12:16]
        if len(content) == 4:
            self.state.f9aaaa01_cents = p.bcd_int_le(content) or 0
        if self.state.broadcast_clear_energy:
            self.state.total_cents_to_zero()
            self.log("⚠ 模拟广播写 F9AAAA01：已无条件清零电能")
        return []

    def _control_unicast(self, raw: bytes):
        if len(raw) < 10:
            return [self._error(0xDC, 0x02)]
        pa = raw[0]
        pwd = raw[1:4]
        n1, n2 = raw[8], raw[9]
        if not self._password_ok(pa, pwd):
            return [self._error(0xDC, 0x04)]
        if n1 == 0x1A:
            if n2 == 0xAA:
                return [self._error(0xDC, 0x01)]
            self.state.status = b"\x08\x02"
            self.log("单播 1A 跳闸 → 08 02")
            return [self._respond(0x9C, b"")]
        if n1 == 0x1C:
            # 实测：合闸接受后先进入 08 00 过渡态，约 1s 后回 00 00
            self.state.status = b"\x08\x00"
            self.state.pending_close_at = time.monotonic() + CLOSE_TRANSITION_S
            self.log(f"单播 1C 合闸 → 08 00，{CLOSE_TRANSITION_S:g}s 后回 00 00")
            if n2 == 0xAA:
                return []
            return [self._respond(0x9C, b"")]
        return [self._error(0xDC, 0x01)]

    def _control_broadcast(self, raw: bytes):
        if len(raw) < 10:
            return []
        n1, n2 = raw[8], raw[9]
        if n1 == 0x1A and n2 == 0xAA and self.state.keep_flag == 0xAA:
            self.state.status = b"\x08\x06"
            self.log("广播 1A 拉闸 → 08 06")
        elif n1 == 0x1C and n2 == 0xAA and self.state.status == b"\x08\x06":
            self.state.status = b"\x00\x00"
            self.log("广播 1C 合闸 → 00 00")
        return []

    # ---------------------------------------------------------- 入口
    def handle(self, frame: p.Frame):
        if frame.is_response:
            return []
        addr = frame.addr_wire
        raw = p.sub33(frame.data)
        cmd = frame.command
        own = addr == self.state.aw
        bcast = addr == p.BCAST_WIRE
        short = addr == p.SHORT_WIRE

        if short:
            if cmd == 0x13 and not frame.data:
                return [self._respond(0x93, self.state.aw)]
            if cmd == 0x15:
                if len(raw) != 6:
                    return [self._error(0xD5, 0x02)]
                new_addr = p.display_addr(raw)
                if p.validate_address(new_addr) is None:
                    return [self._error(0xD5, 0x02)]
                self.state.address = new_addr
                self.log(f"通信地址改为 {new_addr}")
                return [self._respond(0x95, b"")]
            return []

        if bcast:
            if cmd == 0x1C:
                return self._control_broadcast(raw)
            if cmd == 0x14:
                return self._write_broadcast(raw)
            if cmd == 0x15:
                return [self._error(0xD5, 0x02)]
            return []

        if not own:
            self.rx_other += 1
            return []

        self.rx_own += 1
        if cmd == 0x11:
            return self._read(raw)
        if cmd == 0x12:
            if not raw:
                return []    # 实测：空数据域 12H 真实表静默忽略
            return [self._error(0xD2, 0x01)]
        if cmd == 0x13:
            return [self._error(0xD3, 0x02)]
        if cmd == 0x14:
            return self._write_unicast(raw)
        if cmd == 0x15:
            return [self._error(0xD5, 0x02)]
        if cmd == 0x17:
            return self._baud(raw)
        if cmd == 0x1C:
            return self._control_unicast(raw)
        if cmd == 0x01:
            return [self._error(0xC1, 0x02)]
        return []

    def _baud(self, raw: bytes):
        if len(raw) != 1 or raw[0] not in p.FEATURE_RATE:
            return [self._error(0xD7, 0x01)]
        rate = p.FEATURE_RATE[raw[0]]
        self.state.pending_baud = rate
        self.state.pending_baud_at = time.monotonic() + 2.0
        return [self._respond(0x97, raw)]

    def check_pending_baud(self, link: Link):
        state = self.state
        if state.pending_baud is None:
            return
        if time.monotonic() >= state.pending_baud_at:
            link.set_baud(state.pending_baud)
            self.log(f"速率切换为 {state.pending_baud}bps")
            state.pending_baud = None

    def check_pending_close(self):
        """08 00 合闸清除过渡到期后回 00 00；期间状态被改写则不再覆盖。"""
        state = self.state
        if state.pending_close_at is None:
            return
        if time.monotonic() >= state.pending_close_at:
            state.pending_close_at = None
            if state.status == b"\x08\x00":
                state.status = b"\x00\x00"
                self.log("合闸清除完成 → 00 00")


def simulate(link: Link, simulator: MeterSimulator, log=print,
             stop_after: float | None = None):
    stream = FrameStream()
    started = time.monotonic()
    while True:
        if stop_after is not None and time.monotonic() - started >= stop_after:
            return
        chunk = link.read(0.05)
        simulator.check_pending_baud(link)
        simulator.check_pending_close()
        if not chunk:
            continue
        for frame in stream.feed(chunk):
            if frame.is_response:
                continue
            log("← " + p.describe_frame(frame).splitlines()[0])
            for out in simulator.handle(frame):
                link.send(out)
                stream.ignore_raw(out)
                body = out[simulator.state.wake_fe:]
                valid, _ = p.find_frames(body)
                if valid:
                    log("→ " + p.describe_frame(valid[0]).splitlines()[0])


def load(path: str) -> MeterState:
    try:
        with open(path, encoding="utf-8") as fp:
            return MeterState.from_dict(json.load(fp))
    except (OSError, ValueError, json.JSONDecodeError):
        return MeterState()


def save(path: str, state: MeterState):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(state.to_dict(), fp, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ 自检
def _unwrap(outputs):
    frames = []
    for out in outputs:
        body = out.lstrip(b"\xFE")
        valid, _ = p.find_frames(body)
        frames.extend(valid)
    return frames


def selftest():
    state = MeterState()
    state.set_total_cents(533)
    sim = MeterSimulator(state)

    req = p.build_read(p.wire_addr_of(state.address), 0x00000000)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x91
    assert p.decode_value(0, p.sub33(frames[0].data)[4:]) == "5.33 kWh"

    req = p.build_read(p.SHORT_WIRE, 0x00000000)
    valid, _ = p.find_frames(req)
    assert sim.handle(valid[0]) == []

    req = p.build_read(p.wire_addr_of(state.address), 0xF9010100)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert "合闸正常" in p.decode_value(
        0xF9010100, p.sub33(frames[0].data)[4:])

    req = p.build_read(p.wire_addr_of(state.address), 0x02010100)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert p.decode_value(0x02010100, p.sub33(frames[0].data)[4:]) == "220.0 V"

    req = p.build_read(p.wire_addr_of(state.address), 0x02020100)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert "非电流" in p.decode_value(0x02020100,
                                      p.sub33(frames[0].data)[4:])

    state.power_mw = 120    # 0.12 kW，缩放已与实表表显核对
    req = p.build_read(p.wire_addr_of(state.address), 0x02030000)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    payload = p.sub33(frames[0].data)[4:]
    assert payload == bytes.fromhex("001200"), payload.hex(" ").upper()
    assert p.decode_value(0x02030000, payload) == "0.1200 kW"
    state.power_mw = 0

    req = p.build_read(p.wire_addr_of(state.address), 0x04000504)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD1 and p.err_code(frames[0]) == 0x02

    req = p.build_read(p.wire_addr_of(state.address), 0x12)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD1

    req = p.build_read_address()
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x93
    assert p.decode_address_reply(frames[0]) == state.address

    req = p.build_read(p.wire_addr_of(state.address), 0x12)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD1 and p.err_code(frames[0]) == 0x02

    req = p.build_frame(p.wire_addr_of(state.address), 0x12)
    valid, _ = p.find_frames(req)
    assert sim.handle(valid[0]) == []    # 实测：空数据域 12H 静默忽略

    req = p.build_frame(p.wire_addr_of(state.address), 0x12,
                        p.add33(p.di_le_bytes(0xF9010100)))
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD2 and p.err_code(frames[0]) == 0x01

    block = bytes([0x03, 0x63, 0x00]) + b"\x00" * 27
    req = p.build_write(p.wire_addr_of(state.address), 0xF9030100,
                        0, "000000", block)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x94 and state.param_block == block

    req = p.build_write(p.wire_addr_of(state.address), 0x00000000,
                        0, "000000", b"\x00" * 4)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD4 and p.err_code(frames[0]) == 0x02

    req = p.build_write(p.wire_addr_of(state.address), 0xF9030100,
                        4, "000000", block)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0xD4 and p.err_code(frames[0]) == 0x04

    state.status = b"\x08\x02"
    req = p.build_control(p.wire_addr_of(state.address), 0, "000000", 0x1C)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x9C and state.status == b"\x08\x00"
    state.pending_close_at = time.monotonic() - 0.1   # 快进过渡期
    sim.check_pending_close()
    assert state.status == b"\x00\x00" and state.pending_close_at is None

    req = p.build_control(p.wire_addr_of(state.address), 0, "000000", 0x1A)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x9C and state.status == b"\x08\x02"

    state.param_block = bytes([0x03, 0x63, 0xAA]) + b"\x00" * 27
    state.status = b"\x00\x00"
    req = p.build_control(p.BCAST_WIRE, 0, "000000", 0x1A, 0xAA)
    valid, _ = p.find_frames(req)
    assert sim.handle(valid[0]) == [] and state.status == b"\x08\x06"

    state.param_block = bytes([0x03, 0x63, 0x00]) + b"\x00" * 27
    state.status = b"\x00\x00"
    assert sim.handle(valid[0]) == [] and state.status == b"\x00\x00"

    req = p.build_write_address("000000000123")
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x95 and frames[0].addr_display == "000000000123"

    req = p.build_baud(p.wire_addr_of("000000000123"), 2400)
    valid, _ = p.find_frames(req)
    frames = _unwrap(sim.handle(valid[0]))
    assert frames[0].ctrl == 0x97 and state.pending_baud == 2400

    print("meter selftest OK")


if __name__ == "__main__":
    selftest()
