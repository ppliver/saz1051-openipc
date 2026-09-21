# SAZ1051 — OpenIPC 移植方案

> 起草：2026-09-21（硬件全部摸清后首次系统性评估）
> 状态：**第一阶段已完成** —— OS05L10 传感器库在 OpenIPC 体系内编译通过并与官方 ABI 对齐。
> 关联：[`SAZ1051-Sensor根因纠正-OS05L10与出图路径.md`](SAZ1051-Sensor根因纠正-OS05L10与出图路径.md)、[`README.md`](README.md)

---

## 0. 一句话结论

**OpenIPC 上游已原生支持 Hi3516CV610**（`BR2_OPENIPC_SOC_ALIASES` 明确含 `hi3516cv610`，DTS 就是 `hi3516cv610-demb`），
移植在原理上没有硬障碍。本轮最大的未知（Sensor）**已被消除**：

> **`libsns_os05l10.so` 已在 OpenIPC 官方工具链下从源码编译通过，零错误，导出符号与官方 `libsns_os04d10.so` 完全同构。**

剩余工作集中在三处：**存储介质（NOR→SPI-NAND）**、**WiFi 无线栈**、**传感器名注册**。

---

## 1. 本轮产出（已完成）

| 产出 | 位置 | 说明 |
|---|---|---|
| `libsns_os05l10.so` | `openipc/build_out/libsns_os05l10.so`（95,180 B） | OpenIPC 传感器库，用官方 `arm-openipc-linux-musleabi` GCC 13.3 编译 |
| 可重复构建脚本 | `tools/oipc_sensorlib.py` | `build / sync / pull` 三命令，一键换 sensor 重编 |
| 官方镜像解包 | `openipc/extracted/` | `firmware.bin.hi3516cv6xx` 切分为 `kernel_fit.bin`(2.7MB) + `rootfs.squashfs`(7.7MB) |
| OpenIPC 构建树 | `build/oipc/upstream` | firmware builder 完整 checkout |
| 177 工作区 | `/home/zhang/oipc-saz/{openhisilicon,tc,in}` | 源码 156MB、工具链 110MB、解包 rootfs |

---

## 2. 关键技术事实（含取证位置）

### 2.1 上游支持面

| 事实 | 位置 |
|---|---|
| hi3516cv6xx 已有 ultimatedefconfig，别名含 **hi3516cv610 / hi3516cv608** | `br-ext-chip-hisilicon/configs/hi3516cv6xx_ultimate_defconfig` |
| DTS = `hi3516cv610-demb`（海思官方 DEMB 参考板） | 同文件 `BR2_LINUX_KERNEL_INTREE_DTS_NAME` |
| 闭源 MPP 用户态 `libss_mpi*.so` 已随 osdrv 包发布 | `general/package/hisilicon-osdrv-hi3516cv6xx/*.mk` |
| 内核模块为 **openhisilicon 自研 `open_*.ko`**（非原厂 `ot_*.ko`），源码可得 | `general/package/hisilicon-opensdk/` → github `openipc/openhisilicon` |
| cv6xx 内核 config 已带 `MTD_SPI_NAND_BSP / MTD_SPI_NAND_FMC100 / MTD_UBI / UBIFS_FS` | `board/hi3516cv6xx/hi3516cv6xx.generic.config` |

### 2.2 官方 nor 镜像构成（实测解包）

```
firmware.bin.hi3516cv6xx (10,551,296 B)
├─ @0x000000  fitImage (2,818,048 B)  ← FIT: zImage(load/entry 0x40018000) + cv610-demb.dtb(load 0x40000000)
└─ @0x2b0000  rootfs.squashfs (7,733,248 B)   ← 64KiB 对齐追加
```
- 内核版本 **5.10.221**（与我们现有第三方内核**同版本**）
- 自带全套 `open_*.ko`：`open_base / open_isp / open_vi / open_vpss / open_venc / open_mipi_rx / open_sensor_i2c / open_sys_config …`（共 39 个）
- `usr/lib/sensors/` 内有 8 个 sensor so：`gc4023 / imx307 / os02m10 / os04d10 / sc431hai / sc4336p / sc450ai / sc500ai`
- **没有 OS05L10**（全树 grep 0 命中）

### 2.3 传感器机制（决定我们要补哪些文件）

1. 开机 rcS 取 `export SENSOR=$(fw_printenv -n sensor)`；
2. `load_hisilicon -i` → `modprobe open_sys_config sensors=sns0=$SNS_TYPE0,...,board=dmeb_qfn`；
3. 用户态 **`libsns_*.so` 由 majestic 自己 `dlopen`**（majestic 内含字符串 `/usr/lib/sensors/`），
   再取 `*_get_obj()` 拿到 `ot_isp_sns_obj`；
4. I2C 走内核 `CONFIG_I2C_BSP` 驱动，寄存器由用户态写。

> ⚠️ **I2C 地址坑在此复用**：openhisilicon `sensor_common.c:190` 同样做
> `ioctl(fd, OT_I2C_SLAVE_FORCE, i2c->addr >> 1)`，即 **`*_I2C_ADDR` 必须写 8-bit**。
> 官方 `os04d10_cmos.h` 里 `OS04D10_I2C_ADDR = 0x78` 正是明证，与我们此前结论一致 → OS05L10 用 `0x78`。

### 2.4 决定性捷径

我们既有的 OS05L10 驱动（第三方 SDK）头部注释写明：
*"Modeled on the Shenshu **os04d10** (OmniVision) template"* ——
**它与 openhisilicon 树里的 os04d10 是同一份代码的分支**。
因此"移植"实质是**回归同源**：改包含路径、Makefile，其余几乎原样通过。这也是本次能一次编译成功的原因。

---

## 3. 三大缺口与解法

### 缺口 1：官方 NOR/squashfs vs 本机 128MB SPI-NAND

| 项 | 官方 | SAZ1051 |
|---|---|---|
| 介质 | NOR 16MB | SPI-NAND 128MB（DS35Q1GA，PEB 128KiB / page 2048） |
| rootfs | squashfs-xz | 需要 **UBIFS** |
| 擦除块 | `post-image.sh` 硬编码 64KiB | 128KiB |

**内核侧不需要动**（`MTD_SPI_NAND_BSP=y`、`MTD_SPI_NAND_FMC100=y`、`MTD_UBI=y`、`UBIFS_FS=y` 均已开）。需要改的是打包侧：

1. defconfig 增补（照抄 `hi3516ev200_ultimate_defconfig:42-46`）：
   `BR2_TARGET_ROOTFS_UBI=y` / `_SUBSIZE=2048` / `_USE_CUSTOM_CONFIG=y` /
   `_UBIFS_LEBSIZE=0x1f000` / 自定义 ubinize.cfg
2. `post-image.sh` 的 `ERASEBLOCK_SIZE` 改 128KiB，并输出 `rootfs.ubi`
3. `ubinize.cfg`：rootfs 卷 + data 卷（autoresize），按 128MB 重算
4. bootargs 改 `root=ubi0:rootfs ubi.mtd=<N> rootfstype=ubifs`
   （内核 `CONFIG_CMDLINE=""`，靠 U-Boot 传）

> 注意：卷名必须叫 **`rootfs`**（对应 root=ubi0:rootfs），这与我们旧 NAND 镜像里必须叫 `ubifs` 不同，别混用。

### 缺口 2：WiFi（WS73 / Hi3873V100 USB）

- 现状：cv6xx 内核 `CONFIG_WIRELESS / WLAN / RFKILL` **全部 not set**，整套无线被裁掉
  （defconfig 里有注释明确说明原因）。
- **但 `USB_DWC3` + `USB_XHCI_*` 已开** —— WS73 是 USB 接口，物理层基础在。
- 解法：
  1. 重编 `openipc/linux` 的 `hisilicon-hi3516cv6xx` 分支，打开 `cfg80211 / mac80211 / RFKILL`；
  2. WS73 驱动（`muxfix.ko` + `plat_soc.ko` + `wifi_soc.ko`）作为 out-of-tree 包加入；
  3. **存在偷懒路径**：这些 ko 在第三方固件里已针对 **5.10.221** 编好，vermagic 为
     `5.10.221 SMP mod_unload ARMv7 thumb2 p2v8`，与 OpenIPC 官方 ko 的 vermagic **字符串完全一致** →
     在无 `MODVERSIONS` 的情况下大概率可直接 `insmod`。需实机验证。
  4. 用户态 `wpa_supplicant -Dnl80211` 需随 rootfs 补回（当前 config 里被注释掉）。

### 缺口 3：OS05L10 注册接入（已解决编译，待解决注册）

| 子项 | 现状 | 下一步 |
|---|---|---|
| `libsns_os05l10.so` 编译 | ✅ 通过 | 已完成 |
| 传感器名注册表 | ❌ `open_sys_config.ko` 硬编码只有 sc4336p/gc4023/sc431hai/sc450ai/sc500ai/os04d10 | 源码 `kernel/sys_config/hi3516cv6xx/sys_cfg.c` 可得 → 加 `os05l10` 并重编该 ko |
| majestic 如何选 sensor | 待确认（`/etc/majestic.yaml` 的 `isp.sensorConfig` 或 `fw_printenv sensor`） | 实机跑一次即可定 |
| ISP 参数 | 现直接沿用 os04d10 的 `*_cmos_param.h`（284,774 B，同源） | 颜色正常但非最优；后续应从原厂 `resource/scene_param/sensor_os05l10/` 抽取真参数 |

---

## 4. 两条落地路线

### 路线 A（推荐先行）：免刷机，在现 TF 卡系统上验证 OpenIPC 用户态

**理由**：设备当前正好从 **TF 卡 vfat 根** 启动（`mount` 显示 `/dev/root on / type vfat`，7GB 可用），
根目录**可直接写**，是天然的沙盒；失败了 TF 卡拔下来就恢复，NAND 里的工作系统毫发无损。

步骤：
1. 打包「OpenIPC 用户态子集」：`/usr/bin/majestic` + 其 `DT_NEEDED` 全量 `.so`
   + `/usr/lib/sensors/libsns_os05l10.so` + 一份 `/etc/majestic.yaml` 模板；
2. 推到 TF 卡某目录（如 `/opt/oipc/`）；
3. `killall -INT sample_venc` 干净释放 MPP；
4. `LD_LIBRARY_PATH=/opt/oipc/lib /opt/oipc/majestic` 前台跑，看串口/终端输出；
5. 判断：能否拿到帧、ISP 是否起、libsns 是否被 dlopen。

风险：本机 MPP 为 `ot_*.ko`（非 openhisilicon），majestic 可能依赖某些只有 `open_*` 才提供的行为。
若失败则退阶：先换上具有同名的 OpenIPC `open_*.ko`（vermagic 已一致）再试。

### 路线 B（正统 destination）：完整 OpenIPC 固件

1. 177 上取 `openipc/linux` 分支 `hisilicon-hi3516cv6xx` → 加 cfg80211/mac80211/RFKILL → 编 zImage + cv610-demb.dtb（并按本机改 DTS 的 NAND/memory/USB 节点）；
2. rootfs：**不推荐跑完整 buildroot**（177 只有 2 核 / 3GB，ultimate 全量包会跑很久且易 OOM），
   改为**官方预编译 rootfs 二次加工**：解包 → 注入 `libsns_os05l10.so` + WS73 驱动/脚本 + 网络配置 → 重新打包 UBIFS；
3. `open_*.ko` 从源码补编 `sys_config`（加 os05l10）；
4. 用现有 ToolPlatform 流程刷入（分区表走我们已验证的 env.bin / 4 分区方案）。

> 若确需 buildroot 从零构建，务必先把 `zerotier / aws-webrtc / vtund / uacme / motors / exfat` 等裁掉，
> 否则 177 大概率撑不住。

---

## 5. 下一步优先级

| 优先级 | 任务 | 依赖 | 备注 |
|---|---|---|---|
| **P0** | 路线 A 冒烟：TF 卡上跑 majestic + `libsns_os05l10.so` | 设备在线、停当前应用 | 零风险，一次即可判定用户态可行性 |
| P1 | `sys_cfg.c` 加 `os05l10` 并重编 `open_sys_config.ko` | 177 源码就位 | 源码已在 `/home/zhang/oipc-saz/openhisilicon` |
| P2 | 内核加 cfg80211/mac80211/RFKILL，验证 WS73 能否 insmod | 克隆 `openipc/linux` 分支（约数百 MB） | vermagic 一致是利好信号 |
| P3 | rootfs 改 UBIFS + ubinize + bootargs → 出可刷 NAND 固件 | DTS、rootfs 加工完成 | 走已成熟的 ToolPlatform 流程 |
| P4 | ISP 参数从原厂 `scene_param/sensor_os05l10` 替换掉 os04d10 借来的 `_cmos_param.h` | 出图后 | 画质精调 |

---

## 6. 资产与坑清单（续）

**新工具**
- `tools/oipc_sensorlib.py` —— 端到端构建/回拉传感器库
- `tools/pingdev.py` —— Windows 下正确判定设备在线（绕过 cmd 中文编码）

**已踩/已知坑**
- 经 Windows 中转的源码**必须 `sed -i 's/\r$//'`**，否则含 CRLF 的 `.h` 会编译异常（脚本已内建）。
- 首次编译后若只改了 `.c` 而未清 `-force` 依赖状态，可能出现 dynsym 不全的假象 → 用脚本的 `clean all`。
- 177 与我们设备**不在同网段**（177 ping 不通 192.168.6.178），抓帧验证要在 Windows 主机侧做。
- UBIFS 卷名：OpenIPC 用 `rootfs`，我们旧 NAND 方案用 `ubifs`，两者不可混。
