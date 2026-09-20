"""
字符码表 - 浏览 47 个字符的摩斯电码并播放
移植自源项目 morse/mabiao.py

适配点：
  - 4 键 IRQ -> 3 键轮询（A=上一个, C=下一个, B短=播放, B长=返回）
  - 图片路径 sd/pic/ -> /pic/
  - config.hex2rgb -> hex2rgb
  - run() 改为 loop() 逐帧返回
"""
import gc
import utime
from font import hanzi100 as font
from morse.tft_helpers import hex2rgb, ButtonAdapter, load_en_font, draw_en

# 英文摩斯码串（".-.-"）字体：优先矢量 italicc，缺失则降级 vga1 位图字体
font_En, _EN_VECTOR = load_en_font()


class MaBiao:
    def __init__(self, tft_, buttons, morse):
        self.tft = tft_
        self.btn = ButtonAdapter(buttons)
        self.Morse = morse
        self.uid = 0
        self.Morse.Ui('字符码表')
        self.abc = self.Morse.morse_ma[self.uid][0]
        self.HomeUi()
        self.state = 0
        self.radio = False
        self.Exit_ = False

    def HomeUi(self):
        self.tft.fill_rect(60, 60, 120, 120, 0)
        self.tft.png("/pic/morse/{}.png".format(self.abc), 60, 60, True)
        self.tft.png("/pic/morse/mabiao_0.png", 0, 195, True)
        # 摩斯码串（".-.-"）：矢量字体 draw(左下角 y=213)，位图降级 text(左上角 y=205)
        draw_en(self.tft, font_En, _EN_VECTOR,
                self.Morse.morse_ma[self.uid][1], 106, 213,
                hex2rgb('#FFFFFF'), fallback_y=205)

    def fayin(self, mes):
        # 显示小喇叭
        self.tft.png("/pic/mic_1.png", 3, 62, True)
        # 播音
        self.Morse.morse(mes)
        # 关闭小喇叭显示
        self.tft.fill_rect(3, 62, 32, 32, 0)

    def loop(self):
        a_short, b_short, c_short, b_long = self.btn.scan()
        if b_long:
            return False
        if a_short:
            self.radio = False
            self.state = -1
        if c_short:
            self.radio = False
            self.state = 1
        if b_short:
            self.radio = True
            self.state = 0

        if self.radio:
            self.fayin(self.abc)
            self.radio = False
        elif self.state != 0:
            n = self.uid + self.state
            if n < 0:
                n = 46
            elif n > 46:
                n = 0
            self.uid = n
            self.abc = self.Morse.morse_ma[self.uid][0]
            self.HomeUi()
            self.state = 0

        gc.collect()
        utime.sleep_ms(30)
        return True
