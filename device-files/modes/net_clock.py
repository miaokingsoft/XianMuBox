"""
多功能终端框架 - 网络时钟 (赛博霓虹 · UX 重构版)
闲沐工坊 · 信息桌面摆件

== 页面结构 ==
  时钟页(默认) --A--> 天气页 --A--> 时钟页
  任意主页 --B--> 功能菜单 (秒表 / 定时器 / 闹钟 / 刷新天气 / 返回)
  C短按: 工具页/菜单 → 返回上一级, 主页 → 退出模式
  闹钟: 列表页(查看/快速开关); 触发/响铃由全局服务 framework.alarm
        在 BaseMode.run() 中处理 (任何模式运行中都会响);
        时间/重复规则在 Web 端配置, 设备端每秒沿读 settings 缓存热加载

== 统一按键 (全模块一致) ==
  A键: 翻页 / 下一项 / 加值 / 启停
  B键: 主操作 (菜单 / 确认 / 清零 / 下一位)
  C键: 返回上级 / 退出

== 统一视觉 ==
  每页: 顶部标题栏(标题 + WiFi 点) + 底部 [A]/[B]/[C] 提示栏
  时钟页: 数字雨 + 动态赛博边框 + 辉光大字 + 渐变秒针盘
  天气页: 大天气图标 + 温度 + 湿度/AQI/UV 徽章 + 月相 + 进度条

MicroPython 兼容: 无 f-string / 无 with / time.ticks_*
"""

import time
import gc
import math
import random
import struct
from machine import RTC

from .base import BaseMode, SUB_FONT, get_sub_font, get_big_font
from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_CARD, CLR_GREEN, CLR_RED, CLR_MAGENTA, CLR_ORANGE, CLR_AMBER, CLR_BLUE,
)
from framework.wifi import check_wifi
from framework import weather as wx_service
from framework import moon as moon_mod
from font import hanzi_24 as hanzi24

try:
    from config import W, H
except ImportError:
    W, H = 240, 240


# ===== 字体惰性加载 =====
_SY32 = None


def _get_sy32():
    global _SY32
    if _SY32 is not None:
        return _SY32
    try:
        from font import sy_32x32 as f
        _SY32 = f
    except ImportError:
        _SY32 = None
    return _SY32


# ============================================================
#  配色 / 布局常量
# ============================================================

NEON_CYAN = CLR_CYAN
NEON_MAGENTA = 0xF81F
NEON_GLOW = 0x03EF        # 暗青辉光 (大字光晕)
DIM_G = 0x03E0            # 雨滴尾迹中段
DARK_G = 0x0180           # 雨滴尾迹末端

CYBER_CORNER = 0xF81F     # 边框角标品红
CYBER_GLOW = 0x03FF       # 边框暗青辉光
_BORDER_COLORS = (CLR_CYAN, CLR_CYAN, CLR_GREEN, CLR_MAGENTA)

RAIN_ENABLED = True       # 数字雨开关 (低内存机型可改 False 省资源)

# -- 雨滴单元格 --
_CHAR_W = 8
_CHAR_H = 8
_RCOLS = W // _CHAR_W
_RROWS = H // _CHAR_H
_SPEED_POOL = (1, 1, 1, 2)
_SPEED_POOL2 = (1, 1, 2)

# -- 时钟页布局 --
CLK_FRAME = (4, 18, 232, 214)         # 动态边框 x,y,w,h (18..232)
CLK_X0 = (W - 164) // 2               # 大字左缘
CLK_Y0 = 116                          # 大字上缘 (116..153)
CLK_COLON_X = CLK_X0 + 74 + 8 - 3     # 冒号点左上 x
DIAL_CX = W // 2                      # 秒针盘圆心
DIAL_CY = 188
DIAL_R = 24

# -- 雨滴遮罩区 (x0,y0,x1,y1): 框外 / 信息行 / 星期 / 日期 / 大字 / 提示行 --
_RAIN_MASKS = (
    (0, 0, 240, 20),
    (0, 18, 240, 44),
    (56, 48, 184, 80),
    (40, 80, 200, 114),
    (30, 110, 210, 158),
    (0, 212, 240, 28),
)

# -- 定时器 --
TM_SET_Y0 = 70                        # 设置页数字上缘
TM_RUN_Y0 = 60                        # 运行页数字上缘
TM_DIAL_R = 40                        # 运行页进度盘半径

# -- 菜单 --
_MENU_ITEMS = (
    ("stopwatch", "秒表",   CLR_GREEN),
    ("timer",     "定时器", CLR_ORANGE),
    ("alarm",     "闹钟",   CLR_MAGENTA),
    ("wx_refresh", "刷新天气", CLR_CYAN),
    ("back",      "返回",   CLR_GRAY),
)
_MENU_COUNT = len(_MENU_ITEMS)

# -- 闹钟 --
ALARM_COUNT = 3                 # 与 framework.alarm.ALARM_COUNT 一致
ALARM_DAYS_LABEL = ("日", "一", "二", "三", "四", "五", "六")

# -- 星期 --
WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAYS_FULL = ("星期一", "星期二", "星期三", "星期四",
                 "星期五", "星期六", "星期日")

_CUM_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


# ============================================================
#  绘制辅助 (模块级)
# ============================================================

def _lerp565(c1, c2, t):
    """RGB565 两色线性插值, t ∈ [0,1]"""
    if t < 0:
        t = 0
    if t > 1:
        t = 1
    r1 = (c1 >> 11) & 0x1F
    g1 = (c1 >> 5) & 0x3F
    b1 = c1 & 0x1F
    r2 = (c2 >> 11) & 0x1F
    g2 = (c2 >> 5) & 0x3F
    b2 = c2 & 0x1F
    return (((r1 + int((r2 - r1) * t)) << 11) |
            ((g1 + int((g2 - g1) * t)) << 5) |
            (b1 + int((b2 - b1) * t)))


def _arc_color(ang):
    """进度弧角度 -> 渐变色 (青 → 品红)"""
    sec = (ang + 90.0) / 6.0
    return _lerp565(NEON_CYAN, NEON_MAGENTA, sec / 60.0)


def _arc_seg(d, cx, cy, r, start_deg, end_deg, color, width=2):
    """圆弧段 (角度插值折线, 支持线宽)"""
    if end_deg < start_deg:
        end_deg += 360
    span = end_deg - start_deg
    if span <= 0:
        return
    step_deg = max(360.0 / (2 * 3.14159 * r), 0.5)
    steps = max(int(span / step_deg), 2)
    px = cx + int(r * math.cos(math.radians(start_deg)))
    py = cy + int(r * math.sin(math.radians(start_deg)))
    i = 1
    while i <= steps:
        a = start_deg + span * i / steps
        nx = cx + int(r * math.cos(math.radians(a)))
        ny = cy + int(r * math.sin(math.radians(a)))
        if width > 1:
            w_off = -(width // 2)
            while w_off <= width // 2:
                d.line(px, py + w_off, nx, ny + w_off, color)
                w_off += 1
        else:
            d.line(px, py, nx, ny, color)
        px, py = nx, ny
        i += 1


def _fill_circle(d, cx, cy, r, color):
    """实心圆 (扫描线)"""
    y = -r
    while y <= r:
        x_extent = int(math.sqrt(r * r - y * y))
        d.hline(cx - x_extent, cy + y, 2 * x_extent + 1, color)
        y += 1


def _fill_circle_outline(d, cx, cy, r, color):
    """圆轮廓 (Bresenham)"""
    x = 0
    y = r
    d_val = 3 - 2 * r
    while x <= y:
        d.line(cx + x, cy + y, cx + x, cy + y, color)
        d.line(cx - x, cy + y, cx - x, cy + y, color)
        d.line(cx + x, cy - y, cx + x, cy - y, color)
        d.line(cx - x, cy - y, cx - x, cy - y, color)
        d.line(cx + y, cy + x, cx + y, cy + x, color)
        d.line(cx - y, cy + x, cx - y, cy + x, color)
        d.line(cx + y, cy - x, cx + y, cy - x, color)
        d.line(cx - y, cy - x, cx - y, cy - x, color)
        if d_val < 0:
            d_val += 4 * x + 6
        else:
            d_val += 4 * (x - y) + 10
            y -= 1
        x += 1


def _draw_rounded_rect(d, x, y, w, h, color, radius=2):
    d.fill_rect(x + radius, y, w - 2 * radius, h, color)
    d.fill_rect(x, y + radius, w, h - 2 * radius, color)


def _draw_chevron(d, x, y, color, w=4, h=8):
    d.line(x, y, x + w, y + h // 2, color)
    d.line(x, y + h, x + w, y + h // 2, color)


def _write_glow(d, font, ch, x, y, fg, glow):
    """霓虹辉光字: 四方向暗色偏移光晕 + 亮色主体"""
    d.write(font, ch, x - 1, y, glow, CLR_BG)
    d.write(font, ch, x + 1, y, glow, CLR_BG)
    d.write(font, ch, x, y - 1, glow, CLR_BG)
    d.write(font, ch, x, y + 1, glow, CLR_BG)
    d.write(font, ch, x, y, fg, CLR_BG)


def _draw_mini_bar(d, x, y, w, pct, color):
    """细进度条"""
    d.rect(x, y, w, 7, CLR_DIM)
    fw = (w - 2) * pct // 100
    if fw > 0:
        d.fill_rect(x + 1, y + 1, fw, 5, color)


def _day_of_year(y, m, day):
    doy = _CUM_DAYS[m - 1] + day
    if m > 2 and ((y % 4 == 0 and y % 100 != 0) or y % 400 == 0):
        doy += 1
    return doy


def _uv_color(val):
    c = CLR_GREEN
    if val >= 3:
        c = CLR_AMBER
    if val >= 6:
        c = CLR_ORANGE
    if val >= 8:
        c = CLR_RED
    if val >= 11:
        c = CLR_MAGENTA
    return c


def _aqi_color(val):
    if val > 150:
        return CLR_RED
    if val > 100:
        return CLR_AMBER
    return CLR_GREEN


# ---- 图标族 ----

def _draw_sun_icon(d, cx, cy, r=10, color=0xFD20):
    d.fill_rect(cx - r, cy - r, 2 * r + 1, 2 * r + 1, color)
    d.fill_rect(cx - r + 1, cy - r + 1, 2 * r - 1, 2 * r - 1, CLR_BG)
    d.fill_rect(cx - 2, cy - 2, 5, 5, color)
    rays = ((0, -1), (0, 1), (-1, 0), (1, 0),
            (-1, -1), (1, -1), (-1, 1), (1, 1))
    for dx, dy in rays:
        if dx == 0:
            x1, y1 = cx, cy + dy * (r + 3)
            x2, y2 = cx, cy + dy * (r + 7)
        elif dy == 0:
            x1, y1 = cx + dx * (r + 3), cy
            x2, y2 = cx + dx * (r + 7), cy
        else:
            x1, y1 = cx + dx * (r + 2), cy + dy * (r + 2)
            x2, y2 = cx + dx * (r + 6), cy + dy * (r + 6)
        d.line(x1, y1, x2, y2, color)


def _draw_moon_icon(d, cx, cy, r=10, illumination=0.5, is_waxing=True):
    """月相图标: illumination 0=新月 0.5=弦月 1=满月"""
    _fill_circle_outline(d, cx, cy, r, CLR_WHITE)
    if illumination < 0.05:
        return
    if illumination > 0.95:
        _fill_circle(d, cx, cy, r, CLR_WHITE)
        return
    _fill_circle(d, cx, cy, r, CLR_WHITE)
    for x_off in range(-r, r + 1):
        if is_waxing and x_off < 0:
            continue
        if not is_waxing and x_off > 0:
            continue
        rel = abs(x_off) / r
        if rel > 1:
            continue
        if is_waxing:
            shadow_half_h = int(math.sqrt(1 - rel * rel) * r * (1 - illumination * 2))
        else:
            shadow_half_h = int(math.sqrt(1 - rel * rel) * r * (1 - (1 - illumination) * 2))
        if shadow_half_h > 0:
            d.line(cx + x_off, cy - shadow_half_h,
                   cx + x_off, cy + shadow_half_h, CLR_BG)


def _draw_cloud_icon(d, cx, cy, r=10, color=CLR_WHITE):
    d.fill_rect(cx - r, cy + 1, 2 * r + 1, r, color)
    d.fill_rect(cx - r // 2, cy - r // 2, r, r, color)
    d.fill_rect(cx, cy - r // 3, 3 * r // 4, 3 * r // 4, color)


def _draw_weather_icon(d, cx, cy, code, r=12):
    """天气代码 -> 图标"""
    if code == "sunny":
        _draw_sun_icon(d, cx, cy, r, 0xFD20)
    elif code == "cloudy":
        _draw_cloud_icon(d, cx, cy, r, CLR_WHITE)
    elif code == "rain":
        _draw_cloud_icon(d, cx, cy - 2, r, CLR_WHITE)
        for i in range(3):
            dx = cx - 6 + i * 6
            d.line(dx, cy + r + 2, dx - 2, cy + r + 7, CLR_CYAN)
    elif code == "thunder":
        _draw_cloud_icon(d, cx, cy - 2, r, CLR_WHITE)
        d.line(cx - 2, cy + 6, cx + 2, cy + 10, CLR_AMBER)
        d.line(cx + 2, cy + 10, cx - 1, cy + 14, CLR_AMBER)
    elif code == "snow":
        _draw_cloud_icon(d, cx, cy - 2, r, CLR_WHITE)
        for i in range(3):
            dx = cx - 6 + i * 6
            d.fill_rect(dx, cy + r + 2, 2, 2, CLR_WHITE)
            d.fill_rect(dx + 1, cy + r + 4, 1, 1, CLR_WHITE)
    elif code == "fog":
        for i in range(3):
            d.hline(cx - r, cy - 3 + i * 4, 2 * r, CLR_GRAY)
    else:
        _draw_cloud_icon(d, cx, cy, r, CLR_WHITE)


# ---- 天气图标库 (icons/black, 心知天气数字代码) ----

# 内部代码 -> 图标库数字 code (code_num 缺失时的回退映射)
_INTERNAL_ICON = {"sunny": "0", "cloudy": "1", "rain": "6",
                  "thunder": "4", "snow": "13", "fog": "17"}


def _png_size(path):
    """读 PNG 文件头取宽高 (IHDR 固定偏移 16..24)"""
    f = open(path, "rb")
    try:
        head = f.read(24)
    finally:
        f.close()
    if len(head) < 24:
        raise ValueError("bad png")
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def _draw_weather_png(d, cx, cy, code_num, internal_code, scale=2):
    """用 icons/black 图标库画天气图标 (按文件头宽高居中);
    code 文件缺失依次回退 99(未知) -> 手绘矢量图标"""
    candidates = []
    n = str(code_num)
    if n.isdigit():
        candidates.append(n)
    candidates.append("99")
    for c in candidates:
        path = "/icons/black/%s@%dx.png" % (c, scale)
        try:
            w, h = _png_size(path)
            d.png(path, cx - w // 2, cy - h // 2, True)
            return
        except Exception as e:
            print("[NETCLK] png fail %s: %s" % (path, e))
    print("[NETCLK] weather icon fallback hand-drawn")
    _draw_weather_icon(d, cx, cy,
                       _INTERNAL_ICON.get(internal_code, "cloudy"),
                       r=12 if scale == 1 else 18)


# ---- 数字雨 ----

def _rain_masked(cx, cy):
    """雨滴单元格是否落在遮罩区 (固定矩形 + 秒针盘圆域)"""
    for mx0, my0, mx1, my1 in _RAIN_MASKS:
        if mx0 <= cx < mx1 and my0 <= cy < my1:
            return True
    dx = cx + 4 - DIAL_CX
    dy = cy + 4 - DIAL_CY
    return (dx * dx + dy * dy) <= (DIAL_R + 10) * (DIAL_R + 10)


def _rain_init():
    drops = []
    for _ in range(_RCOLS):
        drops.append([random.randint(-20, -1),
                      random.choice(_SPEED_POOL),
                      random.randint(4, 10),
                      random.random() < 0.6])
    return drops


def _rain_step(d, drops):
    """推进一帧数字雨 (遮罩区跳过)"""
    for col in range(_RCOLS):
        row, speed, trail, active = drops[col]
        if not active:
            if random.random() < 0.05:
                drops[col][0] = 0
                drops[col][1] = random.choice(_SPEED_POOL2)
                drops[col][2] = random.randint(4, 10)
                drops[col][3] = True
            continue
        cx = col * _CHAR_W
        if 0 <= row < _RROWS and not _rain_masked(cx, row * _CHAR_H):
            cy = row * _CHAR_H
            d.fill_rect(cx, cy, _CHAR_W, _CHAR_H, CLR_GREEN)
            d.pixel(cx + 3, cy + 3, CLR_WHITE)
        t = 1
        while t <= trail:
            tr = row - t
            if 0 <= tr < _RROWS and not _rain_masked(cx, tr * _CHAR_H):
                ty = tr * _CHAR_H
                d.fill_rect(cx, ty, _CHAR_W, _CHAR_H,
                            DIM_G if t <= trail // 2 else DARK_G)
            t += 1
        er = row - trail - 1
        if 0 <= er < _RROWS and not _rain_masked(cx, er * _CHAR_H):
            d.fill_rect(cx, er * _CHAR_H, _CHAR_W, _CHAR_H, CLR_BG)
        row += speed
        drops[col][0] = row
        if row - trail > _RROWS:
            drops[col][0] = random.randint(-5, -1)
            drops[col][1] = random.choice(_SPEED_POOL)
            drops[col][2] = random.randint(4, 10)
            drops[col][3] = random.random() < 0.7


def _draw_cyber_frame(d, x, y, w, h, idx):
    """动态赛博边框 (无背景填充, 4色轮转 + 四角亮标)"""
    main_c = _BORDER_COLORS[idx % 4]
    corner_c = CYBER_CORNER if idx % 2 == 0 else CLR_CYAN
    d.rect(x, y, w, h, main_c)
    d.rect(x - 1, y - 1, w + 2, h + 2, CYBER_GLOW)
    arm = 8
    d.hline(x, y, arm, corner_c)
    d.vline(x, y, arm, corner_c)
    d.hline(x + w - arm, y, arm, corner_c)
    d.vline(x + w - 1, y, arm, corner_c)
    d.hline(x, y + h - 1, arm, corner_c)
    d.vline(x, y + h - arm, arm, corner_c)
    d.hline(x + w - arm, y + h - 1, arm, corner_c)
    d.vline(x + w - 1, y + h - arm, arm, corner_c)
    mx = x + w // 2
    my = y + h // 2
    d.fill_rect(mx - 3, y - 1, 6, 3, corner_c)
    d.fill_rect(mx - 3, y + h - 2, 6, 3, corner_c)
    d.fill_rect(x - 1, my - 3, 3, 6, corner_c)
    d.fill_rect(x + w - 2, my - 3, 3, 6, corner_c)


# ============================================================
#  主模式
# ============================================================

class NetClockMode(BaseMode):
    """网络时钟 - 时钟/天气/菜单/秒表/定时器"""

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._rtc = None
        self._page = "clock"
        self._exit_mode = False

        # 数据
        self._weather = None
        self._last_weather_check = 0

        # 时钟页
        self._rain_drops = None
        self._frame_idx = 0
        self._frame_ts = 0
        self._colon_on = True
        self._colon_ts = 0
        self._last_hms = ""
        self._last_sec = -1
        self._last_hm = -1
        self._ring_angle = -90.0
        self._ring_tick = None       # (x, y, ang)

        # 菜单
        self._menu_sel = 0

        # 秒表
        self._sw_running = False
        self._sw_start = 0
        self._sw_elapsed = 0
        self._sw_last_display = ""
        self._sw_last_tenth = -1

        # 定时器
        self._tm_set_m = 5
        self._tm_set_s = 0
        self._tm_cursor = 0
        self._tm_running = False
        self._tm_paused = False
        self._tm_total_ms = 0
        self._tm_start_ms = 0
        self._tm_paused_remain = 0
        self._tm_alarm = False
        self._tm_last_display = ""
        self._tm_cursor_blink_ms = 0
        self._tm_cursor_on = True
        self._tm_arc_angle = 270.0
        self._tm_arc_color = None
        self._tm_digits_shown = False
        self._tm_tick = None         # (x, y)

        # 整点报时 / NTP
        self._chime_end = 0
        self._ntp_next = 0

        # 闹钟 (配置来自 settings.json 的 alarms 节点, Web 端编辑;
        # 触发/响铃由 framework.alarm 全局服务处理, 见 BaseMode.run)
        self._alarms = []            # [{en,h,m,days}, ...]
        self._alarm_list_sel = 0     # 列表页选中项
        self._poll_http = None       # 惰性加载的 poll_http_server

    # ------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------

    def enter(self):
        self.display.fill(CLR_BG)
        self._rtc = RTC()
        self._alarm_reload()
        self._page = "clock"
        self._exit_mode = False
        self._weather = wx_service.get_weather()
        self._clock_enter()

    def loop(self):
        if self._exit_mode:
            return False
        # 闹钟触发/响铃由 framework.alarm 在 BaseMode.run() 全局处理,
        # 这里只负责页面分发
        self._http_poll()
        if self._page == "alarms":
            return self._alarm_list_loop()
        if self._page == "clock":
            return self._clock_loop()
        if self._page == "weather":
            return self._weather_loop()
        if self._page == "menu":
            return self._menu_loop()
        if self._page == "stopwatch":
            return self._sw_loop()
        if self._page == "timer":
            return self._timer_loop()
        return True

    def exit(self):
        self._weather = None

    # ------------------------------------------------------------
    #  统一框架: 标题栏 / 提示栏 / 公共服务
    # ------------------------------------------------------------

    def _draw_topbar(self, title, right=""):
        d = self.display
        sf = get_sub_font()
        if not sf:
            return
        d.fill_rect(0, 0, W, 14, CLR_BG)
        d.text(sf, title, 6, 4, CLR_GRAY, CLR_BG)
        if right:
            d.text(sf, right, W - len(right) * 8 - 16, 4, CLR_WHITE, CLR_BG)
        ok, _ip = check_wifi()
        d.fill_rect(W - 10, 4, 6, 6, CLR_GREEN if ok else CLR_RED)
        d.hline(0, 14, W, CLR_DIM)

    def _draw_hints(self, a_txt, b_txt, c_txt):
        d = self.display
        sf = get_sub_font()
        d.fill_rect(0, H - 26, W, 26, CLR_BG)
        d.hline(0, H - 26, W, CLR_DIM)
        if not sf:
            return
        y = H - 19
        d.text(sf, a_txt, 8, y, CLR_CYAN, CLR_BG)
        d.text(sf, b_txt, (W - len(b_txt) * 8) // 2, y, CLR_GREEN, CLR_BG)
        d.text(sf, c_txt, W - len(c_txt) * 8 - 8, y, CLR_RED, CLR_BG)

    def _clock_read_time(self):
        """(hh, mm, ss, wd, year, month, day), 异常给默认值"""
        try:
            dt = self._rtc.datetime()
            return dt[4], dt[5], dt[6], dt[3], dt[0], dt[1], dt[2]
        except Exception:
            return 12, 0, 0, 0, 2026, 1, 1

    def _ntp_tick(self):
        """NTP 重同步: 进入 5s 后首次; 成功后每 6 小时, 失败 60s 后重试"""
        if self._ntp_next == 0:
            self._ntp_next = time.ticks_add(time.ticks_ms(), 5000)
            return
        if time.ticks_diff(time.ticks_ms(), self._ntp_next) < 0:
            return
        ok = False
        try:
            from framework.http_server import sync_device_time
            ok = bool(sync_device_time().get("ok"))
        except Exception:
            pass
        # 失败不能照排 6 小时: RTC 无掉电保持, 开机 NTP 失败时这里是
        # 唯一兜底, 60s 快速重试直到校准
        self._ntp_next = time.ticks_add(
            time.ticks_ms(), 6 * 3600 * 1000 if ok else 60 * 1000)

    def _weather_tick(self):
        """天气缓存过期检查 (每分钟一次); 返回 True 表示数据已更新"""
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_weather_check) <= 60000:
            return False
        self._last_weather_check = now
        ts = self._weather.get("ts", 0) if self._weather else 0
        if not self._weather or (time.time() - ts) > 600:
            self._weather = wx_service.get_weather()
            return True
        return False

    def _trigger_chime(self):
        self._chime_end = time.ticks_add(time.ticks_ms(), 900)
        if self.buzzer:
            try:
                self.buzzer.melody([(880, 120, 60), (1100, 120, 60),
                                    (1320, 220, 0)])
            except Exception:
                pass

    def _chime_tick(self, redraw_fn):
        """整点放射扩散圆环 (非阻塞), 结束后回调重绘当前页"""
        if not self._chime_end:
            return
        now = time.ticks_ms()
        remain = time.ticks_diff(self._chime_end, now)
        if remain <= 0:
            self._chime_end = 0
            redraw_fn()
            return
        el = 900 - remain
        d = self.display
        colors = (CLR_CYAN, CLR_MAGENTA, CLR_GREEN)
        k = 0
        while k < 3:
            r = el // 3 - k * 22
            if r > 4:
                _fill_circle_outline(d, W // 2, H // 2, r,
                                     colors[(self._frame_idx + k) % 3])
            k += 1

    def _chime_guard(self):
        """整点检测 (秒沿调用): mm==ss==0 触发报时"""
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        if ss == 0 and mm == 0 and hh != self._last_hm:
            self._last_hm = hh
            self._trigger_chime()

    # ============================================================
    #  页面 1: 时钟页 (数字雨 + 辉光大字 + 秒针盘)
    # ============================================================

    def _clock_enter(self):
        d = self.display
        gc.collect()
        d.fill(CLR_BG)
        if self._rain_drops is None:
            self._rain_drops = _rain_init()
        self._frame_idx = 0
        self._frame_ts = time.ticks_ms()
        self._colon_on = True
        self._colon_ts = time.ticks_ms()
        self._last_hms = ""
        self._last_sec = -1
        self._ring_angle = -90.0
        self._ring_tick = None
        self._clock_layout()

    def _clock_layout(self):
        d = self.display
        _draw_cyber_frame(d, CLK_FRAME[0], CLK_FRAME[1],
                          CLK_FRAME[2], CLK_FRAME[3], self._frame_idx)
        self._clock_info_strip()
        self._clock_dial_base()
        self._clock_draw_weekday()
        self._clock_draw_digits()
        self._clock_hints()

    def _clock_info_strip(self):
        """框内信息行: 天气图标 + 天气/温度 (左), 城市 + WiFi 点 (右)"""
        d = self.display
        sf = get_sub_font()
        d.fill_rect(8, 22, W - 16, 14, CLR_BG)
        d.hline(8, 42, W - 16, CLR_DIM)
        if not sf:
            return
        code = self._weather.get("code", "cloudy") if self._weather else "cloudy"
        text = self._weather.get("text", "--") if self._weather else "--"
        temp = self._weather.get("temp") if self._weather else None
        _draw_weather_icon(d, 14, 29, code, r=5)
        left = text
        if temp is not None:
            left = "%s %dC" % (text, temp)
        d.text(sf, left, 24, 25, CLR_CYAN, CLR_BG)
        city = self._weather.get("city", "--") if self._weather else "--"
        ok, _ip = check_wifi()
        d.fill_rect(W - 14, 26, 6, 6, CLR_GREEN if ok else CLR_RED)
        d.text(sf, city, W - 22 - len(city) * 8, 25, CLR_WHITE, CLR_BG)
        # 下一闹钟提示 (居中, 与左右内容有碰撞时省略)
        hint = self._alarm_next_hint()
        if hint:
            hx = W // 2 - len(hint) * 4
            if (24 + len(left) * 8 < hx
                    and hx + len(hint) * 8 < W - 22 - len(city) * 8):
                d.text(sf, hint, hx, 25, CLR_MAGENTA, CLR_BG)

    def _alarm_next_hint(self):
        """今日下一个将触发的启用闹钟提示 ('AL 07:30'), 无则空串"""
        if not self._alarms:
            return ""
        hh, mm, _ss, wd, _yr, _mo, _dy = self._clock_read_time()
        now_min = hh * 60 + mm
        best = None
        for a in self._alarms:
            if not a.get("en"):
                continue
            days = a.get("days")
            if days and wd not in days:
                continue
            t = a.get("h", 0) * 60 + a.get("m", 0)
            if t < now_min:
                continue
            if best is None or t < best:
                best = t
        if best is None:
            return ""
        return "AL %02d:%02d" % (best // 60, best % 60)

    def _clock_draw_weekday(self):
        d = self.display
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        d.fill_rect(56, 48, 128, 32, CLR_BG)
        wk = WEEKDAYS_CN[wd] if 0 <= wd < 7 else "周一"
        if hanzi24:
            wk_w = len(wk) * hanzi24.MAX_WIDTH
            d.write(hanzi24, wk, (W - wk_w) // 2, 52, CLR_WHITE, CLR_BG)
        # 日期 (24px 字库数字, 逐字取宽居中, 便于老花识读)
        d.fill_rect(20, 82, 200, 30, CLR_BG)
        if hanzi24:
            date_str = "%04d-%02d-%02d" % (yr, mo, dy)
            dw = 0
            for ch_str in date_str:
                idx = hanzi24.MAP.index(ch_str)
                dw += hanzi24._WIDTHS[idx]
            d.write(hanzi24, date_str, (W - dw) // 2, 84,
                    CLR_CYAN, CLR_BG)

    def _draw_colon(self, color):
        d = self.display
        d.fill_rect(CLK_COLON_X, CLK_Y0 + 8, 6, 6, color)
        d.fill_rect(CLK_COLON_X, CLK_Y0 + 22, 6, 6, color)

    def _clock_draw_digits(self, force=False):
        """辉光大字 HH:MM (仅分钟变化或 force 时重绘)"""
        d = self.display
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        hms_str = "%02d:%02d" % (hh, mm)
        if hms_str == self._last_hms and not force:
            return
        self._last_hms = hms_str
        d.fill_rect(34, 112, 172, 46, CLR_BG)
        sy32 = _get_sy32()
        if sy32:
            cx = CLK_X0
            for ch_str in hms_str:
                if ch_str == ':':
                    self._draw_colon(CLR_CYAN)
                    cx += 16 + 4
                else:
                    _write_glow(d, sy32, ch_str, cx, CLK_Y0,
                                CLR_WHITE, NEON_GLOW)
                    cx += 33 + 4
        else:
            BIG_FONT, BIG_W, BIG_H = get_big_font()
            if BIG_FONT:
                d.text(BIG_FONT, hms_str,
                       (W - len(hms_str) * BIG_W) // 2, 108, CLR_WHITE, CLR_BG)

    def _clock_hints(self):
        d = self.display
        sf = get_sub_font()
        d.fill_rect(8, 216, W - 16, 14, CLR_BG)
        if not sf:
            return
        d.text(sf, "A:WX", 12, 218, CLR_CYAN, CLR_BG)
        d.text(sf, "B:MENU", (W - 6 * 8) // 2, 218, CLR_GREEN, CLR_BG)
        d.text(sf, "C:EXIT", W - 12 - 6 * 8, 218, CLR_RED, CLR_BG)

    def _clock_dial_base(self):
        _fill_circle_outline(self.display, DIAL_CX, DIAL_CY, DIAL_R, CLR_DIM)

    def _clock_dial_update(self):
        """秒针盘: 增量补弧 + 外侧游标刻线 + 盘内秒数"""
        d = self.display
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        frac = (time.ticks_ms() % 1000) / 1000.0
        ang = -90.0 + (ss + frac) * 6.0
        if ang < self._ring_angle:
            # 整分回绕兜底 (正常由秒沿重置)
            self._ring_angle = -90.0
            self._clock_dial_base()
        if ang > self._ring_angle:
            _arc_seg(d, DIAL_CX, DIAL_CY, DIAL_R,
                     self._ring_angle, ang,
                     _arc_color(self._ring_angle), width=3)
            self._ring_angle = ang
        # 游标刻线: 擦旧 + 补弧 + 画新
        if self._ring_tick:
            ox, oy, oang = self._ring_tick
            d.fill_rect(ox - 3, oy - 3, 7, 7, CLR_BG)
            _arc_seg(d, DIAL_CX, DIAL_CY, DIAL_R,
                     oang - 2.0, oang + 2.0, _arc_color(oang), width=3)
        rad = math.radians(ang)
        x1 = DIAL_CX + int((DIAL_R + 1) * math.cos(rad))
        y1 = DIAL_CY + int((DIAL_R + 1) * math.sin(rad))
        x2 = DIAL_CX + int((DIAL_R + 5) * math.cos(rad))
        y2 = DIAL_CY + int((DIAL_R + 5) * math.sin(rad))
        tx = DIAL_CX + int((DIAL_R + 3) * math.cos(rad))
        ty = DIAL_CY + int((DIAL_R + 3) * math.sin(rad))
        d.line(x1, y1, x2, y2, CLR_WHITE)
        self._ring_tick = (tx, ty, ang)
        # 盘内秒数
        sf = get_sub_font()
        if sf:
            d.fill_rect(DIAL_CX - 10, DIAL_CY - 6, 20, 12, CLR_BG)
            d.text(sf, "%02d" % ss, DIAL_CX - 8, DIAL_CY - 4,
                   CLR_CYAN, CLR_BG)

    def _clock_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            self._page = "weather"
            self._weather_enter()
            return True
        if sel:
            self._menu_enter()
            self._page = "menu"
            return True
        if down:
            self.flash_exit()
            return False

        now = time.ticks_ms()

        # 秒沿: 新分钟重绘大字/日期, 重置秒针盘, 整点报时
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        if ss != self._last_sec:
            self._last_sec = ss
            if ss == 0:
                self._ring_angle = -90.0
                self._ring_tick = None
                self._clock_dial_base()
                self._clock_draw_digits()
                self._clock_draw_weekday()
                self._chime_guard()

        self._clock_dial_update()

        if RAIN_ENABLED and self._rain_drops:
            _rain_step(self.display, self._rain_drops)

        # 动态边框 (400ms)
        if time.ticks_diff(now, self._frame_ts) > 400:
            self._frame_ts = now
            self._frame_idx += 1
            _draw_cyber_frame(self.display, CLK_FRAME[0], CLK_FRAME[1],
                              CLK_FRAME[2], CLK_FRAME[3], self._frame_idx)

        # 冒号闪烁 (500ms)
        if time.ticks_diff(now, self._colon_ts) > 500:
            self._colon_ts = now
            self._colon_on = not self._colon_on
            self._draw_colon(CLR_CYAN if self._colon_on else CLR_DIM)

        # 整点报时 / 天气过期 / NTP
        self._chime_tick(self._clock_layout)
        if self._weather_tick():
            self._clock_info_strip()
        self._ntp_tick()

        time.sleep_ms(40)
        return True

    # ============================================================
    #  页面 2: 天气页
    # ============================================================

    def _weather_enter(self):
        d = self.display
        gc.collect()
        d.fill(CLR_BG)
        city = self._weather.get("city", "--") if self._weather else "--"
        self._draw_topbar("WEATHER", city)
        self._weather_draw_body()
        self._draw_hints("A:TIME", "B:MENU", "C:EXIT")

    def _weather_draw_body(self):
        d = self.display
        sf = get_sub_font()
        w = self._weather or {}

        # 大图标 (图标库 @2x, 居中) + 温度 + 天气文字
        d.fill_rect(0, 18, W, 122, CLR_BG)
        code = w.get("code", "cloudy")
        _draw_weather_png(d, 62, 78,
                          w.get("code_num", _INTERNAL_ICON.get(code, "99")),
                          code, scale=2)
        temp = w.get("temp")
        sy32 = _get_sy32()
        if temp is not None and sy32:
            cx = 134
            for ch_str in str(temp):
                if ch_str.isdigit():
                    _write_glow(d, sy32, ch_str, cx, 44,
                                CLR_WHITE, NEON_GLOW)
                    cx += 33 + 4
                else:
                    if sf:
                        d.text(sf, ch_str, cx, 70, CLR_GRAY, CLR_BG)
                    cx += 12
            if sf:
                d.text(sf, "C", cx, 70, CLR_GRAY, CLR_BG)
        elif sf:
            d.text(sf, "--", 134, 44, CLR_GRAY, CLR_BG)
        if hanzi24:
            text = w.get("text", "--")
            d.write(hanzi24, text, 134, 88, CLR_CYAN, CLR_BG)

        # 三徽章: 湿度 / AQI / UV
        d.fill_rect(0, 142, W, 28, CLR_BG)
        hum = w.get("humidity")
        hum_s = "%d%%" % hum if hum is not None else "--"
        aqi = w.get("aqi")
        aqi_s = str(aqi) if aqi is not None else "--"
        aqi_c = _aqi_color(aqi) if aqi is not None else CLR_GRAY
        uv = w.get("uv")
        uv_s = str(uv) if uv is not None else "--"
        uv_c = _uv_color(uv) if uv is not None else CLR_GRAY
        badges = (("HUM", hum_s, CLR_CYAN),
                  ("AQI", aqi_s, aqi_c),
                  ("UV", uv_s, uv_c))
        i = 0
        while i < 3:
            label, val, vc = badges[i]
            bx = 6 + i * 80
            _draw_rounded_rect(d, bx, 144, 68, 26, CLR_CARD, radius=3)
            if sf:
                d.text(sf, label, bx + 8, 148, CLR_GRAY, CLR_CARD)
                d.text(sf, val, bx + 8, 158, vc, CLR_CARD)
            i += 1

        # 月相
        d.fill_rect(0, 174, W, 22, CLR_BG)
        try:
            m = moon_mod.moon_phase()
            _draw_moon_icon(d, 30, 185, 9, m["illumination"], m["is_waxing"])
            if sf:
                d.text(sf, "MOON %d%%" % int(m["illumination"] * 100),
                       48, 181, CLR_WHITE, CLR_BG)
        except Exception:
            if sf:
                d.text(sf, "MOON --", 48, 181, CLR_GRAY, CLR_BG)

        # 进度条: 当日 / 年度
        d.fill_rect(0, 196, W, 18, CLR_BG)
        hh, mm, ss, wd, yr, mo, dy = self._clock_read_time()
        if sf:
            day_pct = (hh * 3600 + mm * 60 + ss) * 100 // 86400
            d.text(sf, "D", 8, 197, CLR_CYAN, CLR_BG)
            _draw_mini_bar(d, 24, 198, 164, day_pct, CLR_CYAN)
            d.text(sf, "%d%%" % day_pct, 192, 197, CLR_CYAN, CLR_BG)
            year_pct = _day_of_year(yr, mo, dy) * 100 // 365
            d.text(sf, "Y", 8, 205, CLR_MAGENTA, CLR_BG)
            _draw_mini_bar(d, 24, 206, 164, year_pct, CLR_MAGENTA)
            d.text(sf, "%d%%" % year_pct, 192, 205, CLR_MAGENTA, CLR_BG)

    def _weather_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            self._page = "clock"
            self._clock_enter()
            return True
        if sel:
            self._menu_enter()
            self._page = "menu"
            return True
        if down:
            self.flash_exit()
            return False

        if self._weather_tick():
            city = self._weather.get("city", "--") if self._weather else "--"
            self._draw_topbar("WEATHER", city)
            self._weather_draw_body()
        self._ntp_tick()
        self._chime_tick(self._weather_enter)

        time.sleep_ms(200)
        return True

    # ============================================================
    #  页面 3: 功能菜单
    # ============================================================

    def _menu_enter(self):
        d = self.display
        gc.collect()
        d.fill(CLR_BG)
        self._menu_sel = 0
        self._draw_topbar("MENU")
        self._menu_draw_items()
        self._draw_hints("A:NEXT", "B:OK", "C:BACK")

    def _menu_draw_items(self):
        d = self.display
        i = 0
        while i < _MENU_COUNT:
            self._menu_draw_item(i)
            i += 1

    def _menu_draw_item(self, idx):
        d = self.display
        item_id, name, accent = _MENU_ITEMS[idx]
        y = 20 + idx * 32            # 6 项适配: 底项不压提示栏
        selected = (idx == self._menu_sel)
        d.fill_rect(16, y - 2, W - 32, 30, CLR_BG)
        if selected:
            _draw_rounded_rect(d, 20, y, 200, 28, CLR_CARD, radius=3)
            d.rect(20, y, 200, 28, CLR_WHITE)
            d.fill_rect(26, y + 3, 6, 22, accent)
            if hanzi24:
                d.write(hanzi24, name, 44, y + 2, CLR_WHITE, CLR_CARD)
            _draw_chevron(d, 204, y + 10, CLR_WHITE)
        else:
            d.fill_rect(26, y + 3, 6, 22, CLR_DIM)
            if hanzi24:
                d.write(hanzi24, name, 44, y + 2, CLR_GRAY, CLR_BG)

    def _menu_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            old = self._menu_sel
            self._menu_sel = (self._menu_sel + 1) % _MENU_COUNT
            self._menu_draw_item(old)
            self._menu_draw_item(self._menu_sel)
            if self.buzzer:
                self.buzzer.tone(800, 15)
        if sel:
            item_id = _MENU_ITEMS[self._menu_sel][0]
            if self.buzzer:
                self.buzzer.tone(1200, 30)
            if item_id == "back":
                self._page = "clock"
                self._clock_enter()
                return True
            if item_id == "wx_refresh":
                wx_service.invalidate_cache()
                self._weather = wx_service.get_weather(force_refresh=True)
                self._page = "clock"
                self._clock_enter()
                if self.buzzer:
                    self.buzzer.tone(1200, 50)
                return True
            if item_id == "stopwatch":
                self._page = "stopwatch"
                self._sw_enter()
                return True
            if item_id == "timer":
                self._page = "timer"
                self._tm_enter()
                return True
            if item_id == "alarm":
                self._page = "alarms"
                self._alarm_list_enter()
                return True
        if down:
            # C: 返回时钟页
            self._page = "clock"
            self._clock_enter()
            return True
        time.sleep_ms(30)
        return True

    # ============================================================
    #  页面 4: 秒表
    # ============================================================

    def _sw_enter(self):
        d = self.display
        gc.collect()
        d.fill(CLR_BG)
        self._sw_running = False
        self._sw_start = 0
        self._sw_elapsed = 0
        self._sw_last_display = ""
        self._sw_last_tenth = -1
        self._draw_topbar("STOPWATCH")
        self._sw_draw_status()
        self._draw_hints("A:RUN", "B:CLR", "C:BACK")
        self._sw_draw_time()

    def _sw_get_elapsed(self):
        if self._sw_running:
            return self._sw_elapsed + time.ticks_diff(time.ticks_ms(), self._sw_start)
        return self._sw_elapsed

    def _sw_draw_status(self):
        sf = get_sub_font()
        if not sf:
            return
        d = self.display
        d.fill_rect((W - 8 * 8) // 2 - 4, 126, 72, 14, CLR_BG)
        if self._sw_running:
            d.text(sf, "RUN", (W - 3 * 8) // 2, 130, CLR_GREEN, CLR_BG)
        elif self._sw_elapsed > 0:
            d.text(sf, "STOP", (W - 4 * 8) // 2, 130, CLR_AMBER, CLR_BG)
        else:
            d.text(sf, "READY", (W - 5 * 8) // 2, 130, CLR_DIM, CLR_BG)

    def _sw_draw_time(self):
        """大字 MM:SS (仅秒变化重绘) + 十分位小字 (10Hz 重绘)"""
        d = self.display
        sy32 = _get_sy32()
        elapsed = self._sw_get_elapsed()
        total_sec = elapsed // 1000
        tenth = (elapsed % 1000) // 100
        mm = (total_sec // 60) % 100
        ss = total_sec % 60
        fg = CLR_GREEN if self._sw_running else CLR_WHITE

        display_str = "%02d:%02d" % (mm, ss)
        if display_str != self._sw_last_display:
            self._sw_last_display = display_str
            d.fill_rect(30, 68, 180, 50, CLR_BG)
            if sy32:
                cw = 33
                gap = 4
                x0 = (W - (4 * cw + 16 + 3 * gap)) // 2
                y0 = 72
                cx = x0
                for ch_str in display_str:
                    if ch_str == ':':
                        d.fill_rect(cx + 5, y0 + 8, 6, 6, fg)
                        d.fill_rect(cx + 5, y0 + 22, 6, 6, fg)
                        cx += 16 + gap
                    else:
                        _write_glow(d, sy32, ch_str, cx, y0, fg, NEON_GLOW)
                        cx += cw + gap
            elif SUB_FONT:
                d.text(SUB_FONT, display_str, (W - 5 * 8) // 2, 84, fg, CLR_BG)

        # 十分位小字
        if tenth != self._sw_last_tenth:
            self._sw_last_tenth = tenth
            sf = get_sub_font()
            if sf:
                d.fill_rect(200, 96, 24, 12, CLR_BG)
                d.text(sf, ".%d" % tenth, 200, 96, CLR_CYAN, CLR_BG)

    def _sw_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            if self._sw_running:
                self._sw_elapsed += time.ticks_diff(time.ticks_ms(), self._sw_start)
                self._sw_running = False
                if self.buzzer:
                    self.buzzer.tone(600, 30)
            else:
                self._sw_start = time.ticks_ms()
                self._sw_running = True
                if self.buzzer:
                    self.buzzer.tone(1000, 30)
            self._sw_last_display = ""
            self._sw_draw_status()
        if sel:
            # B: 清零 (仅停止时)
            if not self._sw_running and self._sw_elapsed > 0:
                self._sw_elapsed = 0
                self._sw_last_display = ""
                self._sw_last_tenth = -1
                self._sw_draw_status()
                if self.buzzer:
                    self.buzzer.tone(800, 50)
        if down:
            # C: 返回菜单
            self._page = "menu"
            self._menu_enter()
            return True
        self._sw_draw_time()
        time.sleep_ms(50)
        return True

    # ============================================================
    #  页面 5: 定时器
    # ============================================================

    def _tm_enter(self):
        d = self.display
        gc.collect()
        d.fill(CLR_BG)
        self._tm_running = False
        self._tm_paused = False
        self._tm_alarm = False
        self._tm_last_display = ""
        self._tm_cursor_blink_ms = time.ticks_ms()
        self._tm_cursor_on = True
        self._draw_topbar("TIMER")
        self._draw_tm_setting()
        self._draw_hints("A:+1", "B:NEXT/GO", "C:BACK")

    def _tm_digit_x(self, idx):
        return (W - 164) // 2 + idx * (33 + 4)

    def _tm_draw_digit(self, idx):
        """重绘单个数字位 (光标闪烁用, 避免整块清屏闪烁)"""
        d = self.display
        sy32 = _get_sy32()
        if not sy32:
            return
        cw, ch = 33, 37
        y0 = TM_SET_Y0
        cx = self._tm_digit_x(idx)
        is_sel = (idx == self._tm_cursor)
        d.fill_rect(cx - 2, y0 - 14, cw + 4, ch + 26, CLR_BG)
        if idx < 2:
            digit = ("%02d" % self._tm_set_m)[idx]
        else:
            digit = ("%02d" % self._tm_set_s)[idx - 2]
        if is_sel and self._tm_cursor_on:
            d.fill_rect(cx - 2, y0 - 2, cw + 4, ch + 4, CLR_WHITE)
            d.write(sy32, digit, cx, y0, CLR_BG, CLR_WHITE)
            ax = cx + cw // 2
            d.line(ax - 4, y0 - 8, ax, y0 - 12, CLR_WHITE)
            d.line(ax + 4, y0 - 8, ax, y0 - 12, CLR_WHITE)
            d.line(ax - 4, y0 + ch + 6, ax, y0 + ch + 10, CLR_WHITE)
            d.line(ax + 4, y0 + ch + 6, ax, y0 + ch + 10, CLR_WHITE)
        else:
            color = CLR_AMBER if is_sel else CLR_WHITE
            d.write(sy32, digit, cx, y0, color, CLR_BG)

    def _draw_tm_setting(self):
        d = self.display
        d.fill_rect(10, 40, W - 20, 90, CLR_BG)
        i = 0
        while i < 4:
            self._tm_draw_digit(i)
            i += 1
        cx0 = (W - 164) // 2
        d.fill_rect(cx0 + 74 + 8 - 3, TM_SET_Y0 + 8, 6, 6, CLR_ORANGE)
        d.fill_rect(cx0 + 74 + 8 - 3, TM_SET_Y0 + 22, 6, 6, CLR_ORANGE)

    def _tm_increment(self):
        cur = self._tm_cursor
        if cur == 0:
            v = (self._tm_set_m // 10 + 1) % 10
            self._tm_set_m = v * 10 + self._tm_set_m % 10
        elif cur == 1:
            v = (self._tm_set_m % 10 + 1) % 10
            self._tm_set_m = (self._tm_set_m // 10) * 10 + v
        elif cur == 2:
            v = (self._tm_set_s // 10 + 1) % 6
            self._tm_set_s = v * 10 + self._tm_set_s % 10
        elif cur == 3:
            v = (self._tm_set_s % 10 + 1) % 10
            self._tm_set_s = (self._tm_set_s // 10) * 10 + v

    def _timer_loop(self):
        if self._tm_alarm:
            return self._tm_alarm_loop()
        if self._tm_running:
            return self._tm_running_loop()

        # 设置模式
        now = time.ticks_ms()
        if time.ticks_diff(now, self._tm_cursor_blink_ms) > 500:
            self._tm_cursor_on = not self._tm_cursor_on
            self._tm_cursor_blink_ms = now
            self._tm_draw_digit(self._tm_cursor)

        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            self._tm_increment()
            self._draw_tm_setting()
            if self.buzzer:
                self.buzzer.tone(800, 15)
        if sel:
            # B: 下一位 / 最后一位时开始
            if self._tm_cursor < 3:
                self._tm_cursor += 1
                self._tm_cursor_on = True
                self._tm_cursor_blink_ms = now
                self._draw_tm_setting()
                if self.buzzer:
                    self.buzzer.tone(600, 15)
            else:
                total_ms = (self._tm_set_m * 60 + self._tm_set_s) * 1000
                if total_ms > 0:
                    self._tm_total_ms = total_ms
                    self._tm_start_ms = time.ticks_ms()
                    self._tm_running = True
                    self._tm_last_display = ""
                    self._tm_enter_running()
                    if self.buzzer:
                        self.buzzer.tone(1200, 50)
                elif self.buzzer:
                    self.buzzer.tone(300, 100)
        if down:
            # C: 返回菜单
            self._page = "menu"
            self._menu_enter()
            return True

        time.sleep_ms(80)
        return True

    def _tm_enter_running(self):
        d = self.display
        d.fill(CLR_BG)
        self._draw_topbar("TIMER-RUN")
        _fill_circle_outline(d, W // 2, 160, TM_DIAL_R, CLR_DIM)
        self._tm_arc_angle = 270.0
        self._tm_arc_color = None
        self._tm_digits_shown = False
        self._tm_tick = None
        self._draw_hints("A:PAUSE", "B:RESET", "C:CANCEL")

    def _tm_get_remain(self):
        if self._tm_paused:
            return self._tm_paused_remain
        elapsed = time.ticks_diff(time.ticks_ms(), self._tm_start_ms)
        return max(self._tm_total_ms - elapsed, 0)

    def _tm_running_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            # A: 暂停 / 继续
            if not self._tm_paused:
                self._tm_paused = True
                self._tm_paused_remain = self._tm_get_remain()
                if self.buzzer:
                    self.buzzer.tone(600, 30)
            else:
                self._tm_start_ms = time.ticks_ms() - (self._tm_total_ms - self._tm_paused_remain)
                self._tm_paused = False
                self._tm_last_display = ""
                if self.buzzer:
                    self.buzzer.tone(1000, 30)
            sf = get_sub_font()
            if sf:
                d = self.display
                d.fill_rect((W - 8 * 8) // 2 - 4, 104, 72, 14, CLR_BG)
                d.text(sf, "PAUSED" if self._tm_paused else "RUNNING",
                       (W - 7 * 8) // 2, 108,
                       CLR_AMBER if self._tm_paused else CLR_DIM, CLR_BG)
        if sel:
            # B: 重置回设置页
            self._tm_running = False
            self._tm_paused = False
            self._tm_last_display = ""
            self._tm_enter()
            if self.buzzer:
                self.buzzer.tone(800, 50)
            return True
        if down:
            # C: 取消并返回菜单
            self._tm_running = False
            self._tm_paused = False
            self._page = "menu"
            self._menu_enter()
            return True

        remain = self._tm_get_remain()
        if remain <= 0 and not self._tm_paused:
            self._tm_trigger_alarm()
            return True

        self._tm_draw_running(remain)
        time.sleep_ms(50)
        return True

    def _tm_draw_running(self, remain):
        d = self.display
        total_sec = remain // 1000
        mm = (total_sec // 60) % 100
        ss = total_sec % 60

        if total_sec <= 10:
            color = CLR_RED
        elif total_sec <= 30:
            color = CLR_AMBER
        else:
            color = CLR_CYAN

        cx, cy, r = W // 2, 160, TM_DIAL_R

        # 进度弧: 增量收缩 (无整圈清除, 防闪烁)
        if self._tm_total_ms > 0:
            ratio = remain / self._tm_total_ms
            if ratio > 1.0:
                ratio = 1.0
            end_angle = -90.0 + ratio * 360.0

            # 颜色档位变化: 一次性整段覆盖换色
            if color != self._tm_arc_color:
                self._tm_arc_color = color
                if end_angle > -90.0:
                    _arc_seg(d, cx, cy, r, -90.0, end_angle, color, width=5)

            # 尾端回缩: 擦掉进度段并还原底环 (同时抹掉旧游标)
            prev = self._tm_arc_angle
            if end_angle < prev:
                _arc_seg(d, cx, cy, r, end_angle, prev, CLR_BG, width=5)
                _arc_seg(d, cx, cy, r, end_angle, prev, CLR_DIM, width=4)
                hi = prev + 3.0
                if hi > 269.0:
                    hi = 269.0
                if hi > prev:
                    _arc_seg(d, cx, cy, r, prev, hi, color, width=5)
            self._tm_arc_angle = end_angle

            # 游标: 擦旧刻线, 画新刻线 (刻线在弧外侧, 不压弧段)
            if self._tm_tick:
                ox, oy = self._tm_tick
                d.fill_rect(ox - 3, oy - 3, 7, 7, CLR_BG)
            rad = math.radians(end_angle)
            x1 = cx + int((r + 1) * math.cos(rad))
            y1 = cy + int((r + 1) * math.sin(rad))
            x2 = cx + int((r + 5) * math.cos(rad))
            y2 = cy + int((r + 5) * math.sin(rad))
            d.line(x1, y1, x2, y2, CLR_WHITE)
            self._tm_tick = (cx + int((r + 3) * math.cos(rad)),
                             cy + int((r + 3) * math.sin(rad)))

            # 盘内剩余百分比
            sf = get_sub_font()
            if sf:
                pct = int(ratio * 100)
                txt = "%d%%" % pct
                d.fill_rect(cx - 16, cy - 6, 32, 12, CLR_BG)
                d.text(sf, txt, cx - len(txt) * 4, cy - 4, color, CLR_BG)

        # 时间数字: 仅变化/闪烁时重绘数字区
        display_str = "%02d%02d" % (mm, ss)
        show_time = True
        if 0 < total_sec <= 10:
            if (time.ticks_ms() // 200) % 2 == 0:
                show_time = False

        if show_time:
            if display_str != self._tm_last_display or not self._tm_digits_shown:
                d.fill_rect(34, 56, 172, 46, CLR_BG)
                sy32 = _get_sy32()
                if sy32:
                    cw = 33
                    colon_w = 16
                    gap = 4
                    x0 = (W - 164) // 2
                    y0 = TM_RUN_Y0
                    cx2 = x0
                    mm_str = "%02d" % mm
                    ss_str = "%02d" % ss
                    for ch_str in mm_str + ":" + ss_str:
                        if ch_str == ':':
                            d.fill_rect(cx2 + colon_w // 2 - 2, y0 + 8, 4, 4, color)
                            d.fill_rect(cx2 + colon_w // 2 - 2, y0 + 22, 4, 4, color)
                            cx2 += colon_w + gap
                        else:
                            _write_glow(d, sy32, ch_str, cx2, y0, color, NEON_GLOW)
                            cx2 += cw + gap
                self._tm_digits_shown = True
                self._tm_last_display = display_str
        elif self._tm_digits_shown:
            d.fill_rect(34, 56, 172, 46, CLR_BG)
            self._tm_digits_shown = False
            self._tm_last_display = display_str

    def _tm_trigger_alarm(self):
        self._tm_alarm = True
        self._tm_running = False

        d = self.display
        flash = 0
        while flash < 3:
            d.fill(CLR_RED)
            time.sleep_ms(120)
            d.fill(CLR_BG)
            time.sleep_ms(120)
            flash += 1

        d.fill(CLR_BG)
        sy32 = _get_sy32()
        if sy32:
            msg = "TIME UP"
            mw = len(msg) * sy32.MAX_WIDTH
            d.write(sy32, msg, (W - mw) // 2, 90, CLR_RED, CLR_BG)
        elif SUB_FONT:
            d.text(SUB_FONT, "TIME UP!", 70, 90, CLR_RED, CLR_BG)

        sf = get_sub_font()
        if sf:
            d.text(sf, "Press any key", (W - 13 * 8) // 2, 140, CLR_GRAY, CLR_BG)

        if self.buzzer:
            try:
                self.buzzer.melody([(800, 200, 100), (1000, 200, 100), (1200, 300, 0)])
            except Exception:
                pass

    def _tm_alarm_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up or sel or down:
            self._tm_alarm = False
            self._tm_enter()
            return True
        time.sleep_ms(100)
        return True

    # ============================================================
    #  页面 6: 闹钟列表 (查看 / 快速开关)
    #  触发与响铃由全局服务 framework.alarm 处理 (BaseMode.run),
    #  配置存于 settings.json 的 alarms 节点 (恒 3 组), Web 端编辑;
    #  days: 0=周日..6=周六, 空列表 = 单次 (触发后自动关闭)。
    # ============================================================

    def _http_poll(self):
        """Web 配置生效: HTTP 服务开启时轮询请求 (非阻塞)。

        通过轻量 http_server_state 探测服务是否开启, 仅在开启后才
        惰性加载重型 http_server 模块; 平时零内存开销。
        """
        if self._poll_http is None:
            try:
                from framework import http_server_state as _hss
                if getattr(_hss, "_http_server_sock", None) is None:
                    return
            except Exception:
                self._poll_http = False
                return
            try:
                from framework.http_server import poll_http_server as _fn
                self._poll_http = _fn
            except Exception:
                self._poll_http = False
                return
        if self._poll_http:
            try:
                self._poll_http()
            except Exception:
                pass

    def _alarm_reload(self):
        """从 settings 模块缓存载入闹钟配置 (秒沿调用, 每秒一次)。

        Web 端保存走同一 settings 模块的 update(), 缓存即时更新, 这里
        每秒重读即可 ≤1s 生效; 不依赖文件 mtime (flash 时间戳粒度粗,
        同秒写入无法检出, 曾导致 Web 保存的闹钟永不生效)。
        """
        try:
            from framework import settings as st
            self._alarms = st.get_alarms()
        except Exception:
            self._alarms = []
        while len(self._alarms) < ALARM_COUNT:
            self._alarms.append({"en": False, "h": 0, "m": 0, "days": []})

    def _alarm_persist(self):
        """把当前闹钟列表写回 settings.json (列表页开关 / 单次自动关闭)"""
        try:
            from framework import settings as st
            st.update({"alarms": self._alarms})
        except Exception:
            pass

    def _alarm_days_label(self, days):
        """重复规则的紧凑 ASCII 标签 (列表页右侧): 1x=单次"""
        if not days:
            return "1x"
        if len(days) == 7:
            return "1-7"
        if days == [1, 2, 3, 4, 5]:
            return "1-5"
        if days == [0, 6]:
            return "0,6"
        return ",".join(str(d) for d in days)

    # ----- 页面 6: 闹钟列表 (查看 + 快速开关) -----

    def _alarm_list_enter(self):
        d = self.display
        gc.collect()
        self._alarm_reload()
        if self._alarm_list_sel >= ALARM_COUNT:
            self._alarm_list_sel = 0
        d.fill(CLR_BG)
        self._draw_topbar("ALARM")
        self._alarm_list_draw_items()
        self._draw_hints("A:NEXT", "B:ON/OFF", "C:BACK")

    def _alarm_list_draw_items(self):
        i = 0
        while i < ALARM_COUNT:
            self._alarm_list_draw_item(i)
            i += 1

    def _alarm_list_draw_item(self, idx):
        d = self.display
        sf = get_sub_font()
        sy32 = _get_sy32()
        if idx < len(self._alarms):
            a = self._alarms[idx]
        else:
            a = {"en": False, "h": 0, "m": 0, "days": []}
        en = bool(a.get("en"))
        y = 26 + idx * 62
        selected = (idx == self._alarm_list_sel)
        d.fill_rect(12, y, W - 24, 58, CLR_BG)
        if selected:
            d.rect(14, y + 2, 212, 52, CLR_WHITE)
        d.fill_rect(20, y + 8, 6, 40, CLR_RED if en else CLR_DIM)
        # 时间大字 (sy_32x32, 未启用置灰)
        fg = CLR_WHITE if en else CLR_GRAY
        tm = "%02d:%02d" % (a.get("h", 0), a.get("m", 0))
        tx = 32
        ty = y + 10
        if sy32:
            for ch_str in tm:
                if ch_str == ':':
                    cc = CLR_CYAN if en else CLR_DIM
                    d.fill_rect(tx + 6, ty + 8, 6, 6, cc)
                    d.fill_rect(tx + 6, ty + 22, 6, 6, cc)
                    tx += 16 + 4
                else:
                    _write_glow(d, sy32, ch_str, tx, ty, fg, NEON_GLOW)
                    tx += 33 + 4
        elif sf:
            d.text(sf, tm, 32, ty + 12, fg, CLR_BG)
        # 右侧: 开关状态 + 重复规则
        if sf:
            st_txt = "ON" if en else "OFF"
            d.text(sf, st_txt, W - 16 - len(st_txt) * 8, y + 8,
                   CLR_GREEN if en else CLR_DIM, CLR_BG)
            dl = self._alarm_days_label(a.get("days"))
            d.text(sf, dl, W - 16 - len(dl) * 8, y + 36,
                   CLR_CYAN if en else CLR_DIM, CLR_BG)

    def _alarm_list_loop(self):
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if up:
            old = self._alarm_list_sel
            self._alarm_list_sel = (self._alarm_list_sel + 1) % ALARM_COUNT
            self._alarm_list_draw_item(old)
            self._alarm_list_draw_item(self._alarm_list_sel)
            if self.buzzer:
                self.buzzer.tone(800, 15)
        if sel:
            # B: 快速开关该组闹钟 (即改即存)
            a = self._alarms[self._alarm_list_sel]
            a["en"] = not a.get("en")
            self._alarm_persist()
            self._alarm_list_draw_item(self._alarm_list_sel)
            if self.buzzer:
                self.buzzer.tone(1200 if a.get("en") else 600, 40)
        if down:
            self._page = "menu"
            self._menu_enter()
            return True
        time.sleep_ms(40)
        return True
