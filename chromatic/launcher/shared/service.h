/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Service sessions: Chromatic Manager's jobs, run as CHR1 actions (shared/remote.h,
 * docs/Design-notes.md) so the console can show what is happening, with a
 * live percentage for the read-only work the launcher does itself. Portable C,
 * host-tested (check_remote.c); the ESP32 shell only supplies the flash read.
 *
 * Rules this file keeps:
 *  - Read only. The only flash access is the shell's read callback; nothing writes,
 *    erases or touches settings.
 *  - Never serves secrets: the settings partition (NVS, 0x9000-0xF000) and both
 *    provisioning sectors (0x3FE000-0x400000: Wi-Fi passwords, CHR1 key, Jellyfin token)
 *    are refused to svc-read and svc-hash alike (a hash would allow guessing a weak
 *    password offline). The PC reads those with esptool, as before.
 *  - Bounded: a session ends on svc-end, 20 min after it began, 15 s after a FREEZE
 *    with no reset, or when its CHR1 link closes; every end leads to stock.
 *  - The owner can always leave: the menu button closes the overlay and ends it.
 *  - Bounded work per main-loop pass (16 KiB of hashing), so the watchdog is fed.
 */
#ifndef PJ_SERVICE_H
#define PJ_SERVICE_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "launcher_logic.h"
#include "md5.h"
#include "sha256.h"

#define PJ_SVC_READ_MAX 0x4000u
#define PJ_SVC_HASH_STEP 4096u
#define PJ_SVC_HASH_STEPS 4u   /* hashing per main-loop pass: 16 KiB, a few ms */
#define PJ_SVC_MAX_MS 1200000u /* 20 minutes */
#define PJ_SVC_FREEZE_MS 15000u
#define PJ_SVC_DONE_MS 1500u
#define PJ_SVC_SPIN_MS 100u
#define PJ_SVC_FLASH_BYTES 0x400000u

typedef enum {PJ_SVC_OFF,PJ_SVC_ACTIVE,PJ_SVC_FROZEN,PJ_SVC_FINISHING,PJ_SVC_ENDED} pj_svc_state;
static const char *const pj_svc_state_names[]={"off","active","frozen","finishing","ended"};
typedef enum {
    PJ_SVC_OP_STATUS,PJ_SVC_OP_BACKUP,PJ_SVC_OP_VERIFY,PJ_SVC_OP_INSTALL,PJ_SVC_OP_UPDATE,PJ_SVC_OP_REMOVE,
    PJ_SVC_OP_RESTORE,PJ_SVC_OP_PROVISION,PJ_SVC_OP_COUNT
} pj_svc_op;
static const char *const pj_svc_op_names[PJ_SVC_OP_COUNT]={
    "status","backup","verify","install","update","remove","restore","provision"};
typedef enum {
    PJ_SVC_END_NONE,PJ_SVC_END_OK,PJ_SVC_END_FAIL,PJ_SVC_END_LINK_CLOSED,PJ_SVC_END_TIME_CAP,
    PJ_SVC_END_FREEZE_TIMEOUT,PJ_SVC_END_CONSOLE_CLOSED,PJ_SVC_END_COUNT
} pj_svc_end_reason;
static const char *const pj_svc_end_names[PJ_SVC_END_COUNT]={
    "none","ok","fail","link-closed","time-cap","freeze-timeout","console-closed"};
typedef enum {PJ_SVC_SCREEN_OPENING,PJ_SVC_SCREEN_SHOWN,PJ_SVC_SCREEN_HIDDEN} pj_svc_screen;
static const char *const pj_svc_screen_names[3]={"opening","shown","hidden"};
typedef enum {PJ_SVC_KIND_WRITE,PJ_SVC_KIND_CHECK} pj_svc_kind;

/* Reads `size` bytes of flash at `offset` into `out`; returns 0 on success. */
typedef int (*pj_svc_read_fn)(void *ctx,uint32_t offset,void *out,uint32_t size);

typedef struct {
    /* Set once by the shell. The MAC and flash ID only ever leave as a salted digest. */
    uint8_t mac[6];uint32_t flash_id;unsigned chip_rev;
    pj_svc_read_fn read;void *read_ctx;
    uint8_t state,op,end_reason,screen,kind,redraw,frame_sent,release_sent,released;
    uint8_t hashing,hash_failed,hash_ready;
    uint8_t owner_left; /* a physical press while held: the owner wants out (remote.h sets it) */
    uint32_t begin_ms,state_ms,total,served;
    unsigned pct,pc_pct,spin;int has_pc_pct;
    uint32_t job_off,job_len,job_pos;pj_md5 md5;pj_sha256 sha;
    uint8_t chunk[PJ_SVC_HASH_STEP];
} pj_svc;

static inline int pj_svc_in_session(const pj_svc *s) {
    return s->state==PJ_SVC_ACTIVE||s->state==PJ_SVC_FROZEN||s->state==PJ_SVC_FINISHING;
}
static inline void pj_svc_init(pj_svc *s,const uint8_t mac[6],uint32_t flash_id,unsigned chip_rev,
                               pj_svc_read_fn read,void *read_ctx) {
    memset(s,0,sizeof(*s));
    memcpy(s->mac,mac,6);s->flash_id=flash_id;s->chip_rev=chip_rev;s->read=read;s->read_ctx=read_ctx;
    s->state=PJ_SVC_OFF;s->screen=PJ_SVC_SCREEN_OPENING;
}
/* Ends the session for good; the launcher goes on to stock (pj_svc_link). A launcher
 * never starts a second session. */
static inline void pj_svc_finish(pj_svc *s,pj_svc_end_reason why) {
    if(!pj_svc_in_session(s))return;
    s->state=PJ_SVC_ENDED;s->end_reason=(uint8_t)why;s->hashing=0;s->redraw=0;
}
/* 1..8 lowercase hex digits, nothing else. */
static inline int pj_svc_parse_hex8(const char *text,uint32_t *value) {
    size_t n=strlen(text);uint32_t v=0;
    if(!n||n>8)return 0;
    for(size_t i=0;i<n;i++) {
        char c=text[i];
        if(c>='0'&&c<='9')v=v<<4|(uint32_t)(c-'0');else if(c>='a'&&c<='f')v=v<<4|(uint32_t)(c-'a'+10);else return 0;
    }
    *value=v;return 1;
}
static inline int pj_svc_range(uint32_t off,uint32_t len,uint32_t limit) {
    return len&&len<=limit&&off<PJ_SVC_FLASH_BYTES&&len<=PJ_SVC_FLASH_BYTES-off;
}
#define PJ_SVC_NVS_START 0x9000u
#define PJ_SVC_NVS_END 0xf000u
#define PJ_SVC_PROVISION_START 0x3fe000u
#define PJ_SVC_PROVISION_END 0x400000u
/* 1 if [off, off+len) touches the settings partition or the provisioning sectors. */
static inline int pj_svc_secret(uint32_t off,uint32_t len) {
    uint32_t end=off+len;
    return (off<PJ_SVC_NVS_END&&end>PJ_SVC_NVS_START)||(off<PJ_SVC_PROVISION_END&&end>PJ_SVC_PROVISION_START);
}

/* ---- the actions (each returns NULL, or a short refusal reason) ---------------- */
/* total: bytes the PC will hash plus read, for the percentage (0: spinner only). */
static inline const char *pj_svc_begin(pj_svc *s,const char *op,uint32_t total,uint32_t now) {
    int index=-1;
    for(unsigned i=0;i<PJ_SVC_OP_COUNT;i++)if(!strcmp(pj_svc_op_names[i],op))index=(int)i;
    if(index<0)return "unknown operation";
    if(s->state!=PJ_SVC_OFF)return s->state==PJ_SVC_ENDED?"session already used":"session already open";
    s->state=PJ_SVC_ACTIVE;s->op=(uint8_t)index;s->total=total;s->begin_ms=s->state_ms=now;
    s->served=0;s->pct=0;s->has_pc_pct=0;s->spin=0;s->redraw=1;
    return NULL;
}
/* SHA-256 of "PJ-SVC-ID1", the PC's 8 nonce bytes, the factory MAC's 6 bytes (as
 * esptool read_mac prints them) and the 3 JEDEC flash ID bytes. The PC computes the
 * same from its backup record; the MAC itself never leaves the chip. */
static inline void pj_svc_id_digest(const uint8_t nonce[8],const uint8_t mac[6],uint32_t flash_id,uint8_t out[32]) {
    uint8_t jedec[3]={(uint8_t)(flash_id>>16),(uint8_t)(flash_id>>8),(uint8_t)flash_id};
    pj_sha256 h;pj_sha256_init(&h);
    pj_sha256_update(&h,"PJ-SVC-ID1",10);pj_sha256_update(&h,nonce,8);pj_sha256_update(&h,mac,6);
    pj_sha256_update(&h,jedec,3);pj_sha256_final(&h,out);
}
static inline const char *pj_svc_read(pj_svc *s,uint32_t off,uint32_t len,uint8_t *out) {
    if(s->state!=PJ_SVC_ACTIVE)return "no active service session";
    if(s->hashing)return "busy hashing";
    if(!pj_svc_range(off,len,PJ_SVC_READ_MAX))return "range";
    if(pj_svc_secret(off,len))return "protected: settings and provisioning are not served";
    if(s->read(s->read_ctx,off,out,len))return "flash read failed";
    s->served+=len;
    return NULL;
}
static inline const char *pj_svc_hash_start(pj_svc *s,uint32_t off,uint32_t len) {
    if(s->state!=PJ_SVC_ACTIVE)return "no active service session";
    if(s->hashing)return "busy hashing";
    if(!pj_svc_range(off,len,PJ_SVC_FLASH_BYTES))return "range";
    if(pj_svc_secret(off,len))return "protected: settings and provisioning are not served";
    s->hashing=1;s->hash_failed=0;s->hash_ready=0;s->job_off=off;s->job_len=len;s->job_pos=0;
    pj_md5_init(&s->md5);pj_sha256_init(&s->sha);
    return NULL;
}
/* After hashing ends: "off=<hex> len=<hex> md5=<32 hex> sha256=<64 hex>". */
static inline size_t pj_svc_hash_result(pj_svc *s,char *out,size_t size) {
    uint8_t md5[16],sha[32];char md5_hex[33],sha_hex[65];
    pj_md5_final(&s->md5,md5);pj_sha256_final(&s->sha,sha);pj_hex(md5,16,md5_hex);pj_hex(sha,32,sha_hex);
    s->hash_ready=0;
    int n=snprintf(out,size,"off=%lx len=%lx md5=%s sha256=%s",(unsigned long)s->job_off,(unsigned long)s->job_len,
                   md5_hex,sha_hex);
    return n>0&&(size_t)n<size?(size_t)n:0;
}
static inline const char *pj_svc_set_pct(pj_svc *s,unsigned pct) {
    if(!pj_svc_in_session(s))return "no service session";
    if(pct>100u)return "pct is 0..100";
    s->pc_pct=pct;s->has_pc_pct=1;
    return NULL;
}
/* Draws the still screen. The PC may reset the chip once pj_svc_freeze_done says so. */
static inline const char *pj_svc_freeze(pj_svc *s,const char *kind,uint32_t now) {
    if(s->state!=PJ_SVC_ACTIVE)return "no active service session";
    if(s->hashing)return "busy hashing";
    if(strcmp(kind,"write")&&strcmp(kind,"check"))return "kind is write or check";
    s->kind=(uint8_t)(strcmp(kind,"write")?PJ_SVC_KIND_CHECK:PJ_SVC_KIND_WRITE);
    s->state=PJ_SVC_FROZEN;s->state_ms=now;s->frame_sent=0;s->redraw=1;
    return NULL;
}
/* The FREEZE answer may go once the still frame went out, or at once if it cannot be seen. */
static inline int pj_svc_freeze_done(const pj_svc *s) {
    return s->state!=PJ_SVC_FROZEN||s->frame_sent||s->screen!=PJ_SVC_SCREEN_SHOWN;
}
static inline const char *pj_svc_end(pj_svc *s,int ok,uint32_t now) {
    if(s->state!=PJ_SVC_ACTIVE&&s->state!=PJ_SVC_FROZEN)return "no service session to end";
    if(s->hashing)return "busy hashing";
    s->end_reason=(uint8_t)(ok?PJ_SVC_END_OK:PJ_SVC_END_FAIL);
    if(ok)s->pct=100;
    s->state=PJ_SVC_FINISHING;s->state_ms=now;s->redraw=1;
    return NULL;
}

/* ---- work ------------------------------------------------------------------------ */
static inline unsigned pj_svc_auto_pct(const pj_svc *s) {
    if(s->has_pc_pct)return s->pc_pct;
    if(!s->total)return 0;
    uint64_t pct=(uint64_t)s->served*100u/s->total;
    return pct>99u?99u:(unsigned)pct;
}
/* Once per main-loop pass: timeouts, a bounded piece of hashing, the spinner. */
static inline void pj_svc_poll(pj_svc *s,uint32_t now) {
    if(!pj_svc_in_session(s))return;
    if((uint32_t)(now-s->begin_ms)>=PJ_SVC_MAX_MS){pj_svc_finish(s,PJ_SVC_END_TIME_CAP);return;}
    if(s->state==PJ_SVC_FROZEN&&(uint32_t)(now-s->state_ms)>=PJ_SVC_FREEZE_MS){pj_svc_finish(s,PJ_SVC_END_FREEZE_TIMEOUT);return;}
    if(s->state==PJ_SVC_FINISHING&&(uint32_t)(now-s->state_ms)>=PJ_SVC_DONE_MS) {
        pj_svc_finish(s,(pj_svc_end_reason)s->end_reason);return;
    }
    if(s->hashing) {
        for(unsigned step=0;step<PJ_SVC_HASH_STEPS&&s->job_pos<s->job_len;step++) {
            uint32_t n=s->job_len-s->job_pos;if(n>PJ_SVC_HASH_STEP)n=PJ_SVC_HASH_STEP;
            if(s->read(s->read_ctx,s->job_off+s->job_pos,s->chunk,n)){s->hashing=0;s->hash_failed=1;return;}
            pj_md5_update(&s->md5,s->chunk,n);pj_sha256_update(&s->sha,s->chunk,n);s->job_pos+=n;s->served+=n;
        }
        if(s->job_pos==s->job_len){s->hashing=0;s->hash_ready=1;}
    }
    unsigned pct=pj_svc_auto_pct(s);
    if(s->state==PJ_SVC_FINISHING&&s->end_reason==PJ_SVC_END_OK)pct=100;
    if(pct>s->pct){s->pct=pct;s->redraw=1;} /* only ever goes up */
    unsigned spin=(unsigned)((now-s->begin_ms)/PJ_SVC_SPIN_MS);
    if(s->state==PJ_SVC_ACTIVE&&spin!=s->spin){s->spin=spin;s->redraw=1;}
}
/* The shell sent the FREEZE frame: after it nothing is drawn again. */
static inline void pj_svc_frame_sent(pj_svc *s) {if(s->state==PJ_SVC_FROZEN)s->frame_sent=1;}
static inline int pj_svc_owns_screen(const pj_svc *s) {return pj_svc_in_session(s);}
/* 1 when a new frame should be sent now; never after the FREEZE frame, so the still
 * screen is what stays up while the ESP32 is in its ROM bootloader. */
static inline int pj_svc_wants_frame(const pj_svc *s) {
    if(!pj_svc_in_session(s)||!s->redraw||s->screen!=PJ_SVC_SCREEN_SHOWN)return 0;
    return !(s->state==PJ_SVC_FROZEN&&s->frame_sent);
}

/* ---- the launcher around it --------------------------------------------------- */
/* Opening failures that involve no owner action leave the overlay closed, so a session
 * may keep the launcher (screen hidden) until it ends. Anything the owner did does not,
 * including buttons held while opening (open-buttons-held). */
static inline int pj_svc_hold_reason(unsigned done_reason) {
    return done_reason==PJ_DONE_NO_FPGA_REPLY||done_reason==PJ_DONE_OPEN_NO_RESPONSE;
}
static inline uint8_t pj_svc_screen_of(const pj_launcher *l) {
    pj_launch_phase p=l->phase;
    return (p==PJ_L_MENU||p==PJ_L_MEDIA||p==PJ_L_OTHER)&&l->menu_open?PJ_SVC_SCREEN_SHOWN:
           (p==PJ_L_PRIME||p==PJ_L_OPEN_PRESS||p==PJ_L_OPEN_WAIT)?PJ_SVC_SCREEN_OPENING:PJ_SVC_SCREEN_HIDDEN;
}
/* Call every pass after pj_launch_step, with its action. Returns 1 while the restart to
 * stock is held for the session. Every session end leads to stock: an open overlay is
 * closed (close reason service-end), a held DONE goes on to the restart. */
static inline int pj_svc_link(pj_svc *s,pj_launcher *l,pj_launch_action *a,uint32_t now) {
    uint8_t screen=pj_svc_screen_of(l);
    if(screen!=s->screen){s->screen=screen;if(screen==PJ_SVC_SCREEN_SHOWN)s->redraw=1;}
    if(pj_svc_in_session(s)) {
        l->service=1;
        if(l->phase==PJ_L_DONE) {
            if(!pj_svc_hold_reason(l->done_reason)||s->owner_left)pj_svc_finish(s,PJ_SVC_END_CONSOLE_CLOSED);
            else {
                if(s->release_sent)a->poke=0; /* DONE's release is sent once, not every pass */
                s->release_sent=1;a->restart_to_stock=0;
                return 1;
            }
        }
    }
    if(s->state==PJ_SVC_ENDED&&!s->released) {
        s->released=1;
        pj_launch_phase p=l->phase;
        if(p==PJ_L_MENU||p==PJ_L_MEDIA||p==PJ_L_OTHER)pj_launch_begin_close(l,now,PJ_CLOSE_SERVICE_END);
    }
    if(s->state==PJ_SVC_ENDED)l->service=0;
    return 0;
}

/* ---- diagnostics (PJ-DIAG bodies; launcher_diag.h checks every line) ------------ */
static inline void pj_svc_diag_begin(char *out,size_t size,const pj_svc *s) {
    snprintf(out,size,"service begin op=%s screen=%s",pj_svc_op_names[s->op%PJ_SVC_OP_COUNT],
             pj_svc_screen_names[s->screen%3u]);
}
static inline void pj_svc_diag_end(char *out,size_t size,const pj_svc *s,uint32_t now) {
    snprintf(out,size,"service end reason=%s op=%s served=%lu ms=%lu",
             pj_svc_end_names[s->end_reason%PJ_SVC_END_COUNT],pj_svc_op_names[s->op%PJ_SVC_OP_COUNT],
             (unsigned long)s->served,(unsigned long)(now-s->begin_ms));
}
#endif
