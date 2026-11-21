/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * The built-in "Local Weather" channel of the Chromatic TV app: an early-1990s cable local
 * forecast loop drawn on the console (160x144), from free US National Weather Service data
 * (api.weather.gov, no key). See docs/Design-notes.md.
 *
 * Parts: wx_art.h (fonts, icons, maps; generated), wx_location.h (the compiled-in place and its
 * requests; generated), wx_json.h (streaming scanner), wx_astro.h (calendar, sun, moon),
 * wx_data.h (what is kept of a reply), wx_radar.h (the radar picture, decoded as it downloads),
 * wx_sound.h (music and announcer feed), wx_draw.h (pages).
 *
 * Two structs, two owners, no locks:
 *   wx_fetch  one reply being parsed. Touched only by whoever reads the network (the fetch task).
 *   wx_state  everything the channel shows and plays. Touched only by the main loop.
 * The hand-over is wx_fetch_commit(), called by the main loop once the task has finished a reply
 * and is not touching the wx_fetch any more.
 *
 * Every failure ends in pages that still cycle and show "--" or "DATA NOT AVAILABLE": no reply,
 * a cut-off or wrong reply, an HTTP error, a clock that is not set. Nothing here allocates,
 * blocks, does I/O or writes flash. Portable C99 (gcc on the PC, xtensa gcc of ESP-IDF 5.3).
 */
#ifndef WX_H
#define WX_H
#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "wx_art.h"
#include "wx_location.h"
#include "wx_json.h"
#include "wx_astro.h"
#include "wx_data.h"
#include "wx_lpack.h"
#include "wx_place.h"
#include "wx_radar.h"
#include "wx_sound.h"

#define WX_PAGE_MS 8000u        /* each page is held this long */
#define WX_IDENT_MS 6000u       /* the opening card a little less */
#define WX_MAX_PAGES 16
#define WX_MAX_LOCAL_PAGES 5

typedef struct wx_state {
    /* data */
    wx_obs obs[WX_STATIONS];            /* slot 0 = the home station */
    uint16_t obs_tried;                 /* bit per station: its latest report has been asked for */
    uint8_t trend;                      /* WX_TREND_* of the home pressure */
    uint8_t alerts;                     /* active alerts */
    uint32_t fc_at;                     /* when the home forecast was committed (UTC s); 0 = none */
    uint32_t city_at[WX_CITIES];
    wx_forecast fc;
    wx_city city[WX_CITIES];
    char alert[WX_ALERT];               /* the alert crawl text */
    wx_almanac alm;
    /* The radar picture: precipitation levels, WX_RADAR_W x WX_RADAR_H, 4 bits a pixel (wx_radar.h). The page of
     * levels is the caller's (it is too large to live here); NULL = none held. */
    const uint8_t *radar;
    uint32_t radar_at;                  /* when it was taken (UTC s) */
    uint8_t radar_show;                 /* held and still current: the Current Radar page replaces the Rain Outlook */
    /* the show */
    uint32_t enter_ms,page_ms;
    uint32_t seen_sec,seen_ticker;      /* what the last reported frame showed */
    uint8_t page,n_pages,dirty,seen_page;
    uint8_t updating;                   /* a round of requests is in flight: the pages say UPDATING */
    uint8_t pages[WX_MAX_PAGES][2];     /* kind, argument */
    wx_sound snd;
    /* 1.6.3: the weather location (wx_place.h), the caller's and unchanged while it is set; NULL = the
     * compiled-in default pack (Orlando, FL). Set with wx_set_place. */
    const wx_place *place;
} wx_state;

/* ---- the place: a built-in pack's tables (wx_location.h), or a weather location's ---- */
/* 1.6.4: the pack whose whole tables are shown: the default pack (Orlando, FL) with no record, the pack a record names
 * exactly (1.6.5: also a location pack made for the place), or NULL for any other place (its own stations). */
static inline const wx_pack *wx_tables(const wx_state *s){return !s->place?WX_DEFAULT_PACK:s->place->builtin?s->place->pack:NULL;}
/* the pack whose Regional Forecast maps and radar view a place uses (NULL: none: a place with no pack) */
static inline const wx_pack *wx_region(const wx_state *s){return !s->place?WX_DEFAULT_PACK:s->place->pack;}
static inline wx_tz wx_state_tz(const wx_state *s){return s->place?s->place->tz:wx_tz_eastern();}  /* the default pack: US Eastern */
static inline const char *wx_home_name(const wx_state *s){const wx_pack *t=wx_tables(s);return t?t->name:s->place->name;}
static inline const char *wx_home_area(const wx_state *s){const wx_pack *t=wx_tables(s);return t?t->area:s->place->area;}
static inline double wx_home_lat(const wx_state *s){const wx_pack *t=wx_tables(s);return t?t->lat:s->place->lat_e4/10000.0;}
static inline double wx_home_lon(const wx_state *s){const wx_pack *t=wx_tables(s);return t?t->lon:s->place->lon_e4/10000.0;}
static inline unsigned wx_obs_rows(const wx_state *s){const wx_pack *t=wx_tables(s);return t?t->obs_rows:s->place->n_stations;}
static inline const char *wx_obs_label_at(const wx_state *s,unsigned i) {
    const wx_pack *t=wx_tables(s);
    return t?t->obs_label[i%t->obs_rows]:s->place->label[i%WX_PLACE_STATIONS];
}
static inline unsigned wx_obs_slot_at(const wx_state *s,unsigned i) {
    const wx_pack *t=wx_tables(s);
    return t?t->obs_slot[i%t->obs_rows]:i%WX_PLACE_STATIONS;
}
/* The two Regional Forecast pages: the region's state map and its close-up (Florida and Central Florida built in;
 * any other place's from its location pack). */
static inline int wx_shows_regional(const wx_state *s){return wx_region(s)!=NULL;}

/* ---- requests (the default pack's table) ---- */
static inline unsigned wx_request_count(void){return WX_DEFAULT_PACK->requests;}
static inline const wx_request *wx_request_at(unsigned index){return index<WX_DEFAULT_PACK->requests?&WX_DEFAULT_PACK->req[index]:NULL;}
/* Seconds between refreshes of a request. */
static inline uint32_t wx_request_period_s(unsigned index){return index<WX_DEFAULT_PACK->requests?WX_DEFAULT_PACK->period[index]:3600u;}
/* 0 for a request that is not needed now: the larger "recent reports" reply of a station is asked
 * for only when its latest report came without a temperature or without sky wording. */
static inline int wx_kind_wanted(const wx_state *s,unsigned kind,unsigned slot) {
    if(kind!=WX_K_RECENT)return 1;
    const wx_obs *o=&s->obs[slot%WX_STATIONS];
    return ((s->obs_tried>>(slot%WX_STATIONS))&1u)&&(o->temp_f==WX_NA||!o->sky[0]);
}
static inline int wx_request_wanted(const wx_state *s,unsigned index) {
    const wx_request *r=wx_request_at(index);
    return r?wx_kind_wanted(s,r->kind,r->slot):0;
}

static inline void wx_clear_data(wx_state *s) {
    for(int i=0;i<WX_STATIONS;i++)wx_obs_clear(&s->obs[i]);
    memset(&s->fc,0,sizeof(s->fc));
    for(int i=0;i<WX_CITIES;i++){memset(&s->city[i],0,sizeof(s->city[i]));s->city[i].temp=WX_NA;s->city[i].pop=-1;s->city_at[i]=0;}
    s->obs_tried=0;s->trend=WX_TREND_NONE;s->alerts=0;s->alert[0]=0;s->fc_at=0;
}
static inline void wx_build_pages(wx_state *s);
/* Gives the channel a finished radar picture (wx_gif_ok, wx_radar_despeckle done), or takes it away
 * (levels NULL). The page of levels must stay as it is until the next call: call this with NULL
 * BEFORE a new picture is decoded into the same memory. Only WX_RADAR_W x WX_RADAR_H is shown.
 * Main loop only. */
static inline void wx_set_radar(wx_state *s,const uint8_t *levels,int w,int h,int64_t taken_epoch) {
    int ok=levels&&w==WX_RADAR_W&&h==WX_RADAR_H&&wx_clock_ok(taken_epoch);
    s->radar=ok?levels:NULL;s->radar_at=ok?(uint32_t)taken_epoch:0;s->radar_show=(uint8_t)ok;s->dirty=1;
}
/* A round of requests has started (1) or ended (0): while it runs the header's date line shows
 * UPDATING every other second (all the time without a clock) and the opening card says it under
 * its last line; no value is covered. wx_tick asks for a redraw when this changes. Main loop only. */
static inline void wx_set_updating(wx_state *s,int on) {
    uint8_t v=(uint8_t)(on!=0);
    if(s->updating!=v){s->updating=v;s->dirty=1;}
}
static inline void wx_init(wx_state *s) {
    memset(s,0,sizeof(*s));
    wx_clear_data(s);
    wx_sound_reset(&s->snd,0);
    wx_build_pages(s);
}
/* The weather location to show (NULL: the default pack). It must stay unchanged and in place while set. The
 * held values stay: the caller clears them (wx_init) when they belong to another place. The almanac
 * is worked out again for the place. Main loop only. */
static inline void wx_set_place(wx_state *s,const wx_place *p) {
    if(s->place!=p){s->place=p;memset(&s->alm,0,sizeof(s->alm));s->dirty=1;}
}

/* After a reply ended. http_status 200 with the reply complete (or everything wanted already
 * read) commits what was staged; anything else leaves the state as it was. epoch_s is the clock
 * now; without a believable clock nothing is committed. Main loop only. */
static inline void wx_fetch_commit(wx_state *s,const wx_fetch *f,int http_status,int64_t epoch_s) {
    if(http_status!=200||f->bad||!wx_clock_ok(epoch_s))return;
    if(!f->done&&!wx_json_complete(&f->js))return; /* cut off before the end */
    uint32_t now=(uint32_t)epoch_s;
    switch(f->kind) {
    case WX_K_OBS:
    case WX_K_RECENT:
    case WX_K_HISTORY: {
        if(f->slot>=WX_STATIONS)return;
        wx_obs *row=&s->obs[f->slot];
        const wx_obs *in=&f->u.obs.out;
        if(f->kind==WX_K_OBS)s->obs_tried|=(uint16_t)(1u<<f->slot);
        if(f->kind==WX_K_HISTORY&&f->u.obs.trend){s->trend=f->u.obs.trend;s->dirty=1;}
        if(!in->time_s||in->time_s>now+3600u||now-in->time_s>WX_OBS_STALE_S)break; /* nothing, or too old to show */
        if(f->kind==WX_K_OBS) { /* the newest report; what it lacks stays from a report up to 2 h older */
            wx_obs fresh=*in;
            if(row->time_s&&row->time_s<=fresh.time_s&&fresh.time_s-row->time_s<=WX_OBS_MERGE_S)wx_obs_fill(&fresh,row);
            *row=fresh;
        } else if(!row->time_s)*row=*in;
        else if(row->time_s>=in->time_s?row->time_s-in->time_s<=WX_OBS_MERGE_S:in->time_s-row->time_s<=WX_OBS_MERGE_S)wx_obs_fill(row,in);
        s->dirty=1;
        break;
    }
    case WX_K_FORECAST:
        if(!f->u.fc.fc.n_local)break;
        s->fc=f->u.fc.fc;s->fc_at=now;
        if(f->u.fc.city_set&&f->slot<WX_CITIES){s->city[f->slot]=f->u.fc.city;s->city_at[f->slot]=now;}
        s->dirty=1;
        break;
    case WX_K_CITY:
        if(!f->u.fc.city_set||f->slot>=WX_CITIES)break;
        s->city[f->slot]=f->u.fc.city;s->city_at[f->slot]=now;s->dirty=1;
        break;
    case WX_K_ALERTS:
        s->alerts=f->u.alert.count;
        memcpy(s->alert,f->u.alert.text,WX_ALERT);s->alert[WX_ALERT-1]=0;
        s->dirty=1;
        break;
    default:
        break;
    }
}

#include "wx_draw.h"

/* ---- the cycle ---- */
static inline void wx_add_page(wx_state *s,unsigned kind,unsigned arg) {
    if(s->n_pages<WX_MAX_PAGES){s->pages[s->n_pages][0]=(uint8_t)kind;s->pages[s->n_pages][1]=(uint8_t)arg;s->n_pages++;}
}
/* The page list for the data held now: the Local Forecast takes one page per period, more when
 * the text is long (at most WX_MAX_LOCAL_PAGES in all); everything else is fixed. The page on
 * show stays on show when the list changes under it. */
static inline void wx_build_pages(wx_state *s) {
    unsigned kind=s->n_pages?s->pages[s->page%WX_MAX_PAGES][0]:WX_P_IDENT,arg=s->n_pages?s->pages[s->page%WX_MAX_PAGES][1]:0;
    int local=0;
    s->n_pages=0;
    wx_add_page(s,WX_P_IDENT,0);wx_add_page(s,WX_P_CUR1,0);wx_add_page(s,WX_P_CUR2,0);
    for(unsigned i=0;i<wx_obs_rows(s);i+=6)wx_add_page(s,WX_P_OBS,i);
    wx_add_page(s,WX_P_ALMANAC,0);
    for(unsigned p=0;p<s->fc.n_local&&p<WX_LOCAL;p++) {
        int lines=wx_wrap_count(s->fc.local[p].text,WX_PS,WX_LOCAL_W);
        for(int k=0;k==0||k*WX_LOCAL_LINES<lines;k++) {
            if(local>=WX_MAX_LOCAL_PAGES||k>15)break;
            wx_add_page(s,WX_P_LOCAL,(p<<4)|(unsigned)k);local++;
        }
    }
    if(!local)wx_add_page(s,WX_P_LOCAL,0);
    wx_add_page(s,WX_P_EXT,0);wx_add_page(s,WX_P_EXT,3);
    if(wx_shows_regional(s)){wx_add_page(s,WX_P_STATE,0);wx_add_page(s,WX_P_TRI,0);}
    wx_add_page(s,s->radar&&s->radar_show?WX_P_RADAR:WX_P_OUTLOOK,0); /* no radar picture: the rain outlook */
    unsigned at=s->n_pages;
    for(unsigned i=0;i<s->n_pages;i++)if(s->pages[i][0]==kind&&s->pages[i][1]==arg){at=i;break;}
    if(at==s->n_pages)for(unsigned i=0;i<s->n_pages;i++)if(s->pages[i][0]==kind){at=i;break;}
    s->page=(uint8_t)(at<s->n_pages?at:0);
}
/* The announcer line that opens the section a page belongs to (0 = none). */
static inline unsigned wx_page_line(const wx_state *s,unsigned page) {
    unsigned kind=s->pages[page%WX_MAX_PAGES][0],before=page?s->pages[page-1][0]:WX_P_KINDS;
    if(kind==before)return 0;
    switch(kind) {
    case WX_P_IDENT:return s->alerts?WX_CLIP_ALERT:0;
    case WX_P_CUR1:return WX_CLIP_CURRENT;
    case WX_P_LOCAL:return WX_CLIP_LOCAL;
    case WX_P_EXT:return WX_CLIP_EXTENDED;
    case WX_P_STATE:return WX_CLIP_REGIONAL;
    case WX_P_RADAR:return WX_CLIP_RADAR;
    default:return 0;
    }
}
/* The channel was tuned: the cycle starts again at the opening card, the music from its start. */
static inline void wx_enter(wx_state *s,uint32_t now_ms) {
    s->page=0;s->n_pages=0;
    wx_build_pages(s);
    s->page=0;s->enter_ms=s->page_ms=now_ms;s->dirty=1;
    s->seen_page=255;s->seen_sec=0;s->seen_ticker=0;
    wx_sound_reset(&s->snd,now_ms);
    wx_sound_say(&s->snd,wx_page_line(s,0));
}
/* Every main-loop pass while the channel is on. Returns 1 when the frame should be drawn again:
 * a new page, the clock's second, a step of the ticker (at most ten a second), new data.
 * epoch_s 0 (or any clock that is not believable): the clock shows "--:--". */
static inline int wx_tick(wx_state *s,uint32_t now_ms,int64_t epoch_s) {
    int redraw=0,clock=wx_clock_ok(epoch_s);
    if(!s->n_pages)wx_build_pages(s);
    if(clock) {
        uint32_t now=(uint32_t)epoch_s;
        if(now!=s->seen_sec) { /* once a second: the almanac's day, and data too old to show */
            wx_time t;
            wx_local_time_z(epoch_s,wx_state_tz(s),&t);
            if(s->alm.day!=t.days||now<s->alm.base||now-s->alm.base>21600u){
                wx_almanac_begin_z(&s->alm,epoch_s,wx_home_lat(s),wx_home_lon(s),wx_state_tz(s));s->dirty=1;
            }
            for(int i=0;i<WX_STATIONS;i++)if(s->obs[i].time_s&&now>s->obs[i].time_s&&now-s->obs[i].time_s>WX_OBS_STALE_S+3600u){wx_obs_clear(&s->obs[i]);s->dirty=1;}
            if(s->radar_show&&now>s->radar_at&&now-s->radar_at>WX_RADAR_FRESH_S){s->radar_show=0;s->dirty=1;}
            if(s->fc_at&&now>s->fc_at&&now-s->fc_at>WX_FC_STALE_S){memset(&s->fc,0,sizeof(s->fc));s->fc_at=0;s->dirty=1;}
            for(int i=0;i<WX_CITIES;i++)if(s->city_at[i]&&now>s->city_at[i]&&now-s->city_at[i]>WX_FC_STALE_S) {
                memset(&s->city[i],0,sizeof(s->city[i]));s->city[i].temp=WX_NA;s->city[i].pop=-1;s->city_at[i]=0;s->dirty=1;
            }
        }
        if(s->alm.day&&!s->alm.moon_ready&&wx_almanac_step(&s->alm))s->dirty=1;
    } else if(s->alm.day){memset(&s->alm,0,sizeof(s->alm));s->dirty=1;}
    if(s->dirty){wx_build_pages(s);s->dirty=0;redraw=1;}
    uint32_t hold=s->pages[s->page][0]==WX_P_IDENT?WX_IDENT_MS:WX_PAGE_MS;
    if((uint32_t)(now_ms-s->page_ms)>=hold) {
        s->page=(uint8_t)(s->page+1u<s->n_pages?s->page+1u:0u);
        s->page_ms=(uint32_t)(now_ms-s->page_ms)>=2u*hold?now_ms:s->page_ms+hold; /* a late loop does not shorten the next page */
        wx_sound_say(&s->snd,wx_page_line(s,s->page));
    }
    uint32_t sec=clock?(uint32_t)epoch_s:0,ticker=wx_ticker_phase(s,(uint32_t)(now_ms-s->enter_ms));
    if(s->page!=s->seen_page||sec!=s->seen_sec||ticker!=s->seen_ticker)redraw=1;
    s->seen_page=s->page;s->seen_sec=sec;s->seen_ticker=ticker;
    return redraw;
}
/* The whole 160x144 frame, RGB565, row-major. */
static inline void wx_draw(const wx_state *s,uint16_t *fb,uint32_t now_ms,int64_t epoch_s) {
    wx_gc g={fb,0,0,WX_W,WX_H_PX};
    unsigned page=s->n_pages?s->page%s->n_pages:0,kind=s->n_pages?s->pages[page][0]:WX_P_IDENT,arg=s->n_pages?s->pages[page][1]:0;
    wx_gc body=g;
    wx_backdrop(&g);
    body.y1=131; /* nothing of a page reaches the ticker */
    switch(kind) {
    case WX_P_CUR1:wx_page_current_1(&body,s,epoch_s);break;
    case WX_P_CUR2:wx_page_current_2(&body,s);break;
    case WX_P_OBS:wx_page_obs(&body,s,(int)arg);break;
    case WX_P_ALMANAC:wx_page_almanac(&body,s);break;
    case WX_P_LOCAL:wx_page_local(&body,s,arg);break;
    case WX_P_EXT:wx_page_extended(&body,s,(int)arg);break;
    case WX_P_STATE:wx_page_regional(&body,s,0);break;
    case WX_P_TRI:wx_page_regional(&body,s,1);break;
    case WX_P_OUTLOOK:wx_page_outlook(&body,s);break;
    case WX_P_RADAR:if(s->radar)wx_page_radar(&body,s);else wx_page_outlook(&body,s);break;
    default:wx_page_ident(&body,s,s->updating);kind=WX_P_IDENT;break;
    }
    if(kind!=WX_P_IDENT)wx_header(&g,kind,epoch_s,s->updating,wx_state_tz(s));
    wx_ticker(&g,s,(uint32_t)(now_ms-s->enter_ms));
}
/* Sound: call every main-loop pass with the sink (see wx_sound.h). Never blocks; with no sound
 * hardware simply do not call it. */
static inline void wx_audio_feed(wx_state *s,uint32_t now_ms,wx_audio_write write,void *ctx){wx_sound_feed(&s->snd,now_ms,write,ctx);}
#endif

