/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.3.9: one server picture made into one the Pocket's JPEG decoder takes (docs/Design-notes.md
 * "Launcher 1.3.9").
 *
 * Why. The Pocket decodes in hardware (rtl/pocket_jpeg_mmio.v: the pinned core_jpeg with SUPPORT_WRITABLE_DHT=0) and
 * takes only (a) a picture of exactly 128/160/256 x 96/128/144 (valid_geometry, and the picture's own SOF size must be
 * that size) and (b) the standard Huffman tables of JPEG Annex K, which it has in ROM: tables in the picture are not
 * loaded. The owner's server (Jellyfin 10.11, measured by scripts/probe_link_stream_geometry.py) gives neither:
 *  - It keeps the title's shape. EnforceResolutionLimit (MediaBrowser.Controller EncodingHelper) turns width/height
 *    into maxWidth/maxHeight and scales to fit inside them, so a 16:9 title asked for at 128x96 comes as 128x72, a
 *    2.39:1 film as 128x54. No request parameter gives an exact size (only 4:3 titles land on 128x96; 256x144 fits
 *    16:9 but is 35-45 KB/s of pictures, over the link, and its largest pictures exceed the 12 KB frame buffer).
 *  - Its MJPEG encoder writes optimised Huffman tables in every picture (none of 360 sampled pictures had Annex K
 *    tables), which the Pocket's decoder would read with its ROM tables: garbage or a decoder fault.
 *
 * What. Lossless, in the coded domain: the picture's coefficients are read with its own tables and written again with
 * the Annex K ones (DC values re-predicted, AC run/size symbols and their extra bits unchanged), and whole rows of
 * black blocks (MCU rows, 16 pixels for 4:2:0) are added above and below to reach the Pocket's height: letterboxing,
 * as Jellyfin shows a film. No pixel is decoded or re-quantised. The tables are left out of the result (the decoder
 * has them; 300-400 bytes a picture less on the link, as scripts/jpeg_payload.py compact() does); with_dht puts them
 * in for a PC check that decodes the result with an ordinary decoder. Restrictions, all checked: baseline 8-bit,
 * 3 components 4:2:0 (Y 2x2, Cb 1x1, Cr 1x1) in one interleaved scan, no restart interval, the picture's width equal
 * to the target width, its height at most the target height. A picture's last MCU row keeps the encoder's own edge
 * padding (up to 15 rows of the bottom line repeated) below the picture.
 *
 * In place: the picture is moved to the end of `buf` and the result written from the start, the writer always behind
 * the reader (checked: PJ_FIT_ROOM). So the picture may use at most about half of `buf`.
 * Work area: pj_fit_work (about 3 KB) is the caller's; the launcher uses the half of the gateway's scratch area that
 * the PJAV ring leaves free while a title plays (pocket_link.h), so this costs no memory. */
#ifndef PJ_POCKET_JPEG_FIT_H
#define PJ_POCKET_JPEG_FIT_H
#include <stdint.h>
#include <stddef.h>
#include <string.h>

enum {
    PJ_FIT_OK=0,
    PJ_FIT_FORMAT=-1, /* not a baseline 4:2:0 single-scan JPEG without restarts */
    PJ_FIT_SIZE=-2,   /* its width is not the target width, or it is taller than the target */
    PJ_FIT_DATA=-3,   /* the coded data is damaged (no such code, too many coefficients, cut short) */
    PJ_FIT_ROOM=-4    /* the result would not fit in `buf` behind the picture */
};
#define PJ_FIT_HEADROOM 1024u /* buf must hold twice the picture plus this (the result's headers and black rows) */

/* Decoding table (JPEG F.2.2.3): codes of each length are consecutive. */
typedef struct {int32_t maxcode[18];uint16_t mincode[17],count;uint8_t valptr[17];uint8_t vals[256];} pj_fit_dtab;
typedef struct {
    pj_fit_dtab dc[2],ac[2];          /* the picture's tables (or Annex K where it has none) */
    uint16_t ac_code[2][256];uint8_t ac_size[2][256]; /* Annex K, for writing: 0 = luminance, 1 = chrominance */
    uint16_t dc_code[2][12];uint8_t dc_size[2][12];
} pj_fit_work;

/* ---- JPEG Annex K tables K.3 to K.6 ---- */
static const uint8_t pj_fit_std_bits[4][16]={
    {0,1,5,1,1,1,1,1,1,0,0,0,0,0,0,0},          /* DC luminance */
    {0,3,1,1,1,1,1,1,1,1,1,0,0,0,0,0},          /* DC chrominance */
    {0,2,1,3,3,2,4,3,5,5,4,4,0,0,1,0x7d},       /* AC luminance */
    {0,2,1,2,4,4,3,4,7,5,4,4,0,1,2,0x77}};      /* AC chrominance */
static const uint8_t pj_fit_std_dc_vals[12]={0,1,2,3,4,5,6,7,8,9,10,11};
static const uint8_t pj_fit_std_ac_luma[162]={
    0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,0x13,0x51,0x61,0x07,
    0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,
    0x24,0x33,0x62,0x72,0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
    0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,0x49,
    0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,
    0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
    0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,
    0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,
    0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
    0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa};
static const uint8_t pj_fit_std_ac_chroma[162]={
    0x00,0x01,0x02,0x03,0x11,0x04,0x05,0x21,0x31,0x06,0x12,0x41,0x51,0x07,0x61,0x71,
    0x13,0x22,0x32,0x81,0x08,0x14,0x42,0x91,0xa1,0xb1,0xc1,0x09,0x23,0x33,0x52,0xf0,
    0x15,0x62,0x72,0xd1,0x0a,0x16,0x24,0x34,0xe1,0x25,0xf1,0x17,0x18,0x19,0x1a,0x26,
    0x27,0x28,0x29,0x2a,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,
    0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,
    0x69,0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x82,0x83,0x84,0x85,0x86,0x87,
    0x88,0x89,0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,
    0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,
    0xc4,0xc5,0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,
    0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa};
static inline const uint8_t *pj_fit_std_vals(unsigned k){return k<2?pj_fit_std_dc_vals:k==2?pj_fit_std_ac_luma:pj_fit_std_ac_chroma;}

/* The Pocket's height for a picture the server sent at w x h: the shortest of 96/128/144 that holds it, 0 when the
 * width is not one the Pocket takes (128/160/256: the fit never adds columns) or nothing holds it. */
static inline unsigned pj_fit_height(unsigned w,unsigned h) {
    static const unsigned heights[3]={96u,128u,144u};
    if((w!=128u&&w!=160u&&w!=256u)||!h)return 0;
    for(unsigned i=0;i<3;i++)if(h<=heights[i])return heights[i];
    return 0;
}

/* A decoding table from a DHT's 16 counts and its symbols; 0 when the counts are not a prefix code. */
static inline int pj_fit_dtab_build(pj_fit_dtab *t,const uint8_t bits[16],const uint8_t *vals,unsigned count,unsigned max_vals) {
    if(count>max_vals||count>256u)return 0;
    int32_t code=0;unsigned k=0;
    for(unsigned len=1;len<=16;len++) {
        unsigned n=bits[len-1];
        if(!n)t->maxcode[len]=-1;
        else {
            t->valptr[len]=(uint8_t)k;t->mincode[len]=(uint16_t)code;code+=(int32_t)n;k+=n;t->maxcode[len]=code-1;
            if(code>(1<<len))return 0; /* more codes of this length than there are */
        }
        code<<=1;
    }
    t->maxcode[17]=0x7fffffff;memcpy(t->vals,vals,count);t->count=(uint16_t)count;
    return 1;
}
/* Annex K codes for writing (JPEG C.2): DC 12 categories, AC 162 run/size symbols; absent symbols have size 0. */
static inline void pj_fit_std_codes(pj_fit_work *w) {
    for(unsigned k=0;k<4;k++) {
        const uint8_t *vals=pj_fit_std_vals(k);unsigned code=0,at=0;
        if(k>=2)memset(w->ac_size[k-2],0,256);
        for(unsigned len=1;len<=16;len++) {
            for(unsigned i=0;i<pj_fit_std_bits[k][len-1];i++,at++,code++) {
                uint8_t s=vals[at];
                if(k<2){w->dc_code[k][s]=(uint16_t)code;w->dc_size[k][s]=(uint8_t)len;}
                else{w->ac_code[k-2][s]=(uint16_t)code;w->ac_size[k-2][s]=(uint8_t)len;}
            }
            code<<=1;
        }
    }
}

/* ---- bits ---- */
/* Reader over entropy-coded data: FF 00 is a data FF; any other marker ends the data (zeros are read after it, and
 * counted: reading real data past its end is damage). acc holds `bits` bits, the next one at bit 31. */
typedef struct {const uint8_t *p,*end;uint32_t acc;int bits;int32_t real;uint8_t ended;} pj_fit_br;
static inline void pj_fit_fill(pj_fit_br *r) {
    while(r->bits<=24) {
        uint32_t b=0;int is_real=0;
        if(!r->ended&&r->p<r->end) {
            b=*r->p;
            if(b==0xFFu) {
                if(r->p+1<r->end&&r->p[1]==0){r->p+=2;is_real=1;}
                else{r->ended=1;b=0;}
            } else{r->p++;is_real=1;}
        }
        r->acc|=b<<(24-r->bits);r->bits+=8;if(is_real)r->real+=8;
    }
}
static inline uint32_t pj_fit_get(pj_fit_br *r,unsigned n) {
    if(!n)return 0;
    pj_fit_fill(r);
    uint32_t v=r->acc>>(32u-n);r->acc<<=n;r->bits-=(int)n;r->real-=(int32_t)n;
    return v;
}
/* One Huffman symbol, or -1 for a code the table does not have. */
static inline int pj_fit_huff(pj_fit_br *r,const pj_fit_dtab *t) {
    pj_fit_fill(r);
    int32_t code=0;
    for(unsigned len=1;len<=16;len++) {
        code=(code<<1)|(int32_t)(r->acc>>31);r->acc<<=1;r->bits--;r->real--;
        if(code<=t->maxcode[len]) {
            unsigned at=(unsigned)t->valptr[len]+(unsigned)(code-(int32_t)t->mincode[len]);
            return at<t->count?t->vals[at]:-1;
        }
    }
    return -1;
}
/* Writer: FF is followed by a stuffed 00; it stops (full) rather than reach *guard, the reader's next unread byte. */
typedef struct {uint8_t *p;uint8_t *const *guard;uint32_t acc;int bits;uint8_t full;} pj_fit_bw;
static inline void pj_fit_byte(pj_fit_bw *w,uint8_t b) {
    if(w->full||w->p>=*w->guard){w->full=1;return;}
    *w->p++=b;
    if(b==0xFFu){if(w->p>=*w->guard){w->full=1;return;}*w->p++=0;}
}
static inline void pj_fit_put(pj_fit_bw *w,uint32_t v,unsigned n) {
    if(!n)return;
    w->acc=(w->acc<<n)|(v&((1u<<n)-1u));w->bits+=(int)n;
    while(w->bits>=8){w->bits-=8;pj_fit_byte(w,(uint8_t)(w->acc>>w->bits));}
}
static inline void pj_fit_flush(pj_fit_bw *w){if(w->bits)pj_fit_put(w,0x7fu,(unsigned)(8-w->bits));} /* 1s to the byte */
/* A DC difference with the Annex K table `t` (0 luminance, 1 chrominance); 0 when it needs more than 11 bits. */
static inline int pj_fit_put_dc(pj_fit_bw *w,const pj_fit_work *wk,unsigned t,int32_t v) {
    uint32_t a=(uint32_t)(v<0?-v:v);unsigned c=0;
    while(a){c++;a>>=1;}
    if(c>11u)return 0;
    pj_fit_put(w,wk->dc_code[t][c],wk->dc_size[t][c]);
    if(c)pj_fit_put(w,(uint32_t)(v<0?v+(int32_t)(1u<<c)-1:v),c);
    return 1;
}
/* One black (or, for chroma, neutral) block: DC `dc` against *pred, then EOB. */
static inline int pj_fit_pad_block(pj_fit_bw *w,const pj_fit_work *wk,unsigned t,int32_t dc,int32_t *pred) {
    if(!pj_fit_put_dc(w,wk,t,dc-*pred))return 0;
    *pred=dc;pj_fit_put(w,wk->ac_code[t][0],wk->ac_size[t][0]);return 1;
}
/* One block of the picture: read with its tables, written with Annex K `t`. */
static inline int pj_fit_copy_block(pj_fit_br *r,pj_fit_bw *w,const pj_fit_work *wk,const pj_fit_dtab *dc,const pj_fit_dtab *ac,
                                    unsigned t,int32_t *pred_in,int32_t *pred_out) {
    int s=pj_fit_huff(r,dc);
    if(s<0||s>11)return 0;
    int32_t diff=0;
    if(s){uint32_t v=pj_fit_get(r,(unsigned)s);diff=v<(1u<<(s-1))?(int32_t)v-(int32_t)((1u<<s)-1u):(int32_t)v;}
    *pred_in+=diff;
    if(!pj_fit_put_dc(w,wk,t,*pred_in-*pred_out))return 0;
    *pred_out=*pred_in;
    for(unsigned k=1;k<64;) {
        int rs=pj_fit_huff(r,ac);
        if(rs<0)return 0;
        unsigned run=(unsigned)rs>>4,size=(unsigned)rs&15u;
        if(!size) {
            if(run==15u){k+=16u;if(k>64u)return 0;pj_fit_put(w,wk->ac_code[t][0xf0],wk->ac_size[t][0xf0]);continue;} /* ZRL */
            if(run)return 0;
            pj_fit_put(w,wk->ac_code[t][0],wk->ac_size[t][0]);return 1; /* EOB */
        }
        k+=run;
        if(k>63u||!wk->ac_size[t][rs])return 0; /* past the block, or a size above 10 (not baseline 8-bit) */
        uint32_t bits=pj_fit_get(r,size);
        pj_fit_put(w,wk->ac_code[t][rs],wk->ac_size[t][rs]);pj_fit_put(w,bits,size); /* same size: the same extra bits */
        k++;
    }
    return 1;
}

/* Fits the JPEG in buf[0..len) for the Pocket at out_w x out_h (see the header). buf holds cap bytes. On PJ_FIT_OK the
 * result is in buf[0..*out_len); on anything else buf's contents are undefined (the picture is lost: skip it). */
static inline int pj_fit_jpeg(uint8_t *buf,size_t len,size_t cap,unsigned out_w,unsigned out_h,int with_dht,pj_fit_work *wk,size_t *out_len) {
    if(!buf||!wk||!out_len||len<4u||len>cap||buf[0]!=0xFFu||buf[1]!=0xD8u)return PJ_FIT_FORMAT;
    if(!out_w||!out_h||out_w%16u||out_h%16u||out_w>4096u||out_h>4096u)return PJ_FIT_SIZE;
    if(2u*len+PJ_FIT_HEADROOM>cap)return PJ_FIT_ROOM;
    uint8_t *in=buf+cap-len;memmove(in,buf,len);
    uint8_t *o=buf;*o++=0xFF;*o++=0xD8;
    unsigned w=0,h=0;uint8_t cid[3]={0},ctq[3]={0},sel[3]={0},have=0,qseen=0;uint16_t dcq[4]={0};size_t scan=0;
    const uint8_t *sof=NULL;
    for(size_t at=2;;) {
        if(at+4u>len||in[at]!=0xFFu)return PJ_FIT_FORMAT;
        uint8_t m=in[at+1];
        if(m==0xFFu){at++;continue;} /* fill */
        if(m==0xD8u||m==0xD9u||m==0x01u||(m>=0xD0u&&m<=0xD7u))return PJ_FIT_FORMAT;
        size_t seg=((size_t)in[at+2]<<8)|in[at+3];
        if(seg<2u||at+2u+seg>len)return PJ_FIT_FORMAT;
        const uint8_t *s=in+at+4;size_t n=seg-2u;
        if(m==0xDBu) { /* DQT: 8-bit tables only (the Pocket's decoder); copied as they are */
            for(size_t i=0;i<n;i+=65u){if((s[i]>>4)||(s[i]&15u)>3u||i+65u>n)return PJ_FIT_FORMAT;dcq[s[i]&15u]=s[i+1];qseen|=(uint8_t)(1u<<(s[i]&15u));}
            if((size_t)(o-buf)+2u+seg>PJ_FIT_HEADROOM/2u)return PJ_FIT_FORMAT;
            memcpy(o,in+at,2u+seg);o+=2u+seg;
        } else if(m==0xC4u) { /* DHT */
            for(size_t i=0;i<n;) {
                if(i+17u>n)return PJ_FIT_FORMAT;
                unsigned tc=s[i]>>4,th=s[i]&15u,count=0;
                for(unsigned j=0;j<16;j++)count+=s[i+1+j];
                if(tc>1u||th>1u||i+17u+count>n)return PJ_FIT_FORMAT;
                if(!pj_fit_dtab_build(tc?&wk->ac[th]:&wk->dc[th],s+i+1,s+i+17,count,tc?256u:12u))return PJ_FIT_FORMAT;
                have|=(uint8_t)(1u<<(tc*2u+th));i+=17u+count;
            }
        } else if(m==0xC0u) { /* SOF0: 8-bit, Y 2x2 + Cb 1x1 + Cr 1x1 */
            if(n!=15u||s[0]!=8u||s[5]!=3u||s[7]!=0x22u||s[10]!=0x11u||s[13]!=0x11u)return PJ_FIT_FORMAT;
            h=((unsigned)s[1]<<8)|s[2];w=((unsigned)s[3]<<8)|s[4];
            for(unsigned c=0;c<3;c++){cid[c]=s[6+3*c];ctq[c]=s[8+3*c];if(ctq[c]>3u)return PJ_FIT_FORMAT;}
            sof=in+at;
        } else if(m==0xDDu) { /* DRI: restarts are not handled */
            if(n<2u||s[0]||s[1])return PJ_FIT_FORMAT;
        } else if(m==0xDAu) { /* SOS: one interleaved scan of the three, in frame order */
            if(!sof||n!=10u||s[0]!=3u||s[7]!=0||s[8]!=63u||s[9]!=0)return PJ_FIT_FORMAT;
            for(unsigned c=0;c<3;c++){if(s[1+2*c]!=cid[c]||(s[2+2*c]>>4)>1u||(s[2+2*c]&15u)>1u)return PJ_FIT_FORMAT;sel[c]=s[2+2*c];}
            scan=at+2u+seg;break;
        } else if((m>=0xC1u&&m<=0xCFu&&m!=0xC4u&&m!=0xC8u&&m!=0xCCu)||m==0xDCu||m==0xDEu||m==0xDFu)return PJ_FIT_FORMAT; /* not baseline */
        at+=2u+seg; /* APPn, COM and the rest: left out */
    }
    for(unsigned c=0;c<3;c++)if(!(qseen&(1u<<ctq[c])))return PJ_FIT_FORMAT;
    if(!dcq[ctq[0]])return PJ_FIT_FORMAT;
    if(w!=out_w||!h||h>out_h)return PJ_FIT_SIZE;
    /* Tables the picture does not carry are the Annex K ones (an MJPEG picture may leave them out, AVI1) */
    for(unsigned k=0;k<4;k++)if(!(have&(1u<<k))) {
        unsigned tc=k>>1,th=k&1u,count=0;for(unsigned j=0;j<16;j++)count+=pj_fit_std_bits[tc*2u+th][j];
        if(!pj_fit_dtab_build(tc?&wk->ac[th]:&wk->dc[th],pj_fit_std_bits[tc*2u+th],pj_fit_std_vals(tc*2u+th),count,256u))return PJ_FIT_FORMAT;
    }
    pj_fit_std_codes(wk);
    /* the result's headers: DQT (copied above), SOF0 at the new height, Annex K tables (only with_dht), SOS 0/0 1/1 1/1 */
    memcpy(o,sof,19);o[5]=(uint8_t)(out_h>>8);o[6]=(uint8_t)out_h;o+=19;
    if(with_dht) {
        size_t length=2u;for(unsigned k=0;k<4;k++){length+=17u;for(unsigned j=0;j<16;j++)length+=pj_fit_std_bits[k][j];}
        *o++=0xFF;*o++=0xC4;*o++=(uint8_t)(length>>8);*o++=(uint8_t)length;
        for(unsigned k=0;k<4;k++) {
            unsigned count=0;*o++=(uint8_t)((k>>1)<<4|(k&1u));
            for(unsigned j=0;j<16;j++){*o++=pj_fit_std_bits[k][j];count+=pj_fit_std_bits[k][j];}
            memcpy(o,pj_fit_std_vals(k),count);o+=count;
        }
    }
    {static const uint8_t sos[14]={0xFF,0xDA,0,12,3,0,0x00,0,0x11,0,0x11,0,63,0};
     memcpy(o,sos,14);o[5]=cid[0];o[7]=cid[1];o[9]=cid[2];o+=14;}
    /* the scan */
    pj_fit_br r={in+scan,in+len,0,0,0,0};
    uint8_t *reader=(uint8_t *)r.p,*const end_guard=buf+cap;
    pj_fit_bw bw={o,(uint8_t *const *)&reader,0,0,0};
    unsigned mcus=w/16u,in_rows=(h+15u)/16u,out_rows=out_h/16u,pad=out_rows-in_rows;
    unsigned top=(out_h-h)/2u;top=(top+8u)/16u;if(top>pad)top=pad;
    int32_t black=-(int32_t)((1024u+dcq[ctq[0]]/2u)/dcq[ctq[0]]); /* Y=0: (0-128)*8 quantised; Cb=Cr=0 is neutral */
    if(black<-2047)black=-2047;
    int32_t pin[3]={0,0,0},pout[3]={0,0,0};
    for(unsigned row=0;row<out_rows;row++) {
        int picture=row>=top&&row<top+in_rows;
        for(unsigned x=0;x<mcus;x++) {
            for(unsigned c=0;c<3;c++) {
                unsigned blocks=c?1u:4u,t=c?1u:0u;
                for(unsigned b=0;b<blocks;b++) {
                    int ok=picture?pj_fit_copy_block(&r,&bw,wk,&wk->dc[sel[c]>>4],&wk->ac[sel[c]&15u],t,&pin[c],&pout[c])
                                  :pj_fit_pad_block(&bw,wk,t,c?0:black,&pout[c]);
                    if(!ok)return PJ_FIT_DATA;
                    reader=(uint8_t *)r.p; /* everything before it is read (into acc): the writer may take it */
                    if(bw.full)return PJ_FIT_ROOM;
                }
            }
        }
        if(picture&&row+1u==top+in_rows) { /* the picture is read: it must not have run into its end */
            if(r.real<0)return PJ_FIT_DATA;
            reader=end_guard;
        }
    }
    pj_fit_flush(&bw);
    if(bw.full||bw.p+2u>end_guard)return PJ_FIT_ROOM;
    *bw.p++=0xFF;*bw.p++=0xD9;
    *out_len=(size_t)(bw.p-buf);
    return PJ_FIT_OK;
}
#endif
