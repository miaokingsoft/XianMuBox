#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fast_push - 单连接批量文件传输 (绕过 mpremote 每文件一次连接的重启地狱)

原理: 抢占 raw REPL 一次, 之后所有文件在同一条连接里以 base64 分块写入,
设备全程不软复位; 传完 ctrl-B + ctrl-D 一次性重启进应用。
用法: python tools/fast_push.py COM5 <本地目录> [设备端前缀]
"""

import base64
import sys
import time
from pathlib import Path

import serial

CHUNK = 3072          # base64 前的原始块大小
SETTLE = 0.02


def open_port(port):
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 2
    s.dtr = True
    s.rts = False
    s.open()
    time.sleep(0.4)
    return s


def exec_raw(s, code, timeout=25):
    """raw REPL 下执行一段代码, 返回 (输出, 是否异常)"""
    s.reset_input_buffer()
    s.write(code.encode("utf-8") + b"\x04")
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        buf += s.read(2048)
        if buf.endswith(b">"):
            err = b"Traceback" in buf
            return buf, err
        if b"raw REPL" in buf:      # 刚进入 raw REPL 的回显, 继续等
            continue
    raise TimeoutError("exec timeout: %r" % buf[-200:])


def enter_raw(s):
    """打断设备并进入 raw REPL (boot 动画期间需多试几轮)"""
    for attempt in range(8):
        s.reset_input_buffer()
        for _ in range(4):
            s.write(b"\x03")
            time.sleep(0.12)
        s.write(b"\r\n")
        time.sleep(0.3)
        s.reset_input_buffer()
        s.write(b"\x01")
        time.sleep(0.5)
        buf = s.read(2048)
        if b"raw REPL" in buf:
            return True
        time.sleep(2.0)      # 撞上开机动画, 等它转完
    return False


def main():
    port = sys.argv[1]
    src_dir = Path(sys.argv[2])
    prefix = sys.argv[3] if len(sys.argv) > 3 else "/"

    files = sorted(p for p in src_dir.rglob("*") if p.is_file())
    total = sum(f.stat().st_size for f in files)
    print("push %d files, %.1f KB -> %s" % (len(files), total / 1024, port))

    s = open_port(port)
    if not enter_raw(s):
        print("[FATAL] 无法进入 raw REPL")
        sys.exit(1)
    print("[OK] raw REPL 就绪")

    _, err = exec_raw(s, "import ubinascii\n")
    if err:
        print("[FATAL] ubinascii 不可用")
        sys.exit(1)

    # 建目录
    dirs = sorted({str(f.parent.relative_to(src_dir)).replace("\\", "/")
                   for f in files if str(f.parent.relative_to(src_dir)) != "."})
    code = "import os\n"
    for d in dirs:
        code += "try:\n os.mkdir('%s/%s')\nexcept OSError as e:\n print(e)\n" % (prefix.rstrip("/"), d)
    exec_raw(s, code, timeout=20)

    fail = []
    t0 = time.time()
    done = 0
    for f in files:
        rel = str(f.relative_to(src_dir)).replace("\\", "/")
        dev = prefix.rstrip("/") + "/" + rel
        data = f.read_bytes()
        ok = True
        out, err = exec_raw(s, "f=open('%s','wb')\n" % dev, timeout=15)
        if err:
            ok = False
            print("[DBG] open %s: %r" % (rel, out[:200]))
        else:
            for i in range(0, len(data), CHUNK):
                b64 = base64.b64encode(data[i:i + CHUNK]).decode()
                out, err = exec_raw(s, "f.write(ubinascii.a2b_base64('%s'))\n" % b64,
                                    timeout=30)
                if err:
                    ok = False
                    print("[DBG] write %s@%d: %r" % (rel, i, out[:200]))
                    break
            if ok:
                out, err = exec_raw(s, "f.close()\n", timeout=15)
                if err:
                    ok = False
                    print("[DBG] close %s: %r" % (rel, out[:200]))
        if ok:
            # 回读校验长度
            out, err = exec_raw(s, "print(len(open('%s','rb').read()))\n" % dev,
                                timeout=15)
            echoed = out.decode("utf-8", "ignore")
            if str(len(data)) not in echoed:
                ok = False
        done += 1
        if done % 20 == 0 or not ok:
            print("[%3d/%d] %5.1f KB/s %s" % (done, len(files),
                  done and (sum(x.stat().st_size for x in files[:done]) / 1024) / max(0.1, time.time() - t0),
                  "ok" if ok else "FAIL " + rel))
        if not ok:
            fail.append(rel)

    print("== 完成: %d/%d, 失败 %d %s" % (done, len(files), len(fail), fail[:5]))
    # 回 friendly REPL 并软复位启动应用
    s.write(b"\x02")
    time.sleep(0.4)
    s.write(b"\x04")
    time.sleep(1)
    s.close()
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
