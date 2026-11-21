# -*- coding: utf-8 -*-
"""实时监测：按固定顺序轮询实时参数，每取得一项立即刷新面板显示。

面板在终端备用屏幕内从顶部整块重绘，退出时还原原界面。
单主站模式按固定间隔（默认 3s）逐项轮询；共存模式不引入额外节奏，
只在观测到一个新的主站请求帧后才发起一次查询（即每窗 1 次，
轮询节奏与主站保持一致）。按 Q 或 Esc（或 Ctrl+C）退出，确认统计
摘要后还原原界面返回菜单。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

from . import protocol as p
from . import ui
from .scheduler import Request, Result

# 实时监测依次轮询的数据项；DI 必须已收录在 protocol.DI_TABLE。
MONITOR_ITEMS = (
    ("A相电压", 0x02010100),
    ("瞬时总有功功率", 0x02030000),
    ("总电能", 0x00000000),
    ("继电器状态", 0xF9010100),
    ("总功率因数", 0x02060000),
    ("总无功功率", 0x02040000),
)

DEFAULT_INTERVAL = 3   # 单主站模式默认轮询间隔秒

_POLL_STATUS = {"ok": "成功", "late": "迟到成功", "err": "表拒绝",
                "timeout": "超时", "failed": "失败", "aborted": "中断",
                "no_master": "无窗口"}

_NAME_W, _VALUE_W, _STATUS_W = 14, 30, 8
_PANEL_W = 67   # 与数据行同宽，标题/状态行按此裁剪
_MIN_ROWS = 16  # 备用屏幕至少需要的终端行数

# 备用屏幕缓冲：面板锚定在屏幕顶部绝对重绘，避免相对移动在终端
# 滚动后错位（Windows Terminal/ConPTY 下会残留旧行）；退出时还原主屏。
_ALT_ENTER = "\x1b[?1049h\x1b[?25l"
_ALT_LEAVE = "\x1b[?25h\x1b[?1049l"


def _wide(ch: str) -> int:
    return 2 if ord(ch) > 0x2E7F else 1


def _disp_width(text: str) -> int:
    return sum(_wide(ch) for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _clip(text: str, width: int) -> str:
    if _disp_width(text) <= width:
        return text
    out, used = "", 0
    for ch in text:
        w = _wide(ch)
        if used + w > width - 2:
            break
        out += ch
        used += w
    return out + ".."


def _short_value(result) -> str | None:
    """面板中显示的短值；继电器组合读只取状态段，不重复显示电能。"""
    if result.status not in ("ok", "late"):
        return None
    di = result.request.meta.get("di")
    if di == 0xF9010100 and result.payload is not None \
            and len(result.payload) >= 6:
        return p.relay_status_desc(result.payload[4:6])
    return result.value


def _summary(view: MonitorView) -> str:
    return (f"监测结束：共 {view.count} 次轮询，"
            f"成功 {view.ok}，失败 {view.fail}。")


class ExitKeys:
    """非阻塞检测退出键（Q/Esc）；Ctrl+C 由调用方捕获 KeyboardInterrupt。"""

    def __init__(self):
        self.exit = False
        self._msvcrt = None
        self._posix = None      # (termios, select, fd, 原始终端属性)
        try:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        except Exception:
            interactive = False
        if os.name == "nt":
            import msvcrt
            self._msvcrt = msvcrt
        elif interactive:
            import select as _select
            import termios as _termios
            import tty as _tty
            fd = sys.stdin.fileno()
            self._posix = (_termios, _select, fd, _termios.tcgetattr(fd))
            _tty.setcbreak(fd)

    def poll(self) -> bool:
        """扫描一次按键；按过 Q/Esc 之后恒返回 True。"""
        if self.exit:
            return True
        if self._msvcrt is not None:
            while self._msvcrt.kbhit():
                if self._msvcrt.getwch() in ("q", "Q", "\x1b"):
                    self.exit = True
                    break
        elif self._posix is not None:
            termios, select, fd, _old = self._posix
            try:
                while select.select([fd], [], [], 0)[0]:
                    data = os.read(fd, 1)
                    if not data:
                        break
                    if data in (b"q", b"Q", b"\x1b"):
                        self.exit = True
                        break
            except OSError:
                pass
        return self.exit

    def close(self):
        if self._posix is not None:
            termios, _select, fd, old = self._posix
            self._posix = None
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass


class MonitorView:
    """监测面板：tty 下在备用屏幕内原位刷新；输出被重定向时逐行打印。"""

    def __init__(self, title: str):
        self.title = _clip(title, _PANEL_W - 2)
        self.live = sys.stdout.isatty()
        if self.live:
            try:
                if shutil.get_terminal_size().lines < _MIN_ROWS:
                    self.live = False   # 终端太矮放不下整块面板
            except Exception:
                pass
        self._alt_active = False
        self.rows = {di: {"value": "--", "status": "待轮询", "ts": ""}
                     for _, di in MONITOR_ITEMS}
        self.status = ""
        self.count = 0
        self.ok = 0
        self.fail = 0
        if os.name == "nt" and self.live:
            os.system("")   # 让 Windows 控制台启用 ANSI 转义
        if self.live:
            sys.stdout.write(_ALT_ENTER)
            sys.stdout.flush()
            self._alt_active = True
        else:
            print(self.title)

    def close(self):
        """离开备用屏幕，还原进入监测前的界面。"""
        if self._alt_active:
            self._alt_active = False
            self.live = False
            sys.stdout.write(_ALT_LEAVE)
            sys.stdout.flush()

    def set_status(self, text: str):
        text = _clip(text, _PANEL_W - 2)
        if text == self.status:
            return
        self.status = text
        self.render()

    def update_result(self, name: str, di: int, result):
        row = self.rows[di]
        self.count += 1
        row["status"] = _POLL_STATUS.get(result.status, result.status)
        value = _short_value(result)
        if value:
            row["value"] = value
            row["ts"] = time.strftime("%H:%M:%S")
            self.ok += 1
        else:
            self.fail += 1
        self.status = _clip(f"{name}: {row['status']} {value or row['value']}",
                            _PANEL_W - 2)
        if self.live:
            self.render()
        else:
            print(f"[{name}] {row['status']} {value or '--'} {row['ts']}")

    def render(self):
        lines = self._lines()
        if not self.live:
            return
        # 备用屏幕顶部绝对定位，整块重绘，不滚动；定位符后必须紧跟
        # 第一行，中间不能插入换行，否则面板整体下移一行并残留旧行。
        body = "\n".join("\r\x1b[2K" + line for line in lines)
        sys.stdout.write("\x1b[H" + body + "\n")
        sys.stdout.flush()

    def _lines(self) -> list[str]:
        lines = ["=" * _PANEL_W, f" {self.title}", "=" * _PANEL_W,
                 f" {_pad('参数', _NAME_W)}  {_pad('当前值', _VALUE_W)}"
                 f"  {_pad('状态', _STATUS_W)}  更新时刻"]
        for name, di in MONITOR_ITEMS:
            row = self.rows[di]
            lines.append(f" {_pad(name, _NAME_W)}"
                         f"  {_pad(_clip(row['value'], _VALUE_W), _VALUE_W)}"
                         f"  {_pad(row['status'], _STATUS_W)}"
                         f"  {row['ts']}")
        lines += ["-" * _PANEL_W, f" {self.status or '(启动中)'}",
                  f" 已轮询 {self.count} 次：成功 {self.ok}，"
                  f"失败 {self.fail}（按 Q/Esc 退出）"]
        return lines


def run_monitor(session, *, coexist: bool) -> None:
    """实时监测入口；session 为 master_cli / coexist_cli 的会话对象。"""
    client = session.client
    if client is None:
        print("串口未连接，无法监测。")
        return
    scheduler = client.scheduler
    observer = getattr(scheduler, "observer", None)
    if coexist and observer is None:
        print("当前调度器不支持共存监测。")
        return

    if coexist:
        interval = None
        rhythm = "每窗1次"
    else:
        interval = ui.ask_int("每项轮询间隔秒", DEFAULT_INTERVAL, lo=1, hi=60)
        if ui._INPUT_CLOSED:
            return
        rhythm = f"每项间隔 {interval}s"
    title = (f"实时监测（{'共存主站' if coexist else '单主站'}） "
             f"目标 {client.address} {rhythm} 按 Q/Esc 退出")

    view = MonitorView(title)
    was_live = view.live
    keys = ExitKeys()
    original_log = scheduler.log
    scheduler.log = view.set_status          # 调度消息显示在状态行
    if not coexist:
        scheduler.stop_check = keys.poll     # 退出键可中断应答等待
    try:
        view.set_status("按顺序轮询中")
        if coexist:
            _loop_coexist(client, observer, view, keys)
        else:
            _loop_direct(client, view, keys, interval)
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.log = original_log
        scheduler.stop_check = None
        keys.close()
        if was_live:
            # 统计与确认在备用屏幕内完成：此时旧菜单还不可见，
            # 避免“旧菜单 + 按回车继续”叠加让用户误以为菜单已可输入。
            sys.stdout.write("\n " + _summary(view) + "\n")
            sys.stdout.flush()
        else:
            print(_summary(view))
        ui.pause("按回车返回主菜单")
        view.close()    # 离开备用屏幕，还原原界面
    if was_live:
        print(_summary(view))   # 还原后落一行摘要，衔接新旧菜单


def _sleep_checkable(keys: ExitKeys, seconds: float, view: MonitorView,
                     interval: int):
    end = time.monotonic() + max(0.0, seconds)
    while not keys.poll():
        remain = end - time.monotonic()
        if remain <= 0:
            return
        view.set_status(f"间隔 {interval}s，{remain:.0f}s 后读下一项")
        time.sleep(min(0.1, remain))


def _loop_direct(client, view: MonitorView, keys: ExitKeys, interval: int):
    """单主站模式：从每项读取开始计时，按固定间隔依次轮询。"""
    position = 0
    while not keys.poll():
        name, di = MONITOR_ITEMS[position % len(MONITOR_ITEMS)]
        position += 1
        view.set_status(f"正在读取 {name} ...")
        started = time.monotonic()
        result = client.read(di)
        if result.status == "aborted" and keys.exit:
            break    # 退出键触发调度器中断，不计入统计
        view.update_result(name, di, result)
        remain = interval - (time.monotonic() - started)
        if remain > 0 and not keys.poll():
            _sleep_checkable(keys, remain, view, interval)


def _wait_master_frame(observer, view: MonitorView,
                       keys: ExitKeys, anchor) -> bool:
    """等到观测到比 anchor 新的主站请求帧才返回 True；退出键返回 False。

    每个主站请求帧对应一个可注入的空窗；等到新帧才发下一查询，
    即保证每窗 1 次查询、轮询节奏与主站保持一致。
    """
    while not keys.poll():
        last = observer.last_master
        if last is not None and last != anchor:
            return True
        view.set_status("等待主站下一轮询帧 ...")
        observer.pump(0.05)
    return False


def _loop_coexist(client, observer, view: MonitorView, keys: ExitKeys):
    """共存模式：观测到一个新主站帧后，在其后的空窗内注入一次查询。"""
    position = 0
    anchor = None
    while not keys.poll():
        if not _wait_master_frame(observer, view, keys, anchor):
            continue    # 按了退出键，交给循环条件结束
        name, di = MONITOR_ITEMS[position % len(MONITOR_ITEMS)]
        position += 1
        view.set_status(f"等待空窗读取 {name} ...")
        result = client.read(di)
        if result.status == "aborted" and keys.exit:
            break    # 退出键触发调度器中断，不计入统计
        # 查询完成后以最新主站帧为锚点：下次必须等到更新的主站帧，
        # 才能保证一个主站周期（一窗）最多一次查询。
        anchor = observer.last_master
        view.update_result(name, di, result)


# ------------------------------------------------------------------ 自检
def selftest():
    for name, di in MONITOR_ITEMS:
        assert di in p.DI_BY_CODE, f"监测项 {name} 的 DI 未收录"

    assert _disp_width("A相电压") == 7
    assert _pad("AB", 5) == "AB   "
    assert _clip("abcdef", 4) == "ab.."
    assert _clip("电压表", 8) == "电压表"

    request = Request(p.build_read(p.wire_addr_of("123456789012"),
                                   0xF9010100),
                      "读 F9010100", meta={"di": 0xF9010100})
    payload = bytes.fromhex("56341200") + b"\x08\x02"
    result = Result(request, "ok", payload=payload)
    result.value = p.decode_value(0xF9010100, payload)
    assert _short_value(result) == "远程拉闸(单播 1A 所致) (08 02)"

    view = MonitorView("测试面板（自检）")
    assert len(view._lines()) == 7 + len(MONITOR_ITEMS)
    view.live = False
    view.update_result("A相电压", 0x02010100, result)
    assert view.rows[0x02010100]["status"] == "成功"
    assert view.count == 1 and view.ok == 1 and view.fail == 0
    print("monitor selftest OK")


if __name__ == "__main__":
    selftest()
