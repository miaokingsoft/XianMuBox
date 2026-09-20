"""
常用短语 - 浏览 96 条摩斯电码常用短语并逐字播放
移植自源项目 morse/duanyu.py

适配点：
  - 4 键 IRQ -> 3 键轮询（A/C=下一条, B短=播放, B长=返回）
  - 图片路径 sd/pic/ -> /pic/
  - config.hex2rgb/LineAuto/RowAuto -> hex2rgb/LineAuto/RowAuto
  - run() 改为 loop() 逐帧返回
"""
import gc
import utime
import random
from font import duanyu24 as font
from morse.tft_helpers import hex2rgb, LineAuto, RowAuto, ButtonAdapter
from morse.data.dy import Morse_Dy


class DuanYu:
    def __init__(self, tft_, buttons, morse):
        self.tft = tft_
        self.btn = ButtonAdapter(buttons)
        self.Morse = morse
        self.uid = random.randrange(0, len(Morse_Dy))
        self.code = ''
        self.Morse.Ui('常用短语')
        self.HomeUi()
        self.Ac = False
        self.radio = False
        self.Exit_ = False

    def Dy_txt(self):
        code, title = Morse_Dy[self.uid]
        self.code = code
        self.tft.write(font, code, 5, 65, hex2rgb('#CCCCCC'), 0)
        title_xy = LineAuto(font, title, 220, 80, 5, 112)
        for x, y, txt in title_xy:
            self.tft.write(font, txt, x, y, hex2rgb('#A5D63F'), 0)

    def HomeUi(self):
        self.tft.png("/pic/mic_0.png", 120, 62, True)
        self.tft.line(0, 100, 240, 100, hex2rgb('#3282B8'))
        self.Dy_txt()

    def duqu(self, ac):
        gc.collect()
        code, title = Morse_Dy[self.uid]
        self.tft.fill_rect(5, 64, 100, 32, 0)
        self.tft.fill_rect(5, 112, 220, 80, 0)
        self.Dy_txt()

    def bofang(self):
        gc.collect()
        self.tft.png("/pic/mic_1.png", 120, 62, True)
        mess = RowAuto(font, self.code, 110, 5, 65)
        morse_code = ""
        DOT_LENGTH = self.Morse.DOT_LENGTH
        for x, y, char__ in mess:
            self.tft.write(font, char__, x, y, 0, hex2rgb('#A5D63F'))
            char_ = self.Morse.morsetab.get(char__.upper(), '?')
            self.tft.fill_rect(160, 62, 80, 35, 0)
            self.tft.write(font, char_, 160, 65, hex2rgb('#A5D63F'), 0)
            for char in char_:
                self.Morse.toggle_Ac(1)
                utime.sleep(DOT_LENGTH * 3)  # pause between characters
                if char == ' ':
                    utime.sleep(DOT_LENGTH * 7)  # pause between words
                else:
                    for code in char:
                        utime.sleep(DOT_LENGTH)  # pause between code elements
                        if code == '.':
                            self.Morse.toggle_Ac(0)
                            utime.sleep(DOT_LENGTH)  # pause between dots
                        else:
                            self.Morse.toggle_Ac(0)
                            utime.sleep(DOT_LENGTH * 3)  # pause between dashes
                        self.Morse.toggle_Ac(1)
            self.tft.write(font, char__, x, y, hex2rgb('#CCCCCC'), 0)
        self.tft.png("/pic/mic_0.png", 120, 62, True)
        self.tft.fill_rect(160, 62, 80, 35, 0)

    def loop(self):
        a_short, b_short, c_short, b_long = self.btn.scan()
        if b_long:
            return False
        if a_short or c_short:
            # 下一条短语
            if self.uid == len(Morse_Dy) - 1:
                self.uid = 0
            else:
                self.uid += 1
            self.Ac = True
            self.radio = False
        if b_short:
            self.radio = True
            self.Ac = True

        if self.Ac:
            if self.radio:
                self.bofang()
                self.radio = False
            else:
                self.duqu(self.uid)
            self.Ac = False

        gc.collect()
        utime.sleep_ms(30)
        return True
