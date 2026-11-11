/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * RPC chunks may split PJAV headers/payloads or contain several whole packets. */
#ifndef PJ_STREAM_READER_H
#define PJ_STREAM_READER_H
#include "../shared/av_stream.h"
typedef struct {
    uint8_t packet[PJ_AV_HEADER+PJ_AV_LIMIT];size_t used,target;
    uint32_t session;int ended,failed;
} pj_stream_reader;
static inline void pj_stream_reader_init(pj_stream_reader *r,uint32_t session) {
    r->used=0;r->target=PJ_AV_HEADER;r->session=session;r->ended=r->failed=0;
}
/* Consume as many bytes as accepted. Complete blocked packet stays owned here;
 * caller retains any unconsumed tail and retries it after servicing playback. */
static inline int pj_stream_reader_feed(pj_stream_reader *r,const uint8_t *data,size_t size,
                  size_t *consumed,int (*push)(const uint8_t *,size_t)) {
    *consumed=0;if(r->failed)return -1;
    for(;;) {
        if(r->used==PJ_AV_HEADER&&r->target==PJ_AV_HEADER) {
            if(!pj_av_header(r->packet)||pj_av_u32(r->packet+8)!=r->session)goto fail;
            r->target=PJ_AV_HEADER+pj_av_u32(r->packet+24);
        }
        if(r->used==r->target) {
            if(r->ended||!pj_av_validate(r->packet,r->packet+PJ_AV_HEADER,r->used-PJ_AV_HEADER))goto fail;
            int result=push(r->packet,r->used);
            if(result<0)goto fail;
            if(!result)return 0;
            r->ended=r->packet[5]==PJ_AV_END;r->used=0;r->target=PJ_AV_HEADER;
        }
        if(*consumed==size)return 1;
        if(r->ended)goto fail;
        size_t n=r->target-r->used;if(n>size-*consumed)n=size-*consumed;
        memcpy(r->packet+r->used,data+*consumed,n);r->used+=n;*consumed+=n;
    }
fail:r->failed=1;return -1;
}
static inline int pj_stream_reader_eof(const pj_stream_reader *r) {
    return !r->failed&&r->ended&&!r->used;
}
#endif
