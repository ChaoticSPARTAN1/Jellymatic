/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * ESP32 binding of the on-console setup (shared/setup_ui.h): owns the setup's memory (all of it on
 * the heap, only while the setup is open: static DRAM here is a few pointers and flags), runs the
 * screens' requests (Wi-Fi scan, Jellyfin discovery, HTTP for Quick Connect, the provisioning
 * commit) and feeds the answers back on the main task. See setup_esp32.c. Built only with
 * PJ_ENABLE_SETUP=1 (chromatic/launcher/CMakeLists.txt). */
#ifndef PJ_SETUP_ESP32_H
#define PJ_SETUP_ESP32_H
#include <stdint.h>
#include "setup_ui.h"
/* Heap the setup insists on before opening: its own state plus room for the largest job
 * (Quick Connect reply 8 KiB, the commit's three 4 KiB buffers) and the shell's own needs. */
#define PJ_SETUP_HEAP_MIN (40u*1024u)
/* Opens the setup: reads the saved network names (and whether a sign-in exists) from the provisioning
 * sectors, allocates every buffer, and returns the setup or NULL (not enough heap / a worker still
 * running). `account`: this console's current sign-in from the shell's RAM copy (for Logout), or NULL. */
/* connected_ssid: the saved network the shell is on (marked "connected" in the list), or NULL. */
pj_setup *pj_setup_esp32_open(const pj_setup_account *account,int wifi_connected,const char *connected_ssid,const char *version);
/* Frees everything (wiped first). Only after pj_setup_esp32_busy() is 0. */
void pj_setup_esp32_close(pj_setup *s);
/* Every main-loop pass while the setup is open: advances timers, starts a requested job in a worker
 * task (scan, discovery, HTTP) or runs the commit inline, and delivers finished results to the
 * screens. Returns 1 when the screen should be redrawn. */
/* radio_busy: the shell's radio check or join task is running (the scan then refuses and can be retried);
 * wifi_connected: the shell has joined a network (Quick Connect needs it). */
/* join_state / join_fail: the shell's join (PJ_SU_JOIN_*, pj_join_words index), for the Switching screen. */
int pj_setup_esp32_service(pj_setup *s,uint32_t now,int radio_busy,int wifi_connected,const char *connected_ssid,int join_state,int join_fail);
/* 1 while a worker task is still running (the shell must not free the setup meanwhile). */
int pj_setup_esp32_busy(void);
/* 12 random bytes for a fresh Jellyfin DeviceId (esp_fill_random). */
void pj_setup_esp32_random(uint8_t out[12]);
#endif
