"""
XianMuBox - 全局整点新闻播报服务 (v2.3)

由 BaseMode.run() / 主菜单循环逐帧调用 check(), 设备停在任意界面,
整点到点即接管屏幕进入播报页: 拉取闲沐云平台 news_hourly 数据源
(整点档, 每日 9:00~22:00 共 14 档, 服务端每小时 :55 预生成),
播放当档音频 (MAX98357A), 播报期间屏幕只显示喇叭帧动画
+ 进度条, 不显示文字内容 (v1.5.1 确立, v1.6 维持)。

v2.3 MP3 直连: 平台 audio_url 为 .mp3 时直接播放 (固件需含 mp3dec
模块, mp129-romfont-mp3dec 起), 网络负载仅 WAV 的 1/8~1/24;
旧固件无解码器时自动回退原 .mp3->.wav 换后缀链路, 行为不变。

v1.9 SD 卡存档 (framework/sdcard 挂载层就绪时启用):
  - SD 挂载成功则当档音频先分块落盘 /sd/music/news_YYYYMMDD_HH.mp3
    (或 .wav, 按平台当档音源格式) 再从文件播放 (同档重复播报免重下);
    落盘失败/未挂载/RTC 无效一律回退原有在线流式 (现状路径, 行为不变)
  - 存档保留一天: 每次落盘/打开回听列表时清理非当日文件
  - replay_list()/replay(): 供新闻模式"今日回听"页选择回听
    (mp3/wav 存档均可回听, 按扩展名分派解码)
  - 落盘下载期间 (v1.9.2) 中央动画区显示下载图标帧 (箭头落托盘
    往返轮播), 不再用喇叭帧 — 避免下载期间"有播放图标却无声"的
    语义误导; 状态行同步显示琥珀色「SD缓存中」+ 字节进度条

数据来源 (framework/cloud 统一取数通道):
  GET /api/v1/data/news_hourly -> payload (官方手册 v2026-09):
    {"items": [{"title","url"}...], "hour_label": "15点",
     "audio_url": "/media/news_audio/latest.wav",   ← 24kHz/16bit/单声道
     "script_url": "/media/news_audio/latest.txt",
     "duration_est_sec": 165, "size_bytes": ~8MB,
     "generated_by": "llm", "source_name": "..."}
  audio_url 为 .mp3 时 (mp129-romfont-mp3dec 起) 直接流式解码播放,
  否则按原优先级解析 WAV; audio_url/script_url 为站内相对路径, 与
  平台 endpoint 拼接后普通 GET 下载 (无需鉴权头); 音频不可用时降级
  为纯文字翻页+蜂鸣提示 (唯一会显示文字的路径)。

设计约束:
  - check() 常驻每帧: 只做 RTC 分钟变化比较, 开销极小
  - 分钟沿匹配 + 5 分钟回看补漏 (阻塞操作跳过秒沿不会漏播);
    回看窗口内同一档位用 "日-时" 指纹防重
  - 配置热加载: 每次触发直接读 settings 模块缓存 (Web 保存即生效)
  - 取数兜底 (v1.8.3): 网络类失败 (网络失败/云服务异常) 屏显
    「10秒后重试」等 10s 重取一次, 任意键提前放弃; 业务类错误
    (未订阅/错误码/暂无数据) 不重试, 直接进失败页
  - 播报交互是阻塞式接管 (宿主模式 loop 暂停), 结束后由宿主
    enter() 整页重绘恢复; 播报中任意键停止
  - 动画 (v1.6.2 喇叭帧): 播报页按已播秒数轮播 4 帧 Tabler 喇叭
    图标 (声波 0→3→0 档, build 由 PNG 生成 RGB565 raw, blit 单帧
    约 3ms, 仅为整区重绘开销的零头); 由 play_wav 的 on_progress
    逐块驱动 (I2S 写阻塞天然按播放速率节拍); 红横幅直播点 500ms
    闪烁, 状态行右侧已播时长, 底部琥珀色进度条。v1.6 程序绘制
    均衡器 (12 柱) 与 v1.6.1 差量重绘版均因绘制耗时任占比过高
    弃用 (真机 _DBG 实测 54ms/30ms 每块)
  - 手动播报: request_play() 置标志 (Web /api/news/play 与新闻
    模式使用), check() 检出后无论时段/开关立即播报一次
  - 播报结果缓存于内存 (status() 供 Web /api/news/status 展示)

MicroPython 兼容: 无 f-string / time.ticks_* / urequests 惰性 import
"""

import time
from machine import RTC

try:
    from config import W, H
except ImportError:
    W, H = 240, 240

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM,
    CLR_GREEN, CLR_RED, CLR_AMBER,
)

SOURCE_CODE = "news_hourly"
_FETCH_TIMEOUT = 10        # 媒体文件下载超时 (秒)
_FETCH_RETRY_MS = 10000    # 取数网络类失败后的重试等待 (v1.8.3)
_SCRIPT_MAX = 3000         # 播报稿最多下载字节数 (约 1000 汉字)
_SCRIPT_PAGE_MS = 4000     # 无音频降级时, 纯文字翻页停留时长
_FALLBACK_BEEP_HZ = 2600   # 降级提示音
_SD_DIR = "/sd/music"      # SD 存档目录 (v1.9, 未挂载时全部回退在线流播)
_SD_DL_CHUNK = 8192        # 落盘下载块大小 (字节, 不整档进 RAM)
_WAV_BPS = 48000           # WAV 字节率 (24kHz*16bit*单声道), 回听时长估算
_MP3_BPS = 16000           # MP3 字节率假设 (128kbps), 仅回听时长估算 (进度不受影响)
_RTC = None
_HZ24 = None
_TXT_FONT = None           # 新闻正文字体 (hanzi_24 全量覆盖)

# ---- 播报页喇叭帧动画 (v1.6.2, build 由 PNG 生成 RGB565 raw) ----
_SPK_SZ = 80                # 帧尺寸 (px), build 取 src/icons 下 80x80 PNG
_SPK_X = (W - _SPK_SZ) // 2  # 水平居中
_SPK_Y = 128 - _SPK_SZ // 2  # 声波区中轴垂直居中
_SPK_SEQ = (0, 1, 2, 3, 2, 1)  # 往返轮播序列 (声波 0→3→0, 按已播秒数切换)
_spk_cache = {}             # 帧号 -> bytearray / False (raw 缺失免重复读)

# ---- SD 落盘下载帧动画 (v1.9.2, 与喇叭帧同槽位同尺寸) ----
_DL_SEQ = (0, 1, 2, 1)      # 往返轮播序列 (箭头 0→2→0 档高度)
_DL_FRAME_MS = 350          # 轮播间隔 (时间驱动, 与网络块到达节奏解耦)
_dl_cache = {}              # 帧号 -> bytearray / False (raw 缺失免重复读)

# ===== 运行状态 (仅 RAM) =====
_last_min = -1             # 上次检查过的当日分钟序号 (hh*60+mm)
_fired_key = ""            # "日-时" 同一整点档防重指纹
_manual_req = False        # Web/模式手动播报请求标志
_playing = False           # 正在播报 (播报页处于接管状态)
_last = {                  # 最近一次播报结果 (status() 供 Web 展示)
    "ok": False,
    "msg": "尚未播报",
    "hour_label": "",
    "items": [],
    "source_name": "",
    "trigger": "",         # hourly / manual
    "ts": "",              # 本地时刻 "HH:MM"
}


def _get_rtc():
    global _RTC
    if _RTC is None:
        _RTC = RTC()
    return _RTC


def _get_hz24():
    global _HZ24
    if _HZ24 is None:
        try:
            from font import hanzi_24 as f
            _HZ24 = f
        except ImportError:
            _HZ24 = False
    return _HZ24


def _get_text_font():
    """新闻正文/标题字体 (v1.8 起 hanzi_24 全量 GB2312+ASCII)。

    历史上 hanzi24 (约千字) 优先于 hz_24x24 (369 字) 以减少缺字;
    hanzi_24 全量覆盖后两者统一, 保留双函数结构仅为最小改动。
    """
    global _TXT_FONT
    if _TXT_FONT is None:
        try:
            from font import hanzi_24 as f
            _TXT_FONT = f
        except ImportError:
            _TXT_FONT = False
    return _TXT_FONT or _get_hz24()


def _read_time():
    """(hh, mm, ss, 日), RTC 异常时 ss=-1 保证不会误触发"""
    try:
        dt = _get_rtc().datetime()
        return dt[4], dt[5], dt[6], dt[2]
    except Exception:
        return 0, 0, -1, 1


def request_play():
    """请求立即播报一次 (Web /api/news/play 调用)。

    非阻塞: 仅置标志, 由主循环下一帧 check() 拉起播报接管。
    """
    global _manual_req
    if _playing:
        return False
    _manual_req = True
    return True


def play_now(display, buttons, buzzer):
    """立即阻塞播报一次 (新闻模式 B 键调用)。

    与 request_play() 的区别: 在调用处直接进入播报接管页。
    返回 False = 已在播报中。
    """
    if _playing:
        return False
    _broadcast(display, buttons, buzzer, "manual")
    return True


def fetch_latest():
    """拉取当档 news_hourly payload (新闻模式浏览用)。

    返回 (payload, err_text), payload 为 None 时 err_text 可展示。
    """
    return _fetch_payload()


def preview():
    """拉取当档 news_hourly 并解析媒体完整 URL (Web /api/news/preview 用)。

    与 fetch_latest 的区别: 面向 API 体检 —— 附带标题列表、时长、
    来源与拼接好的 wav/script 完整 URL (平台媒体无鉴权, 浏览器可
    直接试听/查看), 供用户在网页核对数据源是否正常。
    返回 dict: ok / msg / hour_label / items / source_name /
    duration_est_sec / wav_url / script_url。
    """
    payload, err = _fetch_payload()
    if payload is None:
        return {"ok": False, "msg": err}
    items = payload.get("items")
    titles = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("title"):
                titles.append(str(it.get("title"))[:30])
    try:
        total_sec = int(payload.get("duration_est_sec", 0))
    except (ValueError, TypeError):
        total_sec = 0
    audio_url, audio_fmt = _resolve_audio_url(payload)
    return {
        "ok": True,
        "msg": "",
        "hour_label": str(payload.get("hour_label", "")),
        "items": titles,
        "source_name": str(payload.get("source_name", "")).strip(),
        "duration_est_sec": total_sec,
        "wav_url": audio_url,
        "audio_fmt": audio_fmt,
        "script_url": _media_url(payload.get("script_url")),
    }


def is_playing():
    """播报页是否处于接管状态"""
    return _playing


def status():
    """最近一次播报结果 (不发起网络请求), 供 Web /api/news/status"""
    out = dict(_last)
    out["playing"] = _playing
    return out


def sd_mounted():
    """SD 卡是否已挂载 (回听页空态文案区分"未挂载/暂无存档"用)"""
    try:
        from framework import sdcard
        return sdcard.mounted()
    except Exception:
        return False


def replay_list():
    """今日 SD 存档时段列表 (升序, 供新闻模式"今日回听"页)。

    SD 未挂载 / RTC 日期无效返回空表; 同时顺带清理非当日存档
    (保留一天)。返回 [9, 10, ...] 小时整数列表。
    """
    today = _sd_ready()
    if not today:
        return []
    hours = []
    try:
        import os
        prefix = "news_" + today + "_"
        for fn in os.listdir(_SD_DIR):
            if fn.startswith(prefix) \
                    and (fn.endswith(".wav") or fn.endswith(".mp3")):
                try:
                    hours.append(int(fn[len(prefix):-4]))
                except ValueError:
                    pass
        hours.sort()
    except Exception as e:
        print("[NEWS] replay_list error:", e)
        return []
    return hours


def replay(hour, display, buttons):
    """回听今日 SD 存档 (新闻模式回听页 B 键调用, 阻塞接管)。

    复用播报接管页 UI (红横幅+喇叭帧动画+进度条+已播时长), 档期
    显示所选时段; 任意键停止。返回 True=播完, False=未播/被打断/
    文件缺失。
    """
    global _playing
    if _playing:
        return False
    _playing = True
    import gc
    d = display
    hz = _get_hz24()
    label = "%d点档" % hour
    try:
        gc.collect()
        today = _sd_ready()
        path = _sd_find(today, hour) if today else ""
        buttons.clear_events()
        if not path:
            _draw_frame(d, hz, label, "存档缺失", CLR_RED)
            _wait_quiet(buttons, 1200)
            return False
        _draw_frame(d, hz, label, "正在回听", CLR_GREEN)
        try:
            import os
            bps = _MP3_BPS if path.endswith(".mp3") else _WAV_BPS
            total_sec = os.stat(path)[6] // bps
        except Exception:
            total_sec = 0
        state, _sec = _play_file(
            path, _make_spk_prog(d, total_sec), lambda: _any_key(buttons))
        if state == "cancelled":
            return False
        if state == "ok":
            _summary_page(d, hz, label, "回听完成", buttons)
            return True
        _draw_frame(d, hz, label, "回听失败", CLR_RED)
        _wait_quiet(buttons, 1200)
        return False
    finally:
        _playing = False
        gc.collect()


def _record(ok, msg, hour_label="", titles=None, source_name="",
            trigger=""):
    """写入最近播报结果"""
    global _last
    hh, mm = _read_time()[0], _read_time()[1]
    _last = {"ok": bool(ok), "msg": msg,
             "hour_label": str(hour_label or ""),
             "items": titles[:10] if titles else [],
             "source_name": str(source_name or ""),
             "trigger": trigger, "ts": "%02d:%02d" % (hh, mm)}


def _media_url(path):
    """payload 站内相对路径 -> 完整 URL (拼平台 endpoint)"""
    p = str(path or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p
    try:
        from framework.settings import get_cloud
        base = str(get_cloud().get("endpoint", "")).rstrip("/")
    except Exception:
        base = ""
    if not base:
        return ""
    return base + (p if p.startswith("/") else "/" + p)


def _resolve_audio_url(payload):
    """确定当档音频地址与格式 (v2.3)。

    解析优先级 (mp3 直连仅在固件带解码器时生效):
      1. audio_wav_url        (兼容字段, 官方当前未下发; 平台显式
                               指定 WAV 时尊重之)
      2. audio_url 以 .wav 结尾 (官方现状, 直接使用)
      3. audio_url 以 .mp3 结尾 -> MP3 直连 (流量仅 WAV 的 1/8~1/24;
                               旧固件无解码器时跳过此级)
      4. script_url 同目录 latest.wav (latest.* 滚动覆盖约定推断)
      5. audio_url 的 .mp3 -> .wav (旧固件兜底, 行为与 v1.9 一致)
    返回 (url, "wav"/"mp3"), 解析不出返回 ("", "")。
    推断/下载失败按文字降级处理, 串口 [NEWS]/[AUDIO] 可诊断。
    """
    url = _media_url(payload.get("audio_wav_url"))
    if url:
        return url, "wav"
    audio = str(payload.get("audio_url") or "").strip()
    abs_audio = _media_url(audio)
    if abs_audio.endswith(".wav"):
        return abs_audio, "wav"
    if abs_audio.endswith(".mp3"):
        try:
            from framework import audio as _audio
            if _audio.mp3_supported():
                return abs_audio, "mp3"
        except Exception:
            pass
    probe = str(payload.get("script_url") or "").strip()
    abs_probe = _media_url(probe)
    if abs_probe:
        return abs_probe.rsplit("/", 1)[0] + "/latest.wav", "wav"
    if abs_audio.endswith(".mp3"):
        return abs_audio[:-4] + ".wav", "wav"
    return "", ""


def _fetch_payload():
    """拉取当档 news_hourly。返回 (payload, err_text)"""
    import gc
    gc.collect()
    try:
        from framework.cloud import fetch_json
        code, data = fetch_json(SOURCE_CODE)
    except Exception as e:
        print("[NEWS] fetch error:", e)
        return None, "云服务异常"
    if code is None:
        return None, "网络失败"
    if code == 40302:
        return None, "未订阅该数据源"
    if code != 0 or not isinstance(data, dict):
        return None, "错误码 %s" % code
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None, "暂无数据"
    return payload, ""


def _download_text(url):
    """下载播报稿文本 (UTF-8)。返回 (text, err_text)"""
    import gc
    if not url:
        return "", "无链接"
    gc.collect()
    r = None
    try:
        try:
            import urequests as requests
        except ImportError:
            import requests
        r = requests.get(url, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            return "", "HTTP %s" % r.status_code
        raw = r.raw.read(_SCRIPT_MAX)
        try:
            return str(raw, "utf-8"), ""
        except Exception:
            return "", "编码错误"
    except Exception as e:
        print("[NEWS] script download error:", e)
        return "", "下载失败"
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()


def _msg_lines(text, chars_per_line, max_lines):
    """播报稿整段拆页: 按行宽断行, 每页最多 max_lines 行"""
    pages = []
    cur = []
    line = ""
    for ch in text:
        if ch == "\n":
            ch = " "
        if ch == " " and not line:
            continue
        line += ch
        if len(line) >= chars_per_line:
            cur.append(line)
            line = ""
            if len(cur) >= max_lines:
                pages.append(cur)
                cur = []
    if line:
        cur.append(line)
    if cur:
        pages.append(cur)
    return pages


# ================= 播报页绘制 (v1.6 新闻红主题) =================
# 布局 (240x240):
#   y 0-27   红色横幅: 闪烁直播点 + 整点新闻 + 右侧当档档期
#   y 30-61  状态行: 左状态文字 (hz24) + 右已播时长 MM:SS (vga)
#   y 68-187 喇叭帧动画区 (80x80 居中, 声波 0→3→0 档轮播)
#   y 202    进度条 (琥珀色填充)
#   y H-12   B:STOP 按键提示

def _write_len(d, font, s):
    """测量比例字库文本宽度 (write_len 不可用时按 MAX_WIDTH 估算)"""
    try:
        return d.write_len(font, s)
    except Exception:
        return len(s) * getattr(font, "MAX_WIDTH", 24)


def _draw_header(d, hz, hour_label):
    """红色新闻横幅: 直播点 + 标题 + 右侧档期 (dot 默认点亮)"""
    d.fill_rect(0, 0, W, 28, CLR_RED)
    d.fill_rect(10, 11, 7, 7, CLR_WHITE)
    if hz:
        d.write(hz, "整点新闻", 24, 2, CLR_WHITE, CLR_RED)
        if hour_label:
            hw = _write_len(d, hz, hour_label)
            d.write(hz, hour_label, W - 8 - hw, 2, CLR_AMBER, CLR_RED)


def _draw_live_dot(d, on):
    """横幅直播点闪烁 (只重绘 7x7 点区, 供播报回调逐块调用)"""
    d.fill_rect(10, 11, 7, 7, CLR_WHITE if on else CLR_RED)


def _draw_status(d, hz, state_text, state_color):
    """状态行: 左状态文字, 右侧留给已播时长"""
    d.fill_rect(0, 30, W, 32, CLR_BG)
    if hz and state_text:
        d.write(hz, state_text, 8, 34, state_color, CLR_BG)


def _draw_elapsed(d, sec):
    """状态行右侧已播时长 MM:SS (sec<0 时清除)"""
    try:
        from font import vga1_8x8 as sf
    except ImportError:
        return
    d.fill_rect(184, 44, 48, 14, CLR_BG)
    if sec >= 0:
        d.text(sf, "%02d:%02d" % (sec // 60, sec % 60),
               192, 48, CLR_GRAY, CLR_BG)


def _spk_frame(d, idx):
    """blit 单帧喇叭动画 (raw 80x80 大端 RGB565, build 由 PNG 生成)。

    raw 首次使用时读入并常驻缓存 (4 帧共 80KB, PSRAM 堆可容纳);
    缺失时置 False 免反复读文件, 页面该区域保持纯黑 (安全降级)。
    """
    buf = _spk_cache.get(idx)
    if buf is None:
        buf = False
        try:
            import os
            path = "/icons/news_spk_%d.raw" % idx
            os.stat(path)
            f = open(path, "rb")
            try:
                data = f.read()
            finally:
                f.close()
            if len(data) == _SPK_SZ * _SPK_SZ * 2:
                buf = bytearray(data)
        except Exception:
            buf = False
        _spk_cache[idx] = buf
    if buf:
        d.blit_buffer(memoryview(buf), _SPK_X, _SPK_Y, _SPK_SZ, _SPK_SZ)


def _spk_idle(d, idx=2):
    """静态喇叭帧 (拉取/准备/失败等阻塞阶段的一次性画面)"""
    d.fill_rect(0, 68, W, 120, CLR_BG)
    _spk_frame(d, idx)


def _dl_frame(d, idx):
    """blit 单帧下载动画 (raw 80x80 大端 RGB565, build 由 PNG 生成)。

    SD 落盘阶段替代喇叭帧, 明示"正在缓存"而非"正在播放" (v1.9.2);
    raw 缓存与缺失降级策略同 _spk_frame (缺失时该区域保持纯黑)。
    """
    buf = _dl_cache.get(idx)
    if buf is None:
        buf = False
        try:
            import os
            path = "/icons/news_dl_%d.raw" % idx
            os.stat(path)
            f = open(path, "rb")
            try:
                data = f.read()
            finally:
                f.close()
            if len(data) == _SPK_SZ * _SPK_SZ * 2:
                buf = bytearray(data)
        except Exception:
            buf = False
        _dl_cache[idx] = buf
    if buf:
        d.blit_buffer(memoryview(buf), _SPK_X, _SPK_Y, _SPK_SZ, _SPK_SZ)


def _dl_idle(d, idx=0):
    """静态下载帧 (SD 落盘首块到达时的一次性画面)"""
    d.fill_rect(0, 68, W, 120, CLR_BG)
    _dl_frame(d, idx)


def _draw_hint(d):
    """底部按键提示"""
    try:
        from font import vga1_8x8 as sf
        d.text(sf, "B:STOP", W - 6 * 8 - 8, H - 12, CLR_GRAY, CLR_BG)
    except ImportError:
        pass


def _draw_frame(d, hz, hour_label, state_text, state_color, dl=False):
    """播报页整页静态框 (v1.6): 红横幅 + 状态行 + 静态图标 + 提示。

    dl=True 显示静态下载帧 (SD 落盘页, v1.9.2), 默认静态喇叭帧;
    播报中的动态喇叭帧 / 下载中的动态帧由各自 blit 逐帧覆盖同一区域。
    """
    d.fill(CLR_BG)
    _draw_header(d, hz, hour_label)
    _draw_status(d, hz, state_text, state_color)
    if dl:
        _dl_idle(d)
    else:
        _spk_idle(d)
    _draw_hint(d)


def _clear_content(d):
    """清空内容区 (状态行以下、进度条以上, 文字降级/失败页用)"""
    d.fill_rect(0, 62, W, H - 62 - 34, CLR_BG)


def _draw_progress(d, sec, total):
    """进度条 (琥珀色填充, 播报进度/文字翻页共用)"""
    if total > 0:
        frac = sec / total
        if frac > 1:
            frac = 1
        w = int((W - 32) * frac)
        d.fill_rect(16, 202, W - 32, 5, CLR_DIM)
        if w > 0:
            d.fill_rect(16, 202, w, 5, CLR_AMBER)


def _make_spk_prog(d, total_sec):
    """播报/回听共用的播放动态 UI 回调 (v1.9 自 _broadcast 抽出)。

    按已播秒数轮播喇叭帧 (1s/帧) + 进度条 + 已播时长 + 直播点闪烁;
    变化才重绘对应区域。返回 _prog(sec) 单参回调。
    """
    _shown = {"sec": -1, "f": -1}

    def _prog(sec):
        now = time.ticks_ms()
        f = _SPK_SEQ[sec % 6]        # 按已播秒数轮播 (1s/帧)
        if f != _shown["f"]:
            _shown["f"] = f
            _spk_frame(d, f)
        _draw_progress(d, sec, total_sec)
        if sec != _shown["sec"]:
            _shown["sec"] = sec
            _draw_elapsed(d, sec)
        _draw_live_dot(d, ((now // 500) & 1) == 0)

    return _prog


def _any_key(buttons):
    """是否有任意按键事件"""
    up, sel, down = buttons.scan()
    return bool(up or sel or down)


def _wait_quiet(buttons, ms):
    """静默等待 ms 毫秒, 期间任意键可提前结束。

    返回 True = 等待期间被按键打断; False = 等满时长自然结束。
    """
    buttons.clear_events()
    end = time.ticks_add(time.ticks_ms(), ms)
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        if _any_key(buttons):
            return True
        time.sleep_ms(30)
    return False


# ============ SD 卡存档 (v1.9, /sd/music/, 保留一天) ============

def _today_str():
    """本地日期 "YYYYMMDD"; RTC 异常/未同步返回 "" (SD 存档退避)"""
    try:
        dt = _get_rtc().datetime()
        if dt[0] < 2020:
            return ""
        return "%04d%02d%02d" % (dt[0], dt[1], dt[2])
    except Exception:
        return ""


def _hour_num(hour_label, fallback):
    """档期标签转小时数: "15点" -> 15; 解析失败回退本地小时"""
    s = str(hour_label or "").strip()
    if s.endswith("点"):
        try:
            h = int(s[:-1])
            if 0 <= h <= 23:
                return h
        except ValueError:
            pass
    return fallback


def _sd_path(date_str, hour, ext="wav"):
    """今日存档路径 /sd/music/news_YYYYMMDD_HH.<ext> (ext: wav/mp3)"""
    return "%s/news_%s_%02d.%s" % (_SD_DIR, date_str, hour, ext)


def _sd_find(date_str, hour):
    """今日存档实际路径 (mp3 优先, 无则 wav); 都缺失返回 \"\""""
    import os
    for ext in ("mp3", "wav"):
        p = _sd_path(date_str, hour, ext)
        try:
            os.stat(p)
            return p
        except OSError:
            pass
    return ""


def _sd_ensure_dir():
    """确保 /sd/music 存在 (已存在忽略)"""
    import os
    try:
        os.mkdir(_SD_DIR)
    except OSError:
        pass


def _sd_cleanup(today):
    """删除非当日 news_*.wav/.mp3 存档 (保留一天); 异常静默"""
    import os
    try:
        for fn in os.listdir(_SD_DIR):
            if fn.startswith("news_") \
                    and (fn.endswith(".wav") or fn.endswith(".mp3")):
                if fn[5:13] != today:
                    os.remove(_SD_DIR + "/" + fn)
    except Exception:
        pass


def _sd_ready():
    """SD 存档可用性检查: 卡已挂载且 RTC 日期有效。

    可用时顺带确保目录存在并清理非当日存档, 返回今日日期串;
    不可用返回 "" (调用方回退在线流式/隐藏回听列表)。
    """
    try:
        from framework import sdcard
        if not sdcard.mounted():
            return ""
    except Exception:
        return ""
    today = _today_str()
    if not today:
        return ""
    try:
        _sd_ensure_dir()
        _sd_cleanup(today)
    except Exception:
        pass
    return today


def _download_to_sd(url, path, on_dl, cancelled):
    """把音频 (WAV/MP3 通用) 分块落盘到 SD (不整档进 RAM, 下载进度经 on_dl 上报)。

    返回 True=完整落盘; False=下载/写卡失败 (调用方回退在线流播);
    "cancelled"=落盘中被按键打断 (不回退不播放)。半截文件一律删除。
    """
    if cancelled and cancelled():
        return "cancelled"
    r = None
    f = None
    done = False
    try:
        try:
            import urequests as requests
        except ImportError:
            import requests
        r = requests.get(url, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            print("[NEWS] sd audio http:", r.status_code)
            return False
        total = 0
        try:
            for k in r.headers:
                if str(k).lower() == "content-length":
                    total = int(r.headers[k])
                    break
        except Exception:
            total = 0
        f = open(path, "wb")
        got = 0
        while True:
            if cancelled and cancelled():
                return "cancelled"
            chunk = r.raw.read(_SD_DL_CHUNK)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if on_dl:
                on_dl(got, total)
        done = True
        return True
    except Exception as e:
        print("[NEWS] sd download error:", e)
        return False
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        if not done:
            try:
                import os
                os.remove(path)
            except Exception:
                pass
        import gc
        gc.collect()


def _play_file(path, on_prog, cancelled):
    """从 SD 文件播放音频 (落盘播报复用/回听共用), 按扩展名分派解码。

    返回 (state, played_sec), 语义同 _stream_audio。
    """
    import gc
    gc.collect()
    try:
        from framework import audio
        from framework.settings import get_news
        vol = int(get_news().get("volume", 80))
        holder = {"sec": 0}

        def _prog(sec):
            holder["sec"] = sec
            on_prog(sec)

        play = audio.play_mp3 if path.endswith(".mp3") else audio.play_wav
        f = open(path, "rb")
        try:
            ok, err = play(f.read, volume=vol,
                           is_cancelled=cancelled,
                           on_progress=_prog)
        finally:
            f.close()
        if err == "cancelled":
            return ("cancelled", holder["sec"])
        if ok:
            return ("ok", holder["sec"])
        print("[NEWS] audio file play fail:", err)
        return ("fail", holder["sec"])
    except Exception as e:
        print("[NEWS] audio file error:", e)
        return ("fail", 0)


def _play_audio(audio_url, fmt, hour_label, on_prog, on_dl, on_play_start,
                cancelled):
    """当档音频播放编排 (v2.3): SD 挂载则落盘后播, 否则在线流播。

    - 今日该档文件已存在: 免下载直接播 (同档重复播报秒开)
    - 落盘成功: 从文件播 (下载进度经 on_dl(已收字节, 总字节))
    - 下载被打断: 返回 ("cancelled", 0), 不回退
    - 下载/文件播放失败: 经 on_play_start() 重绘后回退 _stream_audio
    返回 (state, played_sec), 语义同 _stream_audio。
    """
    import gc
    try:
        from framework import audio
        if not audio.available():
            print("[NEWS] audio unavailable: i2s not available")
            return ("fail", 0)
    except Exception as e:
        print("[NEWS] audio import error:", e)
        return ("fail", 0)
    gc.collect()
    today = _sd_ready()
    if not today:
        return _stream_audio(audio_url, fmt, on_prog, cancelled)
    hh = _read_time()[0]
    path = _sd_path(today, _hour_num(hour_label, hh), fmt)
    exists = False
    try:
        import os
        os.stat(path)
        exists = True
    except OSError:
        pass
    if exists:
        if on_play_start:
            on_play_start()
        return _play_file(path, on_prog, cancelled)
    dl = _download_to_sd(audio_url, path, on_dl, cancelled)
    if dl == "cancelled":
        return ("cancelled", 0)
    if dl:
        if on_play_start:
            on_play_start()
        state = _play_file(path, on_prog, cancelled)
        if state[0] != "fail":
            return state
        # 落盘文件播放异常 (罕见, 如写卡坏块): 回退在线流播自愈
        print("[NEWS] sd file play fail, fallback stream")
    if on_play_start:
        on_play_start()
    return _stream_audio(audio_url, fmt, on_prog, cancelled)


def _stream_audio(url, fmt, on_progress, cancelled):
    """下载并流式播放音频 (fmt: "wav"/"mp3")。

    返回 (state, played_sec): state 为
      "ok"        播放完毕
      "cancelled" 播放中被按键打断
      "fail"      下载失败/解析失败/I2S 不可用
    """
    import gc
    if not url:
        return ("fail", 0)
    try:
        from framework import audio
        if not audio.available():
            print("[NEWS] audio unavailable: i2s not available")
            return ("fail", 0)
    except Exception as e:
        print("[NEWS] audio import error:", e)
        return ("fail", 0)
    gc.collect()
    r = None
    try:
        try:
            import urequests as requests
        except ImportError:
            import requests
        r = requests.get(url, timeout=_FETCH_TIMEOUT)
        if r.status_code != 200:
            print("[NEWS] audio http:", r.status_code)
            return ("fail", 0)
        from framework.settings import get_news
        vol = int(get_news().get("volume", 80))
        holder = {"sec": 0}

        def _prog(sec):
            holder["sec"] = sec
            on_progress(sec)

        play = audio.play_mp3 if fmt == "mp3" else audio.play_wav
        ok, err = play(r.raw.read, volume=vol,
                       is_cancelled=cancelled,
                       on_progress=_prog)
        if err == "cancelled":
            return ("cancelled", holder["sec"])
        if ok:
            return ("ok", holder["sec"])
        print("[NEWS] audio play fail:", err)
        return ("fail", holder["sec"])
    except Exception as e:
        print("[NEWS] audio stream error:", e)
        return ("fail", 0)
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        import gc
        gc.collect()


def _draw_script_page(d, font, lines):
    """绘制一页播报稿文字 (仅文字降级模式使用; 正文字体, 最多 5 行)"""
    _clear_content(d)
    y = 62
    if font:
        for ln in lines:
            d.write(font, ln, 8, y, CLR_WHITE, CLR_BG)
            y += 28


def _text_mode(d, hz, hour_label, script, buttons, buzzer):
    """纯文字播报降级: 播报稿分页自动滚动, 任意键停止。

    正文用 hanzi24 (约千字覆盖, v1.5.1), 行宽按字体 MAX_WIDTH 动态算。
    """
    if buzzer:
        try:
            buzzer.tone(_FALLBACK_BEEP_HZ, 80)
        except Exception:
            pass
    font = _get_text_font()
    cpl = max(1, (W - 16) // getattr(font, "MAX_WIDTH", 24)) if font else 9
    pages = _msg_lines(script, cpl, 5)
    _draw_frame(d, hz, hour_label, "文字播报", CLR_AMBER)
    if not pages:
        return
    idx = 0
    buttons.clear_events()
    while True:
        _draw_script_page(d, font, pages[idx])
        _draw_progress(d, idx + 1, len(pages))
        end = time.ticks_add(time.ticks_ms(), _SCRIPT_PAGE_MS)
        stop = False
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            if _any_key(buttons):
                stop = True
                break
            time.sleep_ms(30)
        if stop:
            return
        idx += 1
        if idx >= len(pages):
            return


def _summary_page(d, hz, hour_label, msg, buttons):
    """播报收尾页: 仅状态文字 + 满格进度条 (v1.5.1 起不显示标题内容),
    停留约 1.2s (任意键可跳过)"""
    _draw_frame(d, hz, hour_label, msg, CLR_GREEN)
    _draw_progress(d, 1, 1)
    _wait_quiet(buttons, 1200)


def _broadcast(display, buttons, buzzer, trigger):
    """阻塞式播报接管页 (仿 alarm._ring):

    取数 -> 流播 WAV (喇叭帧动画 + 进度条 + 已播时长, 可打断)
    -> (失败降级纯文字翻页) -> 收尾页。
    播报全程不显示新闻文字内容 (v1.5.1 确立, v1.6 维持);
    结果写入 _last 供 Web 展示。
    """
    global _playing, _manual_req
    import gc
    _playing = True
    _manual_req = False
    d = display
    hz = _get_hz24()
    hour_label = ""
    titles = []
    source_name = ""
    try:
        gc.collect()
        buttons.clear_events()
        _draw_frame(d, hz, "", "正在拉取", CLR_GRAY)

        payload, err = _fetch_payload()
        if payload is None and err in ("网络失败", "云服务异常"):
            # 网络类失败: 等 10s 重试一次 (整点档错过要等下小时;
            # 业务类错误 10s 后不会变, 不重试)。任意键提前放弃。
            print("[NEWS] fetch failed (%s), retry once in 10s" % err)
            _draw_frame(d, hz, "", err, CLR_RED)
            _clear_content(d)
            if hz:
                d.write(hz, "10秒后重试", (W - 5 * 24) // 2, 118,
                        CLR_AMBER, CLR_BG)
            if _wait_quiet(buttons, _FETCH_RETRY_MS):
                _record(False, "已停止", trigger=trigger)
                return
            payload, err = _fetch_payload()
        if payload is None:
            _draw_frame(d, hz, "", err, CLR_RED)
            _clear_content(d)
            if hz:
                d.write(hz, "播报失败", (W - 4 * 24) // 2, 118,
                        CLR_RED, CLR_BG)
            if buzzer:
                try:
                    buzzer.tone(_FALLBACK_BEEP_HZ, 100)
                except Exception:
                    pass
            _record(False, err, trigger=trigger)
            _wait_quiet(buttons, 2000)
            return

        hour_label = str(payload.get("hour_label", ""))
        items = payload.get("items")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("title"):
                    titles.append(str(it.get("title"))[:30])
        source_name = str(payload.get("source_name", "")).strip()
        try:
            total_sec = int(payload.get("duration_est_sec", 0))
        except (ValueError, TypeError):
            total_sec = 0

        _draw_frame(d, hz, hour_label, "正在准备", CLR_GRAY)
        script, err = _download_text(_media_url(payload.get("script_url")))
        if buzzer:
            try:
                buzzer.tone(1200, 30)
            except Exception:
                pass
        if _any_key(buttons):
            _record(False, "已停止", hour_label, titles, source_name,
                    trigger)
            return

        # ---- 音频播放 (可打断): 喇叭帧动画, 不显示文字 ----
        audio_url, audio_fmt = _resolve_audio_url(payload)
        state, played_sec = "fail", 0
        if audio_url:
            print("[NEWS] audio(%s):" % audio_fmt, audio_url)
            _draw_frame(d, hz, hour_label, "正在播报", CLR_GREEN)

            def _start_play():
                # 落盘->播放切换 / SD 路径回退流播时重绘, 修正状态行
                _draw_frame(d, hz, hour_label, "正在播报", CLR_GREEN)

            _prog = _make_spk_prog(d, total_sec)
            _dl_on = {"on": False, "f": -1}

            def _dl(got, total):
                # SD 落盘阶段: 首块整绘"SD缓存中"页 (下载图标, v1.9.2 起不
                # 用喇叭帧以免误读为播放中), 之后按时间轮播下载帧+刷进度条
                if not _dl_on["on"]:
                    _dl_on["on"] = True
                    _draw_frame(d, hz, hour_label, "SD缓存中", CLR_AMBER,
                                dl=True)
                    _dl_on["f"] = 0
                f = _DL_SEQ[(time.ticks_ms() // _DL_FRAME_MS)
                            % len(_DL_SEQ)]
                if f != _dl_on["f"]:
                    _dl_on["f"] = f
                    _dl_frame(d, f)
                _draw_progress(d, got // 1024, total // 1024)

            state, played_sec = _play_audio(
                audio_url, audio_fmt, hour_label, _prog, _dl, _start_play,
                lambda: _any_key(buttons))
        if state == "cancelled":
            _record(False, "已停止", hour_label, titles, source_name,
                    trigger)
            return

        # ---- 音频完成 -> 收尾页; 失败/不可用 -> 文字降级 ----
        if state == "ok":
            _record(True, "播报完成", hour_label, titles, source_name,
                    trigger)
            _summary_page(d, hz, hour_label, "播报完成", buttons)
            return

        if audio_url:
            # 音频在线但下载/播放失败 -> 提示后走文字降级
            print("[NEWS] audio unavailable (state=%s), fallback text"
                  % state)
            _draw_frame(d, hz, hour_label, "音频不可用, 文字播报",
                        CLR_AMBER)
            if buzzer:
                try:
                    buzzer.tone(_FALLBACK_BEEP_HZ, 100)
                except Exception:
                    pass
            _wait_quiet(buttons, 1200)
            if _any_key(buttons):
                _record(False, "已停止", hour_label, titles, source_name,
                        trigger)
                return
        if not script:
            _draw_frame(d, hz, hour_label, "播报稿不可用", CLR_RED)
            if buzzer:
                try:
                    buzzer.tone(_FALLBACK_BEEP_HZ, 100)
                except Exception:
                    pass
            _record(False, "播报稿不可用", hour_label, titles,
                    source_name, trigger)
            _wait_quiet(buttons, 2000)
            return
        _text_mode(d, hz, hour_label, script, buttons, buzzer)
        if _any_key(buttons):
            _record(False, "已停止", hour_label, titles, source_name,
                    trigger)
            return
        _record(True, "文字播报完成", hour_label, titles, source_name,
                trigger)
        _summary_page(d, hz, hour_label, "文字播报完成", buttons)
    except Exception as e:
        print("[NEWS] broadcast error:", repr(e))
        _record(False, "播报异常", trigger=trigger)
    finally:
        _playing = False
        gc.collect()


def check(display, buttons, buzzer):
    """每帧调用 (BaseMode.run / 主菜单循环): 分钟沿检查整点播报。

    - 手动播报优先: request_play() 置标志后下一帧立即触发
      (无视开关/时段), 供 Web /api/news/play 与新闻模式使用
    - 自动播报: settings.news.enabled 且整点档在 hours 白名单内,
      分钟沿触发 + 5 分钟回看补漏; "日-时" 指纹防止回看窗口重复播
    - 命中即接管屏幕进入播报页 (阻塞至播完/停止), 返回 True 表示
      触发过, 宿主模式应自行 enter() 整页重绘恢复
    排障日志 (串口可见): [NEWS] fire ... / [NEWS] wav ...
    """
    global _last_min, _fired_key
    if _manual_req and not _playing:
        print("[NEWS] fire manual")
        _broadcast(display, buttons, buzzer, "manual")
        return True
    hh, mm, ss, dom = _read_time()
    if ss < 0:
        return False            # RTC 异常, 不误触发
    cur = hh * 60 + mm
    if cur == _last_min:
        return False
    prev = _last_min
    _last_min = cur
    try:
        from framework.settings import get_news
        cfg = get_news()
    except Exception as e:
        print("[NEWS] load config error:", e)
        return False
    if not cfg.get("enabled"):
        return False
    hours = cfg.get("hours") or []
    # 待匹配分钟区间: 当前分钟 + 回看补漏 (跨零点/首帧不回看)
    if prev < 0 or cur <= prev:
        span = [cur]
    else:
        lo = prev + 1
        if lo < cur - 5:
            lo = cur - 5
        span = list(range(lo, cur + 1))
    for m in span:
        if m % 60 != 0:
            continue
        h = m // 60
        if h not in hours:
            continue
        key = "%d-%d" % (dom, h)
        if key == _fired_key:
            continue
        _fired_key = key
        print("[NEWS] fire hourly %02d:00" % h)
        _broadcast(display, buttons, buzzer, "hourly")
        return True
    return False
