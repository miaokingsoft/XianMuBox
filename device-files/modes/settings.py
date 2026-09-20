"""
多功能终端框架 - 本机设置
闲沐工坊 · iOS Edition

功能:
  - 设置项列表 (iOS 风格卡片式):
      1. 无线网络  - 显示 SSID/IP, 查看连接状态
      2. WiFi配网  - SoftAP 热点 + 二维码配网页 (framework/provision)
      3. 屏幕亮度  - B 键循环切换 (25%/50%/75%/100%)
      4. HTTP服务  - 全屏二维码子页, 扫码直达 Web 配置页;
                     页内 B 键开关服务, WiFi 未连接时等待连上自动启动
      5. 云端激活  - 云平台状态/大字 SN/激活码展示, 打开期间自动轮询,
                     B 键手动激活
      6. 关于本机  - 显示版本和内存信息
      7. 恢复出厂  - 二次确认后重启设备
      8. 返回主菜单  - 返回主菜单

  注: 时区固定为北京时间 UTC+8, 不再提供设置项
  - 列表交互:
      A 短按: 上移光标
      C 短按: 下移光标
      B 短按: 操作当前项
"""

import time
import gc
import sys
from font import hanzi_24 as hanzi24

from .base import BaseMode, SUB_FONT
from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_CYAN,
    CLR_CARD, CLR_CARD_HI, CLR_CARD_SH, CLR_DIM, CLR_AMBER,
    CLR_GREEN, CLR_RED, CLR_MAGENTA, CLR_BLACK,
)
try:
    from config import W, H, TFT_BLK
except ImportError:
    W, H = 240, 240
    TFT_BLK = 7

try:
    from framework import settings as _settings
except ImportError:
    _settings = None


# ===================== 亮度档位 (百分比值, PWM duty) =====================
_BRIGHT_LEVELS = (
    (25,  256),
    (50,  512),
    (75,  768),
    (100, 1023),
)

def _brightness_to_idx(pct):
    """把亮度百分比映射到 _BRIGHT_LEVELS 的索引，未知值回退到 100%(idx=3)"""
    for i, (p, _) in enumerate(_BRIGHT_LEVELS):
        if p == pct:
            return i
    return len(_BRIGHT_LEVELS) - 1

# ===================== 布局常量 =====================
# 屏幕分区:
#   y=0~31   标题 "本机设置" 区域 (32px, 文字垂直居中)
#   y=32     分隔线 (1px)
#   y=33~    设置项列表 (到底)
ITEM_X      = 8       # 列表项左边距 (卡片距屏幕边缘 8px)
ITEM_W      = W - 16  # 列表项宽度 (两侧各留 8px)
ICON_X      = 16      # 左侧色块 x 坐标 (卡片内左边距 6px)
MENU_X_TXT  = 42      # 主标签文字 x 坐标 (色块 14 + 宽 12 + 间距 16)
MENU_X_CHE  = W - 22  # 右侧 chevron 箭头 x 坐标
ITEM_Y0     = 37      # 列表第一行 y 起始 (标题区 32 + 分隔线 1)
ITEM_STEP   = 45      # 每行高度 (含间距)
ICON_W      = 12      # 左侧色块宽度
ICON_H      = 16      # 左侧色块高度
CHE_W       = 4       # chevron 箭头宽度
CHE_H       = 8       # chevron 箭头高度
TITLE_Y     = 5       # 标题文字 y (32px 区域内垂直居中, (32-25)/2≈3)
SEP_Y       = 35      # 分隔线 y (标题区底部)


def _calc_view_count():
    """根据屏幕高度计算可见项目数"""
    return max(1, (H - ITEM_Y0) // ITEM_STEP)


# ===================== 大字绘制辅助 (云端激活子页, v1.4.1) =====================
# sy_32x32: 32px ASCII/数字字库, 用于设备号 SN 与激活码的大字展示
_CLOUD_POLL_MS = 10000      # 子页打开期间激活码轮询间隔
_SY32 = None                # 惰性加载缓存 (False = 不可用)


def _get_sy32():
    global _SY32
    if _SY32 is None:
        try:
            from font import sy_32x32 as f
            _SY32 = f
        except ImportError:
            _SY32 = False
    return _SY32


def _sym_w(font, ch):
    try:
        return font._WIDTHS[font.MAP.index(ch)]
    except ValueError:
        return font.MAX_WIDTH


def _sym_text_w(font, text):
    w = 0
    for ch in text:
        w += _sym_w(font, ch)
    return w


def _draw_sym_center(d, font, text, y, fg, bg):
    """32px ASCII/数字整串居中绘制 (缺字跳过)"""
    x = (W - _sym_text_w(font, text)) // 2
    for ch in text:
        if ch in font.MAP:
            d.write(font, ch, x, y, fg, bg)
        x += _sym_w(font, ch)


def _sym_lines(font, text, max_w):
    """长串按实际字宽贪心拆行 (SN 整串超屏宽时分行)"""
    lines = []
    cur = ""
    cur_w = 0
    for ch in text:
        w = _sym_w(font, ch)
        if cur and cur_w + w > max_w:
            lines.append(cur)
            cur = ch
            cur_w = w
        else:
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    return lines


def _draw_chevron(d, x, y, color):
    """绘制 chevron '>'"""
    d.line(x, y, x + CHE_W, y + CHE_H // 2, color)
    d.line(x, y + CHE_H, x + CHE_W, y + CHE_H // 2, color)


# ===================== WiFi 信号图标动画 =====================
# 圆弧半径 (px)，由内到外 4 圈
_WIFI_ARC_RADII = (6, 14, 22, 30)
# 圆弧分段数（越多越平滑，每段 ~5.6 度）
_WIFI_ARC_SEG = 14
# sin/cos 查表缓存 (模块级全局变量; MicroPython 函数对象不支持动态属性)
_WIFI_SIN_TABLE = None
_WIFI_COS_TABLE = None


def _draw_arc(d, cx, cy, r, color):
    """绘制以 (cx, cy) 为底部中心、半径 r 的上半圆弧 (180 度)"""
    global _WIFI_SIN_TABLE, _WIFI_COS_TABLE
    if r <= 0:
        return
    # 懒加载 sin/cos 查表 (首次调用时计算, 之后复用)
    if _WIFI_SIN_TABLE is None:
        import math
        seg = _WIFI_ARC_SEG
        _WIFI_SIN_TABLE = [0] * (seg + 1)
        _WIFI_COS_TABLE = [0] * (seg + 1)
        for i in range(seg + 1):
            ang = math.pi * i / seg  # 0..pi
            _WIFI_SIN_TABLE[i] = int(math.sin(ang) * 100)
            _WIFI_COS_TABLE[i] = -int(math.cos(ang) * 100)  # 上半圆 cos 为负
    sin_t = _WIFI_SIN_TABLE
    cos_t = _WIFI_COS_TABLE
    px = cx
    py = cy
    for i in range(1, _WIFI_ARC_SEG + 1):
        nx = cx + (r * sin_t[i]) // 100
        ny = cy + (r * cos_t[i]) // 100
        d.line(px, py, nx, ny, color)
        px, py = nx, ny


def _draw_wifi_icon(d, cx, cy, level, color=CLR_CYAN):
    """
    绘制 WiFi 信号图标 (同心弧 + 中心点)
    cx, cy  - 中心底部坐标
    level   - 0..5  (0=无信号, 5=满格)
    color   - 弧/中心点颜色
    """
    # 清空图标区域 (整个图标约 64x40, 留足边距)
    d.fill_rect(cx - 32, cy - 32, 64, 40, CLR_BG)
    # 中心点（位于底部中心，4x4 像素）—— 始终亮起（只要 level>=1）
    if level >= 1:
        d.fill_rect(cx - 2, cy - 2, 4, 4, color)
    # 4 圈弧（仅当 level 达到对应阈值时显示）
    for i, r in enumerate(_WIFI_ARC_RADII):
        if level >= i + 2:
            _draw_arc(d, cx, cy, r, color)


class SettingsMode(BaseMode):
    """本机设置模式"""

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._idx = 0
        self._scroll = 0
        self._popup = None        # None / "wait" / "confirm"
        self._popup_ts = 0
        self._view_count = _calc_view_count()

        # 亮度状态: 从 settings.json 恢复 (默认 100%)
        saved_pct = 100
        if _settings is not None:
            try:
                saved_pct = int(_settings.get("brightness", 100))
            except Exception:
                saved_pct = 100
        self._bright_idx = _brightness_to_idx(saved_pct)
        self._bl_pwm = None

        # 重置二次确认
        self._reset_confirm = False
        self._reset_ts = 0

        # HTTP 服务等待 WiFi 连接
        self._http_wait_wifi = False
        self._http_wait_ts = 0

        # WiFi 连接子页面状态
        # None = 不在子页面, 'connecting' = 连接动画中, 'done' = 已结束等待返回
        self._wifi_state = None
        self._wifi_connector = None
        self._wifi_last_ssid = None
        self._wifi_last_level = -1
        self._wifi_done_ts = 0

        # 设置项 (实例级, 可动态修改 detail/color)
        bright_pct = _BRIGHT_LEVELS[self._bright_idx][0]
        self._items = [
            {"name": "无线网络", "detail": "",            "color": CLR_CYAN,    "id": "wifi"},
            {"name": "WiFi配网", "detail": "",            "color": CLR_CYAN,    "id": "provision"},
            {"name": "屏幕亮度", "detail": "%d%%" % bright_pct, "color": CLR_AMBER,   "id": "brightness"},
            {"name": "HTTP服务", "detail": "Stopped",    "color": CLR_GREEN,  "id": "http_server"},
            {"name": "云端激活", "detail": "",            "color": CLR_CYAN,    "id": "cloud"},
            {"name": "关于本机", "detail": "",            "color": CLR_GRAY,    "id": "about"},
            {"name": "恢复出厂", "detail": "",            "color": CLR_MAGENTA, "id": "reset"},
            {"name": "退出", "detail": "",          "color": CLR_WHITE,   "id": "back"},
        ]
        # 延迟刷新: _refresh_http_detail 会 import http_server (socket+json ~16KB),
        # 在 __init__ 时内存碎片可能不够; 推迟到 enter() 中 gc.collect() 之后执行
        self._need_refresh_http = True
        self._refresh_wifi_detail()
        self._refresh_about_detail()
        self._refresh_cloud_detail()

    # ==================== 生命周期 ====================

    def enter(self):
        """进入: 清屏 + 绘制标题 + 首次列表"""
        d = self.display
        d.fill(CLR_BG)

        # 标题 "本机设置" (hanzi24 每字 24px 宽)
        title = "本机设置"
        tw = len(title) * 24
        d.write(hanzi24, title, (W - tw) // 2, TITLE_Y, CLR_WHITE, CLR_BG)

        # 分隔线
        d.hline(ITEM_X, SEP_Y, ITEM_W, CLR_DIM)

        self._popup = None
        self._reset_confirm = False
        self._http_wait_wifi = False
        self._http_wait_ts = 0

        # 重置 WiFi 子状态（若上次未正常返回）
        self._wifi_state = None
        self._wifi_connector = None
        self._wifi_last_ssid = None
        self._wifi_last_level = -1
        self._wifi_done_ts = 0

        # 延迟刷新 http_server 状态（__init__ 时跳过的重型导入，
        # 此处 display 初始化已完成、gc.collect 后内存碎片更充分）
        if self._need_refresh_http:
            self._need_refresh_http = False
            gc.collect()
            self._refresh_http_detail()

        self._draw_all_items()

    def exit(self):
        """退出: 恢复背光到满亮度"""
        if self._bl_pwm is not None:
            try:
                self._bl_pwm.duty(1023)
            except Exception:
                pass
            self._bl_pwm = None

    # ==================== 列表绘制 ====================

    def _draw_item(self, vi, selected, item):
        """绘制单个设置项卡片"""
        d = self.display
        y = ITEM_Y0 + vi * ITEM_STEP

        # 1. 清行
        d.fill_rect(0, y, W, ITEM_STEP, CLR_BG)

        if selected:
            # 选中态: iOS 卡片
            cy = y + 1
            ch = ITEM_STEP - 2
            d.fill_rect(ITEM_X, cy, ITEM_W, ch, CLR_CARD)
            d.hline(ITEM_X, cy, ITEM_W, CLR_CARD_HI)
            d.hline(ITEM_X, cy + ch - 1, ITEM_W, CLR_CARD_SH)
            d.rect(ITEM_X, cy, ITEM_W, ch, CLR_WHITE)
            fg = CLR_WHITE
            bg = CLR_CARD
            chev_c = CLR_WHITE
        else:
            fg = CLR_GRAY
            bg = CLR_BG
            chev_c = CLR_DIM

        # 2. 左侧色块
        ic_y = y + (ITEM_STEP - ICON_H) // 2
        d.fill_rect(ICON_X, ic_y, ICON_W, ICON_H, item["color"])
        if selected:
            d.rect(ICON_X, ic_y, ICON_W, ICON_H, CLR_WHITE)

        # 3. 主标签 (hanzi24)
        _cn_h = hanzi24.HEIGHT  # 25
        if item["detail"]:
            # 有副标题: 主标签上移, 为副标题留空间
            ty = y + 4
        else:
            # 无副标题: 主标签垂直居中
            ty = y + (ITEM_STEP - _cn_h) // 2
        d.write(hanzi24, item["name"], MENU_X_TXT, ty, fg, bg)

        # 副标题 (8x8)
        if item["detail"]:
            d.text(SUB_FONT, item["detail"],
                   MENU_X_TXT, ty + _cn_h + 2, fg, bg)

        # 4. chevron
        cy = y + (ITEM_STEP - CHE_H) // 2
        _draw_chevron(d, MENU_X_CHE, cy, chev_c)

    def _draw_all_items(self):
        """重绘可见列表"""
        vi = 0
        while vi < self._view_count:
            idx = self._scroll + vi
            if idx < len(self._items):
                self._draw_item(vi, idx == self._idx, self._items[idx])
            else:
                # 清除多余空行
                y = ITEM_Y0 + vi * ITEM_STEP
                self.display.fill_rect(0, y, W, ITEM_STEP, CLR_BG)
            vi += 1

    def _adjust_scroll(self):
        """调整滚动偏移, 确保选中项可见"""
        if self._idx < self._scroll:
            self._scroll = self._idx
        elif self._idx >= self._scroll + self._view_count:
            self._scroll = self._idx - self._view_count + 1

    def _redraw_list_page(self):
        """清屏重绘设置列表页 (标题 + 分隔线 + 全部条目), 供子页返回时恢复"""
        d = self.display
        d.fill(CLR_BG)
        d.write(hanzi24, "本机设置", (W - 4 * 24) // 2, TITLE_Y,
                CLR_WHITE, CLR_BG)
        d.hline(ITEM_X, SEP_Y, ITEM_W, CLR_DIM)
        self._draw_all_items()

    # ==================== 弹窗 ====================

    def _show_popup(self, lines):
        """弹出信息对话框
        lines: [(text, color), ...]
        """
        d = self.display
        n = len(lines)
        pw, ph = 160, max(60, 28 + n * 18)
        px = (W - pw) // 2
        py = (H - ph) // 2
        d.fill_rect(px, py, pw, ph, 0x18C3)
        d.rect(px, py, pw, ph, CLR_WHITE)
        d.rect(px + 1, py + 1, pw - 2, ph - 2, CLR_CYAN)
        line_y = py + 12
        for text, color in lines:
            lw = len(text) * 8
            if SUB_FONT:
                d.text(SUB_FONT, text, (W - lw) // 2, line_y, color, 0x18C3)
            line_y += 18
        self._popup = "wait"
        self._popup_ts = time.ticks_ms()

    def _hide_popup(self):
        """关闭弹窗, 重绘列表"""
        self._popup = None
        self._reset_confirm = False
        d = self.display
        d.fill_rect(0, ITEM_Y0, W, self._view_count * ITEM_STEP, CLR_BG)
        self._draw_all_items()

    # ==================== 设置项操作 ====================

    def _handle_item(self, item):
        """分发设置项操作"""
        item_id = item.get("id", "")
        if item_id == "wifi":
            self._action_wifi()
        elif item_id == "provision":
            self._action_provision()
        elif item_id == "brightness":
            self._action_brightness()
        elif item_id == "http_server":
            self._action_http_qr()
        elif item_id == "cloud":
            self._action_cloud()
        elif item_id == "about":
            self._action_about()
        elif item_id == "reset":
            self._action_reset()
        elif item_id == "back":
            self._reset_confirm = False
            self.flash_exit()
            return False
        else:
            self._show_popup([(item["name"], CLR_WHITE),
                              ("N/A", CLR_AMBER)])
            self.beep(1200, 30)

    def _action_wifi(self):
        """无线网络:
        - 已连接 -> 弹窗显示 IP (维持原行为)
        - 未连接 -> 进入 WiFi 连接动画子页面
        """
        # 防重复触发: 动画进行中再次按 B 忽略
        if self._wifi_state == 'connecting':
            return
        try:
            from framework.wifi import check_wifi
            ok, ip = check_wifi()
        except ImportError:
            self._show_popup([("WiFi", CLR_WHITE), ("N/A", CLR_RED)])
            self.beep(800, 30)
            return

        if ok:
            # 已连接: 弹窗显示 IP (2 秒自动关闭)
            self._show_popup([
                ("WiFi Connected", CLR_GREEN),
                ("IP: " + (ip or ""), CLR_WHITE),
            ])
            self.beep(800, 30)
        else:
            # 未连接: 启动连接动画
            self._start_wifi_connect()

    def _action_provision(self):
        """WiFi配网: 进入 SoftAP 热点配网页 (阻塞式子页)。

        设备开热点 XianMuBox-XXXX, 屏幕显示二维码, 手机连接后自动
        弹出配网页选择 WiFi; 成功或 C 键退出后返回设置列表。
        """
        try:
            from framework.provision import run_provision_page
        except ImportError:
            self._show_popup([("Provision", CLR_WHITE), ("N/A", CLR_RED)])
            self.beep(800, 30)
            return

        ok, ip = run_provision_page(self.display, self.buttons, self.buzzer)
        # 回到列表前刷新 WiFi 项状态 (配网成功后 IP 已变化)
        self._refresh_wifi_detail()
        self._redraw_list_page()
        if ok:
            self._show_popup([
                ("WiFi Connected", CLR_GREEN),
                ("IP: " + (ip or ""), CLR_WHITE),
            ])
        self.beep(1000, 20)

    # ==================== WiFi 连接子页面 ====================

    def _start_wifi_connect(self):
        """启动 WiFi 连接动画子页面 (清屏 + 初始化 connector)"""
        try:
            from framework.wifi import WiFiConnector
        except ImportError:
            self._show_popup([("WiFi", CLR_WHITE), ("N/A", CLR_RED)])
            self.beep(800, 30)
            return

        d = self.display
        d.fill(CLR_BG)

        # 顶部标题 "WiFi"
        title = "WiFi"
        tw = len(title) * 8
        d.text(SUB_FONT, title, (W - tw) // 2, 8, CLR_WHITE, CLR_BG)

        # 初始 SSID 文字区占位
        d.fill_rect(0, 168, W, 12, CLR_BG)

        # 初始状态文字: "Starting..." (start() 会阻塞 ~500ms 做 disconnect+connect)
        d.fill_rect(0, H - 24, W, 12, CLR_BG)
        d.text(SUB_FONT, "Starting...",
               (W - 10 * 8) // 2, H - 22, CLR_AMBER, CLR_BG)

        # 初始图标 (无信号)
        _draw_wifi_icon(d, W // 2, H // 2 + 16, 1, CLR_DIM)

        # 诊断: 打印 wifi 配置数量
        try:
            from framework.settings import get_wifi_configs
            configs = get_wifi_configs()
            print("[WIFI] start connect, configs:", len(configs))
            for c in configs:
                print("[WIFI]   ssid:", c.get("ssid", ""))
        except Exception as e:
            print("[WIFI] cannot read configs:", e)

        self._wifi_state = 'connecting'
        self._wifi_connector = WiFiConnector()
        self._wifi_connector.start()  # 内部已处理 disconnect 清除 stale 状态
        self._wifi_last_ssid = None
        self._wifi_last_level = 1
        self._wifi_log_ts = 0  # 诊断日志时间戳
        self.beep(800, 20)

    def _wifi_connect_loop(self):
        """WiFi 连接动画主循环 (在 SettingsMode.loop() 中每帧调用)"""
        # 1. 处理按键取消 (任意短按)
        up, sel, down, _, _, _ = self._scan_with_timing()
        if up or sel or down:
            self._cancel_wifi_connect()
            return True

        # 2. 推进一步
        try:
            self._wifi_connector.tick()
        except Exception:
            pass

        # 3. 读取当前状态
        try:
            ssid = self._wifi_connector.current_ssid
        except Exception:
            ssid = None
        try:
            progress = self._wifi_connector.progress
        except Exception:
            progress = 0
        try:
            state = self._wifi_connector.state
        except Exception:
            state = 'connecting'

        # 4. 渲染 (绘制异常不影响连接流程, 仅打印日志)
        try:
            self._render_wifi_frame(ssid, progress, state)
        except Exception as e:
            print("[WIFI] render error:", e)

        # 5. 完成判定
        if state in ('success', 'failed'):
            self._finish_wifi_connect()
            return True

        # 6. 诊断日志 (每 1 秒打印一次状态, 方便排查)
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, self._wifi_log_ts) > 1000:
            self._wifi_log_ts = now_ms
            try:
                print("[WIFI] state:", state,
                      "ssid:", self._wifi_connector.current_ssid,
                      "progress:", self._wifi_connector.progress)
            except Exception:
                pass

        time.sleep_ms(50)  # ~20 FPS
        return True

    def _render_wifi_frame(self, ssid, progress, state):
        """渲染一帧 WiFi 连接动画 (SSID 文字 + 图标 + 状态文字)"""
        d = self.display

        # SSID 文字 (变化时才重绘)
        if ssid != self._wifi_last_ssid:
            self._wifi_last_ssid = ssid
            d.fill_rect(0, 168, W, 12, CLR_BG)
            text = "WiFi: " + (ssid or "...")
            tw = len(text) * 8
            if tw > W:
                tw = W
            d.text(SUB_FONT, text, (W - tw) // 2, 170,
                   CLR_CYAN, CLR_BG)

        # 图标动画级别 (变化时才重绘)
        level = min(5, max(1, progress // 20 + 1))
        if level != self._wifi_last_level:
            self._wifi_last_level = level
            _draw_wifi_icon(d, W // 2, H // 2 + 16,
                            level, CLR_CYAN)

        # 状态文字 (connecting 时显示)
        if state == 'connecting':
            d.fill_rect(0, H - 24, W, 12, CLR_BG)
            d.text(SUB_FONT, "Connecting...",
                   (W - 12 * 8) // 2, H - 22,
                   CLR_AMBER, CLR_BG)

    def _finish_wifi_connect(self):
        """WiFi 连接完成: 显示结果 1.5 秒后自动返回列表"""
        try:
            ok, ip = self._wifi_connector.result
        except Exception:
            ok, ip = (False, None)
        d = self.display

        # 失败时打印诊断信息 (串口可见)
        if not ok:
            try:
                from framework.wifi import _get_wlan
                wlan = _get_wlan()
                print("[WIFI] connect FAILED")
                print("[WIFI]   wlan active:", wlan.active())
                print("[WIFI]   wlan connected:", wlan.isconnected())
                if wlan.isconnected():
                    print("[WIFI]   ifconfig:", wlan.ifconfig())
            except Exception as e:
                print("[WIFI] diag error:", e)

        # 重绘最终图标 (满格绿 或 全暗红)
        _draw_wifi_icon(d, W // 2, H // 2 + 16,
                        5 if ok else 1,
                        CLR_GREEN if ok else CLR_RED)

        # 状态文字
        d.fill_rect(0, H - 24, W, 12, CLR_BG)
        if ok:
            text = "OK! " + (ip or "")
            d.text(SUB_FONT, text, (W - len(text) * 8) // 2,
                   H - 22, CLR_GREEN, CLR_BG)
            self.beep(1200, 30)
        else:
            d.text(SUB_FONT, "Failed",
                   (W - 6 * 8) // 2, H - 22, CLR_RED, CLR_BG)
            self.beep(400, 60)

        self._wifi_state = 'done'
        self._wifi_done_ts = time.ticks_ms()

    def _check_wifi_done_return(self):
        """done 状态 1.5 秒后自动返回列表"""
        if self._wifi_state != 'done':
            return
        if time.ticks_diff(time.ticks_ms(), self._wifi_done_ts) > 1500:
            # 刷新列表项 (IP/颜色)
            self._refresh_wifi_detail()
            # 重绘整个设置列表 (恢复标题 + 列表)
            self._redraw_list_page()
            self._wifi_state = None
            self._wifi_connector = None
            self.beep(1000, 20)

    def _cancel_wifi_connect(self):
        """用户按键取消: 立即停止 + 返回列表"""
        # 断开正在进行的连接尝试, 避免后台继续跑
        try:
            wlan = self._wifi_connector._wlan
            if wlan:
                try:
                    wlan.disconnect()
                except Exception:
                    pass
        except Exception:
            pass
        self._wifi_state = None
        self._wifi_connector = None
        self._redraw_list_page()
        self.beep(500, 30)

    def _action_brightness(self):
        """屏幕亮度: 循环切换 25%/50%/75%/100%"""
        self._bright_idx = (self._bright_idx + 1) % len(_BRIGHT_LEVELS)
        pct, duty = _BRIGHT_LEVELS[self._bright_idx]

        try:
            from machine import Pin, PWM
            if self._bl_pwm is None:
                self._bl_pwm = PWM(Pin(TFT_BLK), freq=1000, duty=duty)
            else:
                self._bl_pwm.duty(duty)
        except Exception as e:
            print("[SETTINGS] Brightness error:", e)

        # 持久化到 settings.json
        if _settings is not None:
            try:
                _settings.update({"brightness": pct})
            except Exception as e:
                print("[SETTINGS] Save brightness error:", e)

        self._items[2]["detail"] = str(pct) + "%"
        self._draw_all_items()
        self.beep(800 + self._bright_idx * 100, 30)

    def _action_http_qr(self):
        """HTTP 服务: 进入二维码子页, 扫码直达 Web 配置页

        WiFi 已连接 -> 确保服务启动后显示 URL 二维码;
        未连接      -> 沿用等待弹窗, 连上后自动启动 (loop 状态机)。
        """
        try:
            from framework.http_server import (
                start_http_server, is_running
            )
            from framework.wifi import check_wifi
        except ImportError:
            self._show_popup([("HTTP", CLR_WHITE), ("N/A", CLR_RED)])
            self.beep(800, 30)
            return

        ok, ip = check_wifi()
        if not ok:
            self._http_wait_wifi = True
            self._http_wait_ts = time.ticks_ms()
            self._show_popup([
                ("WiFi", CLR_WHITE),
                ("Waiting...", CLR_AMBER),
            ])
            self.beep(700, 30)
            return

        if not is_running():
            start_http_server()

        # 拼 URL: 默认 80 端口省略
        port = 80
        if _settings is not None:
            try:
                port = int(_settings.get("http_port", 80))
            except Exception:
                port = 80
        if port == 80:
            url = "http://" + (ip or "") + "/"
        else:
            url = "http://" + (ip or "") + ":%d/" % port
        self._http_qr_page(url)

    def _http_qr_page(self, url):
        """二维码子页 (阻塞式): B=开关服务, A/C=返回列表

        本子页阻塞了全局 tick, HTTP 请求由循环内 poll_http_server() 接管。
        """
        try:
            from framework.http_server import (
                start_http_server, stop_http_server, is_running,
                poll_http_server,
            )
        except ImportError:
            return

        self.beep(1200, 30)
        self._draw_http_qr(url, is_running())
        while True:
            up, sel, down, _bl, _al, _cl = self._scan_with_timing()
            if up or down:
                self.beep(900, 15)
                break
            if sel:
                if is_running():
                    stop_http_server()
                    self.beep(700, 30)
                else:
                    start_http_server()
                    self.beep(1200, 30)
                self._draw_http_qr_status(is_running())
                time.sleep_ms(200)
                continue
            try:
                poll_http_server()
            except Exception:
                pass
            time.sleep_ms(30)

        self._refresh_http_detail()
        self._redraw_list_page()

    def _draw_http_qr(self, url, running):
        """绘制二维码子页整页 (标题 + 二维码 + URL + 状态 + 按键提示)"""
        d = self.display
        d.fill(CLR_BG)
        d.write(hanzi24, "扫码访问", (W - 4 * 24) // 2, 4,
                CLR_WHITE, CLR_BG)
        d.hline(ITEM_X, 32, ITEM_W, CLR_DIM)

        try:
            from framework.qrcode import get_matrix
            n, rows = get_matrix(url)
        except ImportError:
            n, rows = None, None
        if n is None:
            if SUB_FONT:
                d.text(SUB_FONT, "QR ERROR",
                       (W - 8 * 8) // 2, H // 2, CLR_RED, CLR_BG)
            return

        # 矩阵四边已含 2 模块空白; scale=5 时 v2/v3 均落在 172px 预算内
        scale = 5
        qs = n * scale
        qx = (W - qs) // 2
        qy = 34 + (172 - qs) // 2
        d.fill_rect(qx, qy, qs, qs, CLR_WHITE)
        for y in range(n):
            row = rows[y]
            for x in range(n):
                if row[x]:
                    d.fill_rect(qx + x * scale, qy + y * scale,
                                scale, scale, CLR_BLACK)

        if SUB_FONT:
            line = url[:29]
            d.text(SUB_FONT, line, (W - len(line) * 8) // 2, H - 32,
                   CLR_CYAN, CLR_BG)
            self._draw_http_qr_status(running)
            d.text(SUB_FONT, "B:ON/OFF  A/C:BACK",
                   (W - 18 * 8) // 2, H - 10, CLR_GRAY, CLR_BG)

    def _draw_http_qr_status(self, running):
        """二维码子页服务状态行 (B 键切换时局部重绘)"""
        if not SUB_FONT:
            return
        d = self.display
        d.fill_rect(0, H - 20, W, 8, CLR_BG)
        st = "Running" if running else "Stopped"
        d.text(SUB_FONT, st, (W - len(st) * 8) // 2, H - 20,
               CLR_GREEN if running else CLR_RED, CLR_BG)

    def _start_http_and_refresh(self):
        """启动 HTTP 服务并刷新 UI/音效"""
        try:
            from framework.http_server import start_http_server
            start_http_server()
        except Exception:
            pass
        self._finish_http_toggle()

    def _finish_http_toggle(self):
        """HTTP 切换后统一刷新详情、列表和音效"""
        self._refresh_http_detail()
        self._draw_all_items()

        # 音效: 启动高音, 停止低音
        if self._items[3]["detail"] == "Running":
            self.beep(1200, 30)
        else:
            self.beep(700, 30)

    # ==================== 云端激活子页 (v1.4.1) ====================
    # 绑定流程唯一设备端入口: 状态 + 大字 SN + 激活码, 打开期间轮询

    def _refresh_cloud_detail(self):
        """刷新云端激活列表项的 detail (ASCII 摘要, 避免列表字体缺字)"""
        detail = "Idle"
        color = CLR_GRAY
        try:
            from framework import cloud as _c
            st = _c.status()
            detail = {"active": "OK", "binding": "BIND",
                      "unconfigured": "NoKey", "no_time": "NoNTP",
                      "disabled": "OFF"}.get(st.get("state", ""), "Idle")
            if st.get("err_code"):
                detail = "E" + str(st.get("err_code"))
            color = CLR_GREEN if detail == "OK" else CLR_GRAY
        except Exception:
            pass
        self._items[4]["detail"] = detail
        self._items[4]["color"] = color

    def _cloud_status_text(self, st):
        """状态机关键字 -> 子页中文文案"""
        key = st.get("state", "")
        if key == "active":
            return "已激活"
        if key == "binding":
            return "待绑定"
        if key == "unconfigured":
            return "未配置"
        if key == "no_time":
            return "等待校时"
        if key == "disabled":
            return "已停用"
        if st.get("err_code"):
            return "失败:" + str(st.get("err_code"))
        return "未激活"

    def _cloud_page(self):
        """绘制云端激活子页。

        布局 (240x240):
          y=6    标题「云端激活」+ 分隔线
          y=44   状态行 (24px 中文)
          中区   待绑定: 激活码 32px 大字 + 有效期 + 平台网页输入 + SN 小字
                 其余:   SN 32px 大字两行 (隔屏可读) + 状态对应提示/信息
          y=224  底部按键提示 (轮询/激活请求期间临时替换为 SYNC/结果)
        """
        d = self.display
        d.fill(CLR_BG)
        title = "云端激活"
        d.write(hanzi24, title, (W - len(title) * 24) // 2, 6,
                CLR_WHITE, CLR_BG)
        d.hline(10, 36, W - 20, CLR_DIM)

        st = {}
        pending = None
        try:
            from framework import cloud as _c
            st = _c.status()
            pending = _c.get_pending()
        except Exception:
            pass

        state = st.get("state", "")
        stxt = self._cloud_status_text(st)
        scol = CLR_GREEN if state == "active" else (
            CLR_AMBER if pending else CLR_WHITE)
        d.write(hanzi24, "状态", 14, 44, CLR_CYAN, CLR_BG)
        d.write(hanzi24, stxt, 74, 44, scol, CLR_BG)

        sy32 = _get_sy32()
        sn = str(st.get("sn", ""))
        if pending:
            # 待绑定: 大字激活码 (用户在平台网页回填完成绑定)
            code = str(pending.get("code", ""))
            expire = str(pending.get("expire_at", ""))
            if sy32 and code:
                _draw_sym_center(d, sy32, code, 64, CLR_AMBER, CLR_BG)
            if SUB_FONT and len(expire) >= 16:
                line = "EXP " + expire[5:16]
                d.text(SUB_FONT, line, (W - len(line) * 8) // 2, 108,
                       CLR_GRAY, CLR_BG)
            d.write(hanzi24, "平台网页输入", (W - 6 * 24) // 2, 124,
                    CLR_CYAN, CLR_BG)
            if SUB_FONT:
                line = ("SN: " + sn)[:24]
                d.text(SUB_FONT, line, (W - len(line) * 8) // 2, 162,
                       CLR_GRAY, CLR_BG)
        else:
            # 大字 SN: 隔屏可读, 供用户抄到平台网页发起绑定
            if sy32 and sn:
                y = 78
                for ln in _sym_lines(sy32, sn, W - 16):
                    _draw_sym_center(d, sy32, ln, y, CLR_WHITE, CLR_BG)
                    y += 40
            if state == "active":
                host = (str(st.get("endpoint", ""))
                        .replace("https://", "").replace("http://", "")
                        .rstrip("/"))
                d.write(hanzi24, "平台", 14, 166, CLR_CYAN, CLR_BG)
                if SUB_FONT:
                    d.text(SUB_FONT, host[:19], 74, 174,
                           CLR_WHITE, CLR_BG)
                d.write(hanzi24, "心跳", 14, 196, CLR_CYAN, CLR_BG)
                if SUB_FONT:
                    hb = ((str(st.get("last_hb", "")) or "--")
                          + " " + str(st.get("interval", "")) + "s")
                    d.text(SUB_FONT, hb[:19], 74, 204,
                           CLR_WHITE, CLR_BG)
            elif state == "no_time":
                d.write(hanzi24, "等待校时", (W - 4 * 24) // 2, 170,
                        CLR_GRAY, CLR_BG)
            elif state in ("unconfigured", "disabled"):
                line = "CLOUD OFF" if state == "disabled" else "NO ACT KEY"
                if SUB_FONT:
                    d.text(SUB_FONT, line, (W - len(line) * 8) // 2, 176,
                           CLR_GRAY, CLR_BG)
            else:
                d.write(hanzi24, "等待激活码", (W - 5 * 24) // 2, 170,
                        CLR_CYAN, CLR_BG)
        if SUB_FONT:
            d.text(SUB_FONT, "B:ACTIVATE A/C:BACK",
                   (W - 19 * 8) // 2, H - 16, CLR_GRAY, CLR_BG)

    def _cloud_poll_once(self):
        """子页周期轮询: 拉取云端待执行指令 (激活码), 新码到达提示音"""
        try:
            from framework import cloud as _c
        except Exception:
            return
        if not _c.ready():
            return
        d = self.display
        had = _c.get_pending()
        if SUB_FONT:
            d.fill_rect(0, H - 16, W, 8, CLR_BG)
            d.text(SUB_FONT, "SYNC...", (W - 7 * 8) // 2, H - 16,
                   CLR_CYAN, CLR_BG)
        try:
            _c.poll_pending()
        except Exception:
            pass
        if _c.get_pending() and not had:
            self.beep(1500, 40)
            time.sleep_ms(60)
            self.beep(1900, 40)
        self._cloud_page()

    def _action_cloud(self):
        """云端激活子页 (阻塞式): B=手动激活, A/C=返回列表。

        打开期间每 10s 轮询一次待执行指令 (进页立即拉一次, 接住用户
        先在网页发起的绑定); 全局 tick 被本子页阻塞, 轮询由
        cloud.poll_pending() 接管。激活为阻塞请求 (约 1~10s)。
        """
        self._cloud_page()
        d = self.display
        next_poll = 0
        while True:
            up, sel, down, _bl, _al, _cl = self._scan_with_timing()
            if up or down:
                # 返回列表: 重绘列表页
                self.beep(900, 15)
                self._refresh_cloud_detail()
                self._redraw_list_page()
                return
            if sel:
                self.beep(1000, 30)
                d.fill_rect(0, H - 16, W, 8, CLR_BG)
                if SUB_FONT:
                    d.text(SUB_FONT, "Activating...", (W - 12 * 8) // 2,
                           H - 16, CLR_CYAN, CLR_BG)
                try:
                    from framework import cloud as _c
                    ok, err = _c.activate_now()
                except Exception as e:
                    ok, err = False, str(e)
                self._cloud_page()
                line = "OK" if ok else "FAIL:" + str(err)[:12]
                if SUB_FONT:
                    d.text(SUB_FONT, line, (W - len(line) * 8) // 2,
                           H - 16, CLR_GREEN if ok else CLR_RED, CLR_BG)
                self.beep(1200 if ok else 400, 60)
                time.sleep_ms(400)
                next_poll = time.ticks_add(time.ticks_ms(),
                                           _CLOUD_POLL_MS)
                continue
            now = time.ticks_ms()
            if time.ticks_diff(now, next_poll) >= 0:
                next_poll = time.ticks_add(now, _CLOUD_POLL_MS)
                self._cloud_poll_once()
            time.sleep_ms(30)

    def _action_about(self):
        """关于本机: 弹窗显示版本和内存"""
        free_kb = gc.mem_free() // 1024
        ver = ""
        try:
            ver = sys.version.split()[0]
        except Exception:
            ver = "MP"
        box_ver = "?"
        try:
            from config import FW_VERSION
            box_ver = str(FW_VERSION)
        except Exception:
            pass

        self._show_popup([
            ("XianMuBox v" + box_ver, CLR_CYAN),
            ("MP: " + ver, CLR_WHITE),
            ("Free: " + str(free_kb) + "KB", CLR_GREEN),
        ])
        self.beep(1200, 30)

    def _action_reset(self):
        """恢复出厂: 二次确认后重启设备"""
        if not self._reset_confirm:
            # 首次按下: 弹出确认请求
            self._reset_confirm = True
            self._reset_ts = time.ticks_ms()
            self._show_popup([
                ("Reset?", CLR_RED),
                ("Press B again!", CLR_AMBER),
            ])
            self.beep(800, 60)
        else:
            # 确认: 执行重启
            self._reset_confirm = False
            self._show_popup([("Resetting...", CLR_RED)])
            self.beep(400, 100)
            time.sleep_ms(500)
            try:
                import machine
                machine.reset()
            except Exception:
                pass

    # ==================== 数据刷新 ====================

    def _get_wifi_ip(self):
        """获取当前 WiFi IP (无副作用)"""
        try:
            from framework.wifi import check_wifi
            ok, ip = check_wifi()
            return ip if ok else None
        except Exception:
            return None

    def _refresh_wifi_detail(self):
        """刷新 WiFi 项的 detail 和 color"""
        ip = self._get_wifi_ip()
        if ip:
            self._items[0]["detail"] = ip
            self._items[0]["color"] = CLR_GREEN
        else:
            self._items[0]["detail"] = "Disconnected"
            self._items[0]["color"] = CLR_RED

    def _refresh_http_detail(self):
        """刷新 HTTP 服务项的 detail 和 color"""
        try:
            from framework.http_server import is_running
            running = is_running()
        except ImportError:
            running = False

        self._items[3]["detail"] = "Running" if running else "Stopped"
        self._items[3]["color"] = CLR_GREEN if running else CLR_RED

    def _refresh_about_detail(self):
        """刷新关于项的 detail (可用内存)"""
        try:
            free_kb = gc.mem_free() // 1024
            self._items[5]["detail"] = str(free_kb) + "KB free"
        except Exception:
            self._items[5]["detail"] = ""
    # ==================== 主循环 ====================

    def loop(self):
        """主循环"""
        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            # ====== WiFi 连接子页面 (优先级最高) ======
            if self._wifi_state == 'connecting':
                return self._wifi_connect_loop()
            if self._wifi_state == 'done':
                self._check_wifi_done_return()
                time.sleep_ms(50)
                return True

            # 重置确认超时 (5 秒)
            if self._reset_confirm:
                if time.ticks_diff(time.ticks_ms(), self._reset_ts) > 5000:
                    self._reset_confirm = False

            # 弹窗状态处理
            if self._popup:
                now = time.ticks_ms()
                if self._reset_confirm:
                    # 重置确认弹窗: 等待 B 确认 / 上下取消 / 超时
                    if sel:
                        self._action_reset()
                    elif up or down:
                        self._hide_popup()
                    elif time.ticks_diff(now, self._popup_ts) > 5000:
                        self._hide_popup()
                elif self._http_wait_wifi:
                    # 等待 WiFi 连接: 轮询 / 超时 / 按键取消
                    if sel or up or down:
                        self._http_wait_wifi = False
                        self._hide_popup()
                    elif time.ticks_diff(now, self._http_wait_ts) > 500:
                        self._http_wait_ts = now
                        try:
                            from framework.wifi import check_wifi
                            ok, _ = check_wifi()
                            if ok:
                                self._http_wait_wifi = False
                                self._hide_popup()
                                self._start_http_and_refresh()
                        except Exception:
                            pass
                    elif time.ticks_diff(now, self._popup_ts) > 10000:
                        self._http_wait_wifi = False
                        self._hide_popup()
                        self._show_popup([
                            ("WiFi", CLR_WHITE),
                            ("Timeout", CLR_RED),
                        ])
                else:
                    # 普通信息弹窗: 自动关闭
                    if time.ticks_diff(now, self._popup_ts) > 2000:
                        self._hide_popup()
                time.sleep_ms(100)
                return True

            # 列表导航
            if up:
                self._idx = (self._idx - 1) % len(self._items)
                self._adjust_scroll()
                self._draw_all_items()
                self.beep(800, 15)
                time.sleep_ms(50)  # 短驻留防连跳; 消抖已由 hal.Buttons 保证
            elif down:
                self._idx = (self._idx + 1) % len(self._items)
                self._adjust_scroll()
                self._draw_all_items()
                self.beep(800, 15)
                time.sleep_ms(50)  # 短驻留防连跳; 消抖已由 hal.Buttons 保证
            elif sel:
                result = self._handle_item(self._items[self._idx])
                if result is False:
                    return False
                time.sleep_ms(200)

            # HTTP 服务器非阻塞轮询 (每轮调用一次)
            self._poll_http()

            time.sleep_ms(50)
            return True
        except Exception as e:
            print("[SETTINGS] loop error:", e)
            return False

    def _poll_http(self):
        """非阻塞轮询 HTTP 服务器, 处理一个待处理请求"""
        try:
            from framework.http_server import poll_http_server
            poll_http_server()
        except Exception:
            pass
