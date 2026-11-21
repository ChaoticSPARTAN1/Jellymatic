/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: the sound feed. The short music loop plays all the time; an announcer
 * line (or the alert tone) is laid over it with the music turned down to 30 %, the levels of the
 * approved preview mix (the project's PC reference (not published)).
 *
 * The clips are IMA ADPCM in flash (wx_sound_data.c): the music and the tone at 8,000 Hz, the lines
 * at 16,000 Hz. The console's sound path has been proven at 16,000 Hz only, so this feed always
 * hands out 16,000 Hz blocks: each clip is decoded (the jf_audio.h arithmetic), an 8,000 Hz clip
 * is doubled with a 4-point interpolator ((-1, 9, 9, -1)/16 between samples), the two are mixed
 * in 16 bits and encoded again as IMA ADPCM in 256-byte blocks (4 header bytes, 2 samples a
 * byte). About 60 integer operations a sample: under 1 % of the ESP32.
 *
 * Pacing: the sink (a small ring ahead of the I2S output) takes what it has room for. The feed
 * stops when the sink takes less than offered, never offers more than real time plus
 * WX_SND_AHEAD bytes (so a sink that swallows everything cannot make it run away), and makes at
 * most WX_SND_CALL_BLOCKS blocks a call. It never waits.
 * Portable C99, no allocation. Part of wx.h; include that.
 */
#ifndef WX_SOUND_H
#define WX_SOUND_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "../jellyfin/jf_audio.h"
#include "wx_sound_data.h"

#define WX_AUDIO_RATE WX_SND_OUT_RATE  /* 16,000 Hz, mono, IMA ADPCM blocks of WX_SND_BLOCK bytes */
#define WX_SND_AHEAD 2048u             /* bytes the feed may run ahead of real time: the sink's ring (256 ms) */
#define WX_SND_CALL_BLOCKS 4u          /* blocks made per call at most (128 ms of sound) */
#define WX_SND_LINE_DELAY 3200u        /* samples from a page change to its line (0.2 s; the ring adds about 0.25 s) */
#define WX_SND_DUCK_STEP 16u           /* samples per step of the music's level: 179 steps, 0.18 s */

typedef size_t (*wx_audio_write)(void *ctx,const uint8_t *adpcm,size_t size,int block_start);

typedef struct {                 /* one clip being read */
    uint32_t pos;                /* next byte of the clip */
    uint16_t left;               /* samples left in the block */
    int16_t h[4];                /* the last four stored samples (for doubling) */
    jf_ima ima;
    uint8_t clip,loop,active,phase,nibble,hold;
} wx_voice;
typedef struct {
    wx_voice music,line;
    uint32_t t0_ms,last_ms;      /* pacing: bytes handed out since t0 */
    uint32_t fed;
    uint16_t delay;              /* samples until the pending line starts */
    uint16_t level;              /* the music's level now, of 256 */
    uint8_t pending;             /* clip asked for (0 = none; the music is clip 0 and never asked for) */
    uint8_t running,ramp;
    uint8_t out_len_hi;          /* 1: out holds a whole block not yet fully taken */
    uint16_t out_pos;
    int16_t enc_pred;int8_t enc_index;
    uint8_t out[WX_SND_BLOCK];
} wx_sound;

static inline void wx_voice_start(wx_voice *v,unsigned clip,int loop) {
    memset(v,0,sizeof(*v));
    v->clip=(uint8_t)clip;v->loop=(uint8_t)loop;v->active=1;
    jf_ima_init(&v->ima);
}
/* The clip's next stored sample; 0 once a clip that does not loop has ended (v->active drops). */
static inline int wx_voice_stored(wx_voice *v) {
    const wx_clip *c=&wx_clips[v->clip<WX_CLIPS?v->clip:0];
    if(!v->active)return 0;
    if(!v->left) { /* a block header: its first sample, and the step index */
        if(v->pos+WX_SND_BLOCK>c->bytes) {
            if(!v->loop||c->bytes<WX_SND_BLOCK){v->active=0;return 0;}
            v->pos=0;
        }
        const uint8_t *b=c->data+v->pos;
        int p=(int)b[0]|((int)b[1]<<8);
        v->ima.predictor=(int16_t)(p>=32768?p-65536:p);
        v->ima.index=(int8_t)(b[2]>88?88:b[2]);
        v->pos+=4;v->left=(uint16_t)(WX_SND_PER-1u);v->nibble=0;
        return v->ima.predictor;
    }
    v->left--;
    if(!v->nibble){v->hold=c->data[v->pos++];v->nibble=1;return jf_ima_nibble(&v->ima,v->hold&15u);}
    v->nibble=0;
    return jf_ima_nibble(&v->ima,(unsigned)v->hold>>4);
}
/* The clip's next sample at the output rate. */
static inline int wx_voice_next(wx_voice *v) {
    if(!wx_clips[v->clip<WX_CLIPS?v->clip:0].half)return wx_voice_stored(v);
    if(!v->phase) {
        v->h[0]=v->h[1];v->h[1]=v->h[2];v->h[2]=v->h[3];v->h[3]=(int16_t)wx_voice_stored(v);
        v->phase=1;
        return v->h[1];
    }
    v->phase=0;
    int s=(9*((int)v->h[1]+v->h[2])-v->h[0]-v->h[3]+8)>>4;
    return s>32767?32767:s<-32768?-32768:s;
}
static inline void wx_sound_reset(wx_sound *a,uint32_t now_ms) {
    memset(a,0,sizeof(*a));
    wx_voice_start(&a->music,WX_CLIP_MUSIC,1);
    a->level=256;a->t0_ms=a->last_ms=now_ms;
}
/* Ask for a line (WX_CLIP_CURRENT ...) or the tone; it starts WX_SND_LINE_DELAY samples later. */
static inline void wx_sound_say(wx_sound *a,unsigned clip){if(clip>0&&clip<WX_CLIPS){a->pending=(uint8_t)clip;a->delay=WX_SND_LINE_DELAY;}}
static inline int wx_sound_sample(wx_sound *a) {
    int target=256,line=0;
    if(a->pending) {
        target=(int)WX_SND_DUCK;
        if(a->delay)a->delay--;
        else{wx_voice_start(&a->line,a->pending,0);a->pending=0;}
    }
    if(a->line.active) {
        target=(int)WX_SND_DUCK;
        line=wx_voice_next(&a->line);
    }
    if(++a->ramp>=WX_SND_DUCK_STEP){a->ramp=0;if(a->level<target)a->level++;else if(a->level>target)a->level--;}
    int s=((wx_voice_next(&a->music)*(int)a->level)>>8)+line;
    return s>32767?32767:s<-32768?-32768:s;
}
static inline unsigned wx_ima_choose(int predictor,int index,int sample) {
    int step=jf_ima_steps[index],delta=sample-predictor;
    unsigned nibble=0;
    if(delta<0){nibble=8;delta=-delta;}
    if(delta>=step){nibble|=4;delta-=step;}
    if(delta>=step>>1){nibble|=2;delta-=step>>1;}
    if(delta>=step>>2)nibble|=1;
    return nibble;
}
static inline void wx_sound_block(wx_sound *a) {
    jf_ima enc;
    int first=wx_sound_sample(a);
    enc.predictor=(int16_t)first;enc.index=a->enc_index;enc.head=0;
    a->out[0]=(uint8_t)(first&255);a->out[1]=(uint8_t)((first>>8)&255);a->out[2]=(uint8_t)enc.index;a->out[3]=0;
    for(unsigned i=4;i<WX_SND_BLOCK;i++) {
        unsigned lo=wx_ima_choose(enc.predictor,enc.index,wx_sound_sample(a));
        jf_ima_nibble(&enc,lo);
        unsigned hi=wx_ima_choose(enc.predictor,enc.index,wx_sound_sample(a));
        jf_ima_nibble(&enc,hi);
        a->out[i]=(uint8_t)(lo|(hi<<4));
    }
    a->enc_index=enc.index;a->enc_pred=enc.predictor;
    a->out_len_hi=1;a->out_pos=0;
}
static inline void wx_sound_feed(wx_sound *a,uint32_t now_ms,wx_audio_write write,void *ctx) {
    if(!write)return;
    if(!a->running||(uint32_t)(now_ms-a->last_ms)>1000u) { /* the first call, or the caller was away: start the clock again */
        a->running=1;a->t0_ms=now_ms;a->fed=0;
    }
    a->last_ms=now_ms;
    /* Real time in bytes: 16 samples a millisecond, WX_SND_PER samples in WX_SND_BLOCK bytes (4096/505 a ms).
     * The counts are wound back every 505 s (exactly 4,096,000 bytes) so they stay small. */
    if((uint32_t)(now_ms-a->t0_ms)>=505000u){a->t0_ms+=505000u;a->fed=a->fed>=4096000u?a->fed-4096000u:0;}
    uint32_t allowed=(uint32_t)(now_ms-a->t0_ms)*(WX_AUDIO_RATE/1000u*WX_SND_BLOCK)/WX_SND_PER+WX_SND_AHEAD;
    for(unsigned made=0;;) {
        if(!a->out_len_hi) {
            if(made>=WX_SND_CALL_BLOCKS||a->fed+WX_SND_BLOCK>allowed)return;
            wx_sound_block(a);made++;
        }
        size_t want=WX_SND_BLOCK-a->out_pos,took=write(ctx,a->out+a->out_pos,want,a->out_pos==0);
        if(took>want)took=want;
        a->out_pos=(uint16_t)(a->out_pos+took);a->fed+=(uint32_t)took;
        if(a->out_pos>=WX_SND_BLOCK)a->out_len_hi=0;
        if(took<want)return; /* the sink is full: come back on the next pass */
    }
}
#endif
