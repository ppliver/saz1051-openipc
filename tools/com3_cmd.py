#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""com3_cmd.py — 通过 COM3 串口在设备的 shell 上执行一条命令并回显结果。

设备 SSH 起不来 / 系统卡在裸 shell 时的兜底通道。支持一次性执行多条命令（用 ; 分隔
或传入多个参数，会逐条发送）。

用法:
    com3_cmd.py "uptime; free"
    com3_cmd.py --wait 8 "grep MemAvailable /proc/meminfo"
"""
import serial
import sys
import time

PORT, BAUD = 'COM3', 115200


def main():
    args = sys.argv[1:]
    wait = 6
    if '--wait' in args:
        i = args.index('--wait')
        wait = float(args[i + 1])
        args = args[:i] + args[i + 2:]
    cmd = ' '.join(args).strip()
    if not cmd:
        print("usage: com3_cmd.py [--wait N] \"cmd\"")
        return 2

    s = serial.Serial(PORT, BAUD, timeout=0.3, write_timeout=3)
    try:
        s.reset_input_buffer()
    except Exception:
        pass
    # 先敲回车唤醒提示符
    s.write(b'\r')
    time.sleep(0.6)
    s.reset_input_buffer()
    s.write(cmd.encode('latin1', 'replace') + b'\r')
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < wait:
        d = s.read(4096)
        if d:
            buf += d
    s.close()
    txt = bytes(buf).decode('latin1', 'replace')
    print(txt)
    return 0


if __name__ == '__main__':
    sys.exit(main())
