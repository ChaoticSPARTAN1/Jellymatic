/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): one task, run a slice at a time by the model's scheduler. */
#ifndef WX_STUB_FREERTOS_TASK_H
#define WX_STUB_FREERTOS_TASK_H
#include "FreeRTOS.h"
typedef struct host_task *TaskHandle_t;
BaseType_t xTaskCreate(void (*code)(void *),const char *name,uint32_t stack,void *arg,UBaseType_t priority,TaskHandle_t *handle);
void vTaskDelay(TickType_t ticks);
void vTaskDelete(TaskHandle_t task);
TickType_t xTaskGetTickCount(void);
UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t task);
#endif
