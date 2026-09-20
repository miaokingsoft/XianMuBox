"""
XianMuBox - 持仓股票模式（金融终端深色版 · 三场景 v2.2）
闲沐工坊

每只持仓股票三个场景 (B 键循环):
  场景1 简易: 现价大字 + 涨跌幅徽章双焦点 / 成本↔现价对比条 / 持仓盈亏
  场景2 明细: 两列数据表 (今开/昨收/最高/最低/量/额/换手/振幅/PE/PB/市值/涨跌停)
  场景3 K线:  满宽网格 / 成交量柱带 / AI预测段(品红) / 最新价虚线

数据双源:
  - mooview 接口: 持仓列表(仅进入模式时拉一次; 失败可在空态按 B 重试)
    + 日K线/AI预测(30min缓存按需)
  - 腾讯免费行情: 全部持仓批量实时价, 交易时段(周一~五 9:15-15:05)每10s,
    非交易时段降频10min; 失败静默回退 mooview 持仓价

操作: A=切换股票(循环,120s自动翻页) B=循环切场景(空态=重试拉持仓) C=退出

渲染: 每场景整页绘入 framebuf 离屏画布(大端字节序技巧)后单次 blit_buffer
上屏, 数据刷新零闪烁; 场景/股票切换 300ms 滑动过场; 全部 HTTP 走
framework.http_nb 非阻塞状态机, 等待数据期间动画不冻结。

注:
  - 腾讯行情时间戳上屏 (TX + HH:MM), 回退 mooview 时显示 (MV)
  - 行情降级链: 主域名 → 备用域名 → urequests 阻塞兜底(与 user/kxian 同款),
    全失败才回退 (MV), 并把失败原因带到场景2 空态 (自诊断, 不用抓串口)
  - 网络失败显示 "网络中断" (网络/中断 为字库覆盖字)
  - 文件头旧版 "B 长按返回主菜单" 手势已废弃, 按键规范见框架文档 2.4
"""

import time
import gc
from font import vga1_8x16 as vga8
from font import stockchars_24x24 as hanzi24

from .base import BaseMode
from . import stock_quote as sq
from framework import http_nb
from framework.colors import (
    CLR_WHITE,
    CLR_STK_BG, CLR_STK_HDR, CLR_STK_UP, CLR_STK_DN,
    CLR_STK_GAIN, CLR_STK_LOSS, CLR_STK_TAG,
    CLR_STK_FCAST, CLR_STK_PAGEDOT, CLR_STK_PAGEDOT_OFF,
    CLR_STK_BTN_BG, CLR_STK_BRACKET,
    CLR_STK_TXT_DIM, CLR_STK_TXT_HI,
    CLR_STK_GRID, CLR_STK_UP_DIM, CLR_STK_DN_DIM,
)
from framework.settings import get_stock_config, _DEFAULT_STOCK

try:
    import framebuf
except ImportError:
    framebuf = None  # PC 仿真由 tools/simulator/stubs 注入等价实现

from config import W, H

# 兜底常量：仅在 settings 异常时使用，真实值从 settings.json → stock 读取
_DEFAULT_ENDPOINT = _DEFAULT_STOCK["endpoint"]

# ====================== 刷新 / 缓存周期 ======================
# 持仓列表: 仅在 enter() 拉取一次, 不再周期刷新 (读取失败可在空态按 B 重试)
_KLINE_TTL_MS      = 30 * 60_000    # K 线缓存 30 分钟
_QUOTE_MS_TRADING  = 10_000         # 实时行情: 交易时段 10s
_QUOTE_MS_IDLE     = 600_000        # 实时行情: 非交易时段 10min
_AUTO_PAGE_MS      = 120_000        # 自动翻下一支持仓
_TRANS_MS          = 300            # 场景/股票切换滑动过场时长
_BTN_FB_MS         = 250            # 按钮按下反馈时长 (非阻塞)
_LOADING_MS        = 2500           # 加载进度条推进到 95% 的时长

# ====================== 场景定义 ======================
SCENE_SIMPLE = 1   # 简易行情
SCENE_DETAIL = 2   # 明细数据
SCENE_KLINE  = 3   # 预测K线
# B 键标签 = 按下后进入的场景
_SCENE_BTNS = {
    SCENE_SIMPLE: ["翻页", "明细", "退出"],
    SCENE_DETAIL: ["翻页", "K线", "退出"],
    SCENE_KLINE:  ["翻页", "简易", "退出"],
}
# 空态/错误态 (无持仓或读取失败): 中间键由"主操作"改为"重试拉取持仓"
_BTNS_EMPTY = ["翻页", "重试", "退出"]

# ====================== 布局常量（240x240）======================
HEADER_H = 40          # 顶部栏
STRIP_Y  = 41          # 进度/加载条 (自动翻页倒计时 + K线加载动画)
STRIP_H  = 4
BTN_Y0   = 199         # 按钮区
BTN_W    = 73
BTN_H    = 36

# 场景1 简易
S1_PRICE_Y = 54        # 现价大字 (vga1_16x32, 高32)
S1_BADGE_X = 148       # 涨跌幅徽章
S1_BADGE_Y = 52
S1_BADGE_W = 82
S1_BADGE_H = 36
S1_COST_Y  = 104       # 成本/每股盈亏行
S1_BAR_Y   = 126       # 成本↔现价对比条轨道
S1_BAR_H   = 5
S1_POS_Y   = 152       # Pos / 总盈亏行
S1_TS_Y    = 176       # 行情来源+时间戳行

# 场景2 明细 (两列表格 6 行)
S2_Y0    = 50
S2_ROW_H = 22
S2_LBL_X = (10, 124)   # 两列标签 x
S2_VAL_R = (114, 228)  # 两列数值右对齐边界
S2_LIM_Y = 178         # 涨跌停行
S2_TS_Y  = 176         # 时间戳 (右侧)

# 场景3 K线
CHART_X0 = 8           # K线画布左
CHART_X1 = 232         # K线画布右 (去掉Y轴刻度区, 满宽用足画布)
CHART_Y0 = 48
CHART_Y1 = 150         # 主图区
VOL_Y0   = 154
VOL_Y1   = 182         # 成交量柱带 (日期行 y=183, 底边紧贴按钮区 199)
BAR_W    = 6
BAR_GAP  = 2
BAR_PITCH = BAR_W + BAR_GAP
MAX_BARS = (CHART_X1 - CHART_X0) // BAR_PITCH   # 28 根满宽


# ====================== 离屏渲染基础 ======================
def _sw(c):
    """RGB565 字节序交换: framebuf 以小端存储像素,
    先交换颜色值写入, 整块缓冲即成为设备 blit_buffer 需要的大端字节流"""
    return ((c & 0xFF) << 8) | (c >> 8)


# 调色板离屏版 (画布内一律使用交换值; 直接绘屏的进度条用原值)
C_BG    = _sw(CLR_STK_BG)
C_HDR   = _sw(CLR_STK_HDR)
C_UP    = _sw(CLR_STK_UP)
C_DN    = _sw(CLR_STK_DN)
C_GAIN  = _sw(CLR_STK_GAIN)
C_LOSS  = _sw(CLR_STK_LOSS)
C_TAG   = _sw(CLR_STK_TAG)
C_FCAST = _sw(CLR_STK_FCAST)
C_PDOT  = _sw(CLR_STK_PAGEDOT)
C_PDOT_OFF = _sw(CLR_STK_PAGEDOT_OFF)
C_BTN_BG   = _sw(CLR_STK_BTN_BG)
C_BRACKET  = _sw(CLR_STK_BRACKET)
C_TXT_DIM  = _sw(CLR_STK_TXT_DIM)
C_TXT_HI   = _sw(CLR_STK_TXT_HI)
C_GRID     = _sw(CLR_STK_GRID)
C_UP_DIM   = _sw(CLR_STK_UP_DIM)
C_DN_DIM   = _sw(CLR_STK_DN_DIM)


def _font_bits(font):
    return getattr(font, "FONT", None) if getattr(font, "FONT", None) is not None \
        else getattr(font, "_FONT")


def _fb_text(fb, font, s, x, y, fg, bg):
    """定宽 ASCII 字体 (vga1_*) 绘入离屏画布, 返回宽度"""
    data = _font_bits(font)
    wd, ht = font.WIDTH, font.HEIGHT
    first = getattr(font, "FIRST", 0x20)
    last = getattr(font, "LAST", first + 95)
    rb = (wd + 7) // 8
    cx = x
    for ch in str(s):
        idx = ord(ch) - first
        if idx < 0 or idx > last - first:
            idx = ord("?") - first
        off = idx * ht * rb
        if bg is not None:
            fb.fill_rect(cx, y, wd, ht, bg)
        if rb == 1:
            for ry in range(ht):
                b = data[off + ry]
                if b == 0:
                    continue
                for rx in range(wd):
                    if b >> (7 - rx) & 1:
                        fb.pixel(cx + rx, y + ry, fg)
        else:
            for ry in range(ht):
                base = off + ry * rb
                for rx in range(wd):
                    if data[base + (rx >> 3)] >> (7 - (rx & 7)) & 1:
                        fb.pixel(cx + rx, y + ry, fg)
        cx += wd
    return cx - x


def _fb_hanzi(fb, font, s, x, y, fg, bg):
    """font2bitmap 比例中文字体 (MAP/WIDTHS/OFFSETS/BITMAPS) 绘入离屏画布"""
    mapping = font.MAP
    widths = font.WIDTHS
    offsets = font.OFFSETS
    bits = font.BITMAPS
    ow = getattr(font, "OFFSET_WIDTH", 2)
    bpp = getattr(font, "BPP", 1)
    ht = font.HEIGHT
    blen = len(bits)
    cx = x
    for ch in str(s):
        idx = mapping.find(ch)
        if idx < 0:
            cx += 12
            continue
        w = widths[idx]
        ob = int.from_bytes(offsets[idx * ow:(idx + 1) * ow], "big")
        if bg is not None:
            fb.fill_rect(cx, y, w, ht, bg)
        nbits = w * ht * bpp
        end = (ob + nbits + 7) // 8
        for r in range(ht):
            base = ob + r * w * bpp
            for c2 in range(w):
                bp = base + c2 * bpp
                bp3 = bp >> 3
                if bp3 < blen and bp3 <= end and \
                        bits[bp3] >> (7 - (bp & 7)) & 1:
                    fb.pixel(cx + c2, y + r, fg)
        cx += w
    return cx - x


def _fb_hanzi_width(font, s):
    w = 0
    for ch in str(s):
        idx = font.MAP.find(ch)
        w += font.WIDTHS[idx] if idx >= 0 else 12
    return w


def _fb_center_text(fb, font, s, y, fg, bg, hanzi=False):
    """水平居中: ASCII 用定宽字体, 中文用比例字体"""
    s = str(s)
    is_hz = hanzi or any(ord(c) > 127 for c in s)
    if is_hz:
        tw = _fb_hanzi_width(font, s)
        _fb_hanzi(fb, font, s, (W - tw) // 2, y, fg, bg)
    else:
        tw = len(s) * font.WIDTH
        _fb_text(fb, font, s, (W - tw) // 2, y, fg, bg)


class _Canvas(object):
    """整屏离屏画布: 大端 RGB565 字节流, 单次 blit_buffer 上屏"""

    def __init__(self):
        self.buf = bytearray(W * H * 2)
        self.fb = framebuf.FrameBuffer(self.buf, W, H, framebuf.RGB565)


class StockMode(BaseMode):
    MODE_ID = "stock"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._idx = 0
        self._stocks = []             # mooview 持仓 (含成本/股数)
        self._quotes = {}             # 腾讯实时行情 {归一裸码: quote}
        self._quotes_ms = 0           # 最近一次行情发起/成功时刻
        self._quote_try_bak = False   # 本刷新周期内已试过备用域名
        self._quote_blocking = False  # 非阻塞链路失败过 → 后续直接走阻塞兜底
        self._quote_err = ""          # 最近一次行情失败原因 (诊断上屏)
        self._fetch_error = False     # 持仓读取失败
        self._net_err = ""            # 最近一次网络失败原因 (诊断上屏, ASCII)
        self._holdings_loading = True
        self._page = SCENE_SIMPLE
        self._klines = {}             # code -> {bars, fetched_ms, forecast_start}
        self._kline_pending = set()   # 在途/排队 K 线请求
        self._kline_loading = False
        self._kline_load_ms = 0
        self._legacy_tried = False        # user 请求阻塞兜底: 每次进入模式限一次
        self._kline_legacy = set()        # 已走过阻塞兜底的 code
        self._last_page_ms = 0
        # 非阻塞请求队列
        self._queue = []
        self._req = None
        self._req_kind = None
        # 渲染
        self._cv = None               # 当前屏幕画布 (与屏幕同步)
        self._cv_n = None             # 过场: 新场景画布
        self._cv_w = None             # 过场: 合成画布
        self._trans_t0 = None
        self._dirty = True
        self._btn_fb = None           # (idx, until_ms) 按钮反馈

    # ============================================================
    # enter / loop / exit
    # ============================================================
    def enter(self):
        self._ensure_canvas()
        self._idx = 0
        self._stocks = []
        self._fetch_error = False
        self._net_err = ""
        self._holdings_loading = True
        self._page = SCENE_SIMPLE
        self._kline_loading = False
        self._legacy_tried = False
        self._kline_legacy = set()
        self._quote_try_bak = False
        self._quote_blocking = False
        self._quote_err = ""
        self._last_page_ms = time.ticks_ms()
        self._trans_t0 = None
        self._btn_fb = None
        self._queue_user()
        self._render_blit()

    def loop(self):
        up, sel, down = self._scan_with_timing()[:3]

        # 1) 网络状态机推进 (不阻塞帧)
        self._pump()
        # 2) 周期任务: 行情 10s(交易时段) / 自动翻页 120s (持仓只在 enter 拉一次)
        self._timers()
        # 3) 按键 (非阻塞反馈)
        if up:
            if self._stocks:
                self._press_feedback(0)
                self._switch_stock()
        if sel:
            if self._stocks:
                self._press_feedback(1)
                self._switch_scene((self._page % 3) + 1)
            elif not self._holdings_loading:
                # 空态/读取失败页: B = 重新拉取持仓 (不再周期刷新后的手动重试)
                self._press_feedback(1)
                self._retry_holdings()
        if down:
            self._press_feedback(2)
            self.flash_exit()
            return False
        # 4) 按钮反馈恢复 / 过场 / 重绘
        self._tick_feedback()
        if self._trans_t0 is not None:
            self._advance_trans()
        elif self._dirty:
            self._render_blit()
        self._draw_strip()
        time.sleep_ms(40)
        return True

    def exit(self):
        self._stocks = []
        self._quotes = {}
        self._klines = None
        self._queue = []
        if self._req is not None:
            self._req.close()
            self._req = None
        self._cv = None
        self._cv_n = None
        self._cv_w = None
        gc.collect()

    # ============================================================
    # 网络层: 队列 + 状态机泵
    # ============================================================
    def _cfg(self):
        try:
            return get_stock_config()
        except Exception:
            return {"endpoint": _DEFAULT_ENDPOINT, "api_key": ""}

    def _enqueue(self, kind, url, timeout_ms=8000, headers=None):
        self._queue.append({"k": kind, "u": url, "t": timeout_ms, "h": headers})

    def _user_url(self):
        cfg = self._cfg()
        ep = (cfg.get("endpoint", _DEFAULT_ENDPOINT) or _DEFAULT_ENDPOINT).rstrip("/")
        key = (cfg.get("api_key", "") or "").strip()
        return ep + "/?" + "action=user&api_key=" + key

    def _queue_user(self):
        cfg = self._cfg()
        if not (cfg.get("api_key", "") or "").strip():
            self._holdings_loading = False
            self._fetch_error = True
            self._net_err = "NO API KEY"
            print("[STOCK] no api_key (settings.json -> stock)")
            self._dirty = True
            return
        self._enqueue("user", self._user_url(), 8000)

    def _retry_holdings(self):
        """空态/读取失败页 B 键: 重新拉取一次持仓列表"""
        self._fetch_error = False
        self._net_err = ""
        self._holdings_loading = True
        self._dirty = True
        self._queue_user()

    def _queue_quotes(self, mode="primary"):
        """入队一批行情请求.
        mode: primary=主域名非阻塞 / backup=备用域名非阻塞 /
              legacy=urequests 阻塞兜底 (非阻塞链路真机失败时的自愈路径)
        """
        codes = []
        for s in self._stocks:
            c = s["code"]
            if c and c not in codes:
                codes.append(c)
        if not codes:
            return
        for i in range(0, len(codes), 20):
            chunk = codes[i:i + 20]
            n = i // 20
            # 只有 backup 模式换备用域名; legacy 是"同一 URL 换读取方式",
            # 因为走到那一步时两个域名已经都失败过了
            bak = (mode == "backup")
            if mode == "legacy":
                kind = "quote_legacy:%d" % n
            else:
                kind = ("quote_bak:%d" if bak else "quote:%d") % n
            self._enqueue(kind, sq.build_url(chunk, backup=bak), 6000,
                          sq.quote_headers())

    def _kline_url(self, code):
        cfg = self._cfg()
        ep = (cfg.get("endpoint", _DEFAULT_ENDPOINT) or _DEFAULT_ENDPOINT).rstrip("/")
        key = (cfg.get("api_key", "") or "").strip()
        return ep + "/?" + "action=kxian&code=" + code + "&api_key=" + key

    def _queue_kline(self, code):
        if code in self._kline_pending:
            return
        cfg = self._cfg()
        key = (cfg.get("api_key", "") or "").strip()
        if not key or not code:
            return
        self._kline_pending.add(code)
        self._enqueue("kxian:" + code, self._kline_url(code), 9000)

    def _pump(self):
        """每帧推进: 取队首建连, tick 当前请求, 完成后分发"""
        if self._req is None and self._queue:
            item = self._queue.pop(0)
            kind = item["k"]
            if kind == "user_legacy":
                # 非阻塞路径 json 失败时的一次性兜底: v1.4 同款 urequests
                status, data = http_nb.get_json_blocking(
                    item["u"], None, item.get("t", 8000))
                self._on_done("user", status, data, None)
                return
            if kind.startswith("kxian_legacy:"):
                code = kind.split(":", 1)[1]
                self._kline_pending.discard(code)
                status, data = http_nb.get_json_blocking(
                    item["u"], None, item.get("t", 9000))
                self._apply_kline(code, status, None, data)
                return
            if kind.startswith("quote_legacy:"):
                # 行情非阻塞链路失败后的一次性兜底: urequests 阻塞取原始文本
                # (腾讯返回 v_xxx="..." 非 JSON, 故用 get_text_blocking)
                status, body = http_nb.get_text_blocking(
                    item["u"], item.get("h"), item.get("t", 6000))
                self._on_done(kind, status, None, body)
                return
            if item["u"].startswith("https://"):
                # TLS 无法分步, 退化为一次性阻塞请求 (罕见配置)
                status, data = http_nb.get_json_blocking(
                    item["u"], item.get("h"), item.get("t", 8000))
                self._on_done(kind, status, data if data else None, None)
                return
            self._req = http_nb.Request(item["u"], headers=item.get("h"),
                                        timeout_ms=item.get("t", 8000))
            self._req_kind = item["k"]
            self._req.start()
        if self._req is None:
            return
        st = self._req.tick()
        if st == http_nb.RUN:
            return
        kind = self._req_kind
        if st == http_nb.DONE:
            status = self._req.status
            body = self._req.body()
            self._req.close()
            self._req = None
            self._req_kind = None
            self._on_done(kind, status, None, body)
        elif st == http_nb.FAIL:
            err = self._req.error or "FAIL"
            self._req.close()
            self._req = None
            self._req_kind = None
            self._on_fail(kind, err)

    def _on_done(self, kind, status, data, body):
        try:
            if kind == "user":
                payload = data
                if payload is None and status == 200 and body:
                    try:
                        payload = sq.json_loads(body)
                    except Exception as e:
                        payload = None
                        print("[STOCK] user json err:", e,
                              "len", len(body),
                              "tail", repr(body[-80:]))
                if status != 200:
                    self._net_err = "HTTP %d" % status
                    print("[STOCK] user HTTP", status,
                          repr((body or b"")[:120]))
                elif payload is None:
                    self._net_err = "BAD JSON"
                    print("[STOCK] user bad json:", repr((body or b"")[:120]))
                    if body and not self._legacy_tried:
                        # 非阻塞读取路径失败 → v1.4 同款 urequests 兜底一次
                        self._legacy_tried = True
                        print("[STOCK] fallback to blocking urequests")
                        self._enqueue("user_legacy", self._user_url(), 8000)
                self._apply_holdings(payload)
            elif kind.startswith("kxian:"):
                code = kind.split(":", 1)[1]
                self._kline_pending.discard(code)
                self._apply_kline(code, status, body)
            elif kind.startswith("quote"):
                if status != 200:
                    # 非 200（重定向页/错误页）与连接失败同等对待, 走同一条降级链
                    print("[STOCK] quote HTTP", status,
                          repr((body or b"")[:80]))
                    return self._on_fail(kind, "HTTP %d" % status)
                n = self._apply_quotes(body)
                if n:
                    if kind.startswith("quote_legacy"):
                        # 阻塞兜底可用 → 后续周期直接走阻塞, 不再空等非阻塞超时
                        self._quote_blocking = True
                        print("[STOCK] quote via blocking fallback OK:", n)
                elif not self._quote_err:
                    self._quote_err = "NO DATA"
                    print("[STOCK] quote parse empty: len", len(body or b""),
                          "head", repr((body or b"")[:100]),
                          "tail", repr((body or b"")[-60:]))
        except Exception as e:
            print("[STOCK] on_done:", e)

    def _on_fail(self, kind, err=""):
        try:
            if kind == "user":
                self._holdings_loading = False
                self._fetch_error = True
                self._net_err = (err or "FAIL")[:28]
                print("[STOCK] user FAIL:", err)
                if not self._legacy_tried:
                    # 截断/超时等读取失败 → v1.4 同款 urequests 兜底一次
                    self._legacy_tried = True
                    print("[STOCK] user fallback (urequests)")
                    self._enqueue("user_legacy", self._user_url(), 8000)
                    return  # 屏幕保持当前态, 等兜底结果
                self._dirty = True
            elif kind.startswith("kxian:"):
                code = kind.split(":", 1)[1]
                self._kline_pending.discard(code)
                self._kline_loading = False
                if code not in self._kline_legacy:
                    self._kline_legacy.add(code)
                    print("[STOCK] kxian fallback (urequests):", code, err)
                    self._kline_pending.add(code)
                    self._enqueue("kxian_legacy:" + code,
                                  self._kline_url(code), 9000)
                    return
                self._klines[code] = {"bars": None, "fetched_ms": time.ticks_ms(),
                                      "forecast_start": 0}
                self._dirty = True
            elif kind.startswith("quote"):
                self._quote_err = (err or "FAIL")[:28]
                print("[STOCK] quote FAIL:", err,
                      "legacy" if kind.startswith("quote_legacy")
                      else ("bak" if self._quote_try_bak else "primary"))
                if kind.startswith("quote_legacy"):
                    self._quote_blocking = False  # 兜底也不通, 下周期回到非阻塞
                    self._dirty = True
                elif not self._quote_try_bak:
                    # 换备用域名重试一次 (每个刷新周期给主/备域名各一次机会)
                    self._quote_try_bak = True
                    self._queue_quotes("backup")
                elif not self._quote_pending():
                    # 主/备都失败 → urequests 阻塞兜底 (与 user/kxian 同款自愈)
                    print("[STOCK] quote fallback (urequests)")
                    self._queue_quotes("legacy")
                else:
                    self._dirty = True
        except Exception as e:
            print("[STOCK] on_fail:", e)

    # ---------- 数据落地 ----------
    def _apply_holdings(self, data):
        self._holdings_loading = False
        out = []
        if isinstance(data, dict):
            body = data.get("data", {}) or {}
            raw = body.get("holdings", []) or []
            for h in raw:
                if not isinstance(h, dict):
                    continue
                try:
                    cost = float(h.get("cost_price", 0) or 0)
                    price = float(h.get("market_price", 0) or 0)
                    qty = int(h.get("quantity", 0) or 0)
                except Exception:
                    continue
                code = sq.norm_code(h.get("stock_code", ""))
                if not code:
                    # 无可用代码 → 行情/K线都组不出请求, 持仓仍上屏但注明
                    print("[STOCK] holding without usable code:",
                          repr(h.get("stock_code")),
                          repr(h.get("stock_name")))
                out.append({
                    "name": str(h.get("stock_name", "") or ""),
                    "code": code,
                    "shares": qty, "cost": cost, "price": price,
                    "profit": float(h.get("profit", 0) or 0) or (price - cost) * qty,
                    "profit_pct": float(h.get("profit_pct", 0) or 0) or (
                        (price - cost) / cost * 100 if cost > 0 else 0),
                })
        self._fetch_error = not (isinstance(data, dict)
                                 and bool(data.get("success")))
        if not self._fetch_error:
            self._net_err = ""
        self._stocks = out
        if self._idx >= len(self._stocks):
            self._idx = 0
        self._last_page_ms = time.ticks_ms()   # 自动翻页计时从持仓落地起算
        # 持仓变化后立刻拉一轮实时行情 (时间戳前移, 规避 ticks 回绕语义)
        self._quotes_ms = time.ticks_add(time.ticks_ms(),
                                         -_QUOTE_MS_TRADING - 1)
        if self._stocks:
            self._queue_quotes()
        self._dirty = True

    def _apply_quotes(self, body):
        """行情落地 → 解析成功的条数 (0 表示无有效标的, 由调用方记诊断)"""
        got = sq.parse_body(body or b"")
        if got:
            self._quotes.update(got)
            self._quotes_ms = time.ticks_ms()
            self._quote_err = ""
            self._dirty = True   # 现价/盈亏变化, 场景重绘 (离屏整页换帧, 零闪烁)
        return len(got)

    def _apply_kline(self, code, status, body, data=None):
        bars = None
        forecast_start = 0
        if status == 200 and (body or data is not None):
            try:
                if data is None:
                    data = sq.json_loads(body)
                bd = (data.get("data", {}) or {}) if isinstance(data, dict) else {}
                hist = bd.get("history", []) or []
                fcst = bd.get("forecast", []) or []
                lst = []
                for src in (hist, fcst):
                    for b in src:
                        if not isinstance(b, dict):
                            continue
                        try:
                            lst.append({
                                "trade_date": str(b.get("trade_date", "")),
                                "open": float(b.get("open", 0) or 0),
                                "close": float(b.get("close", 0) or 0),
                                "high": float(b.get("high", 0) or 0),
                                "low": float(b.get("low", 0) or 0),
                                "volume": int(float(b.get("volume", 0) or 0)),
                            })
                        except Exception:
                            continue
                if lst:
                    bars = lst
                    forecast_start = len(hist)
            except Exception as e:
                print("[STOCK] kxian parse:", e,
                      "len", len(body or b""),
                      "tail", repr((body or b"")[-80:]))
                if body and code not in self._kline_legacy:
                    # 非阻塞读取路径失败 → v1.4 同款 urequests 兜底一次
                    self._kline_legacy.add(code)
                    print("[STOCK] kxian fallback (urequests):", code)
                    self._kline_pending.add(code)
                    self._enqueue("kxian_legacy:" + code,
                                  self._kline_url(code), 9000)
                    return  # 不落地失败缓存, 等兜底结果
        self._klines[code] = {"bars": bars, "fetched_ms": time.ticks_ms(),
                              "forecast_start": forecast_start if bars else 0}
        if code == self._cur_code():
            self._kline_loading = False
        self._dirty = True

    # ---------- 合并视图 (腾讯实时价优先, 回退 mooview) ----------
    def _cur(self):
        if self._stocks and self._idx < len(self._stocks):
            return self._stocks[self._idx]
        return None

    def _cur_code(self):
        s = self._cur()
        return s["code"] if s else ""

    def _view(self, s):
        """持仓 + 实时行情 → 显示用合并视图"""
        q = self._quotes.get(s["code"])
        if q:
            price = q["price"]
            prev = q["prev_close"]
            pl = (price - s["cost"]) * s["shares"]
            hold_pct = (pl / (s["cost"] * s["shares"]) * 100
                        if s["cost"] > 0 and s["shares"] > 0 else 0)
            return {
                "price": price, "chg": q["change"], "pct": q["change_pct"],
                "pl": pl, "pl_share": price - s["cost"], "hold_pct": hold_pct,
                "src": "TX", "ts": sq.hhmm(q.get("ts", "")),
                "prev": prev, "q": q,
                "day_dir": 1 if price >= prev > 0 else (-1 if prev > 0 else 0),
            }
        price = s["price"]
        return {
            "price": price, "chg": price - s["cost"], "pct": s["profit_pct"],
            "pl": s["profit"], "pl_share": price - s["cost"],
            "hold_pct": s["profit_pct"], "src": "MV", "ts": "",
            "prev": 0, "q": None,
            "day_dir": 1 if s["profit"] > 0 else (-1 if s["profit"] < 0 else 0),
        }

    # ============================================================
    # 周期任务
    # ============================================================
    @staticmethod
    def _in_trading_window():
        """交易时段判定 (委托 stock_quote, 供测试与场景1 CLOSED 标记共用)"""
        return sq.in_trading_window()

    def _timers(self):
        now = time.ticks_ms()
        # 持仓列表只在 enter() 拉一次 (失败由空态 B 键手动重试), 不再周期刷新
        interval = _QUOTE_MS_TRADING if self._in_trading_window() \
            else _QUOTE_MS_IDLE
        if self._stocks and self._req is None and \
                time.ticks_diff(now, self._quotes_ms) >= interval and \
                not self._quote_pending():
            self._quotes_ms = now
            self._quote_try_bak = False   # 新周期: 主域名重新获得一次机会
            self._queue_quotes("legacy" if self._quote_blocking else "primary")
        # 自动翻页
        if self._stocks and self._trans_t0 is None:
            if time.ticks_diff(now, self._last_page_ms) >= _AUTO_PAGE_MS:
                self._last_page_ms = now
                self._switch_stock()

    def _quote_pending(self):
        for it in self._queue:
            if it["k"].startswith("quote"):
                return True
        return self._req is not None and self._req_kind.startswith("quote")

    # ============================================================
    # 场景/股票切换 (滑动过场)
    # ============================================================
    def _ensure_canvas(self):
        if self._cv is None:
            self._cv = _Canvas()
        if self._cv_n is None:
            self._cv_n = _Canvas()
        if self._cv_w is None:
            self._cv_w = _Canvas()

    def _press_feedback(self, idx):
        self._btn_fb = (idx, time.ticks_add(time.ticks_ms(), _BTN_FB_MS))
        self._draw_btn_direct(idx, True)
        self.beep(900, 15)

    def _tick_feedback(self):
        if self._btn_fb is not None:
            if time.ticks_diff(self._btn_fb[1], time.ticks_ms()) <= 0:
                y = BTN_Y0
                mv = memoryview(self._cv.buf)[y * W * 2:(y + BTN_H) * W * 2]
                self.display.blit_buffer(mv, 0, y, W, BTN_H)
                self._btn_fb = None

    def _draw_btn_direct(self, idx, pressed):
        """按钮反馈直接绘屏 (几毫秒, 不走画布重渲)"""
        d = self.display
        bx = 5 + idx * (BTN_W + 5)
        bw = BTN_W + 1 if idx == 2 else BTN_W
        if pressed:
            d.fill_rect(bx, BTN_Y0, bw, BTN_H, CLR_STK_TAG)
        else:
            d.fill_rect(bx, BTN_Y0, bw, BTN_H, CLR_STK_BG)
            d.rect(bx, BTN_Y0, bw, BTN_H, CLR_STK_TXT_DIM)
        label = self._labels()[idx]
        tw = _fb_hanzi_width(hanzi24, label)
        tx = bx + (bw - tw) // 2
        ty = BTN_Y0 + (BTN_H - hanzi24.HEIGHT) // 2
        if pressed:
            d.write(hanzi24, label, tx, ty, CLR_WHITE, CLR_STK_TAG)
        else:
            d.write(hanzi24, label, tx, ty, CLR_STK_TXT_HI, CLR_STK_BG)

    def _switch_scene(self, scene):
        if scene == self._page:
            return
        if scene == SCENE_KLINE:
            self._ensure_kline(self._cur_code())
        self._page = scene
        self._start_trans()

    def _switch_stock(self):
        n = len(self._stocks)
        if not n:
            return
        self._idx = (self._idx + 1) % n
        self._last_page_ms = time.ticks_ms()
        if self._page == SCENE_KLINE:
            self._ensure_kline(self._cur_code())
        self._start_trans()

    def _start_trans(self):
        self._ensure_canvas()
        self._render_into(self._cv_n.fb)
        self._trans_t0 = time.ticks_ms()

    def _advance_trans(self):
        elapsed = time.ticks_diff(time.ticks_ms(), self._trans_t0)
        p = elapsed / _TRANS_MS
        if p >= 1.0:
            self._cv, self._cv_n = self._cv_n, self._cv
            self._trans_t0 = None
            self.display.blit_buffer(memoryview(self._cv.buf), 0, 0, W, H)
            return
        off = int(W * p)
        wfb = self._cv_w.fb
        wfb.blit(self._cv.fb, -off, 0)
        wfb.blit(self._cv_n.fb, W - off, 0)
        self.display.blit_buffer(memoryview(self._cv_w.buf), 0, 0, W, H)

    def _render_blit(self):
        self._render_into(self._cv.fb)
        self.display.blit_buffer(memoryview(self._cv.buf), 0, 0, W, H)
        self._dirty = False

    # ============================================================
    # K 线数据
    # ============================================================
    def _ensure_kline(self, code):
        c = self._klines.get(code) if self._klines else None
        if c and c["bars"] and \
                time.ticks_diff(time.ticks_ms(), c["fetched_ms"]) < _KLINE_TTL_MS:
            self._kline_loading = False
            return
        if code in self._kline_pending:
            self._kline_loading = True
            self._kline_load_ms = time.ticks_ms()
            return
        self._kline_loading = True
        self._kline_load_ms = time.ticks_ms()
        self._queue_kline(code)

    # ============================================================
    # 场景渲染 (整页 → 离屏画布)
    # ============================================================
    def _render_into(self, fb):
        fb.fill(C_BG)
        self._draw_header(fb)
        s = self._cur()
        if s is None:
            if self._fetch_error:
                _fb_center_text(fb, hanzi24, "网络中断", 92, C_TXT_HI, C_BG, True)
                _fb_center_text(fb, vga8,
                                (self._net_err or "check wifi / api")[:28],
                                126, C_TXT_DIM, C_BG)
            elif self._holdings_loading:
                _fb_center_text(fb, hanzi24, "加载中", 96, C_TXT_HI, C_BG, True)
                _fb_center_text(fb, vga8, "holdings...", 128, C_TXT_DIM, C_BG)
            else:
                _fb_center_text(fb, hanzi24, "暂无持仓", 96, C_TXT_DIM, C_BG, True)
            self._draw_buttons(fb)
            return
        if self._page == SCENE_SIMPLE:
            self._draw_scene1(fb, s)
        elif self._page == SCENE_DETAIL:
            self._draw_scene2(fb, s)
        else:
            self._draw_scene3(fb, s)
        self._draw_buttons(fb)

    def _draw_header(self, fb):
        s = self._cur()
        # 通栏底色随当日涨跌: 涨红 / 跌蓝 / 无数据默认红
        bg = C_HDR
        if s is not None and self._view(s)["day_dir"] < 0:
            bg = C_DN
        fb.fill_rect(0, 0, W, HEADER_H, bg)
        name = (s.get("name", "") or s.get("code", "") or "持仓") if s else "持仓"
        _fb_hanzi(fb, hanzi24, name, 6, 8, C_TXT_HI, bg)
        code = s.get("code", "") if s else ""
        if code:
            # 代码紧跟名称, 与名称同尺寸大字
            _fb_hanzi(fb, hanzi24, code,
                      6 + _fb_hanzi_width(hanzi24, name) + 8, 8, C_TXT_HI, bg)
        ind = "%d/3" % self._page
        _fb_text(fb, vga8, ind, W - 6 - len(ind) * 8, 15, C_TXT_HI, bg)

    def _labels(self):
        """当前应显示的三个按钮标签 (空态/错误态中间键变'重试')"""
        if not self._stocks:
            return _BTNS_EMPTY
        return _SCENE_BTNS[self._page]

    def _draw_buttons(self, fb):
        labels = self._labels()
        fb.fill_rect(0, BTN_Y0, W, BTN_H + 1, C_BG)
        gap = 5
        for i, label in enumerate(labels):
            bx = 5 + i * (BTN_W + gap)
            bw = BTN_W + 1 if i == 2 else BTN_W
            fb.fill_rect(bx, BTN_Y0, bw, BTN_H, C_BTN_BG)
            fb.rect(bx, BTN_Y0, bw, BTN_H, C_TXT_DIM)
            tw = _fb_hanzi_width(hanzi24, label)
            tx = bx + (bw - tw) // 2
            ty = BTN_Y0 + (BTN_H - hanzi24.HEIGHT) // 2
            _fb_hanzi(fb, hanzi24, label, tx, ty, C_BG, C_BTN_BG)

    # ---------- 场景1 简易 ----------
    def _draw_scene1(self, fb, s):
        v = self._view(s)
        # 双焦点 1: 现价大字
        price_c = C_UP if v["day_dir"] > 0 else (C_DN if v["day_dir"] < 0 else C_TXT_HI)
        _fb_text(fb, self._big_font(), "%.2f" % v["price"],
                 10, S1_PRICE_Y, price_c, C_BG)
        # 双焦点 2: 涨跌幅徽章
        if v["day_dir"] >= 0:
            bc, pc = C_UP, "+"
        else:
            bc, pc = C_DN, "-"
        fb.fill_rect(S1_BADGE_X, S1_BADGE_Y, S1_BADGE_W, S1_BADGE_H, bc)
        pct = pc + "%.2f%%" % v["pct"]
        _fb_text(fb, vga8, pct,
                 S1_BADGE_X + (S1_BADGE_W - len(pct) * 8) // 2,
                 S1_BADGE_Y + 2, C_TXT_HI, None)
        chg = pc + "%.2f" % abs(v["chg"])
        _fb_text(fb, vga8, chg,
                 S1_BADGE_X + (S1_BADGE_W - len(chg) * 8) // 2,
                 S1_BADGE_Y + 19, C_TXT_HI, None)
        # 成本 / 每股盈亏
        pl_c = C_GAIN if v["pl"] >= 0 else C_LOSS
        _fb_text(fb, vga8, "COST", 10, S1_COST_Y, C_TXT_DIM, C_BG)
        _fb_text(fb, vga8, "%.2f" % s["cost"], 50, S1_COST_Y, C_TXT_HI, C_BG)
        plab = "+P/L" if v["pl"] >= 0 else "-P/L"
        _fb_text(fb, vga8, plab, 130, S1_COST_Y, C_TXT_DIM, C_BG)
        _fb_text(fb, vga8, "%.2f" % abs(v["pl_share"]),
                 170, S1_COST_Y, pl_c, C_BG)
        # 成本↔现价 对比条
        self._draw_range_bar(fb, s["cost"], v["price"], pl_c)
        # Pos / 总盈亏
        _fb_text(fb, vga8, "POS", 10, S1_POS_Y, C_TXT_DIM, C_BG)
        _fb_text(fb, vga8, "%d" % s["shares"], 42, S1_POS_Y, C_TXT_HI, C_BG)
        tlab = "+P&L" if v["pl"] >= 0 else "-P&L"
        _fb_text(fb, vga8, tlab, 130, S1_POS_Y, C_TXT_DIM, C_BG)
        tval = "%.2f" % abs(v["pl"])
        _fb_text(fb, vga8, tval, W - 10 - len(tval) * 8, S1_POS_Y, pl_c, C_BG)
        # 行情来源 + 时间戳
        self._draw_ts_row(fb, v)

    def _draw_range_bar(self, fb, cost, price, color):
        lo = min(cost, price)
        hi = max(cost, price)
        pad = (hi - lo) * 0.5 + hi * 0.001 + 0.01
        a, b = lo - pad, hi + pad
        span = b - a
        fb.fill_rect(10, S1_BAR_Y, W - 20, S1_BAR_H, C_GRID)
        cx = 10 + int((W - 20) * (cost - a) / span)
        px = 10 + int((W - 20) * (price - a) / span)
        fb.fill_rect(min(cx, px), S1_BAR_Y, abs(px - cx) + 1, S1_BAR_H, color)
        # 成本标记(白) / 现价标记(色)
        fb.fill_rect(cx - 1, S1_BAR_Y - 4, 2, S1_BAR_H + 8, C_TXT_HI)
        fb.fill_rect(px - 1, S1_BAR_Y - 4, 2, S1_BAR_H + 8, color)
        _fb_text(fb, vga8, "%.2f" % a, 10, S1_BAR_Y + 8, C_TXT_DIM, C_BG)
        t = "%.2f" % b
        _fb_text(fb, vga8, t, W - 10 - len(t) * 8, S1_BAR_Y + 8, C_TXT_DIM, C_BG)

    def _draw_ts_row(self, fb, v):
        tag = v["src"] + " " + (v["ts"] if v["ts"] else "--:--")
        _fb_text(fb, vga8, tag, 10, S1_TS_Y, C_TXT_DIM, C_BG)
        if not self._in_trading_window():
            _fb_text(fb, vga8, "CLOSED", W - 10 - 6 * 8, S1_TS_Y, C_TXT_DIM, C_BG)

    def _big_font(self):
        try:
            from font import vga1_16x32
            return vga1_16x32
        except ImportError:
            return vga8

    # ---------- 场景2 明细 ----------
    def _draw_scene2(self, fb, s):
        q = self._quotes.get(s["code"])
        if not q:
            _fb_center_text(fb, hanzi24, "暂无数据", 90, C_TXT_DIM, C_BG, True)
            # 带上失败原因 (timeout/dns:/HTTP xxx/NO DATA), 现场自诊断
            msg = ("NO QUOTE: " + self._quote_err) if self._quote_err \
                else "NO QUOTE (MV fallback)"
            _fb_center_text(fb, vga8, msg[:28], 124, C_TXT_DIM, C_BG)
            return
        prev = q["prev_close"]

        def oc(v):
            if prev <= 0:
                return C_TXT_HI
            return C_UP if v >= prev else C_DN

        def f2(v):
            return "%.2f" % v

        vol_w = q["volume"] / 10000.0
        amt_y = q["amount"] / 10000.0  # 万元 → 亿
        rows = (
            ("OPEN", f2(q["open"]), oc(q["open"])),
            ("VOL(W)", "%.1f" % vol_w, C_TXT_HI),
            ("HIGH", f2(q["high"]), oc(q["high"])),
            ("AMT(Y)", "%.2f" % amt_y, C_TXT_HI),
            ("LOW", f2(q["low"]), oc(q["low"])),
            ("TURN%", "%.2f" % q["turnover"], C_TXT_HI),
            ("PREV", f2(prev), C_TXT_HI),
            ("RNG%", "%.2f" % q["amplitude"], C_TXT_HI),
            ("P/E", "%.1f" % q["pe"], C_TXT_HI),
            ("P/B", "%.2f" % q["pb"], C_TXT_HI),
            ("MCAP(Y)", "%.0f" % q["mktcap"], C_TXT_HI),
            ("FLOAT(Y)", "%.0f" % q["floatcap"], C_TXT_HI),
        )
        y = S2_Y0
        for i in range(6):
            yy = y + i * S2_ROW_H
            for col in (0, 1):
                lab, val, c = rows[i * 2 + col]
                _fb_text(fb, vga8, lab, S2_LBL_X[col], yy, C_TXT_DIM, C_BG)
                _fb_text(fb, vga8, val, S2_VAL_R[col] - len(val) * 8, yy, c, C_BG)
        # 涨跌停 + 时间戳 (短格式避免与时间戳重叠)
        lim = "%.0f~%.0f" % (q["limit_dn"], q["limit_up"])
        _fb_text(fb, vga8, "LIM", 10, S2_LIM_Y, C_TXT_DIM, C_BG)
        _fb_text(fb, vga8, lim, 42, S2_LIM_Y, C_TXT_DIM, C_BG)
        ts = sq.hhmm(q.get("ts", "")) or "--:--"
        _fb_text(fb, vga8, "TX " + ts, W - 10 - (3 + len(ts)) * 8, S2_TS_Y,
                 C_TXT_DIM, C_BG)

    # ---------- 场景3 预测K线 ----------
    def _draw_scene3(self, fb, s):
        code = s["code"]
        c = self._klines.get(code) if self._klines else None
        if self._kline_loading and (not c or not c["bars"]):
            self._draw_kline_loading(fb)
            return
        if not c or not c["bars"]:
            _fb_center_text(fb, hanzi24, "暂无数据", 90, C_TXT_DIM, C_BG, True)
            _fb_center_text(fb, vga8, "Press B back", 124, C_TXT_DIM, C_BG)
            return
        self._draw_klines(fb, c["bars"], c["forecast_start"])

    def _draw_kline_loading(self, fb):
        _fb_center_text(fb, hanzi24, "加载中", 84, C_TXT_HI, C_BG, True)
        _fb_center_text(fb, hanzi24, self._cur().get("name", ""), 118,
                        C_TXT_DIM, C_BG)
        _fb_center_text(fb, vga8, "Please wait...", 146, C_TXT_DIM, C_BG)

    def _kline_bounds(self, bars):
        lo = min(b["low"] for b in bars)
        hi = max(b["high"] for b in bars)
        if hi == lo:
            hi = lo + 1
        span = hi - lo
        return lo - span * 0.05, hi + span * 0.05

    def _draw_klines(self, fb, bars, forecast_start):
        if len(bars) > MAX_BARS:
            # 截掉最左侧旧柱时, 预测起点同步左移 (否则分界线画进预测段中间)
            forecast_start = max(0, forecast_start - (len(bars) - MAX_BARS))
            bars = bars[-MAX_BARS:]
        lo, hi = self._kline_bounds(bars)
        span = hi - lo
        chart_h = CHART_Y1 - CHART_Y0

        def y_of(p):
            return int(CHART_Y1 - (p - lo) / span * chart_h)

        # 网格 (视觉分格, 不标价格)
        for i in range(1, 4):
            gy = CHART_Y0 + chart_h * i // 4
            for gx in range(CHART_X0, CHART_X1, 6):
                fb.pixel(gx, gy, C_GRID)
        # K 线柱
        x = CHART_X0 + BAR_W // 2
        vols = [b["volume"] for b in bars]
        vmax = max(vols) if vols else 1
        if vmax <= 0:
            vmax = 1
        vol_h = VOL_Y1 - VOL_Y0
        n = len(bars)
        for i, b in enumerate(bars):
            if i >= forecast_start:
                color = C_FCAST
                vc = C_FCAST
            else:
                color = C_UP if b["close"] >= b["open"] else C_DN
                vc = C_UP_DIM if b["close"] >= b["open"] else C_DN_DIM
            yh, yl = y_of(b["high"]), y_of(b["low"])
            yo, yc = y_of(b["open"]), y_of(b["close"])
            fb.vline(x, yh, max(1, yl - yh + 1), color)
            top, bh = min(yo, yc), max(1, abs(yo - yc) + 1)
            fb.fill_rect(x - BAR_W // 2, top, BAR_W, bh, color)
            # 成交量柱带
            vw = max(1, int(vols[i] / vmax * vol_h))
            fb.fill_rect(x - BAR_W // 2, VOL_Y1 - vw, BAR_W, vw, vc)
            x += BAR_PITCH
        # AI 预测分界虚线 (位于最后一条历史柱与首条预测柱的正中间)
        if 0 < forecast_start < n:
            fx = CHART_X0 + BAR_W // 2 + forecast_start * BAR_PITCH \
                - BAR_PITCH // 2
            for gy in range(CHART_Y0, VOL_Y1, 5):
                fb.pixel(fx, gy, C_FCAST)
                fb.pixel(fx, gy + 1, C_FCAST)
            _fb_text(fb, vga8, "AI", fx - 18, CHART_Y0, C_FCAST, C_BG)
            # 预测首点价格 ▼
            fc = bars[forecast_start]["close"]
            self._draw_price_tag(fb, fx, fc)
        # 最新价虚线 (腾讯实时价, 区间内才画), 现价牌挂在右端
        v = self._view(self._cur())
        lp = v["q"]["price"] if v["q"] else 0
        if lp > 0 and lo < lp < hi:
            ly = y_of(lp)
            lc = C_UP if v["day_dir"] >= 0 else C_DN
            for gx in range(CHART_X0, CHART_X1, 4):
                fb.pixel(gx, ly, lc)
            txt = "%.2f" % lp
            tw = len(txt) * 8
            ty = min(max(ly - 18, CHART_Y0), CHART_Y1 - 16)
            fb.fill_rect(CHART_X1 - tw - 2, ty - 2, tw + 4, 18, C_BG)
            _fb_text(fb, vga8, txt, CHART_X1 - tw, ty, lc, C_BG)
        # 日期轴 (y=183, 底边 199 与按钮区齐平不重叠)
        if bars:
            d0 = bars[0].get("trade_date", "")
            d1 = bars[-1].get("trade_date", "")
            s0 = self._short_date(d0)
            s1 = self._short_date(d1)
            _fb_text(fb, vga8, s0, CHART_X0, VOL_Y1 + 1, C_TXT_DIM, C_BG)
            _fb_text(fb, vga8, s1, CHART_X1 - len(s1) * 8, VOL_Y1 + 1,
                     C_TXT_DIM, C_BG)

    def _draw_price_tag(self, fb, cx, close):
        ty = CHART_Y0 + 2
        txt = "%.2f" % close
        tw = len(txt) * 8
        fb.fill_rect(cx - tw // 2 - 2, ty, tw + 4, 14, C_BG)
        _fb_text(fb, vga8, txt, cx - tw // 2, ty, C_TXT_HI, C_BG)
        # ▼ 倒三角
        fb.line(cx - 5, ty + 15, cx, ty + 21, C_TAG)
        fb.line(cx, ty + 21, cx + 5, ty + 15, C_TAG)
        fb.hline(cx - 5, ty + 15, 11, C_TAG)

    @staticmethod
    def _short_date(d):
        try:
            y, m, dd = d.split("-")
            return str(int(m)) + "." + str(int(dd))
        except Exception:
            return d[:5]

    # ============================================================
    # 进度条 (直接绘屏: 4px 细条, 逐帧重绘无闪烁感)
    # ============================================================
    def _draw_strip(self):
        if self._trans_t0 is not None:
            return
        d = self.display
        if self._page == SCENE_SIMPLE and self._stocks:
            elapsed = time.ticks_diff(time.ticks_ms(), self._last_page_ms)
            p = min(elapsed / _AUTO_PAGE_MS, 1.0)
            fill_w = int(W * p)
            d.fill_rect(0, STRIP_Y - 1, W, STRIP_H + 2, CLR_STK_BG)
            d.rect(0, STRIP_Y, W, STRIP_H, CLR_STK_PAGEDOT_OFF)
            if fill_w > 0:
                d.fill_rect(0, STRIP_Y, fill_w, STRIP_H, CLR_STK_PAGEDOT)
        elif self._page == SCENE_KLINE and self._kline_loading:
            elapsed = time.ticks_diff(time.ticks_ms(), self._kline_load_ms)
            p = min(elapsed / _LOADING_MS, 0.95)
            fill_w = int(W * p)
            d.fill_rect(0, STRIP_Y - 1, W, STRIP_H + 2, CLR_STK_BG)
            d.rect(0, STRIP_Y, W, STRIP_H, CLR_STK_PAGEDOT_OFF)
            if fill_w > 0:
                d.fill_rect(0, STRIP_Y, fill_w, STRIP_H, CLR_STK_PAGEDOT)
