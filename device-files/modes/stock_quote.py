"""
XianMuBox - 持仓股票模式伴生模块: 腾讯免费实时行情客户端
闲沐工坊 · v2.2

数据源: 腾讯公开行情接口 qt.gtimg.cn (备用 web.sqt.gtimg.cn),
一次批量请求 ≤20 只, 与持仓/预测 K 线的 mooview 接口互补:
  - mooview: 持仓列表(成本/股数) + 日 K 线 + AI 预测
  - 腾讯:    实时现价/昨收/今开/最高/最低/成交量/额/换手/PE/PB/市值/涨跌停

编码说明: 响应为 GBK 文本, 但本模式只消费纯 ASCII 数字字段
(股票名取自 mooview 持仓), 故逐字段 decode('utf-8') 即可, 无 GBK 依赖。
代码口径: 持仓与行情两侧一律经 norm_code() 归一为裸码, 保证 key 相等。

字段索引 (v_前缀变量 内 ~ 分隔, 详见 doc/免费股票api接口/测试报告):
  1名称 2代码 3现价 4昨收 5今开 6量(手) 30时间 31涨跌 32涨跌%
  33最高 34最低 37成交额(万) 38换手 39PE 43振幅 44流通市值
  45总市值 46PB 47涨停 48跌停
"""

_QT_URL = "http://qt.gtimg.cn/q="
_QT_URL_BAK = "http://web.sqt.gtimg.cn/q="
_QT_HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "http://finance.qq.com"}
_BATCH_LIMIT = 20          # 官方建议单次 ≤20 只
_MIN_FIELDS = 49           # 解析所需最少字段数

# 首位数字 → 市场前缀 (沪: 6 股票 / 5 基金ETF / 9 B股; 深: 0 主板 / 3 创业板 /
# 1 基金ETF / 2 B股; 北交所: 4 / 8)
_MKT_BY_FIRST = {"6": "sh", "5": "sh", "9": "sh",
                 "0": "sz", "3": "sz", "1": "sz", "2": "sz",
                 "4": "bj", "8": "bj"}


def norm_code(code):
    """代码归一 → 裸码: 去空格、转小写、剥 sh/sz/bj 前缀、剥 .SH/.SZ 后缀
    (如 " SZ000001 " / "600519.SH" → "000001" / "600519")。

    持仓(mooview)与行情(腾讯)两侧统一经此归一, 保证 _quotes 字典 key 相等;
    未知格式照原样返回, 不影响批量中的其他标的。
    """
    c = str(code or "").strip().lower()
    if "." in c:
        c = c.split(".", 1)[0]
    if c[:2] in ("sh", "sz", "bj"):
        c = c[2:]
    return c


def market_prefix(code):
    """裸码 → 带市场前缀 (6/5/9=沪 sh, 0/3/1/2=深 sz, 4/8=北 bj;
    已带前缀的输入先经 norm_code 归一)。

    首位无法判定时原样返回: 腾讯侧无效行会被解析层跳过, 静默不影响其他持仓。
    """
    c = norm_code(code)
    if not c:
        return c
    return _MKT_BY_FIRST.get(c[:1], "") + c


def build_url(codes, backup=False):
    """代码列表 → 批量行情 URL (自动去重截断到批量上限)"""
    seen = []
    for c in codes:
        p = market_prefix(c)
        if p not in seen:
            seen.append(p)
        if len(seen) >= _BATCH_LIMIT:
            break
    return (_QT_URL_BAK if backup else _QT_URL) + ",".join(seen)


def quote_headers():
    """行情请求建议头 (非浏览器访问需要 UA/Referer 降低被拦概率)"""
    return dict(_QT_HEADERS)


def parse_body(raw):
    """响应体 bytes → {原始代码(无前缀): 行情 dict}。

    无效行/停牌(现价为0)/字段不足的标的直接跳过 (调用方回退 mooview 价)。
    """
    out = {}
    if not raw:
        return out
    for line in raw.split(b";"):
        eq = line.find(b"=")
        if eq < 1:
            continue
        key = line[:eq].strip()
        if not key.startswith(b"v_"):
            continue
        try:
            tcode = key[2:].decode()          # 如 sh600519 (纯 ASCII)
        except Exception:
            continue
        raw_code = norm_code(tcode)
        seg = line[eq + 1:].strip()
        if len(seg) < 2 or seg[0:1] != b'"':
            continue
        endq = seg.find(b'"', 1)
        if endq < 0:
            continue
        f = seg[1:endq].split(b"~")
        if len(f) < _MIN_FIELDS:
            continue

        def _num(i, conv=float):
            try:
                return conv(f[i].decode())
            except Exception:
                return 0

        price = _num(3)
        if price <= 0:
            continue  # 停牌/无效快照
        out[raw_code] = {
            "price":      price,
            "prev_close": _num(4),
            "open":       _num(5),
            "volume":     _num(6, int),      # 手
            "change":     _num(31),          # 涨跌额
            "change_pct": _num(32),          # 涨跌幅 %
            "high":       _num(33),
            "low":        _num(34),
            "amount":     _num(37),          # 成交额 万元
            "turnover":   _num(38),          # 换手率 %
            "pe":         _num(39),
            "amplitude":  _num(43),          # 振幅 %
            "floatcap":   _num(44),          # 流通市值 亿
            "mktcap":     _num(45),          # 总市值 亿
            "pb":         _num(46),
            "limit_up":   _num(47),
            "limit_dn":   _num(48),
            "ts":         _num_str(f, 30),   # YYYYMMDDHHMMSS
        }
    return out


def _num_str(fields, i):
    try:
        return fields[i].decode()
    except Exception:
        return ""


def hhmm(ts):
    """YYYYMMDDHHMMSS → HH:MM (行情时间戳上屏用)"""
    if ts and len(ts) >= 12:
        return ts[8:10] + ":" + ts[10:12]
    return ""


def in_trading_window():
    """A股交易时段判定: 周一~五 9:15-15:05 (决定行情 30s/10min 刷新节奏)。

    RTC 不可用时按交易时段处理 (宁可多刷新)。星期约定 0=周日。
    """
    try:
        import machine
        t = machine.RTC().datetime()
        wd, hh, mm = t[3], t[4], t[5]
    except Exception:
        return True
    if wd == 0 or wd == 6:
        return False
    v = hh * 100 + mm
    return 915 <= v <= 1505


def json_loads(body):
    """JSON 解析 (mooview user/kxian 响应), 兼容 MicroPython:
    - 服务端 json.dumps 默认输出的 NaN/Infinity/-Infinity 扩展字面量,
      CPython json 接受而 MP ujson 严格拒绝 (真机解析必炸);
    - 先按原文解析 (字符串内容含 "NaN" 字样时零误伤), 仅失败且
      检出上述字面量时才清洗为 null 二次解析;
    - 剥 UTF-8 BOM。
    """
    import json
    txt = bytes(body).decode()
    if txt[:1] == "\ufeff":
        txt = txt[1:]
    try:
        return json.loads(txt)
    except Exception:
        if "NaN" not in txt and "Infinity" not in txt:
            raise
    txt = (txt.replace("-Infinity", "null")
              .replace("Infinity", "null")
              .replace("NaN", "null"))
    return json.loads(txt)
