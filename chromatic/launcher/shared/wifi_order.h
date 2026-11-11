/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Which saved Wi-Fi network the console tries first ("home, then my parents' house,
 * then home again: reconnect rapidly"; launcher 1.2.9: "where do I go to change back to the other
 * network?"). Up to four networks are saved, and the saved list IS the owner's priority list: top first.
 * The console scans once (about 1-2 s) and tries the saved networks it heard in saved order, then the
 * ones it did not hear in saved order (a hidden network never answers a scan). Signal strength no longer
 * decides between two saved networks that are both in range: "Use this network now" in Setup moves a
 * network to the top, and that choice must win over a stronger neighbour. A failed or empty scan keeps
 * the saved order. Nothing is written to flash by the join: the order is worked out again every time.
 * Pure C, no ESP-IDF: checked on the PC by check_launcher.c. */
#ifndef PJ_WIFI_ORDER_H
#define PJ_WIFI_ORDER_H
#include <stdint.h>
#include <string.h>

#define PJ_WIFI_HEARD_MAX 24u   /* access points kept from one scan */

typedef struct {
    uint8_t ssid[32];   /* as the scan reported it, not NUL-terminated at 32 bytes */
    uint8_t len;        /* 0..32 */
    int8_t rssi;        /* dBm */
} pj_wifi_heard;

/* The strongest signal among the scan entries whose name equals `ssid` exactly, or 0 with *found = 0. */
static inline int8_t pj_wifi_best_rssi(const char *ssid,const pj_wifi_heard *heard,unsigned heard_count,int *found) {
    size_t len=strnlen(ssid,33);
    int8_t best=-128;*found=0;
    if(!len||len>32)return 0;
    for(unsigned i=0;i<heard_count&&i<PJ_WIFI_HEARD_MAX;i++) {
        if(heard[i].len!=len||memcmp(heard[i].ssid,ssid,len))continue;
        if(!*found||heard[i].rssi>best)best=heard[i].rssi;
        *found=1;
    }
    return *found?best:0;
}

/* Fills order[0..count) with the indices 0..count-1 of `ssids` (count <= 8): the networks heard in the
 * scan first, in saved order (the list is the owner's priority list), then the rest in saved order.
 * Returns how many were heard. */
static inline unsigned pj_wifi_join_order(const char *const *ssids,unsigned count,const pj_wifi_heard *heard,
                                          unsigned heard_count,uint8_t *order) {
    int found[8];unsigned heard_n=0,n=0;
    if(count>8)count=8;
    for(unsigned i=0;i<count;i++){pj_wifi_best_rssi(ssids[i],heard,heard_count,&found[i]);if(found[i])heard_n++;}
    for(unsigned i=0;i<count;i++)if(found[i])order[n++]=(uint8_t)i;
    for(unsigned i=0;i<count;i++)if(!found[i])order[n++]=(uint8_t)i;
    return heard_n;
}
#endif
