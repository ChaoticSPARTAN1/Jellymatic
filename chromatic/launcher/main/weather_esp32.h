/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The launcher's view of the Local Weather channel's network side (weather_esp32.c): the clock,
 * the fetch task, the cache and the hand-over of each reply to the channel's state
 * (chromatic/weather/wx.h). The channel itself (wx_enter, wx_tick, wx_draw, wx_audio_feed) is
 * called by the launcher on the state pj_wx_state() returns, only while the weather channel is on
 * screen (the TV app is ended first). Nothing here writes flash or NVS; no call waits on the
 * network. */
#ifndef PJ_WEATHER_ESP32_H
#define PJ_WEATHER_ESP32_H
#include <stddef.h>
#include <stdint.h>
#include "../../weather/wx.h"

/* The channel was selected. 0: pj_wx_state() is valid: the state kept from earlier in this boot
 * with its values and each request's age, or a new cleared one. -2 no memory. -6 the last close
 * or forget is still letting go (keep calling pj_wx_poll; ask again on a later pass). */
int pj_wx_open(void);
/* The channel was left: the round in flight is stopped. Once its task is gone (pj_wx_poll) the
 * round's block, the radar page and decoder are freed and SNTP is stopped. Kept for the rest of
 * the boot: the state and each request's next due time (about 2.8 KB). Returns at once. */
void pj_wx_close(void);
/* Another app needs the heap: as pj_wx_close, and the kept state and ages go too. Returns at once. */
void pj_wx_forget(void);
/* 1 while the fetch task, its stack, the round's block or its connection still exist. */
int pj_wx_busy(void);
/* Every main-loop pass, always: does the freeing after a close or forget. 1 while anything is still leaving. */
int pj_wx_poll(void);
/* Non-NULL from a successful pj_wx_open until pj_wx_close or pj_wx_forget. */
wx_state *pj_wx_state(void);
/* Every pass while open. Starts SNTP once Wi-Fi is up; starts a round when a request is due and
 * the heap gate passes; commits each finished reply on the caller's task; tells the channel when
 * a round runs (wx_set_updating). now_ms: the launcher's millisecond clock, continuous for the
 * whole boot (the ages are kept as due times on it). Never waits on the network. */
void pj_wx_service(uint32_t now_ms,int wifi_up);
/* 1: no new request starts and the round in flight is stopped. 0: rounds may start again. */
void pj_wx_pause(int paused);
int64_t pj_wx_epoch(void);         /* seconds since 1970 UTC; 0 until the clock is set (SNTP, or the first reply's Date) */
/* One diagnostics line body: counts only, never a place or an address. */
void pj_wx_diag(char *out,size_t size);
#endif
