/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.3.0: the "Pocket link" screen, in the launcher's pixel look (the Jellyfin tab bar, rows and colours of
 * setup_ui.h). One plain-words status line with a coloured light, then the next step for the owner:
 *   waiting   (yellow)  plug the cable in, open Jellyfin on the Pocket
 *   connected (green)   the Pocket can browse and play from here
 *   syncing N of M (yellow, with a bar)  pictures are being copied to the Pocket
 *   streaming (green)   a video plays on the Pocket
 *   unplugged (red)     no word from the Pocket for 9 s: plug the cable back in
 *   stopped   (red)     the link mode ended (why, in words); the launcher tries again by itself
 * The state comes from pocket_link.h pj_plink_view. No hardware calls; drawn by the shell, previewed by
 * chromatic/launcher/check_launcher.c. */
#ifndef PJ_POCKET_LINK_SCREEN_H
#define PJ_POCKET_LINK_SCREEN_H
#include <stdio.h>
#include "launcher_logic.h"
#include "pocket_link.h"
#define PJ_PLS_RED 0xff6666u
/* The status line and its light colour. */
static inline unsigned pj_plink_words(const pj_plink_status *s,char *out,size_t size) {
    switch(s->what) {
    case PJ_PLV_CONNECTED:
        if(s->slow){snprintf(out,size,"Pocket link: slow");return PJ_UI_NOTE;} /* 1.3.5: fast UART failed, 115,200 kept */
        snprintf(out,size,"Pocket link: connected");return PJ_UI_SAFE;
    case PJ_PLV_SYNCING:snprintf(out,size,"Syncing %u of %u",(unsigned)s->done,(unsigned)s->total);return PJ_UI_NOTE;
    case PJ_PLV_STREAMING:snprintf(out,size,"Pocket link: streaming");return PJ_UI_SAFE;
    case PJ_PLV_UNPLUGGED:snprintf(out,size,"Pocket link: unplugged");return PJ_PLS_RED;
    case PJ_PLV_STOPPED:snprintf(out,size,"Pocket link: stopped");return PJ_PLS_RED;
    default:snprintf(out,size,"Pocket link: waiting");return PJ_UI_NOTE;
    }
}
/* What failed, in words the owner can act on (pocket_link.h `why` strings are short and technical). */
static inline const char *pj_plink_why_words(const char *why) {
    if(!why||!why[0])return "The link stopped.";
    if(!strcmp(why,"FPGA did not enter link mode"))return "The console refused the link.";
    if(!strcmp(why,"no reply from the FPGA bridge"))return "No reply from the console.";   /* 1.3.5: wrong image or UART */
    if(!strcmp(why,"fast UART not confirmed")||!strcmp(why,"fast UART request not sent"))return "The fast connection failed.";
    if(!strcmp(why,"FPGA status stale"))return "The console stopped answering.";
    if(!strcmp(why,"FPGA status malformed"))return "The console answered oddly.";
    if(!strcmp(why,"FPGA UART busy"))return "The console's line was busy.";
    if(!strcmp(why,"link fault"))return "The cable connection failed.";
    if(!strcmp(why,"link mode ended"))return "Link mode was switched off.";
    return "The link stopped.";
}
/* 1.3.5: the stage a stopped link failed in, as the third line ("stage 1 of 5: entering"), so a future failure says
 * where, not only why. The numbers match pj_plink_stage and the launcher's "plink stage=" diagnostics line. */
static inline const char *pj_plink_stage_words(const char *why) {
    if(!why||!why[0])return "";
    if(!strcmp(why,"FPGA did not enter link mode")||!strcmp(why,"no reply from the FPGA bridge")||!strcmp(why,"FPGA UART busy"))return "Stage 1 of 5: entering.";
    if(!strcmp(why,"fast UART not confirmed")||!strcmp(why,"fast UART request not sent"))return "Stage 2 of 5: fast UART.";
    if(!strcmp(why,"FPGA status stale")||!strcmp(why,"FPGA status malformed")||!strcmp(why,"link mode ended"))return "Stage 3 of 5: console link.";
    if(!strcmp(why,"link fault"))return "Stage 4 of 5: cable.";
    return "";
}
/* 1.3.6: what the link has heard from the Pocket, as one line that always fits the screen: "Heard 21 bytes: 1 good,
 * 0 bad" (good and bad are whole messages; bad counts refused and torn ones). Large numbers are shortened (12k, 3M)
 * and the words give way before the line may run past 144 pixels. */
static inline void pj_plink_count_words(uint32_t value,char *out,size_t size) {
    if(value<10000u)snprintf(out,size,"%lu",(unsigned long)value);
    else if(value<10000000u)snprintf(out,size,"%luk",(unsigned long)(value/1000u));
    else snprintf(out,size,"%luM",(unsigned long)(value/1000000u));
}
static inline void pj_plink_heard_words(const pj_plink_status *s,char *out,size_t size,const unsigned char *advance) {
    static const char *const form[3]={"Heard %s bytes: %s good, %s bad","Heard %s: %s good, %s bad","%s: %s good, %s bad"};
    char heard[8],good[8],bad[8];
    pj_plink_count_words(s->heard,heard,sizeof(heard));pj_plink_count_words(s->good,good,sizeof(good));
    pj_plink_count_words(s->bad,bad,sizeof(bad));
    for(unsigned i=0;i<3;i++){snprintf(out,size,form[i],heard,good,bad);if(pj_osd_measure(out,advance)<=144)break;}
}
static inline void pj_plink_draw(uint16_t *fb,const pj_plink_status *s,uint32_t now,
                                 const unsigned char *alpha,const unsigned char *advance) {
    char line[48];unsigned light=pj_plink_words(s,line,sizeof(line));
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_jellyfin(fb,6,3,0);pj_osd_text(fb,24,4,"Pocket link",PJ_UI_JELLYFIN,alpha,advance);
    /* the status row: a light, the words; the light blinks while something is under way */
    pj_osd_rect(fb,8,24,144,20,light);pj_osd_rect(fb,9,25,142,18,pj_osd_mix(light,0x101016u,40u));
    int blink=(s->what==PJ_PLV_WAITING||s->what==PJ_PLV_SYNCING)&&((now/500u)&1u);
    pj_osd_rect(fb,14,30,8,8,blink?PJ_UI_EDGE:light);
    pj_osd_text(fb,28,28,line,0xffffffu,alpha,advance);
    const char *a="",*b="",*c="";
    switch(s->what) {
    case PJ_PLV_WAITING:a="1. Plug the link cable into";b="   both consoles.";c="2. Open Jellyfin on the Pocket.";break;
    case PJ_PLV_CONNECTED:
        if(s->slow){a="The fast connection failed:";b="browse and pictures only,";c="no playing on this link.";}
        else{a="The Pocket can browse your";b="library and play from here.";c="Keep this screen open.";}
        break;
    case PJ_PLV_SYNCING:a="Copying pictures to the";b="Pocket for offline browsing.";c="";break;
    case PJ_PLV_STREAMING:a="Playing on the Pocket.";b="Keep this screen open.";c="";break;
    case PJ_PLV_UNPLUGGED:a="Plug the cable back in.";b="The Pocket keeps what";c="it has saved.";break;
    default:a=pj_plink_why_words(s->why);b="Trying again in a moment.";c=pj_plink_stage_words(s->why);break;
    }
    const char *back="B: back to Jellyfin";
    if(s->what==PJ_PLV_WAITING) {
        /* 1.3.6: while waiting, the three steps move up and three small lines under a rule say what the cable has
         * carried so far: the count, then one plain sentence from the link diagnostics (pocket_link_diag.h). */
        char one[40],two[40];
        pj_osd_text(fb,8,47,a,0xd0d0d0u,alpha,advance);pj_osd_text(fb,8,59,b,0xd0d0d0u,alpha,advance);
        pj_osd_text(fb,8,71,c,0xd0d0d0u,alpha,advance);
        pj_osd_rect(fb,8,84,144,1,PJ_UI_EDGE);
        pj_plink_heard_words(s,line,sizeof(line),advance);pj_pld_sentence(s->hint,one,sizeof(one),two,sizeof(two));
        pj_osd_text(fb,8,86,line,PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,97,one,0xd0d0d0u,alpha,advance);
        if(two[0])pj_osd_text(fb,8,108,two,0xd0d0d0u,alpha,advance);
        pj_osd_rect(fb,12,122,136,20,0x2e2e3au);pj_osd_rect(fb,13,123,134,18,0x1c1c24u);
        pj_osd_text(fb,80-pj_osd_measure(back,advance)/2,126,back,0xffffffu,alpha,advance);
        return;
    }
    pj_osd_text(fb,8,54,a,0xd0d0d0u,alpha,advance);pj_osd_text(fb,8,68,b,0xd0d0d0u,alpha,advance);
    if(c[0])pj_osd_text(fb,8,82,c,0xd0d0d0u,alpha,advance);
    if(s->what==PJ_PLV_SYNCING&&s->total) { /* a pixel progress bar */
        unsigned w=s->done>=s->total?140u:140u*s->done/s->total;
        pj_osd_rect(fb,8,86,144,8,PJ_UI_EDGE);pj_osd_rect(fb,10,88,(int)w,4,PJ_UI_NOTE);
    }
    if(s->what==PJ_PLV_STOPPED) { /* 1.3.6: a stopped link keeps the count in view while it tries again */
        pj_plink_heard_words(s,line,sizeof(line),advance);pj_osd_text(fb,8,96,line,PJ_UI_MUTED,alpha,advance);
    }
    pj_osd_rect(fb,12,111,136,24,0x2e2e3au);pj_osd_rect(fb,13,112,134,22,0x1c1c24u);
    pj_osd_text(fb,80-pj_osd_measure(back,advance)/2,118,back,0xffffffu,alpha,advance);
}
#endif
