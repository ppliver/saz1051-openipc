#!/bin/sh
# SAZ1051 OpenIPC NAND init  (Hi3516CV610 + OS05L10 + WS73)   [v2 内存/稳定性优化]
# 取代 stock /init：本内核无 OVERLAY_FS，stock init 会在 `grep overlay /proc/filesystems`
# 处 exit 1 -> kill init -> kernel panic。这里直接挂载 + 拉起全栈，逻辑自持、可诊断。
#
# v2 改动(2026-09-22)：针对"访问 web 后台 -> OOM -> clear_isp -> 内核 panic 重启"
#   1. safe_reboot(): 统一走 sync + sysrq(b)，不再用无效的 reboot -f，且重启前 sync
#      防止 UBIFS 半写节点(之前看到的 bad CRC 风暴就是 panic 时未 sync 的次生灾害)。
#   2. majestic 异常退出(被 OOM/信号杀) 不再执行 rmmod open_isp —— 那时 VI 中断仍在跑，
#      卸 ISP 必然 NULL 解引用 panic。改为整机重启兜底。
#   3. tmpfs 限额(size=)：防止 HLS/日志把内存撑爆。
#   4. 内存水位守护：MemAvailable 低于阈值时主动优雅重启 majestic，抢在 OOM 之前。
#   5. majestic.log 轮转，避免长时间运行后无限增长。
#
# ★ 试过但**不可用**：v2 的 trim_modules() 卸载 IVE/NPU/H265E/JPEGE/VCA/音频/PIRIS 后，
#   majestic 起来即 `Unable to handle kernel NULL pointer dereference ...
#   PC is at vpss_get_vb_cfg+0xe/0x18 [open_vpss]`（VB 池配置随之失效）-> panic 循环。
#   MPP 各模块间存在隐式耦合，不能按“看起来没用”来卸。此处整体移除。
export PATH=/opt/oipc/sbin:/opt/tools:/bin:/sbin:/usr/bin:/usr/sbin
export LD_LIBRARY_PATH=/opt/oipc/lib:/lib:/usr/lib
export TZ=CST-8

# 1 = 先"预热"跑一次 majestic 把 MIPI lane mode 设上, 再优雅退出, 然后正式启动。
#   根因见 [6] 注释。0 = 只跑一次(旧行为)。
MAJ_WARMUP=${MAJ_WARMUP:-1}
# 内存水位下限(kB)：低于此值主动重启 majestic，避免走到 OOM-kill。
# ★ 必须低于稳定态实际水位（mem=32M 时实测约 0.9MB），否则会不停重启 majestic。
MEM_LOW_KB=${MEM_LOW_KB:-700}

say() { echo "[SAZ1051] $*"; }
diag() { echo "$*" >> /tmp/boot_diag.log; }

# 可用内存(kB)
memavail() { awk '/MemAvailable/{print $2}' /proc/meminfo 2>/dev/null; }

# 安全重启：先 sync 再 sysrq b。本设备 busybox `reboot -f` 无效(内核 reboot 通路不通)。
safe_reboot() {
	say "[!] safe_reboot: sync -> sysrq b"
	sync 2>/dev/null
	echo s > /proc/sysrq-trigger 2>/dev/null   # emergency sync
	sleep 1
	sync 2>/dev/null
	echo b > /proc/sysrq-trigger 2>/dev/null   # immediate reboot
	sleep 8
	reboot -f 2>/dev/null                       # 兜底
	sleep 10
}

# ---- 0. 基础挂载（带 size 限额，防止 tmpfs 吃光内存）----
mount -t proc     proc   /proc     2>/dev/null
mount -t sysfs    sysfs  /sys      2>/dev/null
mount -t tmpfs -o size=6M tmpfs  /tmp      2>/dev/null
mount -t tmpfs -o size=1M tmpfs  /run      2>/dev/null
mkdir -p /var/run /dev/pts
mount -t tmpfs -o size=1M tmpfs  /var/run  2>/dev/null
mount -t devpts   devpts /dev/pts  2>/dev/null
echo /sbin/mdev > /proc/sys/kernel/hotplug 2>/dev/null
mdev -s 2>/dev/null

say "=== SAZ1051 OpenIPC (NAND) init v3 ==="
say "kernel : $(cat /proc/version)"
say "cmdline: $(cat /proc/cmdline)"
say "mem    : MemAvailable=$(memavail)kB"
diag "=== boot diag (init v2) ==="
diag "cmdline: $(cat /proc/cmdline)"

# ---- 0b. WS73 射频参数（plat_soc 需要，必须先于驱动存在）----
mkdir -p /system/etc
[ -f /system/etc/ws73_cfg.ini ] || cp /etc/ws73_cfg.ini /system/etc/ws73_cfg.ini 2>/dev/null

# ---- 1. MPP(open_*.ko) ----
# ★ 内核侧 sensor 名**必须**传 sc4336p：OpenIPC 预编译 open_sys_config.ko 的
#   g_sensor_list 根本没有 os05l10，传它必报 "parse sensor[os05l10] failed!"。
#   用户态仍按 os05l10 走（/etc/sensors/os05l10.ini + libsns_os05l10.so）。
if [ -x /opt/oipc/load_hisilicon ]; then
	say "[1] load_hisilicon -i -sensor0 sc4336p"
	/opt/oipc/load_hisilicon -i -sensor0 sc4336p 2>&1 | while read l; do echo "    | $l"; done
else
	say "[1] load_hisilicon MISSING"
fi

say "[1] open_* loaded: $(lsmod 2>/dev/null | grep -c '^open_')"
diag "[1] after load_hisilicon: /dev/ot_mipi_rx -> $(ls -la /dev/ot_mipi_rx 2>&1)"
say "[1] /dev/ot_mipi_rx: $(ls -la /dev/ot_mipi_rx 2>&1 | tr '\n' ' ')"

# ---- 2. OS05L10 走线/时钟时序（必须落在 MPP 之后）----
if [ -x /opt/tools/sensor_mux.sh ]; then
	say "[2] sensor_mux.sh"
	/opt/tools/sensor_mux.sh 2>&1 | while read l; do echo "    | $l"; done
fi

# ---- 3. WiFi（后台，不阻塞启动）----
say "[3] bringup_wifi.sh -> /tmp/wifi.log"
/opt/tools/bringup_wifi.sh > /tmp/wifi.log 2>&1 &

# ---- 5. dropbear SSH(取日志/传文件) ----
mkdir -p /etc/dropbear /var/run
[ -x /usr/sbin/dropbear ] && dropbear -R -B -p 22 >/dev/null 2>&1
say "[5] dropbear: $(ls /var/run 2>/dev/null | grep -a dropbear | tr '\n' ' ')"

# ---- 5b. 等 MIPI 设备节点就绪 ----
n=0
while [ $n -lt 60 ]; do
	if ( exec 8<>/dev/ot_mipi_rx ) 2>/dev/null; then break; fi
	n=$((n+1)); sleep 0.5
done
if [ $n -ge 60 ]; then
	diag "[5b] rw-open still failing after 60 tries -> reload open_mipi_rx"
	rmmod open_mipi_rx 2>/dev/null
	sleep 1
	modprobe open_mipi_rx 2>/dev/null
	sleep 2
fi
sleep 2
diag "[5b] rw-open ready after ${n} tries: $(ls -la /dev/ot_mipi_rx 2>&1)"
say "[5b] mipi rw-open ready (tries=${n})"

# ---- 6. majestic ----
# 真根因(2026-09-21): majestic 的 mipi_ioctls() 在 HISDK_COMM_VI_SetMipiAttr(SET_DEV_ATTR)
#   之前执行, 而 open_mipi_rx 驱动要求"先设 lane mode"才接受 ENABLE_CLOCK / UNRESET。
#   冷启动 lane mode 未设 -> 4 个 ioctl 全失败 -> PHY 复位从未解除 -> VI 无帧 -> RTSP 无画面。
# 修法: 先让 majestic 完整跑一次(内部 SET_DEV_ATTR 把 lane mode 设上), 然后 SIGINT 优雅
#   退出并 wait 回收; 第二次启动时 ioctl 即可成功 unpark, MIPI 出数据。
export SENSOR=os05l10

# 清掉 open_isp 的 "ISP[0] already inited" 状态（仅在 majestic 已优雅停止时调用）。
# ★ 危险边界：若 majestic 是被 OOM/信号杀掉的，VI/MIPI 中断仍在产生，此时 rmmod open_isp
#   会让中断上下文访问已失效对象 -> NULL 解引用 -> Kernel panic。
#   因此调用方必须先确认 majestic 已优雅退出（见 maj_supervise 的退出码分支）。
clear_isp() {
	lsmod 2>/dev/null | grep -q '^open_isp' || return 0
	rmmod open_isp 2>/dev/null || { say "[6] rmmod open_isp FAILED"; return 1; }
	modprobe open_isp 2>/dev/null || { say "[6] modprobe open_isp FAILED"; return 1; }
	say "[6] open_isp reloaded (ISP inited state cleared, lane mode kept)"
	return 0
}

# 日志轮转：majestic -s 持续输出，长时间运行会撑爆 tmpfs(限额 6M)。
rotate_log() {
	f=/tmp/majestic.log
	[ -f $f ] || return 0
	lines=$(wc -l < $f 2>/dev/null)
	[ -n "$lines" ] || return 0
	if [ "$lines" -gt 400 ]; then
		tail -100 $f > $f.tmp 2>/dev/null && mv $f.tmp $f 2>/dev/null
		say "[6] majestic.log rotated ($lines -> 100 lines)"
	fi
}

if [ -x /usr/bin/majestic ] && [ "$MAJ_WARMUP" = "1" ]; then
	say "[6] majestic warm-up run (目的: 设 MIPI lane mode)"
	: > /tmp/majestic.log
	/usr/bin/majestic -s >> /tmp/majestic.log 2>&1 &
	WARM=$!
	# ★ "见好就收": 一旦 SET_DEV_ATTR 落地(lane mode 写进 open_mipi_rx)就立刻退出。
	n=0
	while [ $n -lt 80 ]; do
		grep -qa 'SET_DEV_ATTR' /tmp/majestic.log 2>/dev/null && break
		kill -0 $WARM 2>/dev/null || break
		n=$((n+1)); sleep 0.5
	done
	say "[6] warm-up: SET_DEV_ATTR seen after ${n} polls"
	sleep 2
	kill -INT $WARM 2>/dev/null
	wait $WARM 2>/dev/null
	sleep 4
	clear_isp
	say "[6] warm-up exited (reaped, no zombie)"
	diag "[6] after warm-up: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'lane_mode\|cil_clk_cur_stat\|sensor_clk' | tr '\n' ' ')"
	say "[6] after warm-up lane_mode: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'lane_mode' | tr '\n' ' ')"
fi

# 监督循环：前台启动 majestic 并 wait，既回收僵尸，又能在它退出后自动重启。
maj_fail=0
maj_supervise() {
	while true; do
		say "[6] starting majestic (foreground of supervisor), MemAvailable=$(memavail)kB"
		rotate_log
		/usr/bin/majestic -s >> /tmp/majestic.log 2>&1 &
		MP=$!
		sleep 14
		maj_fail=${maj_fail:-0}
		if kill -0 $MP 2>/dev/null && netstat -ltn 2>/dev/null | grep -q ':554'; then
			maj_fail=0
			say "[6] majestic healthy (pid=$MP, :554 listening)"
		else
			maj_fail=$((maj_fail+1))
			say "[6] majestic UNHEALTHY (attempt $maj_fail):"
			tail -8 /tmp/majestic.log 2>/dev/null | while read l; do echo "    | $l"; done
			if [ "$maj_fail" -ge 2 ]; then
				say "[6] SDK/ISP not released by in-place restart -> full reboot to recover"
				sleep 2
				safe_reboot
			fi
		fi
		say "[6] pid=$(pidof majestic 2>/dev/null || echo none)  rtsp554=$(netstat -ltn 2>/dev/null | grep ':554' || echo none)"
		if grep -qa 'mipi_vc0_w *:0' /proc/umap/mipi_rx 2>/dev/null; then
			say "[6] MIPI no data -> replay OS05L10 init table over I2C"
			[ -x /opt/tools/os05l10_replay.sh ] && /opt/tools/os05l10_replay.sh >> /tmp/sensor_replay.log 2>&1
			sleep 4
		fi
		say "[6] MIPI: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'cil_clk_cur_stat\|mipi_vc0_w\|lane0_data' | tr '\n' ' ')"
		say "[6] venc timeouts: $(grep -c 'Timeout from venc' /tmp/majestic.log 2>/dev/null)"
		diag "[6] supervisor check: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'cil_clk_cur_stat\|mipi_vc0_w' | tr '\n' ' ')"

		# ★ 有界等待 + 内存水位守护：裸 wait 在 majestic 卡死时永不返回 -> 监督循环停转。
		w=0
		mem_hit=0
		while [ $w -lt 600 ]; do
			kill -0 $MP 2>/dev/null || break
			w=$((w+1))
			# 每 30s 复核 MIPI
			if [ $((w % 30)) -eq 0 ]; then
				if grep -qa 'mipi_vc0_w *:0' /proc/umap/mipi_rx 2>/dev/null; then
					say "[6] MIPI no data -> replay OS05L10 init table over I2C"
					[ -x /opt/tools/os05l10_replay.sh ] && /opt/tools/os05l10_replay.sh >> /tmp/sensor_replay.log 2>&1
				fi
			fi
			# 每 15s 查内存水位；低于阈值则优雅重启 majestic（抢在 OOM-kill 之前，
			# 因为 OOM 后 rmmod open_isp 会 panic）。
			if [ $((w % 15)) -eq 0 ]; then
				ma=$(memavail)
				if [ -n "$ma" ] && [ "$ma" -lt "$MEM_LOW_KB" ]; then
					say "[6] LOW MEM ${ma}kB < ${MEM_LOW_KB}kB -> graceful restart majestic"
					diag "[6] lowmem restart at ${ma}kB"
					mem_hit=1
					kill -INT $MP 2>/dev/null
					sleep 5
					break
				fi
			fi
			sleep 1
		done
		if kill -0 $MP 2>/dev/null; then
			mem_hit=1
			say "[6] majestic still alive after ${w}s -> SIGKILL"
			kill -9 $MP 2>/dev/null
			sleep 2
		fi
		wait $MP 2>/dev/null
		rc=$?
		say "[6] majestic exited (rc=$rc, memhit=$mem_hit), MemAvailable=$(memavail)kB"
		# ★ 异常退出判定：被信号杀(rc>=128，典型 OOM 是 137/SIGKILL) 或 SIGSEGV/超时强杀
		#   时 VI 中断可能仍在跑 -> 绝不能 rmmod open_isp，否则必然 panic。
		if [ "$rc" -ge 128 ] 2>/dev/null || [ "$mem_hit" = "1" ]; then
			say "[6] abnormal/forced exit (rc=$rc) -> skip clear_isp, reboot whole board"
			diag "[6] abnormal exit rc=$rc -> safe_reboot"
			sleep 2
			safe_reboot
		fi
		clear_isp
		sleep 6
	done
}
if [ -x /usr/bin/majestic ]; then
	maj_supervise &
fi

# ---- 7. 启动判据 ----
sleep 8
say "[7] /proc/umap/vi:"; head -25 /proc/umap/vi 2>/dev/null | while read l; do echo "    | $l"; done
say "[7] wlan0 : $(ifconfig wlan0 2>/dev/null | grep inet || echo none)"
say "[7] wifi.log(tail):"; tail -12 /tmp/wifi.log 2>/dev/null | while read l; do echo "    | $l"; done
say "[7] MemAvailable=$(memavail)kB  (low-water restart at ${MEM_LOW_KB}kB)"
say "=== CONSOLE (logs: /tmp/{wifi,majestic,boot_diag}.log) ==="
# init(pid1) 不能退出(会让内核 panic/重启循环)：串口 shell 退出后自动重开。
while true; do
	/bin/sh
	say "[init] console shell exited, respawning"
	sleep 1
done
