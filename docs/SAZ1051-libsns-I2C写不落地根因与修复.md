# SAZ1051 — libsns I2C 写不落地：根因与修复（2026-09-22）

## 一句话结论

`libsns_os05l10.so` 的 I2C 写"不落地且不报错"，真因不是内核、不是 `bsp-i2c` 驱动，而是
**`bus_info.i2c_dev` 初值 `-1` → `open("/dev/i2c-255")` 失败 → 209 条初始化表从未执行，
此后所有 AE/AWB 写都因 `i2c->fd == -1` 被静默丢弃**。两行修复 + 一处兜底后，
初始化表、AE 曝光写全部落地，画面正常且无洋红。

## 根因链（代码级）

```
os05l10_cmos.c:26      .bus_info = { .i2c_dev = -1 }        ← 初值
        │  （majestic/MPP 从未调用 pfn_set_bus_info，初值原样保留）
        ▼
sensor_common.c:178    dev_num = (td_u8)bus_info->i2c_dev    == (td_u8)(-1) == 255
        ▼
sensor_common.c:184    open("/dev/i2c-255")  →  -1 (ENOENT)
        ▼
cis_i2c_init() 返回 TD_FAILURE
        ▼
os05l10_cmos.c:859  cmos_isp_init():
        ret = cis_i2c_init(cis);
        if (ret != TD_SUCCESS) { isp_err_trace("i2c init failed!"); return; }
        os05l10_linear_5m30_10bit_init(cis);   ← 209 条表【永不执行】
        sensor_state->init = TD_TRUE;          ← 永不置位
        ▼
之后每次 AE 曝光/增益写: cis_write_reg() 见 i2c->fd < 0
        → isp_err_trace("fd is invalid!") → return TD_FAILURE   ← 静默失败
```

**为什么一直没被发现**：
- `isp_err_trace` / `isp_info_trace` 在发行构建里不输出 → 日志里**一条 i2c 报错都没有**；
- `cmos_read_register` 是假实现（直接 `return TD_SUCCESS`），读路径也探测不到；
- 出流靠 init 脚本里 `os05l10_replay.sh` 用 `i2ctransfer` 重放整表兜底，掩盖了"libsns 自己写不进去"。

**排除过程（关键取证）**：

| 取证 | 结论 |
|---|---|
| majestic `/proc/PID/maps` 含 `libsns_os05l10.so`，但 fd 表**没有任何 `/dev/i2c-*`** | libsns 在进程内、却没打开 i2c 设备 |
| `.so` 里有 `/dev/i2c-%u` + `I2C_SLAVE_FORCE` 字符串 | 写路径就是用户态 i2c-dev |
| 健康态 `wait rx no empty` 超时计数 = **0** | `bsp-i2c` 内核通道根本没在挣扎（排除内核驱动层） |
| `i2c-bsp.c`：传输失败**会返回 `-EIO`** | 若真走到总线层必然报错 → 问题在更上层 |
| bank1 exp 寄存器 15s 纹丝不动 `0x049A` | AE 从未写进 sensor |
| maj日志无 `i2c init failed` / `fd is invalid` | trace 被日志级别吞掉 |

## 修复（3 处，均已部署）

1. **`os05l10_cmos.c:26`**：`.bus_info = { .i2c_dev = -1 }` → `.i2c_dev = 0`
   （板载 OS05L10 实际挂 i2c-0，`i2ctransfer -y 0` 早已验证）。
2. **`sensor_common.c` `cis_i2c_init()`**：无效总线号（`<0 || >7`）兜底为 0；
   并加 `printf` 诊断（`[sns] i2c ready: /dev/i2c-0 fd=N slave=0x3c`、失败时带 errno），
   因为 `isp_*_trace` 不可见。
3. **`oipc_init.sh` 配套改造**（见 §4）。

## 构建与部署

```
177:/home/zhang/oipc-saz/openhisilicon/libraries/sensor/hi3516cv6xx/omnivision_os05l10
  make CC=/home/zhang/oipc-saz/tc/arm-openipc-linux-musleabi_sdk-buildroot/bin/arm-linux-gcc
  产物 libsns_os05l10.so 95212B  md5 9378f86e6d8b5accc365f3775e6ad43d
```
已同步：`nandpkg/base/usr/lib/sensors`、`nandpkg/oipc_opt/sensors`、`stage/sensors`、
`rootfs_final/usr/lib/sensors`、`rootfs_ubifs/usr/lib/sensors`、设备 `/usr/lib/sensors/`。

## 验证证据

修复后 majestic 日志（修复前这两行**从未出现过**）：

```
[sns] i2c ready: /dev/i2c-0 fd=27 slave=0x3c        ← warm-up
[sns] i2c ready: /dev/i2c-0 fd=28 slave=0x3c        ← 正式实例
vi_pipe:0,== OS05L10 MIPI 2880x1620@30fps 10bit linear Init OK! ==   ← 209 条表落地
HiSilicon SDK started / RTSP server started on port 554
```

- **AE 曝光写落地**：exp 由 replay 静态值 `0x049A`(1178 行) → `0x1AC7`(6855 行，
  恰好 = `1827×(30/8)`，对应 majestic `Exposure: 125ms (8fps) slow_shutter`)，随后又自适应回落
  → AE 引擎活着的直接证据。
- **稳定性**：uptime 438s 无重启，majestic pid 不变，`:554`+`:80` 监听，
  VENC 序列 5195→8499 持续增长，OOM 0，i2c 超时 0。
- **拉流**：`rtsp://root:admin888@192.168.6.178:554` → 2837 RTP / 3,348,223B / 6s = **545 KB/s**。
- **画质**：解出 5MP 真帧，暗室 + 蓝色 LED 灯条，提亮后噪声中性灰，**无洋红偏色**
  （init 表里本就无 bank1 0x32 条目，replay 先写的 `0x32=0x00` 未被覆盖）。

## §4 init 配套改造（`oipc_init.sh`，已双端同步）

libsns 修好带来一个**新的连锁问题**：warm-up 实例现在能把 ISP 完整初始化成功，
SIGINT 退出后 `open_isp` 仍留着 `ISP[0] already inited` 状态 → 正式实例
`init_isp` 必然 `ERR_ISP_NOT_SUPPORT`。三处修法：

1. **warm-up 见好就收**：轮询日志等 `SET_DEV_ATTR`（lane mode 写进 `open_mipi_rx`）就立刻
   `kill -INT`，不再死等 12s（死等会让它跑完 `init_isp`）。
2. **`clear_isp()`**：warm-up 后/监督循环重启前 `rmmod open_isp && modprobe open_isp`
   清掉 inited 状态；`open_mipi_rx` **不动**，lane mode 保留，正式实例照样 unpark。
   （实测 `open_isp` 引用计数 0，可安全重载；已实测 `rmmod` 成功。）
3. **监督循环有界等待**：裸 `wait $MP` 在 majestic 卡死时永不返回 → 循环停转、
   `reboot -f` 兜底永不触发。改为轮询 + 周期 MIPI 复核 + 600s 超时 SIGKILL。

## 遗留 / 下一步

1. **出 NAND 包**：把新 `libsns_os05l10.so`、`oipc_init.sh`、`majestic.yaml`（jpeg/video1 off、
   watchdog off）、`mem=32M` env 一起进 `177:nandpkg/` → 重建 `rootfs_ubi*.img` → 刷机表。
   设备即可脱离"运行态手工补丁"。
2. **AE/AWB 策略**：majestic 当前 `HiSi_HAL_SetAeTime` 只在启动设一次（slow_shutter 125ms@8fps），
   随后 AE 自适应把曝光回落；若要固定帧率/固定曝光，需在 `/etc/majestic.yaml` 配 `isp:` 段。
3. **源码入库**：`omnivision_os05l10/`（含 `sensor_common.c` 修复）建议进
   `ppliver/saz1051-openipc` 仓库（当前只有内核线），CI 可顺带编 `.so`。
