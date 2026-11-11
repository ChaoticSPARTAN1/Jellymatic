/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Portable SHA-256 and HMAC-SHA256, moved unchanged out of chr1.h so the service
 * mode can hash flash without pulling in CHR1's tables. Host-tested against hashlib.
 */
#ifndef PJ_SHA256_H
#define PJ_SHA256_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* ---- SHA-256 and HMAC (FIPS 180-4, RFC 2104) -------------------------------- */
typedef struct {uint32_t h[8];uint64_t length;uint8_t block[64];size_t used;} pj_sha256;
static const uint32_t pj_sha256_k[64]={
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
#define PJ_ROR(x,n) (((x)>>(n))|((x)<<(32-(n))))
static inline void pj_sha256_block(pj_sha256 *s,const uint8_t *p) {
    uint32_t w[64],a,b,c,d,e,f,g,h;
    for(int i=0;i<16;i++)w[i]=(uint32_t)p[i*4]<<24|(uint32_t)p[i*4+1]<<16|(uint32_t)p[i*4+2]<<8|p[i*4+3];
    for(int i=16;i<64;i++) {
        uint32_t s0=PJ_ROR(w[i-15],7)^PJ_ROR(w[i-15],18)^(w[i-15]>>3);
        uint32_t s1=PJ_ROR(w[i-2],17)^PJ_ROR(w[i-2],19)^(w[i-2]>>10);
        w[i]=w[i-16]+s0+w[i-7]+s1;
    }
    a=s->h[0];b=s->h[1];c=s->h[2];d=s->h[3];e=s->h[4];f=s->h[5];g=s->h[6];h=s->h[7];
    for(int i=0;i<64;i++) {
        uint32_t t1=h+(PJ_ROR(e,6)^PJ_ROR(e,11)^PJ_ROR(e,25))+((e&f)^(~e&g))+pj_sha256_k[i]+w[i];
        uint32_t t2=(PJ_ROR(a,2)^PJ_ROR(a,13)^PJ_ROR(a,22))+((a&b)^(a&c)^(b&c));
        h=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
    }
    s->h[0]+=a;s->h[1]+=b;s->h[2]+=c;s->h[3]+=d;s->h[4]+=e;s->h[5]+=f;s->h[6]+=g;s->h[7]+=h;
}
static inline void pj_sha256_init(pj_sha256 *s) {
    static const uint32_t init[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    memcpy(s->h,init,sizeof(init));s->length=0;s->used=0;
}
static inline void pj_sha256_update(pj_sha256 *s,const void *data,size_t size) {
    const uint8_t *p=data;
    s->length+=size;
    while(size) {
        size_t take=64-s->used;if(take>size)take=size;
        memcpy(s->block+s->used,p,take);s->used+=take;p+=take;size-=take;
        if(s->used==64){pj_sha256_block(s,s->block);s->used=0;}
    }
}
static inline void pj_sha256_final(pj_sha256 *s,uint8_t out[32]) {
    uint64_t bits=s->length*8;uint8_t pad=0x80,zero=0,len[8];
    pj_sha256_update(s,&pad,1);
    while(s->used!=56)pj_sha256_update(s,&zero,1);
    for(int i=0;i<8;i++)len[i]=(uint8_t)(bits>>(56-8*i));
    pj_sha256_update(s,len,8);
    for(int i=0;i<8;i++){out[i*4]=(uint8_t)(s->h[i]>>24);out[i*4+1]=(uint8_t)(s->h[i]>>16);
                         out[i*4+2]=(uint8_t)(s->h[i]>>8);out[i*4+3]=(uint8_t)s->h[i];}
}
/* HMAC-SHA256 over two concatenated parts (head, then body), key of 32 bytes. */
static inline void pj_hmac(const uint8_t key[32],const void *head,size_t head_size,
                           const void *body,size_t body_size,uint8_t out[32]) {
    uint8_t pad[64],inner[32];pj_sha256 s;
    for(int i=0;i<64;i++)pad[i]=(uint8_t)((i<32?key[i]:0)^0x36);
    pj_sha256_init(&s);pj_sha256_update(&s,pad,64);pj_sha256_update(&s,head,head_size);
    if(body_size)pj_sha256_update(&s,body,body_size);
    pj_sha256_final(&s,inner);
    for(int i=0;i<64;i++)pad[i]=(uint8_t)((i<32?key[i]:0)^0x5c);
    pj_sha256_init(&s);pj_sha256_update(&s,pad,64);pj_sha256_update(&s,inner,32);pj_sha256_final(&s,out);
}
static inline void pj_hex(const uint8_t *data,size_t size,char *out) {
    static const char digits[]="0123456789abcdef";
    for(size_t i=0;i<size;i++){out[i*2]=digits[data[i]>>4];out[i*2+1]=digits[data[i]&15];}
    out[size*2]=0;
}
/* Constant-time compare of n hex characters. */
static inline int pj_same(const char *a,const char *b,size_t n) {
    unsigned diff=0;for(size_t i=0;i<n;i++)diff|=(unsigned)(a[i]^b[i]);return diff==0;
}
#endif
