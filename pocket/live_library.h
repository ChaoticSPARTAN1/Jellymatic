/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Native Home/browse/details model fed by whitelisted PJB1 replies. The same
 * bytes arrive from the on-card library and from the tethered Gateway. */
#ifndef PJ_LIVE_LIBRARY_H
#define PJ_LIVE_LIBRARY_H
#include "../shared/bounded_json.h"
#include "../shared/bridge_rpc.h"
#define PJ_HOME_ROWS 12u
#define PJ_HOME_ITEMS 144u
typedef struct {
    uint32_t handle;char title[161],subtitle[81],rating[12];uint64_t duration_ticks,resume_ticks;
    uint16_t year;uint8_t folder,played,unavailable,episode;
    /* The app's own "On this Pocket" tile (live_player.c card_tile_add): opens the card's library.
     * Never sent by a source (pj_library_items clears it). */
    uint8_t card_tile;
    /* Tether art keys (shared/pocket_tether.h): the picture this row shows and the backdrop.
     * 0 when the source sends none. */
    uint32_t art_key,hero_key;
} pj_library_item;
typedef struct {char title[97];unsigned first,count,shape;} pj_library_row;
typedef struct {
    pj_library_item items[PJ_HOME_ITEMS];pj_library_row rows[PJ_HOME_ROWS];
    unsigned count,row_count;uint32_t next,total;int truncated;
    pj_json_token tokens[4096];
} pj_live_library;
typedef struct {uint32_t handle;char overview[1200],genres[96];int valid;} pj_details;
static inline void pj_json_text_optional(const pj_json *j,int object,const char *key,char *out,size_t size) {
    out[0]=0;int at=pj_json_find(j,object,key);
    if(at>=0&&!pj_json_text(j,at,out,size))out[0]=0;
}
static inline uint64_t pj_json_u64_optional(const pj_json *j,int object,const char *key) {
    uint64_t value=0;int at=pj_json_find(j,object,key);
    return at>=0&&pj_json_u64(j,at,&value)?value:0;
}
static inline int pj_library_items(pj_live_library *m,const pj_json *j,int array,unsigned limit) {
    if(array<0||j->tokens[array].type!=PJ_JSON_ARRAY)return 0;
    unsigned count=0;
    for(unsigned i=(unsigned)array+1;i<j->tokens[array].next;i=j->tokens[i].next) {
        if(count++>=limit||m->count>=PJ_HOME_ITEMS||j->tokens[i].type!=PJ_JSON_OBJECT)return 0;
        pj_library_item *item=&m->items[m->count];memset(item,0,sizeof(*item));uint64_t value;
        if(!pj_json_u64(j,pj_json_find(j,(int)i,"handle"),&value)||!value||value>0xffffffffu)return 0;
        item->handle=(uint32_t)value;
        if(!pj_json_text(j,pj_json_find(j,(int)i,"title"),item->title,sizeof(item->title)))return 0;
        if(!pj_json_u64(j,pj_json_find(j,(int)i,"duration_ticks"),&item->duration_ticks)||
           !pj_json_u64(j,pj_json_find(j,(int)i,"resume_ticks"),&item->resume_ticks))return 0;
        item->folder=(uint8_t)pj_json_true(j,pj_json_find(j,(int)i,"folder"));
        item->played=(uint8_t)pj_json_true(j,pj_json_find(j,(int)i,"played"));
        item->unavailable=(uint8_t)pj_json_true(j,pj_json_find(j,(int)i,"unavailable"));
        uint64_t year=pj_json_u64_optional(j,(int)i,"year");
        item->year=year<=9999?(uint16_t)year:0;
        pj_json_text_optional(j,(int)i,"subtitle",item->subtitle,sizeof(item->subtitle));
        pj_json_text_optional(j,(int)i,"rating",item->rating,sizeof(item->rating));
        uint64_t key=pj_json_u64_optional(j,(int)i,"art");item->art_key=key<=0xffffffffu?(uint32_t)key:0;
        key=pj_json_u64_optional(j,(int)i,"hero");item->hero_key=key<=0xffffffffu?(uint32_t)key:0;
        char type[12];pj_json_text_optional(j,(int)i,"type",type,sizeof(type));
        item->episode=(uint8_t)!strcmp(type,"Episode");
        m->count++;
    }
    return 1;
}
static inline unsigned pj_library_shape(const pj_json *j,int object) {
    return pj_json_u64_optional(j,object,"shape")==PJ_ART_THUMB?PJ_ART_THUMB:PJ_ART_POSTER;
}
static inline int pj_library_load(pj_live_library *m,const uint8_t *data,size_t size,int home) {
    pj_json j; m->count=m->row_count=0;m->next=m->total=0;m->truncated=0;
    if(!pj_json_parse(&j,data,size,m->tokens,4096)||j.tokens[0].type!=PJ_JSON_OBJECT)return 0;
    if(!home) {
        strcpy(m->rows[0].title,"Library");m->rows[0].first=0;m->rows[0].shape=pj_library_shape(&j,0);
        if(!pj_library_items(m,&j,pj_json_find(&j,0,"items"),12))return 0;
        m->rows[0].count=m->count;m->row_count=1;uint64_t value;
        if(!pj_json_u64(&j,pj_json_find(&j,0,"next"),&value)||value>0xffffffffu)return 0;
        m->next=(uint32_t)value;
        if(!pj_json_u64(&j,pj_json_find(&j,0,"total"),&value)||value>0xffffffffu)return 0;
        m->total=(uint32_t)value;return 1;
    }
    int rows=pj_json_find(&j,0,"rows");
    if(rows<0||j.tokens[rows].type!=PJ_JSON_ARRAY)return 0;
    m->truncated=pj_json_true(&j,pj_json_find(&j,0,"truncated"));
    for(unsigned i=(unsigned)rows+1;i<j.tokens[rows].next;i=j.tokens[i].next) {
        if(m->row_count>=PJ_HOME_ROWS)return 0;
        pj_library_row *row=&m->rows[m->row_count++];row->first=m->count;
        if(!pj_json_text(&j,pj_json_find(&j,(int)i,"title"),row->title,sizeof(row->title))||
           !pj_library_items(m,&j,pj_json_find(&j,(int)i,"items"),12))return 0;
        row->count=m->count-row->first;row->shape=pj_library_shape(&j,(int)i);
    }
    return 1;
}
/* Details add text to an item already in the model; the tile stays usable if
 * this reply is missing, so failure here is never fatal. */
static inline int pj_details_load(pj_details *d,pj_json_token *tokens,uint32_t handle,const uint8_t *p,size_t n) {
    pj_json j;d->valid=0;d->handle=handle;
    if(!pj_json_parse(&j,p,n,tokens,1024)||j.tokens[0].type!=PJ_JSON_OBJECT)return 0;
    pj_json_text_optional(&j,0,"overview",d->overview,sizeof(d->overview));
    d->genres[0]=0;
    int genres=pj_json_find(&j,0,"genres");
    if(genres>=0&&j.tokens[genres].type==PJ_JSON_ARRAY) {
        size_t used=0;unsigned shown=0;
        for(unsigned i=(unsigned)genres+1;i<j.tokens[genres].next&&shown<3;i=j.tokens[i].next,shown++) {
            char genre[40];
            if(!pj_json_text(&j,(int)i,genre,sizeof(genre)))break;
            size_t length=strlen(genre);
            if(used+length+3>=sizeof(d->genres))break;
            if(used){memcpy(d->genres+used,", ",2);used+=2;}
            memcpy(d->genres+used,genre,length);used+=length;d->genres[used]=0;
        }
    }
    d->valid=1;return 1;
}
/* One artwork download. Missing art is valid, wrong geometry/length is not. */
typedef struct {
    uint32_t handle,total,used;unsigned kind;
    uint8_t bytes[PJ_ART_BACKDROP_W*PJ_ART_BACKDROP_H*2];int complete;
} pj_art_download;
static inline void pj_art_begin(pj_art_download *a,uint32_t handle,unsigned kind) {
    a->handle=handle;a->kind=kind;a->total=a->used=0;a->complete=0;
}
static inline int pj_art_chunk(pj_art_download *a,const uint8_t *p,size_t n) {
    unsigned width=pj_art_width(a->kind),height=pj_art_height(a->kind);
    if(a->complete||n<8||n>PJ_RPC_LIMIT||pj_av_u16(p+4)!=width||pj_av_u16(p+6)!=height)return 0;
    uint32_t total=pj_av_u32(p);
    if(total!=0&&total!=width*height*2)return 0;
    if(a->used&&a->total!=total)return 0;
    if(n-8>total-a->used||(n==8&&total!=0))return 0;
    a->total=total;memcpy(a->bytes+a->used,p+8,n-8);a->used+=(uint32_t)n-8;
    a->complete=a->used==total;return 1;
}
#endif
