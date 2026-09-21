# WiFi 修复桩模块（WS73 / Hi3873V100）

SAZ1051 的 WiFi 是海思闭源栈 **WS73（Hi3873V100）**，OpenIPC 官方 osdrv 包里**不含**该驱动源码
（只有 `atbm-wifi` 等其它方案），因此只能复用厂商预编译的 `cfg80211_v20.ko` / `mac80211.ko` /
`plat_soc.ko` / `wifi_soc*.ko` / `muxfix.ko`。

## 问题

TF 卡 `/opt/saz_wifi/` 里原本的 3 个**依赖桩模块**（`rfkill.ko`、`firmware_class.ko`、
`libarc4.ko`）是**老 `CONFIG_PM=y` 内核**编的。OpenIPC 主线内核必须 `CONFIG_PM=n`
（否则 `open_*.ko` MPP 直接 panic，见 `docs/SAZ1051-MPP加载panic根因-CONFIG_PM-ABI铁律.md`），
所以老桩模块对 `PM=n` 新内核 `insmod` 静默失败 → `cfg80211_v20.ko` 缺
`rfkill_alloc` / `release_firmware` 等符号 → 整条 WiFi 依赖链崩塌
（`Unknown symbol` 累计 260 条：cfg80211 22 + mac80211 180 + wifi_soc 58）。

## 修复

用 **与 OpenIPC FIT 同一构建（CI run 35551646221，PM=n）** 编出的 in-tree 版本替换这 3 个桩模块。
vermagic 一致：`5.10.221 SMP mod_unload ARMv7 thumb2 p2v8`，且已确认导出
`rfkill_alloc` / `release_firmware` / `arc4_crypt` 等 WS73 所需符号。

```
wifi_fix/        PM=n 内核编的修正桩模块（部署用）
  ├─ rfkill.ko
  ├─ firmware_class.ko
  └─ libarc4.ko
wifi_fix_backup/  TF 卡上被替换下来的旧 PM=y 桩模块（回滚用）
  ├─ rfkill.ko.orig
  ├─ firmware_class.ko.orig
  └─ libarc4.ko.orig
```

## 部署（一次性，写到 TF 卡）

```sh
# 经 devssh 推到设备（设备需在原厂固件、SSH 可达）
python tools/devssh.py push wifi/wifi_fix/rfkill.ko        /opt/saz_wifi/rfkill.ko
python tools/devssh.py push wifi/wifi_fix/firmware_class.ko /opt/saz_wifi/firmware_class.ko
python tools/devssh.py push wifi/wifi_fix/libarc4.ko       /opt/saz_wifi/libarc4.ko
```

重启进 OpenIPC 内核后，加载链应为：
`rfkill → firmware_class → libarc4 → cfg80211_v20 → mac80211 → muxfix → plat_soc → wifi_soc_v15`
→ `wlan0` 出现、`/sys/class/net/wlan0/phy80211` 软链建立。

> 注意：不要在**原厂 #12 内核（PM=y）**上 `insmod` 这些 PM=n 模块——ABI 不匹配会失败。
> 它们只对 OpenIPC `CONFIG_PM=n` 内核有效。

## 备选（更干净的架构，未采用）

把内核 fragment 改 `CONFIG_CFG80211=n` + `CONFIG_MAC80211=n`，让无线完全交给厂商栈，
消除 in-tree/vendor cfg80211 的任何歧义。改动更大、需重编内核+重跑 CI，留作稳定后的清理项。
