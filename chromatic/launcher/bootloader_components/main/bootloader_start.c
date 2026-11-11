/* SPDX-FileCopyrightText: 2015-2022 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Apache-2.0
 *
 * ESP-IDF v5.3 second-stage entry with one change: which image to try first comes
 * from pj_boot_choose() instead of otadata. Nothing here writes flash. Selecting
 * the launcher only sets the first candidate: if it fails validation, the IDF loader
 * falls back to the factory slot, which holds the stock firmware.
 */
#include <stdbool.h>
#include "sdkconfig.h"
#include "esp_rom_sys.h"
#include "soc/reset_reasons.h"
#include "bootloader_init.h"
#include "bootloader_utility.h"
#include "bootloader_common.h"
#include "boot_decision.h"

_Static_assert(PJ_RESET_POWER_ON==RESET_REASON_CHIP_POWER_ON,"power-on reason changed");
_Static_assert(PJ_BOOT_STOCK==FACTORY_INDEX,"factory index changed");

void __attribute__((noreturn)) call_start_cpu0(void)
{
    if (bootloader_init() != ESP_OK) {
        bootloader_reset();
    }
    bootloader_state_t bs = {0};
    if (!bootloader_utility_load_partition_table(&bs)) {
        bootloader_reset();
    }
    int index = pj_boot_choose(esp_rom_get_reset_reason(0), bs.app_count > 0 && bs.ota[0].size != 0);
    bootloader_utility_load_boot_image(&bs, index);
}

/* Return global reent struct if any newlib functions are linked to bootloader */
struct _reent *__getreent(void)
{
    return _GLOBAL_REENT;
}
