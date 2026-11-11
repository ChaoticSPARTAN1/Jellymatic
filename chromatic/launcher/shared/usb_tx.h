/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.3.2: the USB (UART0) transmit ring. Portable C: the launcher and the host model
 * (check_remote.c serve-usb-paced) run this same code.
 *
 * Why: before 1.3.2 the main loop copied CHR1 output into the 128-byte UART0 FIFO once per pass.
 * With the Jellyfin app open a pass takes 45-60 ms, so USB carried about 2.5 KB/s instead of the
 * line's 11.5 KB/s: a 46 KB screen took about 18 s, Remote Pilot gave up after 9 s and the session
 * was lost (docs/Design-notes.md, "USB pace with Jellyfin open").
 *
 * Now the main loop moves owed bytes into this ring (pj_rmt_out stays in the main loop, so the CHR1
 * handler keeps one owner), and a 5 ms esp_timer callback moves them from the ring into the FIFO.
 * One producer (main task), one consumer (the drain): head is written only by the producer, tail
 * only by the consumer. The consumer advances tail only after the bytes are in the hardware FIFO,
 * so "ring empty" means every byte has reached the FIFO in order, and a printf after that cannot
 * land inside CHR1 output. Still no UART0 TX driver and no VFS change: printf writes the FIFO
 * itself, as before, and the boot markers are unchanged.
 */
#ifndef PJ_USB_TX_H
#define PJ_USB_TX_H
#include <stddef.h>
#include <stdint.h>

#define PJ_UTX_RING 1024u      /* power of two; covers an 89 ms pass at 11.5 KB/s (Jellyfin: 45-60 ms), 1 KB of DRAM */
#define PJ_UTX_DRAIN_MS 5u     /* the 128-byte FIFO empties in 11 ms at 115200 baud */
typedef struct {
    uint8_t buf[PJ_UTX_RING];
    uint32_t head,tail;        /* free-running counts; head: producer only, tail: consumer only */
} pj_utx;

static inline uint32_t pj_utx_load(const uint32_t *p){return __atomic_load_n(p,__ATOMIC_ACQUIRE);}
static inline void pj_utx_store(uint32_t *p,uint32_t v){__atomic_store_n(p,v,__ATOMIC_RELEASE);}
/* Producer: free bytes. */
static inline size_t pj_utx_space(pj_utx *r){return PJ_UTX_RING-(pj_utx_load(&r->head)-pj_utx_load(&r->tail));}
/* Producer: copies up to size bytes in; returns how many. */
static inline size_t pj_utx_put(pj_utx *r,const uint8_t *data,size_t size) {
    uint32_t head=r->head;size_t room=PJ_UTX_RING-(head-pj_utx_load(&r->tail));
    if(size>room)size=room;
    for(size_t i=0;i<size;i++)r->buf[(head+i)&(PJ_UTX_RING-1u)]=data[i];
    pj_utx_store(&r->head,head+(uint32_t)size);
    return size;
}
/* Consumer: the next contiguous run of queued bytes (at most max); commit after writing them. */
static inline size_t pj_utx_peek(pj_utx *r,const uint8_t **data,size_t max) {
    uint32_t tail=r->tail;size_t queued=pj_utx_load(&r->head)-tail;
    size_t at=tail&(PJ_UTX_RING-1u),run=PJ_UTX_RING-at;
    if(run>queued)run=queued;
    if(run>max)run=max;
    *data=r->buf+at;return run;
}
static inline void pj_utx_commit(pj_utx *r,size_t n){pj_utx_store(&r->tail,r->tail+(uint32_t)n);}
/* Either side: 1 when every queued byte has reached the FIFO. */
static inline int pj_utx_idle(pj_utx *r){return pj_utx_load(&r->head)==pj_utx_load(&r->tail);}
/* Consumer, every PJ_UTX_DRAIN_MS: as many bytes as the FIFO has room for (two runs at the wrap). */
typedef uint32_t (*pj_utx_room_fn)(void);
typedef void (*pj_utx_write_fn)(const uint8_t *data,uint32_t size);
static inline size_t pj_utx_drain(pj_utx *r,pj_utx_room_fn room,pj_utx_write_fn write) {
    size_t sent=0;
    for(int i=0;i<2;i++) {
        uint32_t free_bytes=room();const uint8_t *p;
        size_t n=pj_utx_peek(r,&p,free_bytes);
        if(!n)break;
        write(p,(uint32_t)n);pj_utx_commit(r,n);sent+=n;
    }
    return sent;
}
#endif
