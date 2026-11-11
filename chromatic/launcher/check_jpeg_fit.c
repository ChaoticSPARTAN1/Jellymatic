/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Host check of launcher 1.3.9's picture fit (shared/pocket_jpeg_fit.h): server pictures re-coded with the Pocket
 * decoder's Annex K tables and letterboxed to its height. Synthetic pictures only (pocket/synthetic_avi.h pj_syn_jpeg:
 * real baseline JPEGs with non-Annex-K tables). No hardware, no network, no files except in --fit mode.
 *
 *   check_jpeg_fit                     the checks below
 *   check_jpeg_fit --fit IN OUT H DHT  fit the JPEG file IN to its own width x H into OUT (DHT 1: Annex K tables
 *                                      written, for an ordinary decoder); exit 0 on success, else 2 and the code
 *   check_jpeg_fit --gen W H SEED OUT  write pj_syn_jpeg's picture (for scripts/test_link_letterbox.py)
 * The pixel-level checks (an ordinary decoder, the Pocket decoder's RTL and its C reference) are in
 * scripts/test_link_letterbox.py, which uses the two modes. */
#include <stdio.h>
#include <stdlib.h>
#include "shared/pocket_jpeg_fit.h"
#include "../../pocket/synthetic_avi.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"jpeg fit check failed at line %d\n",__LINE__);exit(1);}} while(0)
#define CAP 12288u /* = PJ_PLINK_BUF, the launcher's frame buffer */

static pj_fit_work work;
/* The markers of a result: SOF size, whether a DHT is there, SOS present, ends in EOI. */
typedef struct {unsigned w,h;int dht,sos,eoi;} facts;
static facts facts_of(const uint8_t *j,size_t n) {
    facts f={0,0,0,0,0};size_t at=2;
    while(at+4<=n&&j[at]==0xff) {
        uint8_t m=j[at+1];size_t seg=((size_t)j[at+2]<<8)|j[at+3];
        if(m==0xc0){f.h=((unsigned)j[at+5]<<8)|j[at+6];f.w=((unsigned)j[at+7]<<8)|j[at+8];}
        if(m==0xc4)f.dht=1;
        if(m==0xda){f.sos=1;break;}
        at+=2+seg;
    }
    f.eoi=n>=2&&j[n-2]==0xff&&j[n-1]==0xd9;
    return f;
}
static int fit(const uint8_t *in,size_t n,unsigned w,unsigned h,int dht,uint8_t *out,size_t *out_n) {
    static uint8_t buf[CAP];memcpy(buf,in,n);
    int r=pj_fit_jpeg(buf,n,CAP,w,h,dht,&work,out_n);
    if(r==PJ_FIT_OK)memcpy(out,buf,*out_n);
    return r;
}
static int file_mode(int argc,char **argv) {
    if(argc==6&&!strcmp(argv[1],"--fit")) {
        static uint8_t in[CAP],out[CAP];FILE *f=fopen(argv[2],"rb");if(!f)return 3;
        size_t n=fread(in,1,sizeof(in),f);fclose(f);
        unsigned h=(unsigned)atoi(argv[4]);int dht=atoi(argv[5]);size_t m=0;
        int r=fit(in,n,facts_of(in,n).w,h,dht,out,&m);
        if(r!=PJ_FIT_OK){printf("fit=%d\n",r);return 2;}
        f=fopen(argv[3],"wb");if(!f)return 3;fwrite(out,1,m,f);fclose(f);printf("fit=0 bytes=%zu\n",m);return 0;
    }
    if(argc==6&&!strcmp(argv[1],"--gen")) {
        static uint8_t out[16384];size_t m=pj_syn_jpeg(out,sizeof(out),(unsigned)atoi(argv[2]),(unsigned)atoi(argv[3]),(uint32_t)strtoul(argv[4],NULL,10));
        if(!m)return 3;
        FILE *f=fopen(argv[5],"wb");if(!f)return 3;fwrite(out,1,m,f);fclose(f);return 0;
    }
    return -1;
}
int main(int argc,char **argv) {
    if(argc>1)return file_mode(argc,argv);
    static uint8_t pic[16384],a[CAP],b[CAP],c[CAP];size_t n,na,nb,nc;
    /* The Pocket's heights for the sizes the server sends (Jellyfin keeps the title's shape) */
    CHECK(pj_fit_height(128,96)==96&&pj_fit_height(128,72)==96&&pj_fit_height(128,54)==96&&pj_fit_height(128,97)==128);
    CHECK(pj_fit_height(160,90)==96&&pj_fit_height(256,144)==144&&pj_fit_height(256,106)==128&&pj_fit_height(128,145)==0);
    CHECK(pj_fit_height(120,96)==0&&pj_fit_height(96,96)==0&&pj_fit_height(128,0)==0);
    /* Every shape: fitted, Annex K (no tables in the result), the Pocket's size, and a fit of the result changes nothing
     * (Annex K in, Annex K out, nothing to add): the coefficients were carried over exactly. With the tables written,
     * the same picture; that one fitted again without them is byte for byte the first result. */
    static const unsigned sizes[][2]={{128,96},{128,72},{128,54},{128,92},{128,80},{128,16},{160,90},{160,128},{256,144},{256,106},{256,128}};
    for(unsigned s=0;s<sizeof(sizes)/sizeof(sizes[0]);s++)for(uint32_t seed=1;seed<=20;seed++) {
        unsigned w=sizes[s][0],h=sizes[s][1],out_h=pj_fit_height(w,h);
        n=pj_syn_jpeg(pic,sizeof(pic),w,h,seed*7919u);CHECK(n>0);
        if(2u*n+PJ_FIT_HEADROOM>CAP){CHECK(fit(pic,n,w,out_h,0,a,&na)==PJ_FIT_ROOM);continue;}
        CHECK(fit(pic,n,w,out_h,0,a,&na)==PJ_FIT_OK);
        facts f=facts_of(a,na);CHECK(f.w==w&&f.h==out_h&&!f.dht&&f.sos&&f.eoi&&na<n+64u*(out_h-h+16u));
        CHECK(fit(a,na,w,out_h,0,b,&nb)==PJ_FIT_OK&&nb==na&&!memcmp(a,b,na));
        CHECK(fit(pic,n,w,out_h,1,c,&nc)==PJ_FIT_OK&&facts_of(c,nc).dht&&facts_of(c,nc).h==out_h);
        if(2u*nc+PJ_FIT_HEADROOM<=CAP)CHECK(fit(c,nc,w,out_h,0,b,&nb)==PJ_FIT_OK&&nb==na&&!memcmp(a,b,na));
    }
    /* Refused, with the reason: */
    n=pj_syn_jpeg(pic,sizeof(pic),128,72,99);
    CHECK(fit(pic,n,160,96,0,a,&na)==PJ_FIT_SIZE);                  /* another width (no columns are added) */
    CHECK(fit(pic,n,128,64,0,a,&na)==PJ_FIT_SIZE);                  /* taller than the target */
    CHECK(fit(pic,n,128,90,0,a,&na)==PJ_FIT_SIZE);                  /* a target that is not whole 16-pixel rows */
    {   /* damaged coded data: cut the scan short (the EOI kept), or a marker in the middle of it */
        size_t sos=0;for(size_t i=2;i+1<n;i++)if(pic[i]==0xff&&pic[i+1]==0xda){sos=i;break;}
        CHECK(sos);memcpy(b,pic,n);size_t cut=sos+14+(n-sos-14)/2;b[cut]=0xff;b[cut+1]=0xd9;
        CHECK(fit(b,cut+2,128,96,0,a,&na)==PJ_FIT_DATA);
        memcpy(b,pic,n);b[sos+40]=0xff;b[sos+41]=0xd0;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_DATA);
        /* Huffman counts that are not a prefix code (more 1-bit codes than there are) */
        size_t dht=0;for(size_t i=2;i+1<n;i++)if(pic[i]==0xff&&pic[i+1]==0xc4){dht=i;break;}
        CHECK(dht&&pic[dht+4]==0x00&&pic[dht+5+3]==12);
        memcpy(b,pic,n);b[dht+5]=3;b[dht+5+3]=9;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
    }
    {   /* not what the Pocket's decoder takes: 4:4:4, 12-bit, progressive, a restart interval, 16-bit DQT, not a JPEG */
        size_t sof=0;for(size_t i=2;i+1<n;i++)if(pic[i]==0xff&&pic[i+1]==0xc0){sof=i;break;}
        CHECK(sof);
        memcpy(b,pic,n);b[sof+11]=0x11;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
        memcpy(b,pic,n);b[sof+4]=12;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
        memcpy(b,pic,n);b[sof+1]=0xc2;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
        static const uint8_t dri[6]={0xff,0xdd,0,4,0,8};memcpy(b,pic,2);memcpy(b+2,dri,6);memcpy(b+8,pic+2,n-2);
        CHECK(fit(b,n+6,128,96,0,a,&na)==PJ_FIT_FORMAT);
        static const uint8_t dri0[6]={0xff,0xdd,0,4,0,0};memcpy(b+2,dri0,6);CHECK(fit(b,n+6,128,96,0,a,&na)==PJ_FIT_OK); /* interval 0: none */
        size_t dqt=0;for(size_t i=2;i+1<n;i++)if(pic[i]==0xff&&pic[i+1]==0xdb){dqt=i;break;}
        memcpy(b,pic,n);b[dqt+4]=0x10;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
        memcpy(b,pic,n);b[0]=0;CHECK(fit(b,n,128,96,0,a,&na)==PJ_FIT_FORMAT);
    }
    {   /* a picture without Huffman tables uses Annex K (AVI1 MJPEG): our own result, tables taken out, fits as is */
        n=pj_syn_jpeg(pic,sizeof(pic),128,96,5);CHECK(fit(pic,n,128,96,0,a,&na)==PJ_FIT_OK);
        CHECK(fit(a,na,128,96,0,b,&nb)==PJ_FIT_OK&&nb==na&&!memcmp(a,b,na));
    }
    {   /* room: a picture over half the buffer is refused, never written past it */
        static uint8_t big[CAP];n=pj_syn_jpeg(pic,sizeof(pic),256,144,3);CHECK(n>0);
        memcpy(big,pic,n);size_t m=0;CHECK(pj_fit_jpeg(big,n,2u*n+PJ_FIT_HEADROOM-1u,256,144,0,&work,&m)==PJ_FIT_ROOM);
    }
    {   /* damage anywhere never crashes and never writes past the buffer: 20,000 pictures with 1..8 bytes changed */
        static uint8_t guard[CAP+64];uint32_t seed=0x51ed;unsigned ok=0,refused=0;
        n=pj_syn_jpeg(pic,sizeof(pic),128,72,77);
        for(unsigned i=0;i<20000;i++) {
            memset(guard,0xa5,sizeof(guard));memcpy(guard,pic,n);
            for(unsigned k=0;k<1u+i%8u;k++){seed=seed*1103515245u+12345u;guard[(seed>>8)%n]=(uint8_t)(seed>>20);}
            size_t m=0;int r=pj_fit_jpeg(guard,n,CAP,128,96,0,&work,&m);
            for(unsigned k=CAP;k<CAP+64;k++)CHECK(guard[k]==0xa5);
            if(r==PJ_FIT_OK){CHECK(m>4&&m<=CAP&&guard[0]==0xff&&guard[1]==0xd8&&guard[m-2]==0xff&&guard[m-1]==0xd9);ok++;}
            else{CHECK(r==PJ_FIT_FORMAT||r==PJ_FIT_SIZE||r==PJ_FIT_DATA||r==PJ_FIT_ROOM);refused++;}
        }
        CHECK(ok>0&&refused>0);
        printf("damaged pictures: %u fitted, %u refused, none past the buffer\n",ok,refused);
    }
    CHECK(sizeof(pj_fit_work)<=4096u); /* behind the 4 KB PJAV ring in the gateway's 8 KB scratch area */
    printf("PASS: picture fit (1.3.9): 11 shapes x 20 pictures with non-Annex-K tables fitted to the Pocket's size, Annex K out, "
           "idempotent, the same with the tables written; refused: other width, too tall, damaged data, 4:4:4, 12-bit, progressive, "
           "restarts, 16-bit DQT, not a JPEG, too big for the buffer; tables absent = Annex K; damage never writes past the buffer. "
           "work area %u bytes.\n",(unsigned)sizeof(pj_fit_work));
    return 0;
}
