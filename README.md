# saz1051-openipc

把 **OpenIPC**（Linux 内核 + HiSilicon MPP 视频栈 + OS05L10 sensor + WS73 USB WiFi + majestic RTSP）
移植到 **SAZ1051** 摄像头（SoC = **Hi3516CV610**，64MB DDR，128MB SPI-NAND，无有线网口）的补丁与构建工程。

本仓库只放**必需的改动 + 可复现的构建流程**；厂商预编译二进制（MPP `open_*.ko`、WS73 私有 ko）
不入库，由部署脚本从对应固件包中取用。

---

## 1. 硬件与运行环境

| 项 | 值 |
| --- | --- |
| SoC | Hi3516CV610（Cortex-A7，ARMv7，THUMB2） |
| DDR | 64 MB（`dmesg` 53080K/65536K）→ kernel 32M + `mmz=0x42000000,32M` |
| 存储 | 128 MB SPI-NAND（DS35Q1GA；PEB 128K / page 2K / OOB 64） |
| Sensor | **OS05L10**（2880x1620@30，10bit linear；I2C 7-bit `0x3C` / SDK 写 8-bit `0x78`；`0xFD`=bank） |
| WiFi | **WS73 (Hi3873V100)** USB STA |
| 串口 | UART 3.3V 115200（本机 CH340 → COM3） |
| U-Boot | 2022.07；有 `loadx/loady/crc32/nand/mmc`，**无 `fatload`**；**只认 FIT**（无 legacy/ATAGS） |
| 内核基线 | [`openipc/linux`](https://github.com/openipc/linux) 分支 `hisilicon-hi3516cv6xx`，钉死 commit `533467722c14`（2026-03-05，5.10.221） |

---

## 2. ★ 头号铁律：`CONFIG_PM=n`（否则 MPP 一加载就 panic）

这不是"最佳实践"，是**硬约束**。MPP 预编译模块 `open_*.ko` 取自 OpenIPC 官方固件
（`openipc.hi3516cv6xx-nor-ultimate`），而该固件的内核配置里 `# CONFIG_PM is not set`。于是：

```
CONFIG_PM=n  →  sizeof(struct dev_pm_info) =  32  →  sizeof(struct device) = 272   ✅
CONFIG_PM=y  →  sizeof(struct dev_pm_info) = 216  →  sizeof(struct device) = 456   ❌ (Δ +184)
```

`open_osal.ko` 的 `.data` 里两个全局量**紧邻**：

```
0x160  g_media_bus        struct device    272 B
0x270  g_media_bus_type   struct bus_type   88 B
```

`CONFIG_PM=y` 时 `device_register(&g_media_bus)` 按 456 字节写入，**越过 272 边界 184 字节**，
正好覆盖 `g_media_bus_type.bus_groups`（被写成 `2`）。紧接着 `bus_register()` 读到
`bus_groups = 2`（非 NULL，绕过 `if (!groups)` 保护）→ `sysfs_create_groups` →
`internal_create_groups` → `ldr r2, [0x2]` → **kernel panic**。

现场特征（照抄即可对上号）：

```
PC = internal_create_groups.part.0+0xc
LR = bus_register+0x135          (c02e16a4 | 1)
"Unable to handle kernel NULL pointer dereference at virtual address 00000002"
r6 = 0x2
Modules linked in: open_osal(O+) open_sys_config(O)
```

**代价**：`CONFIG_PM=n` 会让 `register_pm_notifier` / `unregister_pm_notifier` 消失
（这两个符号只在 `CONFIG_PM_SLEEP` 的 `kernel/power/main.c` 里导出），而 WS73 的 `plat_soc.ko`
恰好引用它们。补偿方式是在内核里内建一个空实现桩：`drivers/vendor/saz_pm_stub.c`
（由 `kernel/saz1051_kernel.patch` 引入，摄像头常供电、从不 suspend，空实现语义正确）。

> 另外两个必须记牢的坑：
> - **改了 `.config` 必须跑 `make prepare`**。只改 `.config` 而不同步
>   `include/generated/autoconf.h`，后续 `make M=... modules` 会静默沿用旧头文件 ——
>   量出来的 struct 尺寸**完全不变**，会让人得出"改配置没用"的错误结论。
> - **交付给 U-Boot 的内核必须是 FIT（`.itb`）**。本机 U-Boot 未编译 legacy/ATAGS 支持，
>   `bootm` 旧式 uImage 会报 `FDT and ATAGS support not compiled in` 并直接 reset。

---

## 3. 仓库结构

```
kernel/
  saz1051_kernel.patch      内核源码补丁（4 处改动 + 1 个新文件）
  saz_fragment.cfg          SAZ1051 增量内核配置（在 hi3516cv610_defconfig 之上追加）
  oipc_saz1051.its          FIT 镜像描述（zImage + dtb → uImage.itb）
  abi_probe.c / .mk         ABI 探针：量 sizeof(struct device) 等，供断言
stubs/
  saz_pm_stub.c             PM notifier 空实现桩（独立 insmod 版；内核内建版见 patch）
  saz_uapi_stub.c           uapi_tsensor_read_temperature / uapi_efuse_* 桩
  Makefile
rootfs/
  oipc_init.sh              验证用 init（PID 1）：WiFi → MPP → sensor_mux → majestic → 出图判据
  load_hisilicon            加载 MPP open_*.ko（按绝对路径，不强依赖 modules.dep）
  sbin/{modprobe,fw_printenv,fw_setenv,ipcinfo}
tools/
  oipc_build_kernel.py      在远程构建机上跑完整构建（含幂等 patch + ABI 守卫）
docs/
  SAZ1051-OpenIPC移植方案.md
  SAZ1051-移植第三方固件注意事项-取证与方法论.md
.github/workflows/
  build-kernel.yml          GitHub Actions：干净环境从零编出可启动的 FIT 内核
```

---

## 4. 复现构建

### 4.1 用 GitHub Actions（推荐，干净环境 + 4 核）

推送到 `main` 或手动 `workflow_dispatch` 即触发。流水线做的事：

1. `git clone openipc/linux` @ 钉死 commit
2. 应用 `kernel/saz1051_kernel.patch`
3. `hi3516cv610_defconfig` + `saz_fragment.cfg` + `scripts/config --disable PM`
4. `make olddefconfig && make prepare`（必须，见上文）
5. **ABI 守卫**：编译探针断言 `sizeof(struct device)==272`、`dev_pm_info==32`、`bus_type==88`；
   不通过 → **直接 fail，绝不出包**
6. `make -j$(nproc) zImage dtbs modules` → `mkimage` 打 FIT → 收集模块
7. 校验 PM 桩已编入 `vmlinux` 且已导出；校验模块 `vermagic`
8. 上传 artifact：`uImage.itb`、`hi3516cv610-demb.dtb`、`kernel-modules-5.10.221.tar.gz`、`SHA256SUMS`

拉回产物：

```bash
gh run download -n saz1051-kernel-5.10.221
# 或从 Actions 页面直接下载 artifact
```

### 4.2 用自备构建机（`tools/oipc_build_kernel.py`）

脚本在远程 Linux 构建机上跑，含幂等 `patch_sources()` 与同一个 ABI 守卫：

```bash
python tools/oipc_build_kernel.py config   # 仅生成 .config + 量 ABI（不编译）
python tools/oipc_build_kernel.py abi      # 同上（别名）
python tools/oipc_build_kernel.py build    # 全量: zImage/dtbs/modules + FIT + 校验
```

> 该脚本顶部 `HOST/USER/PW/TC/LINUX/STAGE` 是**本项目的构建机常量**，换机器需改这几行。
> 它刻意使用与厂商 ko **同版本的 gcc10.3（musl）**，使 vermagic 完全对齐
> （实测产出 `5.10.221 SMP mod_unload ARMv7 thumb2 p2v8`）。CI 走 `gcc-arm-linux-gnueabihf`
> 也能得到同样的 vermagic —— vermagic 由内核配置决定，与编译器版本无关。

---

## 5. 串口零写启动（不打 NAND，改坏了随时可回）

内核经 U-Boot `loadx`（XMODEM-CRC，1K 块）灌进 RAM，`bootm` 启动；rootfs 用 TF 卡 FAT 分区。
全程只 `setenv`，**绝不 `saveenv`**，NAND 零写入。

```
loadx 0x41000000
setenv rootfs_devpart ''
setenv bootargs 'console=ttyAMA0,115200 clk_ignore_unused \
  root=/dev/mmcblk0p1 rootfstype=vfat \
  rootflags=fmask=0000,dmask=0000,codepage=437,iocharset=iso8859-1,shortname=mixed,usefree,utf8,errors=continue \
  rootwait rw init=/opt/oipc/oipc_init.sh panic=10'
bootm 0x41000000
```

TF 卡根文件系统需要 `/opt/oipc/`（`oipc_init.sh`、`load_hisilicon`、`majestic`、`sbin/`）、
`/opt/tools/`（`bringup_wifi.sh`、`sensor_mux.sh`、`sazreg.ko`）、`/system/etc/ws73_cfg.ini`。

`rootfs/oipc_init.sh` 启动后按序做：**sensor_clk 补丁 → WiFi（后台）→ MPP(`load_hisilicon`)
→ `sensor_mux.sh` → `majestic` → 打印出图判据**，最后 `exec /bin/sh` 留一个串口 shell。

---

## 6. 关键坑速查

| # | 现象 | 根因 | 处理 |
| --- | --- | --- | --- |
| 1 | `open_osal.ko` 加载即 panic，`internal_create_groups+0xc` / addr `0x2` | `CONFIG_PM=y` → `struct device` 456 而非 272，越界写坏 `g_media_bus_type.bus_groups` | `CONFIG_PM=n` + PM 桩 |
| 2 | 改了 `.config` 但 struct 尺寸不变 | 没跑 `make prepare`，`autoconf.h` 是旧的 | `olddefconfig` 后**必跑** `prepare` |
| 3 | `bootm` 后一行 `Starting kernel ...` 全静默 | 内核在 PL011 控制台注册前挂死（vendor `pm,sram` 驱动 probe 崩溃） | patch 里 `pm.c` 去雷（`IS_ERR_OR_NULL` + `.of_match_table = NULL`）；再加 `earlycon` |
| 4 | `FDT and ATAGS support not compiled in` → reset | 本机 U-Boot 无 legacy uImage 支持 | 必须交付 **FIT (.itb)** |
| 5 | WS73 永不枚举 | `usb20drd` 需要 `dr_mode="otg"` + `host-mode;` + `init_mode="host"` **三项齐** | patch 里已含 |
| 6 | `plat_soc: Unknown symbol register_pm_notifier (err -2)` | `CONFIG_PM=n` 时不导出该符号 | `drivers/vendor/saz_pm_stub.c` 内建桩 |
| 7 | 量 ABI 时结果死活不变 | 同 #2 | 同 #2 |
| 8 | `Module.symvers` 里"找不到" PM 符号 | 该文件每行以 **TAB** 结尾；`grep '...$'` 会误报 | 去掉 `$` 行尾锚点 |

---

## 7. 现状

- [x] 内核（5.10.221，`CONFIG_PM=n`）编译通过，FIT 产出，ABI 断言 272 通过
- [x] PM 桩编入 `vmlinux` 并 `EXPORT_SYMBOL`（`readelf` 可见 `__ksymtab_register_pm_notifier`）
- [x] 本内核编出模块的 `vermagic` 与预编译 `open_*.ko` 完全一致
- [ ] 串口启动验证：`open_osal.ko` 加载不再 panic
- [ ] `sensor_mux.sh` + OS05L10（I2C `0x78`）→ `/proc/umap/vi` 出 2880x1620
- [ ] `majestic` RTSP :554 出图
- [ ] 4 分区 NAND 可刷包（`uImage` + `rootfs_ubi.img` + `env.bin` + `boot_image.bin` + `burn_table.xml`）

---

## 8. 许可

本仓库内的补丁与脚本以 GPL-2.0 分发（与 Linux 内核一致）。
`openipc/linux`、OpenIPC 固件、HiSilicon MPP、WS73 驱动的版权归各自权利人，本仓库不再分发。
