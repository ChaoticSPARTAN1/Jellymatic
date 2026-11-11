/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Remote control: one CHR1 handler (shared/chr1.h framing, handshake and actions, as
 * Remote Pilot's presets/chromatic/PROTOCOL.md specifies) behind two transports:
 *   USB  - the console UART (USB serial). The PC starts with "CHR1 OPEN".
 *   LAN  - TCP 38771 over Wi-Fi. A connection starts the handshake.
 * Spec: docs/Design-notes.md. Portable C: the launcher and the host tests
 * (check_remote.c, serial and TCP loopback) run this same code.
 *
 * Rules:
 *  - Nothing unauthenticated acts. A session needs the provisioned chr1_key, or a key
 *    paired over USB this power-on, confirmed by the owner pressing A on the console.
 *    Pairing is USB only; virtual buttons can never approve it.
 *  - One session at a time over both transports; a second is refused.
 *  - No flash writes, no settings. Service actions (svc-*) are USB only, read flash
 *    only, and never serve the settings partition or provisioning sectors (service.h).
 *    CHR1 has integrity, not confidentiality: nothing secret is ever a payload.
 *  - After an opening failure (no cartridge, silent FPGA) an authenticated session keeps
 *    the launcher from restarting into stock for at most 10 minutes per power-on, and any
 *    physical button press ends that at once. A pairing request never holds anything.
 *    Holds expire 600 ms after the last refresh; physical buttons always win.
 *  - Both links share one body buffer, so a session cannot start while the other link
 *    still has output owed; a closed link drops any body it still owed.
 *  - Bounded work per main-loop pass; nothing blocks on the PC.
 */
#ifndef PJ_REMOTE_H
#define PJ_REMOTE_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "chr1.h"
#include "launcher_logic.h"
#include "service.h"

#define PJ_RMT_LINE 256u                       /* with its "\n", as CHR1 framing allows */
#define PJ_RMT_BODY_MAX (20u+PJ_CHR1_SCREEN_BYTES) /* a raw CHV1 frame: the largest body */
#define PJ_RMT_LEASE_MS 600000u                /* no-cartridge hold, per power-on */
#define PJ_RMT_PAIR_MS 60000u
#define PJ_RMT_AUTH_GAP_MS 1000u               /* at most one failed handshake a second */
#define PJ_RMT_CAPS "status,log,screen,video,input,hold,perf,console"
enum {PJ_LINK_USB,PJ_LINK_LAN};
enum {PJ_LS_IDLE,PJ_LS_AUTH,PJ_LS_READY};
enum {PJ_PEND_NONE,PJ_PEND_HASH,PJ_PEND_FREEZE,PJ_PEND_VIDEO};
/* While a video plays (the shell sets video_slow) a `video` frame is answered at most this often; an
 * earlier request waits (pending) and is answered when the slot comes. A raw frame is 46 KB: at 115200
 * baud it alone takes 4 s of USB, and over the LAN it competes with the stream for Wi-Fi (the owner saw
 * lag and "can't reach server" with live video on during playback). `screen` is never throttled. */
#define PJ_RMT_VIDEO_SLOW_MS 2000u /* under Remote Pilot's 3 s client timeout */
static const char *const pj_rmt_link_names[2]={"usb","lan"};

typedef struct {
    uint8_t kind,state,overflow,bad,pending,close_after,want_close;
    pj_chr1 c;
    char rx[PJ_RMT_LINE];unsigned used;
    char head[PJ_RMT_LINE];size_t head_len,head_sent;
    const uint8_t *body;size_t body_len,body_sent;
    uint32_t opened_ms;
    size_t sent_mark; /* 1.3.3: head_sent+body_sent at the last pj_rmt_poll, so only real progress keeps a link alive */
} pj_link;
/* Facts only the shell knows, read for status, perf and console. No identifiers. */
typedef struct {
    uint32_t heap_free,heap_min,loop_ms,work_ms,frames,stack_free;
    const char *wifi;     /* off, joining, connected, failed */
    const char *reset;    /* reset reason name */
    /* 1.3.5: the FPGA's Version message (launcher_logic.h: major 5..0, minor 11..6, debug 12), if one was heard.
     * Reported in `status` ("fpga", "fpgaDebug") and `console fpga`, so Chromatic Manager can learn which image the
     * console chip runs without restarting anything. */
    uint16_t fpga_version;uint8_t fpga_seen;
} pj_rmt_facts;
/* "18.41" / "none" and the debug bit, for status and console. */
static inline void pj_rmt_fpga_words(const pj_rmt_facts *f,char *out,size_t size,int *debug) {
    *debug=f->fpga_seen&&((f->fpga_version>>12)&1u);
    if(f->fpga_seen)snprintf(out,size,"%u.%u",(unsigned)(f->fpga_version&63u),(unsigned)((f->fpga_version>>6)&63u));
    else snprintf(out,size,"none");
}
typedef uint32_t (*pj_rmt_random_fn)(void *ctx);
typedef void (*pj_rmt_facts_fn)(void *ctx,pj_rmt_facts *facts);

typedef struct {
    const char *version;pj_launcher *l;pj_svc *svc;const uint16_t *fb;pj_log *log;
    pj_rmt_random_fn random32;pj_rmt_facts_fn facts;void *ctx;
    uint8_t key[32],key_kind; /* 0 none, 1 provisioned, 2 paired over USB (RAM only) */
    pj_link usb,lan;pj_link *owner;
    uint32_t fail_ms;uint8_t failed;
    /* Pairing (USB only): shown on screen, approved with a physical A. */
    uint8_t pairing,pair_answer;uint32_t pair_ms;char pair_code[8],pair_line[100];uint8_t pair_key[32];
    /* Virtual buttons: the held set and when it was last refreshed. */
    uint16_t hold_bits,physical;uint32_t hold_ms;
    /* Lease: how long a session may hold the restart after an opening failure. */
    uint32_t lease_used,lease_mark;uint8_t lease_on,lease_spent,owner_left;
    uint32_t video_seq,requests,refusals,sessions;
    uint8_t video_slow;uint32_t video_ms,video_waited; /* shell: a video plays; last frame; frames held back */
    uint32_t now; /* 1.3.2: the time of the last pj_rmt_rx/pj_rmt_poll call, for log stamps where no time is passed */
    uint8_t body[PJ_RMT_BODY_MAX];
} pj_remote;

static inline void pj_rmt_init(pj_remote *r,const char *version,const uint8_t *provisioned_key,pj_launcher *l,
                               pj_svc *svc,const uint16_t *fb,pj_log *log,pj_rmt_random_fn random32,
                               pj_rmt_facts_fn facts,void *ctx) {
    memset(r,0,sizeof(*r));
    r->version=version;r->l=l;r->svc=svc;r->fb=fb;r->log=log;r->random32=random32;r->facts=facts;r->ctx=ctx;
    if(provisioned_key){memcpy(r->key,provisioned_key,32);r->key_kind=1;}
    r->usb.kind=PJ_LINK_USB;r->lan.kind=PJ_LINK_LAN;
}
static inline void pj_rmt_random(pj_remote *r,uint8_t *out,size_t size) {
    for(size_t i=0;i<size;i+=4){uint32_t v=r->random32(r->ctx);for(size_t j=0;j<4&&i+j<size;j++)out[i+j]=(uint8_t)(v>>(8*j));}
}
static inline int pj_rmt_busy_output(const pj_link *k) {
    return k->head_sent<k->head_len||k->body_sent<k->body_len;
}
static inline void pj_rmt_line(pj_link *k,const char *text) {
    size_t n=strlen(text);if(n>PJ_RMT_LINE-2)n=PJ_RMT_LINE-2;
    memcpy(k->head,text,n);k->head[n++]='\n';k->head[n]=0;
    k->head_len=n;k->head_sent=0;k->body=NULL;k->body_len=k->body_sent=0;
}
static inline void pj_rmt_respond(pj_remote *r,pj_link *k,const char *code,const void *body,size_t size) {
    pj_chr1_response(&k->c,code,body,size,k->head,sizeof(k->head));
    k->head_len=strlen(k->head);k->head_sent=0;k->body=body;k->body_len=size;k->body_sent=0;
    if(strcmp(code,"ok"))r->refusals++;
}
static inline void pj_rmt_refuse(pj_remote *r,pj_link *k,const char *code,const char *reason) {
    size_t n=strlen(reason);if(n>160)n=160;
    memcpy(r->body,reason,n);pj_rmt_respond(r,k,code,r->body,n);
}
/* Ends a link's session: remote buttons released, a service session ended (to stock). */
static inline void pj_rmt_drop(pj_remote *r,pj_link *k) {
    if(r->owner==k) {
        r->owner=NULL;r->hold_bits=0;
        /* A job whose PC went away ends (to stock); a finishing one completes. */
        if(r->svc->state==PJ_SVC_ACTIVE||r->svc->state==PJ_SVC_FROZEN)pj_svc_finish(r->svc,PJ_SVC_END_LINK_CLOSED);
        /* 1.3.2: stamped with the time it closed (it was the time the session began, which made the gaps in the
         * console's log read as the PC reconnecting late) and how long it lasted. */
        if(r->log){char note[48];snprintf(note,sizeof(note),"session closed (%s, %lu s)",pj_rmt_link_names[k->kind&1],
                                          (unsigned long)((r->now-k->opened_ms)/1000u));pj_log_add(r->log,r->now,"chr1",note);}
    }
    k->state=PJ_LS_IDLE;k->pending=PJ_PEND_NONE;k->used=0;k->overflow=k->bad=0;
}
/* Closes a link. A short line still owed (DENY, BUSY) goes out first; a response body
 * still owed is dropped, since nobody will read it, so the shell never waits on it. Then a
 * LAN link asks the shell to close its socket; USB goes back to waiting for "CHR1 OPEN". */
static inline void pj_rmt_close(pj_remote *r,pj_link *k) {
    pj_rmt_drop(r,k);
    if(k->body_sent<k->body_len){k->head_len=k->head_sent=0;k->body=NULL;k->body_len=k->body_sent=0;}
    if(pj_rmt_busy_output(k))k->close_after=1;else if(k->kind==PJ_LINK_LAN)k->want_close=1;
}
static inline void pj_rmt_start(pj_remote *r,pj_link *k,uint32_t now) {
    uint8_t nonce[16];char out[96];
    pj_rmt_random(r,nonce,16);
    pj_chr1_start(&k->c,r->key,nonce,r->version,now,out,sizeof(out));
    out[strlen(out)-1]=0;pj_rmt_line(k,out);
    k->state=PJ_LS_AUTH;k->opened_ms=now;k->close_after=0;k->want_close=0;
}
/* A LAN client connected (the shell has checked it is a private address). */
static inline void pj_rmt_lan_open(pj_remote *r,uint32_t now) {
    pj_link *k=&r->lan;r->now=now;
    pj_rmt_drop(r,k);k->head_len=k->head_sent=0;k->body_len=k->body_sent=0;k->want_close=0;k->close_after=0;
    if(r->owner||pj_rmt_busy_output(&r->usb)||!r->key_kind||(r->failed&&(uint32_t)(now-r->fail_ms)<PJ_RMT_AUTH_GAP_MS)) {
        /* Busy, nothing to authenticate with, or too soon after a failure: closed. */
        if(!r->key_kind)pj_rmt_line(k,"CHR1 DENY");
        k->close_after=pj_rmt_busy_output(k);k->want_close=!k->close_after;return;
    }
    pj_rmt_start(r,k,now);
}
static inline void pj_rmt_lan_closed(pj_remote *r) {
    pj_rmt_drop(r,&r->lan);r->lan.head_len=r->lan.head_sent=r->lan.body_len=r->lan.body_sent=0;
    r->lan.want_close=0;r->lan.close_after=0;
}

/* ---- actions ------------------------------------------------------------------- */
static inline const char *pj_rmt_phase(const pj_launcher *l) {
    switch(l->phase){case PJ_L_MEDIA:return "media";case PJ_L_OTHER:return "other-firmware";
    case PJ_L_CLOSE_PRESS:case PJ_L_CLOSE_WAIT:case PJ_L_DONE:return "handover";default:return "menu";}
}
static inline const char *pj_rmt_screen(const pj_remote *r) {
    const pj_launcher *l=r->l;
    if(r->pairing)return "pairing";
    if(pj_svc_in_session(r->svc))return "service";
    switch(l->phase){case PJ_L_MENU:return "menu";case PJ_L_MEDIA:return "media";case PJ_L_OTHER:return "other";
    case PJ_L_CLOSE_PRESS:case PJ_L_CLOSE_WAIT:return "closing";case PJ_L_DONE:return "hidden";default:return "opening";}
}
static inline void pj_rmt_names(uint16_t bits,char *out,size_t size) {
    size_t n=0;out[0]=0;
    for(int i=0;i<9;i++)if(bits&pj_chr1_bits[i])n+=(size_t)snprintf(out+n,size-n,"%s%s",n?"+":"",pj_chr1_names[i]);
    if(!n)snprintf(out,size,"NONE");
}
static inline size_t pj_rmt_status(pj_remote *r,const pj_link *k,uint32_t now) {
    static const char *const items[PJ_ITEM_COUNT]={"games","jellyfin","other"};
    const pj_launcher *l=r->l;char held[48],countdown[12],fpga[12];int fpga_debug=0;
    pj_rmt_facts f;memset(&f,0,sizeof(f));if(r->facts)r->facts(r->ctx,&f);
    pj_rmt_fpga_words(&f,fpga,sizeof(fpga),&fpga_debug);
    pj_rmt_names(r->hold_bits,held,sizeof(held));
    strcpy(countdown,"null"); /* the menu has no countdown: it waits for the owner */
    int n=snprintf((char *)r->body,1024,"{\"phase\":\"%s\",\"target\":\"launcher\",\"version\":\"%s\",\"screen\":\"%s\","
        "\"selected\":\"%s\",\"countdownS\":%s,\"pcSession\":true,\"held\":\"%s\",\"uptimeMs\":%lu,\"transport\":\"%s\","
        "\"screenVisible\":%s,\"leaseLeftMs\":%lu,\"service\":\"%s\",\"servicePct\":%u,\"key\":\"%s\","
        "\"fpga\":\"%s\",\"fpgaDebug\":%s}",
        pj_rmt_phase(l),r->version,pj_rmt_screen(r),items[l->selected%PJ_ITEM_COUNT],countdown,held,(unsigned long)now,
        pj_rmt_link_names[k->kind],pj_svc_screen_of(l)==PJ_SVC_SCREEN_SHOWN?"true":"false",
        (unsigned long)(r->lease_used>=PJ_RMT_LEASE_MS?0:PJ_RMT_LEASE_MS-r->lease_used),
        pj_svc_state_names[r->svc->state],r->svc->pct,r->key_kind==1?"provisioned":"paired",fpga,fpga_debug?"true":"false");
    return n>0&&n<1024?(size_t)n:0;
}
/* One CHV1 keyframe: RLE (kind 1) when smaller, else raw (kind 0). Never a delta. */
static inline size_t pj_rmt_video(pj_remote *r) {
    uint8_t *p=r->body+20;size_t used=0;int rle=1;
    for(int y=0;y<PJ_OSD_H&&rle;y++)for(int x=0;x<PJ_OSD_W;) {
        uint16_t c=r->fb[y*PJ_OSD_W+x];int run=1;
        while(x+run<PJ_OSD_W&&run<255&&r->fb[y*PJ_OSD_W+x+run]==c)run++;
        if(used+3>PJ_CHR1_SCREEN_BYTES){rle=0;break;}
        p[used++]=(uint8_t)run;p[used++]=(uint8_t)c;p[used++]=(uint8_t)(c>>8);x+=run;
    }
    if(!rle){for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++){p[i*2]=(uint8_t)r->fb[i];p[i*2+1]=(uint8_t)(r->fb[i]>>8);}used=PJ_CHR1_SCREEN_BYTES;}
    uint32_t seq=++r->video_seq;
    memcpy(r->body,"CHV1",4);
    for(int i=0;i<4;i++){r->body[4+i]=(uint8_t)(seq>>(8*i));r->body[8+i]=0;r->body[16+i]=(uint8_t)(used>>(8*i));}
    r->body[12]=(uint8_t)(rle?1:0);r->body[13]=0;r->body[14]=PJ_OSD_H;r->body[15]=0;
    return 20+used;
}
/* "<a>:<b>" with two hex numbers (svc-read/svc-hash) or "<word>:<hex>" (svc-begin). */
static inline int pj_rmt_pair(const char *arg,char *first,size_t first_size,uint32_t *second) {
    const char *colon=strchr(arg,':');
    if(!colon||(size_t)(colon-arg)>=first_size||colon==arg)return 0;
    memcpy(first,arg,(size_t)(colon-arg));first[colon-arg]=0;
    return pj_svc_parse_hex8(colon+1,second);
}
/* Presses a chord as if from the console: same logic as a physical press. Returns 1 if
 * it began the hand-over to stock. */
static inline int pj_rmt_press(pj_remote *r,uint16_t bits,uint32_t now) {
    pj_launcher *l=r->l;
    if(bits&PJ_POKE_MENU) {
        if(l->phase==PJ_L_MENU||l->phase==PJ_L_MEDIA||l->phase==PJ_L_OTHER){pj_launch_begin_close(l,now,PJ_CLOSE_REMOTE_MENU);return 1;}
        return 0;
    }
    pj_launch_press(l,bits,now);
    return l->phase==PJ_L_CLOSE_PRESS;
}
static inline void pj_rmt_console(pj_remote *r,pj_link *k,const char *arg,const pj_rmt_facts *f,uint32_t now) {
    char *t=(char *)r->body;int n=-1;
    if(!strcmp(arg,"help"))n=snprintf(t,1024,"help version uptime heap wifi phase reset-reason fpga");
    else if(!strcmp(arg,"version"))n=snprintf(t,1024,"launcher %s",r->version);
    else if(!strcmp(arg,"fpga")){char v[12];int dbg;pj_rmt_fpga_words(f,v,sizeof(v),&dbg);n=snprintf(t,1024,"fpga version=%s debug=%d",v,dbg);} /* 1.3.5 */
    else if(!strcmp(arg,"uptime"))n=snprintf(t,1024,"%lu ms",(unsigned long)now);
    else if(!strcmp(arg,"heap"))n=snprintf(t,1024,"free %lu, lowest %lu",(unsigned long)f->heap_free,(unsigned long)f->heap_min);
    else if(!strcmp(arg,"wifi"))n=snprintf(t,1024,"%s",f->wifi?f->wifi:"off");
    else if(!strcmp(arg,"phase"))n=snprintf(t,1024,"%s (%s)",pj_rmt_phase(r->l),pj_rmt_screen(r));
    else if(!strcmp(arg,"reset-reason"))n=snprintf(t,1024,"%s",f->reset?f->reset:"unknown");
    if(n<0){pj_rmt_refuse(r,k,"refused","not on the read-only list");return;}
    pj_rmt_respond(r,k,"ok",t,(size_t)n);
}
static inline void pj_rmt_action(pj_remote *r,pj_link *k,const char *action,const char *arg,uint32_t now) {
    pj_rmt_facts f;memset(&f,0,sizeof(f));if(r->facts)r->facts(r->ctx,&f);
    pj_svc *s=r->svc;char word[24];uint32_t a=0,b=0;const char *why=NULL;
    r->requests++;
    if(!strcmp(action,"status")) {
        size_t n=pj_rmt_status(r,k,now);pj_rmt_respond(r,k,"ok",r->body,n);
    } else if(!strcmp(action,"log")) {
        long want=strtol(arg,NULL,10);
        if(want<1||want>16384){pj_rmt_refuse(r,k,"refused","log takes 1..16384");return;}
        size_t n=r->log?pj_log_tail(r->log,(size_t)want,(char *)r->body):0;pj_rmt_respond(r,k,"ok",r->body,n);
    } else if(!strcmp(action,"screen")) {
        for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++){r->body[i*2]=(uint8_t)r->fb[i];r->body[i*2+1]=(uint8_t)(r->fb[i]>>8);}
        pj_rmt_respond(r,k,"ok",r->body,PJ_CHR1_SCREEN_BYTES);
    } else if(!strcmp(action,"video")) {
        if(strcmp(arg,"key")&&strcmp(arg,"delta")){pj_rmt_refuse(r,k,"refused","video takes key or delta");return;}
        if(r->video_slow&&(uint32_t)(now-r->video_ms)<PJ_RMT_VIDEO_SLOW_MS){k->pending=PJ_PEND_VIDEO;r->video_waited++;return;} /* answered by pj_rmt_poll */
        size_t n=pj_rmt_video(r);r->video_ms=now;pj_rmt_respond(r,k,"ok",r->body,n);
    } else if(!strcmp(action,"input")||!strcmp(action,"hold")) {
        int input=action[0]=='i';uint16_t bits;char names[40];int ms=0;
        if(input) {
            const char *colon=strrchr(arg,':');size_t len=colon?(size_t)(colon-arg):0;
            if(!colon||len>=sizeof(names)){pj_rmt_refuse(r,k,"refused","input takes BUTTONS:ms");return;}
            memcpy(names,arg,len);names[len]=0;ms=atoi(colon+1);
        } else snprintf(names,sizeof(names),"%s",arg);
        if(pj_chr1_buttons(names,!input,&bits)<0||(input&&(ms<20||ms>250))) {
            pj_rmt_refuse(r,k,"refused","bad buttons");return;
        }
        if(r->l->service&&bits){pj_rmt_refuse(r,k,"busy","the console is showing a service or pairing screen");return;}
        uint16_t fresh=input?bits:(uint16_t)(bits&~r->hold_bits);
        if(!input){r->hold_bits=bits;r->hold_ms=now;}
        if(r->log){char note[64];snprintf(note,sizeof(note),"%s %s",action,names);pj_log_add(r->log,now,"remote",note);}
        if(fresh&&pj_rmt_press(r,fresh,now)) {
            r->hold_bits=0;pj_rmt_respond(r,k,"ok","handover",8);k->close_after=1;return;
        }
        if(input)pj_rmt_respond(r,k,"ok","queued",6);
        else if(bits)pj_rmt_respond(r,k,"ok","held",4);else pj_rmt_respond(r,k,"ok","released",8);
    } else if(!strcmp(action,"perf")) {
        int n=snprintf((char *)r->body,512,"{\"fps\":%lu,\"frameMs\":%lu,\"workMs\":%lu,\"timestampMs\":%lu,\"heapFreeKb\":%lu,"
                       "\"stackFreeB\":%lu}",(unsigned long)(f.loop_ms?1000u/(f.loop_ms?f.loop_ms:1u):0u),(unsigned long)f.loop_ms,
                       (unsigned long)f.work_ms,(unsigned long)now,(unsigned long)(f.heap_free/1024u),(unsigned long)f.stack_free);
        pj_rmt_respond(r,k,"ok",r->body,n>0?(size_t)n:0);
    } else if(!strcmp(action,"console"))pj_rmt_console(r,k,arg,&f,now);
    else if(!strcmp(action,"ping"))pj_rmt_respond(r,k,"ok","pong",4);
    else if(!strcmp(action,"bye")){pj_rmt_respond(r,k,"ok","bye",3);k->close_after=1;}
    /* Chromatic Manager's service actions: USB only (the cable is physical access), read
     * only, shown on the console. */
    else if(!strncmp(action,"svc-",4)&&k->kind!=PJ_LINK_USB)pj_rmt_refuse(r,k,"refused","service actions are USB only");
    else if(!strcmp(action,"svc-begin")) {
        if(!pj_rmt_pair(arg,word,sizeof(word),&a)){pj_rmt_refuse(r,k,"refused","svc-begin takes op:total");return;}
        if((why=pj_svc_begin(s,word,a,now))){pj_rmt_refuse(r,k,"refused",why);return;}
        r->l->service=1;r->hold_bits=0; /* the service screen takes the buttons at once */
        size_t n=(size_t)snprintf((char *)r->body,64,"screen=%s",pj_svc_screen_names[s->screen%3u]);
        pj_rmt_respond(r,k,"ok",r->body,n);
    } else if(!strcmp(action,"svc-id")) {
        uint8_t nonce[8],digest[32];char hex[65];
        if(!pj_chr1_is_hex(arg,16)){pj_rmt_refuse(r,k,"refused","svc-id takes 16 hex");return;}
        for(int i=0;i<8;i++){char two[3]={arg[i*2],arg[i*2+1],0};nonce[i]=(uint8_t)strtoul(two,NULL,16);}
        pj_svc_id_digest(nonce,s->mac,s->flash_id,digest);pj_hex(digest,32,hex);
        size_t n=(size_t)snprintf((char *)r->body,160,"digest=%s chip-rev=%u flash-id=%06lx",hex,s->chip_rev,
                                  (unsigned long)(s->flash_id&0xffffffu));
        pj_rmt_respond(r,k,"ok",r->body,n);
    } else if(!strcmp(action,"svc-read")||!strcmp(action,"svc-hash")) {
        if(!pj_rmt_pair(arg,word,sizeof(word),&b)||!pj_svc_parse_hex8(word,&a)){pj_rmt_refuse(r,k,"refused","takes off:len");return;}
        if(action[4]=='r') {
            if((why=pj_svc_read(s,a,b,r->body))){pj_rmt_refuse(r,k,"refused",why);return;}
            pj_rmt_respond(r,k,"ok",r->body,b);
        } else {
            if((why=pj_svc_hash_start(s,a,b))){pj_rmt_refuse(r,k,"refused",why);return;}
            k->pending=PJ_PEND_HASH; /* answered by pj_rmt_poll when done */
        }
    } else if(!strcmp(action,"svc-pct")) {
        char *end;unsigned long pct=strtoul(arg,&end,10);
        if(*end||!*arg||(why=pj_svc_set_pct(s,pct>100?101u:(unsigned)pct))){pj_rmt_refuse(r,k,"refused",*end||!*arg?"pct is 0..100":why);return;}
        pj_rmt_respond(r,k,"ok","ok",2);
    } else if(!strcmp(action,"svc-freeze")) {
        if((why=pj_svc_freeze(s,arg,now))){pj_rmt_refuse(r,k,"refused",why);return;}
        k->pending=PJ_PEND_FREEZE; /* answered once the still frame is out */
    } else if(!strcmp(action,"svc-end")) {
        if(strcmp(arg,"ok")&&strcmp(arg,"fail")){pj_rmt_refuse(r,k,"refused","svc-end takes ok or fail");return;}
        if((why=pj_svc_end(s,!strcmp(arg,"ok"),now))){pj_rmt_refuse(r,k,"refused",why);return;}
        pj_rmt_respond(r,k,"ok","next=stock",10);
    } else pj_rmt_refuse(r,k,"refused","unknown action");
}

/* ---- receiving ------------------------------------------------------------------ */
static inline void pj_rmt_usb_command(pj_remote *r,pj_link *k,const char *line,uint32_t now) {
    if(!strcmp(line,"CHR1 OPEN")) {
        if(pj_rmt_busy_output(k)||r->pairing)return; /* finish what is owed first */
        pj_rmt_drop(r,k);
        if(r->owner||pj_rmt_busy_output(&r->lan))pj_rmt_line(k,"CHR1 BUSY");
        else if(!r->key_kind)pj_rmt_line(k,"CHR1 NOKEY");
        else if(r->failed&&(uint32_t)(now-r->fail_ms)<PJ_RMT_AUTH_GAP_MS)pj_rmt_line(k,"CHR1 WAIT");
        else pj_rmt_start(r,k,now);
        return;
    }
    if(!strncmp(line,"CHR1 PAIR ",10)&&k->state==PJ_LS_IDLE&&!pj_rmt_busy_output(k)) {
        const char *pc_nonce=line+10;
        if(r->key_kind){pj_rmt_line(k,"CHR1 PAIR-REFUSED key-present");return;}
        if(r->pairing||r->owner){pj_rmt_line(k,"CHR1 PAIR-REFUSED busy");return;}
        /* The owner must see the code and answer with a physical press: the menu must be up. */
        if(!(r->l->phase==PJ_L_MENU||r->l->phase==PJ_L_MEDIA||r->l->phase==PJ_L_OTHER)||!r->l->menu_open||
           pj_svc_in_session(r->svc)){pj_rmt_line(k,"CHR1 PAIR-REFUSED no-screen");return;}
        if(!pj_chr1_is_hex(pc_nonce,32)){pj_rmt_line(k,"CHR1 PAIR-REFUSED bad-nonce");return;}
        uint8_t nonce[16],digest[32];char dev[33],text[120];
        pj_rmt_random(r,nonce,16);pj_hex(nonce,16,dev);pj_rmt_random(r,r->pair_key,32);
        snprintf(text,sizeof(text),"CHR1 pair-code %s %s",dev,pc_nonce);
        pj_sha256 h;pj_sha256_init(&h);pj_sha256_update(&h,text,strlen(text));pj_sha256_final(&h,digest);
        uint32_t code=((uint32_t)digest[0]<<24|(uint32_t)digest[1]<<16|(uint32_t)digest[2]<<8|digest[3])%1000000u;
        snprintf(r->pair_code,sizeof(r->pair_code),"%06lu",(unsigned long)code);
        snprintf(text,sizeof(text),"CHR1 PAIRING %s %s",dev,r->pair_code);pj_rmt_line(k,text);
        /* From now on A and B answer the question, never the menu under it. */
        r->pairing=1;r->pair_ms=now;r->pair_answer=0;r->l->service=1;
        if(r->log)pj_log_add(r->log,now,"chr1","pairing asked; press A on the console to allow");
    }
}
/* Feed every byte a link receives. On USB, lines that are not CHR1 are ignored. */
static inline void pj_rmt_rx(pj_remote *r,pj_link *k,uint8_t byte,uint32_t now) {
    r->now=now;
    if(byte=='\r')return;
    if(byte!='\n') {
        if(k->overflow)return;
        if(byte<0x20||byte>0x7e)k->bad=1;
        if(k->used>=PJ_RMT_LINE-1){k->overflow=1;return;}
        k->rx[k->used++]=(char)byte;return;
    }
    int damaged=k->overflow||k->bad;
    k->rx[k->used]=0;k->used=0;k->overflow=k->bad=0;
    if(k->close_after||k->want_close)return;
    if(k->state==PJ_LS_IDLE) {
        if(k->kind==PJ_LINK_USB&&!damaged)pj_rmt_usb_command(r,k,k->rx,now);
        return;
    }
    if(k->kind==PJ_LINK_USB&&!damaged&&!strcmp(k->rx,"CHR1 OPEN")) {
        pj_rmt_drop(r,k);k->head_len=k->head_sent=k->body_len=k->body_sent=0;pj_rmt_usb_command(r,k,k->rx,now);return;
    }
    if(damaged||k->pending||pj_rmt_busy_output(k)) { /* framing broken, or not one at a time */
        if(r->log)pj_log_add(r->log,now,"chr1",damaged?"closed: damaged request":k->pending?"closed: request while one is pending":
                                                  "closed: request while the last answer is still sending");
        pj_rmt_close(r,k);return;
    }
    if(k->state==PJ_LS_AUTH) {
        char out[200];
        if(r->owner&&r->owner!=k){pj_rmt_line(k,"CHR1 BUSY");pj_rmt_close(r,k);return;}
        if(pj_chr1_auth(&k->c,k->rx,PJ_RMT_CAPS,now,out,sizeof(out))) {
            out[strlen(out)-1]=0;pj_rmt_line(k,out);k->state=PJ_LS_READY;r->owner=k;r->sessions++;r->failed=0;
            if(r->log)pj_log_add(r->log,now,"chr1",k->kind==PJ_LINK_USB?"session open (usb)":"session open (lan)");
        } else {
            r->failed=1;r->fail_ms=now;pj_rmt_line(k,"CHR1 DENY");pj_rmt_close(r,k);
            if(r->log)pj_log_add(r->log,now,"chr1","handshake refused");
        }
        return;
    }
    char action[16],arg[48];
    if(!pj_chr1_request(&k->c,k->rx,now,action,arg)){pj_rmt_close(r,k);return;} /* forged, replayed or skipped */
    pj_rmt_action(r,k,action,arg,now);
}
/* Physical button readings (every FPGA Buttons message). Answers pairing; physical
 * presses also release remote holds. */
static inline void pj_rmt_physical(pj_remote *r,uint16_t value,uint32_t now) {
    uint16_t buttons=value&PJ_BTN_MASK,pressed=(uint16_t)(buttons&~r->physical);
    r->physical=buttons;
    if(pressed)r->hold_bits=0;
    /* Held after an opening failure (no screen): any press means the owner wants Games. */
    if(pressed&&r->l->phase==PJ_L_DONE){r->owner_left=1;r->svc->owner_left=1;}
    if(r->pairing&&!r->pair_answer&&(pressed&(PJ_BTN_A|PJ_BTN_B)))r->pair_answer=(pressed&PJ_BTN_A)?1:2;
    (void)now;
}

/* ---- each pass ------------------------------------------------------------------- */
static inline void pj_rmt_poll(pj_remote *r,uint32_t now) {
    pj_svc *s=r->svc;r->now=now;
    /* Pairing answer or timeout; the result goes out on USB as soon as the link is free. */
    if(r->pairing&&!pj_rmt_busy_output(&r->usb)) {
        if(r->pair_answer==1) {
            char hex[65];pj_hex(r->pair_key,32,hex);memcpy(r->key,r->pair_key,32);r->key_kind=2;
            snprintf(r->pair_line,sizeof(r->pair_line),"PJ-PAIR %s",hex);pj_rmt_line(&r->usb,r->pair_line);
            memset(r->pair_line,0,sizeof(r->pair_line));memset(hex,0,sizeof(hex));memset(r->pair_key,0,32);
            r->pairing=0;if(r->log)pj_log_add(r->log,now,"chr1","paired over USB");
        } else if(r->pair_answer==2||(uint32_t)(now-r->pair_ms)>=PJ_RMT_PAIR_MS) {
            pj_rmt_line(&r->usb,"CHR1 PAIR-DENIED");memset(r->pair_key,0,32);r->pairing=0;
            if(r->log)pj_log_add(r->log,now,"chr1","pairing not allowed");
        }
    }
    pj_link *links[2]={&r->usb,&r->lan};
    for(int i=0;i<2;i++) {
        pj_link *k=links[i];
        if(k->state==PJ_LS_IDLE)continue;
        /* 1.3.2: while our answer is still going out the PC cannot ask again (one request at a time), so that is
         * not the PC being idle either. Before, a reply that took longer than 15 s (a 46 KB screen at the ~2.5 KB/s
         * of USB with Jellyfin open) closed the session the moment its last byte left.
         * 1.3.3 (1.3.2 review): only while the answer is really moving. A LAN peer that went half-open (the PC
         * asleep) stops taking bytes; output merely pending kept its session, and the console's one CHR1 owner, for
         * minutes until lwIP gave up. Now 15 s without a request and without one byte moved closes it. */
        size_t sent=k->head_sent+k->body_sent;
        if(k->state==PJ_LS_READY&&pj_rmt_busy_output(k)&&sent!=k->sent_mark)k->c.last_ms=now;
        k->sent_mark=sent;
        if(k->pending&&!pj_rmt_busy_output(k)) {
            k->c.last_ms=now; /* waiting on us is not the PC being idle */
            if(k->pending==PJ_PEND_HASH) {
                if(s->hash_ready){size_t n=pj_svc_hash_result(s,(char *)r->body,256);k->pending=0;pj_rmt_respond(r,k,"ok",r->body,n);}
                else if(s->hash_failed||!s->hashing){k->pending=0;pj_rmt_refuse(r,k,"error","flash read failed");}
            } else if(k->pending==PJ_PEND_VIDEO) {
                if(!r->video_slow||(uint32_t)(now-r->video_ms)>=PJ_RMT_VIDEO_SLOW_MS) {
                    size_t n=pj_rmt_video(r);r->video_ms=now;k->pending=0;pj_rmt_respond(r,k,"ok",r->body,n);
                }
            } else if(k->pending==PJ_PEND_FREEZE) {
                if(!pj_svc_in_session(s)){k->pending=0;pj_rmt_refuse(r,k,"error","session ended");}
                else if(pj_svc_freeze_done(s)) {
                    size_t n=(size_t)snprintf((char *)r->body,80,"screen=%s wait-ms=%u",pj_svc_screen_names[s->screen%3u],
                                              PJ_SVC_FREEZE_MS);
                    k->pending=0;pj_rmt_respond(r,k,"ok",r->body,n);
                }
            }
        }
        if(k->state==PJ_LS_AUTH&&(uint32_t)(now-k->opened_ms)>=PJ_CHR1_IDLE_MS)pj_rmt_close(r,k);
        else if(k->state==PJ_LS_READY&&pj_chr1_idle(&k->c,now)) { /* busy here: the answer stopped moving 15 s ago */
            int stalled=pj_rmt_busy_output(k);
            if(r->log)pj_log_add(r->log,now,"chr1",stalled?"answer stalled for 15 s":"no request for 15 s");
            if(stalled){k->head_len=k->head_sent=0;k->body=NULL;k->body_len=k->body_sent=0;} /* nobody takes it: closed at once */
            pj_rmt_close(r,k);
        }
    }
    if(r->hold_bits&&(uint32_t)(now-r->hold_ms)>=PJ_CHR1_HOLD_LEASE_MS)r->hold_bits=0;
    /* Lease: a session or a pairing may hold the restart after an opening failure, 10 min. */
    int want=r->owner!=NULL&&!r->lease_spent&&!r->owner_left; /* never for an unauthenticated pairing */
    if(r->lease_on)r->lease_used+=now-r->lease_mark;
    r->lease_mark=now;
    if(r->lease_used>=PJ_RMT_LEASE_MS){r->lease_spent=1;want=0;}
    r->lease_on=(uint8_t)want;
}
/* Call every pass after pj_launch_step. Returns 1 while the restart to stock is held. */
static inline int pj_rmt_link(pj_remote *r,pj_launch_action *a,uint32_t now) {
    pj_launcher *l=r->l;
    int held=pj_svc_link(r->svc,l,a,now);
    l->service=(uint8_t)(pj_svc_in_session(r->svc)||r->pairing);
    if(!held&&r->lease_on&&l->phase==PJ_L_DONE&&pj_svc_hold_reason(l->done_reason)) {
        if(r->svc->release_sent)a->poke=0;
        r->svc->release_sent=1;a->restart_to_stock=0;held=1;
    }
    /* While held, keep reading the buttons so a physical press can end the hold. */
    if(held&&(uint32_t)(now-l->poll_ms)>=PJ_LAUNCH_POLL_MS){a->request_buttons=1;l->poll_ms=now;}
    return held;
}
/* The launcher is about to hand over: sessions end, nothing more is answered. */
static inline void pj_rmt_shutdown(pj_remote *r) {
    pj_rmt_drop(r,&r->usb);pj_rmt_drop(r,&r->lan);
    r->usb.head_len=r->usb.head_sent=r->usb.body_len=r->usb.body_sent=0;r->usb.close_after=0;
    r->lan.want_close=1;r->pairing=0;
}
/* Copies up to `size` owed bytes for a link into `out`. */
static inline size_t pj_rmt_out(pj_remote *r,pj_link *k,uint8_t *out,size_t size) {
    size_t n=0;
    while(n<size&&k->head_sent<k->head_len)out[n++]=(uint8_t)k->head[k->head_sent++];
    while(n<size&&k->body_sent<k->body_len)out[n++]=k->body[k->body_sent++];
    if(!pj_rmt_busy_output(k)&&k->close_after) {
        k->close_after=0;pj_rmt_drop(r,k);if(k->kind==PJ_LINK_LAN)k->want_close=1;
    }
    return n;
}
/* 1 while USB carries CHR1, so the shell keeps PJ-DIAG lines off the UART (ring only). */
static inline int pj_rmt_usb_quiet(const pj_remote *r) {
    return r->usb.state!=PJ_LS_IDLE||pj_rmt_busy_output(&r->usb);
}
static inline int pj_rmt_owns_screen(const pj_remote *r) {return r->pairing||pj_svc_owns_screen(r->svc);}
#endif
