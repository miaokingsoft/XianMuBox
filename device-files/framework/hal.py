"""
XianMuBox - 硬件抽象层
闲沐工坊 · 插件式架构

提供统一的硬件操作接口：
  - Buttons     三按钮驱动（边沿检测）
  - Buzzer      蜂鸣器驱动（tone / melody）
  - init_display()  屏幕初始化
  - LONG_PRESS_MS  长按判定阈值（所有模式共用）
"""

import time
from machine import Pin, SPI, PWM
import st7789

from config import (
    W, H, ROTATION, BUFFER_SIZE,
    TFT_SCL, TFT_SDA, TFT_RES,
    TFT_DC, TFT_CS, TFT_BLK,
    BTN_A, BTN_B, BTN_C,
)

# ===== 长按判定阈值（所有模式共用） =====
LONG_PRESS_MS = 800
# ===== 按键消抖窗口（毫秒，所有扫描入口共用） =====
# 机械按键抖动尖峰典型 <10ms；IRQ 边沿事件须跨越该窗口才被确认，
# 采样边沿也按"距最近释放沿 > 该窗口"过滤释放抖动
DEBOUNCE_MS = 30
# ===== 有效按压最短低电平时长（毫秒） =====
# 消费 IRQ 锁存事件且引脚已回高时校验 evt→rls 实测低电平段:
# EMI 毛刺(<1ms)与释放余颤(几 ms)的瞬闭段被丢弃, 真实快按(≥40ms)保留。
# 没有这道校验, 毛刺/余颤会在 30ms 后被当正常事件上报(单按双触发)
MIN_PRESS_MS = 15


# ===== 按钮 =====
class Buttons:
    """三按钮驱动：内部上拉，按下=LOW。

    边沿检测 = 硬件 IRQ 锁存 + 轮询采样双路合一：
      - IRQ 按下沿置事件标志（零分配 ISR），主循环阻塞期间发生的
        按压不丢失，恢复扫描时补报（完整按压落在两次采样之间也有效）
      - 时间戳消抖：IRQ 事件须距按下 >= DEBOUNCE_MS 才确认，
        释放抖动窗口内的下降沿直接忽略
      - 时长校验：消费事件时引脚已回高的，还须实测低电平段
        >= MIN_PRESS_MS，滤除 EMI 毛刺与释放余颤（单按双触发的根源）
    对外接口：scan() / scan_timed() / read_raw() / last_state() / update_last()
    """

    def __init__(self):
        self.a = Pin(BTN_A, Pin.IN, Pin.PULL_UP)
        self.b = Pin(BTN_B, Pin.IN, Pin.PULL_UP)
        self.c = Pin(BTN_C, Pin.IN, Pin.PULL_UP)
        self._last = (1, 1, 1)
        # IRQ 锁存状态（ISR 内只做标量属性赋值，零内存分配）。
        # 时间戳必须基于当前 ticks_ms：ticks_diff 的回绕语义下与常量 0
        # 相减可能得到负值；释放沿初始化为" DEBOUNCE_MS 前"，表示
        # 初始静止态已稳定，不吞开机后的首次按压
        now = time.ticks_add(time.ticks_ms(), -DEBOUNCE_MS)
        self._evt_a = 0            # 待消费的按下事件
        self._evt_a_ms = now       # 该事件按下沿时间戳
        self._rls_a_ms = now       # 最近释放沿时间戳（滤释放抖动）
        self._evt_b = 0
        self._evt_b_ms = now
        self._rls_b_ms = now
        self._evt_c = 0
        self._evt_c_ms = now
        self._rls_c_ms = now
        self.a.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                   handler=self._on_irq)
        self.b.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                   handler=self._on_irq)
        self.c.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                   handler=self._on_irq)

    def _on_irq(self, p):
        """Pin IRQ（双沿）：只做时间戳与标志赋值，禁止内存分配"""
        now = time.ticks_ms()
        if p.value():
            # 释放沿：仅记录时刻，供后续下降沿过滤释放抖动
            if p is self.a:
                self._rls_a_ms = now
            elif p is self.b:
                self._rls_b_ms = now
            else:
                self._rls_c_ms = now
            return
        # 按下沿：释放抖动窗口内忽略；已有未消费事件不覆盖（保留最早时刻）
        if p is self.a:
            rls, pending = self._rls_a_ms, self._evt_a
        elif p is self.b:
            rls, pending = self._rls_b_ms, self._evt_b
        else:
            rls, pending = self._rls_c_ms, self._evt_c
        if pending or time.ticks_diff(now, rls) < DEBOUNCE_MS:
            return
        if p is self.a:
            self._evt_a = 1
            self._evt_a_ms = now
        elif p is self.b:
            self._evt_b = 1
            self._evt_b_ms = now
        else:
            self._evt_c = 1
            self._evt_c_ms = now

    def scan_timed(self):
        """返回 (up, sel, down, a, b, c)：三键边沿信号 + 当前电平。

        边沿来源 = 采样比较 OR IRQ 锁存事件（去重，报过即清标志）。
        供需要原始电平做长按检测的模式循环使用；scan() 为其前三元包装。
        """
        now = time.ticks_ms()
        a, b, c = self.a.value(), self.b.value(), self.c.value()
        la, lb, lc = self._last
        self._last = (a, b, c)
        # 采样边沿：加"距最近释放沿 > DEBOUNCE_MS"过滤释放抖动
        up   = la == 1 and a == 0 and \
            time.ticks_diff(now, self._rls_a_ms) >= DEBOUNCE_MS
        sel  = lb == 1 and b == 0 and \
            time.ticks_diff(now, self._rls_b_ms) >= DEBOUNCE_MS
        down = lc == 1 and c == 0 and \
            time.ticks_diff(now, self._rls_c_ms) >= DEBOUNCE_MS
        # IRQ 锁存补偿：采样间隙（阻塞期）内的完整按压由硬件边沿救回。
        # 两路事件互去重：任一路上报即消费 IRQ 标志，同一次按压只报一次。
        # 确认条件：事件已存在 >= DEBOUNCE_MS，且引脚仍低（正按住）或
        # 实测低电平段 >= MIN_PRESS_MS（盲区快按）；引脚已回高但低电平
        # 段极短 = 毛刺/释放余颤，丢弃（否则表现为单按双触发）
        if not up and self._evt_a and \
                time.ticks_diff(now, self._evt_a_ms) >= DEBOUNCE_MS and \
                (a == 0 or time.ticks_diff(self._rls_a_ms,
                                           self._evt_a_ms) >= MIN_PRESS_MS):
            up = True
        if up:
            self._evt_a = 0
        if not sel and self._evt_b and \
                time.ticks_diff(now, self._evt_b_ms) >= DEBOUNCE_MS and \
                (b == 0 or time.ticks_diff(self._rls_b_ms,
                                           self._evt_b_ms) >= MIN_PRESS_MS):
            sel = True
        if sel:
            self._evt_b = 0
        if not down and self._evt_c and \
                time.ticks_diff(now, self._evt_c_ms) >= DEBOUNCE_MS and \
                (c == 0 or time.ticks_diff(self._rls_c_ms,
                                           self._evt_c_ms) >= MIN_PRESS_MS):
            down = True
        if down:
            self._evt_c = 0
        return up, sel, down, a, b, c

    def scan(self):
        """返回 (up, sel, down) 边沿信号"""
        up, sel, down, _a, _b, _c = self.scan_timed()
        return up, sel, down

    def read_raw(self):
        """返回原始引脚值 (a, b, c)，供外部长按检测等扩展使用"""
        return self.a.value(), self.b.value(), self.c.value()

    def last_state(self):
        """返回上次扫描的引脚状态 (a, b, c)"""
        return self._last

    def update_last(self, a, b, c):
        """手动更新上次状态（供外部扩展扫描后同步）"""
        self._last = (a, b, c)

    def clear_events(self):
        """丢弃全部未消费的 IRQ 锁存事件与 _last 残留边沿
        （模式交接处防幻影按键用）"""
        a, b, c = self.a.value(), self.b.value(), self.c.value()
        self._evt_a = self._evt_b = self._evt_c = 0
        self._last = (a, b, c)


# ===== 屏幕 =====
def init_display(rotation=None, buffer_size=None):
    """
    初始化 ST7789 屏幕，返回 display 对象。
    rotation / buffer_size 默认从 config.py（设备档案）读取，
    也可显式传入以覆盖默认值。
    """
    if rotation is None:
        rotation = ROTATION
    if buffer_size is None:
        buffer_size = BUFFER_SIZE

    spi = SPI(1, baudrate=40000000, polarity=0, phase=0,
              sck=Pin(TFT_SCL), mosi=Pin(TFT_SDA))
    display = st7789.ST7789(
        spi, W, H,
        dc=Pin(TFT_DC, Pin.OUT),
        cs=Pin(TFT_CS, Pin.OUT),
        reset=Pin(TFT_RES, Pin.OUT),
        rotation=rotation, inversion=True,
        buffer_size=buffer_size,
    )
    display.init()
    display.fill(0x0000)
    PWM(Pin(TFT_BLK), freq=1000, duty=1023)
    return display


# ===== 蜂鸣器 =====
class Buzzer:
    """蜂鸣器驱动（低电平触发）"""

    def __init__(self, pin):
        self._pin_num = pin
        self.pin = Pin(pin, Pin.OUT, value=1)
        self._pwm = None  # 缓存 PWM 对象，避免重复创建

    def on(self):
        self.pin(0)

    def off(self):
        self.pin(1)

    def beep(self, ms=100):
        self.on()
        time.sleep_ms(ms)
        self.off()

    def tone(self, freq, ms, duty=512):
        # duty: PWM 占空比 0-1023。512(50%) 对无源蜂鸣器基波最优;
        # 有源蜂鸣器可调高(如 950)提升响度。由调用方按需选择。
        if self._pwm is not None:
            try:
                self._pwm.deinit()
            except Exception:
                pass
            self._pwm = None
        self._pwm = PWM(Pin(self._pin_num), freq=freq, duty=duty)
        time.sleep_ms(ms)
        self._pwm.deinit()
        self._pwm = None
        # 恢复为普通输出高电平（蜂鸣器低电平触发，高电平=静音）
        self.pin = Pin(self._pin_num, Pin.OUT, value=1)

    def melody(self, notes):
        for freq, dur, gap in notes:
            self.tone(freq, dur)
            time.sleep_ms(gap)
