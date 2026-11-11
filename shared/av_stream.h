/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md */
#ifndef PJ_AV_STREAM_H
#define PJ_AV_STREAM_H
/* Portable little-endian AV envelope for both device builds. No hardware IO.
 * CRC is IEEE CRC32 over header bytes 0..27 followed by the payload. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#define PJ_AV_HEADER 32u
#define PJ_AV_LIMIT 32768u
#define PJ_AV_CONFIG 1u
#define PJ_AV_VIDEO 2u
#define PJ_AV_AUDIO 3u
#define PJ_AV_END 4u
#define PJ_AV_KEYFRAME 1u
#define PJ_AV_DISCONTINUITY 2u
static inline unsigned pj_av_u16(const uint8_t *p) {return (unsigned)p[0]|((unsigned)p[1]<<8);}
static inline uint32_t pj_av_u32(const uint8_t *p) {
    return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
}
static inline uint64_t pj_av_u64(const uint8_t *p) {
    return pj_av_u32(p)|((uint64_t)pj_av_u32(p+4)<<32);
}
/* Byte-table CRC: every media byte is checked on a 90 MHz CPU that also moves
 * every decoded pixel, so the bit-at-a-time form would cost real frame rate. */
static inline uint32_t pj_av_crc_part(uint32_t crc,const uint8_t *data,size_t n) {
    static uint32_t table[256];
    if(!table[1])for(uint32_t i=0;i<256;i++) {
        uint32_t c=i;
        for(unsigned b=0;b<8;b++)c=(c>>1)^((0u-(c&1u))&0xedb88320u);
        table[i]=c;
    }
    for(size_t i=0;i<n;i++)crc=table[(crc^data[i])&255u]^(crc>>8);
    return crc;
}
static inline int pj_av_header(const uint8_t *h) {
    uint32_t size=pj_av_u32(h+24);
    if(memcmp(h,"PJAV",4)||h[4]!=1||h[5]<1||h[5]>4||h[6]>3||h[7]||
       !pj_av_u32(h+8)||size>PJ_AV_LIMIT) return 0;
    if(h[5]==PJ_AV_CONFIG && (size!=20||pj_av_u32(h+12)!=0||pj_av_u64(h+16)!=0)) return 0;
    if(h[5]==PJ_AV_END ? size!=0 : size==0) return 0;
    return 1;
}
static inline int pj_av_validate(const uint8_t *h,const uint8_t *payload,size_t size) {
    if(!pj_av_header(h)||pj_av_u32(h+24)!=size) return 0;
    uint32_t crc=pj_av_crc_part(0xffffffffu,h,28);
    crc=pj_av_crc_part(crc,payload,size)^0xffffffffu;
    return crc==pj_av_u32(h+28);
}
typedef struct {
    uint32_t session,next_sequence,lost_packets,rate;
    uint64_t video_pts,audio_pts;
    unsigned width,height,fps,video_codec,audio_codec,block;
    uint8_t configured,closed,need_keyframe;
} pj_av_receiver;
/* 1: accepted, 0: stale/waiting for keyframe, -1: invalid (disconnect). */
static inline int pj_av_receive(pj_av_receiver *r,const uint8_t *h,const uint8_t *p,size_t size) {
    if(!pj_av_validate(h,p,size))return -1;
    unsigned kind=h[5],flags=h[6];uint32_t session=pj_av_u32(h+8),seq=pj_av_u32(h+12);
    uint64_t pts=pj_av_u64(h+16);
    if(kind==PJ_AV_CONFIG) {
        unsigned w=pj_av_u16(p),height=pj_av_u16(p+2),fps=pj_av_u16(p+4);
        unsigned video=pj_av_u16(p+6),audio=pj_av_u16(p+12),block=pj_av_u16(p+16);
        uint32_t rate=pj_av_u32(p+8);
        if((w!=128&&w!=160&&w!=256)||(height!=96&&height!=128&&height!=144)||
           (fps!=24&&fps!=25&&fps!=30&&fps!=60)||(video!=1&&video!=2)||
           (rate!=16000&&rate!=22050&&rate!=24000&&rate!=48000)||(audio!=1&&audio!=2)||
           pj_av_u16(p+14)!=1||pj_av_u16(p+18)||
           (audio==1?(block!=128&&block!=256&&block!=512&&block!=1024):block!=0))return -1;
        memset(r,0,sizeof(*r));r->session=session;r->next_sequence=1;
        r->width=w;r->height=height;r->fps=fps;r->rate=rate;r->video_codec=video;
        r->audio_codec=audio;r->block=block;r->configured=1;r->need_keyframe=1;
        return 1;
    }
    if(!r->configured||r->closed||r->session!=session)return 0;
    if(seq!=r->next_sequence) {
        uint32_t gap=seq-r->next_sequence;
        if(gap>=0x80000000u)return 0;
        r->lost_packets+=gap;r->need_keyframe=1;
    }
    r->next_sequence=seq+1;
    if(flags&PJ_AV_DISCONTINUITY){r->need_keyframe=1;r->video_pts=0;r->audio_pts=0;}
    if(kind==PJ_AV_END){r->closed=1;return 1;}
    if(kind==PJ_AV_VIDEO) {
        if(pts<r->video_pts)return -1;
        r->video_pts=pts;
        if(flags&PJ_AV_KEYFRAME)r->need_keyframe=0;
        return r->need_keyframe?0:1;
    }
    if(pts<r->audio_pts)return -1;
    r->audio_pts=pts;
    if(r->audio_codec==1&&(size!=r->block||p[2]>88||p[3]))return -1;
    return 1;
}
#endif
