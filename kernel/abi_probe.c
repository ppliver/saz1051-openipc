#include <linux/module.h>
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
