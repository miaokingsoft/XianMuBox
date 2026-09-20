"""
XianMuBox - 网络电台模式 (v2.6 纯实时流式直连)

收听闲沐云平台 radio_relay 电台转播 (framework/radio 服务层):
唯一交付路径 = 流式端点 (HTTP/1.1 chunked 长连接, audio/mpeg 连续
透传, 即时起播零转码; 首个听众到达服务端才连上游, 无人听 30 分钟
回收; 上游静默 >90s 结束听众流, 重播即恢复)。stream_url 由台代码
直接构造, 收听零取数。阻塞式播放接管, 按键经 is_cancelled 逐帧
轮询打断。

页面 (内容浏览类, 按键规范 2.4: A=翻页/下一项 B=主操作 C=退出):
  台单页 (默认): 电台列表 (内置快照纯内存秒出, 每屏 6 行光标滚动
    窗口, 光标定位上次收听的台)
    A 短按 = 下一台 (光标循环滚动)
    B 短按 = 收听选中台 (阻塞接管播放页)
    C 短按 = 退出返回主菜单
  播放页 (阻塞接管, 直播流直至按键):
    A 短按 = 换下一台 (循环)
    B 短按 = 停止返回台单页
    C 短按 = 退出模式
  固件无 mp3dec 模块时进入即提示需更新固件 (任意键退出)。

播放页布局 (240x240):
  y 8-32   标题行 (与台单页同款): 青色电台名 (hanzi_24, 截 8 字) + 右侧直播点
  y 38     分隔线 (与台单页一致)
  y 48-72  状态行: 左状态文字 (hz24, 接入中/收听/等待/错误) + 右上已播 MM:SS (vga)
  y 76-187 喇叭帧动画区 (80x80 居中, 复用 /icons/news_spk_N.raw 素材)
  y 188    直播信息行 (vga): LIVE
  y H-12   A:NEXT B:STOP C:EXIT

直播循环逻辑: 进台直连流; 流结束 (上游静默 >90s 服务端断流) 5s 短
退避重连; 连接/解码失败指数退避 (15s x2 -> 60s 起翻倍, 上限 30 分钟)
后重连同一 url (url 由台代码构造, 稳定); 听众已满 (42901, 上限
10/台) 60s 重试; 业务类错误 (未订阅/无此台/服务停用) 不自动重试。

MicroPython 兼容: 无 f-string / time.ticks_* / urequests 惰性 import
"""

import time
import gc

try:
    from config import W, H
except ImportError:
    W, H = 240, 240

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_GREEN, CLR_RED, CLR_AMBER,
)
from modes.base import BaseMode

_PER_PAGE = 6                # 台单每屏行数
_ROW_H = 28                  # 列表行距 (px)
_LIST_Y = 48                 # 列表首行 y (px)
_NAME_MAX = 8                # 播放页标题电台名最多字数
_RECONNECT_WAIT_SEC = 5      # 直播断流后的重连等待 (stream_url 复用)
_LIMIT_WAIT_SEC = 60         # 听众已满的重试等待 (上限 10/台)
_NET_FAIL_WAIT_SEC = 15      # 网络类失败前两档等待 (之后 60s 起翻倍)
_NET_WAIT_MAX_SEC = 1800     # 网络退避上限 (官方手册 30 分钟)

# ---- 播放页喇叭帧动画 (复用 news 的 raw 素材, build 由 PNG 生成) ----
_SPK_SZ = 80                  # 帧尺寸 (px)
_SPK_X = (W - _SPK_SZ) // 2   # 水平居中
_SPK_Y = 88                   # 动画区中轴
_SPK_SEQ = (0, 1, 2, 3, 2, 1) # 往返轮播序列 (声波 0→3→0, 按已播秒数切换)


def _net_wait_sec(fails):
    """网络类失败指数退避 (官方手册 60s 基值上限 30 分钟):
    前两档 15s (收听中快速自愈), 之后 60/120/240/... 封顶 1800s"""
    if fails <= 2:
        return _NET_FAIL_WAIT_SEC
    shift = min(fails - 3, 5)
    wait = 60 << shift
    return wait if wait < _NET_WAIT_MAX_SEC else _NET_WAIT_MAX_SEC


class RadioMode(BaseMode):
    MODE_ID = "radio"
    MODE_NAME = "网络电台"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._stations = []      # [{"code","name"}...] 内置快照 (纯内存)
        self._sel = 0            # 台单光标 (self._stations 下标)
        self._hz = None          # hanzi_24 字体
        self._no_fw = False      # 固件无 mp3dec, 进入即提示
        self._spk_cache = {}     # 喇叭帧号 -> bytearray / False

    # ---------- 生命周期 ----------

    def enter(self):
        gc.collect()
        self._hz = self._load_hanzi()
        self._no_fw = False
        try:
            from framework import audio
            if not audio.mp3_supported():
                self._no_fw = True
        except Exception:
            pass
        if self._no_fw:
            self._draw_fw_page()
            return
        # 台单为内置快照 (纯内存秒出, 零网络): 进模式直接成页
        try:
            from framework import radio as svc
            self._stations = [{"code": str(s.get("code", "")),
                               "name": str(s.get("name", ""))}
                              for s in svc.stations()]
        except Exception as e:
            print("[RADIO-MODE] stations error:", e)
            self._stations = []
        self._locate_last()
        self._draw_list()

    def loop(self):
        try:
            if self._no_fw:
                up, sel, down, _bl, _al, _cl = self._scan_with_timing()
                if up or sel or down:
                    self.flash_exit()
                    return False
                time.sleep_ms(50)
                return True
            up, sel, down, _bl, _al, _cl = self._scan_with_timing()
            if up:
                self._next_station()
            elif sel:
                if not self._listen_start():
                    self.flash_exit()
                    return False
            elif down:
                self.flash_exit()
                return False
            time.sleep_ms(30)
            return True
        except Exception as e:
            print("[RADIO-MODE] loop error:", repr(e))
            time.sleep_ms(200)
            return True

    def exit(self):
        self._stations = []
        self._hz = None
        self._spk_cache = {}
        gc.collect()

    # ---------- 台单页 ----------

    def _load_hanzi(self):
        try:
            from font import hanzi_24 as f
            return f
        except ImportError:
            return None

    def _sf(self):
        try:
            from font import vga1_8x8 as sf
            return sf
        except ImportError:
            return None

    def _locate_last(self):
        """光标定位上次收听的电台 (settings.radio.last_station)"""
        last = ""
        try:
            from framework.settings import get_radio
            last = str(get_radio().get("last_station") or "")
        except Exception:
            pass
        self._sel = 0
        if last:
            for i, s in enumerate(self._stations):
                if s["code"] == last:
                    self._sel = i
                    break

    def _next_station(self):
        """台单页 A 短按: 下一台光标循环 (台单为内置快照, 恒非空)"""
        self.beep(800, 15)
        self._sel = (self._sel + 1) % len(self._stations)
        self._draw_list()

    def _listen_start(self):
        """台单页 B 短按: 进入播放页。返回 False = C 键退出模式"""
        if not self._stations:
            self.beep(600, 40)
            return True
        self.beep(1200, 30)
        act = self._listen(self._sel)
        self._draw_list()
        return act != "exit"

    # ---------- 播放页 (阻塞接管, 直播流直连) ----------

    def _listen(self, idx):
        """播放页主循环: 收听 self._stations[idx], A 循环换台。

        直播直连 (v2.6): stream_url 由台代码直接构造, 进台即连流
        零取数; 流结束 (上游静默 >90s 服务端断流, 直播无「播完」概念)
        短退避后重连同一 url; 连接/解码失败指数退避后也重连同一 url
        (url 稳定, 无需刷新)。
        返回 "back"=B 停止返回台单 / "exit"=C 退出模式。
        """
        from framework import radio as svc
        d = self.display
        self.buttons.clear_events()
        while True:
            st = self._stations[idx]
            code = st["code"]
            name = st["name"]
            self._save_last(code)
            net_fail = 0        # 网络类失败连续档 (指数退避用)
            url = svc.stream_url(code)
            switch = False
            while True:
                state, act = self._play_stream(d, svc, name, url)
                if act is None:
                    print("[RADIO-MODE] play result: state=%s fail#%d"
                          % (state, net_fail))
                    if state == "ok":
                        # 流结束 = 上游静默 >90s 服务端断流: 短退避重连
                        act = self._wait_play(d, name, "信号中断",
                                              CLR_AMBER,
                                              _RECONNECT_WAIT_SEC)
                    elif state in ("fail", "badmp3"):
                        # 连接/解码失败: 指数退避后重连同一 url
                        net_fail += 1
                        msg = "解码异常" if state == "badmp3" else "信号中断"
                        act = self._wait_play(d, name, msg, CLR_RED,
                                              _net_wait_sec(net_fail))
                    elif state == "limit":
                        # 听众已满 (上限 10/台): 60s 后重试
                        act = self._wait_play(d, name, "听众已满",
                                              CLR_AMBER, _LIMIT_WAIT_SEC)
                    elif state == "noauth":
                        # 未订阅/未绑定: 业务错不自动重试, 等按键
                        act = self._wait_play(d, name, "未订阅电台源",
                                              CLR_RED, 0)
                    elif state in ("noent", "stop"):
                        # 无此台/服务停用: 业务错不自动重试, 等按键
                        act = self._wait_play(
                            d, name,
                            "电台暂不可用" if state == "noent"
                            else "电台服务未开放",
                            CLR_RED, 0)
                    else:
                        act = None
                if act == "switch":
                    switch = True
                    break
                if act == "back":
                    return "back"
                if act == "exit":
                    return "exit"
                # act 为 None: 倒计时尽, 重连
            if switch:
                idx = (idx + 1) % len(self._stations)

    def _play_stream(self, d, svc, name, url):
        """收听直播流 (stream_url chunked 长连接, 即时起播)。

        返回 (state, act): state 为 ok(流结束)/cancelled/limit/
        noent/stop/noauth/badmp3/fail; act 为 None / "switch" /
        "back" / "exit" (播放被打断时非 None)。
        """
        self._draw_play(d, name, "正在接入", CLR_AMBER)
        holder = {"act": None}
        prog = self._make_prog(d)
        state, _sec = svc.play_stream(url, self._volume(), prog,
                                      self._make_cancel(holder))
        if holder["act"]:
            return ("cancelled", holder["act"])
        return (state, None)

    def _make_cancel(self, holder):
        """play_mp3 的打断回调: 逐帧轮询按键并记录动作。
        A=换台 B=停止返回 C=退出, 与播放页按键语义一致。"""
        btns = self.buttons

        def _cancel():
            up, sel, down, _a, _b, _c = btns.scan_timed()
            if up:
                holder["act"] = "switch"
            elif sel:
                holder["act"] = "back"
            elif down:
                holder["act"] = "exit"
            return holder["act"] is not None

        return _cancel

    def _wait_play(self, d, name, msg, color, wait_sec):
        """播放页等待态 (错误退避/等待下一段), 键 A=换台 B=停止 C=退出。

        wait_sec>0 时倒计时显示 "N秒后重试", 计时尽返回 None (重取);
        wait_sec<=0 (业务类错误) 恒候直至按键。
        """
        self._draw_play(d, name, msg, color)
        btns = self.buttons
        btns.clear_events()
        end = time.ticks_add(time.ticks_ms(),
                             wait_sec * 1000) if wait_sec > 0 else 0
        last_n = -1
        while True:
            up, sel, down, _a, _b, _c = btns.scan_timed()
            if up:
                return "switch"
            if sel:
                return "back"
            if down:
                return "exit"
            if wait_sec > 0:
                left = time.ticks_diff(end, time.ticks_ms()) // 1000 + 1
                if left <= 0:
                    return None
                if left != last_n:
                    last_n = left
                    self._draw_countdown(d, left)
            time.sleep_ms(50)

    # ---------- 供 Web/后续扩展 ----------

    def _volume(self):
        try:
            from framework.settings import get_radio
            v = int(get_radio().get("volume", 80))
        except Exception:
            v = 80
        if not (0 <= v <= 100):
            v = 80
        return v

    def _save_last(self, code):
        """记住上次收听的电台 (台单页光标定位)"""
        try:
            from framework import settings as st
            st.update({"radio": {"last_station": code}})
        except Exception as e:
            print("[RADIO-MODE] save last error:", e)

    # ---------- 绘制 ----------

    def _write_len(self, font, s):
        """测量比例字库文本宽度 (write_len 不可用时按 MAX_WIDTH 估算)"""
        try:
            return self.display.write_len(font, s)
        except Exception:
            return len(s) * getattr(font, "MAX_WIDTH", 24)

    def _draw_list(self):
        """台单页整体重绘: 标题+页码, 光标滚动窗口列表, 底部提示"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            d.write(self._hz, "网络电台", 8, 8, CLR_CYAN, CLR_BG)
        if self._stations:
            sf = self._sf()
            if sf:
                tag = "%d/%d" % (self._sel + 1, len(self._stations))
                d.text(sf, tag, W - 8 - 8 * len(tag), 16, CLR_GRAY, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        if self._stations:
            start = (self._sel // _PER_PAGE) * _PER_PAGE
            y = _LIST_Y
            for i in range(_PER_PAGE):
                k = start + i
                if k >= len(self._stations):
                    break
                sel = k == self._sel
                color = CLR_AMBER if sel else CLR_WHITE
                if self._hz:
                    if sel:
                        d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
                    d.write(self._hz, self._stations[k]["name"][:8],
                            8 + 24, y, color, CLR_BG)
                y += _ROW_H
        sf = self._sf()
        if sf:
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:LISTEN", (W - 8 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:EXIT", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        gc.collect()

    def _draw_fw_page(self):
        """固件无 mp3dec 提示页 (任意键退出)"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            for text, color, y in (
                    ("固件不支持", CLR_RED, 84),
                    ("MP3电台播放", CLR_RED, 116),
                    ("请更新固件", CLR_AMBER, 148)):
                d.write(self._hz, text,
                        (W - self._write_len(self._hz, text)) // 2,
                        y, color, CLR_BG)
        sf = self._sf()
        if sf:
            d.text(sf, "ANY KEY:EXIT", (W - 12 * 8) // 2, H - 20,
                   CLR_GRAY, CLR_BG)

    def _draw_play(self, d, name, state, color):
        """播放页整页静态框: 标题行 + 状态行 + 直播信息行 + 静态喇叭 + 提示"""
        d.fill(CLR_BG)
        # 顶部标题行 (与台单页同款): 青色电台名 (截 _NAME_MAX 字) + 右侧直播点
        self._live_dot(d, True)
        if self._hz:
            d.write(self._hz, name[:_NAME_MAX], 8, 8, CLR_CYAN, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        # 状态行: 左状态文字 (右上部留给已播时长)
        if self._hz and state:
            d.write(self._hz, state[:7], 8, 48, color, CLR_BG)
        self._draw_elapsed(d, -1)
        # 直播信息行
        sf = self._sf()
        if sf:
            d.text(sf, "LIVE", (W - 4 * 8) // 2, 188, CLR_GRAY, CLR_BG)
        self._spk_frame(d, 2)
        sf = self._sf()
        if sf:
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:STOP", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:EXIT", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        gc.collect()

    def _draw_countdown(self, d, left):
        """等待态倒计时文字 (动画区中轴, 每秒一次)"""
        if not self._hz:
            return
        t = "%d秒后重试" % left
        d.fill_rect(0, 116, W, 32, CLR_BG)
        d.write(self._hz, t, (W - self._write_len(self._hz, t)) // 2,
                120, CLR_AMBER, CLR_BG)

    def _live_dot(self, d, on):
        """标题行右侧直播点 (播放中经进度回调 500ms 相位闪烁)"""
        d.fill_rect(W - 18, 10, 8, 8, CLR_WHITE if on else CLR_DIM)

    def _draw_elapsed(self, d, sec):
        """状态行右上已播时长 MM:SS (sec<0 时清除)"""
        sf = self._sf()
        if not sf:
            return
        d.fill_rect(184, 54, 48, 14, CLR_BG)
        if sec >= 0:
            d.text(sf, "%02d:%02d" % (sec // 60, sec % 60), 192, 58,
                   CLR_GRAY, CLR_BG)

    def _make_prog(self, d):
        """播放动态 UI 回调: 首秒切「正在收听」+ 喇叭帧轮播 + 已播时长
        + 直播点闪烁 (直播流无总长, 不画进度条)"""
        shown = {"sec": -1, "f": -1, "started": False}

        def _prog(sec):
            if not shown["started"]:
                shown["started"] = True
                d.fill_rect(0, 48, 176, 30, CLR_BG)
                if self._hz:
                    d.write(self._hz, "正在收听", 8, 48, CLR_GREEN, CLR_BG)
            f = _SPK_SEQ[sec % 6]
            if f != shown["f"]:
                shown["f"] = f
                self._spk_frame(d, f)
            if sec != shown["sec"]:
                shown["sec"] = sec
                self._draw_elapsed(d, sec)
            self._live_dot(d, ((time.ticks_ms() // 500) & 1) == 0)

        return _prog

    def _spk_frame(self, d, idx):
        """blit 单帧喇叭动画 (复用 /icons/news_spk_N.raw, 80x80 RGB565)。
        raw 首次使用时读入并常驻缓存; 缺失时该区域保持纯黑 (安全降级)。"""
        buf = self._spk_cache.get(idx)
        if buf is None:
            buf = False
            try:
                import os
                path = "/icons/news_spk_%d.raw" % idx
                os.stat(path)
                f = open(path, "rb")
                try:
                    data = f.read()
                finally:
                    f.close()
                if len(data) == _SPK_SZ * _SPK_SZ * 2:
                    buf = bytearray(data)
            except Exception:
                buf = False
            self._spk_cache[idx] = buf
        if buf:
            d.blit_buffer(memoryview(buf), _SPK_X, _SPK_Y, _SPK_SZ, _SPK_SZ)
