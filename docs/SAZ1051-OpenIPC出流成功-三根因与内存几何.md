# SAZ1051 OpenIPC 出流成功 —— 三条真根因与内存几何（2026-09-21）

> 结论先行：SAZ1051（Hi3516CV610 + OS05L10 + WS73）**OpenIPC 移植线已在设备上实测出流**：
> 主机侧真拉 `rtsp://192.168.6.178:554/`（Digest `root/12345`），8 秒收到 **3722 个 RTP 包 / 4,402,865 字节 ≈ 537 KB/s（约 4.3 Mbps）**，
> SDP 为 `m=video 0 RTP/AVP 96 / rtpmap:96 H264/90000 / profile-level-id=4d0032 / sprop-parameter-sets=Z00AMpY1QFoBm/PNQ… / a=framerate:20`。

---

## 0. 现状快照（可用配置）

| 项 | 值 |
|---|---|
| 内核命令行 | `console=ttyAMA0,115200 clk_ignore_unused mem=32M root=ubi0:ubifs rootfstype=ubifs rw ubi.mtd=3 mtdparts=nand:512K(u-boot.bin),512K(env.bin),4M(uImage),96M(rootfs.ubifs) init=/opt/oipc/oipc_init.sh panic=10` |
| MMZ | 32MB @ `0x42000000`（`mmz_size = totalmem(64) - mem(32)`） |
| majestic | `video0` 5MP h264 20fps 4096k VBR **开**；`video1` **关**；`jpeg` **关**；watchdog 建议开 |
| sensor | OS05L10，i2c-0/0x3c，**8-bit 寄存器地址 + `0xFD` bank**，表由 `/opt/tools/os05l10_replay.sh` 重放 |
| RTSP | `rtsp://<ip>:554/`，单轨 H264（另有 ONVIF metadata 轨） |
| 设备侧指标 | `mipi_rx: lane_mode 4 / cil_clk HS_LP00 / lane0_data 0x4 lane2_data 0x4 / mipi_vc0 2880x1620`；`venc chn0: 2880x1620 H264`；`int_cnt` 有中断 |

---

## 1. 根因一：sensor 是 8-bit 寄存器，且 `libsns` 的 I2C 写不落地

**A/B 实测（决定性）**：

| 测试 | 命令 | 结果 |
|---|---|---|
| 1 字节寄存器地址 | `i2ctransfer -y 0 w1@0x3c 0x02 r4` | **`05 53 01 01`** ← 与原厂日志里读到的 chip id 逐字节吻合 |
| 2 字节寄存器地址 | `i2ctransfer -y 0 w2@0x3c 0x00 0x02 r4` | `4c 05 53 01` ← 整体偏移 1 字节 |

⇒ OS05L10 **不是** 16-bit 地址器件（0xFD 做 bank 切换）。这类器件上"用 2 字节地址写寄存器"不会报错（I2C 仍然 ACK），但**写进了错误的寄存器**——所以"有应答却零数据"。

我们自己的表（`build/3516cv610_app/src/device/sensor/omnivision_os05l10/os05l10_cfg.h`）写法本来就对（8-bit + 显式 `0xfd` bank，与 OpenIPC 官方 `os04d10` 同构），**但设备上的 `libsns_os05l10.so` 那 209 条表一条都没进 sensor**：

- majestic 日志里**没有** init 函数的那行 `printf`（只有 `[puts] linear mode`）；
- 回读表内特征寄存器全是出厂默认：`0x20=0x0b`（表要 `0x1f`）、`0xc1=0xcc`（表要 `0xee`）、`0xa0=0x00`（表要 `0x01`）、`0x8f=0x48`（表要 `0x40`），而 `0xFD` 确认 = 0（读的 bank 没错）；
- 设备上的 `.so` md5 与我们本地构建一致（`a50e0280…`）→ 不是拿错文件。

**兜底修复（已固化）**：`/opt/tools/os05l10_replay.sh` —— 从上述头文件的表自动生成，用 shell 逐条重放：

```sh
i2ctransfer -y 0 w2@0x3c 0xfd 0x00      # bank 0
i2ctransfer -y 0 w2@0x3c 0x03 0x01      # software reset
sleep 1
# … 209 条 (reg,val) …
i2ctransfer -y 0 w2@0x3c 0xfd 0x00      # bank 0
i2ctransfer -y 0 w2@0x3c 0x00 0x01      # stream ON
```

**重放前后对照**：

| 指标 | 重放前 | 重放后 |
|---|---|---|
| `cil_clk_cur_stat` | `IDLE` | **`HS_LP00`** |
| `lane0_data` | `0x0` | **`0xc1`** |
| `mipi_vc0_w × h` | `0 × 0` | **`2880 × 1620`** |
| ISP `int_cnt` | 0 | **30fps** |
| 回读 `0x20 / 0xc1..c5` | 默认值 | **`0x1f` / `ee 00 00 01 50`** ✓ |

> ⚠️ 遗留：AE/AWB 也走同一条 `cis_write_reg` 路径，因此**曝光/白平衡的自动调节可能同样写不进去**（画面是表内固定曝光）。彻底修法仍是查 `libsns` 为何写不落地（对比 `sensor_common.c` 的 `cis_write_reg` 与我们的 obj 绑定）。

---

## 2. 根因二：MIPI 的 lane mode 时序 —— 必须"两段式启动"

现象：boot 时 majestic 四连报

```
[sdk] mipi_ioctl: OT_MIPI_ENABLE_MIPI_CLOCK    on /dev/ot_mipi_rx: No such file or directory
… (ENABLE_SENSOR_CLOCK / UNRESET_MIPI / UNRESET_SENSOR 同样失败)
[sdk] mipi_ioctls: sensor and MIPI receiver only partly unparked
```

但 `/dev/ot_mipi_rx` 在 majestic 启动前**确实存在**（init 诊断日志打了时间戳证明），`dd` 也能 open。

**真因（dmesg 里 `mipirx not set lane mode` 揭示）**：

1. **lane mode 只能由用户态设置**（驱动无模块参数、DTS 的 `mipi_rx@0x173c0000` 节点里没有任何 lane 属性）；
2. 驱动在 **lane mode 未设置时拒绝**这些 ioctl（返回 ENOENT，不是"文件不存在"）；
3. 而 majestic 的顺序是"**先** MIPI 时钟/复位 ioctl，**后** `SET_DEV_ATTR`（设 lane mode）"→ **全新启动必然四连失败**；
4. 之后 lane mode 被设上了，**再启动一次就成功**（这正是"手动前台跑 majestic 能 unpark"的原因）。

**修法（已写进 `oipc_init.sh`）**：

- `[5b]` **循环 open `/dev/ot_mipi_rx`（O_RDWR，与 majestic 一致）直到成功**，不再只看节点是否存在；
- **两段式启动**：第一段"预热"跑到设置 lane mode 为止就优雅退出，第二段正式启动 → 四个 ioctl 直接 `unparked`。实测日志：

```
run#1: mipi_ioctls@6310: sensor and MIPI receiver only partly unparked
run#2: mipi_ioctls@6308: sensor and MIPI receiver unparked               ← 成功
```

- init 里必须 **`wait()` 回收子进程**：不回收 → majestic 变僵尸 → 它扫 `/proc` 判定"another instance is running" → **再也起不来**（`killall -9` 也无效）。监督循环负责回收 + 崩溃自动重启。

---

## 3. 根因三：内存几何与 majestic 的 VB 池公式

64MB DDR 板，`load_hisilicon` 的几何是 `mmz_size = totalmem(64MB) - mem`：

| `mem=` | 内核 MemTotal | MMZ | 结果 |
|---|---|---|---|
| 28M | 21.7MB | 36MB | ❌ 内核不够：majestic 刚加载 sensor 库即被 **OOM 杀**（rss 仅 1.3MB） |
| **32M** | **25.7MB** | **32MB** | ✅ 内核够用、MMZ 够 5MP 单流 |
| 30M | 23.7MB | 34MB | ⚠️ 内核开始吃紧，出现 1 次 OOM（仍在 5MP+jpeg 场景失败） |

**majestic 的 VB 池公式（实测反推）**：每个"全分辨率流"占用一块 `W×H×1.5`，5MP 即 **6,998,400 字节**。

| 启用组合 | VB 池需求（日志 `dump_vb_configuration`） | 结果 |
|---|---|---|
| video0(5MP) + video1 + jpeg | `[0]: 6998400 ×4` + `[1]: 6998400 ×1` + `[2]: 608256 ×1` ≈ **35.6MB** | ❌ VENC 5MP `ERR_VENC_NO_MEM`，回退成 704x576 |
| video0(5MP) + jpeg | `[0]: 6998400 ×3` + `[1]: 6998400 ×1` = **28MB** | ❌ 32MB 与 34MB MMZ 下**都** NO_MEM |
| **video0(5MP) 单流** | `[0]: 6998400 ×2` + `[1]: 6998400 ×1` = **21MB** | ✅ `VENC chn 0: 2880x1620 H264` 建立、出流 |

⇒ 5MP H.264 的 VENC 自身还需要约 **9MB** MMZ。因此：

- **5MP + jpeg 不可行**：需 28 + 9 + 1 ≈ 38MB MMZ → `mem=26M` → 内核只剩 ~19MB → 必 OOM。
- 想同时要快照/子码流，只能**降主码流分辨率**（例如 2560x1440：块 5.53MB ×4 ≈ 22MB，仍能塞进 32MB MMZ）。

> ⚠️ OpenIPC 官方这块板的默认 `majestic.yaml`（2724B）是 `video0` 5MP **on** / `video1` **off** / **`jpeg: true`** —— 在 64MB RAM + 32MB MMZ 的 SAZ1051 上跑不通（jpeg 那一条会直接让 5MP 建不起来）。**镜像里必须把 `jpeg.enabled` 改成 `false`**，或者把 `video0.size` 降下来。

---

## 4. 复现步骤（从零到出流）

```sh
# 1) 内核 env：MMZ 与内核的分界
mkdir -p /run/lock /var/lock        # ★ fw_setenv 需要锁目录，/run 是 tmpfs 重启即失
fw_setenv bootargs 'console=ttyAMA0,115200 clk_ignore_unused mem=32M \
  root=ubi0:ubifs rootfstype=ubifs rw ubi.mtd=3 \
  mtdparts=nand:512K(u-boot.bin),512K(env.bin),4M(uImage),96M(rootfs.ubifs) \
  init=/opt/oipc/oipc_init.sh panic=10'
fw_printenv bootargs | grep -o 'mem=[0-9]*M'   # ★ 必须回读确认（踩过：锁目录不存在 → 静默没写进去）

# 2) 码流配置（/etc/majestic.yaml）
#    video0: enabled true / codec h264 / fps 20 / bitrate 4096 / rcMode vbr / gopSize 1  (不写 size = 用 sensor 原生 2880x1620)
#    video1: enabled false
#    jpeg  : enabled false        ← 64MB 板必须关，否则 5MP 建不起来

# 3) 启动链（/opt/oipc/oipc_init.sh，由内核 cmdline init= 直接执行）
#    load_hisilicon → [5b] 等 /dev/ot_mipi_rx 可 O_RDWR 打开 → 预热 majestic → 正式 majestic
#    第 6 步后：若无 MIPI 数据则调用 /opt/tools/os05l10_replay.sh

# 4) 验证
cat /proc/umap/mipi_rx | grep -E 'cil_clk_cur_stat|mipi_vc0_w'   # HS_LP00 / 2880
cat /proc/umap/venc    | grep -E '^ 0 '                          # 0 2880 1620 H264 …
python .tmp/rtsp_probe.py 192.168.6.178 554 8 root 12345         # RTP 计数
```

---

## 5. 踩坑清单（本次新增）

| 坑 | 表现 | 解法 |
|---|---|---|
| `fw_setenv` 无锁目录 | 命令"成功"但 env 没变（本次靠 `System RAM …41ffffff`=32MB 与预期 30MB 不符才发现） | 先 `mkdir -p /run/lock /var/lock`，**并 `fw_printenv` 回读** |
| `curl --digest` | 快照 401 | 用 `--anyauth`（majestic 走 Basic，realm `Authentication`） |
| `/image.jpg` 503 | `JPEG is switched off in this camera's configuration` | 需 `jpeg.enabled: true`（但 5MP 场景装不下，见 §3） |
| majestic 僵尸 | `killall -INT` 后 `pidof` 仍显示、`-9` 杀不掉、新实例拒绝启动 | init 里 `wait()` 回收 |
| **in-place 重启不释放 ISP** | 新实例 `ISP[0] already inited!` → `ERR_ISP_NOT_SUPPORT` → `Cannot start SDK` → 无 VENC / 无 :554 | init 监督循环加健康判定（进程存活 && `:554` LISTEN），连续 2 次失败就 `reboot -f`；重启间隔 3s→6s |
| ★ **`watchdog.enabled: true` 与两段式启动冲突** | watchdog 子进程会把被 SIGINT 的 warm-up 实例"复活"（日志出现 9s/13s/39s **三次**启动）→ 两个实例抢 ISP + 内存翻倍 → **OOM**，5MP 链整条废掉 | **保持 `watchdog: false`**；重启职责交给 init 的监督循环。若一定要 watchdog，必须去掉 init 里的 warm-up 与监督重启，二者只能留一个 |
| `dmesg` 里的 `mipirx not set lane mode` | 被误读成"驱动 bug" | 它是 §2 时序问题的**指纹**，不是根因 |
| MIPI 时钟 `0x11018440` | 文档记载原厂写 `0xa010 → 0x4010`（24MHz 分频） | 实测该寄存器**本来就是 `0x4010`**，MCLK 无问题，别再追这条 |

### 5.1 内存余量极小 —— 一次改动就会翻车
`mem=32M` 时 `MemFree ≈ 4.7MB / MemAvailable ≈ 5.7MB`。两个 majestic 实例并存（watchdog 复活的场景）就会 OOM。
**任何让 majestic 多起一个实例、或多分配一块全分辨率 VB 的改动，都要先算内存账。**

---

## 6. 待办

1. **持久化到可刷镜像**：把 `mem=32M`（env.bin）、`jpeg: false` 的 `majestic.yaml`、`os05l10_replay.sh`、新版 `oipc_init.sh` 一起进 `177:nandpkg/base/` 并重建 `rootfs_ubi.img`。
2. **彻底修 `libsns_os05l10.so` 的 I2C 写**（可去掉 replay 兜底、并让 AE/AWB 生效）。
3. 画质确认：解一段 H.264（本机 `.tmp/rtsp_dump.py` → 177 `ffmpeg`）出 JPEG 目检（洋红/曝光）。
4. 快照能力取舍：要么放弃 jpeg（现状），要么把 `video0.size` 降到 2560x1440 换回 jpeg/子码流。
5. watchdog 恢复 `enabled: true`（调试完再开，避免 300s 自杀干扰）。
