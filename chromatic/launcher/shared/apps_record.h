/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.6.4: the apps record. Which optional apps the start-up menu shows (TV, ChromaTube) and whether TV
 * lists the Jellyfin server's Live TV channels or only its built-in Local Weather channel.
 *
 * Where: two 4 KiB sectors inside the launcher slot, 0x3FA000 (A) and 0x3FB000 (B): the pair
 * reserved for TV settings (docs/Design-notes.md), below the weather location (0x3FC000/0x3FD000, wx_place.h) and the setup record
 * (0x3FE000/0x3FF000, provision.h). Chromatic Manager's launcher area (SLOT_APP) ends at 0x3FA000, so installs,
 * updates, downgrades and switch-off keep it; Remove and Restore erase it. Launchers before 1.6.4 never read it.
 * Python twin (the Manager's writer): scripts/chromatic_apps.py; scripts/test_chromatic_apps.py checks they agree.
 *
 * Layout, little-endian, 32 bytes, then erased bytes to the end of the sector:
 *    0 "PJAP" | 4 u8 version (1) | 5 u8 0 | 6 u16 length (32) | 8 u32 sequence (never 0) |
 *   12 u32 flags (PJ_APPS_* only) | 16..27 zero | 28 u32 CRC-32 (IEEE) of bytes 0..27
 * The newer valid copy wins (sequences compared as an int32 distance, as wx_place.h does; A when equal). Absent,
 * blank, torn, corrupt, another version, another length, a non-zero reserved byte or an unknown flag: the copy is
 * not used. Neither copy usable: PJ_APPS_DEFAULTS (TV with only Local Weather; ChromaTube hidden).
 *
 * Written by Chromatic Manager (both copies, A then B, journalled, checked on the chip) and, on the console, by the
 * start-up menu's own Hide / Show hidden apps (provision_write.c pj_apps_commit_with: the setup record's proven
 * pattern, one sector at a time). No secret and nothing personal is in it. */
#ifndef PJ_APPS_RECORD_H
#define PJ_APPS_RECORD_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define PJ_APPS_FLASH_A 0x3FA000u
#define PJ_APPS_FLASH_B 0x3FB000u
#define PJ_APPS_SECTOR 4096u
#define PJ_APPS_SIZE 32u
#define PJ_APPS_VERSION 1u
#define PJ_APPS_TV (1u<<0)     /* the TV row is on the start-up menu */
#define PJ_APPS_TUBE (1u<<1)   /* the ChromaTube row is on the start-up menu */
#define PJ_APPS_LIVETV (1u<<2) /* TV lists the server's Live TV channels (else it opens straight to Local Weather) */
#define PJ_APPS_KNOWN (PJ_APPS_TV|PJ_APPS_TUBE|PJ_APPS_LIVETV)
#define PJ_APPS_DEFAULTS PJ_APPS_TV /* a new console: TV with the built-in Local Weather channel only, no ChromaTube */
typedef struct {uint32_t flags,sequence;} pj_apps;

static inline uint32_t pj_apps_crc32(const uint8_t *data,size_t size) {
    uint32_t crc=0xffffffffu;
    for(size_t i=0;i<size;i++) {
        crc^=data[i];
        for(int bit=0;bit<8;bit++)crc=(crc>>1)^(0xedb88320u&(0u-(crc&1u)));
    }
    return crc^0xffffffffu;
}
static inline uint32_t pj_apps_u32(const uint8_t *p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static inline void pj_apps_put32(uint8_t *p,uint32_t v){p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8);p[2]=(uint8_t)(v>>16);p[3]=(uint8_t)(v>>24);}
/* One copy: 1 valid (into *out), 0 erased (its 32 bytes all 0xFF), -1 anything else. */
static inline int pj_apps_parse(const uint8_t *raw,size_t size,pj_apps *out) {
    if(!raw||size<PJ_APPS_SIZE)return -1;
    unsigned erased=1;
    for(unsigned i=0;i<PJ_APPS_SIZE;i++)if(raw[i]!=0xffu){erased=0;break;}
    if(erased)return 0;
    if(memcmp(raw,"PJAP",4)||raw[4]!=PJ_APPS_VERSION||raw[5]||(raw[6]|(raw[7]<<8))!=(int)PJ_APPS_SIZE)return -1;
    uint32_t sequence=pj_apps_u32(raw+8),flags=pj_apps_u32(raw+12);
    if(!sequence||(flags&~PJ_APPS_KNOWN))return -1;
    for(unsigned i=16;i<28;i++)if(raw[i])return -1;
    if(pj_apps_crc32(raw,28)!=pj_apps_u32(raw+28))return -1;
    out->flags=flags;out->sequence=sequence;
    return 1;
}
/* The record in force: the newer valid copy (B only when (int32_t)(b-a)>0), A when equal. 1 found; 0 none, and
 * then *out holds the defaults with sequence 0. *in_b (may be NULL): 1 when the copy in force is B. */
static inline int pj_apps_current(const uint8_t *a,const uint8_t *b,size_t size,pj_apps *out,int *in_b) {
    pj_apps ra={0,0},rb={0,0};
    int ga=pj_apps_parse(a,size,&ra)==1,gb=pj_apps_parse(b,size,&rb)==1;
    int use_b=gb&&(!ga||(int32_t)(rb.sequence-ra.sequence)>0);
    if(in_b)*in_b=use_b;
    if(use_b)*out=rb;
    else if(ga)*out=ra;
    else{out->flags=PJ_APPS_DEFAULTS;out->sequence=0;}
    return ga||gb;
}
/* The record bytes for `flags` with `sequence` (never 0); the rest of the sector is the caller's (erased). */
static inline int pj_apps_build(uint32_t flags,uint32_t sequence,uint8_t out[PJ_APPS_SIZE]) {
    if(!sequence||(flags&~PJ_APPS_KNOWN))return 0;
    memset(out,0,PJ_APPS_SIZE);
    memcpy(out,"PJAP",4);out[4]=PJ_APPS_VERSION;out[6]=(uint8_t)PJ_APPS_SIZE;
    pj_apps_put32(out+8,sequence);pj_apps_put32(out+12,flags);pj_apps_put32(out+28,pj_apps_crc32(out,28));
    return 1;
}
static inline uint32_t pj_apps_next_sequence(uint32_t sequence){return sequence+1u?sequence+1u:1u;}
#endif
