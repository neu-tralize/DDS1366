# -*- coding: utf-8 -*-
"""通讯请求队列与两种消费方式：直连调度、共存窗口调度。"""
from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field

from . import protocol as p
from .link import BusObserver, Link


@dataclass
class Request:
    frame: bytes
    label: str
    await_ctrl: tuple[int, ...] = (0x91, 0xD1)
    expect_response: bool = True
    retries: int = 2
    gap_after: float = 0.0
    meta: dict = field(default_factory=dict)
    retries_left: int = field(default=0, init=False)
    aborts: int = field(default=0, init=False)

    def __post_init__(self):
        self.retries_left = self.retries


@dataclass
class Result:
    request: Request
    status: str                 # ok/late/err/timeout/aborted/failed/no_master
    frame: p.Frame | None = None
    detail: str = ""
    value: str | None = None
    payload: bytes | None = None


class RequestQueue:
    def __init__(self, items=()):
        self.items = deque(items)

    def put(self, request: Request):
        self.items.append(request)

    def putleft(self, request: Request):
        self.items.appendleft(request)

    def extend(self, requests):
        self.items.extend(requests)

    def peek(self) -> Request:
        return self.items[0]

    def pop(self) -> Request:
        return self.items.popleft()

    def snapshot(self):
        return list(self.items)

    def clear(self):
        self.items.clear()

    def __len__(self):
        return len(self.items)

    def __bool__(self):
        return bool(self.items)


def _drain(queue: RequestQueue, status: str, detail: str) -> list[Result]:
    """把队列中剩余请求按给定状态出队并生成结果。"""
    results = []
    while queue:
        results.append(Result(queue.pop(), status, detail=detail))
    return results


class DirectScheduler:
    """点对点/隔离台架：收到请求即顺序发送，逐条等待应答。"""

    def __init__(self, link: Link, target_wire: bytes,
                 response_timeout: float = 1.0, retry_delay: float = 0.1,
                 drain: float = 0.2, log=None):
        self.link = link
        self.target_wire = target_wire
        self.response_timeout = response_timeout
        self.retry_delay = retry_delay
        self.drain = drain
        self.log = log or (lambda msg: None)
        self.stop_check = None     # 实时监测等场景：返回 True 时放弃等待

    def _stopped(self) -> bool:
        return self.stop_check is not None and bool(self.stop_check())

    def run(self, queue: RequestQueue):
        results = []
        while queue:
            if self._stopped():
                results.extend(_drain(queue, "aborted", "用户请求退出"))
                break
            request = queue.pop()
            while True:
                result = self._attempt(request)
                if result.status in ("timeout", "aborted") and \
                        request.retries_left > 0 and not self._stopped():
                    request.retries_left -= 1
                    self.log(f"[{request.label}] {result.detail} → "
                             f"重试(剩余 {request.retries_left})")
                    time.sleep(self.retry_delay)
                    continue
                results.append(result)
                break
        return results

    def consume(self, queue: RequestQueue, rounds=None):
        return self.run(queue)

    def _attempt(self, request: Request) -> Result:
        self.link.reset_input()
        observer = BusObserver(self.link)
        observer.target_wire = self.target_wire
        observer.note_own(request.frame)
        try:
            self.link.send(request.frame)
        except Exception as exc:
            return Result(request, "aborted", detail=f"串口发送失败: {exc}")

        if not request.expect_response:
            observer.pump(self.drain)
            if request.gap_after:
                time.sleep(request.gap_after)
            return Result(request, "ok", detail="已发送（静默指令）")

        deadline = time.monotonic() + self.response_timeout
        while True:
            if self._stopped():
                return Result(request, "aborted", detail="用户请求退出")
            now = time.monotonic()
            if now >= deadline:
                return Result(request, "timeout", detail="等待应答超时")
            chunk = self.link.read(min(0.05, deadline - now))
            if not chunk:
                continue
            for frame in observer.feed(chunk):
                if not frame.cs_ok:
                    continue
                if not request.meta.get("any_addr") and \
                        self.target_wire is not None and \
                        frame.addr_wire != self.target_wire:
                    continue
                if frame.ctrl not in request.await_ctrl:
                    continue
                if frame.is_exception:
                    return Result(request, "err", frame, p.err_desc(frame))
                return Result(request, "ok", frame, "正常应答")


@dataclass
class CoexistConfig:
    per_window: int = 3        # 每个天窗内最多注入次数
    min_samples: int = 3
    guard: float = 0.5
    re_guard: float = 0.10
    open_min: float = 0.6
    safety: float = 0.5
    resp_min: float = 0.25
    resp_max: float = 0.4
    margin: float = 0.15
    resp_wait: float = 1.0
    txn_wait: float = 30.0
    max_aborts: int = 5
    full_access: bool = False


@dataclass
class Window:
    open: float
    dead: float
    span: float
    n: int
    w: float
    budget: float


class CoexistScheduler:
    """运行总线上窗口注入；默认只读，full_access 按白名单开放写/控/广播。

    天窗指主站一次通讯结束到下一次通讯之间的空窗：观测到新的主站
    请求帧才允许开新窗，每个天窗内最多注入 per_window 帧。
    """

    def __init__(self, link: Link, target_wire: bytes,
                 config: CoexistConfig | None = None, log=None):
        self.link = link
        self.target_wire = target_wire
        self.cfg = config or CoexistConfig()
        self.cfg.per_window = max(1, min(3, self.cfg.per_window))
        self.observer = BusObserver(link)
        self.observer.target_wire = target_wire
        self.log = log or (lambda msg: None)
        self.stop_check = None     # 实时监测等场景：返回 True 时放弃等待
        self.stats = Counter()
        self.done = 0
        self._window_anchor = None   # 上次开窗对应的主站帧时刻；变化才再开窗

    def _stopped(self) -> bool:
        return self.stop_check is not None and bool(self.stop_check())

    def run(self, queue: RequestQueue, rounds: int | None = None):
        results = []
        self._check_read_only(queue)
        if rounds is not None:
            self.done = 0
        wait_started = time.monotonic()
        while queue and (rounds is None or self.done < rounds):
            if self._stopped():
                results.extend(_drain(queue, "aborted", "用户请求退出"))
                break
            window = self._wait_window(queue)
            if window is None:
                if self._stopped():
                    results.extend(_drain(queue, "aborted", "用户请求退出"))
                else:
                    results.extend(_drain(queue, "no_master",
                                          "未观测到完整主站事务，按约定不发送"))
                break
            self._run_window(window, queue, results)
            if time.monotonic() - wait_started > self.cfg.txn_wait * 4:
                results.extend(_drain(queue, "no_master", "等待窗口超时"))
                break
        return results, self.stats

    def consume(self, queue: RequestQueue, rounds=None):
        results, _ = self.run(queue, rounds)
        return results

    def consume_window(self, queue: RequestQueue, on_result=None):
        """在当前/下一个天窗内尽量多发；发不完的请求留在 queue。

        每窗次数由空窗跨度和 per_window 共同决定；on_result 逐条回调，
        便于调用方在窗口结束前刷新已完成的项。
        """
        self._check_read_only(queue)
        results = []
        if not queue.items:
            return results
        window = self._wait_window(queue)
        if window is None:
            return results
        self._run_window(window, queue, results, on_result)
        return results

    def _check_read_only(self, queue: RequestQueue):
        for request in queue.snapshot():
            frame = request.frame
            if request.meta.get("raw"):
                if not self.cfg.full_access:
                    raise ValueError("共存调度默认只读；原始帧未授权")
                continue
            addr = frame[1:7]
            cmd = frame[8] & 0x1F
            if addr == self.target_wire and cmd == 0x11:
                continue
            if self.cfg.full_access:
                if addr == self.target_wire and cmd in (0x14, 0x17, 0x1C):
                    continue
                if addr == p.BCAST_WIRE and cmd in (0x14, 0x1C, 0x15):
                    continue
            raise ValueError("共存调度不允许该帧（地址/命令未授权）")

    def _wait_window(self, queue: RequestQueue | None = None):
        deadline = time.monotonic() + self.cfg.txn_wait
        last_hint = None
        while time.monotonic() < deadline:
            if self._stopped():
                return None
            self.observer.pump(0.03)
            window = self._try_open(queue)
            if window is not None:
                return window
            hint = self._wait_hint()
            if hint != last_hint:    # 状态变化才输出，避免刷屏
                self.log(hint)
                last_hint = hint
        return None

    def _wait_hint(self) -> str:
        """等窗期间的进度提示，按 _try_open 的判据顺序给出阻塞原因。

        首次通讯要采样到足够的主站帧间隔样本才能开窗，此间可能
        长时间无回显；提示状态让用户知道程序在工作而非卡死。
        """
        obs = self.observer
        cfg = self.cfg
        if obs.last_master is None:
            return "尚未观测到主站请求帧，等待中 ..."
        if obs.last_master == self._window_anchor:
            return "本周期已注入过，等待下一次主站通讯 ..."
        end = obs.txn_end(cfg.resp_wait)
        if end is None or time.monotonic() - end < cfg.open_min \
                or time.monotonic() - obs.last_activity < cfg.guard:
            return "等待主站事务结束后的空窗 ..."
        if not obs.ready(cfg.min_samples):
            est = obs.est_interval()
            n = len(obs.gaps)
            if est is None:
                return f"采样主站通信间隔 {n}/{cfg.min_samples} ..."
            return (f"采样主站通信间隔 {n}/{cfg.min_samples}"
                    f"（已测约 {est:.1f}s/次）...")
        return "本周期剩余空间不足，等待下一次主站通讯 ..."

    def _try_open(self, queue: RequestQueue | None = None):
        obs = self.observer
        now = time.monotonic()
        if obs.last_master is None:
            return None
        if obs.last_master == self._window_anchor:
            return None    # 本天窗已开过；等下一次主站通讯
        end = obs.txn_end(self.cfg.resp_wait)
        if end is None or now - end < self.cfg.open_min:
            return None
        if now - obs.last_activity < self.cfg.guard:
            return None
        if not obs.ready(self.cfg.min_samples):
            return None
        items = queue.snapshot() if queue is not None else []
        if items:
            tx0 = max(self._tx_time(request.frame) for request in items)
            needs_response = any(request.expect_response for request in items)
        else:
            tx0 = self._tx_time(p.build_read(self.target_wire, 0))
            needs_response = True
        interval = obs.est_interval()
        safety = self.cfg.safety
        dead = obs.last_master + interval - safety
        span = dead - now
        min_slot = tx0 + (self.cfg.resp_min if needs_response else 0) \
            + self.cfg.margin
        if span < min_slot:
            self.stats["slot_skipped"] += 1
            return None
        n_by_span = max(1, int(span // min_slot))
        n = max(1, min(self.cfg.per_window, 3, n_by_span))
        w = span / n
        budget = max(self.cfg.resp_min,
                     min(self.cfg.resp_max, w - tx0 - self.cfg.margin))
        self._window_anchor = obs.last_master
        return Window(now, dead, span, n, w, budget)

    def _run_window(self, window: Window, queue: RequestQueue, results,
                    on_result=None):
        obs = self.observer
        self.stats["windows"] += 1
        self.log(f"── 天窗打开: 跨度 {window.span:.2f}s，"
                 f"计划 {window.n} 次，槽宽 {window.w:.2f}s，"
                 f"应答预算 {window.budget:.2f}s")
        obs.begin_watch(window.dead)
        planned = window.n
        sent = 0
        try:
            while queue and sent < window.n:
                request = queue.peek()
                tx = self._tx_time(request.frame)
                need = tx + self.cfg.resp_min + self.cfg.margin
                now = time.monotonic()
                if now >= window.dead:
                    break

                if obs.last_activity <= obs.own_end + 0.05:
                    earliest = obs.own_end + self.cfg.re_guard
                else:
                    earliest = obs.last_activity + self.cfg.guard
                earliest = max(earliest, now)

                planned = self._rollback_plan(planned, sent, window, earliest)
                slot_start = window.open + sent * (window.span / planned)
                slot_start = max(slot_start, earliest)
                slot_end = window.open + (sent + 1) * (window.span / planned)
                if slot_start + need > window.dead:
                    self.stats["slot_skipped"] += 1
                    self.log(f"[{request.label}] 本窗剩余空间不足 → 移入后续窗")
                    break

                if obs.wait_until(slot_start):
                    self.stats["aborted"] += 1
                    self.log("等待槽起点期间出现主站帧/他表应答 → 本窗结束")
                    break

                if obs.external:
                    self.stats["aborted"] += 1
                    break

                request = queue.pop()
                obs.note_own(request.frame)
                self.link.reset_input()
                obs.suppress_txn = True
                try:
                    try:
                        self.link.send(request.frame)
                    except Exception as exc:
                        results.append(Result(request, "aborted",
                                              detail=f"串口发送失败: {exc}"))
                        break
                    sent += 1
                    self.stats["sent"] += 1
                    self.log(f"[{request.label}] 第 {sent}/{window.n} 次已发送 "
                             f"(计划 {planned} 次/窗)")

                    budget_dead = min(slot_end, window.dead)
                    status, frame, detail = self._await(request, budget_dead,
                                                        window.dead)
                finally:
                    obs.suppress_txn = False
                obs.own_end = time.monotonic()

                if status in ("ok", "late", "err"):
                    result = Result(request, status, frame, detail)
                    results.append(result)
                    if on_result is not None:
                        on_result(result)
                    self.done += 1
                    if status == "late":
                        self.log("迟到应答算成功，剩余请求移入后续窗")
                        break
                    if obs.external:
                        self.log("本窗出现外部活动，剩余请求移入后续窗")
                        break
                    continue

                self.stats[status] += 1
                if status == "timeout":
                    request.retries_left -= 1
                else:
                    request.aborts += 1
                keep = (request.retries_left >= 0
                        and request.aborts < self.cfg.max_aborts)
                if keep:
                    queue.putleft(request)
                    self.log(f"[{request.label}] {detail} → 重试，"
                             f"本窗剩余请求移入后续窗")
                else:
                    result = Result(request, "failed", detail=detail)
                    results.append(result)
                    if on_result is not None:
                        on_result(result)
                break
        finally:
            obs.end_watch()

    def _await(self, request: Request, budget_dead: float, hard_dead: float):
        obs = self.observer
        any_addr = bool(request.meta.get("any_addr"))
        if not request.expect_response:
            obs.pump(max(0.05, request.gap_after))
            if obs.external:
                return "ok", None, "已发送（静默）；间隙出现外部活动"
            return "ok", None, "已发送（静默）"

        budget_lapsed = False
        while True:
            now = time.monotonic()
            if now >= hard_dead:
                return "timeout", None, "直至本窗结束未收到应答"
            if obs.external and not any_addr:
                return "aborted", None, "等待期出现外部活动"
            chunk = self.link.read(min(0.03, hard_dead - now))
            if chunk:
                for frame in obs.feed(chunk):
                    if bytes(frame.raw) in obs.sent:
                        continue
                    if not frame.cs_ok:
                        self.stats["collision_risk"] += 1
                        if any_addr:
                            continue
                        return "aborted", None, "出现校验存疑帧，疑似冲突"
                    if frame.is_response:
                        matched = frame.addr_wire == self.target_wire \
                            or any_addr
                        if matched and frame.ctrl in request.await_ctrl:
                            if frame.is_exception:
                                return "err", frame, p.err_desc(frame)
                            return ("late" if budget_lapsed else "ok",
                                    frame, "正常应答")
                        if not any_addr and \
                                frame.addr_wire != self.target_wire:
                            self.stats["collision_risk"] += 1
                            return "aborted", None, "出现他表应答"
                    else:
                        self.stats["collision_risk"] += 1
                        return "aborted", None, "出现主站请求帧"
            if not budget_lapsed and time.monotonic() >= budget_dead:
                budget_lapsed = True
                self.log(f"[{request.label}] 超出应答预算，继续监听到本窗结束")

    @staticmethod
    def _rollback_plan(planned: int, sent: int, window: Window,
                       earliest: float) -> int:
        while planned > 1 and \
                window.open + sent * (window.span / planned) < earliest:
            planned -= 1
        return planned

    def _tx_time(self, frame: bytes, baud: int | None = None) -> float:
        if baud is None:
            baud = getattr(self.link, "baud", 2400)
        return len(frame) * 11.0 / max(baud, 1) + 0.02


# ------------------------------------------------------------------ 自检
class _FakeLink:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.sent = []
        self.baud = 2400

    def reset_input(self):
        pass

    def read(self, timeout=None):
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(min(timeout or 0.01, 0.01))
        return b""

    def send(self, frame):
        self.sent.append(bytes(frame))

    def set_baud(self, baud):
        self.baud = baud

    def close(self):
        pass


def selftest():
    target = p.wire_addr_of("123456789012")
    read = p.build_read(target, 0)
    response = p.build_frame(target, 0x91,
                             p.add33(b"\x00\x00\x00\x00") +
                             p.add33(bytes.fromhex("56341200")))

    link = _FakeLink([response])
    sched = DirectScheduler(link, target, response_timeout=0.2)
    results = sched.run(RequestQueue([Request(read, "read")]))
    assert len(results) == 1 and results[0].status == "ok"
    assert p.decode_value(0, p.sub33(results[0].frame.data)[4:]) == "1234.56 kWh"

    link = _FakeLink([])
    sched = DirectScheduler(link, target, response_timeout=0.2)
    sched.stop_check = lambda: True
    results = sched.run(RequestQueue([Request(read, "stopped", retries=1)]))
    assert results[0].status == "aborted" and link.sent == []

    calls = {"n": 0}

    def stop_after_send():
        calls["n"] += 1
        return calls["n"] >= 2   # 进入应答等待循环后才触发

    link = _FakeLink([response])
    sched = DirectScheduler(link, target, response_timeout=0.2)
    sched.stop_check = stop_after_send
    results = sched.run(RequestQueue([Request(read, "mid-stop", retries=1)]))
    assert results[0].status == "aborted" and len(link.sent) == 1

    link = _FakeLink([])
    sched = DirectScheduler(link, target, response_timeout=0.03)
    results = sched.run(RequestQueue([Request(read, "timeout", retries=1)]))
    assert results[0].status == "timeout" and len(link.sent) == 2

    link = _FakeLink([])
    sched = DirectScheduler(link, target)
    results = sched.run(RequestQueue([Request(read, "silent",
                                              expect_response=False)]))
    assert results[0].status == "ok" and len(link.sent) == 1

    window = Window(open=0.0, dead=1.8, span=1.8, n=3, w=0.6, budget=0.3)
    assert CoexistScheduler._rollback_plan(3, 0, window, 0.0) == 3
    assert CoexistScheduler._rollback_plan(3, 1, window, 0.7) == 2
    assert CoexistScheduler._rollback_plan(3, 1, window, 1.0) == 1
    assert CoexistScheduler._rollback_plan(2, 0, window, 0.0) == 2

    # 单窗消费：按计划次数发送，on_result 逐条回调，未发完的留队列
    link = _FakeLink([response])
    sched = CoexistScheduler(link, target)
    sched.observer.last_activity = 0.0
    now = time.monotonic()
    window = Window(open=now, dead=now + 0.9, span=0.9, n=1, w=0.9, budget=0.3)
    sched._wait_window = lambda queue=None: window
    queue = RequestQueue([Request(read, "a"), Request(read, "b")])
    seen = []
    results = sched.consume_window(queue, on_result=seen.append)
    assert len(link.sent) == 1 and len(queue.items) == 1
    assert seen == results and seen[0].status == "ok"

    # 天窗锚点：同一主站周期内不重复开窗；观测到新的主站帧才开新窗
    link = _FakeLink([])
    sched = CoexistScheduler(link, target)
    obs = sched.observer

    def prime_cycle(back: float):
        now = time.monotonic()
        obs.last_master = now - back
        obs.master_req_ts = obs.last_master
        obs.txn_end_ts = obs.last_master + 0.5
        obs.last_activity = obs.last_master + 0.5
        obs.gaps.extend([3.0, 3.0, 3.0])

    # Windows 的 time.monotonic 粒度较粗，两次用不同的 back 保证帧时刻前进
    prime_cycle(2.0)
    opened = sched._try_open()
    assert opened is not None and sched._window_anchor == obs.last_master
    assert sched._try_open() is None          # 本周期已开过窗 → 拒绝
    prime_cycle(1.5)                          # 新主站帧（新周期）→ 允许再开
    assert sched._try_open() is not None
    assert sched._window_anchor == obs.last_master

    # 等窗提示：按阻塞原因分阶段，采样进度有回显
    link = _FakeLink([])
    sched = CoexistScheduler(link, target)
    assert "等待" in sched._wait_hint()
    obs = sched.observer
    prime = lambda back: (
        setattr(obs, "last_master", time.monotonic() - back),
        setattr(obs, "master_req_ts", obs.last_master),
        setattr(obs, "txn_end_ts", obs.last_master + 0.5),
        setattr(obs, "last_activity", obs.last_master + 0.5))
    prime(2.0)
    hint = sched._wait_hint()
    assert "采样" in hint and "0/3" in hint, hint
    obs.gaps.extend([3.0])
    assert "1/3" in sched._wait_hint()
    obs.gaps.extend([3.0, 3.0])
    hint = sched._wait_hint()
    assert hint is not None and "采样" not in hint, hint
    prime(2.0)
    sched._try_open()                        # 开窗 → 记录 anchor
    obs.last_master = sched._window_anchor   # 同周期再查 → 已注入提示
    assert "已注入" in sched._wait_hint()

    link = _FakeLink([])
    sched = CoexistScheduler(link, target)
    write_frame = p.build_write(target, 0xF9AAAA01, 0, "000000", b"\x00" * 4)
    try:
        sched._check_read_only(RequestQueue([Request(write_frame, "write")]))
        raise AssertionError("共存调度未拒绝写请求")
    except ValueError:
        pass

    sched = CoexistScheduler(link, target,
                             config=CoexistConfig(full_access=True))
    for frame in (p.build_read_address(),
                  p.build_write_address("000000000123")):
        try:
            sched._check_read_only(RequestQueue([Request(frame, "addr")]))
            raise AssertionError("共存调度未拒绝 AA×6 地址帧")
        except ValueError:
            pass

    link = _FakeLink([])
    sched = CoexistScheduler(link, target)
    sched.stop_check = lambda: True
    results = sched.consume(RequestQueue([Request(read, "stopped")]))
    assert results[0].status == "aborted"

    print("scheduler selftest OK")


if __name__ == "__main__":
    selftest()
