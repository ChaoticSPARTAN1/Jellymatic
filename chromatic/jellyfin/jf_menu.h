/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Chromatic Jellyfin app: the Jellyfin menu (Select) and the focused card's scrolling name.
 * Included by jf_app.h only, before its input section; the drawing is in jf_ui.h.
 *
 * Why Select: on the stock FPGA the Chromatic's own Menu button never reaches the app. It is
 * not one of the eight button bits; it toggles the FPGA overlay, the launcher then reads "menu
 * closed" (launcher_logic.h, pj_launch_buttons) and starts stock, which means Games. Start
 * already means A on the browsing screens (as in the launcher menu), so Select opens this menu
 * on Home, folders and Details. In the player Select keeps toggling the playback strip.
 * On the stock+media FPGA the launcher sets PJ_MEDIA_MENU while its overlay is up, the FPGA
 * then reports the Menu button instead of closing the overlay, and it arrives here as
 * JF_BTN_MENU: it opens this menu like Select, and closes it like B.
 *
 * Settings chosen here (the card layout) live in RAM only: nothing in the app writes flash
 * or NVS, so they last until Jellyfin closes or the console is switched off.
 *
 * Scrolling cost: only the focused card's name scrolls, and only while it does not fit. A
 * step is one pixel every JF_SCROLL_STEP_MS (at most 12.5 a second), with a pause at the
 * start and the end. A step that is not part of a full redraw asks jf_app_draw to repaint
 * just that label strip (JF_DRAW_LABEL: one page-coloured rectangle and the text, no
 * artwork decoded); full redraws (and their artwork decodes) happen exactly as before.
 */
#ifndef JF_MENU_H
#define JF_MENU_H
#include "../launcher/shared/launcher_logic.h"

#define JF_SCROLL_STEP_MS 80u   /* one pixel per step: about 12 px a second */
#define JF_SCROLL_HOLD_MS 1500u /* pause at the start and at the end of the name */
#define JF_HOME_CARDS_Y 76      /* Home: top of the focused row's cards */

/* Subtitles row (JF_M_SUBS): hidden since 1.2.7 because SubtitleMethod=Encode failed on the owner's server; built
 * only with JF_SUBS_ROW=1 (jf_app.h) until a console run proves burn-in (docs/Design-notes.md
 * "Subtitles: what to test"). Hidden, a->subs stays 0 and no stream asks for subtitles. */
/* 1.3.2: the menu also opens over a playing video with the Menu button (the media FPGA forwards it; sound, and so
 * the audio track, exists only there). The video pauses, the Audio row is focused, and closing the menu with a
 * changed Audio (or Subtitles) row reopens the stream where it was with the new track (jf_switch_tracks). */
/* JF_M_SETUP (1.2.9): "Wi-Fi & sign-in" leaves the app for the launcher's own setup screens (the app must close
 * first: the setup needs its memory). Reachable with Select on the stock FPGA, Select or Menu on the media FPGA. */
/* JF_M_PLINK (1.3.0): "Pocket link" leaves the app for the launcher's Pocket link screen (the link gateway needs the
 * app's memory). Shown only when jf_config.pocket_link says the FPGA has the link bridge; otherwise skipped, so the
 * menu is exactly 1.2.9's. */
enum {JF_M_HOME,JF_M_LAYOUT,JF_M_AUDIO,JF_M_INFO,JF_M_SETUP,JF_M_PLINK,JF_M_CREDITS,JF_M_LEAVE,JF_M_SUBS,JF_M_COUNT};
/* The order on screen (JF_M_SUBS sits under Audio; the enum keeps the 1.3.1 numbers). Launcher 1.5.0 (owner's
 * request): "Pocket link" is the first row, easy to find; still shown only with the link bridge, so without it the
 * menu is unchanged. Launcher 1.5.1 (owner's request): every opening focuses the first row, Pocket link when it is
 * shown, Home when not (before 1.5.1: Home at first, then the row last used); over a video the Audio row, as since
 * 1.3.2. */
static const uint8_t jf_menu_order[JF_M_COUNT]={JF_M_PLINK,JF_M_HOME,JF_M_LAYOUT,JF_M_AUDIO,JF_M_SUBS,JF_M_INFO,JF_M_SETUP,
                                                 JF_M_CREDITS,JF_M_LEAVE};
#define JF_MENU_VIS 4u /* rows shown at once; the list scrolls to keep the focused row in view */

static inline int jf_menu_measure(const jf_app *a,const char *t){return pj_osd_measure(t,a->cfg.font_advance);}
/* The name under a card: an episode's own title, else the item's name. A season that does
 * not fit shows "S1" on cards that are not focused; the focused one scrolls the full name. */
static inline const char *jf_card_label(const jf_app *a,const jf_item *it,int focused,int w,char *compact,size_t cap) {
    const char *label=it->type==JF_T_EPISODE&&it->sub[0]?it->sub:it->name;
    if(!focused&&it->type==JF_T_SEASON&&!strncmp(label,"Season ",7)&&jf_menu_measure(a,label)>w&&cap>1) {
        compact[0]='S';jf_copy(compact+1,cap-1,label+7);return compact;
    }
    return label;
}
/* Where the focused card's name is drawn on this screen; 0 when there is none. */
typedef struct {int x,y,w,over;const char *text;char compact[32];} jf_label;
static inline int jf_focus_label(const jf_app *a,jf_label *l) {
    l->text=NULL;l->over=0;
    if(a->screen==JF_S_HOME&&a->row_count) {
        const jf_row *r=&a->rows[a->row_focus];jf_strip s=jf_strip_of(a,r);
        if(!r->count||!jf_is_thumb(s.shape))return 0; /* poster rows carry no names, as on the Pocket */
        l->x=6+(int)(r->focus-s.first)*s.pitch;l->y=JF_HOME_CARDS_Y+s.h+3;l->w=s.w;l->text=r->items[r->focus].name;
        if(l->x+l->w>JF_W)return 0;
    } else if(a->screen==JF_S_FOLDER&&a->folder.count) {
        const jf_page *p=&a->folder;jf_grid g=jf_grid_of(a,p->shape);unsigned first=jf_first_row(a,p)*g.cols;
        if(p->focus<first)return 0;
        unsigned at=p->focus-first;int y=g.top+(int)(at/g.cols)*g.pitch_y;
        if(y+g.h+12>JF_H)return 0;
        l->x=g.left+(int)(at%g.cols)*g.pitch_x;l->y=y+g.h+2;l->w=g.w;
        l->text=jf_card_label(a,&p->items[p->focus],1,g.w,l->compact,sizeof(l->compact));
    } else return 0;
    int over=jf_menu_measure(a,l->text)-l->w;
    l->over=over>0?over+1:0;
    return 1;
}
/* Called at the end of every tick with the full-redraw flag; returns whether to draw. */
static inline int jf_scroll_tick(jf_app *a,int redraw) {
    a->draw_part=JF_DRAW_FULL;
    jf_label l;
    if(a->playing||!jf_focus_label(a,&l)) {
        if(a->scroll_px&&!redraw)redraw=1; /* never leave a half-scrolled name behind */
        a->scroll_px=0;a->scroll_key=0;return redraw;
    }
    uint32_t key=1u+a->screen+((uint32_t)a->row_focus<<3)+((uint32_t)(a->screen==JF_S_HOME?a->rows[a->row_focus].focus:a->folder.focus)<<6)
                 +((uint32_t)a->big_cards<<14)+((a->folder_start&0x7fffu)<<15);
    if(key!=a->scroll_key) {
        if(a->scroll_px&&!redraw)redraw=1;
        a->scroll_key=key;a->scroll_px=0;a->scroll_at=a->now+JF_SCROLL_HOLD_MS;return redraw;
    }
    if(!l.over) {
        if(a->scroll_px&&!redraw)redraw=1;
        a->scroll_px=0;return redraw;
    }
    if((int32_t)(a->now-a->scroll_at)<0)return redraw;
    if(a->scroll_px<l.over) {
        a->scroll_px++;a->scroll_at=a->now+(a->scroll_px>=l.over?JF_SCROLL_HOLD_MS:JF_SCROLL_STEP_MS);
    } else {a->scroll_px=0;a->scroll_at=a->now+JF_SCROLL_HOLD_MS;}
    if(redraw)return 1; /* the full redraw paints the name at its new place */
    a->draw_part=JF_DRAW_LABEL;return 1;
}

/* ---------- the Jellyfin menu ---------- */
static inline void jf_menu_open(jf_app *a) {
    a->menu_from=a->screen;a->menu_page=0;a->screen=JF_S_MENU;a->redraw=1;
    a->menu_lang=a->audio_lang;a->menu_subs=a->subs;
    /* 1.5.1 (owner's request): the menu opens on its first row, Pocket link when the link FPGA shows it, else Home. */
    a->menu_sel=a->cfg.pocket_link?JF_M_PLINK:JF_M_HOME;
    if(a->menu_from==JF_S_PLAYER) { /* over a video: it pauses (and resumes on close), the Audio row is focused */
        a->menu_sel=JF_M_AUDIO;a->menu_paused=0;
        if(!a->paused) {
            a->paused=1;a->menu_paused=1;
            if(a->audio_on&&!a->audio_dead&&a->io.audio_pause)a->io.audio_pause(a->io.ctx,1);
        }
    }
}
/* Back to the video: a changed track reopens the stream where it was; otherwise it plays on (or stays paused). */
static inline void jf_menu_to_player(jf_app *a) {
    a->screen=JF_S_PLAYER;a->menu_page=0;a->redraw=1;a->osd=1;a->osd_until=a->now+3000;
    if(a->cfg.fb)memset(a->cfg.fb,0,sizeof(uint16_t)*JF_W*JF_VIDEO_H); /* the menu is gone from the picture area */
    a->pictured=0; /* until the next picture, messages show over the black area */
    if(a->audio_lang!=a->menu_lang||a->subs!=a->menu_subs){jf_switch_tracks(a);return;}
    if(a->menu_paused) {
        a->menu_paused=0;a->paused=0;
        if(a->audio_on&&!a->audio_dead&&a->io.audio_pause)a->io.audio_pause(a->io.ctx,0);
        /* the pause is nobody's lateness: the clock goes on from the picture it stopped at (as A in the player) */
        a->clock_ms=a->now-(uint32_t)((uint64_t)(a->avi.frame_index-(uint32_t)a->base_frame)*jf_avi_frame_us(&a->avi)/1000u);
        a->audio_played_at=a->now;a->starve_mark=a->audio_played;a->starve_ms=a->now;a->window_ms=a->now;
    }
}
static inline void jf_menu_close(jf_app *a) {
    if(a->menu_from==JF_S_PLAYER&&a->playing){jf_menu_to_player(a);return;}
    if(a->menu_from==JF_S_PLAYER)a->menu_from=JF_S_DETAILS; /* the video ended under the menu */
    a->screen=a->menu_from;a->menu_page=0;a->redraw=1;
}
static inline int jf_menu_shown(const jf_app *a,unsigned row) {
    if(row==JF_M_SUBS)return JF_SUBS_ROW;
    return row!=JF_M_PLINK||a->cfg.pocket_link;
}
/* The rows on screen, in order; returns how many. */
static inline unsigned jf_menu_rows(const jf_app *a,unsigned out[JF_M_COUNT]) {
    unsigned n=0;for(unsigned k=0;k<JF_M_COUNT;k++)if(jf_menu_shown(a,jf_menu_order[k]))out[n++]=jf_menu_order[k];return n;
}
/* Up/Down to the next shown row, wrapping. */
static inline void jf_menu_step(jf_app *a,int down) {
    unsigned k=0;while(k<JF_M_COUNT&&jf_menu_order[k]!=a->menu_sel)k++;
    if(k==JF_M_COUNT)k=0;
    do k=down?(k+1u)%JF_M_COUNT:(k+JF_M_COUNT-1u)%JF_M_COUNT;while(!jf_menu_shown(a,jf_menu_order[k]));
    a->menu_sel=jf_menu_order[k];a->redraw=1;
}
static inline void jf_menu_press(jf_app *a,uint16_t pressed) {
    if(a->menu_page) { /* Playback info (1) or Credits (2): any of these goes back to the list */
        if(pressed&(JF_BTN_B|JF_BTN_SELECT|JF_BTN_MENU|JF_BTN_A|JF_BTN_START)){a->menu_page=0;a->redraw=1;}
        return;
    }
    if(pressed&(JF_BTN_B|JF_BTN_SELECT|JF_BTN_MENU)){jf_menu_close(a);return;}
    if(pressed&JF_BTN_UP){jf_menu_step(a,0);return;}
    if(pressed&JF_BTN_DOWN){jf_menu_step(a,1);return;}
    /* The adjustable rows change their value in place (RAM only, never flash or NVS): Left/Right or A. */
    if(pressed&(JF_BTN_LEFT|JF_BTN_RIGHT|JF_BTN_A|JF_BTN_START)) {
        if(a->menu_sel==JF_M_LAYOUT){a->big_cards=(uint8_t)!a->big_cards;a->redraw=1;return;}
        if(a->menu_sel==JF_M_AUDIO) { /* English (default) / Original / Japanese */
            a->audio_lang=(uint8_t)((pressed&JF_BTN_LEFT)?(a->audio_lang+2u)%3u:(a->audio_lang+1u)%3u);
            if(!a->tracks_ok)a->streams_done=0; /* no track list yet: the next play (or the switch) fetches it */
            a->redraw=1;return; /* over a video: applied when the menu closes */
        }
        if(a->menu_sel==JF_M_SUBS&&JF_SUBS_ROW) { /* Off / English (burned in by the server) */
            a->subs=(uint8_t)!a->subs;
            if(!a->tracks_ok)a->streams_done=0;
            a->redraw=1;return;
        }
    }
    if(!(pressed&(JF_BTN_A|JF_BTN_START)))return;
    switch(a->menu_sel) {
    case JF_M_HOME:
        if(a->playing)jf_stop(a,1); /* opened over a video: it stops (and reports) before Home */
        a->menu_page=0;a->depth=0;a->row_focus=0;a->redraw=1;
        if(a->row_count)a->screen=JF_S_HOME;
        else{a->screen=JF_S_CONNECT;a->retries=0;jf_load_home(a);} /* Home never loaded: load it */
        break;
    case JF_M_INFO:a->menu_page=1;a->redraw=1;break;
    case JF_M_CREDITS:a->menu_page=2;a->redraw=1;break;
    case JF_M_SETUP:a->exit_to=JF_EXIT_SETUP;a->screen=JF_S_EXIT;break; /* the launcher closes the app, then opens Setup */
    case JF_M_PLINK:if(a->cfg.pocket_link){a->exit_to=JF_EXIT_PLINK;a->screen=JF_S_EXIT;}break; /* the app closes, the link screen opens */
    case JF_M_LEAVE:a->exit_to=JF_EXIT_MENU;a->screen=JF_S_EXIT;break; /* the launcher's menu: Games, Jellyfin, Other */
    }
}
#endif
