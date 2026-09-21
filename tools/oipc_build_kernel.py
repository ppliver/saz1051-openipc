#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""oipc_build_kernel.py — 在 177 上构建 SAZ1051 用的 OpenIPC 内核。
分支: openipc/linux @ hisilicon-hi3516cv6xx (5.10.221)

为什么用 gcc10.3 而不是 OpenIPC 自带 gcc13.3:
  WS73 第三方 ko(plat_soc/wifi_soc/muxfix) 的 vermagic = "5.10.221 SMP mod_unload ARMv7 thumb2 p2v8"
  (无 gcc 标签)。为了让新内核 + 这些 ko 的 vermagic 完全一致、免 patch 直接 insmod, 本内核也用 gcc10.3。
  MPP open_*.ko 同理直接可用。

★★ 头号铁律: CONFIG_PM 必须为 n ★★  (2026-09-21 实锤, 见 abi_check())
  MPP 预编译模块 open_*.ko 来自 OpenIPC 固件(openipc.hi3516cv6xx-nor-ultimate), 与
  OpenIPC 官方 board/hi3516cv6xx/hi3516cv6xx.generic.config 同源 —— 该配置里
  "# CONFIG_PM is not set"。于是:
      CONFIG_PM=n  -> sizeof(struct dev_pm_info)=32  -> sizeof(struct device)=272
      CONFIG_PM=y  -> sizeof(struct dev_pm_info)=216 -> sizeof(struct device)=456  (Δ=+184)
  而 open_osal.ko 的 .data 中 g_media_bus(struct device, 272B) 与 g_media_bus_type
  (struct bus_type, 88B) 紧邻(0x160 / 0x270)。CONFIG_PM=y 时 device_register(&g_media_bus)
  会把内核写到 +456, 越过 272 边界 184 字节, 覆盖掉 g_media_bus_type.bus_groups(写成 2);
  紧接着 bus_register() 读到 bus_groups=2(非 NULL, 绕过 if(!groups) 保护)
    -> sysfs_create_groups -> internal_create_groups -> ldr r2,[0x2] -> kernel panic。
  现场特征: PC=internal_create_groups.part.0+0xc / LR=bus_register+0x135 /
            "virtual address 00000002" / r6=0x2 / Modules: open_osal(O+) open_sys_config(O)
  ⇒ 代价是 plat_soc.ko 需要的 register_pm_notifier 缺失(只有 CONFIG_PM_SLEEP 导出),
    由内建桩 drivers/vendor/saz_pm_stub.c 补上(见 PM_STUB_C)。

相对官方 hi3516cv610_defconfig 的增量(SAZ1051 片段):
  * 关 PM/SUSPEND/PM_SLEEP(ABI 铁律, 见上)
  * 无线栈(WIRELESS/cfg80211/mac80211/rfkill; 另加载厂商预编译 cfg80211_v20.ko)
  * 放开 /dev/mem 严格限制(majestic HAL 要 mmap 寄存器设 sensor clock)
  * CC_VERSION_TEXT=n 使 vermagic 与第三方 ko 对齐
  * 内建 PM notifier 桩
  官方 defconfig 已自带: MODULES=y(无 MODVERSIONS), SMP, THUMB2, MTD_SPI_NAND_BSP/FMC100,
  MTD_UBI, UBIFS_FS, USB_DWC3 —— 即 NAND/UBIFS 启动所需全部就位。

产物(177 上 /home/zhang/oipc-saz/stage/):
  uImage = uImage.itb(FIT: linux_kernel@0x40018000 + fdt-1@0x40000000), hi3516cv610-demb.dtb, modules/*.ko
  ★ 必须是 FIT: 本机 U-Boot 无 legacy/ATAGS 支持, bootm 旧式 uImage 会 "FDT and ATAGS
    support not compiled in" 并 reset。
"""
import os
import sys

import paramiko

HOST, USER, PW = "192.168.219.177", "zhang", "admin888"
LINUX = "/home/zhang/oipc-saz/linux"
# 第三方 SDK 的 gcc10.3(musl-1.2.3), 与 WS73 ko 同编译器
TC = "/home/zhang/saz610/tc/SMP_Linux_GCC_musl/gcc-20250305-arm-v01c02-linux-musleabi/arm-v01c02-linux-musleabi-gcc/bin/arm-v01c02-linux-musleabi-"
STAGE = "/home/zhang/oipc-saz/stage"
ABI_DIR = "/home/zhang/oipc-saz/abi_probe"
LOADADDR = "0x40018000"
# 钉死内核 release = 5.10.221, 与 OpenIPC 预编译 open_*.ko 所在目录(lib/modules/5.10.221)
# 及 WS73 ko 的 vermagic 完全一致; 否则 git 树会追加 "+" 变成 5.10.221+, 导致 modprobe
# 找不到预编译 open_*.ko。
KREL = "5.10.221"

# ★ MPP 预编译模块要求的 ABI 断言值(见 abi_check 的长注释)
ABI_DEVICE_SIZE = 272
ABI_PMINFO_SIZE = 32
ABI_BUS_TYPE_SIZE = 88

FRAGMENT = """# ---- SAZ1051 additions ----
# 注: CONFIG_CC_VERSION_TEXT 由构建系统强制为 gcc 标识字符串, 无法经 .config 置空。
# 解决 vermagic 不一致: 开 MODULE_FORCE_LOAD —— 内核强制加载 vermagic 不符的 ko(taint 但可用);
# 且 defconfig 无 MODVERSIONS → 无符号 CRC 校验, WS73 ko / 预编译 open_*.ko 均可直接 insmod。
CONFIG_WIRELESS=y
CONFIG_CFG80211=m
CONFIG_CFG80211_WEXT=y
CONFIG_CFG80211_CERTIFICATION_ONUS=n
CONFIG_MAC80211=m
CONFIG_MAC80211_STA=y
CONFIG_MAC80211_MESH=y
CONFIG_RFKILL=m
CONFIG_RFKILL_PM=y
CONFIG_WLAN=y
CONFIG_DEVMEM=y
CONFIG_STRICT_DEVMEM=n
CONFIG_IKCONFIG=y
CONFIG_IKCONFIG_PROC=y
CONFIG_MODULE_UNLOAD=y
CONFIG_MODULE_FORCE_LOAD=y
CONFIG_MODULE_FORCE_UNLOAD=y
CONFIG_ARM_APPENDED_DTB=y

# ★★★★★ CONFIG_PM 必须关闭 (ABI 铁律, 见文件头) ★★★★★
# 关掉它才使 sizeof(struct device)=272, 与 MPP 预编译 open_*.ko 的 g_media_bus(272B) 一致。
# 若开: dev_pm_info 216 -> struct device 456(Δ+184) -> device_register(&g_media_bus) 越界
#       写坏 g_media_bus_type.bus_groups -> bus_register() 解引用 0x2 -> panic。
# 这里的 "# ... is not set" 行 + 构建时的 scripts/config --disable 双保险。
# CONFIG_PM is not set
# CONFIG_SUSPEND is not set
# CONFIG_PM_SLEEP is not set
# CONFIG_PM_SLEEP_SMP is not set
# CONFIG_PM_AUTOSLEEP is not set
# CONFIG_PM_DEBUG is not set
# CONFIG_PM_ADVANCED_DEBUG is not set
# CONFIG_PM_GENERIC_DOMAINS is not set
# CONFIG_WQ_POWER_EFFICIENT_DEFAULT is not set

# ★ 早期串口诊断: 把 console + earlycon 编进内置 cmdline 并 EXTEND 到 bootloader cmdline。
#   动机: 本树 CONFIG_CMDLINE="" 且命令行全取自 bootloader; 一旦内核在 PL011 控制台注册前挂死,
#   串口就是 "Starting kernel ..." 之后全静默(实测踩过: vendor pm,sram 驱动 probe 崩溃)。
#   earlycon 能在控制台注册前打印 → 直接暴露卡点。
CONFIG_CMDLINE_EXTEND=y
CONFIG_CMDLINE="console=ttyAMA0,115200 earlycon=pl011,0x11040000"
"""

# ★ 本机 U-Boot 只支持 FIT(无 legacy/ATAGS: bootm 旧式 uImage 会 "FDT and ATAGS support
#   not compiled in" → reset)。故内核交付物必须是 FIT(.itb = linux_kernel + fdt-1),
#   DTB 由 FIT 的 fdt 子镜像提供, 不再依赖 appended DTB。
ITS = '''/dts-v1/;
/ {
    description = "OpenIPC SAZ1051 Hi3516CV610";
    #address-cells = <1>;
    images {
        fdt-1 {
            description = "dtb";
            data = /incbin/("/home/zhang/oipc-saz/stage/hi3516cv610-demb.dtb");
            type = "flat_dt";
            arch = "arm";
            compression = "none";
            load = <0x40000000>;
        };
        linux_kernel {
            description = "Linux";
            data = /incbin/("/home/zhang/oipc-saz/linux/arch/arm/boot/zImage");
            type = "kernel";
            arch = "arm";
            os = "linux";
            compression = "none";
            load = <0x40018000>;
            entry = <0x40018000>;
        };
    };
    configurations {
        default = "config-1";
        config-1 {
            description = "OpenIPC";
            kernel = "linux_kernel";
            fdt = "fdt-1";
            loadables = "linux_kernel", "fdt-1";
        };
    };
};
'''

# ★ 内建 PM notifier 桩 —— CONFIG_PM=n 时补上 plat_soc.ko(WS73 WiFi) 需要的两个符号。
#   加到 drivers/vendor/Makefile: obj-y += saz_pm_stub.o
#   注意: 不能 #include <linux/suspend.h> —— CONFIG_PM_SLEEP=n 时它把这两个函数定义成
#         static inline, 会与本文件的非 inline 定义冲突。
PM_STUB_C = '''// SPDX-License-Identifier: GPL-2.0
/*
 * SAZ1051: PM notifier 空实现桩 (内核以 CONFIG_PM=n 构建时的补偿)
 *
 * 为什么需要(2026-09-21 实锤, 详见 tools/oipc_build_kernel.py 文件头):
 *   - MPP 预编译 open_*.ko 是按 CONFIG_PM=n 的内核构建的, 故本内核必须 CONFIG_PM=n,
 *     否则 sizeof(struct device) 从 272 变 456, device_register(&g_media_bus) 会越界
 *     写坏 open_osal.ko 里的 g_media_bus_type.bus_groups, bus_register() 随即 panic。
 *   - 但 WS73 WiFi 的 plat_soc.ko 引用 register_pm_notifier/unregister_pm_notifier,
 *     这两个符号只在 kernel/power/main.c(CONFIG_PM_SLEEP) 里导出; CONFIG_PM=n 时缺失,
 *     insmod 会报 "plat_soc: Unknown symbol register_pm_notifier (err -2)"。
 *   - 摄像头常供电、从不 suspend, 空实现语义正确。
 *
 * 用 EXPORT_SYMBOL(非 _GPL) 以兼容 GPL / 非 GPL 消费者。
 */
#include <linux/export.h>
#include <linux/notifier.h>

#if !IS_ENABLED(CONFIG_PM_SLEEP)

int register_pm_notifier(struct notifier_block *nb)
{
	return 0;	/* 永不触发: 本内核没有 suspend/resume 路径 */
}
EXPORT_SYMBOL(register_pm_notifier);

int unregister_pm_notifier(struct notifier_block *nb)
{
	return 0;
}
EXPORT_SYMBOL(unregister_pm_notifier);

#endif /* !CONFIG_PM_SLEEP */
'''

PM_STUB_MAKE_LINE = "obj-y += saz_pm_stub.o"

# ★ ABI 探针: 把关键内核结构的 sizeof 暴露成符号尺寸, 构建时读出来做断言。
ABI_PROBE_C = '''#include <linux/module.h>
#include <linux/device.h>
#include <linux/kobject.h>
#include <linux/pm.h>
char sz_dev[sizeof(struct device)];
char sz_pminfo[sizeof(struct dev_pm_info)];
char sz_bus[sizeof(struct bus_type)];
char sz_pmops[sizeof(struct dev_pm_ops)];
static int __init abi_probe_init(void) { return 0; }
static void __exit abi_probe_exit(void) { }
module_init(abi_probe_init);
module_exit(abi_probe_exit);
MODULE_LICENSE("GPL");
'''


def client():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PW, timeout=30,
              look_for_keys=False, allow_agent=False)
    return c


# ---------------------------------------------------------------- 源码补丁(幂等)
PM_C = "/home/zhang/oipc-saz/linux/arch/arm/mach-vendor/pm.c"
USB_DTSI = "/home/zhang/oipc-saz/linux/arch/arm/boot/dts/hi3516cv610_family_usb.dtsi"
VENDOR_MK = "/home/zhang/oipc-saz/linux/drivers/vendor/Makefile"
PM_STUB_SRC = "/home/zhang/oipc-saz/linux/drivers/vendor/saz_pm_stub.c"


def patch_sources(c):
    """幂等应用必需的源码补丁(对齐可工作的 kernel #12)。

    (a) mach-vendor/pm.c —— vendor "pm,sram" 驱动 probe 会崩(release_resource(NULL) /
        漏判 ERR_PTR), 在 CONFIG_PM=y 下于控制台注册前挂死 → 串口在 "Starting kernel ..." 后
        全静默。按 K12 的做法: 补 err.h + IS_ERR_OR_NULL + of_match_table=NULL(NEUTERED)。
        ⚠️ 不能删 pm.o —— platsmp.c 调 hi35xx_pm_init() 会链接失败。
    (b) hi3516cv610_family_usb.dtsi —— vendor wing_usb.c 需要
        dr_mode="otg" + host-mode; + init_mode="host" 三项齐, 否则 USB 被切成 DEVICE,
        不注册 xHCI host、不上 VBUS, WS73 永不枚举。
    (c) drivers/vendor/saz_pm_stub.c + Makefile —— CONFIG_PM=n 下补 PM notifier 符号(见 PM_STUB_C)。
    """
    sftp = c.open_sftp()
    changed = []

    # ---- (a) pm.c ----
    with sftp.open(PM_C) as f:
        s = f.read().decode("utf-8")
    n = s
    if "#include <linux/err.h>" not in n:
        n = n.replace('#include "asm/io.h"', '#include "asm/io.h"\n#include <linux/err.h>', 1)
    if "if (!pm_device->sram_base) {" in n:
        n = n.replace("if (!pm_device->sram_base) {",
                      "if (IS_ERR_OR_NULL(pm_device->sram_base)) {", 1)
    if ".of_match_table = of_match_ptr(hi35xx_pm_ids)," in n:
        n = n.replace(".of_match_table = of_match_ptr(hi35xx_pm_ids),",
                      ".of_match_table = NULL, /* NEUTERED: avoid probing broken vendor "
                      "pm,sram suspend driver (release_resource(NULL) crash); camera never "
                      "suspends, WiFi unaffected */", 1)
    if n != s:
        with sftp.open(PM_C, "w") as f:
            f.write(n)
        changed.append("pm.c")

    # ---- (b) USB dtsi ----
    with sftp.open(USB_DTSI) as f:
        s = f.read().decode("utf-8")
    n = s
    if 'init_mode="device"' in n:
        n = n.replace('init_mode="device"', 'init_mode="host"', 1)
    if "host-mode;" not in n and "support-drd;" in n:
        n = n.replace("support-drd;", "support-drd;\n\t\t\thost-mode;", 1)
    if n != s:
        with sftp.open(USB_DTSI, "w") as f:
            f.write(n)
        changed.append("usb.dtsi")

    # ---- (c) PM notifier 桩 ----
    try:
        with sftp.open(PM_STUB_SRC) as f:
            have = f.read().decode("utf-8")
    except IOError:
        have = None
    if have != PM_STUB_C:
        with sftp.open(PM_STUB_SRC, "w") as f:
            f.write(PM_STUB_C)
        changed.append("saz_pm_stub.c")
    with sftp.open(VENDOR_MK) as f:
        s = f.read().decode("utf-8")
    if PM_STUB_MAKE_LINE not in s:
        if not s.endswith("\n"):
            s += "\n"
        s += "\n# SAZ1051: PM notifier stubs (needed when built with CONFIG_PM=n)\n"
        s += PM_STUB_MAKE_LINE + "\n"
        with sftp.open(VENDOR_MK, "w") as f:
            f.write(s)
        changed.append("drivers/vendor/Makefile")

    sftp.close()
    print("### 源码补丁: %s" % (", ".join(changed) if changed else "已是目标状态(无改动)"))


def sh(c, cmd, quiet=False, timeout=3600):
    if not quiet:
        print("### " + cmd.splitlines()[0], flush=True)
    _i, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if not quiet:
        if o:
            print(o.rstrip())
        if e:
            print("[stderr] " + e.rstrip()[:4000])
    return o, e


def apply_config(c):
    """生成 .config: defconfig + fragment, 再强制 --disable PM, 最后 olddefconfig + modules_prepare。

    ⚠️ 必须同步头文件! 只改 .config 而不同步 include/generated/autoconf.h,
       后续 `make M=... modules` 会静默沿用旧头文件 —— 实测踩过: 测出来的 struct 尺寸
       完全不变, 从而得出"改配置没用"的错误结论。
    ⚠️ 用 modules_prepare 而非 prepare。顶层 Makefile:
         modules_prepare: prepare
                 $(MAKE) $(build)=scripts scripts/module.lds
       即 modules_prepare 是 prepare 的**超集**, 额外生成 scripts/module.lds;
       而 `make M=... modules` 链接 .ko 要 `-T scripts/module.lds`, 缺了会
       "No rule to make target 'scripts/module.lds'" → Error 2。
       (2026-09-21 GitHub Actions 上实测踩到; 177 因早已全量编过一次才没暴露。)
    """
    sh(c, "cat > /home/zhang/oipc-saz/saz_fragment.cfg <<'EOF'\n%s\nEOF" % FRAGMENT, quiet=True)
    sh(c, "cd %s && cp arch/arm/configs/hi3516cv610_defconfig .config" % LINUX)
    sh(c, "cat /home/zhang/oipc-saz/saz_fragment.cfg >> %s/.config" % LINUX, quiet=True)
    # 双保险: 显式关 PM(不依赖 "# ... is not set" 行的解析)
    sh(c, "cd %s && scripts/config --disable PM_SLEEP_SMP --disable PM_SLEEP "
          "--disable SUSPEND --disable PM" % LINUX)
    sh(c, "cd %s && make ARCH=arm CROSS_COMPILE=%s olddefconfig 2>&1 | tail -3" % (LINUX, TC))
    sh(c, "cd %s && make ARCH=arm CROSS_COMPILE=%s modules_prepare 2>&1 | tail -3" % (LINUX, TC))
    sh(c, "cd %s && echo '--- scripts/module.lds (external module 链接必需) ---'; "
          "ls -la scripts/module.lds" % LINUX)
    sh(c, "cd %s && echo '--- PM (必须全为 not set) ---'; "
          "grep -E '^# CONFIG_(PM|SUSPEND|PM_SLEEP|PM_SLEEP_SMP) is not set' .config; "
          "grep -E 'define CONFIG_(PM|PM_SLEEP|SUSPEND) 1' include/generated/autoconf.h "
          "|| echo 'OK: autoconf.h 中无 PM/PM_SLEEP/SUSPEND'; "
          "echo '--- 其它关键项 ---'; "
          "grep -E '^CONFIG_(MODULES|SMP|THUMB2|MTD_SPI_NAND_BSP|MTD_UBI|UBIFS_FS|USB_DWC3|"
          "STRICT_DEVMEM|CFG80211|WIRELESS|IKCONFIG)=' .config" % LINUX)


def abi_check(c):
    """★ ABI 守卫: 断言 sizeof(struct device)==272。

    必须通过才能继续编译 —— 这是 MPP 预编译 open_*.ko 能否加载的判据(见文件头)。
    依赖: apply_config() 已跑过 olddefconfig + prepare。
    """
    sftp = c.open_sftp()
    try:
        sftp.mkdir(ABI_DIR)
    except IOError:
        pass
    with sftp.open(ABI_DIR + "/probe.c", "w") as f:
        f.write(ABI_PROBE_C)
    with sftp.open(ABI_DIR + "/Makefile", "w") as f:
        f.write("obj-m := probe.o\n")
    sftp.close()

    sh(c, "cd %s && make -C %s M=%s ARCH=arm CROSS_COMPILE=%s KERNELRELEASE=%s modules 2>&1 | tail -3"
       % (ABI_DIR, LINUX, ABI_DIR, TC, KREL))
    out, _ = sh(c, "%sreadelf -sW %s/probe.ko | grep -E ' sz_(dev|pminfo|bus|pmops)$' "
                   "| awk '{print $3, $8}'" % (TC, ABI_DIR), quiet=True)

    sizes = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 2 and p[0].isdigit():
            sizes[p[1]] = int(p[0])

    dev = sizes.get("sz_dev")
    print("### ABI 断言: sizeof(struct device)    = %s  (期望 %d = MPP g_media_bus 大小)"
          % (dev, ABI_DEVICE_SIZE))
    print("### ABI 断言: sizeof(struct dev_pm_info)= %s  (期望 %d)"
          % (sizes.get("sz_pminfo"), ABI_PMINFO_SIZE))
    print("### ABI 断言: sizeof(struct bus_type)  = %s  (期望 %d)"
          % (sizes.get("sz_bus"), ABI_BUS_TYPE_SIZE))
    print("### ABI 断言: sizeof(struct dev_pm_ops)= %s" % sizes.get("sz_pmops"))

    ok = (dev == ABI_DEVICE_SIZE
          and sizes.get("sz_pminfo") == ABI_PMINFO_SIZE
          and sizes.get("sz_bus") == ABI_BUS_TYPE_SIZE)
    if not ok:
        print("")
        print("!!! ABI 失配 —— 拒绝继续编译 !!!")
        print("!!! sizeof(struct device)=%s, 期望 %d。" % (dev, ABI_DEVICE_SIZE))
        print("!!! 若带此 ABI 启动, open_osal.ko 的 g_media_bus(272B) 会被")
        print("!!! device_register() 越界写坏紧邻的 g_media_bus_type.bus_groups,")
        print("!!! bus_register() 随即解引用 0x2 → kernel panic")
        print("!!! (PC=internal_create_groups.part.0+0xc, LR=bus_register+0x135)。")
        print("!!! 排查: .config 里 CONFIG_PM 是否被别的项 select 回来了?")
        print("!!!       (改完 .config 一定要 olddefconfig + prepare 再量尺寸)")
    return ok


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    c = client()
    try:
        patch_sources(c)
        if cmd in ("config", "abi", "build"):
            apply_config(c)
            if not abi_check(c):
                return 1
            if cmd in ("config", "abi"):
                return 0
        if cmd == "build":
            # 编译 zImage + dtb + 模块(KERNELRELEASE 钉死 release, 避免 5.10.221+)
            sh(c, "cd %s && make ARCH=arm CROSS_COMPILE=%s KERNELRELEASE=%s -j2 "
                  "zImage dtbs modules 2>&1 | tail -25" % (LINUX, TC, KREL), timeout=9000)
            # 生成 FIT(.itb) —— U-Boot 只认 FIT; 内核 cmdline 由 U-Boot bootargs 提供,
            # DTB 由 FIT fdt 子镜像提供。
            sh(c, "cd %s && mkdir -p %s && cp arch/arm/boot/dts/hi3516cv610-demb.dtb %s/ 2>/dev/null"
                % (LINUX, STAGE, STAGE))
            sh(c, "cat > /home/zhang/oipc_saz1051.its <<'EOF'\n%s\nEOF" % ITS, quiet=True)
            sh(c, "cd %s && mkimage -f /home/zhang/oipc_saz1051.its %s/uImage.itb 2>&1 | tail -14"
                % (LINUX, STAGE))
            sh(c, "cp %s/uImage.itb %s/uImage && ls -la %s/uImage %s/uImage.itb %s/hi3516cv610-demb.dtb"
                % (STAGE, STAGE, STAGE, STAGE, STAGE))
            # 导出模块
            sh(c, "cd %s && rm -rf %s/modules && make ARCH=arm CROSS_COMPILE=%s KERNELRELEASE=%s "
                  "INSTALL_MOD_PATH=%s/modules modules_install 2>&1 | tail -5"
                % (LINUX, STAGE, TC, KREL, STAGE))
            sh(c, "echo '=== 验证 PM 桩已编入 ==='; "
                  "grep -c saz_pm_stub %s/drivers/vendor/built-in.a 2>/dev/null || true; "
                  "echo '=== 内核导出的 PM notifier 符号 ==='; "
                  # ⚠️ 勿加 '$' 行尾锚点: Module.symvers 行是
                  #    "0x00000000\tregister_pm_notifier\tvmlinux\tEXPORT_SYMBOL\t"
                  #    —— 结尾是 TAB, 不是 EOL。曾因 '...$' 误报"桩未被链接"(2026-09-21)。
                  "grep -E '(register_pm_notifier|unregister_pm_notifier)' %s/Module.symvers || "
                  "echo '!! 桩未被链接(Module.symvers 无该符号)'; "
                  "echo '=== vmlinux 中桩是否为 GLOBAL 定义(T) ==='; "
                  "%sreadelf -sW %s/vmlinux | grep -E ' (register_pm_notifier|unregister_pm_notifier)$' || "
                  "echo '!! vmlinux 无该符号 —— 检查 drivers/vendor/Makefile 的 obj-y 行'; "
                  "echo '=== vermagic (取本内核编出的 cfg80211.ko; 期望 "
                  "5.10.221 SMP mod_unload ARMv7 thumb2 p2v8) ==='; "
                  "strings %s/modules/lib/modules/%s/kernel/net/wireless/cfg80211.ko 2>/dev/null "
                  "| grep -m1 'vermagic=' || echo '(cfg80211.ko 未找到)'; "
                  "echo '=== outputs ==='; ls -la %s/uImage %s/hi3516cv610-demb.dtb; "
                  "find %s/modules -name '*.ko' | wc -l"
                % (LINUX, LINUX, TC, LINUX, STAGE, KREL, STAGE, STAGE, STAGE))
        elif cmd == "status":
            sh(c, "ls -la %s/uImage %s/hi3516cv610-demb.dtb %s/arch/arm/boot/zImage 2>/dev/null || "
                  "echo 'not built yet'" % (STAGE, STAGE, LINUX))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
