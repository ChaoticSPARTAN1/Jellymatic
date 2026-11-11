/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Additional terms: ADDITIONAL-TERMS.md */
#ifndef PJ_ESP32_MEDIA_H
#define PJ_ESP32_MEDIA_H
#include "media_bridge.h"
#include "esp_tls.h"
#include "driver/spi_master.h"
#define PJ_MEDIA_QSPI_TAG ((void *)(uintptr_t)0x504a5153u)
typedef struct {
    const char *pc_ipv4;uint16_t port;
    const uint8_t *certificate_der;size_t certificate_size;
    const uint8_t *pairing;size_t pairing_size;
} pj_esp_media_config;
typedef struct {
    esp_tls_t *tls;pj_media_bridge *bridge;pj_media_ops board;
} pj_esp_media;
/* The caller must bring up Wi-Fi (stock firmware has none) and the SPI/UART services.
 * No time sync is needed: certificate dates are not checked (no clock).
 * Uses no NVS, cartridge, bootloader or flash APIs. Calling task owns object.
 * Board callbacks must be bounded and share stock SPI/UART arbitration. */
int pj_esp_media_connect(pj_esp_media *,pj_media_bridge *,const pj_esp_media_config *,pj_media_ops board);
int pj_esp_media_step(pj_esp_media *,uint32_t now_ms);
int pj_esp_media_pocket_bytes(pj_esp_media *,const uint8_t *,size_t,uint32_t now_ms);
void pj_esp_media_close(pj_esp_media *);
/* Describe only: caller supplies DMA-safe data and owns qualified bus mutex.
 * Stock post_cb must skip PJ_MEDIA_QSPI_TAG as well as its existing CART tag;
 * otherwise these writes incorrectly signal an LVGL OSD row completion.
 * Does not submit a transaction or touch the device. */
int pj_esp_media_qspi_describe(spi_transaction_t *,const uint8_t *,size_t);
#endif
