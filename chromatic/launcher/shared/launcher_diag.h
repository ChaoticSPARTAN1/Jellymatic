/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Diagnostic lines the launcher prints on its USB serial console. Remote Pilot (and
 * any serial monitor) reads them read-only; the same text goes into the RAM log ring
 * that the CHR1 "log" action will serve over Wi-Fi in launcher 1.2.
 *
 * Line format (docs/Design-notes.md):
 *     PJ-DIAG t=<ms since boot> <event> key=value key=value ...
 * Events: boot, phase, close, done, wifi, stats, handover.
 *
 * Rules every line keeps, enforced by pj_diag_safe() before printing:
 *  - plain printable ASCII, shorter than PJ_DIAG_MAX;
 *  - only counts, fixed names and small numbers: never a network name, password, key,
 *    MAC, serial number, flash content or any other identifier of the unit;
 *  - never contains "PJ-LAUNCHER" or "rst:", which Chromatic Manager's boot watch
 *    reads, and is never printed after PJ-LAUNCHER-TO-STOCK.
 * Portable C: the PC test formats every event through these same functions.
 */
#ifndef PJ_LAUNCHER_DIAG_H
#define PJ_LAUNCHER_DIAG_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "launcher_logic.h"

#define PJ_DIAG_PREFIX "PJ-DIAG"
#define PJ_DIAG_MAX 200u
#define PJ_DIAG_STATS_MS 1000u

static const char *const pj_diag_phase_names[]={
    "prime","open-press","open-wait","menu","media","other","close-press","close-wait","done"};
static const char *const pj_diag_done_names[PJ_DONE_COUNT]={
    "none","no-fpga-reply","menu-button","closed","held-at-power-on","open-buttons-held","open-no-response"};
static const char *const pj_diag_close_names[PJ_CLOSE_COUNT]={
    "none","chose-games","b-button","other-stock","service-end","remote-menu"};
/* Same order as ESP-IDF v5.3 esp_reset_reason_t (esp_system.h). */
static const char *const pj_diag_reset_names[]={
    "unknown","poweron","ext","sw","panic","int-wdt","task-wdt","wdt","deepsleep","brownout","sdio","usb",
    "jtag","efuse","pwr-glitch","cpu-lockup"};
#define PJ_DIAG_NAME(table,index) \
    ((unsigned)(index)<sizeof(table)/sizeof((table)[0])?(table)[(unsigned)(index)]:"other")

/* Counters the shell keeps; all wrap harmlessly. */
typedef struct {
    uint32_t uart_bytes,messages,polls,pokes,frames,lines,unsafe;
    uint16_t last_buttons;
} pj_diag_counts;

/* What the shell remembers to notice changes. */
typedef struct {uint8_t phase,close_reason,done_reported;} pj_diag_watch;

/* Turns any text into one token: printable, no spaces or '=' , bounded. */
static inline void pj_diag_token(char *out,size_t size,const char *text) {
    size_t n=0;
    if(!size)return;
    for(;text&&*text&&n+1<size;text++) {
        char ch=*text;
        if(ch<=' '||ch>'~'||ch=='=')ch='-';
        out[n++]=ch;
    }
    out[n]=0;
    if(!n&&size>1){out[0]='-';out[1]=0;}
}

/* 1 if a whole line obeys the rules above. */
static inline int pj_diag_safe(const char *line) {
    size_t n=strlen(line),hex=0;
    if(n==0||n>=PJ_DIAG_MAX||strncmp(line,PJ_DIAG_PREFIX " t=",sizeof(PJ_DIAG_PREFIX " t=")-1))return 0;
    if(strstr(line,"PJ-LAUNCHER")||strstr(line,"rst:"))return 0;
    for(size_t i=0;i<n;i++) {
        char ch=line[i];
        if(ch<' '||ch>'~')return 0;
        /* No long hex runs: nothing here is an identifier or a dump. */
        int is_hex=(ch>='0'&&ch<='9')||(ch>='a'&&ch<='f')||(ch>='A'&&ch<='F');
        hex=is_hex?hex+1:0;
        if(hex>=12)return 0;
        if(ch==':')return 0; /* no MAC-style or address-style text */
    }
    return 1;
}

/* Builds the full line from an event body; returns its length, 0 if it was unsafe
 * (then `out` holds a short replacement line that is safe). */
static inline size_t pj_diag_line(char *out,size_t size,uint32_t now,const char *body) {
    if(size<64){if(size)out[0]=0;return 0;}
    int n=snprintf(out,size,PJ_DIAG_PREFIX " t=%lu %s",(unsigned long)now,body);
    if(n>0&&(size_t)n<size&&pj_diag_safe(out))return (size_t)n;
    snprintf(out,size,PJ_DIAG_PREFIX " t=%lu unsafe-line-dropped",(unsigned long)now);
    return 0;
}

/* ---- event bodies ------------------------------------------------------------- */
static inline void pj_diag_boot(char *out,size_t size,const char *version,unsigned reset_reason,int stock_suspect,
                                uint32_t free_heap) {
    char ver[24];pj_diag_token(ver,sizeof(ver),version);
    snprintf(out,size,"boot version=%s reset=%s stock-suspect=%d free-heap=%lu",ver,
             PJ_DIAG_NAME(pj_diag_reset_names,reset_reason),stock_suspect?1:0,(unsigned long)free_heap);
}
static inline void pj_diag_phase(char *out,size_t size,unsigned from,const pj_launcher *l) {
    snprintf(out,size,"phase from=%s to=%s tries=%u fresh=%u menu-open=%u held=0x%04x seen-fpga=%u",
             PJ_DIAG_NAME(pj_diag_phase_names,from),PJ_DIAG_NAME(pj_diag_phase_names,l->phase),l->tries,
             (unsigned)l->fresh,(unsigned)l->menu_open,(unsigned)l->held,(unsigned)l->seen_message);
}
static inline void pj_diag_close(char *out,size_t size,const pj_launcher *l) {
    snprintf(out,size,"close reason=%s selected=%u",PJ_DIAG_NAME(pj_diag_close_names,l->close_reason),
             (unsigned)l->selected);
}
static inline void pj_diag_done(char *out,size_t size,const pj_launcher *l) {
    snprintf(out,size,"done reason=%s close=%s tries=%u menu-open=%u close-hint=%u",
             PJ_DIAG_NAME(pj_diag_done_names,l->done_reason),PJ_DIAG_NAME(pj_diag_close_names,l->close_reason),
             l->tries,(unsigned)l->menu_open,(unsigned)l->close_hint);
}
/* Wi-Fi check: the result, how many networks were heard (a count only, never names),
 * the step that failed and its esp_err name. */
static inline void pj_diag_wifi(char *out,size_t size,int ok,unsigned networks,const char *step,int err,
                                const char *err_name) {
    char s[24],e[40];pj_diag_token(s,sizeof(s),step);pj_diag_token(e,sizeof(e),err_name);
    snprintf(out,size,"wifi result=%s networks=%u step=%s err=%d err-name=%s",ok?"ok":"failed",networks,s,err,e);
}
static inline void pj_diag_stats(char *out,size_t size,const pj_diag_counts *c,const pj_launcher *l,
                                 uint32_t free_heap,uint32_t min_heap) {
    /* Short keys: the worst case (every figure at its 32-bit maximum) must fit. */
    snprintf(out,size,"stats phase=%s uart=%lu msgs=%lu polls=%lu pokes=%lu frames=%lu btn=0x%04x "
             "heap=%lu heap-min=%lu unsafe=%lu",PJ_DIAG_NAME(pj_diag_phase_names,l->phase),
             (unsigned long)c->uart_bytes,(unsigned long)c->messages,(unsigned long)c->polls,(unsigned long)c->pokes,
             (unsigned long)c->frames,(unsigned)c->last_buttons,(unsigned long)free_heap,(unsigned long)min_heap,
             (unsigned long)c->unsafe);
}
/* Last line before PJ-LAUNCHER-TO-STOCK (or STOCK-INVALID). */
static inline void pj_diag_handover(char *out,size_t size,const pj_launcher *l,int stock_suspect,uint32_t uptime_ms) {
    snprintf(out,size,"handover to=%s done=%s uptime-ms=%lu",stock_suspect?"stock-invalid-screen":"stock",
             PJ_DIAG_NAME(pj_diag_done_names,l->done_reason),(unsigned long)uptime_ms);
}
#endif
