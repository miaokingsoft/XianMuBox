"""
XianMuBox - 听老故事模式 (v1.3)

收听闲沐云平台 comic_audio 连环画语音 (framework/story 服务层):
平台把闲沐连环画中"语音完整"的小人书按页合成 MP3, 设备按
「标签 -> 标签下故事 -> 整本播放清单」三级浏览, 整本顺序流式播放
(服务端已备货前 3 条起播零等待; 单线程无法边播边下, 不做真双缓冲,
话间衔接约 2~4s = 1 次连接)。阻塞式播放接管, 按键经 is_cancelled
逐帧轮询打断。

2026-09-19 平台契约改版适配 (v1.1): 故事不再有数字 id 改 htmlname
(11 本/页, playlist/pages 的 story_id 传它); 清单无梗概前置条,
文件名为页号顺序名 (001.mp3...)。

2026-09-19 翻页交互改版 (v1.2): 分页不再随 A 键隐式拉取——每批末尾
追加绿色「拉取新标签/拉取新故事」菜单行 (光标可停留), 光标停在其上
按 B 才拉取下一批 (失败原地红字约 2 秒, 可再按 B 重试); 批已取尽时
菜单行变「回到首页」, 按 B 光标回首条不取数; A 键仅在条目+菜单行内
循环, 永不触发网络请求。

2026-09-19 标签本地缓存 (v1.3): /story_tags.json 存第 1 批+total
(原子写, 24h TTL)。进入命中缓存即整页直绘 (无等待页, 光标定位照旧):
新鲜 (TTL 内) 本批不发请求; 陈旧由 loop 静默刷新 (成功且数据有变化
才替换+落盘+重绘, 失败静默沿用缓存不出错误页); 无缓存走现行等待页
取数, 成功后落盘。弱网/离线时模式始终可用。

页面 (内容浏览类, 按键规范 2.4: A=翻页/下一项 B=主操作 C=退出):
  固件提示页 (无 mp3dec, 进入即提示, 任意键退出)
  标签拉取等待页 (进入/重试/翻页拉取, 整页绘制清除载入图防重叠)
  标签列表页: 标签 + 收录量 (每屏 6 行光标滚动窗口, 光标定位上次收听,
    批末尾绿色「拉取新标签」菜单行)
    A 短按 = 下一项 (条目+菜单行内循环, 不取数)
    B 短按 = 条目: 查看该标签下故事列表 / 菜单行: 拉取下一批(取尽回首页)
    C 短按 = 退出返回主菜单
  故事列表页: 书名 + 页数 (同款翻页菜单行「拉取新故事」)
    A 短按 = 下一项 (条目+菜单行内循环, 不取数)
    B 短按 = 条目: 收听选中书 (阻塞接管播放页, 同书自动续播) /
             菜单行: 拉取下一批(取尽回首页)
    C 短按 = 返回标签列表页
  播放页 (阻塞接管, 话循环直至按键):
    A 短按 = 下一话 (跳过; 等待态为立即重试)
    B 短按 = 停止返回故事列表页
    C 短按 = 退出模式
  无时长进度条以整本进度呈现 (已播话数/总话数), 话内显示已播 MM:SS。

退避策略: 404 = 服务端仍在备货 (清单已列但该条未落盘), 1s 起连败
翻倍封顶 60s; 网络类失败指数退避 (15s x2 -> 60s 起翻倍, 上限 30
分钟); 解码失败同网络类; 未订阅 (40309/40302, 唯一开通途径 = 平台
门户邀请码兑换)/参数错误/50401 (故事不存在/无音频/上游库不可达)
等业务类不自动重试, 恒候按键。

续播记忆 (settings.story): 播放中记录 last_tag/last_story(=htmlname)/
last_track (播完一话即写, 整本播完归零); 标签页与故事页光标定位
上次位置, 同书再按 B 从上次听到的话继续。

MicroPython 兼容: 无 f-string / time.ticks_* / urequests 惰性 import
"""

import time
import gc

try:
    from config import W, H
except ImportError:
    W, H = 240, 240

from framework.colors import (
    CLR_BG, CLR_WHITE, CLR_GRAY, CLR_DIM, CLR_CYAN,
    CLR_GREEN, CLR_RED, CLR_AMBER,
)
from modes.base import BaseMode

_FETCH_COOLDOWN_MS = 60000   # 目录拉取失败后的重试冷却
_PER_PAGE = 6                # 列表每屏行数
_ROW_H = 28                  # 列表行距 (px)
_LIST_Y = 48                 # 列表首行 y (px)
_NAME_MAX = 7                # 行内名称/书名最多字数 (右侧留数字列)
_TITLE_MAX = 8               # 播放页标题书名最多字数
_TRACK_RETRY_BASE_SEC = 1    # 备货中 (404) 退避基值
_TRACK_RETRY_MAX_SEC = 60    # 备货中退避上限 (防紧重试刷屏)
_NET_FAIL_WAIT_SEC = 15      # 网络类失败前两档等待 (之后 60s 起翻倍)
_NET_WAIT_MAX_SEC = 1800     # 网络退避上限 (官方手册 30 分钟)
_API_PAGE_TAGS = 10          # 平台标签分页大小 (10 个/页)
_API_PAGE_STORIES = 11       # 平台故事分页大小 (2026-09-19 契约: 11 本/页)
_NOTE_MS = 2000              # 翻页拉取失败原地提示时长 (ms)

# ---- 播放页喇叭帧动画 (复用 news 的 raw 素材, build 由 PNG 生成) ----
_SPK_SZ = 80                  # 帧尺寸 (px)
_SPK_X = (W - _SPK_SZ) // 2   # 水平居中
_SPK_Y = 76                   # 动画区顶部 (y76~156)
_SPK_SEQ = (0, 1, 2, 3, 2, 1) # 往返轮播序列 (声波 0→3→0, 按已播秒数切换)


def _net_wait_sec(fails):
    """网络类失败指数退避 (官方手册 60s 基值上限 30 分钟):
    前两档 15s (收听中快速自愈), 之后 60/120/240/... 封顶 1800s"""
    if fails <= 2:
        return _NET_FAIL_WAIT_SEC
    shift = min(fails - 3, 5)
    wait = 60 << shift
    return wait if wait < _NET_WAIT_MAX_SEC else _NET_WAIT_MAX_SEC


class StoryMode(BaseMode):
    MODE_ID = "story"
    MODE_NAME = "听老故事"

    def __init__(self, display, buttons, buzzer=None):
        super().__init__(display, buttons, buzzer)
        self._view = 0           # 0=标签列表页 1=故事列表页
        self._tags = []          # [{"name","count"}...] (framework/story)
        self._tags_total = 0     # 平台标签总数 (分页取尽判断)
        self._tag_sel = 0        # 标签光标 (self._tags 下标)
        self._stories = []       # [{"id","title","pages"}...]
        self._stories_total = 0  # 当前标签下故事总数
        self._story_sel = 0      # 故事光标 (self._stories 下标)
        self._cur_tag = ""       # 当前浏览的标签名
        self._err = ""           # 目录拉取错误信息 (空=正常)
        self._tags_fetched = False  # 进入后是否已尝试拉取标签
        self._last_try_ms = 0    # 上次拉取时刻 (失败冷却)
        self._note = ""          # 翻页菜单行原地提示 (如「拉取失败」)
        self._note_until = 0     # 提示消失时刻 (ticks)
        self._tags_refresh = False  # 缓存注水后陈旧, loop 静默刷新一次
        self._hz = None          # hanzi_24 字体
        self._no_fw = False      # 固件无 mp3dec, 进入即提示
        self._spk_cache = {}     # 喇叭帧号 -> bytearray / False

    # ---------- 生命周期 ----------

    def enter(self):
        gc.collect()
        self._hz = self._load_hanzi()
        self._no_fw = False
        try:
            from framework import audio
            if not audio.mp3_supported():
                self._no_fw = True
        except Exception:
            pass
        if self._no_fw:
            self._draw_fw_page()
            return
        if not self._seed_from_cache():
            self._draw_wait("拉取标签", 0.0)

    def loop(self):
        try:
            if self._no_fw:
                up, sel, down, _bl, _al, _cl = self._scan_with_timing()
                if up or sel or down:
                    self.flash_exit()
                    return False
                time.sleep_ms(50)
                return True
            if not self._tags_fetched:
                self._tags_fetched = True
                self._auto_fetch_tags()
            elif self._tags_refresh:
                self._tags_refresh = False
                self._refresh_tags()
            if self._note and time.ticks_diff(
                    time.ticks_ms(), self._note_until) >= 0:
                self._note = ""          # 原地提示到期, 恢复菜单行文案
                if self._view == 0:
                    self._draw_tags()
                else:
                    self._draw_stories()
            up, sel, down, _bl, _al, _cl = self._scan_with_timing()
            if self._view == 0:
                if up:
                    self._next_tag()
                elif sel:
                    if self._tags and self._tag_sel == len(self._tags):
                        self._menu_action_tags()
                    else:
                        self._open_stories()
                elif down:
                    self.flash_exit()
                    return False
            else:
                if up:
                    self._next_story()
                elif sel:
                    if self._stories and \
                            self._story_sel == len(self._stories):
                        self._menu_action_stories()
                    elif not self._listen_start():
                        self.flash_exit()
                        return False
                elif down:
                    self._view = 0
                    self._err = ""
                    self._note = ""
                    self._draw_tags()
            time.sleep_ms(30)
            return True
        except Exception as e:
            print("[STORY-MODE] loop error:", repr(e))
            time.sleep_ms(200)
            return True

    def exit(self):
        self._tags = []
        self._stories = []
        self._hz = None
        self._spk_cache = {}
        gc.collect()

    # ---------- 字体 ----------

    def _load_hanzi(self):
        try:
            from font import hanzi_24 as f
            return f
        except ImportError:
            return None

    def _sf(self):
        try:
            from font import vga1_8x8 as sf
            return sf
        except ImportError:
            return None

    # ---------- 标签列表页 ----------

    def _seed_from_cache(self):
        """进入时用本地标签缓存秒出列表 (v1.3): 命中即整页直绘 (无
        等待页), 新鲜 (TTL 内) 本批不发请求; 陈旧置刷新标记由 loop
        静默刷新。无缓存返回 False 走现行等待页取数。"""
        try:
            from framework import story as svc
            tags, total, age = svc.tags_cached()
        except Exception as e:
            print("[STORY-MODE] cache seed error:", e)
            return False
        if not tags:
            return False
        self._tags = tags
        self._tags_total = total or len(tags)
        self._tag_sel = self._locate_last_tag()
        self._err = ""
        self._tags_fetched = True
        self._tags_refresh = age >= svc.TAGS_TTL_SEC
        self._draw_tags()
        return True

    def _refresh_tags(self):
        """缓存陈旧时的静默刷新 (v1.3): 取标签第 1 批, 成功且数据有
        变化才替换+落盘+重绘; 失败静默沿用缓存不出错误页 (串口留日志)。"""
        try:
            from framework import story as svc
            payload, err, _wait = svc.fetch_tags(1)
        except Exception as e:
            print("[STORY-MODE] tags refresh error:", e)
            payload, err = None, "云服务异常"
        if payload is None:
            print("[STORY-MODE] tags refresh fail:", err)
            return
        new_tags = (payload or {}).get("tags") or []
        new_total = (payload or {}).get("total") or len(new_tags)
        if not new_tags or (new_tags == self._tags
                            and new_total == self._tags_total):
            return                       # 无数据/无变化: 不重绘不落盘
        self._tags = new_tags
        self._tags_total = new_total
        if self._tag_sel >= len(self._tags):
            self._tag_sel = 0
        self._draw_tags()
        try:
            svc.tags_cache_save(self._tags, self._tags_total)
        except Exception as e:
            print("[STORY-MODE] cache save error:", e)

    def _auto_fetch_tags(self):
        """拉取标签第 1 页 (无缓存进入/A 重试路径; 阻塞约 1~5s, 失败
        后冷却), 成功后落盘本地缓存"""
        now = time.ticks_ms()
        if self._tags and time.ticks_diff(now, self._last_try_ms) < 0:
            return
        self._draw_wait("拉取标签", 0.15)
        try:
            from framework import story as svc
            payload, err, _wait = svc.fetch_tags(1)
        except Exception as e:
            print("[STORY-MODE] tags error:", e)
            payload, err = None, "云服务异常"
        self._last_try_ms = time.ticks_add(now, _FETCH_COOLDOWN_MS)
        if payload and payload.get("tags"):
            self._tags = payload["tags"]
            self._tags_total = payload.get("total") or len(self._tags)
            self._err = ""
            self._tag_sel = self._locate_last_tag()
            try:
                from framework import story as svc
                svc.tags_cache_save(self._tags, self._tags_total)
            except Exception as e:
                print("[STORY-MODE] cache save error:", e)
        else:
            self._err = err or "标签获取失败"
        self._draw_tags()

    def _locate_last_tag(self):
        """光标定位上次收听的标签 (settings.story.last_tag)"""
        last = ""
        try:
            from framework.settings import get_story
            last = str(get_story().get("last_tag") or "")
        except Exception:
            pass
        self_sel = 0
        if last:
            for i, t in enumerate(self._tags):
                if t["name"] == last:
                    self_sel = i
                    break
        return self_sel

    def _next_tag(self):
        """标签页 A 短按: 光标在「当前批条目 + 翻页菜单行」内循环,
        不触发取数 (v1.2); 空表触发重试拉取"""
        if not self._tags:
            self._draw_wait("重试拉取", 0.0)
            self._last_try_ms = 0
            self._tags_fetched = False
            return
        self.beep(800, 15)
        self._tag_sel = (self._tag_sel + 1) % (len(self._tags) + 1)
        self._draw_tags()

    def _menu_action_tags(self):
        """标签页翻页菜单行 B 短按: 批未取尽 = 拉取下一批;
        已取尽 = 「回到首页」, 光标回首条不取数 (v1.2)"""
        if len(self._tags) < self._tags_total:
            self._fetch_more_tags()
        else:
            self.beep(1200, 30)
            self._tag_sel = 0
            self._draw_tags()

    def _fetch_more_tags(self):
        """翻页菜单行 B 短按: 取标签下一批 (阻塞, 10 个/批), 成功后
        光标落新批首条; 失败原地红字提示约 2 秒, 可再按 B 重试 (v1.2)"""
        old_len = len(self._tags)
        page = old_len // _API_PAGE_TAGS + 1
        self._draw_wait("取标签第%d页" % page, 0.15)
        try:
            from framework import story as svc
            payload, err, _wait = svc.fetch_tags(page)
        except Exception as e:
            print("[STORY-MODE] tags page error:", e)
            payload, err = None, "云服务异常"
        if payload and payload.get("tags"):
            self._tags.extend(payload["tags"])
            self._tags_total = max(self._tags_total,
                                   payload.get("total") or 0)
            self._tag_sel = old_len
            self._note = ""
            self._err = ""
        else:
            self._note = "拉取失败"
            self._note_until = time.ticks_add(time.ticks_ms(), _NOTE_MS)
            self.beep(600, 40)
        self._draw_tags()

    # ---------- 故事列表页 ----------

    def _open_stories(self):
        """标签页 B 短按: 拉取该标签下故事第 1 页并切换到故事列表页"""
        if not self._tags:
            self.beep(600, 40)
            return
        self.beep(1200, 30)
        tag = self._tags[self._tag_sel]["name"]
        self._load_stories(tag)

    def _load_stories(self, tag):
        """拉取标签下故事列表 (阻塞, 缓存命中即时), 成功切页"""
        self._note = ""
        self._draw_wait("拉取故事", 0.15)
        try:
            from framework import story as svc
            payload, err, _wait = svc.fetch_stories(tag)
        except Exception as e:
            print("[STORY-MODE] stories error:", e)
            payload, err = None, "云服务异常"
        if payload and payload.get("stories"):
            self._cur_tag = tag
            self._stories = payload["stories"]
            self._stories_total = payload.get("total") or len(self._stories)
            self._story_sel = self._locate_last_story()
            self._err = ""
            self._view = 1
            self._draw_stories()
        else:
            self._err = err or "故事获取失败"
            self._draw_tags()

    def _locate_last_story(self):
        """光标定位上次收听的书 (settings.story.last_tag/last_story,
        last_story 为 htmlname 字符串, v1.1 契约改版后无数字 id)"""
        try:
            from framework.settings import get_story
            s = get_story()
            if str(s.get("last_tag") or "") != self._cur_tag:
                return 0
            name = str(s.get("last_story") or "")
            for i, it in enumerate(self._stories):
                if it["htmlname"] == name:
                    return i
        except Exception:
            pass
        return 0

    def _next_story(self):
        """故事页 A 短按: 光标在「当前批条目 + 翻页菜单行」内循环,
        不触发取数 (v1.2); 空表重拉"""
        if not self._stories:
            self._load_stories(self._cur_tag)
            return
        self.beep(800, 15)
        self._story_sel = (self._story_sel + 1) % (len(self._stories) + 1)
        self._draw_stories()

    def _menu_action_stories(self):
        """故事页翻页菜单行 B 短按: 批未取尽 = 拉取下一批;
        已取尽 = 「回到首页」, 光标回首条不取数 (v1.2)"""
        if len(self._stories) < self._stories_total:
            self._fetch_more_stories()
        else:
            self.beep(1200, 30)
            self._story_sel = 0
            self._draw_stories()

    def _fetch_more_stories(self):
        """翻页菜单行 B 短按: 取故事下一批 (阻塞, 11 本/批), 成功后
        光标落新批首条; 失败原地红字提示约 2 秒, 可再按 B 重试 (v1.2)"""
        old_len = len(self._stories)
        page = old_len // _API_PAGE_STORIES + 1
        self._draw_wait("取故事第%d页" % page, 0.15)
        try:
            from framework import story as svc
            payload, err, _wait = svc.fetch_stories(self._cur_tag, "hits",
                                                    page)
        except Exception as e:
            print("[STORY-MODE] stories page error:", e)
            payload, err = None, "云服务异常"
        if payload and payload.get("stories"):
            self._stories.extend(payload["stories"])
            self._stories_total = max(self._stories_total,
                                      payload.get("total") or 0)
            self._story_sel = old_len
            self._note = ""
            self._err = ""
        else:
            self._note = "拉取失败"
            self._note_until = time.ticks_add(time.ticks_ms(), _NOTE_MS)
            self.beep(600, 40)
        self._draw_stories()

    # ---------- 播放页 (阻塞接管, 话循环) ----------

    def _listen_start(self):
        """故事页 B 短按: 进入播放页。返回 False = C 键退出模式"""
        if not self._stories:
            self.beep(600, 40)
            return True
        self.beep(1200, 30)
        act = self._listen(self._story_sel)
        self._draw_stories()
        return act != "exit"

    def _listen(self, idx):
        """播放页主循环: 整本顺序收听 self._stories[idx]。

        返回 "back"=B 停止返回故事列表 / "exit"=C 退出模式。
        """
        from framework import story as svc
        d = self.display
        self.buttons.clear_events()
        st = self._stories[idx]
        sid = st["htmlname"]
        title = st["title"][:_TITLE_MAX]
        tag = self._cur_tag
        files = None
        base = ""
        while True:
            if files is None:
                self._draw_play(d, title, "取清单", CLR_GRAY, 0, 1, "")
                payload, ferr, fwait = svc.playlist_all(sid)
                if payload is None:
                    netlike = ferr in ("网络失败", "云服务异常", "限流中")
                    act = self._wait_play(d, title, ferr,
                                          CLR_RED if netlike else CLR_AMBER,
                                          fwait)
                    if act == "back":
                        return "back"
                    if act == "exit":
                        return "exit"
                    continue          # A 重试 / 倒计时尽重取
                files = payload["files"]
                base = payload["base_url"]
            i = self._resume_idx(sid, len(files))
            noent_fails = 0       # 备货中 (404) 连续档 (短退避用)
            net_fail = 0          # 网络类失败连续档 (指数退避用)
            while i < len(files):
                fname = files[i]
                url = svc.media_url((base or "") + fname)
                label = self._track_label(fname)
                self._save_book(tag, sid, i)
                state, act = self._play_one(d, svc, url, title, label,
                                            i, len(files))
                if act == "back":
                    return "back"
                if act == "exit":
                    return "exit"
                if act == "next":     # A 跳过本话
                    i += 1
                    noent_fails = 0
                    net_fail = 0
                    continue
                if state == "ok":
                    i += 1
                    noent_fails = 0
                    net_fail = 0
                    self._save_book(tag, sid,
                                    i if i < len(files) else 0)
                    if i < len(files):
                        self._draw_play(d, title, "下一话", CLR_GRAY,
                                        i, len(files), "")
                    continue
                if state == "noent":
                    # 404 = 服务端仍在备货 (清单已列但该条未落盘),
                    # 1s 起连败翻倍封顶 60s, 不紧刷不轻弃
                    noent_fails += 1
                    wait = _TRACK_RETRY_BASE_SEC << min(noent_fails - 1, 6)
                    if wait > _TRACK_RETRY_MAX_SEC:
                        wait = _TRACK_RETRY_MAX_SEC
                    act = self._wait_play(d, title, "备货中", CLR_AMBER,
                                          wait)
                else:
                    net_fail += 1
                    msg = "解码异常" if state == "badmp3" else "播放中断"
                    act = self._wait_play(d, title, msg, CLR_RED,
                                          _net_wait_sec(net_fail))
                if act == "back":
                    return "back"
                if act == "exit":
                    return "exit"
                # act 为 "retry" (A) 或 None (倒计时尽): 重试本话
            # 整本播完: 续播位归零, 恒候按键 (A=从头再听 B=返回 C=退出)
            self._save_book(tag, sid, 0)
            act = self._wait_play(d, title, "播完全本", CLR_GREEN, 0)
            if act == "back":
                return "back"
            if act == "exit":
                return "exit"
            files = None              # A: 重取清单从头再听 (清单可能更新)

    def _play_one(self, d, svc, url, title, label, i, total):
        """连接并流式播放单话。

        返回 (state, act): state 为 ok/cancelled/noent/badmp3/fail;
        act 为 None / "next" / "back" / "exit" (播放被打断时非 None)。
        """
        self._draw_play(d, title, "连接中", CLR_GRAY, i, total, label)
        holder = {"act": None}
        prog = self._make_prog(d, i, total)
        state, _sec = svc.play_track(url, self._volume(), prog,
                                     self._make_cancel(holder))
        if holder["act"]:
            return ("cancelled", holder["act"])
        return (state, None)

    def _make_cancel(self, holder):
        """play_mp3 的打断回调: 逐帧轮询按键并记录动作。
        A=下一话 B=停止返回 C=退出, 与播放页按键语义一致。"""
        btns = self.buttons

        def _cancel():
            up, sel, down, _a, _b, _c = btns.scan_timed()
            if up:
                holder["act"] = "next"
            elif sel:
                holder["act"] = "back"
            elif down:
                holder["act"] = "exit"
            return holder["act"] is not None

        return _cancel

    def _wait_play(self, d, title, msg, color, wait_sec):
        """播放页等待态 (错误退避/备货中/播完全本), A=重试 B=返回 C=退出。

        wait_sec>0 时倒计时显示 "N秒后重试", 计时尽返回 None (重试);
        wait_sec<=0 (业务类错误/完本态) 恒候直至按键。
        """
        self._draw_play(d, title, msg, color, 0, 0, "")
        self._draw_hint(msg)
        btns = self.buttons
        btns.clear_events()
        end = time.ticks_add(time.ticks_ms(),
                             wait_sec * 1000) if wait_sec > 0 else 0
        last_n = -1
        while True:
            up, sel, down, _a, _b, _c = btns.scan_timed()
            if up:
                return "retry"
            if sel:
                return "back"
            if down:
                return "exit"
            if wait_sec > 0:
                left = time.ticks_diff(end, time.ticks_ms()) // 1000 + 1
                if left <= 0:
                    return None
                if left != last_n:
                    last_n = left
                    self._draw_countdown(d, left)
            time.sleep_ms(50)

    # ---------- 续播记忆 ----------

    def _resume_idx(self, sid, total):
        """同书续播: settings.story.last_story (htmlname) 匹配时从
        last_track 继续"""
        try:
            from framework.settings import get_story
            s = get_story()
            if str(s.get("last_story") or "") == sid:
                k = int(s.get("last_track") or 0)
                if 0 < k < total:
                    return k
        except Exception:
            pass
        return 0

    def _save_book(self, tag, sid, track):
        """记录续播位 (播完一话/换话即写, 整本播完 track=0 归零)"""
        try:
            from framework import settings as st
            st.update({"story": {"last_tag": str(tag),
                                 "last_story": str(sid),
                                 "last_track": int(track)}})
        except Exception as e:
            print("[STORY-MODE] save error:", e)

    def _track_label(self, fname):
        """话标签: 文件名为页号顺序名 (001.mp3 -> 第1页; 2026-09-19
        契约改版后无梗概前置条)"""
        stem = str(fname or "").split(".")[0]
        try:
            no = int(stem)
        except (ValueError, TypeError):
            return "下一话"
        if no > 0:
            return "第%d页" % no
        return "下一话"

    def _volume(self):
        try:
            from framework.settings import get_story
            v = int(get_story().get("volume", 80))
        except Exception:
            v = 80
        if not (0 <= v <= 100):
            v = 80
        return v

    # ---------- 绘制 ----------

    def _write_len(self, font, s):
        """测量比例字库文本宽度 (write_len 不可用时按 MAX_WIDTH 估算)"""
        try:
            return self.display.write_len(font, s)
        except Exception:
            return len(s) * getattr(font, "MAX_WIDTH", 24)

    def _draw_hint(self, msg):
        """业务错误引导行 (未订阅: 付费源唯一开通途径 = 平台门户
        邀请码兑换, 官方手册 2.9/3.10)"""
        if msg == "未订阅" and self._hz:
            t = "网页兑换邀请码"
            self.display.write(self._hz, t,
                               (W - self._write_len(self._hz, t)) // 2,
                               168, CLR_AMBER, CLR_BG)

    def _draw_menu_row(self, d, y, sel, is_tags):
        """列表末尾翻页菜单行 (v1.2, 绿色与其他行区分): 批未取尽 =
        「拉取新标签/拉取新故事」(B=拉下一批); 已取尽 = 「回到首页」
        (B=光标回首条); 拉取失败时原地红字提示约 2 秒"""
        if self._note:
            text, color = self._note, CLR_RED
        else:
            more = (len(self._tags) if is_tags else len(self._stories)) \
                < (self._tags_total if is_tags else self._stories_total)
            text = ("拉取新标签" if is_tags else "拉取新故事") if more \
                else "回到首页"
            color = CLR_GREEN
        if self._hz:
            if sel:
                d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
            d.write(self._hz, text, 8 + 24, y, color, CLR_BG)

    def _draw_tags(self):
        """标签列表页整体重绘: 标题+页码, 光标滚动窗口列表 (批末尾
        翻页菜单行), 底部提示"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            d.write(self._hz, "听老故事", 8, 8, CLR_CYAN, CLR_BG)
        if self._tags:
            sf = self._sf()
            if sf:
                total = self._tags_total or len(self._tags)
                tag = "%d/%d" % (self._tag_sel + 1, total)
                d.text(sf, tag, W - 8 - 8 * len(tag), 16, CLR_GRAY, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        if self._err and not self._tags:
            if self._hz:
                d.write(self._hz, self._err[:9], 8, 96, CLR_RED, CLR_BG)
            sf = self._sf()
            if sf:
                d.text(sf, "A:RETRY", 8, H - 40, CLR_GRAY, CLR_BG)
        else:
            start = (self._tag_sel // _PER_PAGE) * _PER_PAGE
            total_rows = len(self._tags) + 1     # +1 翻页菜单行
            y = _LIST_Y
            for i in range(_PER_PAGE):
                k = start + i
                if k >= total_rows:
                    break
                sel = k == self._tag_sel
                if k < len(self._tags):
                    color = CLR_AMBER if sel else CLR_WHITE
                    if self._hz:
                        if sel:
                            d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
                        d.write(self._hz,
                                self._tags[k]["name"][:_NAME_MAX],
                                8 + 24, y, color, CLR_BG)
                    sf = self._sf()
                    if sf:
                        cnt = str(self._tags[k].get("count") or "")
                        d.text(sf, cnt, W - 8 - 8 * len(cnt), y + 8,
                               CLR_GRAY, CLR_BG)
                else:
                    self._draw_menu_row(d, y, sel, True)
                y += _ROW_H
        sf = self._sf()
        if sf:
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:OPEN", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:EXIT", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        gc.collect()

    def _draw_stories(self):
        """故事列表页整体重绘: 标签名为题, 书名 + 页数 (批末尾翻页
        菜单行), 底部提示"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            d.write(self._hz, (self._cur_tag or "故事")[:_NAME_MAX],
                    8, 8, CLR_CYAN, CLR_BG)
        if self._stories:
            sf = self._sf()
            if sf:
                total = self._stories_total or len(self._stories)
                tag = "%d/%d" % (self._story_sel + 1, total)
                d.text(sf, tag, W - 8 - 8 * len(tag), 16, CLR_GRAY, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        if self._err and not self._stories:
            if self._hz:
                d.write(self._hz, self._err[:9], 8, 96, CLR_RED, CLR_BG)
            sf = self._sf()
            if sf:
                d.text(sf, "A:RETRY", 8, H - 40, CLR_GRAY, CLR_BG)
        else:
            start = (self._story_sel // _PER_PAGE) * _PER_PAGE
            total_rows = len(self._stories) + 1  # +1 翻页菜单行
            y = _LIST_Y
            for i in range(_PER_PAGE):
                k = start + i
                if k >= total_rows:
                    break
                sel = k == self._story_sel
                if k < len(self._stories):
                    color = CLR_AMBER if sel else CLR_WHITE
                    if self._hz:
                        if sel:
                            d.write(self._hz, ">", 8, y, CLR_AMBER, CLR_BG)
                        d.write(self._hz,
                                self._stories[k]["title"][:_NAME_MAX],
                                8 + 24, y, color, CLR_BG)
                    sf = self._sf()
                    if sf:
                        pages = str(self._stories[k].get("pages") or "")
                        d.text(sf, pages, W - 8 - 8 * len(pages), y + 8,
                               CLR_GRAY, CLR_BG)
                else:
                    self._draw_menu_row(d, y, sel, False)
                y += _ROW_H
        sf = self._sf()
        if sf:
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:PLAY", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:BACK", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        gc.collect()

    def _draw_wait(self, text, frac):
        """拉取等待页: 整页绘制 (清除调度载入图, 防文字重叠) +
        标题行 + 状态文字 + 进度条"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            d.write(self._hz, "听老故事", 8, 8, CLR_CYAN, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        if self._hz:
            d.write(self._hz, text[:9], 8, 96, CLR_GRAY, CLR_BG)
        self._draw_bar(frac)

    def _draw_bar(self, frac):
        """等待进度条 (0.0~1.0, DIM 底槽 + 青色填充, y150 居中)"""
        d = self.display
        if frac < 0:
            frac = 0
        elif frac > 1:
            frac = 1
        w = int((W - 48) * frac)
        d.fill_rect(24, 150, W - 48, 8, CLR_DIM)
        if w > 0:
            d.fill_rect(24, 150, w, 8, CLR_CYAN)

    def _draw_fw_page(self):
        """固件无 mp3dec 提示页 (任意键退出)"""
        d = self.display
        d.fill(CLR_BG)
        if self._hz:
            for text, color, y in (
                    ("固件不支持", CLR_RED, 84),
                    ("MP3故事播放", CLR_RED, 116),
                    ("请更新固件", CLR_AMBER, 148)):
                d.write(self._hz, text,
                        (W - self._write_len(self._hz, text)) // 2,
                        y, color, CLR_BG)
        sf = self._sf()
        if sf:
            d.text(sf, "ANY KEY:EXIT", (W - 12 * 8) // 2, H - 20,
                   CLR_GRAY, CLR_BG)

    def _draw_play(self, d, title, state, color, i, total, label):
        """播放页整页静态框: 标题行 + 状态行 + 喇叭 + 话标签 + 整本进度"""
        d.fill(CLR_BG)
        # 顶部标题行: 青色书名 (截 _TITLE_MAX 字) + 右侧播放点
        self._play_dot(d, True)
        if self._hz:
            d.write(self._hz, title[:_TITLE_MAX], 8, 8, CLR_CYAN, CLR_BG)
        d.hline(0, 38, W, CLR_DIM)
        # 状态行: 左状态文字 (右上部留给已播时长)
        self._draw_state(d, state, color)
        self._draw_elapsed(d, -1)
        # 喇叭动画区 + 话标签 (文件名前缀页码)
        self._spk_frame(d, 2)
        if self._hz and label:
            d.write(self._hz, label,
                    (W - self._write_len(self._hz, label)) // 2,
                    168, CLR_WHITE, CLR_BG)
        self._draw_book_bar(d, i, total)
        sf = self._sf()
        if sf:
            d.text(sf, "A:NEXT", 8, H - 14, CLR_GRAY, CLR_BG)
            d.text(sf, "B:STOP", (W - 6 * 8) // 2, H - 14, CLR_GREEN,
                   CLR_BG)
            d.text(sf, "C:EXIT", W - 6 * 8 - 8, H - 14, CLR_GRAY, CLR_BG)
        gc.collect()

    def _draw_state(self, d, text, color):
        """状态行左文字 (清除后重写, 播放开始时由进度回调切换)"""
        d.fill_rect(0, 46, 180, 28, CLR_BG)
        if self._hz and text:
            d.write(self._hz, text[:7], 8, 48, color, CLR_BG)

    def _draw_countdown(self, d, left):
        """等待态倒计时文字 (话标签区, 每秒一次, 不清喇叭动画)"""
        if not self._hz:
            return
        t = "%d秒后重试" % left
        d.fill_rect(0, 164, W, 28, CLR_BG)
        d.write(self._hz, t, (W - self._write_len(self._hz, t)) // 2,
                168, CLR_AMBER, CLR_BG)

    def _play_dot(self, d, on):
        """标题行右侧播放点 (播放中经进度回调 500ms 相位闪烁)"""
        d.fill_rect(W - 18, 10, 8, 8, CLR_WHITE if on else CLR_DIM)

    def _draw_elapsed(self, d, sec):
        """状态行右上已播时长 MM:SS (sec<0 时清除)"""
        sf = self._sf()
        if not sf:
            return
        d.fill_rect(184, 54, 48, 14, CLR_BG)
        if sec >= 0:
            d.text(sf, "%02d:%02d" % (sec // 60, sec % 60), 192, 58,
                   CLR_GRAY, CLR_BG)

    def _draw_book_bar(self, d, i, total):
        """整本进度条 (琥珀色, 已播话数/总话数, total<=0 不画)"""
        if total <= 0:
            return
        frac = float(i + 1) / total
        if frac > 1:
            frac = 1
        w = int((W - 32) * frac)
        d.fill_rect(16, 202, W - 32, 5, CLR_DIM)
        if w > 0:
            d.fill_rect(16, 202, w, 5, CLR_AMBER)

    def _make_prog(self, d, i, total):
        """播放动态 UI 回调: 首秒切换"正在收听" + 喇叭帧轮播 +
        已播时长 + 播放点闪烁 (整本进度条静态, 不在此重绘)"""
        shown = {"sec": -1, "f": -1, "started": False}

        def _prog(sec):
            if not shown["started"]:
                shown["started"] = True
                self._draw_state(d, "正在收听", CLR_GREEN)
            f = _SPK_SEQ[sec % 6]
            if f != shown["f"]:
                shown["f"] = f
                self._spk_frame(d, f)
            if sec != shown["sec"]:
                shown["sec"] = sec
                self._draw_elapsed(d, sec)
            self._play_dot(d, ((time.ticks_ms() // 500) & 1) == 0)

        return _prog

    def _spk_frame(self, d, idx):
        """blit 单帧喇叭动画 (复用 /icons/news_spk_N.raw, 80x80 RGB565)。
        raw 首次使用时读入并常驻缓存; 缺失时该区域保持纯黑 (安全降级)。"""
        buf = self._spk_cache.get(idx)
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
            self._spk_cache[idx] = buf
        if buf:
            d.blit_buffer(memoryview(buf), _SPK_X, _SPK_Y, _SPK_SZ, _SPK_SZ)
