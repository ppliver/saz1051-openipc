#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flash_v2.py — 抢 U-Boot -> 读 env 提取 mtdparts -> 带分区表启 TF 救援 ->
重建 /dev 节点 -> 整写 NAND(rootfs mtd3) -> 读回校验 -> 重启抓 NAND 启动日志。

与 grab_and_flash.py 的关键区别: TF 救援 bootargs 必须带 mtdparts(从 U-Boot env 提取),
否则内核只把整片 NAND 注册成 mtd0, 没有 mtd3, 无法定位 rootfs 分区。
"""
import serial, sys, time, re, os
PORT, BAUD = 'COM3', 115200
TMP = r'D:\projects\saz1051\.tmp'
READY = os.path.join(TMP, 'uboot_ready.txt')
GO = os.path.join(TMP, 'go_flash.txt')
DONE = os.path.join(TMP, 'flash_done.txt')
FAILED = os.path.join(TMP, 'flash_failed.txt')
ENV = os.path.join(TMP, 'uboot_env.txt')
LOG = os.path.join(TMP, 'flash_v2.log')

UBOOT_PROMPT = b'Ebaina#'
SHELL_PROMPT = b'/ #'
UBOOT_PHASE = re.compile(
    rb'(U-Boot |DRAM:|Hit any key|stop autoboot|Autoboot|Press any|to abort|'
    rb'to stop|EBaina)', re.I)

# TF 救援基础 bootargs(不含 mtdparts, 后面拼上)
BOOTARGS_BASE = (b"setenv bootargs console=ttyAMA0,115200 clk_ignore_unused mem=32M "
                 b"root=/dev/mmcblk0p1 rootfstype=vfat rw rootwait init=/bin/sh ")
BOOT = (b"nand read 0x41000000 0x100000 0x400000\r"
        b"bootm 0x41000000\r")

MTD = "/dev/mtd3"
ERASE = "/saz_nand/my_flash_erase"
WRITE = "/saz_nand/my_nandwrite"
ERASE_LEN = "0x6000000"
# ★ new5 = 2026-09-22 稳定性优化镜像：在 new4 基础上换 init v3（safe_reboot + 内存水位守护
#   + 日志轮转 + tmpfs 限额）与关 HLS/audio 的 majestic.yaml（可用内存 0.9MB -> 4.5MB，
#   web 后台不再触发 OOM panic）。new4/new3/new2 依次留作回退。
IMG_CANDIDATES = ["/rootfs_ubi_new5.img", "/rootfs_ubi_new4.img", "/rootfs_ubi_new3.img",
                  "/rootfs_ubi_new2.img", "/rf.img", "/rootfs_ubi_new.img", "/rootfs_ubi.img"]
LOCAL_IMG = r'D:\projects\saz1051\firmware\nand_package\rootfs_ubi_new5.img'


def log(msg):
    line = "[%s] %s\n" % (time.strftime('%H:%M:%S'), msg)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line)
    print(line, end='')


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


def at_uboot(s, buf=b''):
    return UBOOT_PROMPT in buf[-400:]


def ensure_uboot(s, wait=45):
    s.write(b'\r')
    buf = read_for(s, 1.5)
    if at_uboot(s, buf):
        return True
    t0 = time.time()
    while time.time() - t0 < wait:
        chunk = read_for(s, 0.3)
        buf += chunk
        if at_uboot(s, buf):
            return True
        if UBOOT_PHASE.search(chunk):
            s.write(b'\x03')
    return at_uboot(s, buf)


def reach_uboot(s):
    """设备可能停在 U-Boot、Linux shell 或已关机。
    本机 busybox `reboot -f` 无效(内核 reboot 通路不通), 实测 `echo b > /proc/sysrq-trigger` 可用。
    触发重启后必须持续发 ^C 打断 autoboot, 否则会一路 boot 进 NAND 里的旧系统。"""
    buf = read_for(s, 1.5)
    if at_uboot(s, buf):
        log("already at U-Boot prompt")
        return True

    log("at Linux shell; trying reboot -f ...")
    s.write(b'reboot -f\r')
    buf += read_for(s, 4)
    if at_uboot(s, buf):
        log("reboot -f worked")
        return True

    log("reboot -f ineffective -> sysrq b + ctrl-c spam (90s) ...")
    s.write(b'echo b > /proc/sysrq-trigger\r')
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 90:
        try:
            d = s.read(4096)
        except Exception:
            break
        if d:
            buf += d
            if at_uboot(s, buf):
                log("U-Boot reached after sysrq (%.1fs)" % (time.time() - t0))
                return True
        s.write(b'\x03')
        time.sleep(0.15)
    return at_uboot(s, buf)


def send_wait(s, cmd, prompt, wait=60):
    s.reset_input_buffer()
    s.write(cmd)
    buf = b''
    t0 = time.time()
    while time.time() - t0 < wait:
        d = s.read(8192)
        if d:
            buf += d
            if prompt in buf[-200:]:
                break
    return buf


def grab_env(s):
    s.reset_input_buffer()
    s.write(b'printenv\r')
    buf = b''
    t0 = time.time()
    while time.time() - t0 < 10:
        d = s.read(8192)
        if d:
            buf += d
        if b'Ebaina#' in buf[-200:]:
            break
    return buf


def extract_mtdparts(buf):
    # 优先独立 mtdparts= 变量; 其次 bootargs 内含的 mtdparts=
    m = re.search(rb'^mtdparts=(\S+)', buf, re.M)
    if not m:
        m = re.search(rb'mtdparts=(\S+)', buf)
    if m:
        return m.group(1).decode('latin1')
    return None


def boot_tf_rescue(s, mtdparts):
    args = BOOTARGS_BASE
    if mtdparts:
        args += b' ' + ('mtdparts=%s' % mtdparts).encode()
    args += b'\r'
    s.write(args)
    time.sleep(0.6)
    read_for(s, 1.0)
    s.write(BOOT)
    buf = read_for(s, 3.0)
    t0 = time.time()
    while time.time() - t0 < 60:
        buf += read_for(s, 1.0)
        if SHELL_PROMPT in buf[-200:]:
            return True
    return SHELL_PROMPT in buf[-200:]


def find_image(s, wait=180):
    cands = " ".join(IMG_CANDIDATES)
    probe = ('for f in %s; do if [ -f "$f" ]; then echo FOUND:$f; fi; done\r' % cands).encode()
    bases = [c.split('/')[-1] for c in IMG_CANDIDATES]
    t0 = time.time()
    while time.time() - t0 < wait:
        res = send_wait(s, probe, SHELL_PROMPT, wait=8)
        for m in re.finditer(rb'FOUND:(\S+)', res):
            tok = m.group(1).decode('latin1')
            if tok.startswith('/') and any(b in tok for b in bases):
                return tok
        time.sleep(5)
    return None


def main():
    os.makedirs(TMP, exist_ok=True)
    for f in (READY, DONE, FAILED, ENV):
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    log("flash_v2 start: opening %s @ %d" % (PORT, BAUD))
    s = serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=3)
    time.sleep(0.4)
    try:
        s.reset_input_buffer()
    except Exception:
        pass

    # 设备当前可能停在 U-Boot / Linux shell -> 统一由 reach_uboot 拉回 U-Boot
    if not reach_uboot(s):
        log("FAILED: cannot reach U-Boot"); s.close(); sys.exit(1)
    with open(READY, 'w') as f:
        f.write("READY @ %s\n" % time.strftime('%H:%M:%S'))
    log("AT U-Boot prompt")

    # 读 env 提取 mtdparts
    env = grab_env(s)
    with open(ENV, 'w') as f:
        f.write(env.decode('latin1', 'replace'))
    mtdparts = extract_mtdparts(env)
    log("mtdparts extracted: %s" % (mtdparts or "NONE"))
    if not mtdparts:
        log("FAILED: cannot find mtdparts in U-Boot env")
        with open(FAILED, 'w') as f:
            f.write("NO_MTDPARTS @ %s\n" % time.strftime('%H:%M:%S'))
        s.close(); sys.exit(1)

    # 等开刷信号 (若已存在则立即)
    if not os.path.exists(GO):
        log("Holding for go_flash.txt ...")
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            if os.path.exists(GO):
                break
            s.write(b'\r')
            read_for(s, 1.5)
            if not at_uboot(s, read_for(s, 1.0)):
                if not ensure_uboot(s, wait=45):
                    log("FAILED: lost U-Boot"); s.close(); sys.exit(1)
            time.sleep(3)
    if not os.path.exists(GO):
        log("TIMEOUT waiting for go_flash.txt"); s.close(); sys.exit(1)
    log("go_flash.txt seen -> proceeding")

    # 启 TF 救援(带 mtdparts)
    if not boot_tf_rescue(s, mtdparts):
        log("FAILED: TF rescue shell not reached"); s.close(); sys.exit(1)
    log("TF rescue shell reached")

    devbuf = send_wait(s, b"mdev -s 2>/dev/null; ls -l /dev/mtd3 /dev/mtdblock3 2>&1; echo DEV_DONE\r",
                       SHELL_PROMPT, wait=15)
    log("dev nodes:\n%s" % devbuf[-300:].decode('latin1', 'replace'))
    if b'mtd3' not in devbuf:
        log("FAILED: /dev/mtd3 still missing after mdev -s (mtdparts=%s)" % mtdparts)
        with open(FAILED, 'w') as f:
            f.write("NO_MTD3 @ %s\n" % time.strftime('%H:%M:%S'))
        s.close(); sys.exit(1)

    img = find_image(s, wait=180)
    if not img:
        log("FAILED: image not found on TF card. Candidates: %s" % IMG_CANDIDATES)
        with open(FAILED, 'w') as f:
            f.write("IMAGE_MISSING @ %s\n" % time.strftime('%H:%M:%S'))
        s.close(); sys.exit(1)
    log("image found: %s" % img)

    send_wait(s, b"ubidetach -d 0 2>/dev/null; echo DONE_DETACH\r", SHELL_PROMPT, wait=10)
    ebuf = send_wait(s, ("%s %s 0x0 %s; echo ERASE_RC=$?\r" % (ERASE, MTD, ERASE_LEN)).encode(),
                     SHELL_PROMPT, wait=400)
    log("erase tail:\n%s" % ebuf[-400:].decode('latin1', 'replace'))
    if b'ERASE_RC=0' not in ebuf:
        log("FAILED: erase did not return RC=0")
        with open(FAILED, 'w') as f:
            f.write("ERASE_FAIL @ %s\n" % time.strftime('%H:%M:%S'))
        s.close(); sys.exit(1)

    wbuf = send_wait(s, ("%s %s %s; echo WRITE_RC=$?\r" % (WRITE, MTD, img)).encode(),
                     SHELL_PROMPT, wait=600)
    log("write tail:\n%s" % wbuf[-400:].decode('latin1', 'replace'))
    if b'WRITE_RC=0' not in wbuf:
        log("FAILED: write did not return RC=0")
        with open(FAILED, 'w') as f:
            f.write("WRITE_FAIL @ %s\n" % time.strftime('%H:%M:%S'))
        s.close(); sys.exit(1)

    # 读回校验(全量: 镜像整长 dd -> md5, 避免只校前 1MB 漏掉弱块)
    nblk = (os.path.getsize(LOCAL_IMG) + 131071) // 131072 if os.path.exists(LOCAL_IMG) else 136
    log("full readback verify over %d blocks ..." % nblk)
    send_wait(s, b"busybox dd if=/dev/mtd3 bs=131072 count=%d 2>/dev/null | busybox md5sum > /tmp/rb.md5; echo RB_DONE\r" % nblk,
              SHELL_PROMPT, wait=300)
    send_wait(s, b"busybox dd if=%s bs=131072 count=%d 2>/dev/null | busybox md5sum > /tmp/src.md5; echo SRC_DONE\r" % (img.encode(), nblk),
              SHELL_PROMPT, wait=300)
    cmpbuf = send_wait(s, b"diff /tmp/rb.md5 /tmp/src.md5 >/dev/null 2>&1 && echo RB_MATCH || echo RB_MISMATCH\r",
                       SHELL_PROMPT, wait=30)
    ok = b'RB_MATCH' in cmpbuf
    log("readback verify(full): %s" % ('RB_MATCH' if ok else 'RB_MISMATCH'))

    if not ok:
        # 逐 PEB 定位坏块(弱块会在这里现形)
        loc = ("i=0; while [ $i -lt %d ]; do "
               "a=$(busybox dd if=/dev/mtd3 bs=131072 skip=$i count=1 2>/dev/null | busybox md5sum | cut -c1-8); "
               "b=$(busybox dd if=%s bs=131072 skip=$i count=1 2>/dev/null | busybox md5sum | cut -c1-8); "
               "if [ \"$a\" != \"$b\" ]; then echo BADPEB $i $a $b; fi; i=$((i+1)); done; echo LOC_DONE\r"
               % (nblk, img)).encode()
        lbuf = send_wait(s, loc, SHELL_PROMPT, wait=600)
        bad = re.findall(rb'BADPEB (\d+) (\S+) (\S+)', lbuf)
        log("bad PEBs: %s" % (", ".join(x[0].decode() for x in bad) or "none"))
        for p, a, b in bad:
            log("  PEB %s: nand=%s file=%s" % (p.decode(), a.decode(), b.decode()))
        with open(FAILED, 'w') as f:
            f.write("VERIFY_MISMATCH @ %s pebs=%s\n" % (time.strftime('%H:%M:%S'),
                                                        ",".join(x[0].decode() for x in bad)))
        log("FAILED: full readback mismatch -> aborting (NAND weak block)")
        s.close()
        sys.exit(1)

    log("rebooting into NAND ...")
    s.write(b"reboot -f\r")
    boot = read_for(s, 55)
    log("NAND boot tail:\n%s" % boot[-2200:].decode('latin1', 'replace'))

    with open(DONE, 'w') as f:
        f.write("FLASH_DONE @ %s img=%s mtdparts=%s\n" % (time.strftime('%H:%M:%S'), img, mtdparts))
    log("FLASH DONE")
    s.close()
    sys.exit(0)


if __name__ == '__main__':
    main()
