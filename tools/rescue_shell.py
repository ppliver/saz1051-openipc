#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rescue_shell.py — 当 NAND 里的 init 脚本把系统搞成 panic 循环时的救命通道。

思路：不改 NAND 内容、不刷机，只在 U-Boot 层把 bootargs 的 init 换成 /bin/sh，
     让内核直接挂载 NAND 的 UBIFS 根并给一个裸 shell（不执行 oipc_init.sh），
     然后通过串口 shell 把坏文件改回去。整个流程几十秒，远快于重刷。

用法:
    rescue_shell.py                      # 进 shell 后停住，等人工/调用方接管
    rescue_shell.py --cmd "cp a b; sync" # 进 shell 后自动执行一串命令再重启
"""
import os
import re
import serial
import sys
import time

PORT, BAUD = 'COM3', 115200
UBOOT_PROMPT = b'Ebaina#'
UBOOT_PHASE = re.compile(
    rb'(U-Boot |DRAM:|Hit any key|stop autoboot|Autoboot|Press any|to abort|'
    rb'to stop|EBaina)', re.I)

# 与 NAND 系统完全一致的 bootargs，只把 init 换成 /bin/sh
BOOTARGS = (b"setenv bootargs console=ttyAMA0,115200 clk_ignore_unused mem=32M "
            b"root=ubi0:ubifs rootfstype=ubifs rw ubi.mtd=3 "
            b"mtdparts=nand:512K(u-boot.bin),512K(env.bin),4M(uImage),96M(rootfs.ubifs) "
            b"init=/bin/sh panic=10\r")
BOOT = (b"nand read 0x41000000 0x100000 0x400000\r"
        b"bootm 0x41000000\r")


def log(m):
    print("[%s] %s" % (time.strftime('%H:%M:%S'), m), flush=True)


def read_for(s, t):
    t0 = time.time()
    out = []
    while time.time() - t0 < t:
        try:
            d = s.read(8192)
        except Exception:
            break
        if d:
            out.append(d)
    return b''.join(out)


def at_uboot(buf):
    return UBOOT_PROMPT in buf[-400:]


def reach_uboot(s):
    buf = read_for(s, 1.5)
    if at_uboot(buf):
        log("already at U-Boot")
        return True
    log("trying reboot -f ...")
    s.write(b'reboot -f\r')
    buf += read_for(s, 4)
    if at_uboot(buf):
        log("reboot -f worked")
        return True
    log("reboot -f ineffective -> sysrq b + ctrl-c spam (120s)")
    s.write(b'echo b > /proc/sysrq-trigger\r')
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            d = s.read(4096)
        except Exception:
            break
        if d:
            buf += d
            if at_uboot(buf):
                log("U-Boot reached after sysrq (%.1fs)" % (time.time() - t0))
                return True
            if UBOOT_PHASE.search(d):
                for _ in range(3):
                    s.write(b'\x03')
                    time.sleep(0.4)
    return at_uboot(buf)


def main():
    cmd = None
    if '--cmd' in sys.argv:
        cmd = sys.argv[sys.argv.index('--cmd') + 1]

    s = serial.Serial(PORT, BAUD, timeout=0.3, write_timeout=3)
    try:
        s.reset_input_buffer()
    except Exception:
        pass

    if not reach_uboot(s):
        log("FAILED: cannot reach U-Boot")
        s.close()
        return 1

    time.sleep(0.5)
    s.write(b'\r')
    read_for(s, 1.0)
    s.write(BOOTARGS)
    read_for(s, 1.5)
    s.write(BOOT)
    log("booting NAND kernel with init=/bin/sh ...")

    # 等裸 shell 出现（UBIFS 挂载 + 内核启动约 25-40s）
    buf = b''
    t0 = time.time()
    got = False
    while time.time() - t0 < 90:
        d = s.read(4096)
        if d:
            buf += d
            if b'/ #' in buf[-300:] or b'~ #' in buf[-300:]:
                got = True
                break
    if not got:
        # 再敲一下回车试探
        s.write(b'\r')
        buf += read_for(s, 3)
        got = (b'/ #' in buf[-300:]) or (b'~ #' in buf[-300:])
    log("shell prompt: %s" % ("YES" if got else "NO"))
    if not got:
        tail = buf[-2000:].decode('latin1', 'replace')
        log("--- tail ---\n" + tail)
        s.close()
        return 2

    if cmd:
        log("executing: %s" % cmd)
        s.write(cmd.encode() + b'\r')
        buf += read_for(s, 8)
        log("--- output ---")
        log(buf[-1500:].decode('latin1', 'replace'))
        time.sleep(2)
        buf += read_for(s, 5)
        log("--- post ---")
        log(buf[-1500:].decode('latin1', 'replace'))
    else:
        log("shell ready (no --cmd given); leaving session open 60s")
        read_for(s, 60)

    s.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
