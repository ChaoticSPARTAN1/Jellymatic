/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PJB1 framing shared with the PC gateway. No URLs or credentials. */
#ifndef PJ_BRIDGE_RPC_H
#define PJ_BRIDGE_RPC_H
#include "av_stream.h"
#define PJ_RPC_HEADER 16u
#define PJ_RPC_LIMIT 32768u
#define PJ_RPC_HELLO 1u
#define PJ_RPC_HOME 2u
#define PJ_RPC_BROWSE 3u
#define PJ_RPC_DETAILS 4u
#define PJ_RPC_ART 5u
#define PJ_RPC_START 7u
#define PJ_RPC_READ 8u
#define PJ_RPC_STOP 9u
/* Artwork contract, identical for SD and tethered sources. RGB565 little-endian.
 * Mirrored by ART_KINDS in scripts/jellyfin_artwork.py. */
#define PJ_ART_THUMB 0u
#define PJ_ART_POSTER 1u
#define PJ_ART_BACKDROP 2u
#define PJ_ART_THUMB_W 96u
#define PJ_ART_THUMB_H 54u
#define PJ_ART_POSTER_W 64u
#define PJ_ART_POSTER_H 96u
#define PJ_ART_BACKDROP_W 320u
#define PJ_ART_BACKDROP_H 120u
static inline unsigned pj_art_width(unsigned kind) {
    return kind==PJ_ART_THUMB?PJ_ART_THUMB_W:kind==PJ_ART_POSTER?PJ_ART_POSTER_W:PJ_ART_BACKDROP_W;
}
static inline unsigned pj_art_height(unsigned kind) {
    return kind==PJ_ART_THUMB?PJ_ART_THUMB_H:kind==PJ_ART_POSTER?PJ_ART_POSTER_H:PJ_ART_BACKDROP_H;
}
static inline void pj_put32(uint8_t *p,uint32_t v) {
    for(unsigned i=0;i<4;i++)p[i]=(uint8_t)(v>>(i*8));
}
static inline size_t pj_rpc_encode(uint8_t *out,size_t capacity,unsigned command,
                                  uint32_t sequence,const char *json,size_t n) {
    if(!out||!sequence||command<1||command>9||n>PJ_RPC_LIMIT||capacity<PJ_RPC_HEADER+n)return 0;
    memcpy(out,"PJB1",4);out[4]=(uint8_t)command;out[5]=0;
    out[6]=(uint8_t)n;out[7]=(uint8_t)(n>>8);pj_put32(out+8,sequence);
    memcpy(out+16,json,n);
    uint32_t crc=pj_av_crc_part(0xffffffffu,out,12);
    pj_put32(out+12,pj_av_crc_part(crc,out+16,n)^0xffffffffu);
    return PJ_RPC_HEADER+n;
}
static inline int pj_rpc_validate(const uint8_t *p,size_t n) {
    if(n<PJ_RPC_HEADER||memcmp(p,"PJB1",4)||p[4]<1||p[4]>9||
       (p[5]!=1&&p[5]!=3)||!pj_av_u32(p+8)||pj_av_u16(p+6)>PJ_RPC_LIMIT||
       n!=PJ_RPC_HEADER+pj_av_u16(p+6))return 0;
    uint32_t crc=pj_av_crc_part(0xffffffffu,p,12);
    return (pj_av_crc_part(crc,p+16,n-16)^0xffffffffu)==pj_av_u32(p+12);
}
#endif
