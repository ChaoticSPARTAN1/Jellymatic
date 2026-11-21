/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC previews of the launcher 1.6.0 start-up menu and of the Jellyfin screen's lines for the TV and ChromaTube rows,
 * drawn by the launcher's own code (shared/launcher_logic.h). Run by scripts/test_chromatic_tv.py:
 *   preview_menu <folder>   writes <folder>/<name>.ppm (160x144)
 * Also checks that every line shown for the new rows fits the screen. No hardware. */
#include <stdio.h>
#include <stdlib.h>
#include "shared/launcher_logic.h"
#include "../../pocket/ui_font.h"

static uint16_t screen[PJ_OSD_W*PJ_OSD_H];
static void write_ppm(const char *folder,const char *name) {
    char path[600];snprintf(path,sizeof(path),"%s/%s.ppm",folder,name);
    FILE *f=fopen(path,"wb");
    if(!f){fprintf(stderr,"cannot write %s\n",path);exit(1);}
    fprintf(f,"P6\n%d %d\n255\n",PJ_OSD_W,PJ_OSD_H);
    for(int i=0;i<PJ_OSD_W*PJ_OSD_H;i++) {
        uint16_t c=screen[i];
        if(c==PJ_OSD_KEY){fprintf(stderr,"%s: the transparent key colour was drawn\n",name);exit(1);}
        uint8_t px[3]={(uint8_t)((c>>11)<<3|(c>>13)),(uint8_t)(((c>>5)&63)<<2|((c>>9)&3)),(uint8_t)((c&31)<<3|((c>>2)&7))};
        fwrite(px,1,3,f);
    }
    fclose(f);
}
static void media(const char *folder,const char *name,const char *a,const char *b,const char *c) {
    const char *const lines[3]={a,b,c};
    for(int i=0;i<3;i++)if(12+pj_osd_measure(lines[i],pj_font_advance)>156){fprintf(stderr,"%s: \"%s\" does not fit\n",name,lines[i]);exit(1);}
    pj_launch_draw_media(screen,lines,pj_font_alpha,pj_font_advance);write_ppm(folder,name);
}
static void opening(const char *folder,const char *name,unsigned item,const char *line,const char *trouble) {
    if(pj_osd_measure(line,pj_font_advance)>152||(trouble&&pj_osd_measure(trouble,pj_font_advance)>152)){fprintf(stderr,"%s: a line does not fit\n",name);exit(1);}
    pj_launch_draw_opening(screen,item,line,trouble,pj_font_alpha,pj_font_advance);write_ppm(folder,name);
}
int main(int argc,char **argv) {
    if(argc!=2)return 2;
    static const char *const names[PJ_ITEM_COUNT]={"menu-games-selected","menu-jellyfin-selected","menu-tv-selected",
        "menu-chromatube-selected","menu-other-firmware-selected"};
    pj_launcher l;pj_launch_init(&l,0);
    for(unsigned i=0;i<PJ_ITEM_COUNT;i++) {
        l.selected=(pj_launch_item)i;
        pj_launch_draw_menu(screen,&l,0,pj_font_alpha,pj_font_advance);write_ppm(argv[1],names[i]);
    }
    /* TV or ChromaTube chosen on a console that is not set up: the Jellyfin row's own lines (launcher_main.c) */
    media(argv[1],"not-set-up-no-wifi","Wi-Fi: press A to check","Remote Pilot: USB","Select: set up Wi-Fi here");
    media(argv[1],"not-set-up-no-sign-in","Wi-Fi: Home network","Remote Pilot: 192.168.1.50","Select: sign in to Jellyfin");
    /* TV: between the app and the Local Weather channel (the app's memory goes back first), and the app starting */
    opening(argv[1],"tv-opening-local-weather",PJ_ITEM_TV,"Opening Local Weather...",NULL);
    opening(argv[1],"tv-opening",PJ_ITEM_TV,"Opening TV...",NULL);
    opening(argv[1],"tv-opening-no-memory",PJ_ITEM_TV,"Opening TV...","TV: not enough memory");
    opening(argv[1],"chromatube-opening",PJ_ITEM_TUBE,"Opening ChromaTube...",NULL);
    opening(argv[1],"chromatube-opening-failed",PJ_ITEM_TUBE,"Opening ChromaTube...","ChromaTube could not start");
    /* the Local Weather channel's 3 s banner (over a plain page here: the channel's own pages are the weather suite's) */
    pj_osd_fill(screen,0x1c2a6eu);pj_wx_banner(screen,pj_font_alpha,pj_font_advance);write_ppm(argv[1],"tv-local-weather-banner");
    if(pj_osd_measure("B: channels   Up/Down: TV",pj_font_advance)>148){fprintf(stderr,"the weather banner does not fit\n");exit(1);}
    puts("PASS: start-up menu and Jellyfin-screen previews drawn by the launcher's code; every line fits");
    return 0;
}
