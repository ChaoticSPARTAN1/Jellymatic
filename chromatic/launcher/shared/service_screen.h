/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The 160x144 service screens, in the menu's style (launcher_logic.h): black page,
 * tab bar, stock overlay accents, the project's Roboto atlas.
 *  - working: a spinning pinwheel with the percentage inside, the job, and
 *    "Keep USB connected. Don't switch off.";
 *  - still (FREEZE): drawn once and never again, so it is what stays up while the
 *    ESP32 is in its ROM bootloader. Nothing on it moves, and it says so;
 *  - finished: done, or stopped, then the console goes on to Games.
 *  - pairing: Remote Pilot asks to pair over USB; the code to compare, A to allow.
 * Previews: scripts/test_chromatic_remote.py writes staging/chromatic-launcher/ui-*.png.
 */
#ifndef PJ_SERVICE_SCREEN_H
#define PJ_SERVICE_SCREEN_H
#include "launcher_logic.h"
#include "service.h"

#define PJ_SVC_SPOKES 12u
#define PJ_SVC_CX 80
#define PJ_SVC_CY 60
/* Unit vectors x1024 for 12 o'clock and every 30 degrees clockwise (no libm needed). */
static const int16_t pj_svc_spoke_dir[PJ_SVC_SPOKES][2]={
    {0,-1024},{512,-887},{887,-512},{1024,0},{887,512},{512,887},{0,1024},{-512,887},{-887,512},{-1024,0},
    {-887,-512},{-512,-887}};
static const char *const pj_svc_titles[PJ_SVC_OP_COUNT]={
    "Checking the console","Backing up the console","Checking a backup","Preparing to install",
    "Preparing to update","Preparing to remove","Preparing to restore","Preparing Wi-Fi setup"};

/* Anti-aliased disc; centre and radius in 1/16 pixel, 4x4 samples per pixel. */
static inline void pj_svc_disc(uint16_t *fb,int cx16,int cy16,int r16,unsigned rgb) {
    int x0=(cx16-r16)/16-1,x1=(cx16+r16)/16+1,y0=(cy16-r16)/16-1,y1=(cy16+r16)/16+1;
    for(int y=y0;y<=y1;y++)for(int x=x0;x<=x1;x++) {
        if(x<0||x>=PJ_OSD_W||y<0||y>=PJ_OSD_H)continue;
        unsigned inside=0;
        for(int j=0;j<4;j++)for(int i=0;i<4;i++) {
            int dx=x*16+2+i*4-cx16,dy=y*16+2+j*4-cy16;
            if(dx*dx+dy*dy<=r16*r16)inside++;
        }
        if(inside)fb[y*PJ_OSD_W+x]=pj_osd_rgb(pj_osd_mix(rgb,pj_osd_unpack(fb[y*PJ_OSD_W+x]),inside*255u/16u));
    }
}
/* A line of discs from (x0,y0) to (x1,y1), pixels; used for the check mark. */
static inline void pj_svc_stroke(uint16_t *fb,int x0,int y0,int x1,int y1,int r16,unsigned rgb) {
    int dx=x1-x0,dy=y1-y0,steps=(dx<0?-dx:dx)+(dy<0?-dy:dy);
    if(steps<1)steps=1;
    for(int i=0;i<=steps*2;i++)pj_svc_disc(fb,x0*16+dx*16*i/(steps*2)+8,y0*16+dy*16*i/(steps*2)+8,r16,rgb);
}
static inline void pj_svc_center(uint16_t *fb,int y,const char *text,unsigned rgb,
                                 const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_text(fb,PJ_OSD_W/2-pj_osd_measure(text,advance)/2,y,text,rgb,alpha,advance);
}
static inline void pj_svc_header_titled(uint16_t *fb,unsigned accent,const char *title,const unsigned char *alpha,
                                        const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_chip(fb,6,3,accent);
    pj_osd_text(fb,24,4,title,accent,alpha,advance);
}
static inline void pj_svc_header(uint16_t *fb,unsigned accent,const unsigned char *alpha,const unsigned char *advance) {
    pj_svc_header_titled(fb,accent,"Chromatic Manager",alpha,advance);
}
/* Twelve spokes; the head is brightest and the rest fade behind it as it turns. */
static inline void pj_svc_pinwheel(uint16_t *fb,unsigned head,unsigned accent) {
    for(unsigned k=0;k<PJ_SVC_SPOKES;k++) {
        unsigned behind=(head+PJ_SVC_SPOKES-k)%PJ_SVC_SPOKES;
        unsigned level=behind>9u?40u:255u-behind*21u;
        unsigned rgb=pj_osd_mix(accent,0x2a2a35u,level);
        for(int r=15;r<=24;r+=3)
            pj_svc_disc(fb,PJ_SVC_CX*16+8+pj_svc_spoke_dir[k][0]*r/64,PJ_SVC_CY*16+8+pj_svc_spoke_dir[k][1]*r/64,
                        r>=21?40:34,rgb);
    }
}
static inline void pj_svc_bar(uint16_t *fb,unsigned pct,unsigned rgb) {
    pj_osd_rect(fb,8,134,144,3,0x2a2a35u);
    pj_osd_rect(fb,8,134,(int)(144u*(pct>100u?100u:pct)/100u),3,rgb);
}
static inline void pj_svc_draw_working(uint16_t *fb,const pj_svc *s,const unsigned char *alpha,const unsigned char *advance) {
    pj_svc_header(fb,PJ_UI_GAMES,alpha,advance);
    pj_svc_pinwheel(fb,s->spin%PJ_SVC_SPOKES,PJ_UI_GAMES);
    if(s->total||s->has_pc_pct) {
        char pct[8];snprintf(pct,sizeof(pct),"%u%%",s->pct>100u?100u:s->pct);
        pj_svc_center(fb,PJ_SVC_CY-5,pct,0xffffffu,alpha,advance);
        pj_svc_bar(fb,s->pct,PJ_UI_GAMES);
    }
    pj_svc_center(fb,92,pj_svc_titles[s->op%PJ_SVC_OP_COUNT],0xffffffu,alpha,advance);
    pj_svc_center(fb,106,"Keep USB connected.",PJ_UI_MUTED,alpha,advance);
    pj_svc_center(fb,119,"Don't switch off.",PJ_UI_NOTE,alpha,advance);
}
/* No spinner here on purpose: a spinner that stops looks like a lock-up. */
static inline void pj_svc_draw_still(uint16_t *fb,const pj_svc *s,const unsigned char *alpha,const unsigned char *advance) {
    pj_svc_header(fb,PJ_UI_NOTE,alpha,advance);
    /* A chip with an arrow going into it, drawn once. */
    pj_osd_rect(fb,66,38,28,22,PJ_UI_NOTE);pj_osd_rect(fb,70,42,20,14,PJ_UI_BAR);
    for(int i=0;i<4;i++){pj_osd_rect(fb,69+i*7,60,3,4,PJ_UI_NOTE);pj_osd_rect(fb,69+i*7,34,3,4,PJ_UI_NOTE);}
    pj_osd_rect(fb,78,22,4,12,0xffffffu);
    for(int i=0;i<5;i++)pj_osd_rect(fb,74+i,30+i,12-2*i,1,0xffffffu);
    pj_svc_center(fb,70,s->kind==PJ_SVC_KIND_WRITE?"Updating the console":"Checking the console",0xffffffu,alpha,advance);
    pj_svc_center(fb,86,"Keep USB connected.",PJ_UI_NOTE,alpha,advance);
    pj_svc_center(fb,99,"Don't switch off.",PJ_UI_NOTE,alpha,advance);
    pj_svc_center(fb,116,"This screen stays still",PJ_UI_MUTED,alpha,advance);
    pj_svc_center(fb,129,"until the PC says done.",PJ_UI_MUTED,alpha,advance);
}
static inline void pj_svc_draw_finished(uint16_t *fb,const pj_svc *s,const unsigned char *alpha,const unsigned char *advance) {
    int ok=s->end_reason==PJ_SVC_END_OK;
    pj_svc_header(fb,ok?PJ_UI_SAFE:PJ_UI_NOTE,alpha,advance);
    if(ok) {
        pj_svc_stroke(fb,66,60,75,69,40,PJ_UI_SAFE);pj_svc_stroke(fb,75,69,94,48,40,PJ_UI_SAFE);
        pj_svc_center(fb,88,"Done",0xffffffu,alpha,advance);
        pj_svc_bar(fb,100,PJ_UI_SAFE);
    } else {
        pj_osd_rect(fb,78,40,4,18,PJ_UI_NOTE);pj_osd_rect(fb,78,62,4,4,PJ_UI_NOTE);
        pj_svc_center(fb,82,"Manager stopped",0xffffffu,alpha,advance);
        pj_svc_center(fb,96,"See the PC for details.",PJ_UI_MUTED,alpha,advance);
    }
    pj_svc_center(fb,114,"Starting Games...",PJ_UI_MUTED,alpha,advance);
}
/* Pairing over USB: the owner compares the code with the PC's and allows it with A. */
static inline void pj_pair_draw(uint16_t *fb,const char *code,unsigned seconds_left,
                                const unsigned char *alpha,const unsigned char *advance) {
    pj_svc_header_titled(fb,PJ_UI_JELLYFIN,"Remote Pilot",alpha,advance);
    pj_svc_center(fb,26,"Pair with this PC?",0xffffffu,alpha,advance);
    char spaced[8]={code[0],code[1],code[2],' ',code[3],code[4],code[5],0};
    pj_osd_rect(fb,30,44,100,30,PJ_UI_EDGE);pj_osd_rect(fb,31,45,98,28,PJ_UI_ROW);
    /* The code twice as large: each glyph drawn 2x2 from the atlas. */
    int width=pj_osd_measure(spaced,advance)*2,x0=PJ_OSD_W/2-width/2;
    for(unsigned i=0;spaced[i];i++) {
        unsigned glyph=(unsigned)spaced[i]-32u;
        for(int row=0;row<11;row++)for(int col=0;col<13;col++) {
            unsigned a=alpha[glyph*143u+(unsigned)row*13u+(unsigned)col];
            if(a)pj_osd_rect(fb,x0+col*2,48+row*2,2,2,pj_osd_mix(0xffffffu,0x15151cu,a));
        }
        x0+=advance[glyph]*2;
    }
    pj_svc_center(fb,80,"Same code on the PC?",PJ_UI_MUTED,alpha,advance);
    pj_svc_center(fb,96,"A: allow    B: no",PJ_UI_NOTE,alpha,advance);
    pj_svc_center(fb,112,"Only pair your own PC.",PJ_UI_MUTED,alpha,advance);
    pj_osd_rect(fb,8,134,144,3,0x2a2a35u);
    pj_osd_rect(fb,8,134,(int)(144u*(seconds_left>60u?60u:seconds_left)/60u),3,PJ_UI_JELLYFIN);
}
/* The screen for the session's current state. */
static inline void pj_svc_draw(uint16_t *fb,const pj_svc *s,const unsigned char *alpha,const unsigned char *advance) {
    if(s->state==PJ_SVC_FROZEN)pj_svc_draw_still(fb,s,alpha,advance);
    else if(s->state==PJ_SVC_FINISHING)pj_svc_draw_finished(fb,s,alpha,advance);
    else pj_svc_draw_working(fb,s,alpha,advance);
}
#endif
