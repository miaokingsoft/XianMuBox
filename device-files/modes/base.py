"""
多功能终端框架 - 功能模式基类
闲沐工坊 · iOS Edition

所有功能 UI Demo 共享基类, 提供:
  - 统一生命周期: enter() / loop() / exit()
  - 长按 B 返回主菜单的检测
  - WiFi 状态条绘制 (复用 menu 同款)
  - 卡片绘制工具 (统一卡片样式)
  - 闪白反馈动画

MicroPython 兼容说明:
  - 避免 with 语句 (旧固件可能不支持)
  - 时间相关全部用 time.ticks_ms 避免溢出
  - 字符串拼接使用 + / join, 不使用 f-string
"""

import time

# 从父目录导入共享依赖
try:
    from font import vga1_8x8 as SUB_FONT
except ImportError:
    SUB_FONT = None

# ===== 字体统一加载（各模式共用，避免重复导入）=====
_SUB_FONT_CACHE = None
_BIG_FONT_CACHE = None
_BIG_FONT_SIZE = (8, 8)  # fallback


def get_sub_font():
    """
    惰性加载 vga1_8x8 小字体。
    首次成功后缓存，避免模块级导入失败导致永久不可用。
    """
    global _SUB_FONT_CACHE
    if _SUB_FONT_CACHE is not None:
        return _SUB_FONT_CACHE
    try:
        from font import vga1_8x8 as f
        _SUB_FONT_CACHE = f
    except ImportError:
        _SUB_FONT_CACHE = None
    return _SUB_FONT_CACHE


def get_big_font():
    """
    按优先级尝试加载大字体：vga1_16x32 → vga1_16x16 → SUB_FONT fallback
    返回 (font_module, char_width, char_height)
    首次加载后缓存，后续调用直接返回缓存值。
    """
    global _BIG_FONT_CACHE, _BIG_FONT_SIZE
    if _BIG_FONT_CACHE is not None:
        return _BIG_FONT_CACHE, _BIG_FONT_SIZE[0], _BIG_FONT_SIZE[1]
    try:
        from font import vga1_16x32 as f
        _BIG_FONT_CACHE = f
        _BIG_FONT_SIZE = (16, 32)
    except ImportError:
        try:
            from font import vga1_16x16 as f
            _BIG_FONT_CACHE = f
            _BIG_FONT_SIZE = (16, 16)
        except ImportError:
            _BIG_FONT_CACHE = SUB_FONT  # may be None
            _BIG_FONT_SIZE = (8, 8)
    return _BIG_FONT_CACHE, _BIG_FONT_SIZE[0], _BIG_FONT_SIZE[1]

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_CARD, CLR_CARD_HI, CLR_CARD_SH,
    CLR_GREEN, CLR_RED, CLR_AMBER, CLR_ORANGE,
)
from framework.hal import LONG_PRESS_MS


class BaseMode:
    """
    功能模式 UI 基类

    使用流程:
        mode = MyMode(display, buttons, buzzer)
        mode.run()  # 自动 enter → loop → exit

    子类应覆盖:
        enter():  清屏 + 首次绘制
        loop():   主循环, 返回 False 退出
        exit():   资源清理 (默认 no-op)
    """

    def __init__(self, display, buttons, buzzer=None):
        self.display = display
        self.buttons = buttons
        self.buzzer = buzzer
        # 按钮状态 (用于长按检测)
        self._b_pressed_at = None  # B 按下时刻
        self._a_pressed_at = None
        self._c_pressed_at = None
        # 长按触发标记（防止长按期间重复触发）
        self._b_long_fired = False
        self._a_long_fired = False
        self._c_long_fired = False

    # ---------- 生命周期 ----------

    def enter(self):
        """进入模式: 清屏 + 首次绘制 (子类覆盖)"""
        self.display.fill(CLR_BG)

    def loop(self):
        """主循环: 处理按钮, 返回 False 退出 (子类覆盖)"""
        time.sleep_ms(50)
        return False

    def exit(self):
        """退出模式: 资源清理 (子类覆盖)"""
        pass

    def run(self):
        """统一入口: enter → loop → exit

        每帧夹带三个全局服务检查:
          - framework.alarm: 任何模式运行中闹钟到点即接管屏幕响铃,
            结束后本模式 enter() 整页重绘恢复
          - framework.news: 任何模式运行中整点到点即接管屏幕播报
            当档新闻 (v1.5), 结束后本模式 enter() 整页重绘恢复
          - framework.cloud: 云平台心跳调度 + 绑定确认码全屏接管
            (CLOUD_TICK=False 的模式可豁免, 如 swipe 的 BLE 时序场景)
        服务异常只打印一次, 不静默吞掉 (便于串口排障)。
        """
        self.enter()
        alarm_err_logged = False
        news_err_logged = False
        cloud_err_logged = False
        try:
            while True:
                try:
                    from framework import alarm as _alarm_svc
                    if _alarm_svc.check(self.display, self.buttons,
                                        self.buzzer):
                        self.enter()
                except Exception as e:
                    if not alarm_err_logged:
                        alarm_err_logged = True
                        import sys
                        sys.print_exception(e)
                        print("[ALARM] service disabled after error")
                try:
                    from framework import news as _news_svc
                    if _news_svc.check(self.display, self.buttons,
                                       self.buzzer):
                        self.enter()
                except Exception as e:
                    if not news_err_logged:
                        news_err_logged = True
                        import sys
                        sys.print_exception(e)
                        print("[NEWS] service disabled after error")
                if getattr(self, "CLOUD_TICK", True):
                    try:
                        from framework import cloud as _cloud_svc
                        if _cloud_svc.ready():
                            _cloud_svc.tick()
                    except Exception as e:
                        if not cloud_err_logged:
                            cloud_err_logged = True
                            import sys
                            sys.print_exception(e)
                            print("[CLOUD] service disabled after error")
                if not self.loop():
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.exit()

    # ---------- 长按检测工具 ----------

    def _scan_with_timing(self):
        """
        扫描按钮, 同时跟踪 A/B/C 长按状态
        返回 (up, sel_short, down, b_long, a_long, c_long)

        边沿信号由 framework.hal.Buttons.scan_timed() 统一产出
        (IRQ 锁存 + 消抖), 此处只负责长按计时。
        """
        btns = self.buttons
        up, sel, down, a, b, c = btns.scan_timed()
        now = time.ticks_ms()

        # 长按检测: 持续按下 > LONG_PRESS_MS 触发一次
        b_long = False
        if b == 0:  # B 当前按下
            if self._b_pressed_at is None:
                self._b_pressed_at = now
            elif not self._b_long_fired:
                if time.ticks_diff(now, self._b_pressed_at) > LONG_PRESS_MS:
                    b_long = True
                    self._b_long_fired = True
        else:
            self._b_pressed_at = None
            self._b_long_fired = False

        a_long = False
        if a == 0:
            if self._a_pressed_at is None:
                self._a_pressed_at = now
            elif not self._a_long_fired:
                if time.ticks_diff(now, self._a_pressed_at) > LONG_PRESS_MS:
                    a_long = True
                    self._a_long_fired = True
        else:
            self._a_pressed_at = None
            self._a_long_fired = False

        c_long = False
        if c == 0:
            if self._c_pressed_at is None:
                self._c_pressed_at = now
            elif not self._c_long_fired:
                if time.ticks_diff(now, self._c_pressed_at) > LONG_PRESS_MS:
                    c_long = True
                    self._c_long_fired = True
        else:
            self._c_pressed_at = None
            self._c_long_fired = False

        return up, sel, down, b_long, a_long, c_long

    # ---------- 公共绘制工具 ----------

    def draw_wifi_bar(self, connected, ip):
        """
        顶部 WiFi 状态条 (与 menu 同款)
        已连接: 居中显示绿色 IP
        未连接: 红 X + "WiFi"
        """
        try:
            from config import W
        except ImportError:
            W = 240
        d = self.display
        d.fill_rect(0, 0, W, 16, CLR_BG)
        if not SUB_FONT:
            return

        if connected and ip:
            t = "IP:" + ip
            tw = len(t) * 8
            d.text(SUB_FONT, t, (W - tw) // 2, 4, CLR_GREEN, CLR_BG)
        else:
            # 红叉 8px + 间距 4px + "WiFi" 32px = 44px
            x0 = (W - 44) // 2
            d.line(x0, 4, x0 + 7, 11, CLR_RED)
            d.line(x0 + 7, 4, x0, 11, CLR_RED)
            d.text(SUB_FONT, "WiFi", x0 + 12, 4, CLR_RED, CLR_BG)

    def draw_card(self, x, y, w, h, selected=True):
        """绘制 iOS 风格卡片 (铺底 + 高光 + 阴影 + 描边)"""
        d = self.display
        if selected:
            d.fill_rect(x, y, w, h, CLR_CARD)
            d.hline(x, y, w, CLR_CARD_HI)
            d.hline(x, y + h - 1, w, CLR_CARD_SH)
            d.rect(x, y, w, h, CLR_WHITE)

    def flash_exit(self):
        """
        闪白反馈 (与 menu 一致, 2 次 × 60ms)
        + 蜂鸣器短音
        """
        import gc
        gc.collect()  # 渲染前回收，确保全屏 fill 有足够内存
        d = self.display
        d.fill(CLR_WHITE)
        time.sleep_ms(60)
        d.fill(CLR_BG)
        time.sleep_ms(60)
        d.fill(CLR_WHITE)
        time.sleep_ms(60)
        d.fill(CLR_BG)
        if self.buzzer:
            self.buzzer.tone(1200, 30)

    def beep(self, freq=800, ms=15):
        """蜂鸣器短音"""
        if self.buzzer:
            self.buzzer.tone(freq, ms)