/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * 160x144 Jellyfin screens for the Chromatic: the Pocket app's layout (pocket/live_ui.h,
 * which follows the real Jellyfin client) scaled from 320x240. Same colours, hero backdrop
 * fading into the page, row title with the next row's name on the right, cards with the
 * light focus outline and rounded corners, resume bars and played ticks, details with
 * Resume / From start, and the playback strip below the picture. Drawing helpers, font and
 * Jellyfin mark come from the launcher (launcher_logic.h), so the app matches the menu.
 * Included by jf_app.h only.
 */
#ifndef JF_UI_H
#define JF_UI_H
#include "../launcher/shared/launcher_logic.h"

#define JF_PAGE 0x101010u    /* pj_ui: PJ_UI_PAGE */
#define JF_CARD 0x202020u    /* PJ_UI_CARD */
#define JF_ACCENT 0x00a4dcu  /* PJ_UI_ACCENT */
#define JF_INK 0xffffffu     /* PJ_UI_INK */
#define JF_MUTED 0xb3b3b3u   /* PJ_UI_MUTED */
#define JF_FOCUS 0xe0e0e0u   /* PJ_UI_FOCUS */

static inline void jf_text(const jf_app *a,uint16_t *fb,int x,int y,const char *t,unsigned rgb) {
    pj_osd_text(fb,x,y,t,rgb,a->cfg.font_alpha,a->cfg.font_advance);
}
static inline int jf_measure(const jf_app *a,const char *t){return pj_osd_measure(t,a->cfg.font_advance);}
/* One line cut to `width` (the Pocket's pj_ui_text maxwidth). */
static inline void jf_text_fit(const jf_app *a,uint16_t *fb,int x,int y,int width,const char *t,unsigned rgb) {
    char line[64];jf_copy(line,sizeof(line),t);
    while(line[0]&&jf_measure(a,line)>width){size_t n=strlen(line);line[n-1]=0;}
    jf_text(a,fb,x,y,line,rgb);
}
static inline void jf_shadow(const jf_app *a,uint16_t *fb,int x,int y,int width,const char *t,unsigned rgb) {
    jf_text_fit(a,fb,x+1,y+1,width,t,0x000000u);jf_text_fit(a,fb,x,y,width,t,rgb);
}
/* Text shifted left by `offset` and clipped to the columns [left, right): a scrolling name. */
static inline void jf_text_clip(const jf_app *a,uint16_t *fb,int x,int y,int left,int right,const char *t,unsigned rgb) {
    const unsigned char *alpha=a->cfg.font_alpha,*advance=a->cfg.font_advance;
    if(left<0)left=0;
    if(right>JF_W)right=JF_W;
    for(unsigned i=0;t[i]&&x<right;i++) {
        unsigned c=(unsigned char)t[i];if(c<32||c>126)c='?';
        unsigned glyph=c-32;
        if(x+13>left)for(int row=0;row<11;row++)for(int col=0;col<13;col++) {
            int px=x+col,py=y+row;
            if(px<left||px>=right||py<0||py>=JF_H)continue;
            unsigned v=alpha[glyph*143u+(unsigned)row*13u+(unsigned)col];
            if(v)fb[py*JF_W+px]=pj_osd_rgb(pj_osd_mix(rgb,pj_osd_unpack(fb[py*JF_W+px]),v));
        }
        x+=advance[glyph];
    }
}
/* The 13x11 font resampled to num/den of its size (bilinear on the glyph coverage), so the
 * hero title can sit a little smaller than body text without a second font in memory. */
static inline void jf_text_scaled(const jf_app *a,uint16_t *fb,int x,int y,int width,const char *t,unsigned rgb,unsigned num,unsigned den) {
    const unsigned char *alpha=a->cfg.font_alpha,*advance=a->cfg.font_advance;
    const int ow=(int)((13u*num+den-1u)/den),oh=(int)((11u*num+den-1u)/den);
    uint32_t pen=0; /* 24.8 fixed point */
    for(unsigned i=0;t[i];i++) {
        unsigned c=(unsigned char)t[i];if(c<32||c>126)c='?';
        unsigned glyph=c-32;uint32_t step=(uint32_t)advance[glyph]*num*256u/den;
        if((int)((pen+step)>>8)>width)break;
        const unsigned char *g=alpha+glyph*143u;int gx=x+(int)(pen>>8);
        for(int oy=0;oy<oh;oy++) {
            int sy=(int)(((2u*(unsigned)oy+1u)*den*128u)/num)-128,y0=sy>=0?sy>>8:-1,fy=sy&255;
            for(int ox=0;ox<ow;ox++) {
                int sx=(int)(((2u*(unsigned)ox+1u)*den*128u)/num)-128,x0=sx>=0?sx>>8:-1,fx=sx&255;
                unsigned tap[4]={0,0,0,0};
                for(int k=0;k<4;k++){int cx=x0+(k&1),cy=y0+(k>>1);if(cx>=0&&cx<13&&cy>=0&&cy<11)tap[k]=g[cy*13+cx];}
                unsigned v=((tap[0]*(256u-(unsigned)fx)+tap[1]*(unsigned)fx)*(256u-(unsigned)fy)+(tap[2]*(256u-(unsigned)fx)+tap[3]*(unsigned)fx)*(unsigned)fy)>>16;
                int px=gx+ox,py=y+oy;
                if(v&&px>=0&&px<JF_W&&py>=0&&py<JF_H)fb[py*JF_W+px]=pj_osd_rgb(pj_osd_mix(rgb,pj_osd_unpack(fb[py*JF_W+px]),v>255u?255u:v));
            }
        }
        pen+=step;
    }
}
/* The focused card's name: whole when it fits, else shifted by the scroll and clipped to the
 * card's width. The strip is repainted first, so a label-only redraw (JF_DRAW_LABEL) gives
 * exactly the pixels a full redraw would. */
static inline void jf_draw_focus_label(const jf_app *a,uint16_t *fb,const jf_label *l) {
    pj_osd_rect(fb,l->x,l->y,l->w,11,JF_PAGE);
    if(!l->over)jf_text_fit(a,fb,l->x,l->y,l->w,l->text,JF_INK);
    else jf_text_clip(a,fb,l->x-a->scroll_px,l->y,l->x,l->x+l->w,l->text,JF_INK);
}
/* Word wrap into at most `lines` rows of 12 px (pj_ui_paragraph). */
static inline void jf_paragraph(const jf_app *a,uint16_t *fb,int x,int y,int width,unsigned lines,const char *t,unsigned rgb) {
    const char *p=t;
    for(unsigned line=0;line<lines&&*p;line++) {
        while(*p==' ')p++;
        char row[64];size_t used=0,last_space=0;
        while(p[used]&&used<sizeof(row)-1){row[used]=p[used];row[used+1]=0;if(jf_measure(a,row)>width)break;if(p[used]==' ')last_space=used;used++;}
        if(p[used]&&last_space&&line+1<lines)used=last_space;
        row[used]=0;
        if(line+1==lines&&p[used])jf_text_fit(a,fb,x,y,width,p,rgb);else jf_text(a,fb,x,y,row,rgb);
        p+=used;y+=12;
    }
}
static inline void jf_clock(char *out,size_t cap,uint32_t s) {
    int n=s>=3600?snprintf(out,cap,"%u:%02u:%02u",(unsigned)(s/3600%1000),(unsigned)(s/60%60),(unsigned)(s%60))
                 :snprintf(out,cap,"%u:%02u",(unsigned)(s/60),(unsigned)(s%60));
    (void)n;
}
static inline void jf_duration(char *out,size_t cap,uint32_t s) {
    unsigned m=s/60;
    if(!m)out[0]=0;else if(m<60)snprintf(out,cap,"%um",m);else snprintf(out,cap,"%uh %um",m/60%1000,m%60);
}
static inline void jf_meta(char *out,size_t cap,const jf_item *it) {
    char d[16],y[8]="";jf_duration(d,sizeof(d),it->runtime_s);
    if(it->year)snprintf(y,sizeof(y),"%u",(unsigned)it->year);
    snprintf(out,cap,"%s%s%s",y,y[0]&&d[0]?"   ":"",d);
}
/* pj_ui_round: page-coloured corner pixels. */
static inline void jf_round(uint16_t *fb,int x,int y,int w,int h) {
    pj_osd_rect(fb,x,y,1,1,JF_PAGE);pj_osd_rect(fb,x+w-1,y,1,1,JF_PAGE);
    pj_osd_rect(fb,x,y+h-1,1,1,JF_PAGE);pj_osd_rect(fb,x+w-1,y+h-1,1,1,JF_PAGE);
}
static inline void jf_outline(uint16_t *fb,int x,int y,int w,int h,unsigned rgb) {
    pj_osd_rect(fb,x,y,w,1,rgb);pj_osd_rect(fb,x,y+h-1,w,1,rgb);pj_osd_rect(fb,x,y,1,h,rgb);pj_osd_rect(fb,x+w-1,y,1,h,rgb);
}
static inline int jf_draw_art(const jf_app *a,uint16_t *fb,int x,int y,const char *id,uint8_t kind,uint8_t shape) {
    const jf_art *s=kind?jf_art_find(a,id,kind,shape):NULL;
    int w,h;jf_shape_size(shape,&w,&h);
    if(!s||!s->ok||x<0||y<0||x+w>JF_W||y+h>JF_H)return 0;
    return a->io.decode(a->io.ctx,a->art_bytes+s->off,s->len,fb+y*JF_W+x,JF_W,w,h)==0;
}
/* pj_ui_card: artwork or the item's name on a card, resume bar, played tick, focus outline. */
static inline void jf_card(const jf_app *a,uint16_t *fb,int x,int y,const jf_item *it,uint8_t shape,int focused) {
    int w,h;jf_shape_size(shape,&w,&h);
    if(x>=JF_W)return;
    int fits=x+w<=JF_W;
    if(!fits||!jf_draw_art(a,fb,x,y,it->card_id,it->card_kind,shape)) {
        pj_osd_rect(fb,x,y,(fits?w:JF_W-x),h,JF_CARD);
        if(fits)jf_text_fit(a,fb,x+3,y+h/2-5,w-6,it->name,JF_INK);
    }
    if(fits&&it->runtime_s&&it->resume_s&&!it->played) {
        int fill=(int)((uint64_t)(w-4)*it->resume_s/it->runtime_s);if(fill>w-4)fill=w-4;
        pj_osd_rect(fb,x+2,y+h-4,w-4,2,0x404040u);pj_osd_rect(fb,x+2,y+h-4,fill,2,JF_ACCENT);
    }
    if(fits&&it->played){pj_osd_rect(fb,x+w-9,y+2,7,7,JF_ACCENT);pj_osd_rect(fb,x+w-7,y+5,1,2,JF_INK);pj_osd_rect(fb,x+w-6,y+6,3,1,JF_INK);}
    if(focused){jf_outline(fb,x-1,y-1,w+2,h+2,JF_FOCUS);jf_outline(fb,x-2,y-2,w+4,h+4,JF_FOCUS);jf_round(fb,x-2,y-2,w+4,h+4);}
    else if(fits)jf_round(fb,x,y,w,h);
}
/* pj_ui_hero: the backdrop at 60% fading into the page; a quiet accent wash without one.
 * scrim_top >= 0 (Home) also darkens the picture from that row down to about 14% where the
 * title sits, so lettering inside the artwork (logos, library names) never fights the title. */
static inline void jf_hero(const jf_app *a,uint16_t *fb,const char *id,uint8_t kind,int scrim_top) {
    const int height=JF_HERO_H,fade=26,ramp=16;
    if(!jf_draw_art(a,fb,0,0,id,kind,JF_SHAPE_HERO)) {
        for(int y=0;y<height;y++){unsigned lift=(unsigned)(height-y)*28u/(unsigned)height;
            pj_osd_rect(fb,0,y,JF_W,1,((0x10u+lift/3)<<16)|((0x10u+lift*2/3)<<8)|(0x10u+lift));}
        return;
    }
    uint16_t page=pj_osd_rgb(JF_PAGE);
    for(int y=0;y<height;y++) {
        unsigned keep=y<height-fade?154u:(unsigned)(height-y)*154u/(unsigned)fade;
        if(scrim_top>=0&&y>=scrim_top) {
            unsigned dark=y>=scrim_top+ramp?36u:154u-(unsigned)(y-scrim_top)*118u/(unsigned)ramp;
            if(dark<keep)keep=dark;
        }
        unsigned rest=256u-keep;
        unsigned pr=(unsigned)(page>>11)*rest,pg=(unsigned)((page>>5)&63u)*rest,pb=(unsigned)(page&31u)*rest;
        for(int x=0;x<JF_W;x++) {
            unsigned v=fb[y*JF_W+x];
            fb[y*JF_W+x]=(uint16_t)((((v>>11)*keep+pr)>>8)<<11|((((v>>5)&63u)*keep+pg)>>8)<<5|(((v&31u)*keep+pb)>>8));
        }
    }
}
/* pj_ui_header: the Jellyfin mark top left, the source on the right. */
static inline void jf_header(const jf_app *a,uint16_t *fb) {
    pj_icon_jellyfin(fb,4,3,0);
    const char *label=a->offline?"Offline":a->cfg.server_name;
    int w=jf_measure(a,label);
    jf_shadow(a,fb,JF_W-5-w,3,w+1,label,a->offline?0xff8a65u:JF_MUTED);
}
/* "Created by ChaoticSPARTAN1": the launcher's own pixel font at the Credits page's 10/11
 * scale, muted with a shadow, right-aligned just under the server name in the hero's top band (121 px at
 * full size, too wide for the bottom edge next to bigger cards; here it covers no card, name or title). */
static inline void jf_home_credit(const jf_app *a,uint16_t *fb) {
    static const char credit[]="Created by ChaoticSPARTAN1";
    int w=(int)((uint32_t)jf_measure(a,credit)*10u/11u)+1,x=JF_W-5-w;
    jf_text_scaled(a,fb,x+1,16,w+2,credit,0x000000u,10,11);
    jf_text_scaled(a,fb,x,15,w+2,credit,0x9a9a9au,10,11);
}
static inline void jf_draw_home(const jf_app *a,uint16_t *fb) {
    pj_osd_fill(fb,JF_PAGE);
    if(!a->row_count){jf_hero(a,fb,"",0,-1);jf_header(a,fb);jf_text(a,fb,8,72,"Nothing here yet",JF_INK);return;}
    const jf_row *r=&a->rows[a->row_focus];const jf_item *f=&r->items[r->focus];
    /* Title low in the hero, a little smaller than body text, on a dark scrim (
     * at 28 px it ran into lettering that is part of library and thumb artwork). */
    jf_hero(a,fb,f->hero_id,f->hero_kind,16);
    jf_header(a,fb);jf_home_credit(a,fb);
    jf_text_scaled(a,fb,7,37,148,f->name,0x000000u,10,11);jf_text_scaled(a,fb,6,36,148,f->name,JF_INK,10,11);
    char meta[48];
    if(f->sub[0])jf_copy(meta,sizeof(meta),f->sub);else jf_meta(meta,sizeof(meta),f);
    jf_shadow(a,fb,6,48,148,meta,JF_MUTED);
    jf_text_fit(a,fb,6,62,90,r->title,JF_INK);
    if(a->row_focus+1<a->row_count) {
        char next[36];snprintf(next,sizeof(next),"v %s",a->rows[a->row_focus+1].title);
        int w=jf_measure(a,next);if(w>62)w=62;
        jf_text_fit(a,fb,JF_W-6-w,62,w+1,next,0x707070u);
    }
    jf_strip s=jf_strip_of(a,r);const int top=JF_HOME_CARDS_Y;
    for(unsigned i=s.first;i<r->count&&i<s.first+s.visible+1;i++) {
        int x=6+(int)(i-s.first)*s.pitch;const jf_item *c=&r->items[i];
        jf_card(a,fb,x,top,c,s.shape,i==r->focus);
        if(jf_is_thumb(s.shape)&&x+s.w<=JF_W) {
            if(i!=r->focus)jf_text_fit(a,fb,x,top+s.h+3,s.w,c->name,JF_MUTED);
            if(c->sub[0])jf_text_fit(a,fb,x,top+s.h+14,s.w,c->sub,0x808080u);
        }
    }
    jf_label l;if(jf_focus_label(a,&l))jf_draw_focus_label(a,fb,&l);
}
static inline void jf_draw_folder(const jf_app *a,uint16_t *fb) {
    pj_osd_fill(fb,JF_PAGE);
    pj_icon_jellyfin(fb,4,3,0);
    jf_text_fit(a,fb,20,3,134,a->folder_title,JF_INK);
    const jf_page *p=&a->folder;
    if(!p->count) {
        const char *m=a->busy==JF_R_FOLDER?"Can't reach the server. Trying again...":
                      a->request==JF_R_FOLDER||a->want==JF_W_FOLDER?"Loading...":"This folder is empty";
        jf_text_fit(a,fb,8,66,JF_W-16,m,JF_INK);return;
    }
    jf_grid g=jf_grid_of(a,p->shape);
    unsigned first=jf_first_row(a,p)*g.cols;int bottom=g.top;
    for(unsigned i=first;i<p->count;i++) {
        int y=g.top+(int)((i-first)/g.cols)*g.pitch_y;if(y+g.h+12>JF_H)break;
        int x=g.left+(int)((i-first)%g.cols)*g.pitch_x;
        const jf_item *it=&p->items[i];
        jf_card(a,fb,x,y,it,g.shape,i==p->focus);
        char compact[32];
        if(i!=p->focus)jf_text_fit(a,fb,x,y+g.h+2,g.w,jf_card_label(a,it,0,g.w,compact,sizeof(compact)),JF_MUTED);
        bottom=y+g.h+14;
    }
    jf_label l;if(jf_focus_label(a,&l))jf_draw_focus_label(a,fb,&l);
    /* Bigger posters show one row: the room below tells where you are, like Jellyfin's
     * "1-8 of 15" paging line, with the focused item's year and length. */
    if(g.rows==1&&bottom+11<=JF_H&&p->focus<p->count) {
        const jf_item *f=&p->items[p->focus];char meta[48],where[24];
        if(f->type==JF_T_EPISODE||f->type==JF_T_SEASON||!(f->year||f->runtime_s))meta[0]=0;else jf_meta(meta,sizeof(meta),f);
        snprintf(where,sizeof(where),"%u of %u",(unsigned)(a->folder_start+p->focus+1u),(unsigned)(a->folder_total?a->folder_total:p->count));
        int ww=jf_measure(a,where);
        if(meta[0])jf_text_fit(a,fb,g.left,bottom+4,JF_W-g.left-ww-14,meta,JF_MUTED);
        jf_text(a,fb,JF_W-6-ww,bottom+4,where,0x808080u);
    }
}
static inline void jf_button(const jf_app *a,uint16_t *fb,int x,int y,int w,const char *label,int focused) {
    pj_osd_rect(fb,x,y,w,13,focused?JF_ACCENT:0x303030u);jf_round(fb,x,y,w,13);
    jf_text_fit(a,fb,x+(w-jf_measure(a,label))/2,y+1,w,label,JF_INK);
}
static inline void jf_draw_details(const jf_app *a,uint16_t *fb) {
    const jf_item *d=&a->detail;
    pj_osd_fill(fb,JF_PAGE);
    jf_hero(a,fb,d->hero_id,d->hero_kind,-1);
    jf_header(a,fb);
    jf_card(a,fb,6,18,d,JF_SHAPE_POSTER,0);
    int x=44,width=JF_W-x-5;
    jf_shadow(a,fb,x,18,width,d->name,JF_INK);
    char meta[48];jf_meta(meta,sizeof(meta),d);
    jf_shadow(a,fb,x,30,width,d->sub[0]?d->sub:meta,JF_MUTED);
    if(a->genres[0])jf_shadow(a,fb,x,41,width,a->genres,JF_MUTED);
    uint32_t position=d->resume_s;
    if(jf_playable(d)) {
        if(position>30&&!d->played){jf_button(a,fb,x,53,52,"Resume",a->detail_action==0);jf_button(a,fb,x+56,53,55,"From start",a->detail_action==1);}
        else jf_button(a,fb,x,53,52,"Play",1);
    }
    if(d->runtime_s&&position&&!d->played) {
        char now[12],total[12],line[28];jf_clock(now,sizeof(now),position);jf_clock(total,sizeof(total),d->runtime_s);
        snprintf(line,sizeof(line),"%s / %s",now,total);
        int fill=(int)((uint64_t)width*position/d->runtime_s);if(fill>width)fill=width;
        pj_osd_rect(fb,x,69,width,2,0x404040u);pj_osd_rect(fb,x,69,fill,2,JF_ACCENT);
        jf_text(a,fb,x,73,line,JF_MUTED);
    }
    if(a->overview[0])jf_paragraph(a,fb,6,88,148,4,a->overview,0xd0d0d0u);
    else if(a->request==JF_R_DETAILS)jf_text(a,fb,6,88,"Loading...",0x808080u);
}
/* pj_ui_osd: the strip below the picture; never covers the video. */
static inline void jf_draw_player(const jf_app *a,uint16_t *fb) {
    pj_osd_rect(fb,0,(int)JF_VIDEO_H,JF_W,JF_H-(int)JF_VIDEO_H,0x000000u);
    int note=a->message[0]&&a->pictured; /* a picture is up: the message goes in the strip, not over it */
    if(a->message[0]&&!a->pictured) {
        int w=jf_measure(a,a->message)+12;
        pj_osd_rect(fb,80-w/2,52,w,15,0x000000u);jf_text(a,fb,80-w/2+6,54,a->message,JF_INK);
    }
    if(a->note_until&&a->note[0]&&a->pictured) { /* 1.3.2: a track result, over the top of the picture for a few seconds */
        pj_osd_rect(fb,2,2,JF_W-4,27,0x000000u);
        jf_paragraph(a,fb,6,4,JF_W-12,2,a->note,a->note_warn?PJ_UI_NOTE:JF_INK);
    }
    if(!a->osd&&!a->paused&&!note)return;
    const char *title=a->detail.type==JF_T_EPISODE&&a->detail.sub[0]?a->detail.sub:a->detail.name;
    unsigned ink=note?(a->reconnect_at||a->link_down?PJ_UI_NOTE:JF_MUTED):JF_INK; /* fixing something: yellow, as in Setup */
    jf_text_fit(a,fb,4,(int)JF_VIDEO_H+1,152,note?a->message:a->paused?"Paused":title,ink);
    char now[12],total[12];jf_clock(now,sizeof(now),jf_position_s(a));jf_clock(total,sizeof(total),a->detail.runtime_s);
    int tw=jf_measure(a,total),nw=jf_measure(a,now);
    jf_text(a,fb,4,(int)JF_VIDEO_H+12,now,JF_MUTED);
    jf_text(a,fb,JF_W-4-tw,(int)JF_VIDEO_H+12,total,JF_MUTED);
    int left=8+nw,width=JF_W-left-8-tw;
    if(width>4) {
        pj_osd_rect(fb,left,(int)JF_VIDEO_H+17,width,2,0x404040u);
        if(a->detail.runtime_s){uint32_t p=jf_position_s(a);if(p>a->detail.runtime_s)p=a->detail.runtime_s;
            pj_osd_rect(fb,left,(int)JF_VIDEO_H+17,(int)((uint64_t)width*p/a->detail.runtime_s),2,JF_ACCENT);}
    }
}
/* Connecting and errors: the Pocket's source screen look (banner, plain lines). */
static inline void jf_draw_center(const jf_app *a,uint16_t *fb,const char *line1,const char *line2,const char *hint) {
    pj_osd_fill(fb,JF_PAGE);
    pj_osd_image(fb,24,16,PJ_BRAND_JFBANNER_W,PJ_BRAND_JFBANNER_H,pj_brand_jfbanner_rgb,pj_brand_jfbanner_alpha);
    if(line1)jf_paragraph(a,fb,12,62,136,2,line1,JF_INK);
    if(line2)jf_paragraph(a,fb,12,88,136,2,line2,JF_MUTED);
    if(hint){int w=jf_measure(a,hint);jf_text(a,fb,80-w/2,126,hint,0x808080u);}
}
/* ---------- the Jellyfin menu: the launcher menu's own look (launcher_logic.h) ---------- */
/* 12x12 icons from rectangles, like the launcher's pj_icon_* glyphs. */
static inline void jf_icon_home(uint16_t *fb,int x,int y,unsigned rgb) {
    for(int i=0;i<6;i++)pj_osd_rect(fb,x+5-i,y+i,2+2*i,1,rgb);
    pj_osd_rect(fb,x+2,y+6,8,6,rgb);pj_osd_rect(fb,x+5,y+8,2,4,PJ_UI_BAR);
}
static inline void jf_icon_cards(uint16_t *fb,int x,int y,unsigned rgb,int big) {
    if(big){pj_osd_rect(fb,x,y+2,5,8,rgb);pj_osd_rect(fb,x+7,y+2,5,8,rgb);return;}
    for(int r=0;r<2;r++)for(int c=0;c<3;c++)pj_osd_rect(fb,x+c*4,y+1+r*6,3,4,rgb);
}
static inline void jf_icon_info(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+1,y+1,10,10,rgb);pj_osd_rect(fb,x+2,y+2,8,8,PJ_UI_BAR);
    pj_osd_rect(fb,x+5,y+3,2,2,rgb);pj_osd_rect(fb,x+5,y+6,2,3,rgb);
}
static inline void jf_icon_leave(uint16_t *fb,int x,int y,unsigned rgb) {
    for(int i=0;i<5;i++)pj_osd_rect(fb,x+1+i,y+6-i,1,2*i+1,rgb);
    pj_osd_rect(fb,x+6,y+5,6,3,rgb);
}
static inline void jf_icon_audio(uint16_t *fb,int x,int y,unsigned rgb) { /* a little speaker */
    pj_osd_rect(fb,x+1,y+4,3,4,rgb);for(int i=0;i<4;i++)pj_osd_rect(fb,x+4+i,y+3-i,1,6+2*i,rgb);
    pj_osd_rect(fb,x+9,y+4,1,4,rgb);pj_osd_rect(fb,x+11,y+3,1,6,rgb);
}
static inline void jf_icon_subs(uint16_t *fb,int x,int y,unsigned rgb) { /* a screen with two caption lines */
    pj_osd_rect(fb,x,y+1,12,10,rgb);pj_osd_rect(fb,x+1,y+2,10,8,PJ_UI_BAR);
    pj_osd_rect(fb,x+2,y+6,8,1,rgb);pj_osd_rect(fb,x+3,y+8,6,1,rgb);
}
static inline void jf_icon_credits(uint16_t *fb,int x,int y,unsigned rgb) { /* a small person */
    pj_osd_rect(fb,x+4,y+1,4,4,rgb);pj_osd_rect(fb,x+2,y+6,8,5,rgb);
}
static inline const char *jf_menu_row_label(const jf_app *a,unsigned i) {
    switch(i) {
    case JF_M_HOME:return "Home";
    case JF_M_LAYOUT:return a->big_cards?"Cards: Bigger":"Cards: Standard";
    case JF_M_AUDIO:return a->audio_lang==0?"Audio: English":a->audio_lang==1?"Audio: Original":"Audio: Japanese";
    case JF_M_SUBS:return a->subs?"Subtitles: English":"Subtitles: Off";
    case JF_M_INFO:return "Playback info";
    case JF_M_SETUP:return "Wi-Fi & sign-in";
    case JF_M_PLINK:return "Pocket link";
    case JF_M_CREDITS:return "Credits";
    default:return "Chromatic menu";
    }
}
static inline void jf_icon_wifi(uint16_t *fb,int x,int y,unsigned rgb) { /* four signal bars, as the setup's */
    for(int i=0;i<4;i++)pj_osd_rect(fb,x+1+i*3,y+9-(i+1)*2,2,(i+1)*2,rgb);
}
/* A link plug and its cable (1.3.0 Pocket link row). */
static inline void jf_icon_plink(uint16_t *fb,int x,int y,unsigned rgb) {
    pj_osd_rect(fb,x+1,y+1,6,5,rgb);pj_osd_rect(fb,x+2,y+2,4,3,PJ_UI_BAR);pj_osd_rect(fb,x+3,y+6,2,2,rgb);
    pj_osd_rect(fb,x+4,y+8,1,3,rgb);pj_osd_rect(fb,x+4,y+10,7,1,rgb);pj_osd_rect(fb,x+10,y+5,1,6,rgb);
}
static inline void jf_menu_row_icon(const jf_app *a,uint16_t *fb,unsigned i,int y,unsigned ink) {
    if(i==JF_M_HOME)jf_icon_home(fb,15,y+6,ink);
    else if(i==JF_M_SETUP)jf_icon_wifi(fb,15,y+6,ink);
    else if(i==JF_M_PLINK)jf_icon_plink(fb,15,y+6,ink);
    else if(i==JF_M_LAYOUT)jf_icon_cards(fb,15,y+6,ink,a->big_cards);
    else if(i==JF_M_AUDIO)jf_icon_audio(fb,15,y+6,ink);
    else if(i==JF_M_SUBS)jf_icon_subs(fb,15,y+6,ink);
    else if(i==JF_M_INFO)jf_icon_info(fb,15,y+6,ink);
    else if(i==JF_M_CREDITS)jf_icon_credits(fb,15,y+6,ink);
    else jf_icon_leave(fb,15,y+6,ink);
}
/* The Credits tab: who made it and the notices, in the small (scaled) font. */
static inline void jf_draw_credits(const jf_app *a,uint16_t *fb) {
    const unsigned char *alpha=a->cfg.font_alpha,*advance=a->cfg.font_advance;
    pj_osd_text(fb,24,4,"Credits",PJ_UI_JELLYFIN,alpha,advance);
    pj_osd_rect(fb,8,22,144,100,PJ_UI_EDGE);pj_osd_rect(fb,9,23,142,98,PJ_UI_ROW);
    static const char *const lines[]={
        "Created by ChaoticSPARTAN1","",
        "Jellyfin logo (c) Jellyfin","contributors, CC BY-SA 4.0","",
        "Not made or endorsed by","ModRetro or Jellyfin"};
    int y=28;
    for(unsigned i=0;i<sizeof(lines)/sizeof(lines[0]);i++) {
        if(lines[i][0])jf_text_scaled(a,fb,14,y,134,lines[i],i==0?0xffffffu:0xc8c8c8u,8,11); /* small font */
        y+=lines[i][0]?12:6;
    }
    pj_osd_text(fb,8,132,"B: back",PJ_UI_MUTED,alpha,advance);
}
static inline void jf_draw_menu(const jf_app *a,uint16_t *fb) {
    const unsigned char *alpha=a->cfg.font_alpha,*advance=a->cfg.font_advance;
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_jellyfin(fb,6,3,0);
    if(a->menu_page==2){jf_draw_credits(a,fb);return;}
    if(a->menu_page) { /* Playback info */
        pj_osd_text(fb,24,4,"Playback info",PJ_UI_JELLYFIN,alpha,advance);
        pj_osd_rect(fb,8,26,144,94,PJ_UI_EDGE);pj_osd_rect(fb,9,27,142,92,PJ_UI_ROW);
        char line[48];int y=31;
        snprintf(line,sizeof(line),"Quality: %u kb/s",(unsigned)((a->bitrate?a->bitrate:JF_RATE_START)/1000u));
        jf_text_fit(a,fb,14,y,132,line,0xffffffu);y+=15;
        if(a->shown||a->dropped) {
            snprintf(line,sizeof(line),"Last video: %lu shown",(unsigned long)a->shown);jf_text_fit(a,fb,14,y,132,line,0xd0d0d0u);y+=15;
            snprintf(line,sizeof(line),"Late, skipped: %lu",(unsigned long)a->dropped);jf_text_fit(a,fb,14,y,132,line,0xd0d0d0u);y+=15;
        } else {jf_text_fit(a,fb,14,y,132,"No video played yet",0xd0d0d0u);y+=15;}
        snprintf(line,sizeof(line),"Quality lowered: %u",(unsigned)a->softened);jf_text_fit(a,fb,14,y,132,line,0xd0d0d0u);y+=15;
        snprintf(line,sizeof(line),"Artwork: %u of %u KB",(unsigned)((a->art_used+1023u)/1024u),(unsigned)(a->art_cap/1024u));
        jf_text_fit(a,fb,14,y,132,line,PJ_UI_MUTED);
        pj_osd_text(fb,8,132,"B: back",PJ_UI_MUTED,alpha,advance);
        return;
    }
    pj_osd_text(fb,24,4,"Jellyfin",PJ_UI_JELLYFIN,alpha,advance);
    const char *const notes[JF_M_COUNT]={"Your Jellyfin Home","Not saved: resets on exit",
        a->menu_from==JF_S_PLAYER?"Changes the video now":"English by default","Quality and last video",
        "Change network or sign-in","Share with an Analogue Pocket","About this player","Games and other choices",
        a->menu_from==JF_S_PLAYER?"Changes the video now":"Burned into the picture"};
    /* The rows shown (the Pocket link row only on the link FPGA); the list scrolls so the focused row stays visible
     * (seven or eight rows, four shown). Without the link row this is exactly the 1.2.9 menu. */
    unsigned shown[JF_M_COUNT],count=jf_menu_rows(a,shown),sel=0;
    for(unsigned k=0;k<count;k++)if(shown[k]==a->menu_sel)sel=k;
    unsigned top=sel<JF_MENU_VIS?0:sel-(JF_MENU_VIS-1u);
    if(top>count-JF_MENU_VIS)top=count-JF_MENU_VIS;
    for(unsigned s=0;s<JF_MENU_VIS;s++) {
        unsigned i=shown[top+s];int y=22+(int)s*25,focused=i==a->menu_sel;
        pj_launch_row(fb,y,jf_menu_row_label(a,i),focused,PJ_UI_JELLYFIN,alpha,advance);
        jf_menu_row_icon(a,fb,i,y,i==JF_M_LEAVE?0xd0d0d0u:PJ_UI_JELLYFIN);
    }
    if(top)jf_text(a,fb,146,20,"^",PJ_UI_MUTED);                 /* more above / below */
    if(top+JF_MENU_VIS<count)jf_text(a,fb,146,112,"v",PJ_UI_MUTED);
    pj_osd_text(fb,8,121,notes[a->menu_sel<JF_M_COUNT?a->menu_sel:0],PJ_UI_MUTED,alpha,advance);
    int adj=a->menu_sel==JF_M_LAYOUT||a->menu_sel==JF_M_AUDIO||a->menu_sel==JF_M_SUBS;
    pj_osd_text(fb,8,133,adj?"A: change   B: close":"A: choose   B: close",PJ_UI_MUTED,alpha,advance);
}
static inline void jf_app_draw(const jf_app *a,uint16_t *fb) {
    if(a->draw_part==JF_DRAW_LABEL) { /* a scrolling step: the name strip only, no artwork decoded */
        jf_label l;if(jf_focus_label(a,&l))jf_draw_focus_label(a,fb,&l);
        return;
    }
    switch(a->screen) {
    case JF_S_MENU:jf_draw_menu(a,fb);break;
    case JF_S_CONNECT: {
        char line[48];snprintf(line,sizeof(line),"Connecting to %s",a->cfg.server_name[0]?a->cfg.server_name:"your server");
        jf_draw_center(a,fb,line,a->link_down?"Wi-Fi dropped. The console is rejoining; back in a moment.":
                       a->offline?"The server isn't answering. Trying again.":NULL,"B: back");break;}
    case JF_S_HOME:jf_draw_home(a,fb);break;
    case JF_S_FOLDER:jf_draw_folder(a,fb);break;
    case JF_S_DETAILS:jf_draw_details(a,fb);break;
    case JF_S_PLAYER:jf_draw_player(a,fb);break;
    case JF_S_ERROR:jf_draw_center(a,fb,a->message,a->detail_line[0]?a->detail_line:NULL,
                                   a->err_kind==JF_ERR_UNREACHABLE?"A: retry  Select: find  B: back":
                                   a->err_kind==JF_ERR_SIGNIN?"A: retry  Select: sign in  B: back":"A: try again   B: back");break;
    default:pj_osd_fill(fb,JF_PAGE);break;
    }
}
#endif
