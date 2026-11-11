/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The whole dual-boot policy. Deliberately takes no stored state: there is no
 * flag, otadata or RTC word that can go stale or corrupt. The menu shows only on
 * a true power-on. Every other reset, including the launcher's own "Games" restart,
 * a crash or a watchdog, goes straight to the untouched stock firmware.
 */
#ifndef PJ_BOOT_DECISION_H
#define PJ_BOOT_DECISION_H
#define PJ_RESET_POWER_ON 1u /* RESET_REASON_CHIP_POWER_ON; checked at bootloader compile time */
#define PJ_BOOT_STOCK (-1)   /* FACTORY_INDEX */
#define PJ_BOOT_LAUNCHER 0   /* first OTA slot */
/* An invalid launcher needs no case here: the IDF loader tries this index, then
 * walks back to factory (bootloader_utility_load_boot_image). */
static inline int pj_boot_choose(unsigned reset_reason,int launcher_slot_exists) {
    return reset_reason==PJ_RESET_POWER_ON&&launcher_slot_exists?PJ_BOOT_LAUNCHER:PJ_BOOT_STOCK;
}
#endif
