/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.3.6: what the Pocket link hears, said so plainly that one console try settles it.
 *
 * On a test console the Pocket's first frame (HELLO) never validated although bytes arrived. This header keeps
 * the facts that tell the causes apart, all of them cheap: about 300 bytes inside pj_plink (which the link allocates
 * anyway), no task, no allocation of their own. (The launcher's UART1 event queue, which feeds the `plink rx uart`
 * counts, is separate: launcher_main.c PJ_UART1_EVENTS, about 0.3 KB of heap from boot on, in every mode.)
 *   - counters: upstream bytes and byte-carrying status packets, 0x7E flags seen, frames accepted and rejected (with
 *     the reason), frames the wire decoder tore down, bytes it dropped, bytes never given to it, each FPGA fault bit
 *     counted per occurrence, downstream bytes and replies;
 *   - the last 48 upstream bytes (a ring) and the FIRST 32 bytes after every quiet gap of 500 ms (a capture); the
 *     very first capture since the link began is kept as well, and said again every 30 s while the link does not
 *     work ("plink rx first"), because the launcher's 16 KB log holds under two minutes of its one-a-second lines;
 *   - an analysis of that capture against how HELLO must begin on the wire, 7E 50 4A 42 31 01 00 02 00 (flag, "PJB1",
 *     command 1, flag 0, length 2): intact, shifted by a number of bits, every bit inverted, bit order reversed in
 *     each byte, one bit lost or one bit doubled somewhere in the first eight bytes, nothing recognisable, or too
 *     few bytes to judge (under four);
 *   - the first 16 bytes of the last frame that arrived whole and was still refused;
 *   - the lines for the console log and the two-line sentence for the link screen. Once the link works the lines
 *     step aside: no bytes are printed and one short line is said every 30 s (pj_pld_periodic, pj_pld_event).
 * The FPGA drops idle 0xFF bytes before they reach the MCU, so every expected pattern is built the same way.
 * HELLO's content is fixed text, not private. Nothing here reads Wi-Fi or Jellyfin data. Portable C, no hardware:
 * chromatic/launcher/check_pocket_link.c runs all of it. */
#ifndef PJ_POCKET_LINK_DIAG_H
#define PJ_POCKET_LINK_DIAG_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define PJ_PLD_RING 48u        /* last upstream bytes kept */
#define PJ_PLD_CAP 32u         /* first bytes kept after a quiet gap */
#define PJ_PLD_GAP_MS 500u     /* no upstream byte for this long: the next byte begins a new capture */
#define PJ_PLD_SETTLE_MS 250u  /* a capture shorter than PJ_PLD_CAP is complete this long after its last byte */
#define PJ_PLD_HEAD 16u        /* bytes kept of a refused frame (the whole PJB1 header) */
#define PJ_PLD_REF 9u          /* known leading bytes of HELLO on the wire */
#define PJ_PLD_MIN_BITS 24u    /* fewer matching bits than this never name a pattern */
#define PJ_PLD_JUDGE 4u        /* a capture of fewer bytes than this is too short to judge ("short") */

/* Why a frame that arrived whole (flag to flag, escapes right) was refused. */
enum {PJ_PLR_NONE,PJ_PLR_SHORT,PJ_PLR_MAGIC,PJ_PLR_LENGTH,PJ_PLR_CRC,PJ_PLR_NOTREQ,PJ_PLR_COUNT};
static const char *const pj_pld_reason_names[PJ_PLR_COUNT]={"none","short","magic","len","crc","notreq"};
/* What a capture looks like. */
enum {PJ_PLA_EMPTY,PJ_PLA_NONE,PJ_PLA_EXACT,PJ_PLA_SHIFT,PJ_PLA_INVERTED,PJ_PLA_REVERSED,PJ_PLA_SLIP,PJ_PLA_DUP,PJ_PLA_ZEROS,PJ_PLA_SHORT};
/* kind; arg: SHIFT the bits (+ late, - early), SLIP/DUP the bit position; bits: matching leading bits; at: the byte of
 * the capture the pattern starts at. */
typedef struct {uint8_t kind;int8_t arg;uint8_t bits,at;} pj_pld_result;

typedef struct {
    uint32_t up_bytes,up_packets,flags,fed,frames_ok,frames_bad,reject[PJ_PLR_COUNT],torn,dropped,fault[4],down_bytes,replies;
    uint32_t last_up_ms,cap_ms;
    uint8_t heard,fault_prev,cap_open,cap_n,done_n,rej_reason,rej_head_n,first_n;
    uint16_t rej_size;
    uint32_t done_seq,rej_seq;        /* completed captures and refused frames, counted */
    uint32_t cap_ok0,cap_bad0,done_ok0,done_bad0; /* frames_ok/frames_bad when the capture began */
    pj_pld_result done_result;
    uint8_t ring[PJ_PLD_RING],cap[PJ_PLD_CAP],done[PJ_PLD_CAP],rej_head[PJ_PLD_HEAD];
    /* the first capture since the link began (first_n 0: none yet), and when its last byte came */
    uint32_t first_ms;pj_pld_result first_result;uint8_t first[PJ_PLD_CAP];
} pj_plink_diag;

/* Counters kept outside pj_plink: the V2 packet parser's (pocket_link.h pj_plink_rx) and the UART driver's events. */
typedef struct {uint32_t pkt_crc,pkt_other,pkt_len,ovf,full,parity,frame,brk,evq;} pj_pld_uart;

/* ---- the analysis (pure) ---- */
static const uint8_t pj_pld_ref[PJ_PLD_REF]={0x7e,0x50,0x4a,0x42,0x31,0x01,0x00,0x02,0x00};
static inline unsigned pj_pld_ref_bit(unsigned i){return (pj_pld_ref[i>>3]>>(7u-(i&7u)))&1u;} /* MSB first, as on the cable */
static inline uint8_t pj_pld_rev(uint8_t b){unsigned v=0;for(unsigned i=0;i<8;i++)if(b&(1u<<i))v|=0x80u>>i;return (uint8_t)v;}
/* Bits (one per element, first on the wire first) -> whole bytes; a byte of all ones is left out, as the FPGA drops
 * idle bytes; a last partial byte is left out. Returns the byte count. */
static inline unsigned pj_pld_pack(const uint8_t *bits,unsigned n,uint8_t *out) {
    unsigned count=0;
    for(unsigned i=0;i+8u<=n;i+=8u) {
        unsigned v=0;for(unsigned j=0;j<8;j++)v|=(unsigned)(bits[i+j]&1u)<<(7u-j);
        if(v!=0xffu)out[count++]=(uint8_t)v;
    }
    return count;
}
static inline unsigned pj_pld_lead(uint8_t a,uint8_t b){unsigned x=(unsigned)(a^b),n=0;while(n<8&&!(x&0x80u)){x<<=1;n++;}return n;}
/* The best place of expect[0..m) in cap[0..n): the count of equal leading bits there. full: every byte that could be
 * compared was equal, and they were at least min_bits. */
static inline unsigned pj_pld_match(const uint8_t *expect,unsigned m,const uint8_t *cap,unsigned n,unsigned min_bits,unsigned *at,int *full) {
    unsigned best=0;*at=0;*full=0;
    for(unsigned o=0;o<n;o++) {
        unsigned k=n-o<m?n-o:m,i=0;
        while(i<k&&expect[i]==cap[o+i])i++;
        unsigned bits=i*8u+(i<k?pj_pld_lead(expect[i],cap[o+i]):0u);
        int f=i==k&&bits>=min_bits;
        if((f&&!*full)||(f==*full&&bits>best)){best=bits;*at=o;*full=f;}
    }
    return best;
}
static inline pj_pld_result pj_pld_analyse(const uint8_t *cap,unsigned n) {
    pj_pld_result r={PJ_PLA_EMPTY,0,0,0};uint8_t bits[96],expect[12];unsigned at,m,got;int full;
    if(!cap||!n)return r;
    got=pj_pld_match(pj_pld_ref,PJ_PLD_REF,cap,n,PJ_PLD_MIN_BITS,&at,&full);
    r.kind=(uint8_t)(full?PJ_PLA_EXACT:PJ_PLA_NONE);r.bits=(uint8_t)got;r.at=(uint8_t)at;
    /* Three bytes can agree with the start (24 bits) and prove nothing about the rest: say so instead of "exact". */
    if(n<PJ_PLD_JUDGE){r.kind=PJ_PLA_SHORT;return r;}
    if(full)return r;
    {unsigned z=0;while(z<n&&!cap[z])z++;if(z==n&&n>=4u){r.kind=PJ_PLA_ZEROS;r.bits=0;r.at=0;return r;}}
    /* Shifted: the receiver's byte boundary is `lead` bits before the frame's first bit (the idle line reads ones).
     * Late by d bits and early by 8-d bits are the same bytes; the smaller one is named. The first byte straddles
     * the idle line and the flag; the pattern is also tried without it. */
    static const int8_t shifts[7]={1,-1,2,-2,3,-3,4};
    for(unsigned i=0;i<7;i++) {
        unsigned lead=shifts[i]>0?(unsigned)shifts[i]:(unsigned)(8+shifts[i]);
        for(unsigned j=0;j<lead;j++)bits[j]=1;
        for(unsigned j=0;j<PJ_PLD_REF*8u;j++)bits[lead+j]=(uint8_t)pj_pld_ref_bit(j);
        m=pj_pld_pack(bits,lead+PJ_PLD_REF*8u,expect);
        for(unsigned skip=0;skip<2;skip++) {
            got=pj_pld_match(expect+skip,m-skip,cap,n,PJ_PLD_MIN_BITS,&at,&full);
            if(full){r.kind=PJ_PLA_SHIFT;r.arg=shifts[i];r.bits=(uint8_t)got;r.at=(uint8_t)at;return r;}
        }
    }
    /* Every bit inverted; bit order reversed in every byte. */
    for(unsigned kind=PJ_PLA_INVERTED;kind<=PJ_PLA_REVERSED;kind++) {
        m=0;
        for(unsigned i=0;i<PJ_PLD_REF;i++) {
            uint8_t v=kind==PJ_PLA_INVERTED?(uint8_t)~pj_pld_ref[i]:pj_pld_rev(pj_pld_ref[i]);
            if(v!=0xffu)expect[m++]=v;
        }
        got=pj_pld_match(expect,m,cap,n,PJ_PLD_MIN_BITS,&at,&full);
        if(full){r.kind=(uint8_t)kind;r.arg=0;r.bits=(uint8_t)got;r.at=(uint8_t)at;return r;}
    }
    /* One bit lost (slip) or doubled (dup) at bit p of the first eight bytes; the bits after it arrive one early or
     * one late. The byte holding p and the next one must be there to compare. Bits of equal value in a row cannot be
     * told apart: the first of the row is named. The most matching bits win. */
    unsigned best=0;
    for(unsigned kind=PJ_PLA_SLIP;kind<=PJ_PLA_DUP;kind++)for(unsigned p=0;p<64u;p++) {
        unsigned count=0;
        for(unsigned j=0;j<PJ_PLD_REF*8u;j++) {
            if(kind==PJ_PLA_SLIP&&j==p)continue;
            bits[count++]=(uint8_t)pj_pld_ref_bit(j);
            if(kind==PJ_PLA_DUP&&j==p)bits[count++]=(uint8_t)pj_pld_ref_bit(j);
        }
        m=pj_pld_pack(bits,count,expect);
        unsigned need=((p>>3)+2u)*8u;if(need>m*8u)need=m*8u;if(need<PJ_PLD_MIN_BITS)need=PJ_PLD_MIN_BITS;
        got=pj_pld_match(expect,m,cap,n,need,&at,&full);
        if(full&&got>best){best=got;r.kind=(uint8_t)kind;r.arg=(int8_t)p;r.bits=(uint8_t)got;r.at=(uint8_t)at;}
    }
    return r;
}
/* The short code of a result: empty, none, exact, shift+1, shift-2, inverted, reversed, slip@bit13, dup@bit13, zeros,
 * short. */
static inline const char *pj_pld_code(pj_pld_result r,char *out,size_t size) {
    switch(r.kind) {
    case PJ_PLA_EMPTY:snprintf(out,size,"empty");break;
    case PJ_PLA_EXACT:snprintf(out,size,"exact");break;
    case PJ_PLA_SHIFT:snprintf(out,size,"shift%+d",(int)r.arg);break;
    case PJ_PLA_INVERTED:snprintf(out,size,"inverted");break;
    case PJ_PLA_REVERSED:snprintf(out,size,"reversed");break;
    case PJ_PLA_SLIP:snprintf(out,size,"slip@bit%d",(int)r.arg);break;
    case PJ_PLA_DUP:snprintf(out,size,"dup@bit%d",(int)r.arg);break;
    case PJ_PLA_ZEROS:snprintf(out,size,"zeros");break;
    case PJ_PLA_SHORT:snprintf(out,size,"short");break;
    default:snprintf(out,size,"none");break;
    }
    return out;
}

/* ---- keeping the facts (called by pocket_link.h) ---- */
static inline void pj_pld_close(pj_plink_diag *d) {
    if(!d->cap_open)return;
    d->cap_open=0;if(!d->cap_n)return;
    memcpy(d->done,d->cap,d->cap_n);d->done_n=d->cap_n;d->done_ok0=d->cap_ok0;d->done_bad0=d->cap_bad0;
    d->done_result=pj_pld_analyse(d->done,d->done_n);d->done_seq++;
    if(!d->first_n){memcpy(d->first,d->done,d->done_n);d->first_n=d->done_n;d->first_result=d->done_result;d->first_ms=d->cap_ms;}
}
/* One upstream byte, as the FPGA handed it over (before the wire decoder). */
static inline void pj_pld_byte(pj_plink_diag *d,uint8_t byte,uint32_t now) {
    if(!d->heard||(uint32_t)(now-d->last_up_ms)>=PJ_PLD_GAP_MS) {
        pj_pld_close(d);
        d->cap_open=1;d->cap_n=0;d->cap_ok0=d->frames_ok;d->cap_bad0=d->frames_bad;
    }
    d->heard=1;d->last_up_ms=now;
    d->ring[d->up_bytes%PJ_PLD_RING]=byte;d->up_bytes++;
    if(byte==0x7eu)d->flags++;
    if(d->cap_open){d->cap[d->cap_n++]=byte;d->cap_ms=now;if(d->cap_n>=PJ_PLD_CAP)pj_pld_close(d);}
}
/* Every loop pass: a short capture is complete once the line has been quiet for a moment. */
static inline void pj_pld_poll(pj_plink_diag *d,uint32_t now) {
    if(d->cap_open&&d->cap_n&&(uint32_t)(now-d->cap_ms)>=PJ_PLD_SETTLE_MS)pj_pld_close(d);
}
/* The FPGA's sticky fault bits of one status packet: each bit is counted when it appears (the launcher clears a
 * cable timeout with ENTER, so the next one is a new appearance). */
static inline void pj_pld_faults(pj_plink_diag *d,unsigned faults) {
    unsigned rise=faults&~(unsigned)d->fault_prev;
    for(unsigned i=0;i<4;i++)if(rise&(1u<<i))d->fault[i]++;
    d->fault_prev=(uint8_t)(faults&15u);
}
/* A frame that arrived whole and was refused: why, how long, and its first bytes. */
static inline void pj_pld_reject(pj_plink_diag *d,unsigned reason,const uint8_t *frame,size_t size) {
    if(reason<=PJ_PLR_NONE||reason>=PJ_PLR_COUNT)reason=PJ_PLR_NOTREQ;
    d->frames_bad++;d->reject[reason]++;d->rej_reason=(uint8_t)reason;d->rej_size=(uint16_t)(size>0xffffu?0xffffu:size);
    d->rej_head_n=(uint8_t)(size<PJ_PLD_HEAD?size:PJ_PLD_HEAD);memcpy(d->rej_head,frame,d->rej_head_n);d->rej_seq++;
}
/* The last upstream bytes, oldest first. Returns the count (at most PJ_PLD_RING). */
static inline unsigned pj_pld_ring_copy(const pj_plink_diag *d,uint8_t *out) {
    unsigned n=d->up_bytes<PJ_PLD_RING?(unsigned)d->up_bytes:PJ_PLD_RING;
    for(unsigned i=0;i<n;i++)out[i]=d->ring[(d->up_bytes-n+i)%PJ_PLD_RING];
    return n;
}

/* ---- the link screen's sentence ---- */
enum {PJ_PLH_NOTHING,PJ_PLH_CHECKING,PJ_PLH_GOOD,PJ_PLH_REJECTED,PJ_PLH_CUT,PJ_PLH_SHIFT,PJ_PLH_INVERTED,PJ_PLH_REVERSED,
      PJ_PLH_SLIP,PJ_PLH_DUP,PJ_PLH_ZEROS,PJ_PLH_UNKNOWN,PJ_PLH_SHORT,PJ_PLH_COUNT};
typedef struct {uint8_t what;int8_t arg;uint8_t bits,reason;} pj_pld_hint;
/* Judged on the last complete capture and on what became of the frames since it began. */
static inline pj_pld_hint pj_pld_hint_of(const pj_plink_diag *d) {
    pj_pld_hint h={PJ_PLH_NOTHING,0,0,0};
    if(!d->up_bytes)return h;
    if(!d->done_seq){h.what=(uint8_t)(d->frames_ok?PJ_PLH_GOOD:PJ_PLH_CHECKING);return h;}
    h.arg=d->done_result.arg;h.bits=d->done_result.bits;h.reason=d->rej_reason;
    if(d->frames_ok!=d->done_ok0){h.what=PJ_PLH_GOOD;return h;}
    switch(d->done_result.kind) {
    case PJ_PLA_EXACT:h.what=(uint8_t)(d->frames_bad!=d->done_bad0?PJ_PLH_REJECTED:PJ_PLH_CUT);break;
    case PJ_PLA_SHIFT:h.what=PJ_PLH_SHIFT;break;
    case PJ_PLA_INVERTED:h.what=PJ_PLH_INVERTED;break;
    case PJ_PLA_REVERSED:h.what=PJ_PLH_REVERSED;break;
    case PJ_PLA_SLIP:h.what=PJ_PLH_SLIP;break;
    case PJ_PLA_DUP:h.what=PJ_PLH_DUP;break;
    case PJ_PLA_ZEROS:h.what=PJ_PLH_ZEROS;break;
    case PJ_PLA_SHORT:h.what=PJ_PLH_SHORT;break;
    default:h.what=PJ_PLH_UNKNOWN;break;
    }
    return h;
}
/* Two short lines for the 160-pixel screen (the second may be empty). */
static inline void pj_pld_sentence(pj_pld_hint h,char *a,size_t an,char *b,size_t bn) {
    static const char *const reasons[PJ_PLR_COUNT]={"refused","too short","bad start","bad length","bad CRC","not a request"};
    const char *scrambled="Pocket heard, data scrambled";
    if(bn)b[0]=0;
    switch(h.what) {
    case PJ_PLH_NOTHING:snprintf(a,an,"No data from the Pocket yet.");break;
    case PJ_PLH_CHECKING:snprintf(a,an,"Pocket heard, checking...");break;
    case PJ_PLH_GOOD:snprintf(a,an,"Pocket heard, data correct.");break;
    case PJ_PLH_REJECTED:snprintf(a,an,"Pocket heard, data correct,");
        snprintf(b,bn,"check failed (%s).",reasons[h.reason<PJ_PLR_COUNT?h.reason:0]);break;
    case PJ_PLH_CUT:snprintf(a,an,"Pocket heard, start correct,");snprintf(b,bn,"message cut short.");break;
    case PJ_PLH_SHIFT:{int n=h.arg<0?-h.arg:h.arg;snprintf(a,an,"%s",scrambled);
        snprintf(b,bn,"(shifted %d bit%s %s).",n,n==1?"":"s",h.arg<0?"early":"late");break;}
    case PJ_PLH_INVERTED:snprintf(a,an,"%s",scrambled);snprintf(b,bn,"(every bit flipped).");break;
    case PJ_PLH_REVERSED:snprintf(a,an,"%s",scrambled);snprintf(b,bn,"(bit order reversed).");break;
    case PJ_PLH_SLIP:snprintf(a,an,"%s",scrambled);snprintf(b,bn,"(lost a bit near bit %d).",(int)h.arg);break;
    case PJ_PLH_DUP:snprintf(a,an,"%s",scrambled);snprintf(b,bn,"(extra bit near bit %d).",(int)h.arg);break;
    case PJ_PLH_ZEROS:snprintf(a,an,"Pocket line reads only zeros");snprintf(b,bn,"(check the cable).");break;
    case PJ_PLH_SHORT:snprintf(a,an,"Pocket heard, too few bytes");snprintf(b,bn,"to judge.");break;
    default:snprintf(a,an,"%s",scrambled);snprintf(b,bn,"(no pattern, %u bits right).",(unsigned)h.bits);break;
    }
}

/* ---- the console lines ---- */
/* Bytes as hex in groups of four bytes joined by '-': launcher_diag.h refuses a run of 12 hex characters or more
 * (it could be an identifier of the unit), so the groups are 8. */
static inline const char *pj_pld_hex(char *out,size_t size,const uint8_t *bytes,unsigned n) {
    static const char digits[]="0123456789abcdef";size_t at=0;
    if(!size)return out;
    for(unsigned i=0;i<n&&at+4u<size;i++) {
        if(i&&!(i&3u))out[at++]='-';
        out[at++]=digits[bytes[i]>>4];out[at++]=digits[bytes[i]&15u];
    }
    if(!n&&at+1u<size)out[at++]='-';
    out[at]=0;return out;
}
#define PJ_PLD_PERIODIC 7u         /* lines pj_pld_periodic can say: rx, rx bad, rx link, rx uart, rx cap, rx ring, rx first */
#define PJ_PLD_REPEAT_MS 30000u    /* an unchanged periodic line is said again this often; the linked line: this often */
#define PJ_PLD_RING_MS 15000u      /* the ring line: at most this often */
#define PJ_PLD_EVENT_MS 1000u      /* immediate lines: at most one of each kind a second */
#define PJ_PLD_SAME_MS 10000u      /* an immediate line that says the same as the last one: this often; a refused frame
                                      while the link works: at most this often */
/* What has been said. cap_*: the last capture said under either name ("plink cap", "plink rx cap"), so one capture is
 * never said twice within 30 s. begin_ms: when the link began (pj_pld_printer_init). */
typedef struct {
    uint32_t said_ms[PJ_PLD_PERIODIC],said_sum[PJ_PLD_PERIODIC];uint8_t said[PJ_PLD_PERIODIC];
    uint32_t cap_seq,cap_ms,rej_seq,rej_ms,linked_ms,begin_ms;uint8_t cap_said,rej_said,cap_kind,rej_reason,linked_said;int8_t cap_arg;
} pj_pld_printer;
static inline void pj_pld_printer_init(pj_pld_printer *p,uint32_t now){memset(p,0,sizeof(*p));p->begin_ms=now;}
static inline void pj_pld_cap_text(char *out,size_t size,const char *name,const pj_plink_diag *d) {
    char code[16],hex[2u*PJ_PLD_CAP+PJ_PLD_CAP/4u+4u];
    snprintf(out,size,"%s seq=%lu n=%u code=%s bits=%u at=%u hex=%s",name,(unsigned long)d->done_seq,(unsigned)d->done_n,
        pj_pld_code(d->done_result,code,sizeof(code)),(unsigned)d->done_result.bits,(unsigned)d->done_result.at,
        pj_pld_hex(hex,sizeof(hex),d->done,d->done_n));
}
static inline void pj_pld_cap_said(pj_pld_printer *p,const pj_plink_diag *d,uint32_t now) {
    p->cap_seq=d->done_seq;p->cap_said=1;p->cap_ms=now;p->cap_kind=d->done_result.kind;p->cap_arg=d->done_result.arg;
}
/* The 5 s lines, index 0..PJ_PLD_PERIODIC-1 in turn. `linked`: the link works (the screen says connected, syncing
 * or playing).
 * Not linked: each line is said when what it reports changed since it was last said; rx, link and uart also every
 * 30 s regardless; "bad" only when a frame was refused since; "cap" when the last capture's code differs from the
 * last capture said (as "plink cap" or here), and otherwise only when no capture has been said for 30 s (then the
 * last one again: a Pocket that spoke once stays in a short log); the ring when it changed, at most every 15 s;
 * "first" every 30 s from the link's 30th second on: how long ago the link began (age, seconds), how long after
 * that the first capture was complete (after, ms) and that capture. The launcher's log holds 16 KB, which its
 * one-a-second "stats" line alone fills in about two minutes: without this line a wait of ten minutes would leave
 * neither "plink begin" nor the first capture in it.
 * Linked: nothing of the above, never a byte in hex; index 0 says one short line of sums at most every 30 s
 * ("plink rx linked ..."), so a console at 115,200 baud is not kept busy while pictures or a film go over the cable.
 * Each line fits a diagnostics line with every counter at its largest (the host check proves it). 1: `out` holds a
 * line. */
static inline int pj_pld_periodic(pj_pld_printer *p,const pj_plink_diag *d,const pj_pld_uart *u,uint32_t now,unsigned index,int linked,char *out,size_t size) {
    static const pj_pld_uart none={0,0,0,0,0,0,0,0,0};
    uint32_t sum;if(!u)u=&none;
    if(index>=PJ_PLD_PERIODIC)return 0;
    if(linked) {
        if(index||(p->linked_said&&(uint32_t)(now-p->linked_ms)<PJ_PLD_REPEAT_MS))return 0;
        p->linked_said=1;p->linked_ms=now;
        snprintf(out,size,"plink rx linked bytes=%lu ok=%lu bad=%lu torn=%lu unfed=%lu faults=%lu uart=%lu",
            (unsigned long)d->up_bytes,(unsigned long)d->frames_ok,(unsigned long)d->frames_bad,(unsigned long)d->torn,
            (unsigned long)(d->up_bytes-d->fed),(unsigned long)(d->fault[0]+d->fault[1]+d->fault[2]+d->fault[3]),
            (unsigned long)(u->pkt_crc+u->pkt_other+u->pkt_len+u->ovf+u->full+u->parity+u->frame+u->brk+u->evq));
        return 1;
    }
    if(index==6u) {
        char code[16],hex[2u*PJ_PLD_CAP+PJ_PLD_CAP/4u+4u];unsigned n=d->first_n<PJ_PLD_CAP?d->first_n:PJ_PLD_CAP;
        if((uint32_t)(now-(p->said[6]?p->said_ms[6]:p->begin_ms))<PJ_PLD_REPEAT_MS)return 0;
        p->said[6]=1;p->said_ms[6]=now;
        snprintf(out,size,"plink rx first age=%lu after=%lu n=%u code=%s bits=%u at=%u hex=%s",
            (unsigned long)((uint32_t)(now-p->begin_ms)/1000u),(unsigned long)(n?(uint32_t)(d->first_ms-p->begin_ms):0u),n,
            pj_pld_code(d->first_result,code,sizeof(code)),(unsigned)d->first_result.bits,(unsigned)d->first_result.at,
            pj_pld_hex(hex,sizeof(hex),d->first,n));
        return 1;
    }
    switch(index) {
    case 0:sum=d->up_bytes+d->up_packets+d->flags+d->frames_ok+d->frames_bad+d->torn+d->dropped+d->fed;break;
    case 1:sum=d->frames_bad;if(!sum)return 0;break;
    case 2:sum=d->fault[0]+d->fault[1]+d->fault[2]+d->fault[3]+d->down_bytes+d->replies;break;
    case 3:sum=u->pkt_crc+u->pkt_other+u->pkt_len+u->ovf+u->full+u->parity+u->frame+u->brk+u->evq;break;
    case 4:sum=d->done_seq;if(!sum)return 0;break;
    default:sum=d->up_bytes;if(!sum)return 0;break;
    }
    uint32_t age=(uint32_t)(now-p->said_ms[index]);
    if(index==4u) {
        int same=p->cap_said&&p->cap_kind==d->done_result.kind&&p->cap_arg==d->done_result.arg;
        if(same&&(uint32_t)(now-p->cap_ms)<PJ_PLD_REPEAT_MS)return 0; /* nothing new to say */
        pj_pld_cap_said(p,d,now);
    }
    else if(p->said[index]) {
        if(index==5u){if(sum==p->said_sum[index]||age<PJ_PLD_RING_MS)return 0;}
        else if(index==1u){if(sum==p->said_sum[index])return 0;}
        else if(sum==p->said_sum[index]&&age<PJ_PLD_REPEAT_MS)return 0;
    }
    p->said[index]=1;p->said_ms[index]=now;p->said_sum[index]=sum;
    switch(index) {
    case 0:snprintf(out,size,"plink rx bytes=%lu pkts=%lu flags=%lu ok=%lu bad=%lu torn=%lu dropped=%lu unfed=%lu",
        (unsigned long)d->up_bytes,(unsigned long)d->up_packets,(unsigned long)d->flags,(unsigned long)d->frames_ok,(unsigned long)d->frames_bad,
        (unsigned long)d->torn,(unsigned long)d->dropped,(unsigned long)(d->up_bytes-d->fed));break;
    case 1:snprintf(out,size,"plink rx bad short=%lu magic=%lu len=%lu crc=%lu notreq=%lu",
        (unsigned long)d->reject[PJ_PLR_SHORT],(unsigned long)d->reject[PJ_PLR_MAGIC],(unsigned long)d->reject[PJ_PLR_LENGTH],
        (unsigned long)d->reject[PJ_PLR_CRC],(unsigned long)d->reject[PJ_PLR_NOTREQ]);break;
    case 2:snprintf(out,size,"plink rx link timeout=%lu overflow=%lu downdrop=%lu heartbeat=%lu down=%lu replies=%lu",
        (unsigned long)d->fault[0],(unsigned long)d->fault[1],(unsigned long)d->fault[2],(unsigned long)d->fault[3],
        (unsigned long)d->down_bytes,(unsigned long)d->replies);break;
    case 3:snprintf(out,size,"plink rx uart pkt-crc=%lu pkt-other=%lu pkt-len=%lu ovf=%lu full=%lu parity=%lu frame=%lu break=%lu evq=%lu",
        (unsigned long)u->pkt_crc,(unsigned long)u->pkt_other,(unsigned long)u->pkt_len,(unsigned long)u->ovf,(unsigned long)u->full,
        (unsigned long)u->parity,(unsigned long)u->frame,(unsigned long)u->brk,(unsigned long)u->evq);break;
    case 4:pj_pld_cap_text(out,size,"plink rx cap",d);break;
    default:{uint8_t ring[PJ_PLD_RING];char hex[2u*PJ_PLD_RING+PJ_PLD_RING/4u+4u];unsigned n=pj_pld_ring_copy(d,ring);
        snprintf(out,size,"plink rx ring n=%u hex=%s",n,pj_pld_hex(hex,sizeof(hex),ring,n));break;}
    }
    return 1;
}
/* The immediate lines, one per call at most: a capture just completed ("plink cap ..."), a whole frame was just
 * refused ("plink reject ..."). Not linked: at most one of each a second; one that says the same as the last (same
 * code, same reason) only every 10 s, so a Pocket that retries every 2 s does not flood the log. Linked: no capture
 * line at all (a capture then is the start of an ordinary request; it is not owed later either), and a refused
 * frame at most once in 10 s whatever its reason. 1: `out` holds a line. */
static inline int pj_pld_event(pj_pld_printer *p,const pj_plink_diag *d,uint32_t now,int linked,char *out,size_t size) {
    if(linked)p->cap_seq=d->done_seq;
    else if(d->done_seq!=p->cap_seq) {
        int same=p->cap_said&&p->cap_kind==d->done_result.kind&&p->cap_arg==d->done_result.arg;
        uint32_t age=(uint32_t)(now-p->cap_ms);
        if(!p->cap_said||(!same&&age>=PJ_PLD_EVENT_MS)||age>=PJ_PLD_SAME_MS) {
            pj_pld_cap_said(p,d,now);
            pj_pld_cap_text(out,size,"plink cap",d);return 1;
        }
        if(same)p->cap_seq=d->done_seq; /* nothing new to say */
    }
    if(d->rej_seq!=p->rej_seq) {
        int same=p->rej_said&&p->rej_reason==d->rej_reason;
        uint32_t age=(uint32_t)(now-p->rej_ms);
        if(!p->rej_said||(!linked&&!same&&age>=PJ_PLD_EVENT_MS)||age>=PJ_PLD_SAME_MS) {
            char hex[2u*PJ_PLD_HEAD+PJ_PLD_HEAD/4u+4u];
            p->rej_seq=d->rej_seq;p->rej_said=1;p->rej_ms=now;p->rej_reason=d->rej_reason;
            snprintf(out,size,"plink reject seq=%lu reason=%s size=%u head=%s",(unsigned long)d->rej_seq,
                pj_pld_reason_names[d->rej_reason<PJ_PLR_COUNT?d->rej_reason:0],(unsigned)d->rej_size,
                pj_pld_hex(hex,sizeof(hex),d->rej_head,d->rej_head_n));
            return 1;
        }
        if(same&&!linked)p->rej_seq=d->rej_seq;
    }
    return 0;
}
#endif
