"""
抄码训练 - 摩斯电码听写训练（3 级难度 + 计时 + 答案揭示）
移植自源项目 morse/chaoma.py

适配点：
  - 4 键 IRQ -> 3 键轮询（A=出题, C=难度, B短=答案, B长=退出）
  - 图片路径 sd/pic/ -> /pic/
  - config.hex2rgb -> hex2rgb
  - clock() 内增加按钮轮询（源用 IRQ，目标用轮询）
  - run() 改为 loop() 逐帧返回
"""
import gc
import utime
import random
from font import hanzi100 as font
from font import digitalnumbers
from morse.tft_helpers import hex2rgb, ButtonAdapter, load_en_font, draw_en
from morse import probars
from morse.data.danci import DanCi

# 计时器串（"00:00"）字体：优先矢量 italicc，缺失则降级 vga1 位图字体
font_En, _EN_VECTOR = load_en_font()


class ChaoMa:
    def __init__(self, tft_, buttons, morse):
        self.tft = tft_
        self.btn = ButtonAdapter(buttons)
        self.Morse = morse
        self.nandu = 1
        self.tid = 0
        self.Morse.Ui('抄码训练')
        self.abc = ''
        self.code = {}
        self.str_times = ''
        self.HomeUi()
        self.Ac = False
        self.butB = False
        self.radio = False
        self.Exit_ = False

    def HomeUi(self):
        self.tft.fill_rect(118, 160, 55, 32, hex2rgb('#BBE1FA'))
        self.tft.write(font, '出题', 121, 164, hex2rgb('#000000'), hex2rgb('#BBE1FA'))

        self.tft.fill_rect(181, 160, 55, 32, hex2rgb('#BBE1FA'))
        self.tft.write(font, '难度', 184, 164, hex2rgb('#000000'), hex2rgb('#BBE1FA'))

        self.tft.fill_rect(118, 200, 55, 32, hex2rgb('#BBE1FA'))
        self.tft.write(font, '答案', 120, 204, hex2rgb('#000000'), hex2rgb('#BBE1FA'))

        self.tft.fill_rect(181, 200, 55, 32, hex2rgb('#BBE1FA'))
        self.tft.write(font, '退出', 184, 204, hex2rgb('#000000'), hex2rgb('#BBE1FA'))

        self.tft.png("/pic/morse/nd_{}.png".format(self.nandu), 9, 160, True)
        self.tft.png("/pic/morse/cm_0.png", 71, 68, True)

    def clock(self, font_, x_, y_):
        starTime = utime.time()
        while not (self.radio or self.Ac) and not self.Exit_:
            # 轮询按钮（源用 IRQ，目标用轮询）
            a_short, b_short, c_short, b_long = self.btn.scan()
            if b_long:
                self.Exit_ = True
                return
            if a_short:
                self.Ac = True
            if c_short:
                self.butB = True
                self.nandu += 1
                if self.nandu > 3:
                    self.nandu = 1
            if b_short:
                self.radio = True
            if self.radio or self.Ac:
                break

            seconds = utime.time() - starTime
            _monutes = seconds // 60
            _seconds = seconds % 60
            self.str_times = "{:02d}:{:02d}".format(_monutes, _seconds)
            self.tft.write(font_, self.str_times, x_, y_, hex2rgb('#FF0000'), 0)
            if seconds > 90:
                self.radio = True
                break
            utime.sleep_ms(100)

    def readti(self, nandus_):
        mess = ''
        if nandus_ == 1:
            mess = self.Morse.morse_ma[random.randrange(0, len(self.Morse.morse_ma))][0]
        elif nandus_ == 2:
            for i in range(3):
                mess += self.Morse.morse_ma[random.randrange(0, 35)][0]
        elif nandus_ == 3:
            mess = DanCi[random.randrange(0, 981)][0]
        return mess

    def boyin(self):
        self.abc = self.readti(self.nandu).upper()
        self.code = self.Morse.morse(self.abc)
        # 显示计时器
        self.tft.fill_rect(0, 52, 240, 50, 0)
        self.tft.png("/pic/time_1.png", 5, 65, True)
        self.clock(digitalnumbers, 60, 90)

    def loop(self):
        if self.Exit_:
            return False

        a_short, b_short, c_short, b_long = self.btn.scan()
        if b_long:
            # 退出按钮闪红
            self.tft.fill_rect(181, 200, 55, 32, hex2rgb('#FF5733'))
            utime.sleep(0.1)
            self.tft.fill_rect(181, 200, 55, 32, hex2rgb('#BBE1FA'))
            self.tft.write(font, '退出', 184, 204, hex2rgb('#000000'), hex2rgb('#BBE1FA'))
            return False
        if a_short:
            self.Ac = True
            self.tid += 1
        if c_short:
            self.butB = True
            self.nandu += 1
            if self.nandu > 3:
                self.nandu = 1
        if b_short:
            self.radio = True

        if self.Ac:
            self.tft.fill_rect(118, 160, 55, 32, hex2rgb('#FF5733'))
            utime.sleep(0.1)
            self.tft.fill_rect(118, 160, 55, 32, hex2rgb('#BBE1FA'))
            self.tft.write(font, '出题', 121, 164, hex2rgb('#000000'), hex2rgb('#BBE1FA'))
            self.Ac = False
            # 进度条等待
            self.tft.fill_rect(0, 52, 240, 100, 0)
            jdt = probars.Bars(self.tft, 0, 52, 240, 4, jdcolor=hex2rgb('#f4ea2a'))
            jdt.Ui_line()
            # 开始播报提示
            self.tft.png("/pic/jbyb.png", 60, 90, True)
            self.tft.png("/pic/mic_1.png", 5, 65, True)
            self.boyin()

        elif self.butB:
            self.tft.png("/pic/morse/nd_{}.png".format(self.nandu), 9, 160, True)
            self.tft.fill_rect(181, 160, 55, 32, hex2rgb('#FF5733'))
            utime.sleep(0.1)
            self.tft.fill_rect(181, 160, 55, 32, hex2rgb('#BBE1FA'))
            self.tft.write(font, '难度', 184, 164, hex2rgb('#000000'), hex2rgb('#BBE1FA'))
            self.butB = False

        elif self.radio:
            self.tft.fill_rect(118, 200, 55, 32, hex2rgb('#FF5733'))
            utime.sleep(0.1)
            self.tft.fill_rect(118, 200, 55, 32, hex2rgb('#BBE1FA'))
            self.tft.write(font, '答案', 120, 204, hex2rgb('#000000'), hex2rgb('#BBE1FA'))
            self.radio = False
            # 显示单词并重复播报一次
            # 清理显示区域的计时器
            self.tft.fill_rect(60, 90, 160, 50, 0)
            self.tft.png("/pic/time_1.png", 5, 65, True)
            # 计时器：矢量字体 draw(左下角 y=83)，位图降级 text(左上角 y=75)
            draw_en(self.tft, font_En, _EN_VECTOR,
                    self.str_times, 48, 83,
                    hex2rgb('#FF0000'), fallback_y=75)
            # 显示答案，字母单词
            self.tft.png("/pic/wt_2.png", 5, 110, True)
            self.tft.write(font, self.abc, 48, 115, hex2rgb('#FFFFFFF'))

        gc.collect()
        utime.sleep(0.05)
        return True
