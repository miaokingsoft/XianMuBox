"""
摩斯电码 - TFT 工具函数与硬件适配层
移植自源项目 tft_config.py，提供：
  - hex2rgb / rgb2hex  颜色转换
  - LineAuto / RowAuto  文本自动换行/逐字坐标
  - loading  过渡画面
  - TFA / BFA  硬件滚动区域常量
  - LedNeo  WS2812 RGB LED 驱动（替代源项目的 machine.Pin LED）
  - ButtonAdapter  3 键轮询适配器（替代源项目的 4 键 IRQ Button）
"""

import st7789
import utime

# ===== 硬件滚动区域常量（240x240 ST7789） =====
TFA = 0    # top fixed area
BFA = 80   # bottom fixed area


# ===== 颜色转换 =====

def hex2rgb(hex_str):
    """'#RRGGBB' -> RGB565 整型"""
    h = hex_str.lstrip('#')
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return st7789.color565(r, g, b)


def rgb2hex(rgb):
    """RGB565 -> '#RRGGBB' 字符串"""
    r = (rgb >> 11) & 0x1F
    g = (rgb >> 5) & 0x3F
    b = rgb & 0x1F
    r = (r << 3) | (r >> 2)
    g = (g << 2) | (g >> 4)
    b = (b << 3) | (b >> 2)
    return '#{:X}{:X}{:X}'.format(r, g, b)


# ===== 文本布局工具 =====

def LineAuto(font_, word_, w_, h_, x_, y_):
    """
    自动换行：将 word_ 按 w_ 宽度拆分为多行
    返回 [(x_, y, substring), ...]
    """
    worddata = []
    font = font_
    char_hight = font.HEIGHT + 6
    x, y = x_, y_
    char_ = ''
    for char in word_:
        char_width = font.WIDTHS[font.MAP.index(char)] if char in font.MAP else 0
        x += char_width + 2
        char_ += char
        if x > w_:
            x = 0
            worddata.append((x_, y, char_))
            y += char_hight
            char_ = ''
        if y > y_ + h_ - char_hight:
            break
    if char_ != '':
        worddata.append((x_, y, char_))
    return worddata


def RowAuto(font_, word_, w_, x_, y_):
    """
    标记同一行每个字符的坐标
    返回 [(x, y, char), ...]
    """
    worddata = []
    font = font_
    x, y = x_, y_
    for char in word_:
        worddata.append((x, y, char))
        char_width = font.WIDTHS[font.MAP.index(char)] if char in font.MAP else 0
        x += char_width
        if x > w_:
            break
    return worddata


# ===== 过渡画面 =====

def loading(tft_):
    """绘制 loading 过渡图"""
    tft_.png("/pic/loading.png", 70, 75, True)


# ===== WS2812 RGB LED 驱动 =====

class LedNeo:
    """
    WS2812 RGB LED 驱动（单颗 LED）
    替代源项目的 machine.Pin LED，接口兼容 Pin.value(0/1)
    value(0) = 亮, value(1) = 灭（低电平触发约定）
    """

    def __init__(self, pin, color=(255, 80, 0)):
        """
        Args:
            pin: GPIO 引脚号
            color: (R, G, B) 0-255，亮灯时的颜色（默认橙色，匹配 CLR_ORANGE）
        """
        from machine import Pin
        import neopixel
        self._np = neopixel.NeoPixel(Pin(pin), 1)
        self._color = color
        self.off()

    def on(self):
        """点亮 LED"""
        self._np[0] = self._color
        self._np.write()

    def off(self):
        """熄灭 LED"""
        self._np[0] = (0, 0, 0)
        self._np.write()

    def value(self, v):
        """兼容 Pin.value() 接口：0=亮, 1=灭"""
        if v == 0:
            self.on()
        else:
            self.off()

    def set_color(self, r, g, b):
        """设置亮灯颜色"""
        self._color = (r, g, b)


# ===== 3 键轮询按钮适配器 =====

class ButtonAdapter:
    """
    3 键轮询适配器，替代源项目的 4 键 IRQ Button。
    将 framework Buttons 的 3 个物理按键映射为摩斯电码所需的操作：
      A 键 -> 上一个 / 导航上
      C 键 -> 下一个 / 导航下
      B 短按 -> 确认 / 播放（源项目的 C 键功能）
      B 长按 -> 返回 / 退出（源项目的 D 键功能）
    """

    # 长按判定阈值（毫秒）
    LONG_PRESS_MS = 800

    def __init__(self, buttons):
        """
        Args:
            buttons: framework.hal.Buttons 实例
        """
        self.buttons = buttons
        self._last = (1, 1, 1)
        self._b_press_time = 0
        self._b_long_fired = False

    def scan(self):
        """
        扫描按钮状态，返回 (a_short, b_short, c_short, b_long)
          a_short: A 键短按（上一个/导航上）
          b_short: B 键短按（确认/播放）
          c_short: C 键短按（下一个/导航下）
          b_long:  B 键长按（返回/退出）

        实现说明：
          - 按下沿由 framework Buttons.scan_timed() 统一产出
            (IRQ 锁存 + 30ms 消抖)，主循环阻塞期间的按键不丢失
          - A/C 短按：按下沿触发。
          - B 短按：松开沿（0->1）触发（期间未达长按阈值）；
            盲区内完成按下+松开（松开沿落在两次扫描之间）时，
            IRQ 按下沿仍会救回，直接判为短按。
          - B 长按：持续按下 > LONG_PRESS_MS 触发一次。
        """
        up, sel, down, a, b, c = self.buttons.scan_timed()
        now = utime.ticks_ms()

        # A/C 短按：按下沿 = 按下
        a_short = up
        c_short = down

        # B 键状态机
        b_short = False
        b_long = False
        if sel:
            if b == 0:
                # 按下沿且仍在按：记录按下时刻，开始计时
                self._b_press_time = now
                self._b_long_fired = False
            else:
                # IRQ 救回的按下沿到达时已松开：盲区完整点按，判为短按
                b_short = True
                self._b_press_time = 0
                self._b_long_fired = False
        if b == 0 and self._b_press_time != 0:
            # 持续按下：超过阈值触发一次长按
            if not self._b_long_fired:
                if utime.ticks_diff(now, self._b_press_time) > self.LONG_PRESS_MS:
                    b_long = True
                    self._b_long_fired = True
        prev_b = self._last[1]
        if prev_b == 0 and b == 1:
            # 松开沿：有按下记录且未触发长按则为短按
            if self._b_press_time != 0 and not self._b_long_fired:
                b_short = True
            self._b_press_time = 0
            self._b_long_fired = False
        self._last = (a, b, c)
        return a_short, b_short, c_short, b_long

    def reset(self):
        """重置内部状态（进入新界面时调用），并丢弃 framework 残留事件/边沿"""
        # 清空 IRQ 锁存事件与 _last，使残留按键（如主菜单按 B 进入）
        # 不致误判为新边沿
        self._last = self.buttons.read_raw()
        self.buttons.clear_events()
        self._b_press_time = 0
        self._b_long_fired = False


# ===== 英文/ASCII 字体容错加载 =====
# 摩斯电码字符串（".-.-"）与计时器（"00:00"）需绘制英文/数字。
# 源项目使用矢量字体 italicc，该字体未随固件发布；此处提供容错加载：
# 优先矢量字体 -> 降级 vga1 位图字体 -> None（缺失也不抛异常，确保功能可用）。

def load_en_font():
    """
    加载英文/ASCII 字体。
    返回 (font_module_or_None, is_vector)。
      is_vector=True  -> 矢量字体，用 tft.draw() 绘制（坐标为文字左下角）
      is_vector=False -> 位图字体，用 tft.text() 绘制（坐标为左上角）
      font_module=None-> 无可用字体，调用方应跳过绘制
    """
    # 优先源项目矢量字体（italicc/italict 同义，兼容两种命名）
    for _name in ('italicc', 'italict'):
        try:
            return __import__(_name), True
        except ImportError:
            pass
    # 降级 vga1 位图字体（font/ 包内，随构建部署）
    for _name in ('vga1_16x32', 'vga1_8x16', 'vga1_8x8'):
        try:
            # __import__('font.xxx') 返回包本身，两步取子模块
            return getattr(__import__('font.' + _name), _name), False
        except (ImportError, AttributeError):
            pass
    return None, False


def draw_en(tft_, font_, is_vector, text, x, y, color, fallback_y=None, bg=0):
    """
    绘制英文/ASCII 文本，兼容矢量(draw, 透明背景)与位图(text, 实底背景)字体。
    font_ 为 None 时直接跳过，绝不抛异常。

    Args:
      tft_        显示对象
      font_       load_en_font() 返回的字体模块
      is_vector   是否矢量字体
      text        文本内容
      x, y        矢量字体绘制坐标（左下角）
      color       前景色
      fallback_y  位图字体绘制时的 y 坐标（左上角，需调用方按字体高度换算）
      bg          位图字体背景色（默认黑色）
    """
    if font_ is None:
        return
    if is_vector and hasattr(tft_, 'draw'):
        tft_.draw(font_, text, x, y, color)
    elif hasattr(tft_, 'text'):
        tft_.text(font_, text, x, (y if fallback_y is None else fallback_y), color, bg)
