/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): a 10 ms tick, as the launcher's configuration has. */
#ifndef WX_STUB_FREERTOS_H
#define WX_STUB_FREERTOS_H
#include "../host.h"
typedef uint32_t TickType_t;
typedef int BaseType_t;
typedef unsigned UBaseType_t;
typedef uint8_t StackType_t;
#define pdPASS 1
#define pdMS_TO_TICKS(ms) ((TickType_t)((ms)/10u))
#endif
