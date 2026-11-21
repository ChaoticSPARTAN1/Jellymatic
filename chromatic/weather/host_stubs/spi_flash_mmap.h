/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): mapping flash into the data address space (1.6.5: the location pack is read in place).
 * The model in check_weather_glue.c answers from its picture of the flash, counts the mappings still open and can
 * refuse one (as when no MMU page is free). */
#ifndef WX_STUB_SPI_FLASH_MMAP_H
#define WX_STUB_SPI_FLASH_MMAP_H
#include "host.h"
typedef enum {SPI_FLASH_MMAP_DATA, SPI_FLASH_MMAP_INST} spi_flash_mmap_memory_t;
typedef uint32_t spi_flash_mmap_handle_t;
esp_err_t spi_flash_mmap(size_t src_addr,size_t size,spi_flash_mmap_memory_t memory,const void **out_ptr,
                         spi_flash_mmap_handle_t *out_handle);
void spi_flash_munmap(spi_flash_mmap_handle_t handle);
#endif
