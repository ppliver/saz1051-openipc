# libsns_os05l10 的 I2C 写不落地 —— 修复与构建

OS05L10 在 SAZ1051（Hi3516CV610 + OpenIPC）上出流后，**AE/AWB 完全不工作**、冷启动 MIPI 无数据。
本目录记录根因、修复点与重建方式。完整取证过程见
[`docs/SAZ1051-libsns-I2C写不落地根因与修复.md`](../docs/SAZ1051-libsns-I2C写不落地根因与修复.md)。

## 现象

- 出流靠 init 里的 `os05l10_replay.sh`（`i2ctransfer` 一次性手写 209 条初始化表）兜底；
- sensor 曝光寄存器（bank1 `0x03-0x06`）10~15 秒采样**纹丝不动**；
- majestic 日志里**一条 I2C 报错都没有**；
- 内核 `bsp-i2c` 超时计数 = 0（总线根本没在挣扎）。

## 根因链（钉死到代码行）

```
os05l10_cmos.c  .bus_info = { .i2c_dev = -1 }        ← 初值，majestic 从不调 pfn_set_bus_info
      ↓
cis_i2c_init()  dev_num = (td_u8)(-1) == 255
      →  open("/dev/i2c-255") 失败 → 返回 TD_FAILURE
      →  cmos_isp_init() 提前 return
      →  209 条初始化表【从未执行】，之后每次 AE/AWB 写都因 fd<0 静默丢弃
```

为什么一直没人发现：`isp_err_trace()` 在发行构建里**不输出**，失败路径完全静默。

排除了的错误方向（都验证过，别再走一遍）：

| 方向 | 排除依据 |
|---|---|
| 内核 `bsp-i2c` 驱动坏了 | `i2c-bsp.c` 失败必返回 `-EIO`；健康态超时计数 0 |
| 内核 sensor host 写错寄存器（`sensor_cfg=sc4336p`） | 占位名只影响内核侧；用户态 libsns 走 `/dev/i2c-N` |
| 8-bit/7-bit 地址错误 | 库内 `addr>>1`，`0x78→0x3C` 正确（1B 地址读 ID 得 `05 53 01 01`） |

## 修复（3 处）

### 1. `os05l10_cmos.c` —— 总线号给真值

```c
-        .bus_info = { .i2c_dev = -1 },
+        /* SAZ1051: 板载 OS05L10 挂在 i2c-0 */
+        .bus_info = { .i2c_dev = 0 },
```

### 2. `sensor_common.c` `cis_i2c_init()` —— 无效总线号兜底 + 可诊断

```c
-    dev_num = (td_u8)bus_info->i2c_dev;
+    if (bus_info->i2c_dev < 0 || bus_info->i2c_dev > 7) {
+        printf("[sns] i2c_dev=%d invalid, fallback to bus 0\n", bus_info->i2c_dev);
+        dev_num = 0;
+    } else {
+        dev_num = (td_u8)bus_info->i2c_dev;
+    }
```
并在 `open()` / `ioctl(I2C_SLAVE_FORCE)` 失败分支加 `printf`（因为 `isp_err_trace` 不发声），
成功后打印 `[sns] i2c ready: /dev/i2c-0 fd=%d slave=0x3c`。

### 3. `oipc_init.sh` —— 连带改造（**不做这条，修好 libsns 反而起不来**）

libsns 修好后 warm-up 实例会**真的**把 ISP 初始化成功，SIGINT 退出后仍留 `ISP[0] already inited`，
正式实例必然 `Cannot start SDK`。所以 init 必须：

- warm-up **见好就收**：等日志出现 `SET_DEV_ATTR`（lane mode 已设）即 kill，不要跑完 `init_isp`；
- `clear_isp()`：`rmmod open_isp && modprobe open_isp` 清 ISP 状态，**`open_mipi_rx` 不动**（保住 lane mode）；
- 监督循环**有界等待**：裸 `wait $MP` 在 majestic 卡死时永不返回 → 健康检查停转 → `reboot -f` 兜底永不触发；
- 计数器初始化 `maj_fail=${maj_fail:-0}`（busybox ash 未初始化会报 `invalid number ''`，计数永远加不上）。

## 重建

构建机 177 上（OpenIPC buildroot 工具链，musl）：

```sh
TC=/home/zhang/oipc-saz/tc/arm-openipc-linux-musleabi_sdk-buildroot/bin/arm-linux-
D=/home/zhang/oipc-saz/openhisilicon/libraries/sensor/hi3516cv6xx/omnivision_os05l10
cd $D && make clean && make CC=${TC}gcc
```

产物 `libsns_os05l10.so`（约 95KB），**修复版 md5 `9378f86e6d8b5accc365f3775e6ad43d`**。

用 `tools/oipc_build_rootfs.py` 打包时，脚本会**直接取构建产物**（不再用 nandpkg 里的副本 ——
副本曾漂移成旧版 `a50e0280`），并在构建期断言 md5，不符即拒绝出包。

## 验证判据

| 项 | 修复前 | 修复后 |
|---|---|---|
| majestic 日志 | 无 `[sns]`、无 `Init OK` | `[sns] i2c ready: /dev/i2c-0 fd=28 slave=0x3c` + `== OS05L10 MIPI 2880x1620@30fps 10bit linear Init OK! ==` |
| majestic fd 表 | **没有任何 `/dev/i2c-*`** | 有 `/dev/i2c-0` |
| bank1 exp 寄存器 | 恒定 `0x049A`（replay 的静态值） | 随场景变化（`0x049A ↔ 0x1AC7`） |
| 出流 | 依赖 replay 脚本 | libsns 自己写表 |

## 文件说明

- `os05l10_cmos.c` / `sensor_common.c`：修改后全文（源自 Hi3516CV610 vendor SDK，
  改动点见上文；因 vendor SDK 未公开，这里保留全文以便复现）。
- `../rootfs/majestic.yaml`：设备上实测通过的配置（5MP `2880x1620@20`、**关** `video1`/`jpeg`/`watchdog`）。
  官方默认 `video0+video1+jpeg` 全开需要 VB 池 35.6MB > 32MB MMZ → `ERR_VENC_NO_MEM` + OOM。
- `../rootfs/oipc_init.sh`：含 warm-up 见好就收 + clear_isp + 有界监督的加固版。
