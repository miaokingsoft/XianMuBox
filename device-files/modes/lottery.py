"""
lottery.py - 闲沐盒子 天天中奖模式

双色球 / 大乐透 机选 + 当期对奖, 子菜单结构 (新增彩种只需扩 _GAMES/_GAME_ORDER):
  - 页0 彩种菜单 (列表类按键): 卡片列表 双色球/大乐透/退出, 每行带持票状态;
    A=上移, B=确认(进入开奖号码页), C=下移; 选中"退出"项退出模式;
    开奖日当天卡片时间行变红提醒; 持票状态显示「已选N注」(绿)
  - 页1 开奖号码页 (标题栏=开奖, 进入彩种默认页, 自动联网拉最新开奖):
    期号+日期一行 / 开奖球上移, 下方显示票池 3 组机选号码 (16px 数字);
    A=兑奖(核对当期票池并弹结果提示), B=进机选画面, C=回彩种菜单;
    状态行 = {机选期号} - {已兑奖/未开奖}; 拉取失败回退显示缓存开奖
  - 页2 机选画面: 进入即本地随机 3 组候选 (16px 数字文本, 前区红/后区蓝,
    出号滚动动画); A=保存(联网定期号, 替换式写入票池, 回开奖号码页),
    B=再选(重新随机), C=丢弃候选回彩种菜单
  - 两彩种票池独立保存, 只保存当期一期; 票池容量 = 一次机选 3 组
  - 联网等待均有进度提示 (云路径静态提示, 回落路径流式进度条)
  - 对奖: 拉最新开奖, 持票期已开奖则逐注判定奖级写入 last_result 并清池
  - 存储: 独立文件 /lottery.json (写临时文件 + rename 防掉电损坏)

数据源 (v1.4 起云平台优先):
  - 云平台: GET /api/v1/data/lottery_ssq|lottery_dlt (framework.cloud
    统一取数, 需在 Web 云服务页配置激活密钥且平台侧已订阅), 仅最新一期
  - 回落: https://data.17500.cn/ssq_asc.txt / dlt_asc.txt 流式抓取
    (云未配置/未绑定/未订阅/限流/网络失败时自动回落), 行:
    期号 日期 红×6 蓝 [统计...]; 全量历史约 100KB+, 设备端分块流式
    读 socket, 只保留尾部若干行。
"""

import time
import gc
import random

from font import hanzi_24 as hanzi24
from .base import BaseMode, get_sub_font
from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_RED, CLR_GREEN, CLR_AMBER,
    CLR_CARD,
    CLR_LOT_BLUE, CLR_LOT_GOLD,
)

try:
    from config import W, H
except ImportError:
    W = 240
    H = 240

_SUB = get_sub_font()  # vga1_8x8 (ASCII 列表行), 可能为 None
_HANZI_H = hanzi24.HEIGHT  # 25

# ===== 彩种定义 =====

_GAMES = {
    "ssq": {
        "name": "双色球", "file": "ssq_asc.txt", "source": "lottery_ssq",
        "n_front": 6, "front_max": 33, "n_back": 1, "back_max": 16,
        "days": (1, 3, 6),   # 开奖日 周二/周四/周日 (周一=0)
        "time": "21:15",
    },
    "dlt": {
        "name": "大乐透", "file": "dlt_asc.txt", "source": "lottery_dlt",
        "n_front": 5, "front_max": 35, "n_back": 2, "back_max": 12,
        "days": (0, 2, 5),   # 开奖日 周一/周三/周六
        "time": "21:25",
    },
}
_GAME_ORDER = ("ssq", "dlt")

MAX_TICKETS = 3       # 当期票池容量 (= 一次机选 3 组, 新号码替换老号码)
_PROG_TICK_MS = 100   # 进度条刷新节流

# ===== 16px 数字字体 (机选候选号码, vga1_16x16, 失败回退 8x8) =====

_NUM16 = None
_NUM16_LOADED = False


def _num16_font():
    global _NUM16, _NUM16_LOADED
    if not _NUM16_LOADED:
        _NUM16_LOADED = True
        try:
            from font import vga1_16x16 as _f
            _NUM16 = _f
        except ImportError:
            _NUM16 = None
    return _NUM16

# ===== 持久化: /lottery.json =====
# 结构: {"game": "ssq", "states": {"ssq": {...}, "dlt": {...}}}
# 每个彩种独立保存 period/expect_date/tickets/last_result

_LOTTERY_FILE = "/lottery.json"
_LOTTERY_TMP = "/lottery.json.tmp"


def _default_state():
    return {
        "period": "",        # 当期期号 (持票期)
        "expect_date": "",   # 当期预期开奖日 YYYY-MM-DD
        "tickets": [],       # 当期票池 [{"reds":[...], "blues":[...], "ts": "..."}]
        "last_result": None, # 上次对奖结果 (见 _reconcile)
    }


def _default_data():
    return {
        "game": "ssq",
        "states": {"ssq": _default_state(), "dlt": _default_state()},
    }


def load_lottery_data():
    """读取 /lottery.json, 损坏/缺失回退默认; 兼容旧版单彩种平铺结构"""
    data = _default_data()
    try:
        import json
        f = open(_LOTTERY_FILE, "r")
        try:
            raw = json.load(f)
        finally:
            f.close()
        if isinstance(raw, dict):
            if raw.get("game") in _GAMES:
                data["game"] = raw["game"]
            states = raw.get("states")
            if isinstance(states, dict):
                for g in _GAMES:
                    if isinstance(states.get(g), dict):
                        for k in data["states"][g]:
                            if k in states[g]:
                                data["states"][g][k] = states[g][k]
            elif "tickets" in raw or "period" in raw:
                # 旧版平铺结构 -> 迁移到对应彩种
                st = data["states"][data["game"]]
                for k in st:
                    if k in raw:
                        st[k] = raw[k]
    except Exception as e:
        print("[LOTTERY] load:", e)
    for g in _GAMES:
        st = data["states"][g]
        if not isinstance(st["tickets"], list):
            st["tickets"] = []
    return data


def save_lottery_data(data):
    """写 /lottery.json.tmp 后 rename, 防掉电损坏"""
    import os
    try:
        import json
        f = open(_LOTTERY_TMP, "w")
        try:
            f.write(json.dumps(data))
        finally:
            f.close()
        try:
            os.remove(_LOTTERY_FILE)
        except OSError:
            pass
        os.rename(_LOTTERY_TMP, _LOTTERY_FILE)
    except Exception as e:
        print("[LOTTERY] save:", e)


# ===== 纯函数: 奖级判定 (可 PC 端单测) =====

def judge_prize(game, front_hits, back_hits):
    """按命中数判定奖级, 返回 (等级序号 0~9, 奖级名, 金额)
    金额 -1 = 浮动奖金, 0 = 未中奖
    """
    if game == "ssq":
        red, blue = front_hits, 1 if back_hits > 0 else 0
        if red == 6 and blue == 1:
            return 1, "一等奖", -1
        if red == 6:
            return 2, "二等奖", -1
        if red == 5 and blue == 1:
            return 3, "三等奖", 3000
        if red + blue == 5:
            return 4, "四等奖", 200
        if red + blue == 4:
            return 5, "五等奖", 10
        if blue == 1:
            return 6, "六等奖", 5
        return 0, "", 0
    else:  # dlt
        f, b = front_hits, back_hits
        if f == 5 and b == 2:
            return 1, "一等奖", -1
        if f == 5 and b == 1:
            return 2, "二等奖", -1
        if f == 5 and b == 0:
            return 3, "三等奖", 10000
        if f == 4 and b == 2:
            return 4, "四等奖", 3000
        if f == 4 and b == 1:
            return 5, "五等奖", 300
        if f == 3 and b == 2:
            return 6, "六等奖", 200
        if f == 4 and b == 0:
            return 7, "七等奖", 100
        if (f == 3 and b == 1) or (f == 2 and b == 2):
            return 8, "八等奖", 15
        if (f == 3 and b == 0) or (f == 2 and b == 1) or (b == 2 and f <= 1):
            return 9, "九等奖", 5
        return 0, "", 0


# ===== 纯函数: 期号推进 =====

def next_period_info(latest_qh, latest_date, game):
    """由最新开奖推下一期: 返回 (下一期期号, 预期开奖日 YYYY-MM-DD)
    处理年末进位: 双色球 2026155 -> 2027001, 大乐透 26155 -> 27001
    """
    g = _GAMES[game]
    ymd = _parse_date(latest_date)
    if ymd is None:
        # 无日期兜底: 仅序号 +1
        if len(latest_qh) >= 7:
            return "%04d%03d" % (int(latest_qh[:4]), int(latest_qh[4:]) + 1), ""
        return "%02d%03d" % (int(latest_qh[:2]), int(latest_qh[2:]) + 1), ""
    sec = _mktime_noon(ymd[0], ymd[1], ymd[2])
    tm = None
    for k in range(1, 8):
        tm = time.localtime(sec + k * 86400)
        if tm[6] in g["days"]:
            break
    ny = tm[0]
    if len(latest_qh) >= 7:
        year = int(latest_qh[:4])
        if ny != year:
            nq = "%04d001" % ny
        else:
            nq = "%04d%03d" % (year, int(latest_qh[4:]) + 1)
    else:
        year = int(latest_qh[:2])
        if ny % 100 != year:
            nq = "%02d001" % (ny % 100)
        else:
            nq = "%02d%03d" % (year, int(latest_qh[2:]) + 1)
    return nq, "%04d-%02d-%02d" % (ny, tm[1], tm[2])


def _parse_date(s):
    try:
        p = s.split("-")
        return (int(p[0]), int(p[1]), int(p[2]))
    except Exception:
        return None


def _mktime_noon(y, m, d):
    """当天正午的秒数 (MicroPython 8 元组 / CPython 9 元组双兼容)"""
    t = (y, m, d, 12, 0, 0, 0, 0)
    try:
        return time.mktime(t)
    except (TypeError, ValueError):
        return time.mktime(t + (0,))


# ===== 随机号码 =====

def _seed_rng():
    """硬件随机源播种, 失败回退时钟"""
    seed = 0
    try:
        import machine
        seed = machine.rng() & 0x7FFFFFFF
    except Exception:
        seed = 0
    try:
        seed ^= time.ticks_us() & 0x7FFFFFFF
    except Exception:
        pass
    if seed:
        random.seed(seed)


def random_pick(game):
    """机选一注: 返回 (前区列表, 后区列表), 均为补零字符串且升序"""
    g = _GAMES[game]
    _seed_rng()
    front = _sample_unique(g["front_max"], g["n_front"])
    back = _sample_unique(g["back_max"], g["n_back"])
    return front, back


def _sample_unique(maxv, n):
    """MicroPython random 无 sample, 用池抽取保证不重复"""
    pool = list(range(1, maxv + 1))
    out = []
    for _ in range(n):
        out.append(pool.pop(random.randint(0, len(pool) - 1)))
    out.sort()
    return ["%02d" % i for i in out]  # zfill 为 CPython 方法, MicroPython 无


# ===== 网络数据: 流式读开奖文件尾部 =====

def fetch_recent_draws(game, tail_lines=12, timeout=15, on_prog=None):
    """流式读取开奖历史尾部, 返回 [{qh,date,front,back}] (旧->新), 失败 None
    全量文件约 100KB+, 分块读 socket 只保留尾部, 内存 O(块大小)
    on_prog(received_bytes, total_bytes) 每块回调 (内部自行节流)
    """
    g = _GAMES[game]
    base = "data.17500.cn/" + g["file"]
    for scheme in ("https://", "http://"):
        draws = _try_fetch(scheme + base, g, tail_lines, timeout, on_prog)
        if draws:
            return draws
    return None


def _content_length(r):
    try:
        h = r.headers.get("Content-Length") or r.headers.get("content-length")
        return int(h) if h else 0
    except Exception:
        return 0


def _try_fetch(url, g, tail_lines, timeout, on_prog):
    try:
        import urequests as requests
    except ImportError:
        import requests
    nf, nb = g["n_front"], g["n_back"]
    try:
        r = requests.get(url, timeout=timeout)
    except Exception as e:
        print("[LOTTERY] get failed:", e)
        return None
    draws = []
    try:
        if r.status_code != 200:
            print("[LOTTERY] status:", r.status_code)
            return None
        total_size = _content_length(r)
        raw = r.raw
        buf = b""
        total = 0
        while total < 600000:
            chunk = raw.read(512)
            if not chunk:
                break
            total += len(chunk)
            buf += chunk
            if on_prog:
                try:
                    on_prog(total, total_size)
                except Exception:
                    pass
            if b"\n" in buf:
                parts = buf.split(b"\n")
                buf = parts[-1]  # 未完成的尾行
                for ln in parts[:-1]:
                    dr = _parse_draw_line(ln, nf, nb)
                    if dr:
                        draws.append(dr)
                        while len(draws) > tail_lines:
                            draws.pop(0)
        if buf.strip():
            dr = _parse_draw_line(buf, nf, nb)
            if dr:
                draws.append(dr)
    except Exception as e:
        print("[LOTTERY] stream error:", e)
        return draws or None
    finally:
        try:
            r.close()
        except Exception:
            pass
    gc.collect()
    return draws or None


def _parse_draw_line(ln, nf, nb):
    """行: 期号 日期 前×N 后×M [统计...] -> dict, 无效返回 None"""
    try:
        fields = bytes(ln).decode().split()
        if len(fields) < 2 + nf + nb:
            return None
        return {
            "qh": fields[0],
            "date": fields[1],
            "front": fields[2:2 + nf],
            "back": fields[2 + nf:2 + nf + nb],
        }
    except Exception:
        return None


# ===== 云平台取数 (framework.cloud 统一入口, v1.4) =====

def _norm_qh(game, qh):
    """期号规范化为该彩种本地约定长度 (双色球 7 位 / 大乐透 5 位)。

    平台期号可能带 4 位年份前缀 (如大乐透 2026102), 裁剪为 26102,
    保证与本地票据期号的字符串比较 (对奖判定) 口径一致。
    非法输入返回 ""。
    """
    want = 7 if game == "ssq" else 5
    qh = str(qh or "").strip()
    if len(qh) > want:
        qh = qh[-want:]
    return qh if (qh and qh.isdigit()) else ""


def _adapt_cloud_draw(game, payload):
    """平台 payload -> 模式统一开奖结构, 结构不符返回 None (走回落)。

    双色球 payload 为 red[6]+blue(int), 大乐透为 front[5]+back[2];
    号码统一转补零字符串, 另带 sale_period (在售期号) 与
    source_name (来源免责文案, 随 last_result 落盘留痕)。
    """
    g = _GAMES[game]
    if not isinstance(payload, dict):
        return None
    if game == "ssq":
        front = payload.get("red")
        back = payload.get("blue")
        if isinstance(back, int):
            back = [back]
    else:
        front = payload.get("front")
        back = payload.get("back")

    def _z(v):
        try:
            return "%02d" % int(v)
        except (ValueError, TypeError):
            return None

    fronts = [_z(v) for v in front] if isinstance(front, list) else None
    backs = [_z(v) for v in back] if isinstance(back, list) else None
    qh = _norm_qh(game, payload.get("period"))
    date = str(payload.get("date", "") or "").strip()
    if (not qh or fronts is None or backs is None
            or None in fronts or None in backs
            or len(fronts) != g["n_front"] or len(backs) != g["n_back"]):
        return None
    return {
        "qh": qh, "date": date, "front": fronts, "back": backs,
        "sale_period": _norm_qh(game, payload.get("sale_period")),
        "source_name": str(payload.get("source_name", "") or ""),
    }


# ===== 文本渲染 (缺字跳过) =====

def _char_w(ch):
    try:
        return hanzi24._WIDTHS[hanzi24.MAP.index(ch)]
    except ValueError:
        return _HANZI_H  # 缺字占位方阵宽


def text_w(text):
    w = 0
    for ch in text:
        w += _char_w(ch)
    return w


def draw_text(d, text, x, y, fg, bg):
    """逐字渲染, 缺字按占位宽跳过"""
    for ch in text:
        if ch in hanzi24.MAP:
            d.write(hanzi24, ch, x, y, fg, bg)
        x += _char_w(ch)


def draw_center(d, text, y, fg, bg, x0=0, x1=W):
    draw_text(d, text, x0 + (x1 - x0 - text_w(text)) // 2, y, fg, bg)


def draw_ascii(d, s, x, y, fg, bg):
    if _SUB:
        d.text(_SUB, s, x, y, fg, bg)


def ascii_w(s):
    return len(s) * 8


def draw_ascii_center(d, s, y, fg, bg):
    if _SUB:
        draw_ascii(d, s, (W - ascii_w(s)) // 2, y, fg, bg)


# ===== 球号绘制 =====

def _fill_circle_safe(d, cx, cy, r, color):
    """fill_circle 兼容调用, 不可用时八边形拟合"""
    if hasattr(d, "fill_circle"):
        try:
            d.fill_circle(cx, cy, r, color)
            return
        except TypeError:
            try:
                d.fill_circle(cx, cy, r)
                return
            except Exception:
                pass
        except Exception:
            pass
    d.fill_rect(cx - r + 2, cy - r, 2 * r - 4, 2 * r, color)
    d.fill_rect(cx - r, cy - r + 2, 2 * r, 2 * r - 4, color)
    d.fill_rect(cx - r + 1, cy - r + 1, 2 * r - 2, 2 * r - 2, color)


def draw_ball(d, x, y, r, color, num, empty=False):
    """画一颗球 + 居中号码; empty=True 画灰色占位球
    大球用 hanzi24 号码, 小球 (r<=12) 用 8x8 ASCII
    """
    c = CLR_DIM if empty else color
    _fill_circle_safe(d, x + r, y + r, r, c)
    if not num:
        return
    if r <= 12:
        if _SUB:
            tw = len(num) * 8
            d.text(_SUB, num, x + r - tw // 2, y + r - 4, CLR_WHITE, c)
        return
    tw = 0
    for ch in num:
        tw += _char_w(ch)
    tx = x + r - tw // 2
    ty = y + r - _HANZI_H // 2
    for ch in num:
        if ch in hanzi24.MAP:
            d.write(hanzi24, ch, tx, ty, CLR_WHITE, c)
        tx += _char_w(ch)


# ===== 布局常量 =====

VIEW_MENU, VIEW_PICK, VIEW_DRAW = 0, 1, 2
HDR_H = 40
BTN_Y0 = 199
BTN_W = 73
BTN_H = 36
MENU_X = 8      # 彩种菜单卡片
MENU_W = W - 16
MENU_Y0 = 48
MENU_H = 52
MENU_STEP = 60  # 卡高 + 间距
MENU_VIS = 3    # 同屏可见行数 (含退出项), 超出滚动
PICK_Y0 = 108   # 机选候选号码区首行 y
PICK_STEP = 28  # 候选行距 (16px 数字 + 行间分隔)


class LotteryMode(BaseMode):
    MODE_ID = "lottery"
    MODE_NAME = "天天中奖"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self.data = _default_data()
        self._view = VIEW_MENU         # 当前视图 (菜单/机选/开奖号码)
        self._sel = 0                  # 菜单选中项 (含末尾退出项)
        self._mscroll = 0              # 菜单滚动窗口起点
        self._latest = None            # 最近开奖列表 (旧->新)
        self._candidates = None        # 机选画面 3 组候选 [(front, back) x3]
        self._last_check_ms = 0
        self._fetch_error = False

    # ---------- 生命周期 ----------

    def enter(self):
        """进入模式: 只读 /lottery.json 渲染彩种菜单, 不发起任何网络请求"""
        gc.collect()
        self.data = load_lottery_data()
        self._view = VIEW_MENU
        try:
            self._sel = _GAME_ORDER.index(self.data["game"])
        except ValueError:
            self._sel = 0
        self._mscroll = 0
        self._latest = None
        self._candidates = None
        self._last_check_ms = 0
        self._fetch_error = False
        gc.collect()
        self._draw_page()

    def loop(self):
        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            if self._view == VIEW_MENU:
                # 列表类按键: A=上移 B=确认 C=下移, 退出为列表项
                if up or down:
                    self._menu_move(-1 if up else 1)
                elif sel:
                    if not self._menu_ok():
                        return False
            elif self._view == VIEW_PICK:
                # 机选画面: A=保存 B=再选 C=丢弃候选回彩种菜单
                if up:
                    self._pick_save()
                elif sel:
                    self._pick_repick()
                elif down:
                    self._leave_page()
            else:
                # 开奖号码页: A=兑奖 B=进机选画面 C=回彩种菜单
                if up:
                    self._do_check()
                elif sel:
                    self._enter_pick()
                elif down:
                    self._leave_page()

            time.sleep_ms(30)
            return True
        except Exception as e:
            print("[LOTTERY] loop error:", repr(e))
            time.sleep_ms(200)
            return True

    def exit(self):
        self.data = None
        self._latest = None
        self._candidates = None
        gc.collect()

    # ---------- 公共小工具 ----------

    def _leave_page(self):
        """开奖号码页/机选画面 C 短按: 回彩种菜单 (机选候选丢弃)"""
        self.beep(900, 15)
        self._candidates = None
        self._view = VIEW_MENU
        self._draw_menu()

    # ---------- 数据动作 ----------

    def _st(self):
        """当前彩种的独立状态"""
        return self.data["states"][self.data["game"]]

    def _game(self):
        return _GAMES[self.data["game"]]

    def _menu_move(self, delta):
        """菜单 A/C: 选中项移动 (循环), 末项为退出"""
        n = len(_GAME_ORDER) + 1
        self._sel = (self._sel + delta) % n
        if self._sel < self._mscroll:
            self._mscroll = self._sel
        elif self._sel >= self._mscroll + MENU_VIS:
            self._mscroll = self._sel - MENU_VIS + 1
        self.beep(800, 15)
        self._draw_menu()

    def _menu_ok(self):
        """菜单 B: 确认 -> 进入该彩种开奖号码页 (自动联网拉最新开奖);
        退出项 -> 退出模式"""
        if self._sel >= len(_GAME_ORDER):
            self.flash_exit()
            return False
        gk = _GAME_ORDER[self._sel]
        if self.data["game"] != gk:
            self.data["game"] = gk
            save_lottery_data(self.data)
        self.beep(1200, 30)
        self._view = VIEW_DRAW
        self._draw_page()
        self._auto_fetch()
        return True

    def _auto_fetch(self):
        """开奖号码页进入时自动拉取最新开奖 (云优先, 带进度提示)。

        仅刷新显示, 不自动对奖 —— 核对动作留给「兑奖」按钮;
        持票期已开奖时状态行显示「待兑奖」引导用户按 A。
        """
        self._fetch_error = False
        draws = self._fetch_draws("联网中...")
        if draws:
            self._latest = draws
            self._last_check_ms = time.ticks_ms()
        else:
            self._fetch_error = True
        self._draw_page()

    def _reconcile(self):
        """持票期 <= 最新期 => 已开奖: 逐注判定, 写 last_result, 清空票池"""
        st = self._st()
        period = st.get("period", "")
        if not period or not self._latest:
            return
        latest = self._latest[-1]
        if period > latest["qh"]:
            return  # 未开奖
        # 已开奖: 定位持票期那一条 (期号优先, 日期兜底)
        target = None
        for dr in self._latest:
            if dr["qh"] == period:
                target = dr
                break
        if target is None:
            exp = st.get("expect_date", "")
            if exp:
                for dr in self._latest:
                    if dr["date"] >= exp:
                        target = dr
                        break
        if target is None:
            target = latest  # 极端兜底: 按最新一期对奖
        won_count = 0
        amount = 0
        details = []
        for t in st["tickets"]:
            fh = 0
            for n in t.get("reds", []):
                if n in target["front"]:
                    fh += 1
            bh = 0
            for n in t.get("blues", []):
                if n in target["back"]:
                    bh += 1
            rank, name, amt = judge_prize(self.data["game"], fh, bh)
            details.append({"reds": t.get("reds", []), "blues": t.get("blues", []),
                            "hits": [fh, bh], "rank": rank})
            if amt > 0:
                won_count += 1
                amount += amt
            elif amt < 0:
                won_count += 1
                amount = -1  # 含浮动奖
        st["last_result"] = {
            "game": self.data["game"],
            "period": target["qh"],
            "date": target["date"],
            "draw_front": target["front"],
            "draw_back": target["back"],
            "n_tickets": len(st["tickets"]),
            "won_count": won_count,
            "amount": amount,
            "details": details,
            # 云平台来源免责文案留痕 (17500 回落路径为空串)
            "source": target.get("source_name", ""),
        }
        # 期已过, 清空票池
        st["period"] = ""
        st["expect_date"] = ""
        st["tickets"] = []
        save_lottery_data(self.data)
        # 中奖播报
        if won_count > 0:
            if self.buzzer:
                try:
                    self.buzzer.melody([(880, 80, 30), (1100, 80, 30), (1320, 150, 0)])
                except Exception:
                    self.beep(1320, 150)
        else:
            self.beep(600, 60)

    def _enter_pick(self):
        """开奖号码页 B=机选: 本地随机 3 组候选, 进机选画面 (不联网)"""
        self._candidates = [random_pick(self.data["game"]) for _ in range(3)]
        self.beep(1200, 30)
        self._view = VIEW_PICK
        self._draw_pick()
        self._roll_anim3()

    def _pick_repick(self):
        """机选画面 B=再选: 重新随机 3 组候选 (本地, 零延迟)"""
        self._candidates = [random_pick(self.data["game"]) for _ in range(3)]
        self.beep(800, 15)
        self._roll_anim3()

    def _pick_save(self):
        """机选画面 A=保存: 联网确定当期期号后, 3 组候选写入票池。

        持票期已开奖时先对奖清池再入新期; 保存成功回开奖号码页。
        需要网络 (云优先, 回落 17500), 失败留在机选画面可重试。
        """
        if not self._candidates:
            return
        if not self._wifi_ok():
            self._show_msg("请联网后保存", 1500)
            self._draw_pick()
            return
        data = self.data
        st = self._st()
        self._fetch_error = False
        draws = self._fetch_draws("联网中...")
        if not draws:
            self._fetch_error = True
            self._show_msg("网络读取失败", 1500)
            self._draw_pick()
            return
        self._latest = draws
        self._last_check_ms = time.ticks_ms()
        latest = draws[-1]
        # 旧持票期已开奖 -> 先对奖再清池 (换期必要步骤)
        if st.get("period", "") and st["period"] <= latest["qh"]:
            self._reconcile()
        nq, ndate = next_period_info(latest["qh"], latest["date"], data["game"])
        sp = latest.get("sale_period", "")
        if sp:
            nq = sp  # 平台在售期号优先 (官方权威)
        if st.get("period", "") != nq:
            st["period"] = nq
            st["expect_date"] = ndate
        # 替换式保存: 当期票池 = 本次 3 组 (新号码替换老号码)
        try:
            ts = time.localtime()
            ts_str = "%04d-%02d-%02d" % (ts[0], ts[1], ts[2])
        except Exception:
            ts_str = ""
        st["tickets"] = []
        for front, back in self._candidates:
            st["tickets"].append({"reds": front, "blues": back, "ts": ts_str})
        save_lottery_data(data)
        self._candidates = None
        self._show_msg("已选" + str(len(st["tickets"])) + "注", 1200)
        self.beep(1200, 40)
        self._view = VIEW_DRAW
        self._draw_page()

    def _do_check(self):
        """开奖号码页 A=兑奖: 拉最新开奖核对当期票池, 弹结果提示"""
        st = self._st()
        if not st.get("tickets", []):
            self.beep(400, 80)  # 无票低音提示, 不发起请求
            self._show_msg("当期无已选", 1000)
            self._draw_page()
            return
        if not self._latest:  # 进页拉取失败过 -> 兑奖时重试
            self._fetch_error = False
            draws = self._fetch_draws("对奖中...")
            if not draws:
                self._fetch_error = True
                self._draw_page()
                return
            self._latest = draws
            self._last_check_ms = time.ticks_ms()
        latest = self._latest[-1]
        if st.get("period", "") and st["period"] > latest["qh"]:
            self._show_msg("等待开奖", 1200)  # 持票期尚未开奖
            self._draw_page()
            return
        before = st.get("last_result")
        self._reconcile()
        lr = st.get("last_result")
        if lr is not None and lr is not before:
            amt = lr.get("amount", 0)
            if amt > 0:
                msg = "中" + str(lr.get("won_count", 0)) + "注 " + str(amt) + "元"
            elif amt < 0:
                msg = "命中浮动奖"
            else:
                msg = "未中奖"
            self._show_msg(msg, 1800)
        self._draw_page()

    def _wifi_ok(self):
        try:
            from framework.wifi import check_wifi
            ok, _ip = check_wifi()
            return ok
        except Exception:
            return False

    def _draw_busy(self, label):
        """阻塞请求前的静态提示 (云路径无流式进度可回调)"""
        d = self.display
        d.fill_rect(0, 100, W, 40, CLR_BG)
        draw_center(d, label, 102, CLR_CYAN, CLR_BG)
        d.rect(20, 126, W - 40, 10, CLR_DIM)
        d.fill_rect(22, 128, 24, 6, CLR_CYAN)

    def _fetch_draws(self, label):
        """取开奖数据: 云平台优先 (仅最新一期), 失败自动回落 17500 流式。

        返回 [{qh,date,front,back,...}] (旧->新), 双路径均失败 None。
        云路径显示"平台取数中"静态提示; 回落路径沿用流式进度条 (label)。
        云未配置密钥 (cloud.ready() 为假) 时直接走回落, 无额外提示。
        """
        try:
            from framework import cloud
        except Exception:
            cloud = None
        if cloud is not None and cloud.ready():
            self._draw_busy("平台取数中")
            code, cdata = cloud.fetch_json(self._game()["source"])
            if code == 0:
                dr = _adapt_cloud_draw(self.data["game"],
                                       cdata.get("payload"))
                if dr:
                    gc.collect()
                    return [dr]
            # 云不可用 (未绑定/未订阅/限流/网络失败) -> 静默回落
            gc.collect()
        return fetch_recent_draws(self.data["game"],
                                  on_prog=self._make_progress(label))

    # ---------- 进度条 / 瞬态提示 ----------

    def _make_progress(self, label):
        """生成流式读取的进度回调: 标签 + 进度条, 每 _PROG_TICK_MS 刷新一次
        有 Content-Length 按百分比; 无长度时按 2KB 一格伪进度
        """
        d = self.display
        last = {"ms": 0}

        def cb(received, total):
            now = time.ticks_ms()
            if last["ms"] and time.ticks_diff(now, last["ms"]) < _PROG_TICK_MS:
                return
            last["ms"] = now
            if total:
                pct = received * 100 // total
                if pct > 100:
                    pct = 100
            else:
                pct = (received // 2048) % 100
            d.fill_rect(0, 100, W, 40, CLR_BG)
            draw_center(d, label, 102, CLR_CYAN, CLR_BG)
            d.rect(20, 126, W - 40, 10, CLR_DIM)
            fill_w = (W - 44) * pct // 100
            if fill_w > 0:
                d.fill_rect(22, 128, fill_w, 6, CLR_CYAN)
            draw_ascii(d, "%3d%%" % pct, W - 52, 126, CLR_CYAN, CLR_BG)
        return cb

    def _show_msg(self, text, ms):
        """页内瞬态提示 (阻塞 ms 后由调用方重绘)"""
        d = self.display
        d.fill_rect(0, 96, W, 40, CLR_BG)
        draw_center(d, text, 106, CLR_LOT_GOLD, CLR_BG)
        time.sleep_ms(ms)

    # ---------- UI ----------

    def _btn_labels(self):
        if self._view == VIEW_PICK:
            return ["保存", "再选", "退出"]
        return ["兑奖", "机选", "退出"]

    def _draw_page(self):
        if self._view == VIEW_MENU:
            self._draw_menu()
        elif self._view == VIEW_PICK:
            self._draw_pick()
        else:
            self._draw_draw()

    def _draw_header(self, d, right=None, left="天天中奖"):
        """红色顶栏: 左侧白字 (默认天天中奖, 开奖号码页=开奖) + 右侧金色
        文字 (菜单页=选彩种, 其余=彩种名)"""
        d.fill_rect(0, 0, W, HDR_H, CLR_RED)
        draw_text(d, left, 10, 7, CLR_WHITE, CLR_RED)
        if right is None:
            right = self._game()["name"]
        draw_text(d, right, W - 10 - text_w(right), 7, CLR_LOT_GOLD, CLR_RED)

    def _draw_buttons(self, d, pressed_btn=None):
        """底部三按钮 (风格对齐 stock.py), 仅机选/查询页
        左钮标签 = A 翻页去向, 中钮 = B 主操作"""
        labels = self._btn_labels()
        d.fill_rect(0, BTN_Y0, W, BTN_H, CLR_BG)
        for i, label in enumerate(labels):
            bx = 5 + i * (BTN_W + 5)
            bw = BTN_W + 1 if i == 2 else BTN_W
            bg = CLR_LOT_GOLD if pressed_btn == i else CLR_WHITE
            fg = CLR_BG
            d.fill_rect(bx, BTN_Y0, bw, BTN_H, bg)
            d.rect(bx, BTN_Y0, bw, BTN_H, CLR_DIM)
            tw = text_w(label)
            tx = bx + (bw - tw) // 2
            ty = BTN_Y0 + (BTN_H - _HANZI_H) // 2
            d.fill_rect(tx, ty, tw, _HANZI_H, bg)
            draw_text(d, label, tx, ty, fg, bg)

    # ----- 页0 彩种菜单 (列表类: A上移 B确认 C下移) -----

    def _draw_menu(self):
        d = self.display
        d.fill(CLR_BG)
        self._draw_header(d, "选彩种")
        n = len(_GAME_ORDER) + 1  # 含末尾退出项
        for vi in range(MENU_VIS):
            i = self._mscroll + vi
            if i >= n:
                break
            self._draw_menu_row(d, i, MENU_Y0 + vi * MENU_STEP)

    def _draw_menu_row(self, d, i, y):
        """菜单行: 彩种名 + 持票状态 + 开奖时间; 末项为退出 (iOS 卡片选中态)
        持票状态「已选N注」绿色; 开奖日当天时间行红色提醒"""
        on = i == self._sel
        bg = CLR_CARD if on else CLR_BG
        fg = CLR_WHITE if on else CLR_GRAY
        if on:
            d.fill_rect(MENU_X, y, MENU_W, MENU_H, bg)
            d.rect(MENU_X, y, MENU_W, MENU_H, CLR_WHITE)
        if i >= len(_GAME_ORDER):
            draw_text(d, "退出", 18, y + (MENU_H - _HANZI_H) // 2, fg, bg)
            return
        gk = _GAME_ORDER[i]
        g = _GAMES[gk]
        st = self.data["states"][gk]
        tickets = st.get("tickets", [])
        draw_text(d, g["name"], 18, y + 5, fg, bg)
        # 行1右侧: 持票状态
        if tickets:
            stat = "已选" + str(len(tickets)) + "注"
            stat_c = CLR_GREEN
        else:
            stat = "无机选"
            stat_c = CLR_GRAY
        draw_text(d, stat, MENU_X + MENU_W - 8 - text_w(stat), y + 5,
                  stat_c, bg)
        # 行2: 开奖日期时间 (持票期=预期开奖日, 否则仅开奖时刻)
        # 开奖日当天红色提醒, 非开奖日 持票青 / 无票灰
        try:
            wd = time.localtime()[6]
        except Exception:
            wd = -1
        sub = (st.get("expect_date", "") + " " + g["time"]
               if st.get("period", "") else g["time"])
        if wd in g["days"]:
            sub_c = CLR_RED
        elif tickets:
            sub_c = CLR_CYAN
        else:
            sub_c = CLR_GRAY
        draw_ascii(d, sub, 18, y + 36, sub_c, bg)

    # ----- 页2 机选画面 (3 组候选, 保存/再选/退出) -----

    def _draw_nums16_row(self, d, front, back, y):
        """一行号码: 16px 数字文本, 前区红 / 后区蓝, 号码间 2px 间隔"""
        f16 = _num16_font()
        cw = 16 if f16 else 8
        nums = list(front) + list(back)
        nf = len(front)
        gap = 2
        total = len(nums) * 2 * cw + (len(nums) - 1) * gap
        x = (W - total) // 2
        for i, num in enumerate(nums):
            c = CLR_RED if i < nf else CLR_LOT_BLUE
            if f16:
                d.text(f16, num, x, y, c, CLR_BG)
            else:
                draw_ascii(d, num, x, y, c, CLR_BG)
            x += 2 * cw + gap

    def _draw_pick(self):
        d = self.display
        st = self._st()
        d.fill(CLR_BG)
        self._draw_header(d)

        period = st.get("period", "")
        tickets = st.get("tickets", [])

        # 期号行 (无持票期显示占位) + 候选提示行
        if period:
            draw_center(d, "第" + period + "期", 46, CLR_WHITE, CLR_BG)
        else:
            draw_center(d, "选3注", 46, CLR_WHITE, CLR_BG)
        tip = "新3注"
        if tickets:
            tip = "已选" + str(len(tickets)) + "注 " + tip
        draw_center(d, tip, 76, CLR_CYAN, CLR_BG)

        # 3 组候选: 16px 数字, 行间细分隔线
        if self._candidates:
            y = PICK_Y0
            for i, (front, back) in enumerate(self._candidates):
                if i:
                    d.hline(20, y - 8, W - 40, CLR_DIM)
                self._draw_nums16_row(d, front, back, y)
                y += PICK_STEP

        self._draw_buttons(d)

    def _roll_anim3(self):
        """机选出号动画: 3 行候选滚动随机号码后定格 (页框架保持可见)"""
        d = self.display
        g = self._game()
        cand = self._candidates
        for step in range(6):
            last = step == 5
            d.fill_rect(0, PICK_Y0 - 8, W, BTN_Y0 - PICK_Y0 + 4, CLR_BG)
            y = PICK_Y0
            for i in range(3):
                if i:
                    d.hline(20, y - 8, W - 40, CLR_DIM)
                if last:
                    f, b = cand[i]
                else:
                    f = _sample_unique(g["front_max"], g["n_front"])
                    b = _sample_unique(g["back_max"], g["n_back"])
                self._draw_nums16_row(d, f, b, y)
                y += PICK_STEP
            self.beep(600 + step * 80, 20)
            time.sleep_ms(80)

    # ----- 页1 开奖号码页 (进入彩种默认页, A=兑奖) -----

    def _draw_draw(self):
        d = self.display
        data = self.data
        st = self._st()
        d.fill(CLR_BG)
        self._draw_header(d, left="开奖")

        if self._fetch_error and not self._latest:
            # 拉取失败: 回退显示上次对奖缓存的开奖号码, 无缓存才显示错误
            lr = st.get("last_result")
            if (lr is not None and lr.get("game") == data["game"]
                    and lr.get("draw_front")):
                self._draw_cached_draw(d, lr)
            else:
                draw_center(d, "网络读取失败", 60, CLR_RED, CLR_BG)
                draw_center(d, "请联网后刷新", 96, CLR_GRAY, CLR_BG)
            self._draw_buttons(d)
            return

        latest = self._latest[-1] if self._latest else None
        tickets = st.get("tickets", [])
        period = st.get("period", "")
        lr = st.get("last_result")

        if latest is not None:
            # 期号(左) + 开奖日期(右) 同行, 开奖球整体上移
            qh = latest["qh"]
            draw_text(d, qh, 8, 44, CLR_WHITE, CLR_BG)
            date = latest["date"]
            draw_ascii(d, date, W - 8 - ascii_w(date), 52, CLR_CYAN, CLR_BG)
            # 开奖球: 前区+后区一行 (r=15, 24 点阵号码), 前区红 / 后区蓝
            nf = self._game()["n_front"]
            nums = latest["front"] + latest["back"]
            bx = (W - (len(nums) * 30 + (len(nums) - 1) * 4)) // 2
            for i, num in enumerate(nums):
                draw_ball(d, bx, 73, 15, CLR_RED if i < nf else CLR_LOT_BLUE, num)
                bx += 34
        else:
            draw_ascii_center(d, "A=CHECK", 90, CLR_DIM, CLR_BG)

        # 状态行: {机选期号} - {已兑奖/未开奖}
        if period and tickets:
            draw_center(d, period + " - 未开奖", 106, CLR_AMBER, CLR_BG)
        elif lr is not None and lr.get("game") == data["game"]:
            draw_center(d, lr.get("period", "") + " - 已兑奖", 106,
                        CLR_LOT_GOLD, CLR_BG)
        else:
            draw_center(d, "当期无已选", 106, CLR_GRAY, CLR_BG)

        # 下方: 有票 = 票池 3 组机选号码 (16px 红/蓝); 已兑奖 = 逐注明细
        if period and tickets:
            d.hline(20, 136, W - 40, CLR_DIM)
            y = 143
            for t in tickets[:MAX_TICKETS]:
                self._draw_nums16_row(d, t.get("reds", []),
                                      t.get("blues", []), y)
                y += 19
        elif lr is not None and lr.get("game") == data["game"]:
            # 逐注明细 (ASCII 行, 最多 5 行): 号码 + 命中数 + 奖级 (#6)
            rows = []
            for dt in lr.get("details", []):
                fh, bh = dt.get("hits", [0, 0])
                rank = dt.get("rank", 0)
                row = " ".join(dt.get("reds", [])) + "+" + "+".join(dt.get("blues", []))
                row = row + " " + str(fh) + "+" + str(bh)
                if rank:
                    row = row + " #" + str(rank)
                rows.append(row)
            if rows:
                y = 152
                for i, row in enumerate(rows[:5]):
                    color = CLR_LOT_GOLD if "#" in row else CLR_GRAY
                    draw_ascii(d, str(i + 1) + " " + row[:28], 4, y, color,
                               CLR_BG)
                    y += 8
                if len(rows) > 5:
                    draw_ascii(d, "..." + str(len(rows) - 5), 4, y, CLR_DIM,
                               CLR_BG)

        self._draw_buttons(d)

    def _draw_cached_draw(self, d, lr):
        """拉取失败时的回退显示: 上次对奖缓存的开奖号码 (整体灰化标识)"""
        qh = lr.get("period", "")
        draw_text(d, qh, 8, 44, CLR_DIM, CLR_BG)
        date = lr.get("date", "")
        draw_ascii(d, date, W - 8 - ascii_w(date), 52, CLR_DIM, CLR_BG)
        nf = self._game()["n_front"]
        nums = list(lr.get("draw_front", [])) + list(lr.get("draw_back", []))
        if nums:
            bx = (W - (len(nums) * 30 + (len(nums) - 1) * 4)) // 2
            for i, num in enumerate(nums):
                draw_ball(d, bx, 73, 15, CLR_DIM, num)
                bx += 34
        draw_center(d, "上次开奖 请联网刷新", 106, CLR_GRAY, CLR_BG)
