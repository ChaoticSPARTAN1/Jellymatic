/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * CHR1, the Remote Pilot <-> Chromatic protocol, device side. Portable C: the same code
 * runs on the ESP32 launcher and in the PC test against Remote Pilot's own client.
 * Specification: RemotePilot repo, presets/chromatic/PROTOCOL.md (reference:
 * chromatic/protocol.py). Integrity and authentication only, no confidentiality: anyone
 * on the path can read payloads, so nothing secret may ever be one. The launcher keeps to
 * that by refusing the settings partition and provisioning sectors to svc-read/svc-hash
 * and by allowing svc-* on USB only (shared/service.h, shared/remote.h).
 * No flash writes, no settings: every action is read-only or a button press.
 */
#ifndef PJ_CHR1_H
#define PJ_CHR1_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "log_ring.h" /* pj_log: the ring the CHR1 "log" action serves */
#include "sha256.h"  /* SHA-256, HMAC, hex helpers */

/* ---- protocol ---------------------------------------------------------------- */
#define PJ_CHR1_PORT 38771u
#define PJ_CHR1_MAX_LINE 256u
#define PJ_CHR1_IDLE_MS 15000u
#define PJ_CHR1_HOLD_LEASE_MS 600u
#define PJ_CHR1_SCREEN_BYTES (160u*144u*2u)
enum {PJ_CHR1_WANT_AUTH,PJ_CHR1_READY,PJ_CHR1_CLOSED};
/* Buttons in CHR1 order; MENU only ever a single input. Bits match launcher_logic.h. */
static const char *const pj_chr1_names[9]={"UP","DOWN","LEFT","RIGHT","A","B","START","SELECT","MENU"};
static const uint16_t pj_chr1_bits[9]={1u<<4,1u<<7,1u<<6,1u<<5,1u<<3,1u<<2,1u<<0,1u<<1,1u<<8};

typedef struct {
    uint8_t key[32],session[32];
    char device_nonce[33],pc_nonce[33];
    uint32_t expected_seq,last_ms;
    int state;
} pj_chr1;

/* Parses "A", "A+B" (1..4 distinct names) or "NONE" when allowed. -1 if invalid. */
static inline int pj_chr1_buttons(const char *text,int allow_none,uint16_t *bits) {
    *bits=0;
    if(allow_none&&!strcmp(text,"NONE"))return 0;
    unsigned count=0;int menu=0;
    const char *p=text;
    while(*p) {
        const char *end=strchr(p,'+');size_t len=end?(size_t)(end-p):strlen(p);
        int found=-1;
        for(int i=0;i<9;i++)if(strlen(pj_chr1_names[i])==len&&!strncmp(p,pj_chr1_names[i],len))found=i;
        if(found<0||(*bits&pj_chr1_bits[found])||++count>4)return -1;
        if(found==8)menu=1;
        *bits|=pj_chr1_bits[found];
        if(!end)break;
        p=end+1;if(!*p)return -1;
    }
    if(!count||(menu&&(count>1||allow_none)))return -1;
    return (int)count;
}
static inline void pj_chr1_tag(const uint8_t key[32],const char *text,char out[65]) {
    uint8_t mac[32];pj_hmac(key,text,strlen(text),NULL,0,mac);pj_hex(mac,32,out);
}
/* Starts a connection: device_nonce_bytes must be 16 fresh random bytes. Writes the
 * HELLO line (with "\n") into out. */
static inline void pj_chr1_start(pj_chr1 *c,const uint8_t key[32],const uint8_t nonce[16],const char *version,
                                 uint32_t now,char *out,size_t out_size) {
    memset(c,0,sizeof(*c));memcpy(c->key,key,32);pj_hex(nonce,16,c->device_nonce);
    c->state=PJ_CHR1_WANT_AUTH;c->expected_seq=1;c->last_ms=now;
    snprintf(out,out_size,"CHR1 HELLO %s %s\n",c->device_nonce,version);
}
static inline int pj_chr1_is_hex(const char *s,size_t n) {
    if(strlen(s)!=n)return 0;
    for(size_t i=0;i<n;i++)if(!((s[i]>='0'&&s[i]<='9')||(s[i]>='a'&&s[i]<='f')))return 0;
    return 1;
}
/* Handles the AUTH line. Writes "CHR1 OK ..." or "CHR1 DENY" into out. 1 ok, 0 denied. */
static inline int pj_chr1_auth(pj_chr1 *c,const char *line,const char *caps,uint32_t now,char *out,size_t out_size) {
    char magic[8],word[8],nonce[64],tag[96],expect[65],text[160];
    if(c->state!=PJ_CHR1_WANT_AUTH||strlen(line)>=PJ_CHR1_MAX_LINE||
       sscanf(line,"%7s %7s %63s %95s",magic,word,nonce,tag)!=4||strcmp(magic,"CHR1")||strcmp(word,"AUTH")||
       !pj_chr1_is_hex(nonce,32)||!pj_chr1_is_hex(tag,64)||!strcmp(nonce,c->device_nonce))goto deny;
    snprintf(text,sizeof(text),"CHR1 pc-auth %s %s",c->device_nonce,nonce);
    pj_chr1_tag(c->key,text,expect);
    if(!pj_same(expect,tag,64))goto deny;
    memcpy(c->pc_nonce,nonce,33);
    snprintf(text,sizeof(text),"CHR1 session %s %s",c->device_nonce,c->pc_nonce);
    pj_hmac(c->key,text,strlen(text),NULL,0,c->session);
    snprintf(text,sizeof(text),"CHR1 device-auth %s %s",c->pc_nonce,c->device_nonce);
    pj_chr1_tag(c->key,text,expect);
    snprintf(out,out_size,"CHR1 OK %s %s\n",expect,caps);
    c->state=PJ_CHR1_READY;c->last_ms=now;
    return 1;
deny:
    snprintf(out,out_size,"CHR1 DENY\n");c->state=PJ_CHR1_CLOSED;
    return 0;
}
/* Parses and authenticates a REQ line. 1 with action/arg filled; 0 means close. */
static inline int pj_chr1_request(pj_chr1 *c,const char *line,uint32_t now,char action[16],char arg[48]) {
    char req[8],seq_text[16],mac[64],head[128],expect[65];
    if(c->state!=PJ_CHR1_READY||strlen(line)>=PJ_CHR1_MAX_LINE)return 0;
    if(sscanf(line,"%7s %15s %15s %47s %63s",req,seq_text,action,arg,mac)!=5||strcmp(req,"REQ"))return 0;
    if(seq_text[0]<'1'||seq_text[0]>'9'||strlen(seq_text)>10||!pj_chr1_is_hex(mac,32))return 0;
    unsigned long long seq=0;
    for(const char *p=seq_text;*p;p++){if(*p<'0'||*p>'9')return 0;seq=seq*10u+(unsigned)(*p-'0');}
    snprintf(head,sizeof(head),"REQ %s %s %s",seq_text,action,arg);
    pj_chr1_tag(c->session,head,expect);
    if(!pj_same(expect,mac,32)||seq!=c->expected_seq){c->state=PJ_CHR1_CLOSED;return 0;}
    c->expected_seq++;c->last_ms=now;
    return 1;
}
/* Builds the RES line for a body; the caller sends the line, then the body. */
static inline void pj_chr1_response(const pj_chr1 *c,const char *code,const uint8_t *body,size_t size,
                                    char *out,size_t out_size) {
    char head[96],hex[65];uint8_t mac[32];
    snprintf(head,sizeof(head),"RES %lu %s %lu\n",(unsigned long)(c->expected_seq-1),code,(unsigned long)size);
    pj_hmac(c->session,head,strlen(head),body,size,mac);
    pj_hex(mac,16,hex);
    snprintf(out,out_size,"RES %lu %s %lu %s\n",(unsigned long)(c->expected_seq-1),code,(unsigned long)size,hex);
}
static inline int pj_chr1_idle(const pj_chr1 *c,uint32_t now) {
    return (uint32_t)(now-c->last_ms)>=PJ_CHR1_IDLE_MS;
}

#endif
