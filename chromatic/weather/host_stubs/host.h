/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-ins for the ESP-IDF headers chromatic/launcher/main/weather_esp32.c includes, so that
 * very file can be compiled and run on the PC by chromatic/weather/check_weather_glue.c. Only what
 * that file uses is declared; the behaviour behind the declarations is the model in the check.
 * This header is force-included (-include): it gives the file the model's clock in place of the
 * C library's. Never used in a console build. */
#ifndef WX_HOST_STUBS_H
#define WX_HOST_STUBS_H
#include <stddef.h>
#include <stdint.h>
#include <sys/time.h>
#include <time.h>
time_t host_time(time_t *out);
int host_settimeofday(const struct timeval *tv,const void *zone);
#define time host_time
#define settimeofday host_settimeofday
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL (-1)
#endif
