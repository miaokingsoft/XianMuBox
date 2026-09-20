"""
dashboard.py - 闲沐项目仪表模式（动态版）
从 settings.json 的 project.own_sites 读取站点, 通过 HTTP API 拉取真实数据
每个站点一张卡片, 卡片内展示该站点 API 返回的所有字段
"""

import time
import gc

from font import hanzi_24 as hanzi24
from .base import BaseMode, SUB_FONT
from framework.colors import (
    CLR_BG, CLR_DASH_BG, CLR_WHITE, CLR_GRAY, CLR_GREEN, CLR_RED, CLR_BLUE, CLR_DIM,
    CLR_CARD,
)

# ===== 屏幕尺寸 =====
try:
    from config import W
except ImportError:
    W = 240


# ===== 数据加载与 API 拉取 =====

def _get_project_cfg():
    """读取 project 配置, 失败返回空 dict"""
    try:
        from framework.settings import get_project
        return get_project() or {}
    except Exception:
        return {}


def _field_label(key):
    """直接返回键名 (API 已返回中文键名), 下划线替换为空格"""
    return str(key).replace("_", " ")


def _hanzi_text_width(text):
    
    w = 0
    for ch in text:
        try:
            idx = hanzi24.MAP.index(ch)
            w += hanzi24._WIDTHS[idx]
        except ValueError:
            w += hanzi24.HEIGHT  # 汉字 fallback 方阵宽
    return w


def _hanzi_center_write(d, text, y, fg, bg=CLR_DASH_BG):
    """用 hanzi24 居中写一行 (与 ai_balance / stock 同模式)"""
    tw = _hanzi_text_width(text)
    sx = max(0, (W - tw) // 2)
    d.write(hanzi24, text, sx, y, fg, bg)


def _sub_center_text(d, text, y, fg, bg=CLR_DASH_BG):
    """用 SUB_FONT 居中写一行小字"""
    if not SUB_FONT:
        return
    tw = len(text) * 8
    d.text(SUB_FONT, text, (W - tw) // 2, y, fg, bg)


def _load_site_cards():
    """从 settings.json 读取 own_sites, 构建卡片列表
    返回: [{"name": str, "api": str, "key": str, "fields": {}, "error": bool}]
    """
    proj = _get_project_cfg()
    sites = proj.get("own_sites") or []
    cards = []
    for s in sites:
        if not isinstance(s, dict):
            continue
        name = s.get("name", "")
        api = s.get("api", "")
        key = s.get("key", "")
        # 过滤掉 stock 站点 (由 StockMode 独立处理)
        if name == "stock":
            continue
        if not api:
            continue
        cards.append({
            "name": name or "Unknown",
            "api": api,
            "key": key,
            "fields": {},   # API 拉取后填充
            "error": False, # 拉取失败标记
        })
    return cards


def _build_api_url(site):
    """根据站点配置构建完整的 API URL (统一拼接 api_key 参数)"""
    api = site["api"]
    key = site.get("key", "")
    if not key:
        return api
    sep = "&" if "?" in api else "?"
    return api + sep + "api_key=" + key


def _http_get_json(url):
    """GET 请求并解析 JSON, 返回 (status, data_dict)
    在 finally 中关闭 response, 防止 MicroPython 连接泄漏
    (与 stock.py 一致的 urequests 写法)
    """
    try:
        import urequests as requests
    except ImportError:
        import requests
    try:
        r = requests.get(url, timeout=10)
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


def _fetch_site_data(site):
    """拉取单个站点数据, 返回 dict 或 None
    空 dict 视为无有效数据, 返回 None 以便标记 error"""
    url = _build_api_url(site)
    status, data = _http_get_json(url)
    if status != 200 or not isinstance(data, dict):
        return None
    if data.get("success") is True or data.get("success") == True:
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner if inner else None
    # 如果 data 本身就是 dict 且不含 data 子键, 也尝试直接使用
    if "data" not in data:
        result = {}
        for k, v in data.items():
            if k not in ("success", "error_code", "message", "__error__"):
                result[k] = v
        if result:
            return result
    return None


# ===== 布局常量 =====
# 严格按 doc/项目仪表模式功能文档.md 效果图布局
_REFRESH_MS = 60000       # 60s 自动刷新
HEADER_H = 40             # 蓝色顶栏高度 (0~40)
_HANZI_H = 25             # hanzi24 实际像素高度 (与 font/hanzi_24.py 一致)
ROW_H_BIG = 30            # 前 5 行 hanzi24 行高
LIST_Y = 50               # 字段列表起始 y (HEADER_H + 5)
DATE_Y = 205              # 日期行 y
DOT_Y = 225               # 分页指示器 y
DOT_SIZE = 6              # 分页方块边长
DOT_SPACING = 4           # 分页方块间距
ICON_PATH = "/icons/dashboard.png"  # 顶栏右侧图标


class DashboardMode(BaseMode):
    MODE_ID = "dashboard"
    MODE_NAME = "项目仪表"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self.cards = []
        self._idx = 0
        self._last_fetch_ms = 0
        # 分步加载状态：-1=未开始, 0~len(cards)-1=正在加载第N张, len(cards)=全部完成
        self._load_step = -1
        # 首次数据拉取后是否已切换到卡片视图
        self._first_card_drawn = False

    def enter(self):
        self._idx = 0
        self._last_fetch_ms = time.ticks_ms()   # 用当前时刻初始化，避免设备运行时间 >60s 时刷新周期立即触发
        self._load_step = -1
        self._first_card_drawn = False

        # 动态加载站点配置
        self.cards = _load_site_cards()
        gc.collect()

        if not self.cards:
            self._draw_empty()
            return

        # 不再调用 _show_loading()：registry.dispatch() 已在模块加载前
        # 统一显示 loading_120.png 过渡画面，避免重复绘制闪烁

    def loop(self):
        """主循环: 按键切换 + 周期刷新 + 分步加载数据
        按文档:
          A 短按 = 下一张 (循环) + 900Hz/15ms
          B 短按 = 立即刷新全部数据 + 800Hz/15ms
          C 短按 = 退出 (flash_exit)
        """
        # --- 分步加载：每轮 loop 至多请求一个站点，避免长时间阻塞 ---
        self._step_load()

        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            # C 短按: 退出模式
            if down:
                self.flash_exit()
                return False

            # 无站点配置时空态: 只等退出
            if not self.cards:
                time.sleep_ms(100)
                return True

            # B 短按: 立即刷新所有站点数据
            if sel:
                self._refresh_all()
                self._draw_card()
                if self.buzzer:
                    self.buzzer.tone(800, 15)
                time.sleep_ms(200)

            # A 短按: 下一张 (循环)
            if up:
                self._idx = (self._idx + 1) % len(self.cards)
                self._draw_card()
                if self.buzzer:
                    self.buzzer.tone(900, 15)
                time.sleep_ms(200)

            # 周期刷新 (60s) + 自动轮播
            now = time.ticks_ms()
            if time.ticks_diff(now, self._last_fetch_ms) >= _REFRESH_MS:
                self._step_refresh_cycle()

            time.sleep_ms(50)
            return True

        except Exception as e:
            print("[DASH] loop error:", repr(e))
            return False

    def exit(self):
        pass

    # ===== 分步加载 =====

    def _step_load(self):
        """分步加载站点数据：每轮主循环至多请求一个站点。
        
        优化效果：用户先看到 loading 画面，首个站点数据返回后
        立即切换到卡片视图，其余站点在后续循环中后台加载。
        """
        if not self.cards:
            return
        # 已全部加载完成
        if self._load_step >= len(self.cards):
            return

        # 首次调用：从第 0 张开始
        if self._load_step < 0:
            self._load_step = 0

        # 加载当前步骤对应的站点
        card = self.cards[self._load_step]
        data = _fetch_site_data(card)
        if data is not None:
            card["fields"] = data
            card["error"] = False
        else:
            card["error"] = True
        gc.collect()

        self._load_step += 1

        # 首个站点数据到达后：立即从 loading 画面切换到卡片视图
        if not self._first_card_drawn and self._load_step >= 1:
            self._first_card_drawn = True
            self._draw_card()
            return

        # 后续站点加载完成：若当前显示的正好是刚加载完的卡片，刷新显示
        if self._first_card_drawn and self._load_step == self._idx + 1:
            self._draw_card()

    def _step_refresh_cycle(self):
        """60 秒周期刷新：逐站点分步刷新（复用 _step_load 状态机）。
        每轮 loop 请求一个站点，不阻塞 UI。
        """
        # 重置分步加载状态，触发新一轮逐站点刷新
        self._load_step = 0
        self._last_fetch_ms = time.ticks_ms()
        # 自动轮播：切换到下一张卡片
        self._idx = (self._idx + 1) % len(self.cards)
        self._draw_card()

    # ===== 数据刷新 =====

    def _refresh_all(self):
        """遍历所有卡片, 拉取最新数据"""
        for card in self.cards:
            data = _fetch_site_data(card)
            if data is not None:
                card["fields"] = data
                card["error"] = False
            else:
                card["error"] = True
            gc.collect()
        self._last_fetch_ms = time.ticks_ms()

    # ===== 绘制辅助 =====

    def _draw_header(self, d, name):
        """蓝色顶栏 (0~40px): CLR_BLUE 背景 + 白色 hanzi24 标题(左) + dashboard.png 图标(右)        
        """
        d.fill_rect(0, 0, W, HEADER_H, CLR_BLUE)
        # 标题: x=10, y=10 (文档精确坐标)
        d.write(hanzi24, name, 10, 10, CLR_WHITE, CLR_BLUE)
        # 右侧 20x20 图标 (x=210, y=10)
        try:
            d.png(ICON_PATH, 200, 8, True)
        except Exception:
            # PNG 缺失或驱动不支持: 降级为白色描边占位框
            d.rect(210, 10, 20, 20, CLR_WHITE)

    def _draw_date_row(self, d, fields):
        """从 fields 提取 date 字段, 在 y=200 居中灰色显示"""
        if not fields:
            return
        date_val = fields.get("date")
        if date_val is None or date_val == "":
            return
        date_str = str(date_val)
        if not SUB_FONT:
            return
        tw = len(date_str) * 8
        sx = max(0, (W - tw) // 2)
        d.text(SUB_FONT, date_str, sx, DATE_Y, CLR_GRAY, CLR_DASH_BG)

    def _draw_pagination(self, d):
        """底部分页方块 (严格按文档 2.3 表格 + 效果图)
        数量 = 已配置站点数, 当前页 CLR_GREEN, 其它 CLR_DIM, 6x6, y=220
        """
        n = len(self.cards)
        if n <= 0:
            return
        total_w = n * DOT_SIZE + (n - 1) * DOT_SPACING
        start_x = (W - total_w) // 2
        for i in range(n):
            dx = start_x + i * (DOT_SIZE + DOT_SPACING)
            color = CLR_GREEN if i == self._idx else CLR_DIM
            d.fill_rect(dx, DOT_Y, DOT_SIZE, DOT_SIZE, color)

    def _draw_field_rows(self, d, fields):
        """渲染字段列表 (最多 5 个非 date 字段)
        - 字段名: 左对齐 x=10, 白色, 带冒号
        - 字段值: 右对齐 x=230, 白色
        - 固定行高 30px, hanzi24 (24x24)
        - 第 6 个起不显示
        - 背景: 深灰 #383838, 无额外卡片容器
        """
        items = list(fields.items())
        NAME_X = 10          # 字段名左对齐
        VALUE_RIGHT = 230    # 字段值右对齐
        MAX_ROWS = 5         # 最多显示 5 个字段
        COLON = ":"          # hanzi24 字库无中文冒号, 保留半角 (按用户决策)

        y = LIST_Y  # 起始 y=45
        row_idx = 0          # 实际渲染行计数 (跳过 date 后重新编号)
        for key, val in items:
            # 跳过 date 字段, 统一由 _draw_date_row 在 y=200 渲染
            if key == "date":
                continue
            # 最多 5 个字段, 多余的不显示
            if row_idx >= MAX_ROWS:
                break

            label = _field_label(str(key)) + COLON
            val_str = str(val) if val is not None else "--"

            d.write(hanzi24, label, NAME_X, y, CLR_WHITE, CLR_DASH_BG)
            vw = _hanzi_text_width(val_str)
            vx = max(NAME_X, VALUE_RIGHT - vw)
            d.write(hanzi24, val_str, vx, y, CLR_WHITE, CLR_DASH_BG)
            y += ROW_H_BIG  # +30
            row_idx += 1

    # ===== 绘制 =====

    def _draw_empty(self):
        """无站点配置时显示空态 (按文档 七、错误与边界处理)
        顶栏仍画 (让空态也有视觉一致), 居中提示 + 底部 Long B back
        """
        d = self.display
        d.fill(CLR_DASH_BG)
        # 顶栏: 用 "项目仪表" 作为静态标题, 与普通卡片顶栏风格统一
        self._draw_header(d, "项目仪表")
        # 居中提示
        _hanzi_center_write(d, "未配置站点", 90, CLR_WHITE)
        _hanzi_center_write(d, "请在设置中添加", 120, CLR_DIM)
        # 底部提示 (SUB_FONT 居中灰色, 放在分页指示器区域 y=222, 符合文档)
        if SUB_FONT:
            hint = "C back"
            tw = len(hint) * 8
            sx = max(0, (W - tw) // 2)
            d.text(SUB_FONT, hint, sx, 222, CLR_GRAY, CLR_DASH_BG)

    def _draw_card(self):
        """绘制当前站点卡片 (严格按文档 2.1/2.2 效果图 1:1 布局)
        y 区域划分:
          0~40    : 蓝色顶栏 (CLR_BLUE) - 标题 + dashboard.png 图标
          40~200  : 字段列表区 (#383838, 无卡片容器)
          200~215 : 日期行 (SUB_FONT, 居中灰色)
          215~230 : 分页指示器 (6x6 方块)
          230~240 : 底部留白 (不放任何内容)
        """
        if not self.cards:
            return
        if self._idx >= len(self.cards):
            self._idx = 0

        d = self.display
        card = self.cards[self._idx]

        # 1. 清屏 (#383838)
        d.fill(CLR_DASH_BG)

        # 2. 蓝色顶栏 (标题=站点名)
        self._draw_header(d, card["name"])

        # 3. 字段列表
        fields = card.get("fields") or {}
        if card.get("error") and not fields:
            # 拉取失败 + 无缓存数据: 红字「数据获取失败」(文档 七、错误处理)
            _hanzi_center_write(d, "数据获取失败", 100, CLR_RED)
        elif not fields:
            # 有配置但暂未拉到数据: 灰字「暂无数据」
            _hanzi_center_write(d, "暂无数据", 100, CLR_GRAY)
        else:
            self._draw_field_rows(d, fields)

        # 4. 日期行 (y=200)
        self._draw_date_row(d, fields)

        # 5. 分页指示器 (y=220, 数量=站点数)
        self._draw_pagination(d)
        # 注: y=230~240 为文档规定的底部留白, 不绘制任何内容
