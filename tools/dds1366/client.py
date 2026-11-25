# -*- coding: utf-8 -*-
"""主站高层操作：读、写、继电器、地址、速率、广播。

上层只生产 Request 并交给调度器消费；本模块不直接操作串口。
"""
from __future__ import annotations

from . import protocol as p
from .scheduler import (CoexistScheduler, DirectScheduler, Request,
                        RequestQueue, Result)

CREDENTIALS = {
    0x00: "000000",
    0x01: "000000",
    0x02: "000000",
    0x04: "111111",
}
OK_STATUS = ("ok", "late")


class Client:
    def __init__(self, scheduler, address: str, retries: int = 2):
        addr = p.validate_address(address)
        if addr is None:
            raise ValueError("目标表地址不合法")
        self.scheduler = scheduler
        self.address = addr
        self.aw = p.wire_addr_of(addr)
        self.retries = retries
        self._bind_target()

    def _bind_target(self):
        if isinstance(self.scheduler, (DirectScheduler, CoexistScheduler)):
            self.scheduler.target_wire = self.aw
        if isinstance(self.scheduler, CoexistScheduler):
            self.scheduler.observer.target_wire = self.aw

    def set_address(self, address: str):
        addr = p.validate_address(address)
        if addr is None:
            raise ValueError("目标表地址不合法")
        self.address = addr
        self.aw = p.wire_addr_of(addr)
        self._bind_target()

    def _one(self, request: Request) -> Result:
        return self.scheduler.consume(RequestQueue([request]))[0]

    # ---------------------------------------------------------- 读
    def decode_read(self, result: Result, di: int) -> Result:
        if result.status not in OK_STATUS or result.frame is None:
            return result
        raw = p.sub33(result.frame.data)
        if len(raw) < 4 or int.from_bytes(raw[:4][::-1], "big") != di:
            result.status = "err"
            result.detail = f"应答 DI 不匹配: {raw.hex(' ').upper()}"
            return result
        result.payload = raw[4:]
        result.value = p.decode_value(di, result.payload)
        result.detail = "正常应答"
        return result

    def read(self, di: int, retries: int | None = None) -> Result:
        request = Request(p.build_read(self.aw, di),
                          f"读 {di:08X}",
                          (0x91, 0xD1),
                          retries=self.retries if retries is None else retries,
                          meta={"di": di})
        return self.decode_read(self._one(request), di)

    def read_many(self, dis, retries: int | None = None):
        queue = RequestQueue()
        for di in dis:
            queue.put(Request(p.build_read(self.aw, di), f"读 {di:08X}",
                              (0x91, 0xD1),
                              retries=self.retries if retries is None else retries,
                              meta={"di": di}))
        results = self.scheduler.consume(queue)
        return [self.decode_read(result, result.request.meta["di"])
                for result in results]

    def read_address(self, retries: int | None = None) -> Result:
        request = Request(p.build_read_address(), "读通信地址",
                          (0x93, 0xD3),
                          retries=self.retries if retries is None else retries,
                          meta={"any_addr": True})
        result = self._one(request)
        if result.status in OK_STATUS and result.frame is not None:
            addr = p.decode_address_reply(result.frame)
            if addr is None:
                result.status = "err"
                result.detail = "应答地址长度异常"
            else:
                result.value = addr
                result.detail = "正常应答"
        return result

    def read_parameter(self) -> Result:
        result = self.read(0xF9030100)
        if result.status in OK_STATUS and result.payload is not None \
                and len(result.payload) != 30:
            result.status = "err"
            result.detail = f"参数块长度异常: {len(result.payload)} (应为 30)"
        return result

    # ---------------------------------------------------------- 写
    def write(self, di: int, content: bytes, pa: int = 0,
              pwd: str = "000000", operator: str = "00000000",
              label: str = "") -> Result:
        frame = p.build_write(self.aw, di, pa, pwd, content, operator)
        request = Request(frame, label or f"写 {di:08X}", (0x94, 0xD4),
                          retries=self.retries, meta={"di": di})
        result = self._one(request)
        if result.status in OK_STATUS and result.frame is not None:
            result.payload = p.sub33(result.frame.data)
            result.detail = "写入接受 (94H)"
        return result

    def set_group_flag(self, flag: int, pa: int = 0x00,
                       pwd: str = "000000"):
        """读改写 F9030100 byte[2] 广播分组标志并回读校验。"""
        if not 0x00 <= flag <= 0xFF:
            raise ValueError("广播分组标志应为两位 HEX（00~FF）")
        read1 = self.read_parameter()
        if read1.status not in OK_STATUS or read1.payload is None:
            return False, f"读取 F9030100 失败: {read1.detail}"
        current = read1.payload[2]
        if current == flag:
            return True, f"当前已是 {flag:02X}H，无需写入"
        content = read1.payload[:2] + bytes([flag]) + read1.payload[3:]
        write_result = self.write(0xF9030100, content, pa, pwd,
                                  label=f"写广播分组标志 byte[2]={flag:02X}")
        if write_result.status not in OK_STATUS:
            return False, f"写入未接受: {write_result.detail}"
        read2 = self.read_parameter()
        if read2.status in OK_STATUS and read2.payload == content:
            return True, f"写入并回读一致，byte[2]={flag:02X}"
        return False, "回读与写入内容不一致，需人工复核"

    def write_value_f9aaaa01(self, cents: int, pa: int = 0,
                             pwd: str = "000000") -> Result:
        content = p.bcd_bytes_le(cents, 4)
        return self.write(0xF9AAAA01, content, pa, pwd,
                          label=f"写 F9AAAA01 值={cents / 100:.2f}")

    # ---------------------------------------------------------- 控制
    def control(self, n1: int, n2: int = 0x00, silent: bool = False,
                pa: int = 0x00, pwd: str = "000000",
                label: str = "") -> Result:
        frame = p.build_control(self.aw, pa, pwd, n1, n2)
        request = Request(frame, label or f"1C N1={n1:02X} N2={n2:02X}",
                          (0x9C, 0xDC),
                          expect_response=not silent,
                          retries=self.retries)
        result = self._one(request)
        if result.status in OK_STATUS:
            result.detail = "已发送（静默）" if silent else "已接受 (9CH)"
        return result

    def close_relay(self, **kwargs) -> Result:
        return self.control(0x1C, 0x00, label="1C 直接合闸", **kwargs)

    def trip_relay(self, **kwargs) -> Result:
        return self.control(0x1A, 0x00, label="1A 跳闸", **kwargs)

    # ---------------------------------------------------------- 地址/速率
    def set_baud(self, rate: int, retries: int | None = None) -> Result:
        frame = p.build_baud(self.aw, rate)
        request = Request(frame, f"更改速率 {rate}bps", (0x97, 0xD7),
                          retries=self.retries if retries is None else retries)
        result = self._one(request)
        if result.status in OK_STATUS:
            result.detail = f"已接受，{rate}bps 将在应答后数秒生效"
        return result

    def change_address(self, new_address: str,
                       retries: int | None = None) -> Result:
        new = p.validate_address(new_address)
        if new is None:
            raise ValueError("新地址不合法")
        frame = p.build_write_address(new)
        request = Request(frame, f"写通信地址 → {new}", (0x95, 0xD5),
                          retries=self.retries if retries is None else retries,
                          meta={"any_addr": True})
        result = self._one(request)
        if result.status in OK_STATUS:
            self.set_address(new)
            result.value = new
            result.detail = "改址接受 (95H)，应答地址域为新地址"
        return result

    # ---------------------------------------------------------- 广播
    def broadcast_control(self, n1: int, n2: int = 0xAA, times: int = 2,
                          gap: float = 0.42):
        frame = p.build_control(p.BCAST_WIRE, 0, "000000", n1, n2)
        queue = RequestQueue()
        for index in range(times):
            queue.put(Request(frame, f"广播 1C N1={n1:02X} 第{index + 1}遍",
                              expect_response=False, gap_after=gap))
        return self.scheduler.consume(queue)


# ------------------------------------------------------------------ 自检
class _ScriptLink:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []
        self.baud = 2400

    def reset_input(self):
        pass

    def read(self, timeout=None):
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def send(self, frame):
        self.sent.append(bytes(frame))

    def set_baud(self, baud):
        self.baud = baud

    def close(self):
        pass


def _read_response(addr_wire, di, payload):
    return p.build_frame(addr_wire, 0x91,
                         p.add33(p.di_le_bytes(di)) + p.add33(payload))


def selftest():
    aw = p.wire_addr_of("123456789012")
    link = _ScriptLink([_read_response(aw, 0x00000000,
                                       bytes.fromhex("56341200"))])
    client = Client(DirectScheduler(link, aw, response_timeout=0.1), "123456789012")
    result = client.read(0x00000000)
    assert result.status == "ok" and result.value == "1234.56 kWh", result
    assert result.payload == bytes.fromhex("56341200")

    late_raw = _read_response(aw, 0x00000000, bytes.fromhex("56341200"))
    late_frame = p.find_frames(late_raw)[0][0]
    late = Result(Request(p.build_read(aw, 0x00000000), "late"), "late",
                 late_frame)
    client.decode_read(late, 0x00000000)
    assert late.status == "late" and late.value == "1234.56 kWh", late

    other_aw = p.wire_addr_of("000000000123")
    link = _ScriptLink([
        p.build_frame(other_aw, 0x93, p.add33(other_aw))
    ])
    client = Client(DirectScheduler(link, aw, response_timeout=0.1), "123456789012")
    result = client.read_address()
    assert result.status == "ok" and result.value == "000000000123", result

    block = bytes([0x03, 0x63, 0xAA]) + b"\x00" * 27
    changed = bytes([0x03, 0x63, 0x00]) + b"\x00" * 27
    link = _ScriptLink([
        _read_response(aw, 0xF9030100, block),
        p.build_frame(aw, 0x94, p.add33(b"\x00")),
        _read_response(aw, 0xF9030100, changed),
    ])
    client = Client(DirectScheduler(link, aw, response_timeout=0.1), "123456789012")
    ok, detail = client.set_group_flag(0x00)
    assert ok, detail
    assert len(link.sent) == 3

    link = _ScriptLink([])
    client = Client(DirectScheduler(link, aw), "123456789012")
    results = client.broadcast_control(0x1A)
    assert len(results) == 2 and all(r.status == "ok" for r in results)
    assert all(frame[1:7] == p.BCAST_WIRE for frame in link.sent)

    print("client selftest OK")


if __name__ == "__main__":
    selftest()
