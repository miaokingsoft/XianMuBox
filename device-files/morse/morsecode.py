'''
摩尔斯电码核心引擎
移植自源项目 morse/morsecode.py

适配点：
  - LED 由 machine.Pin 改为 LedNeo（WS2812 RGB LED）
  - 蜂鸣器由 machine.Pin 改为 framework.hal.Buzzer 实例
  - 图片路径 sd/pic/ -> /pic/
  - OrderedDict 改用 dict（MicroPython 兼容，保持插入顺序）
'''
import utime


class MorseCode(object):
    def __init__(self, tft_, led_, buzzer_, topbg_, topfg_, fontcolor_, codecolor_):
        from font import hanzi1k as font
        self._font = font
        self.tft = tft_
        self.topbg = topbg_
        self.topfg = topfg_
        self.fontcolor = fontcolor_
        self.codecolor = codecolor_

        # LED: LedNeo 实例或 None
        self.Led = led_
        self.isled = led_ is not None
        if self.isled:
            self.Led.off()

        # 蜂鸣器: Buzzer 实例或 None
        self.buzzer = buzzer_
        self.isbuzzer = buzzer_ is not None
        if self.isbuzzer:
            self.buzzer.off()

        self.DOT_LENGTH = 0.1  # seconds value for dot '.'
        self.morse_ma = [
            ('A', '.-'),
            ('B', '-...'),
            ('C', '-.-.'),
            ('D', '-..'),
            ('E', '.'),
            ('F', '..-.'),
            ('G', '--.'),
            ('H', '....'),
            ('I', '..'),
            ('J', '.---'),
            ('K', '-.-'),
            ('L', '.-..'),
            ('M', '--'),
            ('N', '-.'),
            ('O', '---'),
            ('P', '.--.'),
            ('Q', '--.-'),
            ('R', '.-.'),
            ('S', '...'),
            ('T', '-'),
            ('U', '..-'),
            ('V', '...-'),
            ('W', '.--'),
            ('X', '-..-'),
            ('Y', '-.--'),
            ('Z', '--..'),
            ('0', '-----'),
            ('1', '.----'),
            ('2', '..---'),
            ('3', '...--'),
            ('4', '....-'),
            ('5', '.....'),
            ('6', '-....'),
            ('7', '--...'),
            ('8', '---..'),
            ('9', '----.'),
            (',', '--..--'),
            ('.', '.-.-.-'),
            ('?', '..--..'),
            (';', '-.-.-.'),
            (':', '---...'),
            ("'", '.----.'),
            ('-', '-....-'),
            ('/', '-..-.'),
            ('(', '-.--.-'),
            (')', '-.--.-'),
            ('_', '..--.-'),
            (' ', ' ')
        ]
        self.morsetab = dict(self.morse_ma)

    def Ui(self, title):
        self.tft.fill(0)
        # 顶部
        self.tft.fill_rect(0, 0, 240, 50, self.topbg)
        # 标题
        self.tft.write(self._font, title, 10, 14, self.topfg, self.topbg)
        self.tft.png("/pic/3-0.png", 200, 15, True)

    def DuanYuUi(self):
        # 电码显示框
        self.tft.fill_rect(0, 170, 60, 60, self.codecolor)
        self.tft.rect(70, 170, 170, 60, self.codecolor)

    def toggle_Ac(self, is_on):
        # is_on=0: 激活（LED亮+蜂鸣器响）
        # is_on=1: 关闭（LED灭+蜂鸣器静）
        if self.isled:
            self.Led.value(is_on)
        if self.isbuzzer:
            if is_on == 0:
                self.buzzer.on()
            else:
                self.buzzer.off()

    def _encode_morse(self, message):
        return [self.morsetab.get(c.upper(), '?') for c in message]

    def input_xs(self, message):
        self.tft.fill_rect(0, 51, 240, 119, 0)
        line = 66
        col = 6
        font = self._font
        for char in message:
            glyph_width = font.WIDTHS[font.MAP.index(char)]
            self.tft.write(font, char, col, line, self.fontcolor, 0)
            col += glyph_width + 2
            if col > 220:
                col = 10
                line += 44

    def code_xs(self, message):
        message = message.upper()
        font = self._font
        glyph_width = font.WIDTHS[font.MAP.index(message)]
        x_ = (60 - glyph_width) // 2
        y_ = 170 + (60 - font.HEIGHT) // 2
        self.tft.fill_rect(0, 170, 60, 60, self.codecolor)
        self.tft.write(font, message, x_, y_, self.fontcolor, self.codecolor)

    def morse_info(self, message):
        self.input_xs(message)
        morse_code = ""

        for char_ in message:
            self.code_xs(char_)
            char_ = self.morsetab.get(char_.upper(), '?')
            self.tft.fill_rect(71, 171, 168, 58, 0)
            self.tft.write(self._font, char_, 80, 185, self.fontcolor, 0)
            for char in char_:
                self.toggle_Ac(1)
                utime.sleep(self.DOT_LENGTH * 3)  # pause between characters
                if char == ' ':
                    morse_code += char
                    utime.sleep(self.DOT_LENGTH * 7)  # pause between words
                else:
                    for code in char:
                        morse_code += code
                        utime.sleep(self.DOT_LENGTH)  # pause between code elements
                        if code == '.':
                            self.toggle_Ac(0)
                            utime.sleep(self.DOT_LENGTH)  # pause between dots
                        else:
                            self.toggle_Ac(0)
                            utime.sleep(self.DOT_LENGTH * 3)  # pause between dashes
                        self.toggle_Ac(1)
        if morse_code == "":
            morse_code = "error"

        return {'code': morse_code}

    def morse(self, message):
        morse_code = ""
        message = self._encode_morse(message)
        for char in message:
            self.toggle_Ac(1)
            utime.sleep(self.DOT_LENGTH * 3)  # pause between characters
            if char == ' ':
                morse_code += char
                utime.sleep(self.DOT_LENGTH * 7)  # pause between words
            else:
                for code in char:
                    morse_code += code
                    utime.sleep(self.DOT_LENGTH)  # pause between code elements
                    if code == '.':
                        self.toggle_Ac(0)
                        utime.sleep(self.DOT_LENGTH)  # pause between dots
                    else:
                        self.toggle_Ac(0)
                        utime.sleep(self.DOT_LENGTH * 3)  # pause between dashes
                    self.toggle_Ac(1)
        if morse_code == "":
            morse_code = "error"

        return message
