/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC check for the Chromatic player's sound path on the PC: jf_avi.h hands '01wb' bytes
 * to a sink in whatever split the "network" delivers (random piece sizes from the seed),
 * the sink itself takes only a bounded amount per call (like the ESP32's audio ring),
 * and jf_audio.h decodes them. The decoded PCM goes to argv[3] as raw s16le for a
 * byte-for-byte comparison with FFmpeg's decode of the same file
 * (scripts/test_chromatic_audio.py). Pictures are counted but not decoded.
 * With HOLD=1 every picture is held for one extra call (hold), checking that sound runs
 * ahead of a held picture and the parser stops in front of the next picture.
 * Usage: check_audio <avi> <seed> <pcm out>
 */
#include "jf_avi.h"
#include "jf_audio.h"
#include <stdio.h>
#include <stdlib.h>

static jf_avi avi;static jf_ima ima;static FILE *pcm;
static unsigned long blocks,audio_bytes,samples,sink_limit=160,held_stops,frames;
static size_t sink(void *ctx,const uint8_t *data,size_t size,int first) {
    (void)ctx;
    if(first){blocks++;jf_ima_block_start(&ima);}
    if(size>sink_limit)size=sink_limit; /* a bounded ring: the rest comes back later */
    int16_t out[2*160+1];
    size_t n=jf_ima_feed(&ima,data,size,out,sizeof(out)/sizeof(out[0]));
    fwrite(out,2,n,pcm);samples+=n;audio_bytes+=size;
    return size;
}
int main(int argc,char **argv) {
    if(argc<4)return 2;
    FILE *f=fopen(argv[1],"rb");if(!f)return 2;
    pcm=fopen(argv[3],"wb");if(!pcm)return 2;
    unsigned seed=(unsigned)atoi(argv[2]);srand(seed);
    int hold=getenv("HOLD")!=NULL;
    if(getenv("SINK"))sink_limit=(unsigned long)atoi(getenv("SINK"));
    if(!sink_limit||sink_limit>160)sink_limit=160;
    static uint8_t buf[65536],frame[16384];
    jf_avi_init(&avi,frame,sizeof(frame));jf_avi_audio(&avi,sink,NULL);jf_ima_init(&ima);
    int holding=0;
    for(;;) {
        size_t want=seed?1+(size_t)(rand()%(seed%3==0?7:4096)):sizeof(buf);
        size_t n=fread(buf,1,want,f);if(!n)break;
        size_t at=0;
        while(at<n) {
            size_t used;
            avi.hold=(uint8_t)holding;
            int r=jf_avi_feed(&avi,buf+at,n-at,&used);at+=used;
            if(r==JF_AVI_BAD){printf("FAILED\n");return 1;}
            if(r==JF_AVI_FRAME){frames++;if(hold)holding=1;}
            else if(r==JF_AVI_VIDEO_HELD){held_stops++;holding=0;} /* "show" the picture, then go on */
            else if(r==JF_AVI_AUDIO_FULL){} /* the sink took part: offer the rest at once */
            else if(used==0&&at<n){printf("STUCK\n");return 1;}
        }
    }
    fclose(pcm);
    printf("A format=%u channels=%u rate=%u block=%u blocks=%lu bytes=%lu samples=%lu frames=%lu held_stops=%lu chunks=%u\n",
           avi.audio_format,avi.audio_channels,(unsigned)avi.audio_rate,avi.audio_block,blocks,audio_bytes,samples,frames,held_stops,(unsigned)avi.audio);
    return 0;
}
