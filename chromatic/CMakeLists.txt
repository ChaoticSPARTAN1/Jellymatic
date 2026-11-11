# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
idf_component_register(SRCS "esp32_media.c" INCLUDE_DIRS "."
    REQUIRES esp-tls esp_timer mbedtls esp_driver_spi freertos lwip)
