#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""oipc_build_rootfs.py — 在 177 上把 OpenIPC 官方 rootfs 加工为 SAZ1051 的 UBIFS(NAND) 镜像。

★ 2026-09-21 重写要点（上一版交付包踩的坑）：
  上一版交付的 firmware/nand_package 用了「设备 TF 卡那套厂商 rootfs tarball」当基线(65MB)，
  结果：缺 /proc /sys /sbin → init 挂 proc 静默失败；/bin 里 45MB busybox 实体副本；
  系统 musl 太旧（无 crypt_r/nextafter/lrintf）→ majestic 起不来。
  **正确基线 = /home/zhang/oipc-saz/in/rootfs（OpenIPC 官方，21MB）**，它自带：
    - OpenIPC init + /etc/init.d/S*   - /usr/bin/majestic + /etc/majestic.yaml
    - MPP 用户库 libss_mpi*.so / libot_mpi_isp.so / libaiisp.so
    - **42 个预编译 open_*.ko（含 open_base/vb/sys/vpss/vi/isp/venc/h264e/h265e/jpege/mmz/mipi_rx/pm）
      且已在 lib/modules/5.10.221/modules.dep 中**（旧版的 modprobe shim 坑自动消失）
  本脚本在此基础上注入「只有我们板子才有」的东西，并自带一个 SAZ1051 专用 init。

输入(177 上):
  /home/zhang/oipc-saz/in/rootfs                   OpenIPC 官方 rootfs 树（基线，21MB）
  /home/zhang/oipc-saz/stage/modules/lib/modules/5.10.221   本内核 CI 编译的内树模块(PM=n)
  /home/zhang/oipc-saz/nandpkg/base/{opt,system}              厂商/自研件：
       opt/oipc/{load_hisilicon,sbin/*,sensors/libsns_os05l10.so}
       opt/tools/{bringup_wifi.sh,sensor_mux.sh,sazreg.ko,sensor_clk_val,...}
       opt/saz_wifi/*.ko
       system/etc/ws73_cfg.ini
       etc/ws73/  (ws73.bin + btc_cali.bin + wifi_cali.bin + wow.bin; plat_soc.ko 必需)
  /home/zhang/oipc-saz/in/{os05l10.ini,wifi/{wifi_soc_v15.ko,wpa_supplicant.conf}}

产出(177 上 /home/zhang/oipc-saz/stage/):
  rootfs_final/     加工后的根文件系统树
  rootfs.ubifs      UBIFS 镜像
  rootfs_ubi.img    ubinize 后的 UBI 卷（写 rootfs mtd 分区）
  rootfs.manifest   SHA256 + 尺寸清单

NAND 参数(DS35Q1GA)：page 2048 / PEB 131072(128K) / LEB 126976(124K)
  mkfs.ubifs -m 2048 -e 126976 -c 760 -x zlib  +  ubinize -p 131072 -m 2048 -s 2048
  卷名必须 ubifs（bootargs root=ubi0:ubifs）。
"""
import hashlib
import os
import sys

import paramiko

HOST, USER, PW = "192.168.219.177", "zhang", "admin888"
SAZ = "/home/zhang/oipc-saz"
SRC = "%s/in/rootfs" % SAZ                     # ★ OpenIPC 官方基线
BASE = "%s/nandpkg/base" % SAZ                 # 厂商/自研件
WORK = "%s/rootfs_final" % SAZ
STAGE = "%s/stage" % SAZ
MODULES = "%s/modules/lib/modules/5.10.221" % STAGE
INI = "%s/in/os05l10.ini" % SAZ
WIFI_DIR = "%s/in/wifi" % SAZ
# ★ 真源策略：libsns 直接取构建产物（不再用 nandpkg 里的副本，副本会漂移成旧版）；
#   init / majestic.yaml 取 nandpkg/base（与设备上验证过的文件同 md5）。
LIBSNS = ("%s/openhisilicon/libraries/sensor/hi3516cv6xx/omnivision_os05l10"
          "/libsns_os05l10.so") % SAZ
INIT_SH_FILE = "%s/opt/oipc/oipc_init.sh" % BASE
YAML_FILE = "%s/etc/majestic.yaml" % BASE
# 构建期断言：与设备上实测通过的指纹一致，不符即拒绝出包（防"镜像装旧件"）
EXPECT_MD5 = {
    "libsns": "9378f86e6d8b5accc365f3775e6ad43d",  # I2C 修复版(.i2c_dev=0)
    # v4: 在 v3 基础上修"600s 有界等待误杀正常 majestic -> 每 10 分钟整机重启"的回归
    "init":   "a9d21478abc2d75d991e4ecf272073df",
    "yaml":   "15c4ee05c4b7d8a1610bf0fe14c5ea73",  # 关 HLS/audio + nightMode.irCutEnabled=false（本板无 IRCUT）
}

# ----------------------------------------------------------------------------
# SAZ1051 专用 init（不用 stock /init：它要求 CONFIG_OVERLAY_FS=y + 第二个 UBI 卷
# rootfs_data —— 本内核 `# CONFIG_OVERLAY_FS is not set`，会 `exit 1` 直接 panic）
# ----------------------------------------------------------------------------
# init 脚本不再内嵌：真源 = INIT_SH_FILE（见 main() 步骤 4）

# ----------------------------------------------------------------------------
# WS73 WiFi 拉起（复用已验证序列；绝对路径 insmod 避免与内树 cfg80211/mac80211 撞名）
# ----------------------------------------------------------------------------
WIFI_SH = r"""#!/bin/sh
# SAZ1051 WS73(Hi3873V100) USB WiFi 拉起
# 只用 /opt/saz_wifi 里的厂商 ko（含 cfg80211_v20/mac80211 的厂商构建版），
# 绝不加载内树 cfg80211/mac80211 —— 两者符号会撞。
K=/opt/saz_wifi
LOG=/tmp/wifi.log

log() { echo "$*"; }

[ -d "$K" ] || { log "WiFi: $K missing"; exit 1; }
if [ -e /sys/class/net/wlan0/phy80211 ]; then log "WiFi already up"; exit 0; fi

mkdir -p /system/etc
[ -f /system/etc/ws73_cfg.ini ] || cp /etc/ws73_cfg.ini /system/etc/ws73_cfg.ini 2>/dev/null

# wpa_supplicant 的 ctrl_iface 需要 unix socket -> 必须在可写文件系统上
mkdir -p /var/run/wpa_supplicant

mount -t tmpfs tmpfs /sys 2>/dev/null   # 仅当 /sys 尚未挂载时无害；已有挂载则失败
mount -t sysfs sysfs /sys 2>/dev/null

# 重新插一次前先清干净（muxfix mode=1 skip_usb=1 只允许插一次）
rmmod wifi_soc_v15 2>/dev/null; rmmod plat_soc 2>/dev/null; rmmod muxfix 2>/dev/null

insmod $K/rfkill.ko          2>&1 | sed 's/^/  /'
insmod $K/libarc4.ko         2>&1 | sed 's/^/  /'
insmod $K/firmware_class.ko  2>&1 | sed 's/^/  /'
insmod $K/cfg80211_v20.ko    2>&1 | sed 's/^/  /'
insmod $K/mac80211.ko        2>&1 | sed 's/^/  /'
sleep 2
insmod $K/muxfix.ko mode=1 skip_usb=1 2>&1 | sed 's/^/  /'
sleep 2
insmod $K/plat_soc.ko        2>&1 | sed 's/^/  /'
if [ -f $K/wifi_soc_v15.ko ]; then
	insmod $K/wifi_soc_v15.ko 2>&1 | sed 's/^/  /'
else
	insmod $K/wifi_soc.ko     2>&1 | sed 's/^/  /'
fi

i=0
while [ $i -lt 90 ]; do
	[ -e /sys/class/net/wlan0/phy80211 ] && break
	sleep 2
	i=$((i+1))
done
if [ ! -e /sys/class/net/wlan0/phy80211 ]; then
	log "WiFi FAIL: wlan0/phy80211 not present"; exit 1
fi

ifconfig wlan0 0.0.0.0 up
wpa_supplicant -B -Dnl80211 -iwlan0 -c /etc/wireless/wpa_supplicant.conf -f /tmp/wp.log 2>/dev/null
i=0
while [ $i -lt 45 ]; do
	wpa_cli -iwlan0 status 2>/dev/null | grep -q COMPLETED && break
	sleep 2
	i=$((i+1))
done
ifconfig wlan0 192.168.6.178 netmask 255.255.255.0
route add default gw 192.168.6.1 wlan0 2>/dev/null
log "WiFi up: $(ifconfig wlan0 | grep inet)"
"""

PRUNE_S = ["S90zerotier", "S97qrscan", "S98vtun", "S98wireguard", "S49ntpd"]


def client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=30,
              look_for_keys=False, allow_agent=False)
    return c


def sh(c, cmd, quiet=False, timeout=3600):
    if not quiet:
        print("### " + cmd.splitlines()[0][:150], flush=True)
    _i, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if not quiet:
        if o:
            print(o.rstrip())
        if e:
            print("[stderr] " + e.rstrip()[:3000])
    return o, e


def put_text(c, text, remote):
    """把文本写到 177 上（走 base64，避免引号/换行被 shell 吃掉）。"""
    import base64
    b = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sh(c, "mkdir -p $(dirname %s)" % remote, quiet=True)
    sh(c, "echo '%s' | base64 -d > %s" % (b, remote), quiet=True)
    sh(c, "chmod 755 %s" % remote, quiet=True)


def main():
    c = client()
    try:
        print("=== 0. 前置检查 ===", flush=True)
        sh(c, "test -d %s && echo 'OpenIPC 基线 OK'" % SRC)
        print("=== 0b. 关键件指纹断言（不符即拒出包）===", flush=True)
        fp = {}
        for k, path in (("libsns", LIBSNS), ("init", INIT_SH_FILE), ("yaml", YAML_FILE)):
            o, e = sh(c, "md5sum %s 2>&1" % path)
            fp[k] = (o.split() or ["MISSING"])[0]
            ok = "OK " if fp[k] == EXPECT_MD5[k] else "MISMATCH!"
            print("    %-7s %s  %s (expect %s)" % (k, ok, fp[k], EXPECT_MD5[k]))
        bad = [k for k in fp if fp[k] != EXPECT_MD5[k]]
        if bad:
            raise SystemExit("ABORT: 关键件指纹不符 %s —— 先把真源同步到 177 再构建" % bad)
        sh(c, "test -f %s && echo 'ini OK'" % INI)
        sh(c, "test -d %s && echo 'CI 模块 OK' || echo 'CI 模块 MISSING'" % MODULES)
        sh(c, "test -f %s/wifi_soc_v15.ko && echo 'wifi ko OK'" % WIFI_DIR)
        sh(c, "test -d %s/opt/oipc && echo 'base/oipc OK'" % BASE)
        sh(c, "which mkfs.ubifs ubinize depmod")

        print("=== 1. 复制 OpenIPC 基线 -> 工作树 ===", flush=True)
        sh(c, "rm -rf %s && cp -a %s %s && du -sh %s" % (WORK, SRC, WORK, WORK))

        print("=== 2. 合并 CI 内树模块（保留 42 个 open_*.ko）===", flush=True)
        sh(c, "rm -rf %s/lib/modules/5.10.221/source %s/lib/modules/5.10.221/build"
              % (WORK, WORK))
        sh(c, "cp -a %s/. %s/lib/modules/5.10.221/ && echo merged" % (MODULES, WORK))
        sh(c, "rm -rf %s/lib/modules/5.10.221/source %s/lib/modules/5.10.221/build"
              % (WORK, WORK))
        sh(c, "ls %s/lib/modules/5.10.221/hisilicon/ | wc -l" % WORK)

        print("=== 3. 注入厂商/自研件 ===", flush=True)
        sh(c, "mkdir -p %s/opt/oipc %s/opt/tools %s/opt/saz_wifi %s/etc/sensors "
              "%s/etc/wireless %s/usr/lib/sensors %s/system/etc"
              % (WORK, WORK, WORK, WORK, WORK, WORK, WORK))
        # load_hisilicon + sbin 工具
        sh(c, "cp -a %s/opt/oipc/load_hisilicon %s/opt/oipc/" % (BASE, WORK))
        sh(c, "cp -a %s/opt/oipc/sbin %s/opt/oipc/ && echo 'oipc/sbin OK'" % (BASE, WORK))
        # 自研工具（含 sensor_mux.sh / sazreg.ko / sensor_clk_val）
        sh(c, "cp -a %s/opt/tools/. %s/opt/tools/ && rm -f %s/opt/tools/bringup_wifi.sh && echo 'tools OK'"
              % (BASE, WORK, WORK))
        # WS73 ko（厂商 5 件 + wifi_soc_v15）
        sh(c, "cp -a %s/opt/saz_wifi/. %s/opt/saz_wifi/ && cp %s/wifi_soc_v15.ko %s/opt/saz_wifi/ "
              "&& ls %s/opt/saz_wifi/" % (BASE, WORK, WIFI_DIR, WORK, WORK))
        # wpa 配置
        sh(c, "cp %s/wpa_supplicant.conf %s/etc/wireless/ && echo 'wpa conf OK'" % (WIFI_DIR, WORK))
        # ★ wpa_supplicant / wpa_cli（OpenIPC 官方 rootfs 完全缺这俩二进制，
        #   之前运行态只能从 TF 卡临时借 -> 重启即丢。这里从 BASE/usr/bin 拷进 rootfs，
        #   让 WiFi 关联自持，不再依赖 TF 卡）。静态链接 ARM ELF，权限步骤会置 755。
        sh(c, "rm -f %s/usr/bin/wpa_supplicant %s/usr/bin/wpa_cli; "
              "cp -a %s/usr/bin/wpa_supplicant %s/usr/bin/wpa_supplicant; "
              "cp -a %s/usr/bin/wpa_cli %s/usr/bin/wpa_cli; "
              "ls -l %s/usr/bin/wpa_supplicant %s/usr/bin/wpa_cli"
              % (WORK, WORK, BASE, WORK, BASE, WORK, WORK, WORK))
        # sensor：ini + 用户态驱动库
        sh(c, "cp %s %s/etc/sensors/os05l10.ini && cp %s %s/etc/os05l10.ini"
              " && cp %s %s/system/etc/os05l10.ini && echo 'ini OK'" % (INI, WORK, INI, WORK, INI, WORK))
        sh(c, "cp %s %s/usr/lib/sensors/ && echo 'libsns OK'" % (LIBSNS, WORK))
        # WS73 射频参数
        sh(c, "cp %s/system/etc/ws73_cfg.ini %s/system/etc/ && cp %s/system/etc/ws73_cfg.ini %s/etc/"
              " && echo 'ws73_cfg OK'" % (BASE, WORK, BASE, WORK))
        # ★ WS73 射频固件（plat_soc.ko 在加载时 filp_open [/etc/ws73/ws73.bin]，
        #   缺失则 wlan_power_open_cmd 死等 -> 整个启动卡在 D 态）。必须整目录拷进 /etc/ws73/。
        sh(c, "mkdir -p %s/etc/ws73" % WORK)
        sh(c, "if [ -d %s/etc/ws73 ]; then cp -a %s/etc/ws73/. %s/etc/ws73/ && "
              "echo 'ws73 fw OK ($(ls %s/etc/ws73/))'; else echo 'ws73 fw MISSING in base'; fi"
              % (BASE, BASE, WORK, WORK))

        print("=== 4. 写 init + WiFi 脚本 + majestic.yaml ===", flush=True)
        # init 必须走文件（含 warm-up 见好就收 + clear_isp + 监督循环有界等待 + reboot 兜底）
        sh(c, "cp %s %s/opt/oipc/oipc_init.sh && chmod 755 %s/opt/oipc/oipc_init.sh && "
              "sh -n %s/opt/oipc/oipc_init.sh && echo 'init syntax OK'"
              % (INIT_SH_FILE, WORK, WORK, WORK))
        put_text(c, WIFI_SH, "%s/opt/tools/bringup_wifi.sh" % WORK)
        # ★ majestic.yaml：OpenIPC 官方默认 video0+video1+jpeg 全开 -> VB 池 35.6MB > 32MB
        #   MMZ -> ERR_VENC_NO_MEM + OOM。必须注入已验证配置。
        sh(c, "cp %s %s/etc/majestic.yaml && chmod 644 %s/etc/majestic.yaml && "
              "md5sum %s/etc/majestic.yaml" % (YAML_FILE, WORK, WORK, WORK))

        print("=== 5. 精简 /etc/init.d（不需要的服务）===", flush=True)
        for s in PRUNE_S:
            sh(c, "rm -f %s/etc/init.d/%s" % (WORK, s), quiet=True)
        sh(c, "ls %s/etc/init.d/" % WORK)

        print("=== 6. 权限铁律：目录755 / ELF与#!脚本755 / 其余644 ===", flush=True)
        sh(c, "python3 - <<'PYEOF'\n"
              "import os\n"
              "W = %r\n"
              "nd = nf = nx = 0\n"
              "for root, dirs, files in os.walk(W):\n"
              "    os.chmod(root, 0o755); nd += 1\n"
              "    for f in files:\n"
              "        p = os.path.join(root, f)\n"
              "        if os.path.islink(p): nx += 1; continue\n"
              "        try:\n"
              "            with open(p, 'rb') as fh: h = fh.read(4)\n"
              "        except Exception: continue\n"
              "        os.chmod(p, 0o755 if (h[:4] == b'\\x7fELF' or h[:2] == b'#!') else 0o644)\n"
              "        nf += 1\n"
              "print('perm: dirs=%%d files=%%d symlinks-skipped=%%d' %% (nd, nf, nx))\n"
              "PYEOF" % WORK)

        print("=== 6b. 写入 root:admin888 口令（离线改 /etc/shadow，避免依赖设备 chpasswd）===", flush=True)
        # openssl passwd -1 = md5 crypt($1$)，与已验证可工作的运行态格式一致（musl 支持）。
        # 整段在 177 上执行：openssl 生成哈希 -> python 改写 rootfs 的 /etc/shadow。
        sh(c, "H=$(openssl passwd -1 admin888) && python3 - \"$H\" <<'PYEOF'\n"
              "import sys, os\n"
              "h = sys.argv[1]\n"
              "W = %r\n"
              "sp = os.path.join(W, 'etc/shadow')\n"
              "lines = []\n"
              "if os.path.exists(sp):\n"
              "    with open(sp) as f: lines = f.read().splitlines()\n"
              "out = []; seen = False\n"
              "for ln in lines:\n"
              "    if ln.startswith('root:'):\n"
              "        out.append('root:' + h + ':0:0:99999:7:::'); seen = True\n"
              "    else:\n"
              "        out.append(ln)\n"
              "if not seen: out.append('root:' + h + ':0:0:99999:7:::')\n"
              "with open(sp, 'w') as f: f.write('\\n'.join(out) + '\\n')\n"
              "os.chmod(sp, 0o600)\n"
              "print('shadow root set, hash=' + h[:20] + '...')\n"
              "PYEOF" % WORK)
        sh(c, "grep '^root:' %s/etc/shadow" % WORK)

        print("=== 7. depmod（把 open_*.ko 也收进 modules.dep）===", flush=True)
        sh(c, "depmod -b %s 5.10.221 2>&1 | tail -5; grep -c 'open_' "
              "%s/lib/modules/5.10.221/modules.dep" % (WORK, WORK))

        print("=== 8. mkfs.ubifs + ubinize ===", flush=True)
        sh(c, "cd %s && rm -f rootfs.ubifs rootfs_ubi.img && mkfs.ubifs -r %s -m 2048 -e 126976 "
              "-c 760 -x zlib -o %s/rootfs.ubifs 2>&1 | tail -6" % (STAGE, WORK, STAGE))
        sh(c, "cat > %s/ubinize.cfg <<'UBIEOF'\n[ubifs]\nmode=ubi\nimage=%s/rootfs.ubifs\n"
              "vol_id=0\nvol_type=dynamic\nvol_name=ubifs\nvol_flags=autoresize\nUBIEOF"
              % (STAGE, STAGE))
        sh(c, "cd %s && ubinize -o %s/rootfs_ubi.img -p 131072 -m 2048 -s 2048 %s/ubinize.cfg "
              "2>&1 | tail -6" % (STAGE, STAGE, STAGE))

        print("=== 8b. 镜像内关键件回读（确认装配正确）===", flush=True)
        sh(c, "md5sum %s/usr/lib/sensors/libsns_os05l10.so %s/opt/oipc/oipc_init.sh "
              "%s/etc/majestic.yaml" % (WORK, WORK, WORK))

        print("=== 9. 产出 ===", flush=True)
        sh(c, "ls -l %s/rootfs.ubifs %s/rootfs_ubi.img && sha256sum %s/rootfs.ubifs "
              "%s/rootfs_ubi.img | tee %s/rootfs.manifest" % (STAGE, STAGE, STAGE, STAGE, STAGE))
        sh(c, "du -sh %s; du -sh %s/* 2>/dev/null | sort -rh | head -12" % (WORK, WORK))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
