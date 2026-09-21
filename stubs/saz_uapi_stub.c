/*
 * saz_uapi_stub.c — SAZ1051 WS73(Hi3873) WiFi 驱动所需的 HiSilicon vendor 符号桩。
 *
 * 背景: plat_soc.ko / wifi_soc_v15.ko 引用了两个只在 HiSilicon BSP 内核里导出的符号:
 *   - uapi_tsensor_read_temperature  (WiFi 温度补偿)
 *   - uapi_efuse_lock                (efuse 读锁)
 * 本 OpenIPC(mainline) 内核没有它们 → insmod 报 "Unknown symbol" → WiFi 起不来。
 * 这里提供最小实现(温度固定 25°C, efuse 锁为空操作), 满足装载即可。
 *
 * 安全性: uapi_tsensor_read_temperature 的真实原型不确定((*temp) 还是 (id,*temp)),
 * 因此不盲目解引用参数 —— 只在内核地址范围内且 virt_addr_valid() 通过时才写入,
 * 两种调用约定都能正确处理, 且不会因野指针崩溃。
 */
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/mm.h>

int uapi_tsensor_read_temperature(unsigned long a, unsigned long b)
{
	/* 调用约定 1: (int *temperature)  -> a 是指针
	 * 调用约定 2: (unsigned id, int *temperature) -> b 是指针
	 * 只有落在内核地址空间且地址合法时才写, 否则当作"无输出参数"。 */
	if (a >= 0xC0000000UL && virt_addr_valid((void *)a)) {
		*(int *)a = 25;
	} else if (b >= 0xC0000000UL && virt_addr_valid((void *)b)) {
		*(int *)b = 25;
	}
	return 0;
}
EXPORT_SYMBOL(uapi_tsensor_read_temperature);

void uapi_efuse_lock(void) { }
EXPORT_SYMBOL(uapi_efuse_lock);

void uapi_efuse_unlock(void) { }
EXPORT_SYMBOL(uapi_efuse_unlock);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("SAZ1051 WS73 vendor-symbol stubs (tsensor/efuse)");
