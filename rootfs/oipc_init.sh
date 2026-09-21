#!/bin/sh
# SAZ1051 OpenIPC 验证用 init(PID1) v3 —— 2026-09-21
# 内核 cmdline: root=/dev/mmcblk0p1 rootfstype=vfat init=/opt/oipc/oipc_init.sh panic=10
#
# 与 v2 的关键差异(每一条都有实测依据):
#  [1] busybox applet farm(/tmp/b) + /opt/oipc/sbin shim, 补全 modprobe/tr/sort/basename 等。
#      本 TF rootfs 的 modules.dep 不含 hisilicon/open_* -> busybox 按名 modprobe 必失败,
#      故 /opt/oipc/sbin/modprobe 改成按绝对路径直插。
#  [2] WiFi 必须用厂商预编译 cfg80211_v20.ko + mac80211.ko(含厂商 SAZNL 补丁)。
#      用内核自带主线 cfg80211 -> wifi_soc 在 cfg80211_register_wdev 空指针 panic(已实测)。
#  [3] mmz 几何: 本板 DDR 仅 64MB(dmesg 53080K/65536K)
#      -> totalmem=64M / osmem=32M -> mmz=0x42000000, 32M
#      (与原厂 load3516cv608 的 64M 变体、OpenIPC load_hisilicon 缺省值均一致)
#  [4] 全部输出同时打控制台: 上一轮 panic 复位导致 vfat/tmpfs 日志全丢, 只能靠串口。
#  [5] 顺序复刻卡上已验证可跑的 /linuxrc: sensor_clk 补丁 -> WiFi(后台) -> MPP -> sensor_mux -> 出图
PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH
BB=/bin/busybox

$BB mount -t proc proc /proc 2>/dev/null
$BB mount -t sysfs sysfs /sys 2>/dev/null
$BB mkdir -p /dev /tmp /var/run /var/log /system/etc /dev/pts
$BB mount -t tmpfs tmpfs /dev 2>/dev/null
$BB mount -t tmpfs tmpfs /tmp 2>/dev/null
$BB mount -t devpts devpts /dev/pts 2>/dev/null
# vfat 不能承载 unix socket -> wpa ctrl_iface 必须落 tmpfs
$BB mount -t tmpfs tmpfs /var/run 2>/dev/null
$BB mkdir -p /var/run/wpa_supplicant
echo /tmp/b/mdev > /proc/sys/kernel/hotplug 2>/dev/null

# --- busybox applet farm(vfat 不支持 symlink -> 建在 tmpfs) ---
$BB mkdir -p /tmp/b
for a in $($BB --list 2>/dev/null); do $BB ln -sf $BB /tmp/b/$a 2>/dev/null; done
export PATH=/opt/oipc/sbin:/tmp/b:/bin:/sbin:/usr/bin:/usr/sbin
export LD_LIBRARY_PATH=/opt/oipc/lib:/opt/lib:/lib:/usr/lib

$BB mdev -s 2>/dev/null
# plat_soc 需要 /system/etc/ws73_cfg.ini(提供 MAC/射频参数), 必须先于驱动存在
[ -f /system/etc/ws73_cfg.ini ] || $BB cp /etc/ws73_cfg.ini /system/etc/ws73_cfg.ini 2>/dev/null

say() { echo "$*"; }

say "================================================================"
say " SAZ1051 OpenIPC verify init v3   ($(date 2>/dev/null))"
say " kernel : $(cat /proc/version)"
say " cmdline: $(cat /proc/cmdline)"
say " iomem  : $(grep 'System RAM' /proc/iomem | head -1)"
say " shims  : modprobe=$(which modprobe 2>/dev/null) fw_printenv=$(which fw_printenv 2>/dev/null)"
say "================================================================"

# ---------- 1) sensor clk 寄存器补丁(原厂 swapp 时序, 可选) ----------
if [ -f /opt/tools/SENSOR_CLK_POKE ]; then
	V=0x4010
	[ -f /opt/tools/sensor_clk_val ] && V=$(cat /opt/tools/sensor_clk_val)
	say "[1] sensor_clk poke 0x11018440 <- $V"
	insmod /opt/tools/sazreg.ko mode=2 raddr=0x11018440 rval=$V 2>&1
	rmmod sazreg 2>/dev/null
else
	say "[1] sensor_clk poke: SKIP (no /opt/tools/SENSOR_CLK_POKE)"
fi

# ---------- 2) WiFi(后台; 用已验证的 bringup_wifi.sh) ----------
say "[2] bringup_wifi.sh (prebuilt cfg80211_v20 + mac80211) -> /tmp/wifi.log"
/opt/tools/bringup_wifi.sh > /tmp/wifi.log 2>&1 &

# ---------- 3) MPP(open_*.ko) + sensor ----------
# ★ 内核侧必须传 open_sys_config.ko 内置 g_sensor_list 里存在的名字!
#   实测(2026-09-21): OpenIPC 预编译 open_sys_config.ko 的 g_sensor_list =
#     {os04d10, sc4336p, sc450ai, sc500ai, sc431hai, gc4023, bt1120/bt656/bt601}
#   —— **没有 os05l10**, 传 os05l10 会打印
#     "parse sensor[os05l10] failed!" 并放弃 sensor 初始化。
#   注意: 把 os05l10 加进那张表是不可能的(模块是预编译二进制)。
#   对照厂商做法(实测): /opt/ko/load3516cv610/load3516cv610_20s_debug 里
#     SNS_TYPE0=sc4336p  —— 厂商给内核也是传 sc4336p(通用 MIPI/pinmux 预设),
#   真正的 OS05L10 走线/时钟时序由下面第 4 步 sensor_mux.sh 直写寄存器补齐
#   (0x17940040/0x17940050/0x17940098/0x1794009c/0x11018440)。
#   userspace 侧仍然是 os05l10: /etc/sensors/os05l10.ini(DllFile=libsns_os05l10.so)
#   + /usr/lib/sensors/libsns_os05l10.so + /opt/ceanic/scene/param/sensor_os05l10。
KERNEL_SENSOR=sc4336p
say "[3] load_hisilicon -i -sensor0 $KERNEL_SENSOR   (userspace sensor=os05l10)"
/opt/oipc/load_hisilicon -i -sensor0 $KERNEL_SENSOR 2>&1 | while read l; do echo "    | $l"; done
say "[3] open_* loaded: $(lsmod 2>/dev/null | grep -c '^open_')"
export SENSOR=os05l10

# ---------- 4) sensor 走线/时钟时序(必须落在 MPP 之后) ----------
if [ -x /opt/tools/sensor_mux.sh ]; then
	say "[4] sensor_mux.sh"
	/opt/tools/sensor_mux.sh 2>&1 | while read l; do echo "    | $l"; done
else
	say "[4] sensor_mux.sh: SKIP (missing)"
fi

# ---------- 5) majestic(OpenIPC RTSP) ----------
say "[5] start majestic -s  (SENSOR=$SENSOR, cfg=/etc/majestic.yaml)"
# ★ 必须显式用 OpenIPC 自带 musl loader 启动, 不能直接 exec:
#   本 TF rootfs 的 /lib/ld-musl-arm.so.1 是厂商精简 musl(1651 个导出符号, 无
#   crypt_r / nextafter / lrintf / crypt), 而 majestic 的 PT_INTERP 硬编码
#   为 /lib/ld-musl-arm.so.1 -> 启动即报
#     "Error relocating /opt/oipc/majestic: crypt_r: symbol not found"
#   OpenIPC 的完整 musl(501KB, 1719 个导出符号)在 /opt/oipc/lib/。
#   把 loader 作为 argv[0] 显式传入即可绕过 PT_INTERP, 且完全不动 /lib 系统 musl
#   (2026-09-21 实测: `ld-musl-arm.so.1 majestic --version` -> 正常打印版本)。
OIPC_LD=/opt/oipc/lib/ld-musl-arm.so.1
if [ -x "$OIPC_LD" ]; then
	LD_LIBRARY_PATH=/opt/oipc/lib "$OIPC_LD" /opt/oipc/majestic -s > /tmp/majestic.log 2>&1 &
else
	/opt/oipc/majestic -s > /tmp/majestic.log 2>&1 &
fi
sleep 15
say "[5] majestic pid: $(pidof majestic 2>/dev/null || echo none)"
say "[5] listen 554 : $(netstat -ltn 2>/dev/null | grep ':554' || echo none)"
say "[5] majestic.log:"
head -25 /tmp/majestic.log 2>/dev/null | while read l; do echo "    | $l"; done

# ---------- 6) 出图判据 ----------
say "[6] /proc/umap/vi (head 45)"
head -45 /proc/umap/vi 2>&1 | while read l; do echo "    | $l"; done
say "[6] lsmod: open_*=$(lsmod 2>/dev/null | grep -c '^open_')  wifi=$(lsmod 2>/dev/null | grep -c -E '^(wifi_soc|plat_soc|muxfix|cfg80211|mac80211) ')"
say "[6] wlan0  : $(ifconfig wlan0 2>/dev/null | grep inet || echo none)"
say "[6] wifi.log:"
tail -20 /tmp/wifi.log 2>/dev/null | while read l; do echo "    | $l"; done

# ---------- 7) dropbear(SSH 取日志) + 交互 shell ----------
[ -x /usr/sbin/dropbear ] && { mkdir -p /var/run; /usr/sbin/dropbear -R >/dev/null 2>&1; }
say "[7] dropbear: $(ls /var/run 2>/dev/null | grep -a dropbear)"
say "=== CONSOLE SHELL (log: /tmp/{wifi,majestic}.log) ==="
exec /bin/sh
