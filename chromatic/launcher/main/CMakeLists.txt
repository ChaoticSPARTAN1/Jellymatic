# SPDX-License-Identifier: GPL-3.0-or-later
# Additional terms: ADDITIONAL-TERMS.md
if(PJ_ENABLE_SETUP)
    set(PJ_SETUP_SRCS "setup_esp32.c" "../shared/provision_write.c")
endif()
idf_component_register(SRCS "launcher_main.c" "jellyfin_esp32.c" ${PJ_SETUP_SRCS}
    INCLUDE_DIRS "." "../shared" "../../../pocket" "../../jellyfin"
    REQUIRES chromatic esp_driver_uart esp_driver_spi esp_driver_gpio esp_driver_i2s esp_wifi esp_netif esp_event hal spi_flash esp_hw_support lwip bootloader_support esp_http_client esp_rom)
if(PJ_ENABLE_SETUP)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PJ_ENABLE_SETUP=1)
endif()
