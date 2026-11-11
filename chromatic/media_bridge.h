/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Bounded MCU forwarding core. Board callbacks never access cartridge/flash.
 * TLS must be pinned/paired before init. Default/failed board mode is GAME.
 */
#ifndef PJ_CHROMATIC_MEDIA_BRIDGE_H
#define PJ_CHROMATIC_MEDIA_BRIDGE_H
#include "../shared/bridge_rpc.h"
#include "../shared/link_framing.h"
#define PJ_MEDIA_KEY 0x504a4c4bu
#define PJ_MEDIA_CREDIT_WINDOW 1024u
enum {PJ_MEDIA_ARMING=1,PJ_MEDIA_IDLE,PJ_MEDIA_NET_WRITE,PJ_MEDIA_NET_READ,PJ_MEDIA_LINK_WRITE,PJ_MEDIA_FAILED};
typedef struct {
    void *context;
    int (*net_write)(void *,const uint8_t *,size_t);
    int (*net_read)(void *,uint8_t *,size_t);
    void (*net_close)(void *);
    int (*enter_media)(void *,uint32_t);
    void (*game_mode)(void *);
    int (*heartbeat)(void *);
    /* 1 fresh snapshot, 0 no snapshot, -1 error. mode must be 2 once armed. */
    int (*status)(void *,uint32_t *consumed,unsigned *mode,unsigned *faults);
    /* Even2..1024 bytes, addr0x504a0000, cmd0x400|(size-1), synchronous
     * completion before returning1. 0 means not submitted, -1 ambiguous/fault. */
    int (*qspi_write)(void *,const uint8_t *,size_t);
} pj_media_ops;
typedef struct {
    pj_media_ops ops;pj_wire_rx upstream;pj_wire_tx downstream;
    uint8_t request[1040],incoming[1040],response[PJ_RPC_HEADER+PJ_RPC_LIMIT],pending[1024];
    size_t request_size,request_at,response_size,response_at,pending_size;
    uint32_t sent,consumed,last_progress_ms,last_heartbeat_ms,last_status_ms,armed_ms;
    unsigned state;int cached;
} pj_media_bridge;
static inline int pj_media_fail(pj_media_bridge *b) {
    if(b->state!=PJ_MEDIA_FAILED){b->state=PJ_MEDIA_FAILED;b->ops.game_mode(b->ops.context);b->ops.net_close(b->ops.context);}
    return -1;
}
static inline int pj_media_init(pj_media_bridge *b,pj_media_ops ops,uint32_t now) {
    if(!b||!ops.net_read||!ops.net_write||!ops.net_close||!ops.enter_media||
       !ops.game_mode||!ops.heartbeat||!ops.status||!ops.qspi_write)return 0;
    memset(b,0,sizeof(*b));b->ops=ops;b->state=PJ_MEDIA_ARMING;
    b->last_progress_ms=b->last_heartbeat_ms=b->last_status_ms=b->armed_ms=now;
    pj_wire_rx_init(&b->upstream,b->incoming,sizeof(b->incoming));
    if(ops.enter_media(ops.context,PJ_MEDIA_KEY)!=1){pj_media_fail(b);return 0;}
    return 1;
}
static inline int pj_media_request_valid(const uint8_t *p,size_t size) {
    if(size<16||size>1040||memcmp(p,"PJB1",4)||p[4]<1||p[4]>9||p[5]||
       !pj_av_u32(p+8)||size!=16+pj_av_u16(p+6))return 0;
    uint32_t crc=pj_av_crc_part(0xffffffffu,p,12);
    return (pj_av_crc_part(crc,p+16,size-16)^0xffffffffu)==pj_av_u32(p+12);
}
/* Only reserved media UART payload bytes enter here; stock RX parsing remains
 * separate. One outstanding request; exact in-flight retries are harmless. */
static inline int pj_media_pocket_bytes(pj_media_bridge *b,const uint8_t *p,size_t size,uint32_t now) {
    if(b->state==PJ_MEDIA_FAILED)return -1;
    if(b->state==PJ_MEDIA_ARMING)return 0;
    for(size_t i=0;i<size;i++) {
        size_t n=0;int got=pj_wire_rx_byte(&b->upstream,p[i],&n);
        if(got<0)return pj_media_fail(b);
        if(!got)continue;
        if(!pj_media_request_valid(b->incoming,n))return pj_media_fail(b);
        int same=n==b->request_size&&!memcmp(b->incoming,b->request,n);
        if(b->state!=PJ_MEDIA_IDLE) {
            if(!same)return pj_media_fail(b);
            continue;
        }
        if(same&&b->cached) {
            pj_wire_tx_init(&b->downstream,b->response,b->response_size);
            b->state=PJ_MEDIA_LINK_WRITE;
        } else {
            memcpy(b->request,b->incoming,n);b->request_size=n;b->request_at=0;
            b->response_at=0;b->response_size=16;b->cached=0;b->state=PJ_MEDIA_NET_WRITE;
        }
        b->last_progress_ms=now;
    }
    return 1;
}
static inline int pj_media_step(pj_media_bridge *b,uint32_t now) {
    if(b->state==PJ_MEDIA_FAILED)return -1;
    uint32_t consumed=0;unsigned mode=0,faults=0;
    int status=b->ops.status(b->ops.context,&consumed,&mode,&faults);
    if(status<0||faults)return pj_media_fail(b);
    if(b->state==PJ_MEDIA_ARMING) {
        if(status==1&&mode==2) {
            if(consumed)return pj_media_fail(b);
            b->state=PJ_MEDIA_IDLE;b->last_progress_ms=b->last_status_ms=now;
        } else if((uint32_t)(now-b->armed_ms)>1000)return pj_media_fail(b);
        return 0;
    }
    if(status==1) {
        if(mode!=2||(uint32_t)(consumed-b->consumed)>(uint32_t)(b->sent-b->consumed))return pj_media_fail(b);
        b->consumed=consumed;b->last_status_ms=now;
    }
    if((uint32_t)(now-b->last_status_ms)>1000)return pj_media_fail(b);
    if((uint32_t)(now-b->last_heartbeat_ms)>=250) {
        int result=b->ops.heartbeat(b->ops.context);
        if(result<0)return pj_media_fail(b);
        if(result==1)b->last_heartbeat_ms=now;
        if((uint32_t)(now-b->last_heartbeat_ms)>1000)return pj_media_fail(b);
    }
    if(b->state!=PJ_MEDIA_IDLE&&(uint32_t)(now-b->last_progress_ms)>20000)return pj_media_fail(b);
    if(b->state==PJ_MEDIA_IDLE) {
        uint8_t byte;int n=b->ops.net_read(b->ops.context,&byte,1);
        if(n!=0)return pj_media_fail(b); /* detects EOF/unexpected unsolicited bytes */
    } else if(b->state==PJ_MEDIA_NET_WRITE) {
        size_t left=b->request_size-b->request_at;
        int n=b->ops.net_write(b->ops.context,b->request+b->request_at,left);
        if(n<0||(size_t)n>left)return pj_media_fail(b);
        if(n){b->request_at+=(size_t)n;b->last_progress_ms=now;}
        if(b->request_at==b->request_size)b->state=PJ_MEDIA_NET_READ;
    } else if(b->state==PJ_MEDIA_NET_READ) {
        size_t left=b->response_size-b->response_at;if(left>4096)left=4096;
        int n=b->ops.net_read(b->ops.context,b->response+b->response_at,left);
        if(n<0||(size_t)n>left)return pj_media_fail(b);
        if(n){b->response_at+=(size_t)n;b->last_progress_ms=now;}
        if(b->response_at==16&&b->response_size==16) {
            if(memcmp(b->response,"PJB1",4)||pj_av_u16(b->response+6)>PJ_RPC_LIMIT)return pj_media_fail(b);
            b->response_size=16+pj_av_u16(b->response+6);
        }
        if(b->response_at==b->response_size) {
            if(!pj_rpc_validate(b->response,b->response_size)||b->response[4]!=b->request[4]||
               pj_av_u32(b->response+8)!=pj_av_u32(b->request+8))return pj_media_fail(b);
            b->cached=1;pj_wire_tx_init(&b->downstream,b->response,b->response_size);b->state=PJ_MEDIA_LINK_WRITE;
        }
    } else if(b->state==PJ_MEDIA_LINK_WRITE) {
        uint32_t inflight=b->sent-b->consumed;
        if(inflight>PJ_MEDIA_CREDIT_WINDOW)return pj_media_fail(b);
        unsigned room=PJ_MEDIA_CREDIT_WINDOW-inflight;
        if(!b->pending_size&&room>=2) {
            size_t limit=room&~1u;if(limit>sizeof(b->pending))limit=sizeof(b->pending);
            while(b->pending_size<limit) {
                uint8_t byte;if(!pj_wire_tx_next(&b->downstream,&byte))break;
                b->pending[b->pending_size++]=byte;
            }
            if(b->pending_size&1)b->pending[b->pending_size++]=PJ_WIRE_IDLE;
        }
        if(b->pending_size) {
            if(b->pending_size>room)return pj_media_fail(b);
            int result=b->ops.qspi_write(b->ops.context,b->pending,b->pending_size);
            if(result<0)return pj_media_fail(b);
            if(result==1){b->sent+=(uint32_t)b->pending_size;b->pending_size=0;b->last_progress_ms=now;}
        }
        if(!b->pending_size&&b->downstream.phase==2)b->state=PJ_MEDIA_IDLE;
    }
    return 0;
}
#endif
