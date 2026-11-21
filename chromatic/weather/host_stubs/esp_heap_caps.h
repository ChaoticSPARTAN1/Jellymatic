/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): a counting heap. */
#ifndef WX_STUB_ESP_HEAP_CAPS_H
#define WX_STUB_ESP_HEAP_CAPS_H
#include "host.h"
#define MALLOC_CAP_8BIT 4u
#define MALLOC_CAP_INTERNAL 2048u
void *heap_caps_malloc(size_t size,uint32_t caps);
void *heap_caps_calloc(size_t n,size_t size,uint32_t caps);
void heap_caps_free(void *block);
size_t heap_caps_get_largest_free_block(uint32_t caps);
#endif
