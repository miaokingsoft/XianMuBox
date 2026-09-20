"""
摩斯电码子菜单路由
移植自源项目 morse/main.py

适配点：
  - 4 键 IRQ -> 3 键轮询（A=上一个, C=下一个, B短=选择, B长=退出）
  - 子菜单含第 5 项「退出」：选中后 B 短按确认返回主菜单（B 长按亦随时可退）
  - 动态 import 子功能模块 + del sys.modules + gc.collect 保留
  - config.loading -> loading, config.hex2rgb -> hex2rgb
  - 创建共享 MorseCode 引擎（LedNeo WS2812 + Buzzer）
  - run() 拆分为 enter()/loop()/exit() 生命周期
"""
import gc
import utime
import sys
from morse.morsecode import MorseCode
from morse.tft_helpers import hex2rgb, loading, LedNeo, ButtonAdapter

try:
    from config import LED_PIN
except ImportError:
    LED_PIN = 21  # 默认 ESP32-S3 N8R8

Morse_level = [('电码概述', 'gaishu', 'morse_1.png'),
               ('字符码表', 'mabiao', 'morse_2.png'),
               ('常用短语', 'duanyu', 'morse_4.png'),
               ('抄码训练', 'chaoma', 'morse_3.png'),
               ('退出', '__exit__', 'back.png')]

# 子功能模块名 -> 类名映射
_FEATURE_CLASS = {
    'gaishu': 'GaiShu',
    'mabiao': 'MaBiao',
    'duanyu': 'DuanYu',
    'chaoma': 'ChaoMa',
}

gc.collect()


class MorseMain:
    def __init__(self, display, buttons, buzzer):
        self.tft = display
        self.buttons = buttons
        self.btn = ButtonAdapter(buttons)

        # 创建共享 MorseCode 引擎（WS2812 LED + 蜂鸣器）
        if LED_PIN is not None:
            led = LedNeo(LED_PIN, color=(255, 80, 0))
        else:
            led = None
        self.Morse = MorseCode(
            display, led, buzzer,
            hex2rgb('#0F4C75'), hex2rgb('#BBE1FA'),
            hex2rgb('#CCCCCC'), hex2rgb('#006699'))

        self.text_ = Morse_level
        self.mod = 0
        self.state = 0
        self.Exit_ = False
        self._feature = None  # 当前子功能实例

    def MainUi(self, n=0):
        self.tft.png("/pic/menu/{}".format(self.text_[n][2]), 72, 70, True)
        self.tft.write(self._font, self.text_[n][0], 66, 178, hex2rgb('#FFFFFF'), 0)

    def NumUi(self, n=0):
        for i in range(len(self.text_)):
            if i != n:
                self.tft.circle(100 + i * 15, 225, 5, hex2rgb('#CCCCCC'))
            else:
                self.tft.fill_circle(100 + i * 15, 225, 5, hex2rgb('#CCCCCC'))

    def HeiUi(self):
        self.tft.fill_rect(0, 178, 240, 62, 0)

    def move_num(self, n):
        if n < 0:
            n = len(self.text_) - 1
        elif n > len(self.text_) - 1:
            n = 0
        return n

    def enter(self):
        """绘制子菜单"""
        from font import hanzi100 as font
        self._font = font
        # 丢弃主菜单按 B 进入本模式时的残留按键边沿，
        # 避免进入瞬间被误判为 B 按下/松开（修复按键无响应 bug）
        self.btn.reset()
        self.Morse.Ui('摩尔斯电码')
        self.MainUi(0)
        self.NumUi()
        gc.collect()

    def loop(self):
        """返回 True 继续, False 退出整个模式"""
        # ----- 子功能运行中：委托给子功能 -----
        if self._feature is not None:
            result = self._feature.loop()
            if not result:
                # 子功能退出，清理模块并重绘子菜单
                self._feature = None
                mod_name = self.text_[self.mod][1]
                key = 'morse.' + mod_name
                if key in sys.modules:
                    del sys.modules[key]
                gc.collect()
                self.Morse.Ui('摩尔斯电码')
                self.MainUi(self.mod)
                self.NumUi(self.mod)
                self.btn.reset()
            return not self.Exit_

        # ----- 子菜单导航 -----
        a_short, b_short, c_short, b_long = self.btn.scan()
        if a_short:
            self.state = -1
        if c_short:
            self.state = 1
        if b_short:
            self.state = 0
            self._enter_feature()
            return not self.Exit_

        if self.state != 0:
            gc.collect()
            self.HeiUi()
            self.mod = self.move_num(self.mod + self.state)
            self.NumUi(self.mod)
            self.MainUi(self.mod)
            self.state = 0

        utime.sleep_ms(30)
        return not self.Exit_

    def _enter_feature(self):
        """动态加载并进入子功能；选中「退出」项则直接返回主菜单"""
        mod_name = self.text_[self.mod][1]
        # 「退出」菜单项：B 短按确认后退出摩斯电码，返回主菜单
        if mod_name == '__exit__':
            self.Exit_ = True
            return
        loading(self.tft)
        gc.collect()
        try:
            full_path = 'morse.' + mod_name
            mod = __import__(full_path)
            for part in full_path.split('.')[1:]:
                mod = getattr(mod, part)
            cls_name = _FEATURE_CLASS.get(mod_name, 'Feature')
            feature_cls = getattr(mod, cls_name)
            self._feature = feature_cls(self.tft, self.buttons, self.Morse)
        except Exception as e:
            print("[MORSE] Feature load error:", e)
            import sys
            sys.print_exception(e)
            self._feature = None
            self.Morse.Ui('摩尔斯电码')
            self.MainUi(self.mod)
            self.NumUi(self.mod)

    def exit(self):
        """关闭 LED 和蜂鸣器"""
        if hasattr(self, 'Morse') and self.Morse:
            if self.Morse.isled:
                self.Morse.Led.off()
            if self.Morse.isbuzzer:
                self.Morse.buzzer.off()
