# -*- coding: utf-8 -*-
"""串口、RS-485 方向控制、帧流解析与总线观测。"""
from __future__ import annotations

import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # 允许离线自检
    serial = None

from . import protocol as p

GAP_LO, GAP_HI = 1.0, 6.0


@dataclass
class SerialConfig:
    port: str
    baud: int = 2400
    parity: str = "E"
    tx_key: str = "rts"
    timeout: float = 0.05


def ports():
    if serial is None:
        return []
    return list(serial.tools.list_ports.comports())


def ports_text() -> str:
    items = ports()
    if not items:
        return "未发现任何串口。"
    lines = [f"共 {len(items)} 个串口:"]
    for item in items:
        mark = "  <== CH340/CH341，大概率是转接板" if item.vid == 0x1A86 else ""
        lines.append(f"  {item.device:10s} {item.description}  [{item.hwid}]{mark}")
    return "\n".join(lines)


def suggest_port():
    items = ports()
    wch = [x.device for x in items if x.vid == 0x1A86]
    if len(wch) == 1:
        return wch[0]
    if len(items) == 1:
        return items[0].device
    return None


class LinkError(RuntimeError):
    pass


class Link:
    def __init__(self, config: SerialConfig):
        if serial is None:
            raise LinkError("未安装 pyserial: pip install pyserial")
        self.config = config
        self.ser = serial.Serial()
        self.ser.port = config.port
        self.ser.baudrate = config.baud
        self.ser.bytesize = 8
        self.ser.parity = config.parity
        self.ser.stopbits = 1
        self.ser.timeout = config.timeout
        self.ser.write_timeout = 2.0
        self.ser.xonxoff = False
        self.ser.rtscts = False
        self.ser.open()
        self.ser.dtr = False
        self.ser.rts = False

    @property
    def baud(self) -> int:
        return int(self.ser.baudrate)

    def send(self, frame: bytes):
        key = self.config.tx_key.lower()
        if key == "rts":
            self.ser.rts = True
        elif key == "dtr":
            self.ser.dtr = True
        try:
            self.ser.write(frame)
            self.ser.flush()
        finally:
            tail = len(frame) * 11.0 / max(self.baud, 1) + 0.02
            time.sleep(tail)
            if key == "rts":
                self.ser.rts = False
            elif key == "dtr":
                self.ser.dtr = False

    def read(self, timeout: float | None = None) -> bytes:
        self.ser.timeout = self.config.timeout if timeout is None else max(0.001, timeout)
        chunk = self.ser.read(4096)
        return chunk or b""

    def reset_input(self):
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def set_baud(self, baud: int):
        self.ser.baudrate = baud

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class FrameStream:
    """按块喂入字节流，返回新出现的完整帧（含校验存疑帧）。"""

    def __init__(self, ignore=()):
        self.buf = b""
        self.base = 0
        self.chunks = []
        self.seen = set()
        self.ignore = {bytes(x) for x in ignore}

    def reset(self):
        self.buf = b""
        self.base = 0
        self.chunks = []
        self.seen.clear()

    def ignore_raw(self, raw: bytes):
        self.ignore.add(bytes(raw))

    def feed(self, chunk: bytes, ts: float | None = None):
        if not chunk:
            return []
        ts = time.monotonic() if ts is None else ts
        self.chunks.append((ts, chunk))
        self.buf += chunk

        markers = []
        off = 0
        for t, part in self.chunks:
            markers.append((off, t))
            off += len(part)
        valid, suspects = p.find_frames(self.buf, markers)
        frames = sorted(valid + suspects, key=lambda f: f.start)

        fresh = []
        cut = 0
        for frame in frames:
            key = (self.base + frame.start, bytes(frame.raw))
            cut = max(cut, frame.start + len(frame.raw))
            if key in self.seen:
                continue
            self.seen.add(key)
            if bytes(frame.raw) not in self.ignore:
                fresh.append(frame)
        if cut:
            self.buf = self.buf[cut:]
            self.base += cut
            self.chunks = [(ts, self.buf)] if self.buf else []
        elif len(self.buf) > 8192:
            keep = self.buf[-2048:]
            self.base += len(self.buf) - len(keep)
            self.buf = keep
            self.chunks = [(ts, keep)]
        return fresh


class BusObserver:
    """只观察、不发送：区分主站请求与从站应答，估计槽间隔。"""

    def __init__(self, link: Link | None = None):
        self.link = link
        self.stream = FrameStream()
        self.sent = deque(maxlen=16)
        self.last_activity = time.monotonic()
        self.own_end = 0.0
        self.last_master = None
        self.master_req_ts = None
        self.txn_end_ts = None
        self.gaps = deque(maxlen=32)
        self.watch_dead = None
        self.external = False
        self.target_wire = None
        self.suppress_txn = False
        self.stats = Counter()
        self.on_frame = None

    def note_own(self, frame: bytes):
        self.sent.append(bytes(frame))
        self.stream.ignore_raw(frame)

    def feed(self, chunk: bytes, ts: float | None = None):
        ts = time.monotonic() if ts is None else ts
        self.last_activity = ts
        frames = self.stream.feed(chunk, ts)
        for frame in frames:
            self.classify(frame)
        return frames

    def classify(self, frame: p.Frame):
        raw = bytes(frame.raw)
        if raw in self.sent:
            return
        if not frame.cs_ok:
            self.stats["suspect"] += 1
            if self.watch_dead is not None:
                self.external = True
            return

        if frame.is_response:
            self.stats["responses"] += 1
            if not self.suppress_txn and self.master_req_ts is not None \
                    and self.txn_end_ts is None:
                self.txn_end_ts = frame.ts or time.monotonic()
            if self.watch_dead is not None and frame.addr_wire != self.target_wire:
                self.external = True
        else:
            ts = frame.ts or time.monotonic()
            if self.last_master is not None:
                gap = ts - self.last_master
                if GAP_LO <= gap <= GAP_HI:
                    self.gaps.append(gap)
            self.last_master = ts
            self.master_req_ts = ts
            self.txn_end_ts = None
            self.stats["master"] += 1
            if self.watch_dead is not None:
                self.external = True

        if self.on_frame:
            self.on_frame(frame)

    def txn_end(self, resp_wait: float = 1.0):
        if self.master_req_ts is None:
            return None
        if self.txn_end_ts is None and \
                time.monotonic() - self.master_req_ts >= resp_wait:
            self.txn_end_ts = self.master_req_ts + resp_wait
        return self.txn_end_ts

    def est_interval(self):
        return statistics.median(self.gaps) if self.gaps else None

    def ready(self, min_samples: int = 3) -> bool:
        return len(self.gaps) >= min_samples

    def pump(self, seconds: float):
        if self.link is None:
            end = time.monotonic() + max(0.0, seconds)
            while time.monotonic() < end:
                time.sleep(min(0.01, end - time.monotonic()))
            return []
        frames = []
        end = time.monotonic() + max(0.0, seconds)
        while True:
            remain = end - time.monotonic()
            if remain <= 0:
                return frames
            chunk = self.link.read(min(0.03, remain))
            if chunk:
                frames.extend(self.feed(chunk))

    def wait_until(self, when: float) -> bool:
        while True:
            if self.external:
                return True
            remain = when - time.monotonic()
            if remain <= 0:
                return False
            self.pump(min(0.03, remain))

    def begin_watch(self, deadline: float):
        self.watch_dead = deadline
        self.external = False

    def end_watch(self):
        self.watch_dead = None
        self.external = False


def selftest():
    f1 = p.build_read(p.wire_addr_of("123456789012"), 0)
    f2 = p.build_frame(p.wire_addr_of("123456789012"), 0x91,
                       p.add33(b"\x00\x00\x00\x00") + p.add33(bytes.fromhex("56341200")))

    stream = FrameStream()
    assert stream.feed(b"\xde\xad") == []
    assert stream.feed(f1[:5]) == []
    got = stream.feed(f1[5:])
    assert len(got) == 1 and got[0].ctrl == 0x11

    stream = FrameStream()
    assert len(stream.feed(f1 + f2)) == 2
    stream.reset()
    stream.ignore_raw(f1)
    assert stream.feed(f1) == []
    assert len(stream.feed(f2)) == 1

    obs = BusObserver()
    obs.target_wire = p.wire_addr_of("123456789012")
    master = p.build_read(p.wire_addr_of("000000000001"), 0)
    obs.feed(master, ts=100.0)
    assert obs.stats["master"] == 1 and obs.master_req_ts == 100.0
    resp = p.build_frame(p.wire_addr_of("000000000001"), 0x91,
                         p.add33(b"\x00\x00\x00\x00") + p.add33(bytes.fromhex("01000000")))
    obs.feed(resp, ts=100.1)
    assert abs(obs.txn_end(0.5) - 100.1) < 1e-9

    obs.feed(master, ts=103.0)
    assert len(obs.gaps) == 1 and abs(obs.gaps[0] - 3.0) < 1e-9
    obs.feed(master, ts=103.5)
    assert len(obs.gaps) == 1  # 连发间隔不入样本

    obs.begin_watch(200.0)
    obs.feed(master, ts=104.0)
    assert obs.external
    obs.end_watch()

    print("link selftest OK")


if __name__ == "__main__":
    selftest()
