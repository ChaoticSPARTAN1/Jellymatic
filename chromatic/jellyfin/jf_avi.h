/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Incremental AVI demuxer for Jellyfin's live MJPEG transcode (Chromatic player).
 *
 * Fed whatever bytes the network delivers, in any split. Stops right after each
 * complete video chunk ('00dc'/'00db') so the caller can show it when it is due:
 * the JPEG stays in a->frame until the next feed. Audio chunks ('01wb') go to the
 * optional sink callback as they arrive (no block is assembled; the sink says how
 * much it can take, and the parser waits for the rest: JF_AVI_AUDIO_FULL); without a
 * sink they are skipped and counted. With `hold` set the parser runs on through
 * sound and headers while a picture is still held, and stops in front of the next
 * video chunk (JF_AVI_VIDEO_HELD), so the audio ring is fed ahead of the pictures
 * (FFmpeg writes each audio block before every picture with a later timestamp).
 * Everything else is skipped. Jellyfin pipes the transcode, so RIFF and 'movi' sizes
 * are 0xFFFFFFFF (unknown): the parser never trusts outer sizes, only chunk sizes,
 * and never seeks. Portable C, no allocation, bounded memory, host-tested by
 * scripts/test_chromatic_player.py and test_chromatic_audio.py against real server
 * samples.
 */
#ifndef JF_AVI_H
#define JF_AVI_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>


enum {JF_AVI_HEAD,JF_AVI_CHUNK,JF_AVI_BODY,JF_AVI_SKIP,JF_AVI_AUDIO,JF_AVI_FAILED};
/* jf_avi_feed results */
enum {JF_AVI_BAD=-1,JF_AVI_MORE=0,JF_AVI_FRAME=1,JF_AVI_AUDIO_FULL=2,JF_AVI_VIDEO_HELD=3};

/* Audio sink: takes up to `size` bytes of the current '01wb' chunk (`first` = the chunk's
 * first bytes, i.e. a new ADPCM block with its own header) and returns how many it took now. */
typedef size_t (*jf_avi_audio_fn)(void *ctx,const uint8_t *data,size_t size,int first);

typedef struct {
    uint8_t state;
    uint8_t in_movi;
    uint8_t header[12];
    unsigned header_have;
    uint32_t chunk_left,pad;
    uint8_t video;                 /* current chunk is a video frame being kept */
    uint8_t hold;                  /* caller still holds a->frame: stop before the next video chunk */
    uint8_t audio_first;           /* the next audio bytes start their chunk */
    uint32_t us_per_frame;         /* from avih, or strh rate/scale when present */
    uint32_t rate,scale;           /* strh of the video stream: fps = rate/scale */
    uint32_t width,height;
    uint32_t frames,skipped,oversize,audio; /* audio: '01wb' chunks seen (delivered or skipped) */
    uint32_t frame_size,frame_index; /* valid after jf_avi_feed returns 1 */
    uint8_t *frame;uint32_t frame_cap; /* caller's buffer; larger frames are skipped, not fatal */
    /* small header capture for avih/strh/strf parsing */
    uint8_t meta[64];uint32_t meta_have,meta_want;uint32_t meta_id;
    uint8_t seen_video_strh,seen_audio_strh,want_audio_strf;
    /* the audio stream's WAVEFORMATEX: 0 until its strf was seen */
    uint16_t audio_format,audio_channels,audio_block;uint32_t audio_rate;
    jf_avi_audio_fn on_audio;void *audio_ctx;
} jf_avi;


static inline uint32_t jf_avi_u32(const uint8_t *p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
#define JF_FCC(a,b,c,d) ((uint32_t)(a)|((uint32_t)(b)<<8)|((uint32_t)(c)<<16)|((uint32_t)(d)<<24))

static inline void jf_avi_init(jf_avi *a,uint8_t *frame,uint32_t cap){memset(a,0,sizeof(*a));a->state=JF_AVI_HEAD;a->frame=frame;a->frame_cap=cap;}
/* Where '01wb' chunks go (NULL: skipped and counted). Survives nothing: set after each init. */
static inline void jf_avi_audio(jf_avi *a,jf_avi_audio_fn fn,void *ctx){a->on_audio=fn;a->audio_ctx=ctx;}

/* Frame duration in microseconds (never 0 once headers were seen; 0 before). */
static inline uint32_t jf_avi_frame_us(const jf_avi *a) {
    if(a->rate&&a->scale){uint64_t us=(uint64_t)a->scale*1000000u/a->rate;if(us>=8000&&us<=200000)return (uint32_t)us;}
    if(a->us_per_frame>=8000&&a->us_per_frame<=200000)return a->us_per_frame;
    return 0;
}

static inline void jf_avi_meta_done(jf_avi *a) {
    const uint8_t *m=a->meta;
    if(a->meta_id==JF_FCC('a','v','i','h')&&a->meta_have>=40) {
        a->us_per_frame=jf_avi_u32(m);
        if(!a->width){a->width=jf_avi_u32(m+32);a->height=jf_avi_u32(m+36);}
    } else if(a->meta_id==JF_FCC('s','t','r','h')&&a->meta_have>=32) {
        if(jf_avi_u32(m)==JF_FCC('v','i','d','s')&&!a->seen_video_strh) {
            a->seen_video_strh=1;a->scale=jf_avi_u32(m+20);a->rate=jf_avi_u32(m+24);
        } else if(jf_avi_u32(m)==JF_FCC('a','u','d','s')&&!a->seen_audio_strh) {
            a->seen_audio_strh=1;a->want_audio_strf=1; /* the next strf is this stream's WAVEFORMATEX */
        }
    } else if(a->meta_id==JF_FCC('s','t','r','f')&&a->meta_have>=16) {
        a->audio_format=(uint16_t)(m[0]|(m[1]<<8));a->audio_channels=(uint16_t)(m[2]|(m[3]<<8));
        a->audio_rate=jf_avi_u32(m+4);a->audio_block=(uint16_t)(m[12]|(m[13]<<8));
    }
}

/* Feeds bytes; *used says how many were taken. Returns JF_AVI_FRAME (1) when a frame is
 * ready (stopped right after it: feed the rest later), JF_AVI_MORE (0) when more bytes are
 * needed, JF_AVI_BAD (-1) once the stream is unusable (sticky), JF_AVI_AUDIO_FULL (2) when
 * the audio sink took less than offered (feed the rest later), JF_AVI_VIDEO_HELD (3) when
 * `hold` is set and the next video chunk is reached (its header is kept; feed again with
 * hold clear). A zero-size video chunk (a dropped frame) advances the index only. */
static inline int jf_avi_feed(jf_avi *a,const uint8_t *data,size_t size,size_t *used) {
    const uint8_t *start=data;*used=0;
    while((size||(a->state==JF_AVI_CHUNK&&a->header_have==8))&&a->state!=JF_AVI_FAILED) {
        if(a->state==JF_AVI_HEAD) {
            /* "RIFF" size "AVI " */
            size_t take=12-a->header_have;if(take>size)take=size;
            memcpy(a->header+a->header_have,data,take);a->header_have+=(unsigned)take;data+=take;size-=take;
            if(a->header_have<12){*used=(size_t)(data-start);return 0;}
            if(memcmp(a->header,"RIFF",4)||memcmp(a->header+8,"AVI ",4)){a->state=JF_AVI_FAILED;return -1;}
            a->header_have=0;a->state=JF_AVI_CHUNK;continue;
        }
        if(a->state==JF_AVI_CHUNK) {
            size_t take=8-a->header_have;if(take>size)take=size;
            memcpy(a->header+a->header_have,data,take);a->header_have+=(unsigned)take;data+=take;size-=take;
            if(a->header_have<8){*used=(size_t)(data-start);return 0;}
            uint32_t id=jf_avi_u32(a->header),len=jf_avi_u32(a->header+4);
            /* A held picture: stop in front of the next video chunk, header kept for the next call. */
            if(a->hold&&(uint16_t)(id&0xffffu)==('0'|('0'<<8))&&((uint16_t)(id>>16)==('d'|('c'<<8))||(uint16_t)(id>>16)==('d'|('b'<<8))))
                {*used=(size_t)(data-start);return JF_AVI_VIDEO_HELD;}
            a->header_have=0;
            if(id==JF_FCC('R','I','F','F')) { /* OpenDML 'AVIX' continuation: same layout */
                a->chunk_left=4;a->pad=0;a->video=0;a->meta_want=0;a->state=JF_AVI_SKIP;continue;
            }
            if(id==JF_FCC('L','I','S','T')) {
                /* Descend: the list type follows; 'movi' marks the frame data. */
                a->chunk_left=4;a->pad=0;a->video=0;a->meta_want=0;a->state=JF_AVI_SKIP;
                a->meta_id=JF_FCC('L','I','S','T');a->meta_have=0;a->meta_want=4;continue;
            }
            /* Every byte of id must be printable FourCC; anything else means we lost sync. */
            for(int i=0;i<4;i++){uint8_t c=a->header[i];if(c<0x20||c>0x7e){a->state=JF_AVI_FAILED;return -1;}}
            a->chunk_left=len;a->pad=len&1u;
            uint16_t stream=(uint16_t)(id&0xffffu),kind=(uint16_t)(id>>16);
            int is_video=(stream==('0'|('0'<<8)))&&(kind==('d'|('c'<<8))||kind==('d'|('b'<<8)));
            if(is_video) {
                if(len>a->frame_cap){a->oversize++;a->video=0;a->state=JF_AVI_SKIP;}
                else {a->video=1;a->frame_size=0;a->state=JF_AVI_BODY;}
                if(!len&&a->video){a->frames++;a->video=0;a->state=JF_AVI_CHUNK;} /* drop-frame marker */
                continue;
            }
            a->video=0;a->state=JF_AVI_SKIP;
            if(stream==('0'|('1'<<8))&&kind==('w'|('b'<<8))) {
                a->audio++;
                if(a->on_audio&&len){a->state=JF_AVI_AUDIO;a->audio_first=1;a->meta_want=0;continue;}
            }
            if(id==JF_FCC('a','v','i','h')||id==JF_FCC('s','t','r','h')||(id==JF_FCC('s','t','r','f')&&a->want_audio_strf)) {
                if(id==JF_FCC('s','t','r','f'))a->want_audio_strf=0;
                a->meta_id=id;a->meta_have=0;a->meta_want=len<sizeof(a->meta)?len:sizeof(a->meta);
            } else a->meta_want=0;
            if(!len&&!a->pad)a->state=JF_AVI_CHUNK;
            continue;
        }
        if(a->state==JF_AVI_AUDIO) { /* sound bytes straight to the sink, as much as it takes now */
            size_t take=a->chunk_left;if(take>size)take=size;
            size_t took=a->on_audio(a->audio_ctx,data,take,a->audio_first);
            if(took>take)took=take;
            if(took)a->audio_first=0;
            data+=took;size-=took;a->chunk_left-=(uint32_t)took;
            if(!a->chunk_left){a->state=a->pad?JF_AVI_SKIP:JF_AVI_CHUNK;a->chunk_left=a->pad;a->pad=0;a->meta_want=0;}
            if(took<take){*used=(size_t)(data-start);return JF_AVI_AUDIO_FULL;}
            continue;
        }
        if(a->state==JF_AVI_BODY) {
            size_t take=a->chunk_left;if(take>size)take=size;
            memcpy(a->frame+a->frame_size,data,take);a->frame_size+=(uint32_t)take;
            a->chunk_left-=(uint32_t)take;data+=take;size-=take;
            if(!a->chunk_left) {
                uint32_t index=a->frames++;
                a->video=0;a->state=a->pad?JF_AVI_SKIP:JF_AVI_CHUNK;a->chunk_left=a->pad;a->pad=0;
                if(a->frame_size>=4&&a->frame[0]==0xff&&a->frame[1]==0xd8) {
                    a->frame_index=index;*used=(size_t)(data-start);return 1;
                }
                a->skipped++;
            }
            continue;
        }
        /* JF_AVI_SKIP: chunk body (or list type / pad byte), capturing small headers. */
        /* body first, then the pad byte: never add them (0xFFFFFFFF + 1 would wrap to 0 and spin) */
        size_t take=a->chunk_left?a->chunk_left:a->pad;if(take>size)take=size;
        if(a->meta_want&&a->meta_have<a->meta_want) {
            size_t keep=a->meta_want-a->meta_have;if(keep>take)keep=take;
            memcpy(a->meta+a->meta_have,data,keep);a->meta_have+=(uint32_t)keep;
            if(a->meta_have==a->meta_want) {
                if(a->meta_id==JF_FCC('L','I','S','T'))a->in_movi=jf_avi_u32(a->meta)==JF_FCC('m','o','v','i')||a->in_movi;
                else jf_avi_meta_done(a);
            }
        }
        data+=take;size-=take;
        if(a->chunk_left)a->chunk_left-=(uint32_t)take;
        else a->pad-=(uint32_t)take;
        if(!a->chunk_left&&!a->pad){a->state=JF_AVI_CHUNK;a->meta_want=0;}
    }
    *used=(size_t)(data-start);
    return a->state==JF_AVI_FAILED?-1:0;
}
#endif
