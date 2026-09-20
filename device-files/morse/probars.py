'''
进度条
移植自源项目 probars.py

适配点：
  - config.hex2rgb -> morse.tft_helpers.hex2rgb
  - 默认颜色参数在函数签名处用 hex2rgb 计算
'''
import utime
from morse.tft_helpers import hex2rgb

'''
进度条
'''


class Bars:
    def __init__(self, tft_, x=0, y=120, w=240, h=20,
                 boxcolor=hex2rgb('#BBE1FA'),
                 jdcolor=hex2rgb('#ee7800'),
                 bgcolor=hex2rgb('#111111')):
        self.tft_ = tft_
        self.boxcolor = boxcolor
        self.jdcolor = jdcolor
        self.bgcolor = bgcolor
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def Ui_line(self, bg_=0, time_=10, at_=False):
        # 长条
        self.tft_.fill_rect(self.x, self.y, self.w, self.h, bg_)
        for i in range(self.w):
            self.tft_.fill_rect(self.x, self.y, i, self.h, self.jdcolor)
            if at_:
                return i
            if time_ != 0:
                utime.sleep_ms(time_)

    def Ui_rect(self, bg_=0, time_=10):
        # 长方形
        # 清屏
        self.tft_.fill_rect(self.x, self.y, self.w, self.h, bg_)
        # 画框
        self.tft_.rect(self.x, self.y, self.w, self.h, self.boxcolor)
        self.tft_.fill_rect(self.x + 1, self.y + 1, self.w - 2, self.h - 2, self.bgcolor)

        for i in range(self.w - 3):
            self.tft_.fill_rect(self.x + 2, self.y + 2, i, self.h - 4, self.jdcolor)
            utime.sleep_ms(time_)

    def boxjd(self, ratio):
        if ratio < 98 and ratio > 0:
            self.tft_.fill_circle(20, 120, 8, self.jdcolor)
            self.tft_.fill_rect(20, 112, ratio * 2, 17, self.jdcolor)
        elif ratio > 98:
            self.tft_.fill_circle(20, 120, 8, self.jdcolor)
            self.tft_.fill_rect(20, 112, ratio * 2, 17, self.jdcolor)
            self.tft_.fill_circle(220, 120, 8, self.jdcolor)
