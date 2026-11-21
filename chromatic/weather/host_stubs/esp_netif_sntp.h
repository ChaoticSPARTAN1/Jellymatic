/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): the fields and calls of esp_netif_sntp.h that are used. */
#ifndef WX_STUB_ESP_NETIF_SNTP_H
#define WX_STUB_ESP_NETIF_SNTP_H
#include <stdbool.h>
#include "host.h"
typedef struct esp_sntp_config {bool smooth_sync,server_from_dhcp,wait_for_sync,start;size_t num_of_servers;const char *servers[1];} esp_sntp_config_t;
#define ESP_NETIF_SNTP_DEFAULT_CONFIG(server) {.smooth_sync=false,.server_from_dhcp=false,.wait_for_sync=true,.start=true,.num_of_servers=1,.servers={server}}
esp_err_t esp_netif_sntp_init(const esp_sntp_config_t *config);
void esp_netif_sntp_deinit(void);
#endif
