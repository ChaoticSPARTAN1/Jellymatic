/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Wi-Fi scan results for the on-console setup, with no driver calls: the ESP32 binding feeds
 * pj_scan_add() one network at a time (setup_esp32.c), and this keeps the list the screen
 * shows: strongest first, one row per name, hidden names left out, capped.
 *
 * The plain-word join results follow Shiny Relay (upstream/shiny-relay-src-1.1.5,
 * mcu/components/cwl/cwl_net.c, GPL-3.0, Shiny Gen 2026): wrong password, network not found,
 * refused, no address, did not answer. Our own words and code; the idea and the mapping of
 * the driver's disconnect reasons are theirs. */
#ifndef PJ_WIFI_SCAN_H
#define PJ_WIFI_SCAN_H
#include <stdint.h>
#include <string.h>
#define PJ_SCAN_MAX 12u
typedef struct {char ssid[33];int8_t rssi;uint8_t secured,saved;} pj_scan_entry;
typedef struct {pj_scan_entry net[PJ_SCAN_MAX];unsigned count,heard;} pj_scan_list;
static inline void pj_scan_reset(pj_scan_list *l){memset(l,0,sizeof(*l));}
/* 0..4 bars, the usual phone thresholds. */
static inline unsigned pj_scan_bars(int rssi){return rssi>=-55?4u:rssi>=-65?3u:rssi>=-75?2u:rssi>=-88?1u:0u;}
/* One heard network. ssid: up to 32 bytes, not NUL-terminated past `len`. Duplicate names
 * (several access points of one network) keep the strongest. Hidden (empty) names are left out.
 * Returns 1 when the list changed. */
static inline int pj_scan_add(pj_scan_list *l,const uint8_t *ssid,unsigned len,int rssi,int secured) {
    l->heard++;
    if(!len||len>32)return 0;
    for(unsigned i=0;i<len;i++)if(ssid[i]<0x20||ssid[i]==0x7f)return 0; /* control bytes: not a name the font can show */
    if(rssi<-127)rssi=-127;
    if(rssi>0)rssi=0;
    for(unsigned i=0;i<l->count;i++) {
        pj_scan_entry *e=&l->net[i];
        if(strlen(e->ssid)==len&&!memcmp(e->ssid,ssid,len)) {
            if(rssi>e->rssi){e->rssi=(int8_t)rssi;e->secured=(uint8_t)(secured!=0);} /* follow the strongest access point */
            else if(!secured)e->secured=0; /* any open access point of the name: joinable without a password */
            goto sort;
        }
    }
    if(l->count<PJ_SCAN_MAX) {
        pj_scan_entry *e=&l->net[l->count++];memset(e,0,sizeof(*e));
        memcpy(e->ssid,ssid,len);e->rssi=(int8_t)rssi;e->secured=(uint8_t)(secured!=0);
    } else {
        unsigned weakest=0;
        for(unsigned i=1;i<l->count;i++)if(l->net[i].rssi<l->net[weakest].rssi)weakest=i;
        if(rssi<=l->net[weakest].rssi)return 0;
        pj_scan_entry *e=&l->net[weakest];memset(e,0,sizeof(*e));
        memcpy(e->ssid,ssid,len);e->rssi=(int8_t)rssi;e->secured=(uint8_t)(secured!=0);
    }
sort:
    for(unsigned i=1;i<l->count;i++) { /* insertion sort, strongest first, stable */
        pj_scan_entry e=l->net[i];unsigned j=i;
        while(j>0&&l->net[j-1].rssi<e.rssi){l->net[j]=l->net[j-1];j--;}
        l->net[j]=e;
    }
    return 1;
}
/* Marks the rows whose name is one of the saved networks. */
static inline void pj_scan_mark_saved(pj_scan_list *l,const char *const *saved,unsigned saved_count) {
    for(unsigned i=0;i<l->count;i++) {
        l->net[i].saved=0;
        for(unsigned s=0;s<saved_count;s++)if(!strcmp(l->net[i].ssid,saved[s]))l->net[i].saved=1;
    }
}
/* Why a join failed, in plain words (the ESP32's WIFI_REASON_* codes; 0 = none). `associated`:
 * the access point accepted us but no address came (DHCP). `timed_out`: nothing at all.
 *
 * Launcher 1.3.4 ("Wi-Fi password is wrong" after pressing A soon after power-on, though
 * the password is right and the next start joins): ONE failed handshake is never called a wrong password.
 * AUTH_EXPIRE, the 4-way/group-key and handshake timeouts, AUTH_FAIL and CONNECTION_FAIL are also what an
 * access point gives a station it still holds from before the power cut (a stale association), or a
 * handshake that lost frames; the ESP32 driver cannot tell them from a bad key. So a single one reads "It
 * could not join", and the join tries the same network again (pj_join_tally below); only a handshake or
 * authentication failure on EVERY one of PJ_JOIN_ATTEMPTS tries, seconds apart, reads as a wrong password. */
enum {PJ_JOIN_OK,PJ_JOIN_WRONG_PASSWORD,PJ_JOIN_NOT_FOUND,PJ_JOIN_REFUSED,PJ_JOIN_NO_ADDRESS,PJ_JOIN_NO_ANSWER,PJ_JOIN_FAILED};
/* An authentication or key-handshake failure: the password could be the cause, or a passing state of the
 * access point; worth trying the same network again. */
static inline int pj_join_auth_reason(int wifi_reason) {
    switch(wifi_reason) {
    case 2:case 15:case 16:case 202:case 204:case 205:return 1; /* AUTH_EXPIRE, 4WAY/GROUP_KEY timeouts, AUTH_FAIL, HANDSHAKE_TIMEOUT, CONNECTION_FAIL */
    default:return 0;
    }
}
static inline int pj_join_result(int wifi_reason,int associated,int timed_out,int got_address) {
    if(got_address)return PJ_JOIN_OK;
    if(pj_join_auth_reason(wifi_reason))return PJ_JOIN_FAILED; /* one of these alone is not a verdict on the password */
    switch(wifi_reason) { /* esp_wifi_types.h values, so this header needs no driver include */
    case 201:return PJ_JOIN_NOT_FOUND;                                               /* NO_AP_FOUND */
    case 203:return PJ_JOIN_REFUSED;                                                 /* ASSOC_FAIL */
    case 1:case 3:case 4:case 5:case 8:return PJ_JOIN_REFUSED;                       /* UNSPECIFIED, DEAUTH/DISASSOC by the access point */
    default:break;
    }
    if(associated)return PJ_JOIN_NO_ADDRESS;
    if(timed_out)return PJ_JOIN_NO_ANSWER;
    return wifi_reason?PJ_JOIN_FAILED:PJ_JOIN_NO_ANSWER;
}
static const char *const pj_join_words[7]={"Connected","The password is wrong","The network was not found","The network refused us",
    "The network gave no address","The network did not answer","It could not join"};
/* The tries of ONE network in a join (launcher_main.c join_task): up to PJ_JOIN_ATTEMPTS, the next one only after an
 * authentication/handshake failure, PJ_JOIN_RETRY_MS(n) apart (2 s, then 5 s: past the moments after a power-on in
 * which an access point still holds the console's last association). No driver calls; host-tested. */
#define PJ_JOIN_ATTEMPTS 3u
#define PJ_JOIN_RETRY_MS(done) ((done)<=1u?2000u:5000u)
#define PJ_JOIN_RETRY_LINE "Wi-Fi: could not join yet, retrying"
typedef struct {uint8_t attempts,auth_failures,associated,timed_out;int last_reason;} pj_join_tally;
static inline void pj_join_tally_begin(pj_join_tally *t){memset(t,0,sizeof(*t));}
/* One attempt ended without an address: its disconnect reason (0: none), whether it was associated, timed out. */
static inline void pj_join_tally_add(pj_join_tally *t,int wifi_reason,int associated,int timed_out) {
    if(t->attempts<255u)t->attempts++;
    if(pj_join_auth_reason(wifi_reason)&&t->auth_failures<255u)t->auth_failures++;
    t->last_reason=wifi_reason;t->associated=(uint8_t)(associated!=0);t->timed_out=(uint8_t)(timed_out!=0);
}
/* 1: try the same network again (after PJ_JOIN_RETRY_MS(t->attempts)). Only an authentication/handshake failure is
 * tried again: a network not found, refused or silent is not fixed by asking it twice in a row. */
static inline int pj_join_retry(const pj_join_tally *t) {
    return t->attempts<PJ_JOIN_ATTEMPTS&&pj_join_auth_reason(t->last_reason);
}
/* The words for this network's tries: a wrong password only when EVERY try failed authentication. */
static inline int pj_join_verdict(const pj_join_tally *t) {
    if(t->attempts>=PJ_JOIN_ATTEMPTS&&t->auth_failures==t->attempts)return PJ_JOIN_WRONG_PASSWORD;
    return pj_join_result(t->last_reason,t->associated,t->timed_out,0);
}
#endif
