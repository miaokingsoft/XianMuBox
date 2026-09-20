"""
self_media.py - 闲沐盒子 自媒统计模式

从 settings.json 的 project.self_media 读取账号列表, 按 url 拉取
{fans, works, likes, views} JSON, 两页 UI: 平台轮播入口 + 详情页 2x2 数据网格。
规格: doc/自媒统计模式功能文档.md (顶栏标题按项目统一改为「闲沐盒子」)
"""

import time
import gc

from font import hanzi_24 as hanzi24
from .base import BaseMode
from framework.colors import (
    CLR_SM_BG, CLR_SM_HDR_BG, CLR_SM_HDR_FG, CLR_SM_ACCENT, CLR_SM_DIV,
    CLR_SM_CHEV, CLR_SM_DIM, CLR_SM_TXT_LBL, CLR_SM_TXT_VAL,
)

# ===== 屏幕尺寸 =====
try:
    from config import W
except ImportError:
    W = 240

_HANZI_H = 25  # hanzi24 实际像素高度


# ===== 平台枚举与样式 =====

_CATEGORIES = ("douyin", "kuaishou", "xiaohongshu", "bilibili")

_DEFAULT_NAME = {
    "douyin":      "我的抖音",
    "kuaishou":    "快手",
    "xiaohongshu": "小红书",
    "bilibili":    "B站UP主",
}

# 程序绘制图标: (底色, 描边色, 短名)
_PLATFORM_STYLE = {
    "douyin":      (0x0000, CLR_SM_ACCENT, "抖音"),
    "kuaishou":    (0xFD20, 0xFFFF,        "快手"),
    "xiaohongshu": (0xF800, 0xFFFF,        "红书"),
    "bilibili":    (0xF9D3, 0xFFFF,        "B站"),
}

# 设备上若存在美工 PNG 则优先使用 (按 category 路由)
_ICON_BY_CATEGORY = {
    "douyin":      "/icons/douyin.png",
    "kuaishou":    "/icons/kuaishou.png",
    "xiaohongshu": "/icons/xiaohongshu.png",
    "bilibili":    "/icons/bilibili.png",
}
_PNG_MISSING = set()  # 已确认不存在的 PNG 路径缓存


# ===== 演示数据兜底 (无配置 / 拉取失败时 UI 永远可渲染) =====

_MOCK_DATA = {
    "douyin":      {"fans": 8881, "works": 123, "likes": 80000, "views": 12},
    "kuaishou":    {"fans": 1234, "works": 56,  "likes": 20000, "views": 8},
    "xiaohongshu": {"fans": 5678, "works": 99,  "likes": 30000, "views": 5},
    "bilibili":    {"fans": 8881, "works": 123, "likes": 80000, "views": 12},
}


def _fallback_fields(category):
    return dict(_MOCK_DATA.get(category) or {"fans": 0, "works": 0, "likes": 0, "views": 0})


def fmt_num(n):
    """数字友好格式化: 8881 / 8w / 1.2ww 风格 (严格按功能文档)"""
    n = int(n or 0)
    if n < 10000:
        return str(n)
    if n < 100000000:
        v = n / 10000
        if v >= 1000:
            return str(int(v)) + "w"
        s = "%.1f" % v
        if s.endswith(".0"):
            s = s[:-2]
        return s + "w"
    v = n / 100000000
    s = "%.1f" % v
    if s.endswith(".0"):
        s = s[:-2]
    return s + "ww"


# ===== 配置读取 =====

def _load_platforms():
    """读取 project.self_media, 过滤非法 category, 补默认名
    返回: [{"category","name","url","fields"}]
    """
    raw = []
    try:
        from framework.settings import get_project
        raw = (get_project() or {}).get("self_media") or []
    except Exception:
        raw = []

    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        cat = str(it.get("category", "")).strip().lower()
        if cat not in _CATEGORIES:
            continue
        name = str(it.get("name", "")).strip() or _DEFAULT_NAME.get(cat, cat)
        out.append({
            "category": cat,
            "name": name[:32],  # 顶栏最多 4 汉字, 预留截断
            "url": str(it.get("url", "")).strip()[:512],
            "fields": {},
        })

    if not out:
        # 空配置 -> 内置演示数据, 保证轮播永远可演示
        for cat in _CATEGORIES:
            out.append({
                "category": cat,
                "name": _DEFAULT_NAME[cat],
                "url": "",
                "fields": {},
            })
    return out


def _http_get_json(url):
    """GET 请求并解析 JSON, 返回 (status, data_dict)
    在 finally 中关闭 response, 防止 MicroPython 连接泄漏 (与 dashboard 一致)
    """
    try:
        import urequests as requests
    except ImportError:
        import requests
    try:
        r = requests.get(url, timeout=8)
        try:
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        finally:
            try:
                r.close()
            except Exception:
                pass
        return r.status_code, data
    except Exception as e:
        return 0, {"__error__": str(e)}


# ===== 布局常量 =====

PAGE_MAIN = 0    # 第 1 页: 轮播入口
PAGE_DETAIL = 1  # 第 2 页: 详情页

HDR_H = 50       # 两页顶栏高度
MAIN_ICON_X = 80
MAIN_ICON_Y = 85
MAIN_ICON_SZ = 80
MAIN_CHEV_Y = MAIN_ICON_Y + MAIN_ICON_SZ // 2 - 8   # chevron 与图标垂直居中 (y=117)
MAIN_NAME_Y = 186
DOT_Y = 226
DOT_SIZE = 5
DOT_GAP = 12

DET_ICON_X = 80
DET_ICON_Y = 80
DET_ICON_SZ = 80
DET_CHEV_Y = DET_ICON_Y + DET_ICON_SZ // 2 - 8      # chevron 与图标垂直居中 (y=112)
DET_ROW1_Y = 183
DET_ROW2_Y = 214
DET_LEFT_X = 10
DET_RIGHT_X = 130
DET_CELL_W = 108  # 每格宽度 (数值右对齐到 cell_x + DET_CELL_W)

ANIM_STEPS = 10   # 滑动动画步数 (raw blit 足够快, 步数多更顺滑)
ANIM_DELAY = 12   # 每步延时 ms

_ICON_RAW_SZ = MAIN_ICON_SZ * MAIN_ICON_SZ * 2  # 80x80 RGB565 = 12800 字节
_raw_cache = {}  # category -> bytearray | None (None = raw 不存在, 需走 PNG/程序绘制)


def _load_raw_icon(category):
    """加载 RGB565 裸缓冲图标 (build 阶段由 PNG 生成); 失败返回 None"""
    if category in _raw_cache:
        return _raw_cache[category]
    buf = None
    try:
        import os
        path = "/icons/%s.raw" % category
        os.stat(path)
        f = open(path, "rb")
        try:
            data = f.read()
        finally:
            f.close()
        if len(data) == _ICON_RAW_SZ:
            buf = bytearray(data)
    except Exception:
        buf = None
    _raw_cache[category] = buf
    return buf


# 滑动动画用的裁剪 blit 暂存缓冲 (避免逐帧分配)
_slide_buf = bytearray(_ICON_RAW_SZ)
_slide_mv = memoryview(_slide_buf)


def _blit_clipped(d, buf, ix, iy):
    """blit_buffer 的屏幕边界裁剪版: ix 可为负 / 越界, 只写可见部分"""
    sx0 = 0
    sx1 = MAIN_ICON_SZ
    if ix < 0:
        sx0 = -ix
    if ix + MAIN_ICON_SZ > W:
        sx1 = W - ix
    if sx1 <= sx0:
        return
    w = sx1 - sx0
    stride = MAIN_ICON_SZ * 2
    seg_len = w * 2
    p = 0
    for row in range(MAIN_ICON_SZ):
        src = row * stride + sx0 * 2
        _slide_mv[p:p + seg_len] = buf[src:src + seg_len]
        p += seg_len
    d.blit_buffer(_slide_mv[:p], max(ix, 0), iy, w, MAIN_ICON_SZ)


def _hanzi_text_width(text):
    w = 0
    for ch in text:
        try:
            idx = hanzi24.MAP.index(ch)
            w += hanzi24._WIDTHS[idx]
        except ValueError:
            w += _HANZI_H  # 缺字 fallback 方阵宽
    return w


def _draw_chevron(d, x, y, direction):
    """direction: -1 = 左(<), +1 = 右(>)"""
    s = 16
    if direction < 0:
        d.line(x + s - 2, y, x + 2, y + s // 2, CLR_SM_CHEV)
        d.line(x + 2, y + s // 2, x + s - 2, y + s, CLR_SM_CHEV)
    else:
        d.line(x + 2, y, x + s - 2, y + s // 2, CLR_SM_CHEV)
        d.line(x + s - 2, y + s // 2, x + 2, y + s, CLR_SM_CHEV)


def _png_exists(path):
    if path in _PNG_MISSING:
        return False
    try:
        import os
        os.stat(path)
        return True
    except OSError:
        _PNG_MISSING.add(path)
        return False
    except ImportError:
        return False


def _draw_platform_icon(d, category, x, y):
    """平台大图标: 优先 .raw blit (最快) -> /icons/{category}.png -> 程序绘制"""
    buf = _load_raw_icon(category)
    if buf is not None:
        try:
            d.blit_buffer(buf, x, y, MAIN_ICON_SZ, MAIN_ICON_SZ)
            return
        except Exception:
            _raw_cache[category] = None

    path = _ICON_BY_CATEGORY.get(category)
    if path and _png_exists(path):
        try:
            d.png(path, x, y, True)
            return
        except Exception:
            _PNG_MISSING.add(path)

    style = _PLATFORM_STYLE.get(category, (0x1082, CLR_SM_ACCENT, "自媒"))
    bg, border, short = style
    d.fill_rect(x, y, MAIN_ICON_SZ, MAIN_ICON_SZ, bg)
    d.rect(x, y, MAIN_ICON_SZ, MAIN_ICON_SZ, border)
    # 短名居中 (2 汉字 = 48px; B站 = 14+24=38px)
    tw = _hanzi_text_width(short)
    d.write(hanzi24, short, x + (MAIN_ICON_SZ - tw) // 2, y + (MAIN_ICON_SZ - _HANZI_H) // 2,
            CLR_SM_TXT_VAL, bg)


def _draw_contact(d, x, y):
    """顶栏右上角双人形 (无 PNG, 程序绘制): 两个圆头 + 身体"""
    has_circle = hasattr(d, "fill_circle")
    if has_circle:
        d.fill_circle(x + 4, y + 3, 4)
    else:
        d.fill_rect(x, y, 8, 8, CLR_SM_HDR_FG)
    d.fill_rect(x, y + 8, 8, 8, CLR_SM_HDR_FG)        # 左身体
    if has_circle:
        d.fill_circle(x + 16, y + 5, 4)
    else:
        d.fill_rect(x + 12, y + 2, 8, 8, CLR_SM_HDR_FG)
    d.fill_rect(x + 12, y + 10, 8, 6, CLR_SM_HDR_FG)  # 右身体 (稍矮, 前后层次)


class SelfMediaMode(BaseMode):
    MODE_ID = "self_media"
    MODE_NAME = "自媒统计"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self.platforms = []
        self._idx = 0
        self._page = PAGE_MAIN
        # 分步预取状态: -1=未开始, 0~n-1=正在取第 N 个, n=全部完成
        self._load_step = -1

    def enter(self):
        self._idx = 0
        self._page = PAGE_MAIN
        gc.collect()
        self.platforms = _load_platforms()
        gc.collect()
        self._draw_main()

    def loop(self):
        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            if self._page == PAGE_MAIN:
                # A 短按 = 翻页(下一平台), B 短按 = 进详情, C 短按 = 退出
                if up:
                    self._idx = (self._idx + 1) % len(self.platforms)
                    self.beep(800, 15)
                    self._slide(+1)
                elif down:
                    self.flash_exit()
                    return False
                elif sel:
                    self.beep(1200, 30)
                    self._page = PAGE_DETAIL
                    self._draw_detail()
            else:
                # 详情页: A 短按 = 返回轮播, B 短按 = 手动刷新当前平台, C 短按 = 退出
                if up:
                    self.beep(900, 15)
                    self._page = PAGE_MAIN
                    self._draw_main()
                elif down:
                    self.flash_exit()
                    return False
                elif sel:
                    plat = self.platforms[self._idx]
                    plat["fields"] = {}
                    self._draw_detail()  # 内部会重新拉取

            # 后台分步预取数据 (每轮至多一个平台, 避免阻塞 UI)
            self._step_load()

            time.sleep_ms(50)
            return True

        except Exception as e:
            print("[SELF_MEDIA] loop error:", repr(e))
            time.sleep_ms(200)
            return True

    def exit(self):
        self.platforms = None
        gc.collect()

    # ===== 数据分步预取 =====

    def _step_load(self):
        """每轮 loop 至多拉取一个平台; mock 平台(无 url)直接填兜底数据"""
        if not self.platforms or self._load_step >= len(self.platforms):
            return
        if self._load_step < 0:
            self._load_step = 0

        plat = self.platforms[self._load_step]
        if not plat["fields"]:
            self._fetch_one(plat)
        self._load_step += 1
        gc.collect()

    def _fetch_one(self, plat):
        """拉取单个平台数据; url 缺失或失败 -> mock 兜底"""
        url = plat.get("url", "")
        if not url:
            plat["fields"] = _fallback_fields(plat["category"])
            return
        status, data = _http_get_json(url)
        if status == 200 and isinstance(data, dict):
            try:
                plat["fields"] = {
                    "fans":  int(data.get("fans", 0) or 0),
                    "works": int(data.get("works", 0) or 0),
                    "likes": int(data.get("likes", 0) or 0),
                    "views": int(data.get("views", 0) or 0),
                }
                return
            except (ValueError, TypeError):
                pass
        print("[SELF_MEDIA] fetch failed:", plat["category"], status)
        plat["fields"] = _fallback_fields(plat["category"])

    # ===== 第 1 页: 轮播入口 =====

    def _draw_main(self):
        d = self.display
        plat = self.platforms[self._idx]

        d.fill(CLR_SM_BG)
        # 顶栏: 标题 + wifi 图标 + 分隔线
        d.write(hanzi24, "闲沐盒子", 10, 13, CLR_SM_DIV, CLR_SM_BG)
        try:
            d.png("/icons/wifi_on.png", 210, 13, True)
        except Exception:
            d.rect(210, 13, 20, 20, CLR_SM_ACCENT)
        d.hline(0, HDR_H - 1, W, CLR_SM_DIV)

        # 右翻页 chevron + 平台图标 (chevron 与图标垂直居中)
        _draw_chevron(d, 212, MAIN_CHEV_Y, +1)
        _draw_platform_icon(d, plat["category"], MAIN_ICON_X, MAIN_ICON_Y)

        # 平台名居中
        tw = _hanzi_text_width(plat["name"])
        d.write(hanzi24, plat["name"], max(0, (W - tw) // 2), MAIN_NAME_Y,
                CLR_SM_TXT_VAL, CLR_SM_BG)

        # 分页点
        n = len(self.platforms)
        total_w = n * DOT_SIZE + (n - 1) * (DOT_GAP - DOT_SIZE)
        sx = (W - total_w) // 2
        for i in range(n):
            dx = sx + i * DOT_GAP
            if i == self._idx:
                d.fill_rect(dx, DOT_Y, DOT_SIZE, DOT_SIZE, CLR_SM_ACCENT)
            else:
                d.rect(dx, DOT_Y, DOT_SIZE, DOT_SIZE, CLR_SM_DIM)

    def _slide(self, direction):
        """平台切换滑动动画: 多步平移过渡 (方向 +1 = 右滑进入)
        每帧仅 fill_rect + blit_buffer (内存拷贝), 无 PNG 解码, 保证顺滑
        """
        d = self.display
        n = len(self.platforms)
        old_cat = self.platforms[(self._idx - direction) % n]["category"]
        new_cat = self.platforms[self._idx]["category"]
        if old_cat == new_cat:
            self._draw_main()
            return
        # 预加载两块缓冲, 动画期间不做任何 IO/解码
        old_raw = _load_raw_icon(old_cat)
        new_raw = _load_raw_icon(new_cat)
        if old_raw is None or new_raw is None:
            # 无 raw 缓冲 (PNG/程序绘制路径): 逐帧解码太慢, 直接切换
            self._draw_main()
            return
        gc.collect()
        step_px = MAIN_ICON_SZ // ANIM_STEPS
        band_y = MAIN_ICON_Y - 6
        band_h = MAIN_ICON_SZ + 12
        for step in range(1, ANIM_STEPS + 1):
            d.fill_rect(0, band_y, W, band_h, CLR_SM_BG)
            old_off = direction * step * step_px
            new_off = -direction * (MAIN_ICON_SZ - step * step_px)
            _blit_clipped(d, old_raw, MAIN_ICON_X + old_off, MAIN_ICON_Y)
            _blit_clipped(d, new_raw, MAIN_ICON_X + new_off, MAIN_ICON_Y)
            time.sleep_ms(ANIM_DELAY)
        self._draw_main()
        gc.collect()

    # ===== 第 2 页: 详情页 =====

    def _draw_detail(self):
        d = self.display
        plat = self.platforms[self._idx]

        # 无缓存先显示连接提示再拉取 (阻塞, 8s 超时兜底)
        if not plat["fields"]:
            d.fill(CLR_SM_BG)
            d.fill_rect(0, 0, W, HDR_H, CLR_SM_HDR_BG)
            d.write(hanzi24, plat["name"], 10, 13, CLR_SM_HDR_FG, CLR_SM_HDR_BG)
            tw = _hanzi_text_width("连接中...")
            d.write(hanzi24, "连接中...", max(0, (W - tw) // 2), 110,
                    CLR_SM_TXT_LBL, CLR_SM_BG)
            self._fetch_one(plat)

        fields = plat["fields"]

        d.fill(CLR_SM_BG)
        # 绿色顶栏: 平台名 + 双人形
        d.fill_rect(0, 0, W, HDR_H, CLR_SM_HDR_BG)
        d.write(hanzi24, plat["name"], 10, 13, CLR_SM_HDR_FG, CLR_SM_HDR_BG)
        _draw_contact(d, 210, 13)

        # 平台大图标 + 返回 chevron (A 短按 = 返回轮播)
        _draw_platform_icon(d, plat["category"], DET_ICON_X, DET_ICON_Y)
        _draw_chevron(d, 12, DET_CHEV_Y, -1)

        # 2x2 数据网格
        self._draw_metric(d, "粉丝", fields.get("fans", 0),  DET_LEFT_X,  DET_ROW1_Y)
        self._draw_metric(d, "作品", fields.get("works", 0), DET_RIGHT_X, DET_ROW1_Y)
        self._draw_metric(d, "获赞", fields.get("likes", 0), DET_LEFT_X,  DET_ROW2_Y)
        self._draw_metric(d, "播放", fields.get("views", 0), DET_RIGHT_X, DET_ROW2_Y)

        # 底部操作提示
        gc.collect()

    def _draw_metric(self, d, label, value, x, y):
        """一个数据项: 灰色标签 + 白色数值 (右对齐到格尾)"""
        d.write(hanzi24, label, x, y, CLR_SM_TXT_LBL, CLR_SM_BG)
        val_str = fmt_num(value)
        vw = _hanzi_text_width(val_str)
        vx = max(x + _hanzi_text_width(label), x + DET_CELL_W - vw)
        d.write(hanzi24, val_str, vx, y, CLR_SM_TXT_VAL, CLR_SM_BG)
