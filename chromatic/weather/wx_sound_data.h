/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel sound: clip table. WRITTEN BY chromatic/weather/tools/make_sound.py. */
#ifndef WX_SOUND_DATA_H
#define WX_SOUND_DATA_H
#include <stdint.h>
#define WX_SND_OUT_RATE 16000u /* what wx_sound.h plays; a clip is stored at this rate or at half of it */
#define WX_SND_BLOCK 256u     /* bytes a block: 4 header bytes, then 2 samples a byte */
#define WX_SND_PER 505u       /* samples a block */
#define WX_SND_DUCK 77u       /* the bed under a line, of 256 */
enum {WX_CLIP_MUSIC,WX_CLIP_CURRENT,WX_CLIP_LOCAL,WX_CLIP_EXTENDED,WX_CLIP_REGIONAL,WX_CLIP_RADAR,WX_CLIP_ALERT,WX_CLIPS};
typedef struct {const uint8_t *data;uint32_t bytes,samples;uint8_t half;} wx_clip; /* half: stored at 8,000 Hz */
extern const wx_clip wx_clips[WX_CLIPS];
#define WX_SND_BYTES 239104u
#endif
