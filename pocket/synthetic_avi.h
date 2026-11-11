/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Test-only: a synthetic Jellyfin-shaped MJPEG + IMA ADPCM AVI stream, laid out as FFmpeg's
 * piped AVI muxer writes it (RIFF/movi sizes unknown, audio blocks before each later picture),
 * for the PC simulations of the tether (pocket/check_tether.c, chromatic/launcher/
 * check_pocket_link.c). The "JPEG" frames are FF D8 ... FF D9 around deterministic bytes of a
 * chosen size (the Pocket's PJAV path checks CRCs and geometry, not picture content; the
 * hardware decoder path is proven elsewhere). IMA blocks are valid (index <= 88) so the Pocket's
 * decoder runs on them. No files, no network. */
#ifndef PJ_SYNTHETIC_AVI_H
#define PJ_SYNTHETIC_AVI_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <stdlib.h>
typedef struct {
    uint8_t *data;size_t size,cap;
    unsigned width,height,fps_rate,fps_scale,block,frames,audio_blocks,frame_bytes;
} pj_syn_avi;
static inline void pj_syn_put(pj_syn_avi *a,const void *p,size_t n) {
    if(a->size+n>a->cap){a->cap=(a->size+n)*2+65536;a->data=realloc(a->data,a->cap);}
    memcpy(a->data+a->size,p,n);a->size+=n;
}
static inline void pj_syn_u32(pj_syn_avi *a,uint32_t v){uint8_t b[4]={(uint8_t)v,(uint8_t)(v>>8),(uint8_t)(v>>16),(uint8_t)(v>>24)};pj_syn_put(a,b,4);}
static inline void pj_syn_u16(pj_syn_avi *a,unsigned v){uint8_t b[2]={(uint8_t)v,(uint8_t)(v>>8)};pj_syn_put(a,b,2);}
static inline void pj_syn_chunk(pj_syn_avi *a,const char *id,const void *p,uint32_t n) {
    pj_syn_put(a,id,4);pj_syn_u32(a,n);pj_syn_put(a,p,n);if(n&1){uint8_t z=0;pj_syn_put(a,&z,1);}
}
/* Launcher 1.3.9 (test-only): a real baseline JPEG of width x height (4:2:0, Y 2x2) with pseudo-random coefficients
 * (DC walks, a few AC values, a zero run of 16 now and then), coded with Huffman tables that are NOT JPEG Annex K
 * (every DC category 4 bits, every AC symbol 8 bits, symbols in reverse order), as the owner's server writes its own
 * tables into every picture. Any baseline decoder reads it. Returns its length (0 when `cap` is too small). */
typedef struct {uint8_t *p,*end;uint32_t acc;int bits,over;} pj_syn_bits;
static inline void pj_syn_bit_byte(pj_syn_bits *w,uint8_t b) {
    if(w->p+2>w->end){w->over=1;return;}
    *w->p++=b;if(b==0xff)*w->p++=0;
}
static inline void pj_syn_bit_put(pj_syn_bits *w,uint32_t v,unsigned n) {
    for(unsigned i=n;i-->0;){w->acc=(w->acc<<1)|((v>>i)&1u);if(++w->bits==8){pj_syn_bit_byte(w,(uint8_t)w->acc);w->acc=0;w->bits=0;}}
}
/* eob: a block ends after each coefficient with chance 1/eob (5 by default: about 1.8 KB at 128x96; more, bigger) */
static inline size_t pj_syn_jpeg_eob(uint8_t *out,size_t cap,unsigned width,unsigned height,uint32_t seed,unsigned eob);
static inline size_t pj_syn_jpeg(uint8_t *out,size_t cap,unsigned width,unsigned height,uint32_t seed) {
    return pj_syn_jpeg_eob(out,cap,width,height,seed,5u);
}
static inline size_t pj_syn_jpeg_eob(uint8_t *out,size_t cap,unsigned width,unsigned height,uint32_t seed,unsigned eob) {
    static const uint8_t head[]={0xff,0xd8,0xff,0xe0,0,16,'J','F','I','F',0,1,1,0,0,1,0,1,0,0};
    uint8_t ac_sym[162];unsigned n=0;
    for(unsigned run=0;run<16;run++)for(unsigned size=1;size<=10;size++)ac_sym[n++]=(uint8_t)(run<<4|size);
    ac_sym[n++]=0x00;ac_sym[n++]=0xf0;
    if(cap<2048u)return 0;
    uint8_t *o=out;memcpy(o,head,sizeof(head));o+=sizeof(head);
    /* DQT: luminance DC step 6, chrominance 9, AC steps 1..16 */
    *o++=0xff;*o++=0xdb;*o++=0;*o++=132;
    for(unsigned t=0;t<2;t++){*o++=(uint8_t)t;for(unsigned i=0;i<64;i++)*o++=(uint8_t)(i?1u+(i+t)%16u:(t?9u:6u));}
    *o++=0xff;*o++=0xc0;*o++=0;*o++=17;*o++=8;*o++=(uint8_t)(height>>8);*o++=(uint8_t)height;*o++=(uint8_t)(width>>8);*o++=(uint8_t)width;
    *o++=3;*o++=1;*o++=0x22;*o++=0;*o++=2;*o++=0x11;*o++=1;*o++=3;*o++=0x11;*o++=1;
    /* DHT: the four tables, symbols in reverse order */
    *o++=0xff;*o++=0xc4;{unsigned length=2u+4u*17u+12u*2u+162u*2u;*o++=(uint8_t)(length>>8);*o++=(uint8_t)length;}
    for(unsigned k=0;k<4;k++) {
        *o++=(uint8_t)((k>>1)<<4|(k&1u));
        for(unsigned len=1;len<=16;len++)*o++=(uint8_t)(k<2?(len==4?12:0):(len==8?162:0));
        if(k<2)for(unsigned i=0;i<12;i++)*o++=(uint8_t)(11u-i);
        else for(unsigned i=0;i<162;i++)*o++=ac_sym[161u-i];
    }
    static const uint8_t sos[]={0xff,0xda,0,12,3,1,0x00,2,0x11,3,0x11,0,63,0};memcpy(o,sos,sizeof(sos));o+=sizeof(sos);
    /* codes: DC category c is 11-c (4 bits); AC symbol ac_sym[i] is 161-i (8 bits) */
    pj_syn_bits w={o,out+cap-2,0,0,0};int pred[3]={0,0,0};
    for(unsigned my=0;my<(height+15u)/16u;my++)for(unsigned mx=0;mx<width/16u;mx++)for(unsigned c=0;c<3;c++)for(unsigned b=0;b<(c?1u:4u);b++) {
        seed=seed*1103515245u+12345u;int dc=pred[c]+(int)((seed>>16)%41u)-20;if(dc>100)dc=100;if(dc<-100)dc=-100;
        int diff=dc-pred[c];pred[c]=dc;unsigned mag=(unsigned)(diff<0?-diff:diff),cat=0;while(mag>>cat)cat++;
        pj_syn_bit_put(&w,11u-cat,4);if(cat)pj_syn_bit_put(&w,(uint32_t)(diff<0?diff+(int)(1u<<cat)-1:diff),cat);
        for(unsigned k=1;k<64;) {
            seed=seed*1103515245u+12345u;unsigned r=(seed>>16)&0xffffu;
            if(r%eob==0){pj_syn_bit_put(&w,161u-160u,8);break;} /* EOB: ac_sym[160]==0x00 */
            /* mostly short runs and small values, as in real pictures (whose Annex K codes are short: long all-ones codes
             * would fill the result with FF bytes, which the link escapes, and understate what the link carries) */
            unsigned rr=(r>>2)%16u,ss=(r>>6)%16u,run=rr<10u?0u:rr<14u?1u:rr-12u,size=ss<9u?1u:ss<14u?2u:ss<15u?3u:4u;
            if(r%23u==0&&k+16u<=63u){pj_syn_bit_put(&w,161u-161u,8);k+=16u;continue;} /* ZRL: ac_sym[161]==0xf0 */
            if(k+run>63u){pj_syn_bit_put(&w,161u-160u,8);break;}
            k+=run;
            unsigned index=run*10u+size-1u;pj_syn_bit_put(&w,161u-index,8);
            uint32_t v=(r>>5)&((1u<<size)-1u);v|=1u<<(size-1u);if(r&0x4000u)v=(uint32_t)(-(int32_t)v+(int32_t)(1u<<size)-1); /* negative half */
            pj_syn_bit_put(&w,v,size);k++;
        }
    }
    if(w.bits)pj_syn_bit_put(&w,0x7fu,(unsigned)(8-w.bits));
    if(w.over||w.p+2>out+cap)return 0;
    *w.p++=0xff;*w.p++=0xd9;
    return (size_t)(w.p-out);
}
/* Builds `seconds` of stream: `frame_bytes` per picture at fps_rate/fps_scale, 16 kHz mono IMA
 * blocks of `block` bytes. Each picture is preceded by the audio that falls before its time.
 * jpeg (1.3.9): each picture a real JPEG of width x height (pj_syn_jpeg, a new seed each), frame_bytes unused. */
static inline void pj_syn_avi_build2(pj_syn_avi *a,unsigned width,unsigned height,unsigned fps_rate,unsigned fps_scale,
                                     unsigned block,unsigned frame_bytes,unsigned seconds,int jpeg_frames);
static inline void pj_syn_avi_build(pj_syn_avi *a,unsigned width,unsigned height,unsigned fps_rate,unsigned fps_scale,
                                    unsigned block,unsigned frame_bytes,unsigned seconds) {
    pj_syn_avi_build2(a,width,height,fps_rate,fps_scale,block,frame_bytes,seconds,0);
}
static inline void pj_syn_avi_build3(pj_syn_avi *a,unsigned width,unsigned height,unsigned fps_rate,unsigned fps_scale,
                                     unsigned block,unsigned frame_bytes,unsigned seconds,int jpeg_frames,
                                     unsigned big_bytes,unsigned small_frames,unsigned big_frames);
static inline void pj_syn_avi_build2(pj_syn_avi *a,unsigned width,unsigned height,unsigned fps_rate,unsigned fps_scale,
                                     unsigned block,unsigned frame_bytes,unsigned seconds,int jpeg_frames) {
    pj_syn_avi_build3(a,width,height,fps_rate,fps_scale,block,frame_bytes,seconds,jpeg_frames,frame_bytes,1u,0u);
}
/* (Core Settings buffer model, check_tether_app.c -DPJ_MODEL_PLAYER): busy scenes. Runs of small_frames
 * pictures of frame_bytes, then big_frames of big_bytes, over and over (real JPEGs: the sparsest at least that big). */
static inline void pj_syn_avi_build3(pj_syn_avi *a,unsigned width,unsigned height,unsigned fps_rate,unsigned fps_scale,
                                     unsigned block,unsigned frame_bytes,unsigned seconds,int jpeg_frames,
                                     unsigned big_bytes,unsigned small_frames,unsigned big_frames) {
    memset(a,0,sizeof(*a));a->width=width;a->height=height;a->fps_rate=fps_rate;a->fps_scale=fps_scale;a->block=block;a->frame_bytes=frame_bytes;
    const unsigned small_bytes=frame_bytes;
    pj_syn_put(a,"RIFF",4);pj_syn_u32(a,0xffffffffu);pj_syn_put(a,"AVI ",4);
    /* LIST hdrl: avih, strl(vids), strl(auds) */
    pj_syn_put(a,"LIST",4);size_t hdrl_size_at=a->size;pj_syn_u32(a,0);pj_syn_put(a,"hdrl",4);
    {uint8_t avih[56];memset(avih,0,sizeof(avih));uint32_t us=(uint32_t)((uint64_t)fps_scale*1000000u/fps_rate);
     avih[0]=(uint8_t)us;avih[1]=(uint8_t)(us>>8);avih[2]=(uint8_t)(us>>16);avih[3]=(uint8_t)(us>>24);
     avih[24]=2;avih[32]=(uint8_t)width;avih[33]=(uint8_t)(width>>8);avih[36]=(uint8_t)height;avih[37]=(uint8_t)(height>>8);
     pj_syn_chunk(a,"avih",avih,56);}
    {pj_syn_put(a,"LIST",4);pj_syn_u32(a,4+8+56+8+40);pj_syn_put(a,"strl",4);
     uint8_t strh[56];memset(strh,0,sizeof(strh));memcpy(strh,"vids",4);memcpy(strh+4,"MJPG",4);
     strh[20]=(uint8_t)fps_scale;strh[21]=(uint8_t)(fps_scale>>8);strh[22]=(uint8_t)(fps_scale>>16);strh[23]=(uint8_t)(fps_scale>>24);
     strh[24]=(uint8_t)fps_rate;strh[25]=(uint8_t)(fps_rate>>8);strh[26]=(uint8_t)(fps_rate>>16);strh[27]=(uint8_t)(fps_rate>>24);
     pj_syn_chunk(a,"strh",strh,56);
     uint8_t strf[40];memset(strf,0,sizeof(strf));strf[0]=40;strf[4]=(uint8_t)width;strf[5]=(uint8_t)(width>>8);strf[8]=(uint8_t)height;strf[9]=(uint8_t)(height>>8);
     strf[12]=1;strf[14]=24;memcpy(strf+16,"MJPG",4);pj_syn_chunk(a,"strf",strf,40);}
    {pj_syn_put(a,"LIST",4);pj_syn_u32(a,4+8+56+8+20);pj_syn_put(a,"strl",4);
     uint8_t strh[56];memset(strh,0,sizeof(strh));memcpy(strh,"auds",4);strh[20]=1;strh[24]=(uint8_t)16000;strh[25]=(uint8_t)(16000>>8);
     pj_syn_chunk(a,"strh",strh,56);
     uint8_t strf[20];memset(strf,0,sizeof(strf));strf[0]=0x11;strf[2]=1;strf[4]=(uint8_t)16000;strf[5]=(uint8_t)(16000>>8);
     uint32_t bps=(uint32_t)((uint64_t)16000*block/(1u+2u*(block-4u)));strf[8]=(uint8_t)bps;strf[9]=(uint8_t)(bps>>8);
     strf[12]=(uint8_t)block;strf[13]=(uint8_t)(block>>8);strf[14]=4;strf[16]=2;strf[18]=(uint8_t)(1+2*(block-4));strf[19]=(uint8_t)((1+2*(block-4))>>8);
     pj_syn_chunk(a,"strf",strf,20);}
    {uint32_t n=(uint32_t)(a->size-hdrl_size_at-4);a->data[hdrl_size_at]=(uint8_t)n;a->data[hdrl_size_at+1]=(uint8_t)(n>>8);a->data[hdrl_size_at+2]=(uint8_t)(n>>16);a->data[hdrl_size_at+3]=(uint8_t)(n>>24);}
    /* LIST movi, unknown size */
    pj_syn_put(a,"LIST",4);pj_syn_u32(a,0xffffffffu);pj_syn_put(a,"movi",4);
    unsigned frames=(unsigned)((uint64_t)seconds*fps_rate/fps_scale),samples_per_block=1u+2u*(block-4u);
    uint64_t audio_samples=0;uint32_t seed=0x1234567u;
    uint8_t *jpeg=malloc(jpeg_frames?16384u:frame_bytes>big_bytes?frame_bytes:big_bytes),*ima=malloc(block);
    for(unsigned f=0;f<frames;f++) {
        uint64_t frame_time_samples=(uint64_t)f*fps_scale*16000u/fps_rate;
        while(audio_samples<=frame_time_samples) {
            for(unsigned i=0;i<block;i++){seed=seed*1103515245u+12345u;ima[i]=(uint8_t)(seed>>16);}
            ima[2]=(uint8_t)(ima[2]%89u);ima[3]=0;
            pj_syn_chunk(a,"01wb",ima,block);audio_samples+=samples_per_block;a->audio_blocks++;
        }
        frame_bytes=big_frames&&f%(small_frames+big_frames)>=small_frames?big_bytes:small_bytes;
        if(jpeg_frames) { /* frame_bytes 0: the default density; else the sparsest picture at least that big */
            size_t n=0;
            /* (busy scenes start from the sparsest of all, DC only: about 1,170 B here, 530 B once the gateway fits it) */
            for(unsigned eob=frame_bytes?(big_frames?1u:2u):5u;eob<=40u;eob++){n=pj_syn_jpeg_eob(jpeg,16384u,width,height,seed+f,eob);if(!frame_bytes||!n||n>=frame_bytes)break;}
            pj_syn_chunk(a,"00dc",jpeg,(uint32_t)n);a->frames++;continue;
        }
        for(unsigned i=0;i<frame_bytes;i++){seed=seed*1103515245u+12345u;jpeg[i]=(uint8_t)(seed>>16);}
        jpeg[0]=0xff;jpeg[1]=0xd8;jpeg[frame_bytes-2]=0xff;jpeg[frame_bytes-1]=0xd9;
        pj_syn_chunk(a,"00dc",jpeg,frame_bytes);a->frames++;
    }
    free(jpeg);free(ima);
}
static inline void pj_syn_avi_free(pj_syn_avi *a){free(a->data);memset(a,0,sizeof(*a));}
#endif
