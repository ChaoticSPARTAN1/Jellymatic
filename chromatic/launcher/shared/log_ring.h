/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * RAM log ring shared by the launcher diagnostics and the CHR1 "log" action.
 * Portable C, no flash, no identifiers: callers pass plain text only.
 */
#ifndef PJ_LOG_RING_H
#define PJ_LOG_RING_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define PJ_LOG_BYTES 16384u
typedef struct {char data[PJ_LOG_BYTES];size_t head,used;} pj_log;
static inline void pj_log_add(pj_log *l,uint32_t now,const char *tag,const char *text) {
    char line[200];
    int n=snprintf(line,sizeof(line),"%lu %s %s\n",(unsigned long)now,tag,text);
    if(n<0)return;
    if((size_t)n>=sizeof(line))n=(int)sizeof(line)-1,line[n-1]='\n';
    for(int i=0;i<n;i++) {
        char ch=line[i];if((ch<0x20&&ch!='\n')||ch==0x7f)ch='?';
        l->data[(l->head+l->used)%PJ_LOG_BYTES]=ch;
        if(l->used<PJ_LOG_BYTES)l->used++;else l->head=(l->head+1)%PJ_LOG_BYTES;
    }
}
/* Copies the newest `want` bytes (at most) into out; returns the count. */
static inline size_t pj_log_tail(const pj_log *l,size_t want,char *out) {
    size_t n=want<l->used?want:l->used,start=l->head+l->used-n;
    for(size_t i=0;i<n;i++)out[i]=l->data[(start+i)%PJ_LOG_BYTES];
    return n;
}
#endif
