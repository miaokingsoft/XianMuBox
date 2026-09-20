"""
XianMuBox - 听老故事服务层 (comic_audio, v1.2)

闲沐盒子数据服务平台 comic_audio 连环画语音接入 (官方手册 2.9):
平台把闲沐连环画 (wwzu.com) 中"语音完整"的小人书按页合成 MP3, 设备按
「标签 -> 标签下故事 -> 整本播放清单」三级浏览, 顺序流式播放整本。
服务端收到清单请求时已备货前 3 条 (起播零等待), 其余按播放顺序后台
备货; 个别条目 404 = 仍在备货, 设备短退避重试即可。

数据通道 (framework/cloud 统一取数, 自动携带设备 SN/Token 鉴权与
40102 重激活; 平台侧按参数分缓存 TTL 3600s, 设备端不做长缓存):
  GET /api/v1/data/comic_audio?action=tags|stories|playlist|pages
    tags      标签列表 (10 个/页, 按收录量降序)
    stories   标签下故事 (11 本/页, sort=hits 热度 / new 最新;
              仅收录语音完整的书, 内容提要/封底不合成不算缺失;
              2026-09-19 契约改版: 不再返回数字 id, 改 htmlname)
    playlist  整本播放清单 (base_url + files 紧凑编码, 128 条/页,
              more=true 时翻页; story_id 参数传 htmlname; 2026-09-19
              契约改版: 无梗概前置条, 文件名为页号顺序名 001.mp3...
              而非源站真实文件名; 源站地址不下发, base_url 为站内
              相对路径)
    pages     页图清单 (story_id 传 htmlname, 带屏设备按需另取,
              v1 纯音频未使用)
  音频  GET <endpoint><base_url><文件名> (无鉴权 /media 静态中转,
        带 Content-Length; 磁盘缓存上限 2GB LRU)
  本地  /story_tags.json 标签第 1 批缓存 (闪存根, ~600B, 原子写;
        24h TTL, 进入秒出列表, 陈旧静默刷新, 失败静默沿用;
        v1.2 新增 tags_cached()/tags_cache_save())

错误与退避 (官方手册): 40309/40302 未订阅 (该付费源唯一开通途径 =
平台门户邀请码兑换, 设备只提示不自动重试); 40001 参数错误;
50401 故事不存在/无音频/上游库不可达 (按数字 id 请求旧契约也会得
50401); 42901 限流按 retry_after; 网络类失败指数退避 (节奏由 UI 层
控制)。

播放复用 framework/audio.play_mp3 流式契约 (read_fn 供数 +
is_cancelled 逐帧打断 + on_progress 每秒回调), 与 radio.play_segment
同款写法; 固件需含 mp3dec 模块 (mp129-romfont-mp3dec 起)。

MicroPython 兼容: 无 f-string / time.ticks_* / urequests 惰性 import
"""

import time

SOURCE_CODE = "comic_audio"
_FETCH_TIMEOUT = 10               # 取数/音频下载超时 (秒)
_CACHE_TTL_MS = 10 * 60 * 1000    # 目录类结果 (tags/stories) 缓存时长
_CACHE_MAX = 12                   # 缓存条目上限 (超出整表清空, 防碎片)
_PLAYLIST_PAGE_MAX = 8            # 清单翻页上限保护 (8 页 = 1024 条)

_cache = {}                       # key -> (ticks_ms, 归一化结果 dict)


def _cache_get(key):
    """读缓存 (TTL 外/未命中返回 None)"""
    hit = _cache.get(key)
    if hit is None:
        return None
    ms, val = hit
    if time.ticks_diff(time.ticks_ms(), ms) >= _CACHE_TTL_MS:
        return None
    return val


def _cache_put(key, val):
    """写缓存 (条目超上限整表清空, 目录类数据量小, 简单策略足够)"""
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.ticks_ms(), val)


def media_url(path):
    """站内相对路径 -> 完整 URL (拼平台 endpoint), 与 radio/news 同款"""
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


def _fetch(params):
    """comic_audio 统一取数 (framework/cloud 托管鉴权)。

    返回 (payload, err, wait_sec):
      payload  响应 payload dict (兼容顶层或 payload 字段两种封装),
               失败为 None
      err      "" 或展示文案 ("网络失败"/"云服务异常"/"未订阅"/
               "参数错误"/"数据源异常"/"暂无音频"/"限流中"/"暂无数据"/
               "错误码 N")
      wait_sec err 非空时建议的重试等待秒 (业务类不自动重试为 0);
               网络类的指数放大节奏由 UI 层控制, 此处只给基值
    """
    import gc
    gc.collect()
    try:
        from framework.cloud import fetch_json
        c, data = fetch_json(SOURCE_CODE, params)
    except Exception as e:
        print("[STORY] fetch error:", e)
        return None, "云服务异常", 60
    if c is None:
        return None, "网络失败", 60
    if c != 0:
        # 串口诊断: 打印平台原始业务码与 msg (定位 50401=上游库不可达
        # 还是 40302/40309 未订阅等, 真机排障看 REPL)
        msg = ""
        try:
            from framework import cloud as _cl
            last = getattr(_cl, "_last_err", None)
            if isinstance(last, tuple) and len(last) > 1:
                msg = str(last[1])
        except Exception:
            msg = ""
        print("[STORY] platform code:", c, "msg:", msg)
    if c == 42901:
        try:
            w = int((data or {}).get("retry_after", 60))
        except (ValueError, TypeError):
            w = 60
        return None, "限流中", max(10, w)
    if c in (40309, 40302):
        # 未订阅 (40309=自助开通已关闭): 唯一开通途径 = 平台门户
        # 邀请码兑换, 设备端只提示, 不自动重试
        return None, "未订阅", 0
    if c == 40001:
        return None, "参数错误", 0
    if c == 40401:
        return None, "数据源异常", 0
    if c == 50401:
        # 手册 50401 双语义按动作区分: playlist = 故事不存在/无音频
        # (选书错或该书未备货, 恒候换书); tags/stories = 上游库不可达
        # (平台侧内容库故障, 与音频无关, 文案不误导)
        if str(params.get("action") or "") == "playlist":
            return None, "暂无音频", 0
        return None, "内容库不可达", 0
    if c != 0:
        return None, "错误码 %s" % c, 60
    if not isinstance(data, dict):
        return None, "暂无数据", 30
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = data
    return payload, "", 0


def fetch_tags(page=1):
    """标签列表 (10 个/页, 按收录量降序), RAM 缓存 10 分钟。

    返回 (payload, err, wait_sec):
      payload = {"tags": [{"name","count"}...], "total": 总标签数}
    """
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    key = "tags:%d" % page
    hit = _cache_get(key)
    if hit is not None:
        return hit, "", 0
    payload, err, wait = _fetch({"action": "tags", "page": page})
    if payload is None:
        return None, err, wait
    tags = []
    for t in payload.get("tags") or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        try:
            cnt = int(t.get("count") or 0)
        except (ValueError, TypeError):
            cnt = 0
        tags.append({"name": name, "count": cnt})
    try:
        total = max(len(tags), int(payload.get("total") or 0))
    except (ValueError, TypeError):
        total = len(tags)
    out = {"tags": tags, "total": total}
    if tags:
        _cache_put(key, out)
    return out, "", 0


def fetch_stories(tag, sort="hits", page=1):
    """标签下故事列表 (11 本/页, 仅语音完整的书), RAM 缓存 10 分钟。

    sort: "hits" 热度 (默认) / "new" 最新。
    返回 (payload, err, wait_sec):
      payload = {"stories": [{"htmlname","title","pages"}...],
                 "total": 总本数}
    2026-09-19 契约改版: 不再返回数字 id, 改 htmlname (playlist/pages
    的 story_id 参数即传它)。
    """
    tag = str(tag or "").strip()
    sort = "new" if str(sort or "").strip() == "new" else "hits"
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    if not tag:
        return None, "参数错误", 0
    key = "stories:%s:%s:%d" % (tag, sort, page)
    hit = _cache_get(key)
    if hit is not None:
        return hit, "", 0
    payload, err, wait = _fetch({"action": "stories", "tag": tag,
                                 "sort": sort, "page": page})
    if payload is None:
        return None, err, wait
    stories = []
    seen = set()
    for s in payload.get("stories") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("htmlname") or "").strip()
        title = str(s.get("title") or "").strip()
        if not name or not title or name in seen:
            continue
        seen.add(name)
        try:
            pages = max(0, int(s.get("pages") or 0))
        except (ValueError, TypeError):
            pages = 0
        stories.append({"htmlname": name, "title": title, "pages": pages})
    try:
        total = max(len(stories), int(payload.get("total") or 0))
    except (ValueError, TypeError):
        total = len(stories)
    out = {"stories": stories, "total": total}
    if stories:
        _cache_put(key, out)
    return out, "", 0


def fetch_playlist(story_id, page=1):
    """整本播放清单 (不缓存: 备货状态实时变化, 每次进入重取)。

    story_id: 连环画 htmlname (2026-09-19 契约改版: 不再收数字 id);
    page: 清单翻页 (128 条/页, more=true 时取下一页, UI 层负责连取
    至全)。
    返回 (payload, err, wait_sec):
      payload = {"title", "base_url", "files": [文件名...], "more": bool}
      文件名为页号顺序名 (001.mp3...), 无梗概前置条; 完整地址 =
      media_url(base_url + 文件名); 无音页 (内容提要/封底) 不在清单,
      设备无需跳过。
    """
    sid = str(story_id or "").strip()
    if not sid or len(sid) > 64 or "/" in sid or "\\" in sid:
        return None, "参数错误", 0
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        page = 1
    payload, err, wait = _fetch({"action": "playlist", "story_id": sid,
                                 "page": page})
    if payload is None:
        return None, err, wait
    files = []
    for f in payload.get("files") or []:
        s = str(f or "").strip()
        # 路径安全: 条目恒为纯文件名, 拒绝带路径/相对段
        if not s or "/" in s or "\\" in s or ".." in s:
            continue
        files.append(s)
    out = {"title": str(payload.get("title") or ""),
           "base_url": str(payload.get("base_url") or ""),
           "files": files,
           "more": bool(payload.get("more"))}
    return out, "", 0


def playlist_all(story_id, on_page=None):
    """连取整本清单至全 (128 条/页, more=true 翻页)。

    on_page(page) 每页取到后的回调 (UI 显示进度, 异常吞掉)。
    返回 (payload, err, wait_sec): payload = {"title","base_url",
    "files":[全本文件名]} 或 None; 翻页上限 _PLAYLIST_PAGE_MAX。
    """
    files = []
    base = ""
    title = ""
    page = 1
    while True:
        payload, err, wait = fetch_playlist(story_id, page)
        if payload is None:
            return None, err, wait
        if not base:
            base = payload["base_url"]
        if not title:
            title = payload["title"]
        files.extend(payload["files"])
        if on_page:
            try:
                on_page(page)
            except Exception:
                pass
        if not payload["more"] or page >= _PLAYLIST_PAGE_MAX:
            break
        page += 1
    if not files:
        return None, "暂无音频", 0
    return {"title": title, "base_url": base, "files": files}, "", 0


def play_track(url, volume=80, on_progress=None, cancelled=None):
    """流式播放一话 MP3 (urequests 直连 /media 地址边下边播)。

    返回 (state, played_sec): state 为
      "ok"        本话播完 (完整文件, 读尽即话尾)
      "cancelled" 播放中被按键打断 (打断动作由 cancelled 回调自行记录)
      "noent"     话地址 HTTP 404 —— 服务端仍在备货 (清单已列但该条
                  尚未落盘, 短退避重试即可, 勿长退避)
      "badmp3"    解码器无法从数据中解出帧 (bad mp3)
      "fail"      连接/I2S/其他失败
    """
    import gc
    if not url:
        return ("fail", 0)
    try:
        from framework import audio
        if not audio.available():
            print("[STORY] audio unavailable: i2s not available")
            return ("fail", 0)
        if not audio.mp3_supported():
            print("[STORY] mp3dec missing")
            return ("fail", 0)
    except Exception as e:
        print("[STORY] audio import error:", e)
        return ("fail", 0)
    gc.collect()
    r = None
    try:
        try:
            import urequests as requests
        except ImportError:
            import requests
        r = requests.get(url, timeout=_FETCH_TIMEOUT)
        if r.status_code == 404:
            print("[STORY] audio http 404 (该话仍在备货)")
            return ("noent", 0)
        if r.status_code != 200:
            print("[STORY] audio http:", r.status_code)
            return ("fail", 0)
        holder = {"sec": 0}

        def _prog(sec):
            holder["sec"] = sec
            if on_progress:
                on_progress(sec)

        ok, err = audio.play_mp3(r.raw.read, volume=volume,
                                 is_cancelled=cancelled,
                                 on_progress=_prog)
        if err == "cancelled":
            return ("cancelled", holder["sec"])
        if ok:
            return ("ok", holder["sec"])
        print("[STORY] play fail:", err)
        if err == "bad mp3":
            return ("badmp3", holder["sec"])
        return ("fail", holder["sec"])
    except Exception as e:
        print("[STORY] stream error:", e)
        return ("fail", 0)
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
        gc.collect()


# ===================== 标签本地缓存 (v1.2, 闪存根) =====================
# 只缓存第 1 批 + total (~600B): 进入秒出列表, 24h TTL 陈旧才刷新,
# 刷新成功且数据有变化才落盘 (限磨损); 原子写防掉电损坏。目录第 2 批
# 以后仍实时拉取 (翻深页=强网络意图, 不值得缓存全量目录)。

TAGS_TTL_SEC = 24 * 3600         # 缓存新鲜期 (秒): 进入时 age >= TTL 刷新
_TAGS_CACHE_PATH = "/story_tags.json"   # 闪存根 (独立文件, /lottery.json 先例)
_TAGS_CACHE_VER = 1              # schema 版本 (契约改版整文件作废)
_tags_cache_memo = None          # 读盘记忆 (tags, total, age_sec), None=未读


def tags_cached():
    """读取本地标签缓存。

    返回 (tags, total, age_sec):
      tags    [{"name","count"}...] (第 1 批), 无缓存/损坏/版本不符
              时为 None
      total   平台标签总数
      age_sec 缓存年龄秒; RTC 未同步/回拨时给大值 (促使在线刷新)
    """
    global _tags_cache_memo
    if _tags_cache_memo is not None:
        return _tags_cache_memo
    out = (None, 0, 0)
    try:
        import ujson as json
    except ImportError:
        import json
    try:
        f = open(_TAGS_CACHE_PATH, "r")
        try:
            data = json.loads(f.read())
        finally:
            f.close()
        if isinstance(data, dict) and data.get("ver") == _TAGS_CACHE_VER:
            tags = []
            for t in data.get("tags") or []:
                if not isinstance(t, dict):
                    continue
                name = str(t.get("name") or "").strip()
                if not name:
                    continue
                try:
                    cnt = int(t.get("count") or 0)
                except (ValueError, TypeError):
                    cnt = 0
                tags.append({"name": name, "count": cnt})
            if tags:
                try:
                    total = max(len(tags), int(data.get("total") or 0))
                except (ValueError, TypeError):
                    total = len(tags)
                try:
                    saved_at = int(data.get("saved_at") or 0)
                except (ValueError, TypeError):
                    saved_at = 0
                try:
                    now = int(time.time())
                except Exception:
                    now = 0
                if saved_at <= 0 or now < saved_at:
                    age = 1 << 30      # RTC 未同步/回拨: 视为过期
                else:
                    age = now - saved_at
                out = (tags, total, age)
    except Exception as e:
        print("[STORY] tags cache read:", e)
        out = (None, 0, 0)
    _tags_cache_memo = out
    return out


def tags_cache_save(tags, total):
    """标签缓存落盘 (原子写: 临时文件 + rename, 防掉电损坏)。

    tags: [{"name","count"}...] (第 1 批); total: 平台标签总数。
    写失败仅打印不影响功能; 成功后读盘记忆同步为新鲜。
    """
    global _tags_cache_memo
    try:
        import ujson as json
    except ImportError:
        import json
    try:
        now = int(time.time())
    except Exception:
        now = 0
    payload = {"ver": _TAGS_CACHE_VER, "saved_at": now,
               "total": int(total), "tags": tags}
    tmp = _TAGS_CACHE_PATH + ".tmp"
    try:
        f = open(tmp, "w")
        try:
            f.write(json.dumps(payload))
        finally:
            f.close()
        import os
        try:
            os.remove(_TAGS_CACHE_PATH)
        except OSError:
            pass
        os.rename(tmp, _TAGS_CACHE_PATH)
        _tags_cache_memo = (tags, int(total), 0)
    except Exception as e:
        print("[STORY] tags cache save:", e)
