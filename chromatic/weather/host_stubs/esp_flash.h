/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): flash reads only (the weather location record). The model in
 * check_weather_glue.c answers from its own 4 MiB picture of the flash and counts the reads. */
#ifndef WX_STUB_ESP_FLASH_H
#define WX_STUB_ESP_FLASH_H
#include "host.h"
typedef struct esp_flash_t esp_flash_t;
esp_err_t esp_flash_read(esp_flash_t *chip,void *buffer,uint32_t address,uint32_t length);
#endif
