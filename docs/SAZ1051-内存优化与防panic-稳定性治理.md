# SAZ1051 内存优化与防 panic 稳定性治理（2026-09-22）

Hi3516CV610（**64MB** 内存）+ OS05L10 5MP + OpenIPC。
目标：解决"访问 Web 后台后摄像头离线"这一必现故障，并把可用内存从濒死水位拉回来。

---

## 1. 故障现象

NAND 镜像刷入、出流均正常，但运行 6~13 分钟后整机重启、网络不通；
串口可见 UBIFS `bad CRC / expected node type 9` 风暴，shell 仍活着但网络进程起不来。

用户反馈"可能是我访问 web 后台导致的"——**判断正确**。

## 2. 因果链（串口取证）

```
访问 /cgi-bin/live.cgi（首页会自动 302 到它）
  → 额外内存申请，而可用内存只剩 ~1MB
  → Out of memory: Killed process 1157 (majestic) anon-rss:6012kB      (rc=137)
  → init 监督逻辑调用 clear_isp() 执行 rmmod open_isp
  → 但 VI/MIPI 中断仍在产生，上下文已失效
  → Unable to handle kernel NULL pointer dereference at 0000004c
      [vi_drv_capture_irq_route+0x7e, open_vi]
  → Kernel panic: Fatal exception in interrupt  →  整机重启
```

**UBIFS `bad CRC` 是次生灾害**：OOM 那一刻 UBIFS 正在写，节点半写损坏。
所以"刷机三次都校验通过"是对的——镜像和 NAND 都没问题，问题在运行期。

复现方法（100% 命中，优化前）：
`curl -u root:admin888 http://192.168.6.178/cgi-bin/live.cgi`
（`/` 首页 200、`/mjpeg` 500、`/image.jpg` 503 均不致命，**只有 live.cgi 致命**。）

## 3. 内存账（mem=32M）

| 项 | 占用 |
|---|---|
| MemTotal | 25.7 MB |
| Slab | 7.1 MB（UBI/UBIFS 元数据为主） |
| VmallocUsed | 8.2 MB（MPP 模块 ≈3.8MB、WiFi 驱动等） |
| AnonPages | 6.5 MB（**majestic 独占 6.0MB**） |
| **MemAvailable** | **≈0.9 MB** |

关于"配置算中等"：对 PC 成立，对这颗 64MB 的 SoC 不成立。
OpenIPC 官方在 64MB 板上通常跑 1080p/3MP，**5MP 是超配**。

## 4. 已实施的优化（init v3 + majestic.yaml）

### 4.1 init v3（`rootfs/oipc_init.sh`）

1. **`safe_reboot()`**：统一走 `sync` + `echo s` + `echo b > /proc/sysrq-trigger`。
   - 本机 busybox `reboot -f` **无效**（内核 reboot 通路不通）。
   - 重启前 sync，避免再次产生 UBIFS 半写节点。
2. **异常退出不再 `rmmod open_isp`**：被 OOM/信号杀（rc≥128）或强杀时，
   VI 中断可能仍在跑，卸 ISP 必然 panic → 改为直接整机重启兜底。
   只有"优雅退出（warm-up SIGINT / 正常 exit）"路径才允许 `clear_isp()`。
3. **内存水位守护**：每 15s 查 `MemAvailable`，低于 `MEM_LOW_KB`（默认 700）
   主动 `kill -INT` majestic 并优雅重启，抢在 OOM-kill 之前。
   - ⚠️ 阈值必须低于稳定态实际水位，否则会不停重启 majestic。
4. **tmpfs 限额**：`/tmp` 6M、`/run` `/var/run` 各 1M，防止 HLS/日志撑爆内存。
5. **majestic.log 轮转**：超过 400 行截断到 100 行。

### 4.2 majestic.yaml

- `hls: enabled: false`
- `audio: enabled: false`

**效果：MemAvailable 0.9 MB → 4.5 MB（+3.6MB）。**
这是本次最大的单项收益，且无任何画质代价。

## 5. 试过但**不可用**的做法（勿重试）

### 5.1 trim_modules：卸载"看起来没用"的 MPP 模块

卸掉 `open_ive / open_svp_npu / open_h265e / open_jpege / open_vca / open_piris`
及音频全套（约 0.86MB），结果 majestic 启动即：

```
Unable to handle kernel NULL pointer dereference at virtual address 00000038
PC is at vpss_get_vb_cfg+0xe/0x18 [open_vpss]
```

**MPP 各模块间存在隐式耦合（VB 池配置随之失效），不能按"看起来没用"来卸。**

### 5.2 提高 `mem=` 给内核加内存

5MP 下 VENC 需要 MMZ **剩余 ≥8MB** 才能创建通道：

| mem= | MMZ | MMZ 剩余 | VENC |
|---|---|---|---|
| 32M | 32MB | 8.9MB | ✅ 成功 |
| 34M | 30MB | 7.1MB | ❌ `ERR_VENC_NO_MEM` |
| 36M | 28MB | 5.0MB | ❌ `ERR_VENC_NO_MEM` |
| 40M | 24MB | 0.9MB | ❌ `ERR_VENC_NO_MEM` |

**结论：5MP 模式下 `mem` 只能 = 32M，无法靠调 bootargs 增容。**
（mem=40M 时可用内存确实能到 12.5MB，但 VENC 建不起来 → 不出流。）

要真正增容，必须先降低 VB 池需求（降分辨率或减少 blkCnt）。

## 6. 优化后验证

| 项 | 优化前 | 优化后 |
|---|---|---|
| MemAvailable | 920 kB | **4568 kB** |
| `ERR_VENC_NO_MEM` | — | 0（出流正常，346 包/391KB @6s） |
| live.cgi 连打 5 次 | 立即 100% 丢包 + panic | **全部 200，设备存活** |
| 访问后内存跌落 | OOM | 仅 -380 kB |
| dmesg panic/OOM 计数 | 必现 | **0** |
| 30 分钟长时监控 | 6~13 分钟即重启 | 无重启、内存稳定在 ~4.2MB |

## 7. 救援工具（本次新增）

- **`tools/rescue_shell.py`**：init 脚本改坏导致 panic 循环时的救命通道。
  在 U-Boot 层把 `bootargs` 的 `init` 换成 `/bin/sh`，直接挂载 NAND 的 UBIFS 根给裸 shell，
  改回坏文件即可。几十秒完成，**不需要重刷**。
  - ⚠️ 裸 shell 里 `/proc` 未挂载，`echo b > /proc/sysrq-trigger` 会失败；
    必须先 `mount -t proc proc /proc` 再重启。
- **`tools/com3_cmd.py`**：SSH 起不来时通过 COM3 串口执行命令。
- **`tools/mem_watch.py`**：长时内存/稳定性监控（识别重启、panic、UBIFS 错误）。

## 8. 后续可选项（按性价比）

1. 降一档分辨率（2880x1620 → 2304x1296 或 1080p）：VB 池从 20.5MB 降到 ~13MB，
   可把 `mem` 提到 44M，可用内存 ≈16MB。代价是画质。
2. 精简 rootfs 文件数，降低 UBIFS 的 TNC/slab 占用。
3. 调小 ISP `blkCnt`（majestic VB sizing 现为 3 块）以省约 6.8MB MMZ。
