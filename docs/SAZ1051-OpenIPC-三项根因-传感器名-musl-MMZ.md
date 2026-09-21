# SAZ1051 OpenIPC 移植 —— 三项根因（传感器名 / musl / MMZ）

> 日期：2026-09-21
> 结论一句话：ABI（`CONFIG_PM=n`）修好后仍有 **三个独立根因** 挡住 MPP 出图，
> 分别落在 **内核内存布局**、**用户态 libc**、**内核态 sensor 名表** 三层，
> 互不相关，必须分别修。三条都已定位到"铁证"级别的字符串/符号。

---

## 0. 前情：ABI 已修好（见另一篇）

`CONFIG_PM=y` 会把 `struct device` 从 272 撑到 456，`open_osal.ko` 的
`device_register(&g_media_bus)` 溢出 184 字节踩坏紧随其后的 `g_media_bus_type.bus_groups`，
`bus_register()` 读到非 NULL 的 `bus_groups=2` → `ldr r2,[0x2]` → panic。
改 `CONFIG_PM=n` 后 `open_osal.ko` 正常加载、0 panic（2026-09-21 串口实测）。

**本文三个根因是在 ABI 修好之后才暴露出来的。**

---

## 根因 1：内核吃满 DDR → MMZ 冲突 → MPP 只加载到 `open_*=2`

### 现象
```
Conflict MMZ: PHYS(0x42000000, 0x43FFFFFF)
MMZ conflict to kernel memory (0x40000000, 0x43FFFFFF)
insmod: can't insert 'open_mmz.ko': Operation not permitted
******* Error: There's something wrong, please check! *****
```
`load_hisilicon` 提前退出，`lsmod | grep -c '^open_'` 卡在 **2**。

### 机理
本板 DDR 只有 **64MB**。`load_hisilicon` 的几何算法是"**内核上限由 cmdline 的
`mem=` 决定**"：

```
mem_total   = fw_printenv totalmem          # 本板 64
os_mem_size = cmdline 里的 mem=NM           # 没有就回退 fw_printenv osmem，再缺省 32
mmz_start   = 0x40000000 + os_mem_size      # → 0x42000000
mmz_size    = mem_total - os_mem_size       # → 32M
```

bootargs 里**没有 `mem=`** → 内核按硬件 RAM 吃满 `0x40000000-0x43FFFFFF`（64MB）→
MMZ 要的 `0x42000000+32M` 正好压在已归内核的区间里 → `open_mmz.ko` 拒绝加载。

### 修法
`tools/oipc_com3_boot.py` 的 `BOOTARGS` 里显式加 `mem=32M`：

```
console=ttyAMA0,115200 clk_ignore_unused mem=32M root=/dev/mmcblk0p1 ...
```

内核只用 `0x40000000-0x41FFFFFF`，`0x42000000+` 让给 MMZ，
与 OpenIPC 官方 64MB 板（`mem=<os内存>`，余下归 MMZ）的惯例一致。

> 旁证：厂商 `load3516cv610_20s_debug` 里也是 `mem_total=128 / os_mem_size=64M /
> mmz_start=0x44000000 / mmz_size=64M` 的同一套算法——**它的注释明写
> "MMZ start: 0x44000000 = 0x40000000 + 0x4000000(OS)"**，即 MMZ 起点永远是
> "物理基址 + OS 内存"。所以 `mem=` 不是可选项，是这套几何的输入。

---

## 根因 2：`majestic` 的 PT_INTERP 硬编码到"厂商精简 musl"

### 现象
```
Error relocating /opt/oipc/majestic: crypt_r: symbol not found
Error relocating /opt/oipc/majestic: nextafter: symbol not found
Error relocating /opt/oipc/majestic: lrintf: symbol not found
```

### 机理（决定性证据：直接比对两个 musl 的导出符号表）
TF rootfs 上有 **两个 musl**：

| 路径 | 大小 | `.dynsym` 导出符号数 |
|---|---|---|
| `/lib/ld-musl-arm.so.1`（厂商/devial v4 rootfs 的精简 musl） | 337,672 B | **1651** |
| `/opt/oipc/lib/ld-musl-arm.so.1`（OpenIPC 的完整 musl） | 501,464 B | **1719** |

逐符号核对：

```
crypt_r        fw=False  oi=True
nextafter      fw=False  oi=True
lrintf         fw=False  oi=True
crypt          fw=False  oi=True
nexttoward     fw=False  oi=True
```

**关键点：`LD_LIBRARY_PATH` 救不了这个坑。** musl 是"单文件 libc"——动态链接器
`ld-musl-arm.so.1` *本身*就是 libc。程序用哪个 musl 由 **ELF 的 `PT_INTERP`
（绝对路径）** 决定，而 `LD_LIBRARY_PATH` **不能覆盖 interpreter**。
`majestic` 的 `PT_INTERP` 正是硬编码的 `/lib/ld-musl-arm.so.1`（21 字节，
偏移 372），于是它必然用到那份**缺 `crypt_r/nextafter/lrintf` 的精简 musl**。

### 修法（零风险，已实测）
把 OpenIPC 的 loader 作为 `argv[0]` **显式**传入即可绕过 `PT_INTERP`，
且完全不动 `/lib` 里的系统 musl：

```sh
OIPC_LD=/opt/oipc/lib/ld-musl-arm.so.1
LD_LIBRARY_PATH=/opt/oipc/lib "$OIPC_LD" /opt/oipc/majestic -s > /tmp/majestic.log 2>&1 &
```

实测（设备上、不重启）：
```
$ LD_LIBRARY_PATH=/opt/oipc/lib /opt/oipc/lib/ld-musl-arm.so.1 /opt/oipc/majestic --version
Lite HiSilicon (hi3516cv6xx), master+fda10bc, 2026-09-08 17:32
```
符号全部解析、版本正常打印。

> 备选（等价）：把 `/lib/ld-musl-arm.so.1` + `/lib/libc.so` 换成 OpenIPC 那两份。
> 这是 OpenIPC 官方 rootfs 的做法；但会**系统级**影响厂商 busybox / wpa_supplicant，
> 风险高于上面的显式 loader 方案。**首选显式 loader。**
>
> 另一条路（若要做成可刷固件、不依赖 init.sh 命令行）：把 `majestic` 的 `PT_INTERP`
> 原地改成 ≤21 字节的路径（如 `/lib/ld-oipc.so.1`，17 字节，尾部补 NUL），
> 再把 OpenIPC musl 复制到那个路径。二进制补丁 + 新路径，与系统 musl 解耦。

---

## 根因 3：OpenIPC 预编译 `open_sys_config.ko` 的 sensor 名表里**没有 `os05l10`**

### 现象
```
parse sensor[os05l10] failed!
```

### 机理（决定性证据：直接扒 .ko 里的字符串表）
该错误串只由 `open_sys_config.ko` 发出：

```
74  %s,%d: parse sensor[%s] failed!
62  FUNC:%s line:%d  err sensor index: [%s]
45  parse_sensor_name
...
120 g_sensor_list          ← 内置名字表
79  parm=sensors:sns0=sc4336p,sns1=sc4336p
```

扒 `g_sensor_list` 实测结果：

| 名字 | 在表内 |
|---|---|
| `os04d10` | ✅ |
| `sc4336p` | ✅ |
| `sc450ai` | ✅ |
| `sc500ai` | ✅ |
| `sc431hai` | ✅ |
| `gc4023` | ✅ |
| `bt1120` / `bt656` / `bt601` | ✅ |
| **`os05l10`** | ❌ **不在** |

对照 OpenIPC 官方 osdrv 包（`general/package/hisilicon-osdrv-hi3516cv6xx/files/sensor/`）
也只有 `gc4023 / imx307 / os02m10 / os04d10 / sc431hai / sc4336p / sc450ai / sc500ai`
——**OpenIPC 主线根本不支持 OS05L10**。我们手上的
`libsns_os05l10.so`（95,180 B）是从**厂商**固件里抽出来的，不是 OpenIPC 的。
表是**预编译二进制**，加不进去。

### 修法（照抄厂商已验证的做法）
厂商自己给内核传的**也不是** os05l10。`/opt/ko/load3516cv610/load3516cv610_20s_debug`：

```
SNS_TYPE0=sc4336p;               # sensor type
SNS_TYPE1=sc4336p;               # sensor type
...
insmod $ko_path/sys_config.ko sensors=sns0=$SNS_TYPE0,sns1=$SNS_TYPE1 board=$dmeb_qfn ...
```

即：**内核侧传 `sc4336p`（通用 MIPI/pinmux 预设），真正的 OS05L10 时序另行补**。

补齐 OS05L10 的两件事：

1. **内核态 mux/clock 直写寄存器**（`sensor_mux.sh`，必须在 MPP 之后，否则被改回）：
   ```
   0x17940040 <- 0x1212    sensor0 clk pad
   0x17940050 <- 0x1137    sensor0 rstn pad
   0x17940098 <- 0x1135    i2c0 sda
   0x1794009c <- 0x1135    i2c0 scl
   0x11018440 <- 0xa010    sensor clock enable
   ```
2. **用户态仍然是 os05l10**（这层本来就通）：
   - `/etc/sensors/os05l10.ini` —— `DllFile=libsns_os05l10.so`，
     `sensor_type=g_sns_os05l10_obj`，2880x1620，MIPI，10bit，`BAYER_BGGR`，24MHz
   - `/usr/lib/sensors/libsns_os05l10.so`
   - `/opt/ceanic/scene/param/sensor_os05l10`（ISP 标定参数）

所以 `oipc_init.sh` 改成：

```sh
KERNEL_SENSOR=sc4336p                                  # 内核侧
/opt/oipc/load_hisilicon -i -sensor0 $KERNEL_SENSOR
export SENSOR=os05l10                                  # 用户态
```

---

## 三层根因一览

| # | 层 | 载体 | 症状 | 修法 |
|---|---|---|---|---|
| 1 | 内核内存布局 | bootargs | `Conflict MMZ`，`open_*=2` | 加 `mem=32M` |
| 2 | 用户态 libc | `majestic` 的 `PT_INTERP` | `crypt_r: symbol not found` | 显式用 OpenIPC loader 启动 |
| 3 | 内核态 sensor 名 | `open_sys_config.ko` 的 `g_sensor_list` | `parse sensor[os05l10] failed!` | 内核传 `sc4336p` + `sensor_mux.sh` 补时序 |

**三条互相独立**：任一条不修，都会在"看起来像同一个症状（MPP 起不来/没图）"的
位置失败，很容易被误判成同一个问题。排障时必须逐层看**不同**的物证：
`/proc/iomem`（#1）、`.dynsym` 符号表（#2）、`.ko` 字符串表（#3）。

---

## 附：本次会话顺带修好的 CI 问题

GitHub Actions `ppliver/saz1051-openipc` 的 `build-kernel.yml`：

1. **`make prepare` → `make modules_prepare`**：`scripts/module.lds` 只由
   `modules_prepare` 生成（Makefile 1491-1492：
   `modules_prepare: prepare` 之后 `$(MAKE) $(build)=scripts scripts/module.lds`）。
   用 `prepare` 会导致外挂模块链接报
   `No rule to make target 'scripts/module.lds', needed by 'abi/probe.ko'`。
2. **补装 `device-tree-compiler`**：`mkimage -f xxx.its yyy.itb` 会 fork `dtc` 去编译
   ITS；缺 dtc 时报 `sh: 1: dtc: not found` + `mkimage: Can't open uImage.itb.tmp`。
   `u-boot-tools` 并不依赖 dtc，必须显式装。

修好后 CI 已能在干净 `ubuntu-22.04` 上完整走通
`patch → defconfig+fragment(PM=n) → ABI guard(272) → zImage/dtbs/modules → FIT`。
**ABI guard 的 272 由 CI 独立复现**，说明 `CONFIG_PM=n` 这一修法不依赖本机环境。
