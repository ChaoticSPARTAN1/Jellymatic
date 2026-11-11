/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Portable MD5 (RFC 1321), for the service mode's per-chunk check. esptool's
 * flash_md5sum reports MD5, so the PC can compare the two directly. Not used for
 * anything secret. Host-tested against hashlib.
 */
#ifndef PJ_MD5_H
#define PJ_MD5_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef struct {uint32_t h[4];uint64_t length;uint8_t block[64];size_t used;} pj_md5;
#define PJ_MD5_ROL(x,n) (((x)<<(n))|((x)>>(32-(n))))
static inline void pj_md5_block(pj_md5 *m,const uint8_t *p) {
    static const uint8_t shift[64]={7,12,17,22,7,12,17,22,7,12,17,22,7,12,17,22,5,9,14,20,5,9,14,20,5,9,14,20,5,9,14,20,
        4,11,16,23,4,11,16,23,4,11,16,23,4,11,16,23,6,10,15,21,6,10,15,21,6,10,15,21,6,10,15,21};
    static const uint32_t k[64]={
        0xd76aa478,0xe8c7b756,0x242070db,0xc1bdceee,0xf57c0faf,0x4787c62a,0xa8304613,0xfd469501,
        0x698098d8,0x8b44f7af,0xffff5bb1,0x895cd7be,0x6b901122,0xfd987193,0xa679438e,0x49b40821,
        0xf61e2562,0xc040b340,0x265e5a51,0xe9b6c7aa,0xd62f105d,0x02441453,0xd8a1e681,0xe7d3fbc8,
        0x21e1cde6,0xc33707d6,0xf4d50d87,0x455a14ed,0xa9e3e905,0xfcefa3f8,0x676f02d9,0x8d2a4c8a,
        0xfffa3942,0x8771f681,0x6d9d6122,0xfde5380c,0xa4beea44,0x4bdecfa9,0xf6bb4b60,0xbebfbc70,
        0x289b7ec6,0xeaa127fa,0xd4ef3085,0x04881d05,0xd9d4d039,0xe6db99e5,0x1fa27cf8,0xc4ac5665,
        0xf4292244,0x432aff97,0xab9423a7,0xfc93a039,0x655b59c3,0x8f0ccc92,0xffeff47d,0x85845dd1,
        0x6fa87e4f,0xfe2ce6e0,0xa3014314,0x4e0811a1,0xf7537e82,0xbd3af235,0x2ad7d2bb,0xeb86d391};
    uint32_t w[16],a=m->h[0],b=m->h[1],c=m->h[2],d=m->h[3];
    for(int i=0;i<16;i++)w[i]=(uint32_t)p[i*4]|(uint32_t)p[i*4+1]<<8|(uint32_t)p[i*4+2]<<16|(uint32_t)p[i*4+3]<<24;
    for(unsigned i=0;i<64;i++) {
        uint32_t f;unsigned g;
        if(i<16){f=(b&c)|(~b&d);g=i;}
        else if(i<32){f=(d&b)|(~d&c);g=(5*i+1)&15;}
        else if(i<48){f=b^c^d;g=(3*i+5)&15;}
        else{f=c^(b|~d);g=(7*i)&15;}
        uint32_t t=d;d=c;c=b;b=b+PJ_MD5_ROL(a+f+k[i]+w[g],shift[i]);a=t;
    }
    m->h[0]+=a;m->h[1]+=b;m->h[2]+=c;m->h[3]+=d;
}
static inline void pj_md5_init(pj_md5 *m) {
    m->h[0]=0x67452301;m->h[1]=0xefcdab89;m->h[2]=0x98badcfe;m->h[3]=0x10325476;m->length=0;m->used=0;
}
static inline void pj_md5_update(pj_md5 *m,const void *data,size_t size) {
    const uint8_t *p=data;
    m->length+=size;
    while(size) {
        size_t take=64-m->used;if(take>size)take=size;
        memcpy(m->block+m->used,p,take);m->used+=take;p+=take;size-=take;
        if(m->used==64){pj_md5_block(m,m->block);m->used=0;}
    }
}
static inline void pj_md5_final(pj_md5 *m,uint8_t out[16]) {
    uint64_t bits=m->length*8;uint8_t pad=0x80,zero=0,len[8];
    pj_md5_update(m,&pad,1);
    while(m->used!=56)pj_md5_update(m,&zero,1);
    for(int i=0;i<8;i++)len[i]=(uint8_t)(bits>>(8*i));
    pj_md5_update(m,len,8);
    for(int i=0;i<4;i++)for(int j=0;j<4;j++)out[i*4+j]=(uint8_t)(m->h[i]>>(8*j));
}
#endif
