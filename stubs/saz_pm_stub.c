/*
 * saz_pm_stub.c — 为 CONFIG_PM 关闭的内核补上 PM notifier 的空实现。
 *
 * 背景(2026-09-21 实锤的一组双向约束):
 *
 *  (1) MPP 预编译模块 open_*.ko(OpenIPC 出处) 是按 "CONFIG_PM 未开" 的内核构建的:
 *        sizeof(struct dev_pm_info) = 32  ->  sizeof(struct device) = 272
 *      若本内核开 CONFIG_PM, dev_pm_info 变成 216 -> sizeof(struct device) = 456(Δ=+184)。
 *      而 open_osal.ko 的 .data 里 g_media_bus(struct device, 272B) 与
 *      g_media_bus_type(struct bus_type, 88B) 紧邻(0x160 / 0x270), 于是
 *      device_register(&g_media_bus) 让内核写到 +456 —— 越过 272 边界 184 字节,
 *      把 g_media_bus_type.bus_groups 覆盖成 2;
 *      随后的 bus_register() 读到 bus_groups=2(非 NULL, 绕过 if(!groups) 保护)
 *        -> sysfs_create_groups -> internal_create_groups -> ldr r2,[0x2] -> kernel panic
 *      (现场 PC=internal_create_groups.part.0+0xc, LR=bus_register+0x135, addr=00000002)
 *      ⇒ 本内核必须 CONFIG_PM=n。
 *
 *  (2) 但 WS73 WiFi 的 plat_soc.ko 引用 register_pm_notifier / unregister_pm_notifier,
 *      这两个符号只在 kernel/power/main.c(CONFIG_PM_SLEEP) 中导出 ⇒ CONFIG_PM=n 时缺失,
 *      insmod 报 "plat_soc: Unknown symbol register_pm_notifier (err -2)" -> WiFi 起不来。
 *
 * 解法: 在 CONFIG_PM=n 的内核上由本模块导出这两个符号的空实现。
 *   - 摄像头常供电、从不 suspend, 空实现语义正确(notifier 永不需要被触发);
 *   - 本模块必须在 plat_soc.ko 之前 insmod —— 先有导出, plat_soc.ko 才能解析该符号。
 *
 * 注意: 不要 #include <linux/suspend.h> —— CONFIG_PM_SLEEP=n 时它会把这两个函数定义成
 *       static inline, 与这里的非 inline 定义冲突。只包含 notifier.h 即可。
 *
 * 用 EXPORT_SYMBOL 而非 EXPORT_SYMBOL_GPL: 兼容 GPL / 非 GPL 消费者
 * (plat_soc.ko 的 license 未确认, 普通导出对两者都放行)。
 */
#include <linux/module.h>
#include <linux/notifier.h>

int register_pm_notifier(struct notifier_block *nb)
{
	return 0;	/* 永不触发: 内核没有 suspend/resume 路径 */
}
EXPORT_SYMBOL(register_pm_notifier);

int unregister_pm_notifier(struct notifier_block *nb)
{
	return 0;
}
EXPORT_SYMBOL(unregister_pm_notifier);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("SAZ1051 no-op PM notifier stubs (kernel built with CONFIG_PM=n)");
