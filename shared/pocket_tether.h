/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Chromatic <-> Pocket tether, phase 2 contract (docs/Design-notes.md).
 * Shared by the Chromatic gateway (chromatic/launcher/shared/pocket_link.h), the Pocket app
 * (pocket/tether.h) and the simulations. Numbers only; no I/O.
 *
 * Link clock. The Pocket is the clock master. It opens at 256 kHz (the link fast-mode rate
 * every cable and level shifter carries), then raises the clock to what the Chromatic says
 * in HELLO ("link_khz"). The Chromatic's engine runs on gClk 8.388 MHz with a two-flop
 * synchroniser: a 512 kHz half period is 8.2 gClk cycles. 512 kHz is the streaming rate and
 * the fastest one used (64,000 bytes/s raw each way); 1 MHz (4.1 cycles) does not pass the
 * Chromatic's link input filter and is never asked. What a stream really gets end to end is
 * much less than the raw cable rate: see the stream profiles below.
 *
 * MCU <-> FPGA UART. Stock: 115,200 baud (divider 72 + correction: 72.75 gClk per bit). Under
 * the media-link key the bridge may switch UART2 to 524,288 baud = gClk / 16 exactly (divider
 * 16, correction off, the RX filter bypassed; ESP32: IDF clk_div 2441/16 of 80 MHz = 524,375,
 * +0.017%). Simulated with UART2 itself (rtl/chromatic_uart2_tb.sv). 52,429 bytes/s raw; 64-byte
 * data packets (68 on the wire) give about 49,300 bytes/s downstream. (The first proposal,
 * 1,048,576 with the correction left on, was really 958,698 baud and failed.) Stock firmware never sends
 * the key, so stock never sees the fast rate; SysCtrl, leave, reset and heartbeat expiry
 * return the UART to 115,200 with the pins.
 */
#ifndef PJ_POCKET_TETHER_H
#define PJ_POCKET_TETHER_H
#include <stdint.h>
#include <stddef.h>

/* ---- link clock ---- */
#define PJ_TETHER_LINK_KHZ_OPEN 256u
#define PJ_TETHER_LINK_KHZ_STREAM 512u
#define PJ_TETHER_LINK_KHZ_FAST 512u /* was 1000: 1 MHz does not pass the Chromatic's link input filter */

/* ---- MCU <-> FPGA wire (chromatic_pocket_link.v) ---- */
#define PJ_TETHER_UART_SLOW_BAUD 115200u
#define PJ_TETHER_UART_FAST_BAUD 524288u       /* gClk 8,388,608 / 16, correction off (uart.v hook) */
#define PJ_TETHER_UART_FAST_DIVIDER 16u
#define PJ_TETHER_DATA_BYTES 64u               /* 0x1F payload, phase 2 (phase 1: 8) */
#define PJ_TETHER_CTRL_ENTER 1u
#define PJ_TETHER_CTRL_LEAVE 2u
#define PJ_TETHER_CTRL_HEARTBEAT 3u
#define PJ_TETHER_CTRL_FAST 4u                 /* keyed: UART2 to the fast rate once TX is idle */
#define PJ_TETHER_STATUS_FAST_BIT 0x40u        /* status byte 0 bit 6: UART is at the fast rate */
#define PJ_TETHER_QUEUE 1024u
#define PJ_TETHER_MARGIN 48u

/* ---- Pocket-acknowledged credit (launcher 1.5.0, docs/Design-notes.md) ----
 * The unit. What the launcher hands the bridge in a 0x1F packet is a run of PJ wire bytes
 * (shared/link_framing.h pj_wire_tx_next: 7E flags, escaped payload); the wire escapes 0xFF, so
 * none of them is 0xFF, and the launcher pads nothing. The bridge queues exactly those bytes and,
 * with its queue empty, clocks out the idle byte 0xFF, which the Pocket's link block drops before
 * its receive queue (rtl/pocket_media_link.v idle_rx). So every byte the Pocket receives is one
 * the launcher handed over, in the same order, and the two ends count the same bytes.
 * The ACK. While the Pocket waits for a reply it sends, in place of the lone 7E filler, one frame
 * of PJ_TETHER_ACK_SIZE bytes: [seq][count lo][count hi][check lo][check hi]. Five, less than any
 * request (16 or more): with its two 7Es that is 7 wire bytes (more only when a byte needs
 * escaping), so it reaches the launcher in two bridge statuses (the bridge sends the first one as
 * soon as the leading 7E waits, the other six fit the next). seq is the low
 * byte of the sequence of the last reply it received whole and valid (its "anchor"), count the
 * bytes it has taken off the cable since that reply's closing 7E (received plus its receive
 * queue; saturates at 65,535). The launcher knows where each reply it sent ends in its own count
 * of handed-over bytes, so anchor end + count is a byte it knows has left the bridge: the bound
 * (handed over) - (that) can only be too large, never too small, however late the ACK. The Pocket
 * drops its anchor (sends the plain filler) after a damaged frame, a link fault or a reset, until
 * the next whole reply. Sent only to a launcher whose HELLO said "credit_ack":1. */
#define PJ_TETHER_ACK_TAG 0x4bu  /* 'K': not sent, but the first byte of what the check covers */
#define PJ_TETHER_ACK_SIZE 5u
/* The check: the low 16 bits of the CRC-32 (reflected 0xEDB88320, as pj_av_crc) of 'K', seq, count lo, count hi.
 * A damaged ACK that passes it (1 in 65,536) still cannot claim more than the launcher sent (pj_plink_ack refuses
 * that); the bridge's link input filter and the wire framing are what keep damage rare. */
static inline uint16_t pj_tether_ack_check(const uint8_t *p) {
    uint8_t m[4]={PJ_TETHER_ACK_TAG,p[0],p[1],p[2]};uint32_t crc=0xffffffffu;
    for(unsigned i=0;i<4;i++){crc^=m[i];for(unsigned b=0;b<8;b++)crc=(crc>>1)^(0xedb88320u&(0u-(crc&1u)));}
    return (uint16_t)(crc^0xffffffffu);
}
static inline void pj_tether_ack_build(uint8_t *p,uint8_t seq,uint32_t count) {
    if(count>65535u)count=65535u; /* smaller is safe: the launcher's bound only grows */
    p[0]=seq;p[1]=(uint8_t)count;p[2]=(uint8_t)(count>>8);
    uint16_t c=pj_tether_ack_check(p);p[3]=(uint8_t)c;p[4]=(uint8_t)(c>>8);
}
/* 1: a well-formed ACK (seq, count out); 0: not one. */
static inline int pj_tether_ack_parse(const uint8_t *p,size_t n,uint8_t *seq,uint32_t *count) {
    if(n!=PJ_TETHER_ACK_SIZE||pj_tether_ack_check(p)!=(uint16_t)(p[3]|(p[4]<<8)))return 0;
    *seq=p[0];*count=(uint32_t)p[1]|((uint32_t)p[2]<<8);return 1;
}

/* ---- modelled throughput (bytes/s), for the design table and the simulations ---- */
static inline unsigned pj_tether_uart_payload_bps(unsigned baud,unsigned packet_bytes) {
    /* 10 bits per byte on the wire; 4 overhead bytes (0x8F, address, count, CRC) per packet */
    return (unsigned)((uint64_t)baud/10u*packet_bytes/(packet_bytes+4u));
}
static inline unsigned pj_tether_link_raw_bps(unsigned khz){return khz*1000u/8u;}

/* ---- stream profiles the Pocket asks for (PJB1 START "profile"), served by the Chromatic ----
 * Geometry must satisfy shared/av_stream.h: width 128/160/256, height 96/128/144. 16:9 titles
 * are asked as 160x96 (5:3, a 6% horizontal squeeze), 4:3 titles as 128x96. The Chromatic
 * refuses START when the server's AVI does not come back in a whitelisted size.
 *
 * The stream budget. What reaches the Pocket's player end to end (PJAV bytes a second, after
 * framing escapes, the reply header of every READ and the READ turnaround), measured in
 * pocket/check_tether_app.c with the launcher's real credit rule, its 10 ms pass and 504-byte
 * UART room, and the bridge's 1,024-byte queue:
 *   512 kHz cable, Pocket ACKs on (launcher 1.5.0)       41.0 KB/s  (the launcher's 10 ms pass and its 504-byte UART
 *                                                        room are the limit; the credit never stops a send);
 *                                                        table: 40.0. With the 1.4.0 rule (filler, credit
 *                                                        allowance for a late status, or an older Pocket
 *                                                        app or launcher) 36.9.
 *   256 kHz cable, Pocket filler on                      29.1 KB/s  (the cable is the limit); table: 26.7
 *   either rate with no filler                           18.2 KB/s  (the bridge's 50 ms status period);
 *                                                        table: 18.8, the earlier figure (no profile
 *                                                        is sized from it; the check allows -10 %)
 * A profile must fit the rate it is for with PJ_TETHER_STREAM_HEADROOM (85 %) when the server
 * uses its whole video allowance: video_kbit * 125 bytes, one 32-byte PJAV header a picture, the
 * sound at its worst (16 kHz mono IMA in 128-byte blocks of 249 samples, each with a 32-byte
 * header: 10,281 bytes/s), and 2 % for framing escapes on top. pj_tether_profile_fits() is that
 * sum; pocket/check_tether_app.c refuses a table with a row that does not fit, and checks the
 * figures above against what it measures.
 *
 * Names. The Pocket asks by link rate and frame rate ("pocket-link-<kHz>-<fps>", the first
 * PJ_TETHER_LINK_PROFILES rows; pj_tether_profile_for() chooses). The three older names stay
 * for an older Pocket app and are sized to fit as well. The Chromatic answers an unknown name
 * with row 0, so row 0 is the smallest. link_khz is the slowest cable rate the row fits. */
typedef struct {const char *name;unsigned width,height,fps,video_kbit,link_khz;} pj_tether_profile;
#define PJ_TETHER_LINK_PROFILES 5u
static inline const pj_tether_profile *pj_tether_profiles(unsigned *count) {
    static const pj_tether_profile table[]={
        {"pocket-link-256-24",128,96,24,85,256},
        {"pocket-link-256-30",128,96,30,85,256},
        {"pocket-link-512-24",128,96,24,150,512},
        {"pocket-link-512-30",128,96,30,150,512},
        {"pocket-link-512-60",128,96,60,140,512},
        {"pocket-link-jpeg-min-24",128,96,24,85,256},
        {"pocket-link-jpeg-low-30",128,96,30,85,256},
        {"pocket-link-jpeg-motion-60",128,96,60,140,512},
    };
    if(count)*count=8;
    return table;
}
static inline const pj_tether_profile *pj_tether_profile_find(const char *name) {
    unsigned n;const pj_tether_profile *t=pj_tether_profiles(&n);
    for(unsigned i=0;i<n;i++){const char *a=t[i].name,*b=name;while(*a&&*a==*b){a++;b++;}if(!*a&&!*b)return &t[i];}
    return NULL;
}
#define PJ_TETHER_STREAM_BPS_512 40000u    /* measured end to end, ACKs on (1.5.0): 41.0 KB/s, rounded down (1.4.0: 36000) */
#define PJ_TETHER_STREAM_BPS_256 26700u
#define PJ_TETHER_STREAM_BPS_PLAIN 18800u  /* measured with no filler, at either rate */
#define PJ_TETHER_STREAM_HEADROOM 85u      /* per cent of the measured figure a profile may use */
#define PJ_TETHER_SOUND_BPS 10281u         /* 16 kHz mono IMA, 128-byte blocks, 32-byte headers */
/* PJAV bytes a second a link rate brings to the player; 0: too slow to play. */
static inline unsigned pj_tether_stream_capacity(unsigned khz) {
    return khz>=512u?PJ_TETHER_STREAM_BPS_512:khz>=256u?PJ_TETHER_STREAM_BPS_256:0u;
}
/* Bytes a second a profile needs when the server uses its whole allowance. */
static inline unsigned pj_tether_profile_need(const pj_tether_profile *p) {
    return (unsigned)((uint64_t)(p->video_kbit*125u+p->fps*32u+PJ_TETHER_SOUND_BPS)*102u/100u);
}
static inline int pj_tether_profile_fits(const pj_tether_profile *p) {
    return (uint64_t)pj_tether_profile_need(p)*100u<=(uint64_t)pj_tether_stream_capacity(p->link_khz)*PJ_TETHER_STREAM_HEADROOM;
}
/* The profile to ask for at a link rate with the owner's 24/30/60 choice: the set for the
 * fastest rate not above the link's, and in it the highest frame rate not above the choice
 * (60 at 256 kHz is asked as 30). NULL below 256 kHz. */
static inline const pj_tether_profile *pj_tether_profile_for(unsigned khz,unsigned wanted_fps) {
    const pj_tether_profile *t=pj_tether_profiles(NULL),*best=NULL;
    for(unsigned i=0;i<PJ_TETHER_LINK_PROFILES;i++) {
        if(t[i].link_khz>khz)continue;
        if(!best||t[i].link_khz>best->link_khz){best=&t[i];continue;}
        if(t[i].link_khz<best->link_khz)continue;
        int fits=t[i].fps<=wanted_fps,had=best->fps<=wanted_fps;
        if(fits&&(!had||t[i].fps>best->fps))best=&t[i];
        else if(!fits&&!had&&t[i].fps<best->fps)best=&t[i];
    }
    return best;
}
static inline int pj_tether_geometry_ok(unsigned w,unsigned h) {
    return (w==128||w==160||w==256)&&(h==96||h==128||h==144);
}
/* The PJAV CONFIG fps field is one of 24/25/30/60; the title keeps its own cadence through
 * per-packet timestamps. Rounds a measured frame period (us) to the nearest allowed value. */
static inline unsigned pj_tether_fps_field(uint32_t frame_us) {
    static const unsigned allowed[]={24,25,30,60};unsigned best=30;uint32_t best_err=0xffffffffu;
    if(!frame_us)return 30;
    for(unsigned i=0;i<4;i++){uint32_t us=1000000u/allowed[i];uint32_t err=us>frame_us?us-frame_us:frame_us-us;if(err<best_err){best_err=err;best=allowed[i];}}
    return best;
}

/* ---- artwork identity across syncs ----
 * The gateway labels every item with "art": a 32-bit FNV-1a of the image item id and image
 * kind it would serve. The Pocket keeps art by this key, so a re-sync fetches only tiles whose
 * picture changed, however the per-session handles were numbered. 0 means no art. */
static inline uint32_t pj_tether_art_key(const char *image_id,unsigned image_kind) {
    if(!image_id||!image_id[0]||!image_kind)return 0;
    uint32_t h=2166136261u;
    for(const char *p=image_id;*p;p++){h^=(uint8_t)*p;h*=16777619u;}
    h^=(uint8_t)image_kind;h*=16777619u;
    return h?h:1u;
}

/* ---- Pocket cache on the card (pocket/tether_store.h) ----
 * openfpgaOS nonvolatile data slots: ids 10..19 are 256 KiB each in CRAM0, persisted by the
 * Pocket at core exit (upstream/openfpgaCore targets/pocket/save.c, kernel/syscall.c sys_close).
 * Slot 10 is the diagnostics log; the cache uses 11..19: slot 11 (pjcache0.sav) holds the store
 * header and the watch state, slots 12..19 (pjcache1..8.sav) the library image, 2 MiB. */
#define PJ_TETHER_SLOT_BYTES 262144u
#define PJ_TETHER_SLOT_FIRST 11u
#define PJ_TETHER_SLOTS 9u
#define PJ_TETHER_CACHE_BYTES (PJ_TETHER_SLOT_BYTES*PJ_TETHER_SLOTS)   /* 2,359,296 */
#define PJ_TETHER_CACHE_FILE_PREFIX "pjcache"                          /* pjcache0.sav .. pjcache8.sav */

/* ---- timing the Pocket app uses ---- */
#define PJ_TETHER_PING_US 2000000u       /* idle HELLO cadence while no Chromatic answers */
#define PJ_TETHER_ALIVE_US 5000000u      /* idle HELLO cadence while connected (unplug detection) */
#define PJ_TETHER_GAP_US 3000u           /* clock-quiet gap so the Chromatic's receiver can arm */
#define PJ_TETHER_LOST_US 9000000u       /* no reply for this long: unplugged */
#endif
