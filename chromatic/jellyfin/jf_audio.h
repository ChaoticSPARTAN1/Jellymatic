/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * IMA ADPCM (WAV blocks, 4-bit, mono) for the Chromatic Jellyfin player.
 *
 * Streaming port of shared/ima_audio.h: bytes are fed as the network delivers them,
 * in any split, so no block has to be assembled first (a 1,024-byte block is 2,041
 * samples, 128 ms at 16 kHz). Each block starts with its own predictor and step index
 * (4 header bytes), so a lost block never poisons the next; jf_ima_block_start() marks
 * that boundary (the demuxer knows it from the '01wb' chunk start).
 *
 * The arithmetic is the IMA reference (diff = step>>3, plus step, step>>1, step>>2 for
 * the three magnitude bits, each truncated; header step index clipped to 0..88; the
 * header predictor is the first sample), the same as shared/ima_audio.h and as FFmpeg 9's
 * decoder, so the output is byte-identical to `ffmpeg -f s16le` on the same stream:
 * checked by scripts/test_chromatic_audio.py on a real server sample in 7 network splits.
 * (The multiply form ((2*delta+1)*step)>>3 found in older FFmpeg notes differs in the low
 * bits: measured on the sample, every second sample was off by 2-4.)
 *
 * Portable C, 8 bytes of state, about 12 instructions per sample: well under 1% of the
 * ESP32 at 16,000 samples/s. Used by the ESP32 audio task (jellyfin_esp32.c), by the
 * PC harness (check_app.c) and by the decoder check (check_audio.c).
 */
#ifndef JF_AUDIO_H
#define JF_AUDIO_H
#include <stddef.h>
#include <stdint.h>

#define JF_WAVE_IMA_ADPCM 0x0011u   /* WAVEFORMATEX wFormatTag */
#define JF_WAVE_MP3 0x0055u

typedef struct {
    int16_t predictor;
    int8_t index;
    uint8_t head;        /* header bytes still to take at a block start (4 -> 0) */
    uint8_t hdr[3];      /* the first three header bytes while the fourth is awaited */
} jf_ima;

static const int16_t jf_ima_steps[89]={
    7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,
    80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,
    494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,
    2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,
    8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,
    27086,29794,32767};
static const int8_t jf_ima_changes[8]={-1,-1,-1,-1,2,4,6,8};

static inline void jf_ima_init(jf_ima *s){s->predictor=0;s->index=0;s->head=0;}
/* The next bytes start a new block (its 4-byte header comes first). */
static inline void jf_ima_block_start(jf_ima *s){s->head=4;}

static inline int16_t jf_ima_nibble(jf_ima *s,unsigned nibble) {
    int step=jf_ima_steps[s->index],diff=step>>3;
    if(nibble&1u)diff+=step>>2;
    if(nibble&2u)diff+=step>>1;
    if(nibble&4u)diff+=step;
    int predictor=s->predictor+((nibble&8u)?-diff:diff);
    if(predictor>32767)predictor=32767;
    if(predictor<-32768)predictor=-32768;
    int index=s->index+jf_ima_changes[nibble&7u];
    s->index=(int8_t)(index<0?0:index>88?88:index);
    s->predictor=(int16_t)predictor;
    return s->predictor;
}
/* Decodes `size` stream bytes into `out` (capacity in samples, at least 2*size+1 to be
 * safe; a header byte yields 0 or 1 sample, a data byte 2). Returns samples written. */
static inline size_t jf_ima_feed(jf_ima *s,const uint8_t *data,size_t size,int16_t *out,size_t cap) {
    size_t n=0;
    for(size_t i=0;i<size;i++) {
        uint8_t b=data[i];
        if(s->head) {
            if(s->head>1)s->hdr[4-s->head]=b;
            if(--s->head==0) { /* the fourth header byte (reserved) completes the block header */
                int p=(int)s->hdr[0]|((int)s->hdr[1]<<8);
                s->predictor=(int16_t)(p>=32768?p-65536:p);
                s->index=(int8_t)(s->hdr[2]>88?88:s->hdr[2]);
                if(n<cap)out[n++]=s->predictor;
            }
            continue;
        }
        if(n+2>cap)break;
        out[n++]=jf_ima_nibble(s,b&15u);
        out[n++]=jf_ima_nibble(s,b>>4);
    }
    return n;
}
#endif
