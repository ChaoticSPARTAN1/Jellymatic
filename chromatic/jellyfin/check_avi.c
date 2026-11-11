/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md */
/* PC check for jf_avi.h: feeds an AVI in random-size pieces, prints one line per frame
 * (index size crc32) plus the timing header, for comparison with an independent parser. */
#include "jf_avi.h"
#include <stdio.h>
#include <stdlib.h>
static uint32_t crc32(const uint8_t *p,size_t n){uint32_t c=~0u;while(n--){c^=*p++;for(int k=0;k<8;k++)c=c&1?(c>>1)^0xEDB88320u:c>>1;}return ~c;}
static jf_avi avi;
int main(int argc,char **argv){
    if(argc<3)return 2;
    FILE *f=fopen(argv[1],"rb");if(!f)return 2;
    unsigned seed=(unsigned)atoi(argv[2]);srand(seed);
    static uint8_t buf[65536],frame[16384];jf_avi_init(&avi,frame,sizeof(frame));
    for(;;){
        size_t want=seed?1+(size_t)(rand()%(seed%3==0?7:4096)):sizeof(buf);
        size_t n=fread(buf,1,want,f);if(!n)break;
        size_t at=0;
        while(at<n){
            size_t used;int r=jf_avi_feed(&avi,buf+at,n-at,&used);at+=used;
            if(r<0){printf("FAILED\n");return 1;}
            if(r==1)printf("F %u %u %08x\n",(unsigned)avi.frame_index,(unsigned)avi.frame_size,crc32(avi.frame,avi.frame_size));
            else if(used==0&&at<n){printf("STUCK\n");return 1;}
        }
    }
    printf("H us=%u rate=%u scale=%u w=%u h=%u frames=%u skipped=%u oversize=%u\n",(unsigned)jf_avi_frame_us(&avi),
        (unsigned)avi.rate,(unsigned)avi.scale,(unsigned)avi.width,(unsigned)avi.height,(unsigned)avi.frames,
        (unsigned)avi.skipped,(unsigned)avi.oversize);
    return 0;
}
