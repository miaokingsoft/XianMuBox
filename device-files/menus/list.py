"""
多功能终端框架 - 主菜单 (iOS Edition)
闲沐工坊 · 专业品质 · 240x240

接口：run_menu(display, buttons, buzzer=None) -> mode_id

设计参考 iOS Settings.app:
  - 卡片式选中态 (浅蓝灰底 + 白色 1px 描边)
  - 卡片顶部高光 + 底部阴影 (拟物深度)
  - 右侧 chevron › (iOS 通用进入指示)
  - 顶部 WiFi 状态文字
  - Page Control 页码点
  - 5+ 项时支持滚动 (选中项始终可见)

布局 (240x240):
  y=0~15   WiFi 状态文字
  y=20~200 4 个菜单项 (45px 等距, 滚动)
  y=208    底部滚动三角
  y=220    Page Control

操控:  A=UP  B=OK  C=DOWN
"""

import time, gc

from framework.colors import (
    CLR_BG, CLR_CARD, CLR_CARD_HI, CLR_CARD_SH,
    CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_GREEN, CLR_RED,
)
from framework.wifi import check_wifi
from app_config import MENU_ITEMS

try:
    from font import vga1_8x8 as SUB_FONT
except ImportError:
    SUB_FONT = None

from font import hanzi_24 as hanzi24

# 全局闹钟服务 (惰性加载: False=未尝试, None=不可用, 否则为模块)
_ALARM_SVC = False
# 全局整点新闻播报服务 (惰性加载, 同上): 主菜单画面整点自动播报
_NEWS_SVC = False
# 云平台服务 (惰性加载, 同上): 主菜单空闲期心跳调度 + 绑定码接管
_CLOUD_SVC = False
# HTTP 轮询 (惰性加载, 同上): 主菜单也能接收 Web 端配置保存
_HTTP_POLL = False

MENU_COUNT = len(MENU_ITEMS)

# ===================== 布局常量 =====================
STATUS_Y    = 4
CARD_MARGIN = 8
CARD_X      = CARD_MARGIN
CARD_W      = 240 - 2 * CARD_MARGIN
ICON_X      = 18
ICON_W      = 12
ICON_H      = 16
MENU_X_TXT  = 42
MENU_X_CHE  = 240 - 22
CHE_W       = 4
CHE_H       = 8
MENU_Y0     = 20
ITEM_STEP   = 45
VIEW_ITEMS  = 4
TRI_BOT_Y   = 208
TRI_W       = 5
TRI_H       = 3
DOTS_Y      = 220
DOT_S       = 4
DOT_GAP     = 8

# 滚动状态 (模块级)
scroll_offset = 0


# ===================== 绘图函数 =====================

def _draw_chevron(display, x, y, color):
    display.line(x, y, x + CHE_W, y + CHE_H // 2, color)
    display.line(x, y + CHE_H, x + CHE_W, y + CHE_H // 2, color)


def _draw_triangle(display, x, y, w, h, direction, color):
    if direction == 0:
        i = 0
        while i < h:
            display.hline(x + i, y + i, w - 2 * i, color)
            i += 1
    else:
        i = 0
        while i < h:
            display.hline(x + i, y + h - 1 - i, w - 2 * i, color)
            i += 1


def _draw_wifi_status(display, connected, ip):
    display.fill_rect(0, 0, 240, 16, CLR_BG)
    if not SUB_FONT:
        return

    if connected and ip:
        t = "IP:" + ip
        tw = len(t) * 8
        display.text(SUB_FONT, t, (240 - tw) // 2, STATUS_Y, CLR_GREEN, CLR_BG)
    else:
        x0 = (240 - 44) // 2
        display.line(x0, STATUS_Y, x0 + 7, STATUS_Y + 7, CLR_RED)
        display.line(x0 + 7, STATUS_Y, x0, STATUS_Y + 7, CLR_RED)
        display.text(SUB_FONT, "WiFi", x0 + 12, STATUS_Y, CLR_RED, CLR_BG)


def _draw_menu_item(display, item_idx, vi, selected):
    """item_idx: 全局菜单索引; vi: 可见槽位索引"""
    y = MENU_Y0 + vi * ITEM_STEP
    item = MENU_ITEMS[item_idx]
    text = item["name"]

    display.fill_rect(0, y, 240, ITEM_STEP, CLR_BG)

    if selected:
        cy = y + 1
        ch = ITEM_STEP - 2
        display.fill_rect(CARD_X, cy, CARD_W, ch, CLR_CARD)
        display.hline(CARD_X, cy, CARD_W, CLR_CARD_HI)
        display.hline(CARD_X, cy + ch - 1, CARD_W, CLR_CARD_SH)
        display.rect(CARD_X, cy, CARD_W, ch, CLR_WHITE)
        fg = CLR_WHITE
        bg = CLR_CARD
        chev_c = CLR_WHITE
    else:
        fg = CLR_GRAY
        bg = CLR_BG
        chev_c = CLR_DIM

    # 左侧标识色块 (颜色从菜单项配置中读取)
    ic_y = y + (ITEM_STEP - ICON_H) // 2
    color = item["color"]
    display.fill_rect(ICON_X, ic_y, ICON_W, ICON_H, color)
    if selected:
        display.rect(ICON_X, ic_y, ICON_W, ICON_H, CLR_WHITE)

    # 中文标题
    _cn_h = hanzi24.HEIGHT
    ty = y + (ITEM_STEP - _cn_h) // 2
    display.write(hanzi24, text, MENU_X_TXT, ty, fg, bg)

    # 右侧 chevron
    cy = y + (ITEM_STEP - CHE_H) // 2
    _draw_chevron(display, MENU_X_CHE, cy, chev_c)


def _draw_scroll_indicators(display):
    global scroll_offset
    if scroll_offset + VIEW_ITEMS < MENU_COUNT:
        tx = (240 - TRI_W) // 2
        _draw_triangle(display, tx, TRI_BOT_Y, TRI_W, TRI_H, 1, CLR_DIM)


def _draw_dots(display, selected):
    total = MENU_COUNT * DOT_S + (MENU_COUNT - 1) * DOT_GAP
    sx = (240 - total) // 2
    i = 0
    while i < MENU_COUNT:
        x = sx + i * (DOT_S + DOT_GAP)
        if i == selected:
            display.fill_rect(x, DOTS_Y, DOT_S, DOT_S, CLR_CYAN)
        else:
            display.rect(x, DOTS_Y, DOT_S, DOT_S, CLR_DIM)
        i += 1


def _update_dots(display, selected):
    display.fill_rect(0, DOTS_Y - 2, 240, DOT_S + 4, CLR_BG)
    _draw_dots(display, selected)


def _update_scroll_offset(selected):
    global scroll_offset
    if selected < scroll_offset:
        scroll_offset = selected
    elif selected >= scroll_offset + VIEW_ITEMS:
        scroll_offset = selected - VIEW_ITEMS + 1


def _redraw_frame(display, selected, wf_ok, wf_ip):
    """整帧重绘菜单 (闹钟/绑定码全屏接管结束后的画面恢复)"""
    display.fill(CLR_BG)
    display.fill_rect(0, 0, 240, 16, CLR_BG)
    vi = 0
    while vi < VIEW_ITEMS:
        item_idx = scroll_offset + vi
        if item_idx < MENU_COUNT:
            _draw_menu_item(display, item_idx, vi, item_idx == selected)
        vi += 1
    _draw_scroll_indicators(display)
    _draw_dots(display, selected)
    _draw_wifi_status(display, wf_ok, wf_ip)


# ===================== 主循环 =====================

def run_menu(display, buttons, buzzer=None):
    """
    启动 iOS 风格列表菜单。
    返回选中的菜单项 ID (字符串)。

    参数：
      display  ST7789 显示对象
      buttons  Buttons 实例
      buzzer   Buzzer 实例或 None
    """
    global scroll_offset, _ALARM_SVC, _NEWS_SVC, _CLOUD_SVC, _HTTP_POLL
    gc.collect()

    selected = 0
    scroll_offset = 0
    loop_cnt = 0
    wf_checked = False  # 延迟 WiFi 查询到首帧渲染后
    wf_ok = False
    wf_ip = ""

    # === 一次性绘制（先渲染菜单框架，WiFi 状态留空） ===
    display.fill(CLR_BG)
    # WiFi 状态栏先留空，避免 check_wifi() 阻塞导致黑屏
    display.fill_rect(0, 0, 240, 16, CLR_BG)

    vi = 0
    while vi < VIEW_ITEMS:
        item_idx = scroll_offset + vi
        if item_idx < MENU_COUNT:
            _draw_menu_item(display, item_idx, vi, item_idx == selected)
        vi += 1

    _draw_scroll_indicators(display)
    _draw_dots(display, selected)

    # === 主循环 ===
    while True:
        loop_cnt += 1
        if loop_cnt % 100 == 0:
            gc.collect()

        # 首帧渲染完成后异步查询 WiFi，避免阻塞菜单显示
        if not wf_checked:
            wf_checked = True
            wf_ok, wf_ip = check_wifi()
            _draw_wifi_status(display, wf_ok, wf_ip)

        # 全局闹钟检查: 主菜单画面也覆盖 (设备停在菜单时不留盲区)。
        # 响铃整屏接管后菜单帧被覆盖, 这里整帧重绘恢复
        if _ALARM_SVC is False:
            try:
                from framework import alarm as _svc
                _ALARM_SVC = _svc
            except Exception:
                _ALARM_SVC = None
        if _ALARM_SVC:
            try:
                if _ALARM_SVC.check(display, buttons, buzzer):
                    _redraw_frame(display, selected, wf_ok, wf_ip)
            except Exception:
                pass

        # 全局整点新闻播报: 覆盖主菜单画面 (v1.5, 同闹钟接管机制)。
        # 播报整屏接管后菜单帧被覆盖, 这里整帧重绘恢复
        if _NEWS_SVC is False:
            try:
                from framework import news as _svc
                _NEWS_SVC = _svc
            except Exception:
                _NEWS_SVC = None
        if _NEWS_SVC:
            try:
                if _NEWS_SVC.check(display, buttons, buzzer):
                    _redraw_frame(display, selected, wf_ok, wf_ip)
            except Exception:
                pass

        # 云平台服务: 空闲期心跳调度 (v1.4.1 起绑定码不再全屏接管,
        # 由本机设置「云端激活」子页轮询展示; 未配置密钥时短路零开销)
        if _CLOUD_SVC is False:
            try:
                from framework import cloud as _svc
                _CLOUD_SVC = _svc
            except Exception:
                _CLOUD_SVC = None
        if _CLOUD_SVC:
            try:
                if _CLOUD_SVC.ready():
                    _CLOUD_SVC.tick()
            except Exception:
                pass

        # Web 配置生效: 仅当设置页开启过 HTTP 服务时轮询请求
        # (通过轻量 http_server_state 探测, 平时零内存/零开销),
        # 让设备停在主菜单时 Web 端的保存也能落地
        if _HTTP_POLL is False:
            try:
                from framework import http_server_state as _hss
                if getattr(_hss, "_http_server_sock", None) is not None:
                    from framework.http_server import poll_http_server as _fn
                    _HTTP_POLL = _fn
            except Exception:
                _HTTP_POLL = None
        if _HTTP_POLL:
            try:
                _HTTP_POLL()
            except Exception:
                pass

        up, sel, down = buttons.scan()

        if sel:
            if buzzer:
                buzzer.tone(1200, 30)
            cur_vi = selected - scroll_offset
            y = MENU_Y0 + cur_vi * ITEM_STEP
            cy = y + 1
            ch = ITEM_STEP - 2
            flash = 0
            while flash < 2:
                display.fill_rect(CARD_X, cy, CARD_W, ch, CLR_WHITE)
                time.sleep_ms(40)
                _draw_menu_item(display, selected, cur_vi, True)
                time.sleep_ms(40)
                flash += 1
            return MENU_ITEMS[selected]["id"]

        if up or down:
            old_idx = selected
            old_offset = scroll_offset
            old_vi = old_idx - old_offset
            if up:
                selected = (selected - 1) % MENU_COUNT
            else:
                selected = (selected + 1) % MENU_COUNT
            _update_scroll_offset(selected)
            new_vi = selected - scroll_offset

            if scroll_offset != old_offset:
                vi = 0
                while vi < VIEW_ITEMS:
                    item_idx = scroll_offset + vi
                    if item_idx < MENU_COUNT:
                        _draw_menu_item(display, item_idx, vi, item_idx == selected)
                    vi += 1
                display.fill_rect(0, TRI_BOT_Y, 240, TRI_H, CLR_BG)
                _draw_scroll_indicators(display)
            else:
                _draw_menu_item(display, old_idx, old_vi, False)
                _draw_menu_item(display, selected, new_vi, True)

            _update_dots(display, selected)
            if buzzer:
                buzzer.tone(800, 15)

        time.sleep_ms(30)
