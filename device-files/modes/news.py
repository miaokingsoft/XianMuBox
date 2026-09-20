"""
XianMuBox - 整点新闻播报模式 (v1.9)

浏览闲沐云平台 news_hourly 当档新闻标题, 并可手动触发一次播报
(音频经 framework/audio + MAX98357A 输出; 整点自动播报由
framework/news 全局服务负责, 本模式只做浏览与手动触发)。

页面 (内容浏览类, 按键规范 2.4: A=翻页/下一项 B=主操作 C=退出):
  标题浏览页: 当档新闻标题列表, 每页 6 条 (每条 1 行, 超长截断加省略号)
    A 短按 = 下一页 (循环, 末页后进入今日回听页)
    B 短按 = 立即播报当档 (阻塞接管, 播完自动回到本页)
    C 短按 = 退出返回主菜单
  今日回听页 (v1.9, A 翻页循环末页): SD 卡当日存档时段列表
    (/sd/music/news_YYYYMMDD_HH.wav, 每屏 6 行光标滚动窗口)
    A 短按 = 下一时段 (循环; 空表时返回标题浏览页)
    B 短按 = 回听选中时段 (阻塞接管, 播完自动回到本页)
    C 短按 = 返回标题浏览页
  走马灯轮巡 (v1.8.4): 静置 10s 自动逐条以青色滚动展示完整标题
  (仅滚一次后恢复静态截断态), 本页走完自动翻页, 末页走完停在最后一页;
  任意按键随时打断, 按键/重新进入后可再次触发 (仅标题浏览页)。

数据依赖 (framework/cloud 统一取数):
  GET /api/v1/data/news_hourly -> payload.items[].title 等;
  未订阅/无网/无档期时降级为错误提示页, B 仍可重试。
  进入时优先使用 framework/news 的最近播报缓存, 无缓存才联网。

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
from framework.wifi import check_wifi
from modes.base import BaseMode

_FETCH_COOLDOWN_MS = 60000   # 拉取失败后的重试冷却
_PER_PAGE = 6                # 每页标题条数
_ROW_H = 28                  # 列表行距 (px)
_LIST_Y = 48                 # 列表首行 y (px)
_RIGHT_PAD = 8               # 列表右边距 (px)
_IDLE_TOUR_MS = 10000        # 静置触发走马灯轮巡 (v1.8.4)
_MARQ_CHAR_MS = 350          # 走马灯逐字步进间隔 (v1.8.6)
_MARQ_END_PAUSE_MS = 800     # 单条滚到尾部后的停留
_MARQ_GAP_MS = 300           # 条目间间隔


class NewsMode(BaseMode):
    MODE_ID = "news"
    MODE_NAME = "整点新闻"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._titles = []        # 当档标题列表 (旧->新)
        self._hour_label = ""    # 档期, 如 "15点"
        self._source_name = ""   # 来源标注 (v1.7 起页面不展示, 字段保留)
        self._err = ""           # 拉取错误信息 (空=正常)
        self._page = 0           # 当前页码 (每页 _PER_PAGE 条)
        self._fetched = False    # 进入后是否已尝试联网拉取
        self._last_try_ms = 0    # 上次拉取时刻 (失败冷却)
        self._hz = None          # UI 固定文案字体 (hanzi_24, 全量覆盖)
        self._tfont = None       # 新闻正文字体 (hanzi_24 全量覆盖, 任意新闻文本)
        self._tour = None        # 走马灯状态机 (None=未激活, v1.8.4)
        self._tour_done = False  # 本次停留已走完一轮 (仅一次锁)
        self._idle_since = 0     # 空闲计时起点 (ticks_ms)
        self._rpt = []           # 今日 SD 存档时段列表 (v1.9, 升序小时数)
        self._rpt_idx = 0        # 回听页光标 (self._rpt 下标)

    # ---------- 生命周期 ----------

    def enter(self):
        """进入模式: 先用最近播报缓存渲染, 首帧再联网刷新"""
        gc.collect()
        self._hz = self._load_hanzi()
        self._tfont = self._load_text_font()
        self._load_cached()
        self._tour = None
        self._tour_done = False
        self._idle_since = time.ticks_ms()
        self._draw_page()

    def loop(self):
        try:
            # 首帧后台拉取 (页面先渲染, 避免黑屏等待)
            if not self._fetched:
                self._fetched = True
                self._auto_fetch()
            up, sel, down, _bl, _al, _cl = self._scan_with_timing()
            if up or sel or down:
                # 任意键打断走马灯, 并复位空闲计时与仅一次锁 (可重新轮巡)
                self._tour = None
                self._tour_done = False
                self._idle_since = time.ticks_ms()
                if up:
                    if self._rpt_page():
                        self._rpt_next()
                    else:
                        self._next_page()
                elif sel:
                    if self._rpt_page():
                        self._play_replay()
                    else:
                        self._play()
                else:
                    if self._rpt_page():
                        self.beep(800, 15)
                        self._goto_title()
                    else:
                        self.flash_exit()
                        return False
            elif self._tour:
                self._marq_step()
            elif self._marq_should_start():
                self._marq_start()
            time.sleep_ms(30)
            return True
        except Exception as e:
            print("[NEWS-MODE] loop error:", repr(e))
            time.sleep_ms(200)
            return True

    def exit(self):
        self._titles = None
        self._hz = None
        self._rpt = []
        gc.collect()

    # ---------- 数据 ----------

    def _load_hanzi(self):
        """UI 固定文案字体 (hanzi_24, 全量覆盖)"""
        try:
            from font import hanzi_24 as f
            return f
        except ImportError:
            return None

    def _load_text_font(self):
        """新闻正文字体 (v1.8 起 hanzi_24 全量 GB2312+ASCII,
        历史上 hanzi24 约千字优先于 hz_24x24 的双轨选择已统一)。"""
        try:
            from font import hanzi_24 as f
            return f
        except ImportError:
            return self._hz

    def _load_cached(self):
        """读 framework/news 最近播报缓存 (无网络请求)"""
        try:
            from framework import news as svc
            st = svc.status()
            self._titles = list(st.get("items") or [])
            self._hour_label = str(st.get("hour_label") or "")
            self._source_name = str(st.get("source_name") or "")
            if self._titles:
                self._err = ""
                self._page %= self._page_count()
            else:
                self._err = str(st.get("msg") or "")
        except Exception:
            self._titles = []
            self._err = "尚未播报"

    def _auto_fetch(self):
        """联网拉取当档数据 (阻塞约 1~10s, 失败后进入冷却)"""
        now = time.ticks_ms()
        if self._titles and time.ticks_diff(now, self._last_try_ms) < 0:
            return
        if not self._titles:
            self._draw_state("拉取当档新闻...", CLR_GRAY)
        try:
            from framework import news as svc
            payload, err = svc.fetch_latest()
        except Exception as e:
            print("[NEWS-MODE] fetch error:", e)
            payload, err = None, "云服务异常"
        self._last_try_ms = time.ticks_add(now, _FETCH_COOLDOWN_MS)
        if payload is None:
            if not self._titles:
                self._err = err
                self._draw_page()
            return
        items = payload.get("items")
        titles = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("title"):
                    titles.append(str(it.get("title"))[:30])
        self._titles = titles
        self._hour_label = str(payload.get("hour_label", ""))
        self._source_name = str(payload.get("source_name", "")).strip()
        self._err = "" if titles else "当档暂无标题"
        self._page = 0
        self._tour_done = False
        self._idle_since = time.ticks_ms()
        self._draw_page()

    def _page_count(self):
        """总页数 (至少 1 页)"""
        return max(1, (len(self._titles) + _PER_PAGE - 1) // _PER_PAGE)

    def _write_len(self, font, s):
        """测量比例字库文本宽度 (write_len 不可用时按 MAX_WIDTH 估算)"""
        try:
            return self.display.write_len(font, s)
        except Exception:
            return len(s) * getattr(font, "MAX_WIDTH", 24)

    def _rpt_page(self):
        """当前是否处于今日回听页 (A 翻页循环末页, v1.9)"""
        return self._page >= self._page_count()

    def _goto_title(self):
        """回听页 C/A(空表) : 返回标题浏览页第 1 页"""
        self._page = 0
        self._tour_done = False
        self._idle_since = time.ticks_ms()
        self._draw_page()

    def _load_replay(self):
        """读今日 SD 存档时段列表 (framework/news.replay_list)"""
        try:
            from framework import news as svc
            self._rpt = svc.replay_list()
        except Exception as e:
            print("[NEWS-MODE] replay list error:", repr(e))
            self._rpt = []
        if self._rpt_idx >= len(self._rpt):
            self._rpt_idx = 0

    def _rpt_next(self):
        """回听页 A 短按: 下一时段 (循环+滚窗); 空表时返回标题页"""
        if not self._rpt:
            self._goto_title()
            return
        self.beep(800, 15)
        self._rpt_idx = (self._rpt_idx + 1) % len(self._rpt)
        self._draw_page()

    def _play_replay(self):
        """回听页 B 短按: 回听选中时段 (阻塞接管, 返回后整页重绘)"""
        if not self._rpt:
            self.beep(600, 40)
            return
        hour = self._rpt[self._rpt_idx]
        try:
            from framework import news as svc
            self.beep(1200, 30)
            svc.replay(hour, self.display, self.buttons)
        except Exception as e:
            print("[NEWS-MODE] replay error:", repr(e))
        self._tour_done = False
        self._idle_since = time.ticks_ms()
        self._load_replay()
        self._draw_page()

    def _next_page(self):
        """A 短按: 下一页标题 (循环; 末页后进入今日回听页)"""
        if not self._titles:
            # 无数据时 A 触发重试
            self._draw_state("重试拉取...", CLR_GRAY)
            self._last_try_ms = 0
            self._fetched = False
            return
        self.beep(800, 15)
        self._page = (self._page + 1) % (self._page_count() + 1)
        if self._rpt_page():
            self._load_replay()
        self._draw_page()

    # ---------- 走马灯轮巡 (v1.8.4, 非阻塞状态机) ----------
    # 静置 10s 自动启动: 逐条以青色走马灯逐字步进展示完整标题 (仅滚一次,
    # 滚完恢复静态白色截断态), 本页走完自动翻页, 末页走完停在最后一页。
    # 任意按键随时打断; 按键/重新进入后可再次触发。逐帧推进不阻塞
    # BaseMode.run 的整点播报/闹钟/云端接管检查。
    # v1.8.6: 像素平滑滚动 (tx-off 负 x 绘制) 在真机驱动上有顶部缠影,
    # 改为窗口固定的逐字步进 (t[i:i+cpl] 恒在屏内, 零负坐标/零边缘裁剪)。

    def _marq_should_start(self):
        """静置超时且有可滚动内容时启动 (本轮仅一次; 仅标题浏览页)"""
        if self._tour_done or not self._titles or self._err:
            return False
        if self._rpt_page():
            return False
        return time.ticks_diff(time.ticks_ms(),
                               self._idle_since) >= _IDLE_TOUR_MS

    def _marq_start(self):
        """从当前页第 0 行启动走马灯"""
        self._tour = {"row": 0, "phase": "gap", "i": 0,
                      "t0": time.ticks_ms(), "tl": time.ticks_ms()}

    def _marq_step(self):
        """逐帧推进一相 (gap 等待 / scroll 滚动 / pause 尾部停留)"""
        st = self._tour
        now = time.ticks_ms()
        tf, cw, tx, cpl = self._marq_geom()
        k = self._page * _PER_PAGE + st["row"]
        if k >= len(self._titles):
            self._marq_next_row(st, cpl)
            return
        y = _LIST_Y + st["row"] * _ROW_H
        if st["phase"] == "gap":
            if time.ticks_diff(now, st["t0"]) < _MARQ_GAP_MS:
                return
            if len(self._titles[k]) <= cpl:
                # 短标题本就完整, 跳过不滚
                self._marq_next_row(st, cpl)
                return
            st["phase"] = "scroll"
            st["i"] = 0
            st["tl"] = now
        elif st["phase"] == "scroll":
            if time.ticks_diff(now, st["tl"]) < _MARQ_CHAR_MS:
                return  # 逐字步进节拍
            st["tl"] = now
            st["i"] += 1
            if st["i"] >= len(self._titles[k]) - cpl:
                # 尾窗已完整展示, 停留后恢复静态
                st["phase"] = "pause"
                st["t0"] = now
            self._marq_draw_scroll(self._titles[k], y, tx, cpl, st["i"])
        else:  # pause: 尾部停留后恢复静态, 进下一条
            if time.ticks_diff(now, st["t0"]) < _MARQ_END_PAUSE_MS:
                return
            self.display.fill_rect(0, y - 3, W, _ROW_H, CLR_BG)
            self._draw_row(k, y)
            self._marq_next_row(st, cpl)

    def _marq_draw_scroll(self, t, y, tx, cpl, i):
        """滚动帧: 整行清底后绘制第 i 个滚动窗口 t[i:i+cpl] (青色)。
        窗口恒在屏内 (无负坐标/无右缘出格), 行首符号滚动期间隐藏"""
        d = self.display
        d.fill_rect(0, y - 3, W, _ROW_H, CLR_BG)
        d.write(self._tfont or self._hz, t[i:i + cpl], tx, y,
                CLR_CYAN, CLR_BG)

    def _marq_next_row(self, st, cpl):
        """当前条目结束: 找本页下一条可滚动标题; 页尽翻页, 全部走完收束"""
        row = st["row"] + 1
        base = self._page * _PER_PAGE
        while row < _PER_PAGE and base + row < len(self._titles):
            if len(self._titles[base + row]) > cpl:
                st["row"] = row
                st["phase"] = "gap"
                st["i"] = 0
                st["t0"] = time.ticks_ms()
                st["tl"] = st["t0"]
                return
            row += 1
        # 本页走完: 自动翻页继续; 末页走完停在最后一页 (仅一次锁)
        if self._page + 1 < self._page_count():
            self._page += 1
            self._draw_page()
            st["row"] = 0
            st["phase"] = "gap"
            st["i"] = 0
            st["t0"] = time.ticks_ms()
            st["tl"] = st["t0"]
        else:
            self._tour = None
            self._tour_done = True

    def _play(self):
        """B 短按: 立即播报当档 (阻塞接管, 返回后整页重绘)"""
        try:
            from framework import news as svc
            self.beep(1200, 30)
            if not svc.play_now(self.display, self.buttons, self.buzzer):
                return
            # 播报会接管并覆盖本页; 结束后读最新缓存重绘
            self._load_cached()
        except Exception as e:
            print("[NEWS-MODE] play error:", repr(e))
        self._tour_done = False
        self._idle_since = time.ticks_ms()
        self._draw_page()

    # ---------- 绘制 ----------

    def _marq_geom(self):
        """列表几何: (正文字体, 字宽, 正文起点 tx, 每行字数 cpl)"""
        tf = self._tfont or self._hz
        cw = getattr(tf, "MAX_WIDTH", 24) if tf else 24
        bullet_w = self._write_len(self._hz, ">") if self._hz else 0
        tx = 8 + bullet_w + 4
        cpl = max(1, (W - tx - _RIGHT_PAD) // cw)
        return tf, cw, tx, cpl

    def _draw_row(self, k, y, color=CLR_WHITE):
        """绘制第 k 条标题行 (静态截断态); 走马灯恢复亦复用"""
        d = self.display
        tf, cw, tx, cpl = self._marq_geom()
        t = self._titles[k]
        if self._hz:
            d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
        if tf:
            if len(t) > cpl:
                d.write(tf, t[:cpl - 1], tx, y, color, CLR_BG)
                # 省略号用 UI 字库绘制 (真机/仿真都有字模)
                d.write(self._hz, "…", tx + (cpl - 1) * cw, y, color, CLR_BG)
            else:
                d.write(tf, t, tx, y, color, CLR_BG)

    def _draw_state(self, text, color):
        """状态行文字 (24px 中文, 标题区)"""
        if self._hz:
            self.display.fill_rect(0, 44, W, 30, CLR_BG)
            self.display.write(self._hz, text[:9], 8, 48, color, CLR_BG)

    def _draw_page(self):
        """页面分发: 今日回听页 / 标题浏览页 (v1.9)"""
        if self._rpt_page():
            self._draw_replay_page()
            return
        self._draw_titles_page()

    def _draw_replay_page(self):
        """今日回听页 (v1.9): SD 当日存档时段列表, 光标滚动窗口"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            d.write(self._hz, "今日回听", 8, 8, CLR_CYAN, CLR_BG)
            if self._rpt:
                cnt = "%d档" % len(self._rpt)
                d.write(self._hz, cnt, W - 8 - self._write_len(self._hz, cnt),
                        8, CLR_AMBER, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        if not self._rpt:
            # 空态: 区分未挂载 / 当日尚无存档
            try:
                from framework import news as svc
                mounted = svc.sd_mounted()
            except Exception:
                mounted = False
            msg = "SD卡未挂载" if not mounted else "今日暂无存档"
            color = CLR_RED if not mounted else CLR_GRAY
            if self._hz:
                d.write(self._hz, msg, 8, 96, color, CLR_BG)
        else:
            start = (self._rpt_idx // _PER_PAGE) * _PER_PAGE
            y = _LIST_Y
            for i in range(_PER_PAGE):
                k = start + i
                if k >= len(self._rpt):
                    break
                self._draw_replay_row(k, y, k == self._rpt_idx)
                y += _ROW_H
        # 底部按键提示 (8px ASCII)
        try:
            from font import vga1_8x8 as sf
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:PLAY", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:BACK", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        except ImportError:
            pass
        gc.collect()

    def _draw_replay_row(self, k, y, selected):
        """回听列表第 k 行: 光标行琥珀色 > 标记, 其余白色无标记"""
        d = self.display
        hour = self._rpt[k]
        label = "%d点档" % hour
        color = CLR_AMBER if selected else CLR_WHITE
        if self._hz:
            if selected:
                d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
            d.write(self._hz, label, 8 + 24, y, color, CLR_BG)

    def _draw_titles_page(self):
        """标题浏览页整体重绘 (顶部档期+标题/右上页码, 中部列表)"""
        d = self.display
        d.fill(CLR_BG)
        # 顶部: 左侧 "{档期}新闻播报", 右侧页码
        if self._hz:
            x = 8
            if self._hour_label:
                hl = self._hour_label[:4]
                d.write(self._hz, hl, x, 8, CLR_AMBER, CLR_BG)
                x += self._write_len(self._hz, hl)
            d.write(self._hz, "新闻播报", x, 8, CLR_CYAN, CLR_BG)
        if self._titles:
            try:
                from font import vga1_8x8 as sf
                tag = "%d/%d" % (self._page + 1, self._page_count())
                d.text(sf, tag, W - 8 - 8 * len(tag), 16, CLR_GRAY, CLR_BG)
            except ImportError:
                pass
        d.hline(0, 38, W, CLR_DIM)
        # 中部: 错误提示 或 标题列表 (每页 6 条, 行首>符号, 每条 1 行超长截断)
        if self._err and not self._titles:
            if self._hz:
                d.write(self._hz, self._err[:9], 8, 96, CLR_RED, CLR_BG)
            try:
                from font import vga1_8x8 as sf
                d.text(sf, "A:RETRY", 8, H - 40, CLR_GRAY, CLR_BG)
            except ImportError:
                pass
        elif self._titles:
            base = self._page * _PER_PAGE
            y = _LIST_Y
            for i in range(_PER_PAGE):
                k = base + i
                if k >= len(self._titles):
                    break
                self._draw_row(k, y)
                y += _ROW_H
        # 底部按键提示 (8px ASCII)
        try:
            from font import vga1_8x8 as sf
            d.text(sf, "A:PAGE", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:PLAY", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:EXIT", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        except ImportError:
            pass
        # WiFi 状态条省略 (右上角留给页码, 联网状态由 B 播报结果体现)
        gc.collect()
