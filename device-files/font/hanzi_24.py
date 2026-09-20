# -*- coding: utf-8 -*-
# hanzi_24: 全量汉字 24px 点阵字体 (GB2312 全区 6763 汉字 + ASCII/空格 91 字形)
# 渲染源 AlimamaFangYuanTiVF-Thin.ttf (tools/gen_full_font.py 生成), 数据以
# .incbin 嵌入固件 .rodata, 经 romfont C 模块零拷贝暴露; PC 仿真由
# tools/simulator/stubs.py 注入 romfont 桩, 读 firmware/romfont/hanzi_24_full.bin。
# 接口与旧 hz_24x24 完全一致 (MAP/BPP/HEIGHT/MAX_WIDTH/OFFSET_WIDTH/
# WIDTHS/OFFSETS/BITMAPS 及 _WIDTHS/_OFFSETS/_BITMAPS 别名)。
# 字模基线冻结: HEIGHT=25, 基线偏移 5, 变宽取 bbox 右缘 (与旧字库逐位一致)。

import struct
import romfont

_B = romfont.data()

BPP = _B[4]
HEIGHT = _B[5]
MAX_WIDTH = _B[6]
OFFSET_WIDTH = _B[7]
_N, _MAP_LEN, _W_OFF, _O_OFF, _B_OFF, _B_LEN = struct.unpack("<6I", _B[8:32])
MAP = str(bytes(_B[0x20:0x20 + _MAP_LEN]), "utf-8")

WIDTHS = _B[_W_OFF:_W_OFF + _N]
OFFSETS = _B[_O_OFF:_O_OFF + _N * OFFSET_WIDTH]
BITMAPS = _B[_B_OFF:_B_OFF + _B_LEN]
_WIDTHS = WIDTHS
_OFFSETS = OFFSETS
_BITMAPS = BITMAPS
