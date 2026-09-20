"""
多功能终端框架 - AI 余额
闲沐工坊 · iOS Edition

功能:
  - 多卡片轮播: 每个 (服务商, key) 一张卡 (实时 API)
  - 余额配色 (按数字本身着色):
      >= 10 元  : 青色 (充足)
      1 ~ 10 元 : 琥珀 (警示但不刺眼)
      <  1 元 / None : 红色 (危险 / 失败)
  - 卡片布局: 服务商名 (sy_32x32) + 模糊化 API key (8x8 柠檬绿副标题) + 大号余额 (sy_32x32)
  - 操作:
      A 短按: 翻页 (下一张卡, 循环)
      B 短按: 立即刷新当前服务商 key 的最新余额
              (按下即蜂鸣 + 进度条扫入动画, HTTP 阻塞期间进度条保持,
               请求完成后重绘卡片自动清除, 避免用户以为卡死)
      C 短按: 退出返回主菜单
  - 自动轮播默认开启 (60s 自动切换下一张)
  - 5 分钟自动刷新: 每轮刷新完所有卡后, 自动切到下一张 (单卡不切)

数据来源 (settings.json v1.2 多 key schema):
  - ai_keys 由 framework.settings.get_provider_keys(provider) 提供
  - 每次 enter() 重新读取, HTTP 编辑后无需重启即可生效
"""

import gc
import time

try:
    import urequests as requests
except ImportError:
    import requests

try:
    from framework.settings import (
        get_provider_keys,
        _PROVIDER_META, _title_case,
    )
except ImportError:
    # 容错: settings 模块未到位时使用空实现
    def get_provider_keys(_provider):
        return []
    def _title_case(s):
        if not s:
            return s
        return s[0].upper() + s[1:] if len(s) > 1 else s.upper()
    _PROVIDER_META = {}

from font import hanzi_24 as hanzi24

# sy_32x32 大号数字字体（~7KB）：延迟到首次绘制时导入，
# 避免模块级加载时与 bluetooth 等残留模块挤占内存导致 OOM
_num_font_cache = None

def _get_num_font():
    """延迟加载 sy_32x32 大号字体，首次调用时 import 并缓存"""
    global _num_font_cache
    if _num_font_cache is None:
        try:
            from font import sy_32x32 as _f
            _num_font_cache = _f
        except ImportError:
            pass
    return _num_font_cache

from .base import BaseMode, SUB_FONT
from framework.colors import (
    CLR_WHITE, CLR_DIM, CLR_GRAY,
    CLR_CYAN, CLR_AMBER, CLR_RED,
    CLR_LIME, CLR_DARK, CLR_CHEV,   # AI 模式专属
    CLR_DOT_OFF,                    # 未选分页方块 (#CCCCCC)
)
try:
    from config import W, H
except ImportError:
    W, H = 240, 240

# ===== 布局常量 (按 photo/Deepseek.svg 像素坐标) =====
# 屏幕尺寸: 240x240
# 头部区域: y=0~50 (柠檬绿背景 #A5D63F)
HEADER_H = 50

# 服务商名称区域: y=70~117 (sy_32x32 高度约 37,基线 80)
PROVIDER_Y = 80  # 服务商名基线
CHEVRON_CY = 102  # Chevron箭头中心y坐标

# API key 副标题区域: y=124 (SUB_FONT 8x8 灰色,占 8px 高)
KEY_Y = 124

# 余额数字区域: y=160~197 (大号数字 32x32)
BALANCE_Y = 160  # 数字顶部y坐标 (_get_num_font()高度37, 底部197)

# 分页指示器区域: y=226 (底部居中)
PAGINATION_Y = 226
DOT_SIZE = 4
DOT_SPACING = 8  # 方块间距

# 刷新进度条 (卡片视图底部, 余额数字与分页点之间 y=209)
# 单卡刷新: HTTP 阻塞前先播 ~500ms 扫入动画填到 60%, 阻塞期间保持,
#           请求完成后重绘卡片自动清除 —— 避免用户以为卡死
RBAR_X = 40
RBAR_Y = 209
RBAR_W = 160
RBAR_H = 6
RBAR_SWEEP_TARGET = 0.6   # 扫入动画结束时的填充比例
RBAR_SWEEP_STEPS = 10     # 扫入动画帧数 (每帧 50ms, 共 500ms)

# ===================== API 配置 =====================
# 余额查询已改为各服务商官方接口本地直连。
# 以下接口/字段为常见实现，若厂商文档有变，请在此修改。
#
# DeepSeek: GET https://api.deepseek.com/user/balance
#   余额字段: balance_infos[0].total_balance
# SiliconFlow: GET https://api.siliconflow.com/v1/user/info
#   (原 /user/info 已于 2026-08-14 停服返回 410, 待官方替代接口;
#    域名已从 .cn 迁移到 .com)
#   余额字段: data.totalBalance / data.balance
# Zhipu: 暂无公开余额接口，返回待配置提示
# Kimi(Moonshot): GET https://api.moonshot.cn/v1/users/me/balance
#   余额字段: data.available_balance

# 数据刷新间隔 5 分钟
_REFRESH_MS = 300000

# 自动轮播间隔 60s (模块级常量, 与 _REFRESH_MS 风格一致)
_AUTO_INTERVAL_MS = 60000


# ===== 兼容导出: 旧代码引用 CARDS 不会 ImportError =====
CARDS = []


def _load_cards():
    """根据 settings.json 构建 cards 列表 (v1.2 多 key).

    每个服务商的所有有效 key 都展开成独立卡片. 顺序:
      1. settings.json api_keys 中的非 ai_token 服务商 (按存储顺序)
      2. 同一服务商内按存储顺序排列

    返回 [{name, provider, key, label, balance}], 无 keys 时返回 [].
    """
    cards = []
    api = {}
    hidden = []
    try:
        from framework.settings import load_settings
        st = load_settings()
        api = st.get("api_keys", {}) or {}
        hx = st.get("api_keys_hidden", [])
        if isinstance(hx, list):
            hidden = [str(p) for p in hx]
    except Exception:
        api = {}

    for prov, items in api.items():
        if prov == "ai_token":
            continue
        if prov in hidden:
            continue
        if not isinstance(items, list):
            continue
        meta = _PROVIDER_META.get(prov, {})
        api_provider = meta.get("api_provider", prov.upper())
        display_name = meta.get("label", _title_case(prov))
        for idx, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            k = str(it.get("key", "")).strip()
            if not k:
                continue
            lbl = str(it.get("label", "")).strip()
            if not lbl:
                lbl = display_name
            cards.append({
                "name": display_name,
                "provider": api_provider,
                "key": k,
                "label": lbl[:24],
                "balance": None,
            })
    return cards


def _calc_color(balance):
    """根据余额计算警告色"""
    if balance is None:
        return CLR_RED, "ERROR"
    if balance >= 10:
        return CLR_CYAN, ""
    if balance >= 1:
        return CLR_AMBER, "LOW"
    return CLR_RED, "DANGER"


def _safe_float(s, default=0.0):
    """安全转换字符串为浮点数"""
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _find_balance(data, paths):
    """按点分路径列表在响应字典中查找余额数值.

    路径中数字表示列表索引, 如 'balance_infos.0.total_balance'.
    找到任意有效数值即返回, 否则返回 None.
    """
    for path in paths:
        v = data
        try:
            for part in path.split("."):
                if part.isdigit():
                    v = v[int(part)]
                else:
                    v = v[part]
            if v is not None and v != "":
                return _safe_float(v)
        except Exception:
            continue
    return None


def _http_get_json(url, key):
    """本地余额查询通用 GET 请求, 返回 (status_code, data_dict).

    请求完成后主动关闭 response, 避免 MicroPython urequests 连接泄漏.
    """
    headers = {"Authorization": "Bearer " + key}
    r = requests.get(url, headers=headers, timeout=10)
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


def _fetch_deepseek_balance(key):
    """DeepSeek 官方余额接口.

    接口: GET https://api.deepseek.com/user/balance
    响应示例: {"is_available": true, "balance_infos": [
        {"currency": "CNY", "total_balance": "123.45", ...}]}
    """
    url = "https://api.deepseek.com/user/balance"
    status, data = _http_get_json(url, key)
    if status != 200:
        return {"ok": False, "error": "HTTP " + str(status)}
    bal = _find_balance(data, [
        "balance_infos.0.total_balance",
        "balance_infos.0.balance",
        "balance",
    ])
    if bal is None:
        return {"ok": False, "error": "余额字段未找到"}
    return {"ok": True, "balance": bal}


def _fetch_siliconflow_balance(key):
    """SiliconFlow 官方用户信息/余额接口.

    接口: GET https://api.siliconflow.com/v1/user/info
    响应示例: {"status": "ok", "data": {"balance": "123.45", ...}}

    注意: 原接口已于 2026-08-14 停止服务 (官方公告, 返回 410 Gone),
    官方域名也从 api.siliconflow.cn 迁移到 api.siliconflow.com.
    替代账户 API 尚未发布; 保留请求与解析路径, 上线后自动恢复.
    """
    url = "https://api.siliconflow.com/v1/user/info"
    status, data = _http_get_json(url, key)
    if status in (404, 410):
        return {"ok": False, "error": "接口停服待恢复", "stopped": True}
    if status != 200:
        return {"ok": False, "error": "HTTP " + str(status)}
    bal = _find_balance(data, [
        "data.totalBalance",
        "data.balance",
        "data.user_balance",
    ])
    if bal is None:
        return {"ok": False, "error": "余额字段未找到"}
    return {"ok": True, "balance": bal}


def _fetch_kimi_balance(key):
    """Kimi (Moonshot) 官方余额接口.

    接口: GET https://api.moonshot.cn/v1/users/me/balance
    响应示例: {"code": 0, "data": {"available_balance": 123.45,
              "voucher_balance": ..., "cash_balance": ...},
              "scode": "0x0", "status": true}
    """
    url = "https://api.moonshot.cn/v1/users/me/balance"
    status, data = _http_get_json(url, key)
    if status != 200:
        return {"ok": False, "error": "HTTP " + str(status)}
    bal = _find_balance(data, ["data.available_balance", "data.balance"])
    if bal is None:
        return {"ok": False, "error": "余额字段未找到"}
    return {"ok": True, "balance": bal}


def _fetch_zhipu_balance(key):
    """Zhipu 暂不提供公开余额查询接口."""
    return {"ok": False, "error": "Zhipu 余额接口未配置"}


# provider 代号(与 card["provider"] 对应, 来自 _PROVIDER_META["api_provider"])
# 或 settings.json 中的 provider_key -> 统一小写匹配
_BALANCE_ADAPTERS = {
    "deepseek": _fetch_deepseek_balance,
    "DEEPSEEK": _fetch_deepseek_balance,
    "siliconflow": _fetch_siliconflow_balance,
    "SiliconFlow": _fetch_siliconflow_balance,
    "zhipu": _fetch_zhipu_balance,
    "ZHIPU": _fetch_zhipu_balance,
    "kimi": _fetch_kimi_balance,
    "KIMI": _fetch_kimi_balance,
    "moonshot": _fetch_kimi_balance,
    "Moonshot": _fetch_kimi_balance,
}


def _fetch_one_card(cards, idx):
    """拉取单张卡片余额, 可被 loop 逐步调用实现可中断刷新.

    返回 (success: bool, balance_or_None).
    """
    if not cards or idx < 0 or idx >= len(cards):
        return False, None
    card = cards[idx]
    adapter = _BALANCE_ADAPTERS.get(card.get("provider", ""))
    if adapter is None:
        print("[ai] no adapter for", card.get("provider", ""))
        card["balance"] = None
        card["error"] = "无查询适配器"
        card["stopped"] = False
        return False, None
    try:
        print("[ai] GET", card["provider"], card.get("label", ""))
        result = adapter(card["key"])
        if result.get("ok"):
            card["balance"] = result["balance"]
            card["error"] = None
            card["stopped"] = False
            print("[ai] ok, provider=", card["provider"],
                  "label=", card.get("label", ""),
                  "balance=", card["balance"])
            return True, card["balance"]
        else:
            print("[ai] provider=", card["provider"],
                  "error=", result.get("error"))
            card["balance"] = None
            card["error"] = result.get("error", "")
            card["stopped"] = bool(result.get("stopped"))
    except Exception as e:
        print("[ai] FAIL", card["provider"], repr(e))
        card["balance"] = None
        card["error"] = "请求异常"
        card["stopped"] = False
    return False, None


def _fetch_balances():
    """从 API 拉取所有 key 余额 (兼容旧调用, 模块级入口).

    实际推荐用 AIBalanceMode.enter() 时构建 self.cards 后逐张拉取.
    这里为兼容旧引用而保留, 直接调用会即时构建一份 cards 跑一遍.
    """
    cards = _load_cards()
    if not cards:
        return 0
    ok_count = 0
    for i in range(len(cards)):
        success, _ = _fetch_one_card(cards, i)
        if success:
            ok_count += 1
        gc.collect()
    print("[ai] done, ok=", ok_count)
    return ok_count


def _fetch_balances_now():
    """即时拉取所有 key 余额, 返回结构化结果给 HTTP /api/balance.

    返回 [{"name", "label", "provider", "balance", "status"}, ...]
    status ∈ {"ok", "error", "empty"}, error 含 "msg".
    """
    cards = _load_cards()
    if not cards:
        return []
    out = []
    for idx, card in enumerate(cards):
        success, bal = _fetch_one_card(cards, idx)
        if success:
            out.append({
                "name": card["name"],
                "label": card.get("label", ""),
                "provider": card["provider"],
                "balance": bal,
                "status": "ok",
            })
        else:
            out.append({
                "name": card["name"],
                "label": card.get("label", ""),
                "provider": card["provider"],
                "balance": None,
                "status": "error",
                "msg": card.get("error") or "拉取失败",
            })
        gc.collect()
    return out


def _draw_chevron(d, cx, cy, size, direction, color):
    """绘制 < 或 > 箭头 (单线 SVG 风)
    SVG 中左右箭头均为单线 chevron path, 中心 (20,102)/(222,102)"""
    if direction < 0:  # <
        d.line(cx + size, cy - size, cx, cy, color)
        d.line(cx, cy, cx + size, cy + size, color)
    else:  # >
        d.line(cx - size, cy - size, cx, cy, color)
        d.line(cx, cy, cx - size, cy + size, color)


def _draw_star_icon(d, cx, cy, color):
    """绘制 SVG 风格装饰 (4角星 + 矩形塔 + M 形)
    SVG 原始坐标: 4 角星 12 边形 (215.6~225, 13~22.4)
                  矩形塔 (218.8~221.8, 25~35) - 4x11 像素
                  M 形 (201~215.9, 16~35) - 双峰
    本函数将 (cx, cy) 视为装饰中心, 4 角星位于上方, 塔与 M 形位于下方"""
    # 1. 4 角星 12 边形 (上偏) - 顶点坐标相对中心偏移
    # SVG 中心约 (220, 17.7)
    sx, sy = cx, cy - 6   # 星中心
    star_pts = (
        (sx - 1,  sy - 5),  # 顶部偏左
        (sx,      sy - 7),  # 顶部
        (sx + 1,  sy - 5),  # 顶部偏右
        (sx + 4,  sy - 4),  # 右上
        (sx + 5,  sy - 3),  # 右上
        (sx + 5,  sy - 1),  # 右中
        (sx + 2,  sy),      # 右下
        (sx + 1,  sy + 3),  # 底部偏右
        (sx,      sy + 3),  # 底部
        (sx - 1,  sy),      # 左下
        (sx - 5,  sy - 1),  # 左中
        (sx - 5,  sy - 3),  # 左上
    )
    for i in range(len(star_pts)):
        a = star_pts[i]
        b = star_pts[(i + 1) % len(star_pts)]
        d.line(a[0], a[1], b[0], b[1], color)
    # 填充: 画一对交叉对角线营造充实感
    d.line(sx - 3, sy - 1, sx + 3, sy + 1, color)
    d.line(sx - 3, sy + 1, sx + 3, sy - 1, color)

    # 2. 矩形塔 (中心) - SVG: x=218.8~221.8 y=25~35, 宽4高10
    # 相对 (cx, cy) 下偏
    d.fill_rect(cx - 2, cy + 4, 4, 11, color)

    # 3. M 形 (下偏左) - SVG: x=201~215.9 y=16~35
    # 中心位于 cx-12, cy+9, M 字母宽 14 高 19
    mx, my = cx - 12, cy + 9
    mh = 12  # 高度
    mw = 8   # 宽度
    # 左侧纵
    d.line(mx, my, mx, my + mh, color)
    # 中间双峰
    d.line(mx, my, mx + mw // 2, my + mh // 2, color)
    d.line(mx + mw // 2, my + mh // 2,
           mx + mw, my, color)
    # 右侧纵
    d.line(mx + mw, my, mx + mw, my + mh, color)


class AIBalanceMode(BaseMode):
    """AI 余额模式 - 实时 API 查询 (v1.2: 每 key 一张卡)"""

    MODE_ID = "ai_balance"

    def enter(self):
        """进入: 重新构建 cards (读取最新 settings.json) + 启动非阻塞 fetch."""
        # 关键: 每次进模式都重新读 settings.json, 让 HTTP 编辑立即生效
        self.cards = _load_cards()
        # 当前索引钳到合法范围 (避免上次索引因删 key 而越界)
        self._idx = 0
        if not self.cards:
            self._idx = 0
        elif self._idx >= len(self.cards):
            self._idx = 0
        self._last_switch = 0
        self._auto_on = True
        self._last_auto = 0
        self._last_refresh = 0
        # 状态机: loading 期间 loop 负责 fetch + 动画
        self._loading = True
        self._loading_step = 0
        # 5min 刷新状态机 (_refresh_single=True 表示只刷当前卡, 不自动翻页)
        self._refreshing = False
        self._refresh_idx = 0
        self._refresh_single = False
        # 绘制初始加载界面（不播放动画，直接开始 fetch）
        if not self.cards:
            # 空态: 直接画"未配置 Key"提示, 不进入 loading 状态机
            self._loading = False
            self._draw_empty()
        else:
            self._draw_loading_ui()

    def _draw_empty(self):
        """空态画面: 未配置任何 key 时显示."""
        d = self.display
        d.fill(CLR_DARK)
        d.fill_rect(0, 0, W, HEADER_H, CLR_LIME)
        d.write(hanzi24, "模型余额", 10, 13, CLR_WHITE, CLR_LIME)
        d.write(hanzi24, "AI↑", W - 50, 13, CLR_WHITE, CLR_LIME)
        # 居中提示文字
        empty_text = "未配置 Key"
        tw = 0
        for c in empty_text:
            try:
                tw += hanzi24._WIDTHS[hanzi24.MAP.index(c)]
            except ValueError:
                tw += hanzi24._WIDTHS[0]
        tx = (W - tw) // 2
        d.write(hanzi24, empty_text, tx, 110, CLR_WHITE, CLR_DARK)
        # 副提示
        sub = "请在 HTTP 页配置"
        sw = 0
        for c in sub:
            try:
                sw += hanzi24._WIDTHS[hanzi24.MAP.index(c)]
            except ValueError:
                sw += hanzi24._WIDTHS[0]
        sx = (W - sw) // 2
        d.write(hanzi24, sub, sx, 145, CLR_DIM, CLR_DARK)

    def _draw_loading_ui(self):
        """绘制加载中的 UI（静态界面，供 loop 中动态更新）"""
        d = self.display

        # 绘制头部
        d.fill(CLR_DARK)
        d.fill_rect(0, 0, W, HEADER_H, CLR_LIME)
        d.write(hanzi24, "模型余额", 10, 13, CLR_WHITE, CLR_LIME)
        d.write(hanzi24, "AI↑", W - 50, 13, CLR_WHITE, CLR_LIME)

        # 先清除服务商名称区域（防止残留）
        clear_w = W  # 清除整个宽度
        clear_h = _get_num_font().HEIGHT + 10  # 字体高度 + 上下边距
        clear_y = PROVIDER_Y - 5  # 向上偏移一点确保完全覆盖
        d.fill_rect(0, clear_y, clear_w, clear_h, CLR_DARK)

        # 绘制加载提示文字（居中显示）
        loading_text = "Loading..."
        nw = 0
        for c in loading_text:
            try:
                idx = _get_num_font().MAP.index(c)
                nw += _get_num_font()._WIDTHS[idx]
            except ValueError:
                nw += _get_num_font()._WIDTHS[0]
        name_x = (W - nw) // 2
        d.write(_get_num_font(), loading_text, name_x, PROVIDER_Y, CLR_WHITE, CLR_DARK)
        # 左右箭头
        _draw_chevron(d, 20, CHEVRON_CY, 6, -1, CLR_CHEV)
        _draw_chevron(d, 222, CHEVRON_CY, 6, 1, CLR_CHEV)

        # 绘制进度条框架
        bar_x = 30
        bar_y = 175
        bar_w = W - 60  # 180
        bar_h = 12
        d.fill_rect(bar_x, bar_y, bar_w, bar_h, CLR_DIM)
        d.rect(bar_x, bar_y, bar_w, bar_h, CLR_WHITE)


    def _do_loading_fetch(self):
        """loop 中调用: 完成 fetch 状态机一步 (单张卡片)，同时更新进度条动画
        返回 True 表示加载完成，可以切换到正常卡片视图"""
        d = self.display
        bar_x = 30
        bar_y = 175  # 与 _draw_loading_ui() 保持一致
        bar_w = W - 60  # 180
        bar_h = 12

        if self._loading_step < len(self.cards):
            # 计算当前进度百分比
            progress = self._loading_step / len(self.cards) if self.cards else 0
            filled_w = int(bar_w * progress)

            # 绘制进度条填充
            d.fill_rect(bar_x, bar_y, bar_w, bar_h, CLR_DIM)
            if filled_w > 0:
                d.fill_rect(bar_x, bar_y, filled_w, bar_h, CLR_LIME)
            d.rect(bar_x, bar_y, bar_w, bar_h, CLR_WHITE)

            # 更新文字提示（显示当前 key 的 label）
            card_label = self.cards[self._loading_step].get("label", "")

            # 先清除旧文字区域（防止文字重叠）
            clear_w = W  # 清除整个宽度
            clear_h = _get_num_font().HEIGHT + 10  # 字体高度 + 上下边距
            clear_y = PROVIDER_Y - 5  # 向上偏移一点确保完全覆盖
            d.fill_rect(0, clear_y, clear_w, clear_h, CLR_DARK)

            # 计算新文字宽度并居中
            nw = 0
            for c in card_label:
                try:
                    idx = _get_num_font().MAP.index(c)
                    nw += _get_num_font()._WIDTHS[idx]
                except ValueError:
                    nw += _get_num_font()._WIDTHS[0]
            name_x = (W - nw) // 2
            d.write(_get_num_font(), card_label, name_x, PROVIDER_Y, CLR_WHITE, CLR_DARK)
            # 左右箭头
            _draw_chevron(d, 20, CHEVRON_CY, 6, -1, CLR_CHEV)
            _draw_chevron(d, 222, CHEVRON_CY, 6, 1, CLR_CHEV)

            # 执行 API 请求
            try:
                success, _ = _fetch_one_card(self.cards, self._loading_step)
                if success:
                    print("[ai] OK:", card_label)
                else:
                    print("[ai] FAIL:", card_label)
            except Exception as e:
                print("[ai] fetch card", self._loading_step, "err:", repr(e))

            self._loading_step += 1
            return False  # 还未完成，下帧继续

        # 所有卡片加载完成
        # 绘制满进度条
        d.fill_rect(bar_x, bar_y, bar_w, bar_h, CLR_LIME)
        d.rect(bar_x, bar_y, bar_w, bar_h, CLR_WHITE)

        # 短暂显示"Done"
        # 先清除旧文字区域（防止文字重叠）
        clear_w = W  # 清除整个宽度
        clear_h = _get_num_font().HEIGHT + 10  # 字体高度 + 上下边距
        clear_y = PROVIDER_Y - 5  # 向上偏移一点确保完全覆盖
        d.fill_rect(0, clear_y, clear_w, clear_h, CLR_DARK)

        done_text = "Done"
        nw = 0
        for c in done_text:
            try:
                idx = _get_num_font().MAP.index(c)
                nw += _get_num_font()._WIDTHS[idx]
            except ValueError:
                nw += _get_num_font()._WIDTHS[0]
        name_x = (W - nw) // 2
        d.write(_get_num_font(), done_text, name_x, PROVIDER_Y, CLR_LIME, CLR_DARK)
        time.sleep_ms(200)

        # 切换到正常卡片视图
        self._last_refresh = time.ticks_ms()
        self._loading = False
        try:
            self._draw_card()
        except Exception as e:
            print("[ai] _draw_card crashed:", repr(e))
            d.fill(CLR_DARK)
            d.write(hanzi24, "渲染失败", 60, 100, CLR_RED, CLR_DARK)
        return True

    def _draw_refresh_bar(self, progress):
        """绘制/更新刷新进度条 (余额数字与分页点之间).

        progress: 0.0~1.0 填充比例. 重绘卡片时会整屏覆盖清除.
        """
        d = self.display
        d.fill_rect(RBAR_X, RBAR_Y, RBAR_W, RBAR_H, CLR_DARK)
        d.rect(RBAR_X, RBAR_Y, RBAR_W, RBAR_H, CLR_DIM)
        fill_w = int((RBAR_W - 2) * progress)
        if fill_w > 0:
            d.fill_rect(RBAR_X + 1, RBAR_Y + 1, fill_w, RBAR_H - 2, CLR_LIME)

    def _sweep_refresh_bar(self):
        """单卡刷新的即时反馈动画: HTTP 阻塞前播放 ~500ms,
        进度条 0 -> RBAR_SWEEP_TARGET 平滑推进, 明确告知"刷新进行中".

        随后的 HTTP 阻塞期间进度条停在目标位置 (与 stock.py 加载占位同思路),
        请求完成后 loop 会重绘卡片, 进度条随之清除.
        """
        for i in range(1, RBAR_SWEEP_STEPS + 1):
            self._draw_refresh_bar(RBAR_SWEEP_TARGET * i / RBAR_SWEEP_STEPS)
            time.sleep_ms(50)

    def _draw_card(self):
        """绘制当前卡片 (v1.2: 标题为 card.label, 如 'DeepSeek-1')"""
        d = self.display
        if not self.cards:
            self._draw_empty()
            return
        if self._idx >= len(self.cards):
            self._idx = 0
        card = self.cards[self._idx]
        balance = card["balance"]
        bal_color, _warn_unused = _calc_color(balance)

        # ---- 1. 深灰背景 (#333333) ----
        d.fill(CLR_DARK)

        # ---- 2. 柠檬绿头部栏 (y=0~50) ----
        d.fill_rect(0, 0, W, HEADER_H, CLR_LIME)
        # 左侧标题 "模型余额" (hanzi24, y=13基线)
        d.write(hanzi24, "模型余额", 10, 13, CLR_WHITE, CLR_LIME)
        # 右侧 AI 图标占位 (简化为文字 "AI")
        d.write(hanzi24, "AI↑", W - 50, 13, CLR_WHITE, CLR_LIME)

        # ---- 3. 卡片 label (用户自定义, 如 "DeepSeek-1") + 右翻页箭头 ----
        name = card.get("label", card.get("name", ""))
        nw = 0
        for c in name:
            try:
                idx = _get_num_font().MAP.index(c)
                nw += _get_num_font()._WIDTHS[idx]
            except ValueError:
                nw += _get_num_font()._WIDTHS[0]
        name_x = (W - nw) // 2
        d.write(_get_num_font(), name, name_x, PROVIDER_Y, CLR_WHITE, CLR_DARK)
        _draw_chevron(d, 222, CHEVRON_CY, 6, 1, CLR_CHEV)

        # ---- 4. API key 副标题 (模糊化, SUB_FONT 8x8 灰色居中) ----
        # 模糊规则: 保留前 3 + "***" + 后 3; 短 key 原样显示
        key_raw = card.get("key", "") or ""
        if len(key_raw) > 8:
            key_show = key_raw[:3] + "***" + key_raw[-3:]
        else:
            key_show = key_raw
        # SUB_FONT 8x8 等宽, 宽度 = 字符数 * 8
        kw = len(key_show) * 8
        kx = (W - kw) // 2
        if SUB_FONT:
            d.text(SUB_FONT, key_show, kx, KEY_Y, CLR_LIME, CLR_DARK)

        # ---- 5. 大号余额数字 (32x32 居中, 颜色按阈值) ----
        if balance is None and card.get("stopped"):
            # 接口停服 (如 SiliconFlow /user/info 2026-08-14 下线):
            # 用 hanzi24 居中显示提示, 琥珀色区别于普通错误的红色 "--"
            stopped_text = "停服待恢复"
            sw = 0
            for c in stopped_text:
                try:
                    sw += hanzi24._WIDTHS[hanzi24.MAP.index(c)]
                except ValueError:
                    sw += hanzi24._WIDTHS[0]
            sx = (W - sw) // 2
            # hanzi24 高度 25, 数字区高 37, 垂直居中偏移 6
            d.write(hanzi24, stopped_text, sx, BALANCE_Y + 6,
                    CLR_AMBER, CLR_DARK)
        else:
            if balance is None:
                big_str = "--"
            else:
                is_neg = balance < 0
                abs_bal = -balance if is_neg else balance
                int_part = int(abs_bal)
                dec_part = int((abs_bal - int_part) * 100)
                big_str = ("-" if is_neg else "") + str(int_part) + "." + ("%02d" % dec_part)

            bw = 0
            for c in big_str:
                try:
                    idx = _get_num_font().MAP.index(c)
                    bw += _get_num_font()._WIDTHS[idx]
                except ValueError:
                    bw += _get_num_font()._WIDTHS[0]
            bal_x = (W - bw) // 2
            d.write(_get_num_font(), big_str, bal_x, BALANCE_Y, bal_color, CLR_DARK)

        # ---- 6. 分页方块 (y=226) ----
        n_cards = len(self.cards)
        total_width = n_cards * DOT_SIZE + (n_cards - 1) * DOT_SPACING
        start_x = (W - total_width) // 2
        for i in range(n_cards):
            dx = start_x + i * (DOT_SIZE + DOT_SPACING)
            dot_color = CLR_LIME if i == self._idx else CLR_DOT_OFF
            d.fill_rect(dx, PAGINATION_Y, DOT_SIZE, DOT_SIZE, dot_color)

    def _switch_card(self, direction):
        """切换卡片 (带防抖). 空 cards 时不响应."""
        if not self.cards:
            return
        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_switch) < 250:
            return
        self._idx = (self._idx + direction) % len(self.cards)
        self._last_switch = now
        self._last_auto = now
        self._draw_card()
        if self.buzzer:
            card = self.cards[self._idx]
            bal = card["balance"]
            self.buzzer.tone(900 if bal is not None and bal >= 1 else 1500, 15)

    def loop(self):
        """
        主循环 (非阻塞, 按钮即时响应)
        A 短按: 翻页 (下一张, 循环)
        B 短按: 刷新当前卡余额
        C 短按: 退出返回主菜单
        """
        try:
            up, sel, down, b_long, a_long, c_long = self._scan_with_timing()

            # ---- 加载状态: 步进式拉取, 期间可响应 C 退出 ----
            if self._loading:
                if down:
                    self.flash_exit()
                    self._loading = False
                    return False
                # 步进式拉取 (每帧拉一张, 期间可响应按钮)
                self._do_loading_fetch()
                time.sleep_ms(50)
                return True

            # ---- 刷新状态: 逐张拉取, 可被 C 中断退出 ----
            if self._refreshing:
                if down:
                    self._refreshing = False
                    self.flash_exit()
                    return False
                if not self.cards:
                    self._refreshing = False
                    return True
                # 刷新反馈: 全卡轮刷新显示真实步进进度
                # (单卡刷新的反馈已在 B 按下时的扫入动画给出)
                if not self._refresh_single:
                    self._draw_refresh_bar(
                        self._refresh_idx / len(self.cards))
                _fetch_one_card(self.cards, self._refresh_idx)
                self._refresh_idx += 1
                if self._refresh_idx >= len(self.cards):
                    # 本轮刷新完成
                    self._refreshing = False
                    self._last_refresh = time.ticks_ms()
                    if not self._refresh_single and len(self.cards) > 1:
                        # 全卡轮刷新: 自动切到下一张 (单卡不切, 原地刷新)
                        self._idx = (self._idx + 1) % len(self.cards)
                        # 同步重置 60s 自动轮播计时, 避免刚切完又触发
                        self._last_auto = time.ticks_ms()
                        if self.buzzer:
                            _b = self.cards[self._idx]["balance"]
                            self.buzzer.tone(900 if _b is not None and _b >= 1 else 1500, 15)
                    self._draw_card()
                time.sleep_ms(50)
                return True

            # ---- B 短按: 立即刷新当前卡余额 ----
            if sel and self.cards and not self._refreshing:
                self._refreshing = True
                self._refresh_single = True
                self._refresh_idx = self._idx
                # 即时反馈: 蜂鸣确认 + 进度条扫入动画 (HTTP 阻塞前),
                # 阻塞期间进度条保持, 避免用户以为卡死
                if self.buzzer:
                    self.buzzer.tone(900, 15)
                self._sweep_refresh_bar()
                time.sleep_ms(50)
                return True

            # ---- A 短按: 翻页到下一张 (空态不响应) ----
            if up:
                if self.cards:
                    self._switch_card(1)
                time.sleep_ms(50)
                return True

            # ---- C 短按: 退出返回主菜单 ----
            if down:
                self.flash_exit()
                return False

            # ---- 自动轮播 (非阻塞, 空态不响应) ----
            if self._auto_on and self.cards:
                now = time.ticks_ms()
                if time.ticks_diff(now, self._last_auto) >= _AUTO_INTERVAL_MS:
                    self._last_auto = now
                    self._idx = (self._idx + 1) % len(self.cards)
                    self._draw_card()
                    if self.buzzer:
                        card = self.cards[self._idx]
                        bal = card["balance"]
                        self.buzzer.tone(900 if bal is not None and bal >= 1 else 1500, 15)
                    time.sleep_ms(50)
                    return True

            # ---- 定时刷新余额 (5分钟) - 启动可中断刷新状态机 ----
            if self.cards:
                now = time.ticks_ms()
                if time.ticks_diff(now, self._last_refresh) >= _REFRESH_MS:
                    self._refreshing = True
                    self._refresh_idx = 0
                    self._last_refresh = now  # 避免重复触发

            time.sleep_ms(50)
            return True
        except Exception as e:
            print("[ai] loop error:", repr(e))
            return False

    def exit(self):
        """退出: 无资源清理"""
        pass
