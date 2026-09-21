#!/bin/sh
# SAZ1051 OpenIPC NAND init  (Hi3516CV610 + OS05L10 + WS73)
# 取代 stock /init：本内核无 OVERLAY_FS，stock init 会在 `grep overlay /proc/filesystems`
# 处 exit 1 -> kill init -> kernel panic。这里直接挂载 + 拉起全栈，逻辑自持、可诊断。
export PATH=/opt/oipc/sbin:/opt/tools:/bin:/sbin:/usr/bin:/usr/sbin
export LD_LIBRARY_PATH=/opt/oipc/lib:/lib:/usr/lib
export TZ=CST-8

# 1 = 先"预热"跑一次 majestic 把 MIPI lane mode 设上, 再优雅退出, 然后正式启动。
#   根因见 [6] 注释。0 = 只跑一次(旧行为)。
MAJ_WARMUP=${MAJ_WARMUP:-1}

say() { echo "[SAZ1051] $*"; }
diag() { echo "$*" >> /tmp/boot_diag.log; }

# ---- 0. 基础挂载 ----
mount -t proc     proc   /proc     2>/dev/null
mount -t sysfs    sysfs  /sys      2>/dev/null
mount -t tmpfs    tmpfs  /tmp      2>/dev/null
mount -t tmpfs    tmpfs  /run      2>/dev/null
mkdir -p /var/run /dev/pts
mount -t tmpfs    tmpfs  /var/run  2>/dev/null
mount -t devpts   devpts /dev/pts  2>/dev/null
echo /sbin/mdev > /proc/sys/kernel/hotplug 2>/dev/null
mdev -s 2>/dev/null

say "=== SAZ1051 OpenIPC (NAND) ==="
say "kernel : $(cat /proc/version)"
say "cmdline: $(cat /proc/cmdline)"
say "mounts : $(mount | tr '\n' ';')"
diag "=== boot diag ==="
diag "cmdline: $(cat /proc/cmdline)"

# ---- 0b. WS73 射频参数（plat_soc 需要，必须先于驱动存在）----
mkdir -p /system/etc
[ -f /system/etc/ws73_cfg.ini ] || cp /etc/ws73_cfg.ini /system/etc/ws73_cfg.ini 2>/dev/null

# ---- 1. MPP(open_*.ko) ----
# ★ 内核侧 sensor 名**必须**传 sc4336p：OpenIPC 预编译 open_sys_config.ko 的
#   g_sensor_list = {os04d10, sc4336p, sc450ai, sc500ai, sc431hai, gc4023, bt1120/656/601}
#   根本没有 os05l10，传它必报 "parse sensor[os05l10] failed!" 并放弃 sensor 初始化。
#   用户态仍按 os05l10 走（/etc/sensors/os05l10.ini + libsns_os05l10.so）。
if [ -x /opt/oipc/load_hisilicon ]; then
	say "[1] load_hisilicon -i -sensor0 sc4336p"
	/opt/oipc/load_hisilicon -i -sensor0 sc4336p 2>&1 | while read l; do echo "    | $l"; done
else
	say "[1] load_hisilicon MISSING"
fi
say "[1] open_* loaded: $(lsmod 2>/dev/null | grep -c '^open_')"
diag "[1] after load_hisilicon: /dev/ot_mipi_rx -> $(ls -la /dev/ot_mipi_rx 2>&1)"
diag "[1] open_mipi_rx in lsmod: $(lsmod 2>/dev/null | grep -c '^open_mipi_rx')"
say "[1] /dev/ot_mipi_rx: $(ls -la /dev/ot_mipi_rx 2>&1 | tr '\n' ' ')"

# ---- 2. OS05L10 走线/时钟时序（必须落在 MPP 之后）----
if [ -x /opt/tools/sensor_mux.sh ]; then
	say "[2] sensor_mux.sh"
	/opt/tools/sensor_mux.sh 2>&1 | while read l; do echo "    | $l"; done
fi
diag "[2] mipi after sensor_mux: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'sensor_clk\|mipi_clk' | tr '\n' ' ')"

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
# 真根因(2026-09-21): majestic 的 mipi_ioctls() 在 HISDK_COMM_VI_SetMipiAttr(SET_DEV_ATTR) **之前**
#   执行, 而 open_mipi_rx 驱动的 drv_mipirx_kapi_param_check 要求"先设 lane mode"才接受
#   OT_MIPI_ENABLE_*_CLOCK / OT_MIPI_UNRESET_* 这几个 ioctl。冷启动时 lane mode 尚未设置 ->
#   4 个 ioctl 全失败(dmesg: "mipirx not set lane mode" / "mipirx0 unreset failed!") ->
#   MIPI 接收端 PHY 复位从未解除 -> /proc/umap/mipi_rx 里 cil_clk_cur_stat 恒 IDLE /
#   lane0_data=0 -> VI 无帧 -> venc_read 超时 -> RTSP 连得上但无画面。
#   （这就是"同一个节点 dd 能打开、majestic 却报 ENOENT"之谜的真身：open 成功，是 ioctl 被拒。）
# 修法: 先让 majestic 完整跑一次(它内部会 SET_DEV_ATTR 把 lane mode 设上), 然后 SIGINT
#   优雅退出并 wait 回收; 第二次启动时 4 个 ioctl 即可成功 unpark, MIPI 出数据。
# ★ 必须 wait 回收子进程: 否则僵尸进程会让 majestic 自己判定 "another instance is running"
#   而拒绝启动(僵尸无法被 kill -9, 只有父进程 wait 才能回收)。
export SENSOR=os05l10

# 清掉 open_isp 的 "ISP[0] already inited" 状态。
# ★ 2026-09-22 实测: warm-up 把 ISP 初始化成功后 SIGINT 退出, open_isp 仍留着 inited 状态,
#   正式实例 init_isp 必然报 ERR_ISP_NOT_SUPPORT("Cannot init mem for ispDev=0")。
#   open_isp 无引用计数(lsmod 全 0) -> 可以安全 rmmod/modprobe; open_mipi_rx **不动**,
#   所以 SET_DEV_ATTR 设进去的 lane mode 仍然保留, 正式实例照样能 unpark 拿到数据。
clear_isp() {
	lsmod 2>/dev/null | grep -q '^open_isp' || return 0
	rmmod open_isp 2>/dev/null || { say "[6] rmmod open_isp FAILED"; return 1; }
	modprobe open_isp 2>/dev/null || { say "[6] modprobe open_isp FAILED"; return 1; }
	say "[6] open_isp reloaded (ISP inited state cleared, lane mode kept)"
	return 0
}

if [ -x /usr/bin/majestic ] && [ "$MAJ_WARMUP" = "1" ]; then
	say "[6] majestic warm-up run (目的: 设 MIPI lane mode)"
	: > /tmp/majestic.log
	/usr/bin/majestic -s >> /tmp/majestic.log 2>&1 &
	WARM=$!
	# ★ "见好就收": 一旦 SET_DEV_ATTR 落地(lane mode 写进 open_mipi_rx)就立刻退出。
	#   若死等 12s, warm-up 会继续跑完 init_isp -> 留下 ISP inited 状态 -> 正式实例必挂。
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

# 监督循环: 前台启动 majestic 并 wait, 既回收僵尸, 又能在它退出后自动重启。
# 放在后台子 shell 里跑, 以便 init 仍能在串口上给交互 shell。
# ★ 2026-09-21 实测: in-place 重启**不保证**释放 ISP —— 旧实例优雅退出后 ISP 仍呈
#   "already inited" 状态, 新实例在 init_isp 处报 ERR_ISP_NOT_SUPPORT 直接退出
#   ("isp_check_mem_init_state: ISP[0] already inited!" / "Cannot start SDK"),
#   于是没有 VENC 通道、:554 也不监听。只有整机 reboot 才能拿到干净的 ISP。
#   => 监督循环必须自带健康判定与"重试仍失败就 reboot"的兜底(否则会永远空转)。
maj_fail=0
maj_supervise() {
	while true; do
		say "[6] starting majestic (foreground of supervisor)"
		/usr/bin/majestic -s >> /tmp/majestic.log 2>&1 &
		MP=$!
		sleep 14
		# 修复: maj_fail 必须预初始化, 否则 $((maj_fail+1)) 在空变量上报
		# "sh: invalid number ''" (busybox ash), 计数永不增长, reboot 兜底永不触发。
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
				reboot -f
			fi
		fi
		say "[6] pid=$(pidof majestic 2>/dev/null || echo none)  rtsp554=$(netstat -ltn 2>/dev/null | grep ':554' || echo none)"
		# ★ 兜底(root cause 未闭环): 用户态 libsns_os05l10.so 的 cis_write_reg 路径
		#   写不进 sensor (I2C 写不落地且不报错) -> 209 条 init 表全丢 -> sensor 停在
		#   出厂默认 -> MIPI 无数据 -> VI 无帧 -> venc_read 超时。
		#   实证: 检测到 mipi_vc0_w==0 时, 用 i2ctransfer 重放整表, 数据立刻出现
		#   (cil_clk_cur_stat: HS_LP00, mipi_vc0_w/h=2880x1620, ISP int 30fps, VENC 出货)。
		if grep -qa 'mipi_vc0_w *:0' /proc/umap/mipi_rx 2>/dev/null; then
			say "[6] MIPI no data -> replay OS05L10 init table over I2C"
			[ -x /opt/tools/os05l10_replay.sh ] && /opt/tools/os05l10_replay.sh >> /tmp/sensor_replay.log 2>&1
			sleep 4
		fi
		say "[6] MIPI: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'cil_clk_cur_stat\|mipi_vc0_w\|lane0_data' | tr '\n' ' ')"
		say "[6] venc timeouts: $(grep -c 'Timeout from venc' /tmp/majestic.log 2>/dev/null)  isp int_cnt: $(grep -a 'int_cnt' /proc/umap/isp 2>/dev/null | sed -n '2p' | awk '{print $2}')"
		diag "[6] supervisor check: $(cat /proc/umap/mipi_rx 2>/dev/null | tr -d ' ' | grep -a 'cil_clk_cur_stat\|mipi_vc0_w' | tr '\n' ' ')"
		# ★ 有界等待: 裸 wait 在 majestic 卡死(不退出)时永不返回 -> 整个监督循环停转,
		#   maj_fail 不再增长、reboot 兜底永不触发。改为轮询 + 周期性 MIPI 复核 + 超时强杀。
		w=0
		while [ $w -lt 600 ]; do
			kill -0 $MP 2>/dev/null || break
			w=$((w+1))
			if [ $((w % 30)) -eq 0 ]; then
				if grep -qa 'mipi_vc0_w *:0' /proc/umap/mipi_rx 2>/dev/null; then
					say "[6] MIPI no data -> replay OS05L10 init table over I2C"
					[ -x /opt/tools/os05l10_replay.sh ] && /opt/tools/os05l10_replay.sh >> /tmp/sensor_replay.log 2>&1
				fi
			fi
			sleep 1
		done
		if kill -0 $MP 2>/dev/null; then
			say "[6] majestic still alive after ${w}s -> SIGKILL"
			kill -9 $MP 2>/dev/null
			sleep 2
		fi
		wait $MP 2>/dev/null
		say "[6] majestic exited (rc=$?), restart in 6s"
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
say "=== CONSOLE (logs: /tmp/{wifi,majestic,boot_diag}.log) ==="
# init(pid1) 不能退出(会让内核 panic/重启循环)：串口 shell 退出后自动重开。
while true; do
	/bin/sh
	say "[init] console shell exited, respawning"
	sleep 1
done
