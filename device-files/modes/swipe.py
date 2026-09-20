"""
多功能终端框架 - 控刷视频（BLE HID 真机滑动）
XianMuBox · 插件式架构

功能:
  - 真实 BLE HID 触摸屏协议，驱动手机滑动
  - 多机切换: 同时连接多台手机，A长按切换活跃设备
  - 自动模式: 随机间隔自动滑动（间隔可配置）
  - 滑动参数可配置: 偏移比例/步数/延迟（适配不同手机）
  - 配置持久化: settings.json -> swipe 子节点（含设备列表）
  - 远程调参: HTTP API（配合 framework/http_server.py）

操作:
  - A 短按: 启动/停止自动滑动
  - A 长按: 切换活跃设备（需多机连接）
  - B 短按: 切换滑动模式（上滑/下滑/左滑/右滑）
  - C 短按: 保存配置并退出
"""

import time
import random
import struct
import gc

from font import hanzi_24 as hanzi24

try:
    from font import vga1_8x16 as FONT16
except ImportError:
    FONT16 = None

from .base import BaseMode
from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_CARD, CLR_GREEN, CLR_AMBER, CLR_RED,
    CLR_LIME, CLR_DARK, CLR_ORANGE,
)
from framework.hal import LONG_PRESS_MS

# HTTP 轮询：延迟到首次调用时导入，避免加载本模块时连带编译 http_server
# （后续调用走 sys.modules 缓存，不会产生内存碎片）
# None=未尝试, False=不可用, callable=可用
poll_http_server = None

# 统一配置访问层（settings.json -> swipe 子节点）
try:
    from framework import settings as _settings
except ImportError:
    _settings = None

try:
    from config import W, H
except ImportError:
    W = 240
    H = 240

# BLE HID 驱动独立子模块（拆分以降低 swipe.py 单文件编译峰值，避免二次进入 OOM）
from .swipe_ble import HIDTouch


# ==================== 配置管理 ====================
# 配置统一存于 settings.json 的 "swipe" 子节点，由 framework/settings 管理。
# 旧的 swipe_settings.json 已废弃（首次启动会被 settings 层自动迁移并入）。

# UI 布局常量（240x240 全屏，统一使用 hanzi_24 字体渲染中英文）
HEADER_H = 34          # 顶部栏高度 (0~34)
INFO_Y0 = 35           # 信息区起始y
LINE_SPACING = 34      # 信息行间距（hanzi24.HEIGHT=25 + 9px gap，4行均分）
BTN_Y0 = 199           # 按钮区起始y (199~235, 距底5px)
BTN_W = 73             # 按钮宽度（3按钮 + 2×5间隔 + 2×5边距 = 240）
BTN_H = 36             # 按钮高度
PROGRESS_BAR_Y = 182   # 进度条y坐标
PROGRESS_BAR_H = 8     # 进度条高度


def _hanzi_text_width(text):
    """估算 hanzi_24 字体渲染文本的像素宽度，避免 list.index() 内存分配"""
    # 中文字符约占 MAX_WIDTH，ASCII 约占 MAX_WIDTH//2
    w = 0
    for ch in text:
        if ord(ch) > 127:
            w += hanzi24.MAX_WIDTH
        else:
            w += hanzi24.MAX_WIDTH // 2
    return w

# 模式列表（与UI设计图对应）
MODES = [
    ("刷新视频-上滑", "up"),     # idx=0
    ("刷新视频-下滑", "down"),   # idx=1
    ("看书-左滑", "left"),       # idx=2: 预留扩展
    ("看书-右滑", "right"),      # idx=3: 预留扩展
]

# 预设分辨率列表
PHONE_RESOLUTIONS = [
    (1080, 1920),
    (720, 1280),
    (1080, 2340),
    (1080, 2400),
    (1440, 2560),
    (1440, 3200),
]


class SwipeConfig:
    """控刷视频配置管理器（持久化到 settings.json 的 swipe 子节点）

    swipe v2.0 结构：
      - modes: 4 种滑动模式各自独立的间隔与滑动参数
      - devices: 仅存储不同手机的分辨率（name / screen_w / screen_h）
      - active_device_idx: 当前使用哪台设备的分辨率
      - mode_idx: 当前活跃的滑动模式

    本类同时维护一组“当前生效”的标量字段，供 HIDTouch 和运行循环直接读取，
    避免每次滑动都进行字典查找。
    """

    DEFAULT_INTERVAL_MIN = 5
    DEFAULT_INTERVAL_MAX = 15
    DEFAULT_SCREEN_W = 1080
    DEFAULT_SCREEN_H = 1920
    DEFAULT_SWIPE_OFFSET_RATIO = 6
    DEFAULT_SWIPE_STEPS = 5
    DEFAULT_SWIPE_DELAY_MS = 20
    MAX_DEVICES = 5

    def __init__(self):
        # 当前活跃索引
        self.mode_idx = 0
        self.active_device_idx = 0
        # 模式参数列表（v2.0）与设备分辨率列表
        self.modes = []
        self.devices = []
        # 当前生效标量（供 HIDTouch 和 loop 使用）
        self.screen_w = self.DEFAULT_SCREEN_W
        self.screen_h = self.DEFAULT_SCREEN_H
        self.interval_min = self.DEFAULT_INTERVAL_MIN
        self.interval_max = self.DEFAULT_INTERVAL_MAX
        self.swipe_offset_ratio = self.DEFAULT_SWIPE_OFFSET_RATIO
        self.swipe_steps = self.DEFAULT_SWIPE_STEPS
        self.swipe_delay_ms = self.DEFAULT_SWIPE_DELAY_MS

    def _ensure_modes(self):
        """确保 modes 列表始终有 4 个元素，缺失则补默认值"""
        mode_names = ["刷新视频-上滑", "刷新视频-下滑", "看书-左滑", "看书-右滑"]
        while len(self.modes) < 4:
            self.modes.append({
                "name": mode_names[len(self.modes)],
                "interval_min": self.DEFAULT_INTERVAL_MIN,
                "interval_max": self.DEFAULT_INTERVAL_MAX,
                "swipe_offset_ratio": self.DEFAULT_SWIPE_OFFSET_RATIO,
                "swipe_steps": self.DEFAULT_SWIPE_STEPS,
                "swipe_delay_ms": self.DEFAULT_SWIPE_DELAY_MS,
            })
        # 只保留前 4 个
        if len(self.modes) > 4:
            self.modes = self.modes[:4]

    def load(self):
        """从 settings.json -> swipe 子节点加载配置，失败则使用默认值"""
        if _settings is None:
            self._ensure_modes()
            return
        try:
            d = _settings.get_swipe_config()
            self.mode_idx = int(d.get("mode_idx", 0))
            self.active_device_idx = int(d.get("active_device_idx", 0))

            # 加载模式参数
            self.modes = []
            raw_modes = d.get("modes", [])
            if isinstance(raw_modes, list):
                for m in raw_modes:
                    if not isinstance(m, dict):
                        continue
                    self.modes.append({
                        "name": str(m.get("name", "")),
                        "interval_min": int(m.get("interval_min", self.DEFAULT_INTERVAL_MIN)),
                        "interval_max": int(m.get("interval_max", self.DEFAULT_INTERVAL_MAX)),
                        "swipe_offset_ratio": int(m.get("swipe_offset_ratio", self.DEFAULT_SWIPE_OFFSET_RATIO)),
                        "swipe_steps": int(m.get("swipe_steps", self.DEFAULT_SWIPE_STEPS)),
                        "swipe_delay_ms": int(m.get("swipe_delay_ms", self.DEFAULT_SWIPE_DELAY_MS)),
                    })
            self._ensure_modes()

            # 加载设备分辨率列表
            raw_devs = d.get("devices", [])
            self.devices = []
            if isinstance(raw_devs, list):
                for dev in raw_devs:
                    if not isinstance(dev, dict):
                        continue
                    self.devices.append({
                        "name": str(dev.get("name", "Phone")),
                        "screen_w": int(dev.get("screen_w", self.DEFAULT_SCREEN_W)),
                        "screen_h": int(dev.get("screen_h", self.DEFAULT_SCREEN_H)),
                    })

            # 应用当前模式与活跃设备到标量字段
            self.apply_mode_config(self.mode_idx)
            self.apply_device_config(self.active_device_idx)
            print("[SWIPE] Config loaded, modes:", len(self.modes), "devices:", len(self.devices))
        except Exception as e:
            self._ensure_modes()
            print("[SWIPE] Load config error:", e)

    def save(self):
        """保存当前配置到 settings.json -> swipe 子节点"""
        if _settings is None:
            return
        try:
            # 保存前先把当前标量参数写回当前模式，避免切换模式后未保存的参数丢失
            if 0 <= self.mode_idx < len(self.modes):
                self.modes[self.mode_idx] = {
                    "name": self.modes[self.mode_idx].get("name", ""),
                    "interval_min": self.interval_min,
                    "interval_max": self.interval_max,
                    "swipe_offset_ratio": self.swipe_offset_ratio,
                    "swipe_steps": self.swipe_steps,
                    "swipe_delay_ms": self.swipe_delay_ms,
                }
            d = {
                "mode_idx": self.mode_idx,
                "active_device_idx": self.active_device_idx,
                "modes": self.modes,
                "devices": self.devices,
            }
            _settings.update({"swipe": d})
            print("[SWIPE] Config saved")
        except Exception as e:
            print("[SWIPE] Save config error:", e)

    def get_swipe_offset(self):
        """计算滑动偏移量（像素）"""
        return self.screen_h // self.swipe_offset_ratio

    def get_resolution_index(self):
        """查找当前分辨率在预设列表中的索引"""
        for i, res in enumerate(PHONE_RESOLUTIONS):
            if res[0] == self.screen_w and res[1] == self.screen_h:
                return i
        return 0

    # ===== 模式管理 =====

    def apply_mode_config(self, mode_idx):
        """将指定模式的参数应用到当前生效标量字段"""
        self._ensure_modes()
        if 0 <= mode_idx < len(self.modes):
            m = self.modes[mode_idx]
            self.interval_min = int(m.get("interval_min", self.DEFAULT_INTERVAL_MIN))
            self.interval_max = int(m.get("interval_max", self.DEFAULT_INTERVAL_MAX))
            self.swipe_offset_ratio = int(m.get("swipe_offset_ratio", self.DEFAULT_SWIPE_OFFSET_RATIO))
            self.swipe_steps = int(m.get("swipe_steps", self.DEFAULT_SWIPE_STEPS))
            self.swipe_delay_ms = int(m.get("swipe_delay_ms", self.DEFAULT_SWIPE_DELAY_MS))
            self.mode_idx = mode_idx
            print("[SWIPE] Applied mode config:", m.get("name", mode_idx))

    # ===== 设备管理 =====

    def apply_device_config(self, dev_idx):
        """将指定设备的分辨率应用到当前生效标量字段"""
        if 0 <= dev_idx < len(self.devices):
            dev = self.devices[dev_idx]
            self.screen_w = dev["screen_w"]
            self.screen_h = dev["screen_h"]
            self.active_device_idx = dev_idx
            print("[SWIPE] Applied device config:", dev["name"])

    def register_or_update_device(self, name, conn_idx):
        """注册或更新指定位置的设备（仅名称与分辨率）"""
        while len(self.devices) <= conn_idx:
            self.devices.append({
                "name": "Phone%d" % (len(self.devices) + 1),
                "screen_w": self.DEFAULT_SCREEN_W,
                "screen_h": self.DEFAULT_SCREEN_H,
            })
        self.devices[conn_idx]["name"] = name
        print("[SWIPE] Registered device:", name, "at idx", conn_idx)


# ==================== SwipeMode 主模式 ====================

class SwipeMode(BaseMode):
    """控刷视频模式 - 完整 BLE HID 驱动"""

    # BLE HID 自动滑动对时序敏感, 豁免全局云服务 tick
    # (心跳请求会短暂阻塞 loop, 见 framework/cloud.py)
    CLOUD_TICK = False

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._count = 0
        self._mode_idx = 0           # 当前模式索引
        self._running = False        # 是否正在运行
        self._last_swipe_ts = 0      # 上次滑动时间戳
        self._current_interval = 0   # 当前间隔时间（秒）
        self._current_wait_ms = 0    # 当前间隔毫秒（进度条分母）
        self._config = SwipeConfig()
        self._hid = None
        self._last_conn = None       # 上次 BLE 连接状态
        self._last_conn_count = 0    # 上次连接设备数
        self._err_phase = False      # BLE 错误阶段
        self._dev_registered = 0     # 本次会话已注册设备数
        self._waiting_frame = 0      # 等待连接动画帧 (0~3)
        self._waiting_anim_ts = 0    # 动画帧切换时间戳

        # 加载持久化配置
        self._config.load()
        # 恢复上次模式
        self._mode_idx = self._config.mode_idx if self._config.mode_idx < len(MODES) else 0

    # ========== 生命周期 ==========

    def enter(self):
        """进入模式: 初始化 BLE + 绘制 UI"""
        # 重置错误阶段标志（防止上次残留状态导致黑屏）
        self._err_phase = False

        self.display.fill(CLR_BG)
        
        # 断开 WiFi STA 以减少 GPIO 干扰
        try:
            import network
            sta = network.WLAN(network.STA_IF)
            if sta.isconnected():
                sta.disconnect()
            sta.active(False)
        except Exception:
            pass

        # 初始化 BLE HID
        self._hid = HIDTouch(config=self._config)
        if not self._hid.init_ok:
            self._err_phase = True
            self._draw_error()
            return

        # 初始化状态（恢复上次保存的模式索引）
        self._count = 0
        self._running = False
        self._last_swipe_ts = time.ticks_ms()
        self._current_interval = 0
        self._current_wait_ms = 0
        self._last_conn = None
        self._last_conn_count = 0
        self._dev_registered = 0
        self._waiting_frame = 0
        self._waiting_anim_ts = time.ticks_ms()

        # 恢复上次活跃设备分辨率，并应用当前模式参数
        self._config.apply_mode_config(self._mode_idx)
        if self._config.devices and self._config.active_device_idx < len(self._config.devices):
            self._config.apply_device_config(self._config.active_device_idx)

        # 绘制 UI（先回收碎片，BLE 初始化后内存紧张）
        gc.collect()
        self._draw_header()
        self._draw_info_area()
        self._draw_buttons()

        gc.collect()

    def loop(self):
        """主循环: 非阻塞事件调度"""
        # BLE 错误阶段: 等待用户退出
        if self._err_phase:
            return self._loop_error_phase()

        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            # 轮询 HTTP 服务器（非阻塞）
            self._poll_http()

            # 检查 BLE 状态变化（设备连接/断开）
            self._check_ble_state()

            # 等待连接动画：每500ms轮换点点，仅刷新BLE行避免全屏闪动
            if self._hid and not self._hid.is_connected():
                now = time.ticks_ms()
                if time.ticks_diff(now, self._waiting_anim_ts) > 500:
                    self._waiting_frame = (self._waiting_frame + 1) % 4
                    self._waiting_anim_ts = now
                    self._draw_ble_status()

            # A短按：启动/停止（仅按钮重绘）
            if up:
                if self._can_stop():
                    self._running = not self._running
                    self._button_feedback(0)
                    if self._running:
                        self._last_swipe_ts = time.ticks_ms()
                        self._do_swipe()  # 立即执行一次（内含 _draw_info_area）
                    else:
                        self.beep(600, 30)
                    time.sleep_ms(150)

            # A长按：切换设备（运行时禁用，需多设备）
            if a_long and not self._running:
                if self._hid and self._hid.conn_count() > 1:
                    self._switch_device()
                    time.sleep_ms(200)

            # B短按：切换模式（切换后应用该模式的参数）
            if sel and not self._running:
                if self._can_change_mode():
                    self._mode_idx = (self._mode_idx + 1) % len(MODES)
                    self._config.apply_mode_config(self._mode_idx)
                    self._button_feedback(1)
                    self._draw_info_area()
                    time.sleep_ms(150)

            # C短按：保存配置并退出
            if down and not self._running:
                self._button_feedback(2)
                self._config.mode_idx = self._mode_idx
                self._config.save()
                return False

            # 自动模式逻辑
            if self._running:
                now = time.ticks_ms()
                # 首次进入或上轮完成：生成新随机间隔
                if self._current_wait_ms == 0:
                    self._current_wait_ms = random.randint(
                        self._config.interval_min,
                        self._config.interval_max) * 1000
                if time.ticks_diff(now, self._last_swipe_ts) > self._current_wait_ms:
                    self._current_interval = self._current_wait_ms // 1000
                    self._do_swipe()
                    self._last_swipe_ts = now
                    self._count += 1
                    self._current_wait_ms = 0  # 重置，下帧生成新间隔
                    self._draw_info_area()

            # 刷新进度条（仅局部，非阻塞每帧更新）
            if self._running:
                self._draw_progress_bar()

            time.sleep_ms(50)
            return True
        except Exception as e:
            print("[SWIPE] loop error:", e)
            import sys
            sys.print_exception(e)
            return False

    def exit(self):
        """退出模式: 释放 BLE 资源 + 恢复 WiFi STA"""
        print("[SWIPE] Exiting...")
        if self._hid is not None:
            try:
                self._hid.deinit()
            except Exception as e:
                print("[SWIPE] deinit error:", e)
            self._hid = None
        # 恢复 WiFi STA（enter() 中为减少干扰关掉了，此处恢复以便后续模式联网）
        try:
            import network
            sta = network.WLAN(network.STA_IF)
            sta.active(True)
        except Exception:
            pass
        gc.collect()
        print("[SWIPE] Exit done")

    # ========== 绘制方法 ==========

    def _draw_header(self):
        """绘制顶部绿色栏 (34px)，分辨率8px右置"""
        d = self.display
        d.fill_rect(0, 0, W, HEADER_H, CLR_LIME)
        # 标题（垂直居中）
        title = "控刷手机"
        ty = (HEADER_H - hanzi24.HEIGHT) // 2
        d.write(hanzi24, title, 6, ty, CLR_WHITE, CLR_LIME)
        # 分辨率（右侧，8x16字体）
        if FONT16:
            res_text = "%dx%d" % (self._config.screen_w, self._config.screen_h)
            rw = len(res_text) * 8
            rx = W - 6 - rw
            ry = (HEADER_H - 16) // 2
            d.text(FONT16, res_text, rx, ry, CLR_WHITE, CLR_LIME)

    def _draw_info_area(self):
        """绘制信息区（深灰背景，4行均分，全用 hanzi_24 字体）"""
        d = self.display
        d.fill_rect(0, INFO_Y0, W, BTN_Y0 - INFO_Y0, CLR_DARK)

        if self._hid is None:
            return

        y = INFO_Y0 + 10  # 顶部留白10px，4行均分
        lh = LINE_SPACING  # 34px

        # 第一行: 设备信息
        cnt = self._hid.conn_count()
        dev_name = self._hid.get_active_name()
        dev_line = "Dev:%s [%d]" % (dev_name[:10], cnt)
        dw = _hanzi_text_width(dev_line)
        d.write(hanzi24, dev_line, (W - dw) // 2, y, CLR_CYAN, CLR_DARK)
        y += lh

        # 第二行: BLE状态（未连接时显示动画点点）
        if cnt == 0:
            dots = "." * self._waiting_frame
            ble_status = "Waiting" + dots
        else:
            ble_status = "OK(%d conn)" % cnt
        ble_color = CLR_AMBER if cnt == 0 else CLR_GREEN
        ble_text = "xianmu " + ble_status
        bw = _hanzi_text_width(ble_text)
        d.write(hanzi24, ble_text, (W - bw) // 2, y, ble_color, CLR_DARK)
        y += lh

        # 第三行: 模式（水平居中）
        mode_name = MODES[self._mode_idx][0]
        mw = _hanzi_text_width(mode_name)
        d.write(hanzi24, mode_name, (W - mw) // 2, y, CLR_WHITE, CLR_DARK)
        y += lh

        # 第四行: 计数 + 间隔秒数（右侧16px，右对齐）
        num_text = "Num:%d" % self._count
        
        if FONT16:
            
            sec_text = "%ds" % self._current_interval
            sw = len(sec_text) * 8
            sx = W - 16 - sw
            sy = y + hanzi24.HEIGHT - 16  # 与大字基线对齐
            d.text(FONT16, num_text, 16, sy, CLR_WHITE, CLR_DARK)
            d.text(FONT16, sec_text, sx, sy, CLR_WHITE, CLR_DARK)

        # 进度条框架
        bar_x = 6
        bar_w = W - 12
        d.rect(bar_x, PROGRESS_BAR_Y, bar_w, PROGRESS_BAR_H, CLR_DIM)
        # 进度填充（与 Int 百分比使用同一分母）
        if self._running and self._current_wait_ms > 0:
            now = time.ticks_ms()
            elapsed = time.ticks_diff(now, self._last_swipe_ts)
            progress = min(elapsed / self._current_wait_ms, 1.0)
            fill_w = int(bar_w * progress)
            d.fill_rect(bar_x, PROGRESS_BAR_Y, fill_w, PROGRESS_BAR_H, CLR_LIME)

    def _draw_progress_bar(self):
        """仅刷新进度条区域（避免全屏闪动）"""
        d = self.display
        bar_x = 6
        bar_w = W - 12

        # 擦除进度条区域（+1px 容差覆盖边框）
        d.fill_rect(bar_x - 1, PROGRESS_BAR_Y - 1,
                    bar_w + 2, PROGRESS_BAR_H + 2, CLR_DARK)
        # 框架
        d.rect(bar_x, PROGRESS_BAR_Y, bar_w, PROGRESS_BAR_H, CLR_DIM)
        # 进度填充（与 Int 百分比使用同一分母）
        if self._running and self._current_wait_ms > 0:
            now = time.ticks_ms()
            elapsed = time.ticks_diff(now, self._last_swipe_ts)
            progress = min(elapsed / self._current_wait_ms, 1.0)
            fill_w = int(bar_w * progress)
            if fill_w > 0:
                d.fill_rect(bar_x, PROGRESS_BAR_Y, fill_w, PROGRESS_BAR_H, CLR_LIME)

    def _draw_ble_status(self):
        """仅刷新 BLE 状态行（避免全屏闪动）"""
        gc.collect()
        d = self.display
        if self._hid is None:
            return
        cnt = self._hid.conn_count()
        y = INFO_Y0 + 10 + LINE_SPACING  # 第二行 y=84

        # 擦除该行
        d.fill_rect(0, y, W, hanzi24.HEIGHT, CLR_DARK)

        if cnt == 0:
            dots = "." * self._waiting_frame
            ble_status = "Waiting" + dots
        else:
            ble_status = "OK(%d conn)" % cnt
        ble_color = CLR_AMBER if cnt == 0 else CLR_GREEN
        ble_text = "xianmu " + ble_status
        bw = _hanzi_text_width(ble_text)
        d.write(hanzi24, ble_text, (W - bw) // 2, y, ble_color, CLR_DARK)

    def _draw_buttons(self, pressed_btn=None):
        """
        绘制三个按钮（左右下5px边距，等距均排）
        pressed_btn: 0=停止, 1=模式, 2=退出 (None表示无按下)
        """
        d = self.display
        # 按钮区背景与信息区统一
        d.fill_rect(0, BTN_Y0, W, H - BTN_Y0, CLR_DARK)

        # 按钮间距5px，左右距屏幕各5px
        btn_gap = 5
        btn_start_x = 5

        buttons = [
            ("运行" if not self._running else "停止", self._can_stop()),
            ("模式", self._can_change_mode()),
            ("退出", not self._running),
        ]

        for i, (label, enabled) in enumerate(buttons):
            bx = btn_start_x + i * (BTN_W + btn_gap)
            by = BTN_Y0

            bw = BTN_W + 1 if i == 2 else BTN_W  # 末按钮+1px凑满240

            # 确定样式
            if pressed_btn == i:
                bg = CLR_ORANGE  # 浅黄色按动反馈
                fg = CLR_BG
            elif not enabled:
                bg = CLR_DIM
                fg = CLR_GRAY
            else:
                bg = CLR_WHITE
                fg = CLR_BG

            d.fill_rect(bx, by, bw, BTN_H, bg)
            d.rect(bx, by, bw, BTN_H, CLR_DIM)

            # 按钮文字居中（hanzi24 字体）
            label_w = _hanzi_text_width(label)
            lx = bx + (bw - label_w) // 2
            ly = by + (BTN_H - hanzi24.HEIGHT) // 2
            d.write(hanzi24, label, lx, ly, fg, bg)



    def _draw_error(self):
        """显示 BLE 初始化失败（全部使用 hanzi_24 字体）"""
        d = self.display
        # 使用卡片样式居中显示
        card_w = W - 40
        card_h = 140
        card_x = 20
        card_y = 50
        
        # 卡片背景
        d.fill_rect(card_x, card_y, card_w, card_h, CLR_CARD)
        d.rect(card_x, card_y, card_w, card_h, CLR_WHITE)
        
        # 标题
        err_title = "BLE 初始化失败"
        tw = _hanzi_text_width(err_title)
        d.write(hanzi24, err_title, (W - tw) // 2, card_y + 18, CLR_RED, CLR_CARD)
        
        # 错误详情
        if self._hid and self._hid._err:
            err_text = self._hid._err[:15]
            ew = _hanzi_text_width(err_text)
            d.write(hanzi24, err_text, (W - ew) // 2, card_y + 55, CLR_GRAY, CLR_CARD)
        
        # 底部提示
        hint = "按B键退出"
        hw = _hanzi_text_width(hint)
        d.write(hanzi24, hint, (W - hw) // 2, card_y + 90, CLR_DIM, CLR_CARD)

    # ========== BLE 状态监控 ==========

    def _check_ble_state(self):
        """检测 BLE 连接状态变化，自动注册新设备并更新 UI"""
        if self._hid is None:
            return
        conn = self._hid.is_connected()
        cnt = self._hid.conn_count()
        old_cnt = self._last_conn_count

        # 设备数变化 → 注册新设备 + 刷新 UI
        if cnt != old_cnt:
            # 为新连接的设备注册配置
            while self._dev_registered < cnt:
                name = self._hid.get_active_name()
                self._config.register_or_update_device(name, self._dev_registered)
                self._dev_registered += 1
            self._last_conn_count = cnt
            self._draw_info_area()
            self._draw_buttons()
            # 蜂鸣器提示（连接/断开）
            if cnt > old_cnt:
                self.beep(1200, 30)
            else:
                self.beep(400, 50)

        # 连接状态变化
        if conn != self._last_conn:
            self._last_conn = conn
            self._draw_info_area()

    # ========== 设备切换 ==========

    def _switch_device(self):
        """切换到下一个已连接设备"""
        if self._hid is None:
            return
        old_name = self._hid.get_active_name()
        new_name = self._hid.switch_device()
        if new_name != old_name:
            # 应用新设备的配置
            new_idx = self._hid._active_idx
            if new_idx < len(self._config.devices):
                self._config.apply_device_config(new_idx)
            self.beep(900, 20)
            self._draw_info_area()
            self._draw_buttons()
            print("[SWIPE] Device switched: %s -> %s" % (old_name, new_name))
        else:
            self.beep(300, 30)  # 只有一个设备，提示音

    # ========== 错误阶段 ==========

    def _loop_error_phase(self):
        """BLE 初始化失败后的等待循环（C键退出）"""
        up, sel, down, b_long, a_long, c_long = self._scan_with_timing()
        if down:
            self.flash_exit()
            return False
        time.sleep_ms(100)
        return True

    # ========== 辅助方法 ==========

    def _can_stop(self):
        """判断是否可以点击停止按钮"""
        return self._hid is not None and self._hid.is_connected()

    def _can_change_mode(self):
        """判断是否可以切换模式"""
        return not self._running and self._can_stop()

    def _button_feedback(self, btn_idx):
        """按钮按下视觉反馈"""
        self._draw_buttons(pressed_btn=btn_idx)
        time.sleep_ms(100)
        self._draw_buttons(pressed_btn=None)

    # ========== HTTP 轮询 ==========

    def _poll_http(self):
        """轮询全局 HTTP 服务器（首次调用时延迟导入 http_server）

        http_server 依赖 socket+json 体积较大，BLE 运行态内存紧张时
        可能连导入都失败（抛 MemoryError）。此处用宽泛 except 兜住，
        失败即标记为不可用并清理半成品模块，避免拖垮 BLE 核心功能。
        """
        global poll_http_server
        if poll_http_server is None:
            try:
                from framework.http_server import poll_http_server as _fn
                poll_http_server = _fn
            except Exception:
                # 导入失败（含 MemoryError）：清理可能残留的半成品模块，
                # 永久降级为本模式不可用，不再重试
                poll_http_server = False
                import sys as _sys
                for _k in list(_sys.modules):
                    if _k == "framework.http_server":
                        del _sys.modules[_k]
                gc.collect()
        if poll_http_server:
            try:
                poll_http_server()
            except Exception:
                # 运行期偶发错误忽略，不中断滑动
                pass

    # ========== 核心操作 ==========

    def _do_swipe(self):
        """执行一次真实 BLE 滑动（根据当前模式）"""
        if self._mode_idx >= len(MODES):
            return
        
        mode_type = MODES[self._mode_idx][1]
        self._last_swipe_ts = time.ticks_ms()

        if self._hid and self._hid.is_connected():
            if mode_type == "up":
                self._hid.swipe_up()
            elif mode_type == "down":
                self._hid.swipe_down()
            elif mode_type == "left":
                self._hid.swipe_left()
            elif mode_type == "right":
                self._hid.swipe_right()

        # 蜂鸣器反馈（上下高频，左右低频区分）
        if self.buzzer:
            if mode_type in ("up", "down"):
                freq = 1000 if mode_type == "up" else 700
            else:
                freq = 500 if mode_type == "left" else 400
            self.buzzer.tone(freq, 20)

        # 刷新 UI
        self._draw_info_area()
