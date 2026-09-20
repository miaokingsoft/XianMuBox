"""
XianMuBox - I2S 音频输出层 (v1.5)

驱动 MAX98357A I2S 功放播放 WAV (PCM) / MP3 音频, 供整点新闻播报使用:

  - play_wav(): 边下边播 —— read_fn 流式供数 (HTTP r.raw.read),
    内部解析 WAV 头 (自适应采样率/声道, 仅 16bit PCM), 分块写 I2S
  - play_mp3(): 边下边播 MP3 —— 需固件含 mp3dec 解码 C 模块
    (mp129-romfont-mp3dec 固件起), C 侧完成解码/音量/立体声化,
    Python 侧只维护滑窗与 I2S 写入; 固件无该模块时返回 "no decoder"
  - play_sine(): 生成正弦音, 供「音频自检」验证接线/喇叭
  - 数字音量: 16bit 采样右移缩放 (约 6dB 步进, 0=近似静音),
    MAX98357A 的 GAIN 引脚保持出厂默认 (9dB), 音量全由软件控制

设计约束:
  - machine.I2S 不可用时 (PC 仿真 / 无音频引脚档案的设备) 全部
    接口安全降级: play_* 返回 (False, "no audio"), 不抛异常
  - 播放为阻塞式, 调用方 (framework/news 播报接管页) 通过
    is_cancelled 回调在每个块间 (约 100ms) 轮询按键实现随时打断
  - I2S 外设按次初始化/释放 (整点才播一次, 不常驻占 DMA 内存)
  - 播放正常收尾先排空内部缓冲尾音 (_IBUF 折算时长, v1.5.1) 再
    deinit, 避免每条音频最后一小段被砍; 打断路径不排空, 保持打断
    即时性
  - 采样块转换用 struct.unpack + array('h') (两步均可回退 CPython,
    不依赖 memoryview.cast 等固件差异特性); MP3 路径不经此转换,
    解码输出即立体声 16bit 字节流直写 I2S

MicroPython 兼容: 无 f-string / time.ticks_* / machine 惰性 import
"""

import time

try:
    from machine import I2S, Pin
    _HAS_I2S = True
except ImportError:
    I2S = None
    Pin = None
    _HAS_I2S = False

try:
    from config import I2S_BCLK, I2S_LRC, I2S_DIN
except ImportError:
    I2S_BCLK, I2S_LRC, I2S_DIN = 38, 39, 40

_CHUNK = 8192              # 流式块大小 (字节); 24k/单声道/16bit 源速约 170ms/块
_IBUF = 65536              # I2S 内部 DMA 前置缓冲; 约 680ms 余量, 抗网络/TLS 抖动

_DBG = False  # v1.29 固件升级验收完成（124KB/s），联调期计时打印关闭                # 临时流播诊断: 每块分段计时, 每 16 块打一行
                           # [AUDIO] n/rd(网络)/wr(I2S)/dr(回调)/gapmax(块间隔峰值)/free

_i2s = None


def available():
    """当前固件/硬件是否具备 I2S 输出能力 (PC 仿真返回 False)"""
    return _HAS_I2S and I2S_BCLK is not None and I2S_DIN is not None


def _vol_shift(volume):
    """音量 0-100 -> 右移位数 0-8 (0=满音量, 8=近似静音)"""
    try:
        v = int(volume)
    except (ValueError, TypeError):
        v = 80
    if v < 0:
        v = 0
    elif v > 100:
        v = 100
    return (100 - v) * 8 // 100


def _ensure_i2s(rate, bits=16):
    """按当前音频参数初始化 I2S TX (标准 Philips 时序, MAX98357A 兼容)。

    S3 + MicroPython v1.20 固件的 MONO 档存在采样左右错位 BUG
    (micropython#11635: 单声道数据被按左右交替发送), 故固定以
    STEREO 打开, 数据由 _write_pcm 复制成左右相同帧。
    """
    global _i2s
    if _i2s is not None:
        return _i2s
    _i2s = I2S(0, sck=Pin(I2S_BCLK), ws=Pin(I2S_LRC), sd=Pin(I2S_DIN),
               mode=I2S.TX, bits=bits, format=I2S.STEREO,
               rate=rate, ibuf=_IBUF)
    return _i2s


def deinit():
    """释放 I2S 外设 (播放结束/被打断后调用, 归还 DMA 内存)"""
    global _i2s
    if _i2s is not None:
        try:
            _i2s.deinit()
        except Exception:
            pass
        _i2s = None


def _drain_i2s(rate, channels):
    """播放正常收尾: 等内部缓冲 (_IBUF 前置缓冲 + DMA 描述符) 里的
    尾音播完再返回, 随后的 deinit() 不再砍尾 (v1.5.1)。

    I2S.write() 返回只代表数据已拷入内部缓冲, 稳态时缓冲常驻约
    _IBUF 字节 (~0.3~0.7s 尾音); 直接 deinit 会把每条音频最后一小段
    切掉。按缓冲字节量折算排空时长 + 余量; 被打断路径勿调, 保持
    打断即时性。"""
    try:
        if _i2s is None or not rate:
            return
        bps = int(rate) * 2 * int(channels)   # 16bit STEREO 帧宽
        if bps <= 0:
            return
        ms = (_IBUF * 1000) // bps + 60
        if ms > 1000:
            ms = 1000
        time.sleep_ms(ms)
    except Exception:
        pass


# ==================== WAV 流式播放 ====================

def _read_exact(read, n):
    """从 read(n) 供数函数凑满 n 字节 (EOF 返回短块/空)"""
    buf = b""
    while len(buf) < n:
        part = read(n - len(buf))
        if not part:
            break
        buf = buf + part
    return buf


def _parse_wav_header(read):
    """解析 WAV 头。

    返回 ((audio_fmt, channels, rate, bits), data_size) 或 None;
    data_size 为 data 块声明长度, 0 = 未知 (播到 EOF 为止)。
    """
    hdr = _read_exact(read, 12)
    if len(hdr) < 12 or hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE":
        return None
    fmt = None
    while True:
        ch = _read_exact(read, 8)
        if len(ch) < 8:
            return None
        cid = bytes(ch[0:4])
        csize = ch[4] | (ch[5] << 8) | (ch[6] << 16) | (ch[7] << 24)
        if cid == b"fmt ":
            body = _read_exact(read, csize)
            if len(body) < 16:
                return None
            audio_fmt = body[0] | (body[1] << 8)
            channels = body[2] | (body[3] << 8)
            rate = body[4] | (body[5] << 8) | (body[6] << 16) | (body[7] << 24)
            bits = body[14] | (body[15] << 8)
            fmt = (audio_fmt, channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                return None
            return (fmt, csize)
        else:
            # 跳过 LIST/fact 等其他块
            left = csize
            while left > 0:
                buf = read(left if left < 512 else 512)
                if not buf:
                    return None
                left -= len(buf)


def _write_pcm(chunk, channels, shift):
    """把一块 PCM16 数据按音量缩放后写 I2S (STEREO 帧复制)。

    I2S 以 STEREO 打开 (见 _ensure_i2s), 每个采样复制成左右两份;
    立体声输入取左声道。采样解析必须用 struct.unpack —— 勿用
    array('h', bytes/memoryview) 直读: 部分固件的 array 构造器
    会把缓冲按字节值逐个迭代, 等于把字节流当样本输出, 表现为
    极弱的沙沙噪声 (v1.6.1 真机踩坑)。
    """
    import struct
    try:
        import array as _array
    except ImportError:
        _array = None
    frame = 2 * channels
    n = (len(chunk) // frame) * frame     # 截齐整帧的字节数
    if n <= 0:
        return
    samples = struct.unpack("<%dh" % (n // 2), chunk[:n])
    if channels == 2:
        samples = samples[0::2]           # 立体声取左声道
    if _array is not None:
        m = len(samples)
        out = _array.array("h", bytearray(4 * m))
        j = 0
        for s in samples:
            if shift:
                s >>= shift
            out[j] = s
            out[j + 1] = s
            j += 2
        _i2s.write(out)
    else:
        # 无 array 模块的极端兜底
        _i2s.write(struct.pack("<%dh" % (2 * len(samples)),
                               *(x for s in samples for x in (s, s))))


def play_wav(read_fn, volume=80, is_cancelled=None, on_progress=None):
    """流式播放 WAV (仅 16bit PCM, 单/双声道均可)。

    read_fn(n):       供数函数, 每次返回至多 n 字节, 空表示结束
                      (传 HTTP 响应的 r.raw.read 即可边下边播)
    is_cancelled():   每块间轮询, 返回 True 立即停止 (返回 err="cancelled")
    on_progress(sec): 每块回调已播秒数 (供进度条)
    返回 (ok, err):   ok=True 播完; err 为 "cancelled"/"no audio"/
                      "bad wav"/"unsupported"/"io"
    """
    global _i2s
    if not available():
        # 无 I2S 环境: 仍把流读完, 保证调用方语义完整 (降级演示)
        while True:
            buf = read_fn(_CHUNK)
            if not buf:
                break
        return (False, "no audio")

    parsed = _parse_wav_header(read_fn)
    if parsed is None:
        print("[AUDIO] bad wav header (not RIFF/WAVE or truncated)")
        return (False, "bad wav")
    (audio_fmt, channels, rate, bits), _data_size = parsed
    if audio_fmt not in (1, 0xFFFE) or bits != 16 or channels not in (1, 2):
        print("[AUDIO] unsupported wav: fmt=%d ch=%d rate=%d bits=%d"
              % (audio_fmt, channels, rate, bits))
        return (False, "unsupported")
    try:
        _ensure_i2s(rate, 16)
    except Exception as e:
        print("[AUDIO] i2s init error:", e)
        return (False, "io")

    shift = _vol_shift(volume)
    frame = 2 * channels          # 每采样帧字节数 (16bit)
    bytes_per_sec = rate * frame
    played = 0
    ok = True
    err = ""
    drain = False                 # 正常播完 = True: 收尾排空内部缓冲 (v1.5.1)
    if _DBG:
        import time
        import gc as _gc
        # [累计读, 累计写I2S, 累计回调, 块数, 块间隔峰值, 上次时刻]
        acc = [0, 0, 0, 0, 0, time.ticks_ms()]
    try:
        while True:
            if _DBG:
                now = time.ticks_ms()
                gap = time.ticks_diff(now, acc[5])
                if gap > acc[4]:
                    acc[4] = gap
                acc[5] = now
            if is_cancelled and is_cancelled():
                ok = False
                err = "cancelled"
                break
            chunk = read_fn(_CHUNK)
            if _DBG:
                now = time.ticks_ms()
                acc[0] += time.ticks_diff(now, acc[5])
                acc[5] = now
            if not chunk:
                drain = True
                break
            usable = len(chunk) - (len(chunk) % frame)
            if usable <= 0:
                continue
            played += usable
            if on_progress and bytes_per_sec:
                try:
                    on_progress(played // bytes_per_sec)
                except Exception:
                    pass
            if _DBG:
                now = time.ticks_ms()
                acc[2] += time.ticks_diff(now, acc[5])
                acc[5] = now
            try:
                _write_pcm(chunk, channels, shift)
            except Exception as e:
                print("[AUDIO] write error:", e)
                ok = False
                err = "io"
                break
            if _DBG:
                now = time.ticks_ms()
                acc[1] += time.ticks_diff(now, acc[5])
                acc[5] = now
                acc[3] += 1
                if acc[3] >= 16:
                    print("[AUDIO] n=%d rd=%dms wr=%dms dr=%dms gapmax=%dms free=%d"
                          % (acc[3], acc[0], acc[1], acc[2], acc[4],
                             _gc.mem_free()))
                    acc[0] = acc[1] = acc[2] = acc[3] = acc[4] = 0
    except Exception as e:
        print("[AUDIO] stream error:", e)
        ok = False
        err = "io"
    finally:
        if drain:
            _drain_i2s(rate, channels)
        deinit()
    return (ok, err)


# ==================== MP3 流式播放 ====================

_MP3_KEEP = 8192      # 滑窗补数阈值: 未消费数据低于此值才向供数函数要下一块;
                      # 也作坏数据判定线 —— 达线仍解不出任何帧 => "bad mp3"
_MP3_FEED = 16384     # 单次补数后的目标缓冲量 (约 0.9s @144kbps)
_MP3_OUT_MIN = 4608   # 输出缓冲下限: 1152 采样 x 双声道 x 2B


def mp3_supported():
    """当前固件是否带 mp3dec 解码 C 模块 (mp129-romfont-mp3dec 固件起)"""
    try:
        import mp3dec
        return True
    except ImportError:
        return False


def play_mp3(read_fn, volume=80, is_cancelled=None, on_progress=None):
    """流式播放 MP3 (固件含 mp3dec 模块时可用)。

    参数语义与 play_wav 完全一致: read_fn(n) 供数 / is_cancelled()
    每帧轮询打断 / on_progress(sec) 每整秒回调。
    解码在 mp3dec C 模块内逐帧完成 (音量移位 + 单声道复制为立体声),
    Python 侧只做滑窗供数与 I2S 写入, I2S 采样率取首个成功帧。
    返回 (ok, err): ok=True 播完; err 为 "cancelled"/"no audio"/
                    "no decoder"/"bad mp3"/"io"/"memory"
    """
    global _i2s
    if not available():
        # 无 I2S 环境: 仍把流读完, 保证调用方语义完整 (降级演示)
        while True:
            buf = read_fn(_CHUNK)
            if not buf:
                break
        return (False, "no audio")

    try:
        import mp3dec as _mp3
    except ImportError:
        return (False, "no decoder")

    try:
        out_sz = _mp3.MAX_SAMPLES_PER_FRAME * 4
    except AttributeError:
        out_sz = _MP3_OUT_MIN
    if out_sz < _MP3_OUT_MIN:
        out_sz = _MP3_OUT_MIN

    shift = _vol_shift(volume)
    buf = bytearray()
    out = bytearray(out_sz)
    pos = 0               # buf[:pos] 已消费 (滑窗起点)
    eof = False
    rate = 0
    samples_total = 0
    last_sec = -1
    started = False
    ok = True
    err = ""
    drain = False         # 正常播完 = True: 收尾排空内部缓冲尾音 (v1.5.1)
    try:
        dec = _mp3.Decoder()
        while True:
            if is_cancelled and is_cancelled():
                ok = False
                err = "cancelled"
                break
            # 补数据: 未消费量低于阈值时向供数函数要一块
            if not eof and len(buf) - pos < _MP3_KEEP:
                chunk = read_fn(_MP3_FEED - (len(buf) - pos))
                if chunk:
                    buf += chunk
                else:
                    eof = True
            if pos >= len(buf):
                if eof:
                    drain = True
                    break               # 数据耗尽, 正常播完
                continue
            consumed, pcm_len, samples, rate_n, ch_n = dec.feed(
                memoryview(buf)[pos:], out, shift)
            pos += consumed
            if samples > 0:
                if not started:
                    # 首帧才知道采样率, 此时才初始化 I2S
                    started = True
                    rate = rate_n
                    try:
                        _ensure_i2s(rate, 16)
                    except Exception as e:
                        print("[AUDIO] i2s init error:", e)
                        ok = False
                        err = "io"
                        break
                samples_total += samples
                try:
                    _i2s.write(memoryview(out)[:pcm_len])
                except Exception as e:
                    print("[AUDIO] write error:", e)
                    ok = False
                    err = "io"
                    break
                if on_progress and rate:
                    sec = samples_total // rate
                    if sec != last_sec:
                        last_sec = sec
                        try:
                            on_progress(sec)
                        except Exception:
                            pass
            elif consumed == 0:
                if eof:
                    drain = True
                    break               # 尾部不足一帧, 放弃残帧
                if len(buf) - pos >= _MP3_KEEP:
                    # 缓冲已到补数阈值仍解不出帧: 补数条件不再触发,
                    # 继续等只会死循环 => 判定数据损坏
                    print("[AUDIO] mp3 resync failed, %d bytes buffered"
                          % (len(buf) - pos))
                    ok = False
                    err = "bad mp3"
                    break
            # 滑窗压缩: 已消费过阈值则把尾部搬回头部 (摊销 O(n))
            if pos > _MP3_KEEP:
                buf = buf[pos:]
                pos = 0
    except MemoryError:
        ok = False
        err = "memory"
    except Exception as e:
        print("[AUDIO] mp3 stream error:", e)
        ok = False
        err = "io"
    finally:
        if drain:
            _drain_i2s(rate, 2)         # mp3 恒以 STEREO 打开
        deinit()
    return (ok, err)


# ==================== 自检音 ====================

def play_sine(freq_hz=440, ms=400, volume=80):
    """播放指定频率正弦音 (接线/喇叭自检)。返回 (ok, err)"""
    global _i2s
    if not available():
        return (False, "no audio")
    try:
        import math
        rate = 16000
        n = int(rate * ms / 1000)
        amp = 20000
        two_pi_f = 2 * 3.14159 * freq_hz / rate
        shift = _vol_shift(volume)
        try:
            _ensure_i2s(rate, 16)
        except Exception as e:
            print("[AUDIO] i2s init error:", e)
            return (False, "io")
        try:
            try:
                import array as _array
                mono = _array.array(
                    "h",
                    (int(amp * math.sin(two_pi_f * i)) >> shift
                     for i in range(n)))
                out = _array.array("h", bytearray(4 * n))
                j = 0
                for s in mono:
                    out[j] = s
                    out[j + 1] = s
                    j += 2
                _i2s.write(out)
            except ImportError:
                import struct
                samples = [int(amp * math.sin(two_pi_f * i)) >> shift
                           for i in range(n)]
                _i2s.write(struct.pack(
                    "<%dh" % (2 * n),
                    *(x for s in samples for x in (s, s))))
        except MemoryError:
            return (False, "memory")
        except Exception as e:
            print("[AUDIO] write error:", e)
            return (False, "io")
        finally:
            deinit()
        return (True, "")
    except Exception as e:
        print("[AUDIO] sine error:", e)
        deinit()
        return (False, "io")
