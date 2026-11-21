/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: a weather location other than the compiled-in default (1.6.3; 1.6.4: the
 * default is the Orlando, FL pack, and a record naming a pack's home shows that pack whole; 1.6.5: a
 * location pack (wx_lpack.h) that names the record's place gives it its own area pages).
 *
 * Chromatic Manager writes one 256-byte record, the same in two flash sectors (WX_PLACE_FLASH_A and
 * _B, inside the launcher partition, below the setup record and above every launcher image), A first,
 * then B. This file reads and checks it and turns it into the channel's request list. Everything is
 * checked: magic, version, length, CRC-32, a non-zero sequence, every field's range and characters,
 * reserved bytes zero. Anything else (blank, torn, corrupt, another version) means "no record": the
 * channel is the compiled-in default pack of wx_location.h (Orlando, FL). Of two good copies
 * the newer sequence wins. The writer is scripts/chromatic_weather_location.py (pack()); the two
 * agree on the vectors of scripts/test_chromatic_weather_place.py.
 *
 * Record, little-endian:
 *   0  "PJWX"          4  version (1)      5  stations (1..6)  6  length (256)   8  sequence (!= 0)
 *  12  standard offset, minutes (int16)   14  US daylight rule (0/1)          15  0
 *  16  latitude x 1e4 (int32)             20  longitude x 1e4 (int32)
 *  24  grid x (uint16)  26  grid y        28  forecast office, 3 letters, 0   32  state, 2 letters  34 0 0
 *  36  name, 24 bytes ("DENVER, CO")      60  station ids, 6 x 8              108 labels, 6 x 16
 * 204  48 bytes 0                         252 CRC-32 (IEEE) of bytes 0..251
 * Strings are NUL-padded, capitals, digits and " ,.'-/" only (ids: capitals and digits), so every
 * path built from them is plain.
 *
 * What a place without a pack shows, and why, is in docs/Design-notes.md
 * ("Weather location"). Portable C99, no allocation, no I/O. Part of wx.h; include that.
 */
#ifndef WX_PLACE_H
#define WX_PLACE_H
#ifndef WX_H
#error "include wx.h"
#endif

#define WX_PLACE_FLASH_A 0x3FC000u   /* absolute flash addresses: launcher slot 0x260000 + 0x19C000 / 0x19D000 */
#define WX_PLACE_FLASH_B 0x3FD000u
#define WX_PLACE_RECORD 256u
#define WX_PLACE_VERSION 1u
#define WX_PLACE_STATIONS 6
#define WX_PLACE_NAME 24
#define WX_PLACE_IDLEN 8
#define WX_PLACE_LABEL 16
/* 1.6.4: a place keeps Current Radar when it lies inside its region pack's radar view (wx_pack radar_w..radar_n:
 * the built-in Orlando pack's KMLB view). */
/* the most requests a place makes: home observation, forecast, alerts, history, its region's (nine) cities,
 * five other stations' latest and recent reports, the radar */
#define WX_PLACE_REQUESTS (4+WX_CITIES+2*(WX_PLACE_STATIONS-1)+1)
#if WX_PLACE_REQUESTS > WX_REQUESTS || WX_PLACE_STATIONS > WX_STATIONS || WX_PLACE_STATIONS > WX_OBS_ROWS
#error "a place's tables must fit the compiled-in ones"
#endif

typedef struct wx_place {
    uint32_t id;                         /* the record's CRC-32 (1 if that was 0): which place cached values belong to */
    uint32_t sequence;
    int32_t lat_e4,lon_e4;
    uint16_t grid_x,grid_y;
    wx_tz tz;
    uint8_t n_stations;
    uint8_t radar;                       /* inside its region pack's radar view: Current Radar */
    /* 1.6.4: the built-in pack of its state (Florida; NULL elsewhere): its two Regional Forecast pages; builtin:
     * the record names that pack's home exactly (point, office, grid), so the pack is shown whole. 1.6.5: a location
     * pack naming the place exactly (wx_place_use_pack) takes this place, builtin, with its whole table. */
    const wx_pack *pack;
    uint8_t builtin;
    char name[WX_PLACE_NAME];
    char area[WX_PLACE_NAME+6];          /* "DENVER AREA" */
    char state[3];
    char office[5];
    char station[WX_PLACE_STATIONS][WX_PLACE_IDLEN];
    char label[WX_PLACE_STATIONS][WX_PLACE_LABEL];
} wx_place;

static inline uint32_t wx_crc32(const uint8_t *p,size_t n) {
    uint32_t c=0xFFFFFFFFu;
    for(size_t i=0;i<n;i++){c^=p[i];for(int k=0;k<8;k++)c=c&1u?(c>>1)^0xEDB88320u:c>>1;}
    return ~c;
}
static inline uint32_t wx_le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static inline uint16_t wx_le16(const uint8_t *p){return (uint16_t)(p[0]|p[1]<<8);}
/* A NUL-padded text field of `size` bytes: `min`..size-1 characters, then only NULs. kind 0: a name
 * or label (first a capital, then capitals, digits, " ,.'-/"), 1: an id (capitals and digits), 2:
 * capitals only. Copied out terminated. 0 when it is not one. */
static inline int wx_place_text(const uint8_t *field,size_t size,size_t min,int kind,char *out) {
    size_t n=0;
    while(n<size&&field[n])n++;
    if(n<min||n>=size)return 0;
    for(size_t i=n;i<size;i++)if(field[i])return 0;
    for(size_t i=0;i<n;i++) {
        unsigned char c=field[i];
        int cap=c>='A'&&c<='Z',dig=c>='0'&&c<='9';
        int ok=kind==2?cap:kind==1?cap||dig:i==0?cap:cap||dig||c==' '||c==','||c=='.'||c=='\''||c=='-'||c=='/';
        if(!ok)return 0;
        out[i]=(char)c;
    }
    out[n]=0;
    if(kind==0&&out[n-1]==' ')return 0;
    return 1;
}
/* One copy of the record (WX_PLACE_RECORD bytes as read from flash) into `out`. 1 when it is a good
 * record, else 0 and `out` is cleared. */
static inline int wx_place_parse(const uint8_t *r,size_t size,wx_place *out) {
    memset(out,0,sizeof(*out));
    if(!r||size<WX_PLACE_RECORD)return 0;
    if(memcmp(r,"PJWX",4)||r[4]!=WX_PLACE_VERSION||wx_le16(r+6)!=WX_PLACE_RECORD)goto bad;
    uint32_t crc=wx_crc32(r,WX_PLACE_RECORD-4u);
    if(crc!=wx_le32(r+WX_PLACE_RECORD-4u))goto bad;
    out->sequence=wx_le32(r+8);
    unsigned n=r[5];
    int16_t std=(int16_t)wx_le16(r+12);
    out->lat_e4=(int32_t)wx_le32(r+16);out->lon_e4=(int32_t)wx_le32(r+20);
    out->grid_x=wx_le16(r+24);out->grid_y=wx_le16(r+26);
    if(!out->sequence||n<1u||n>WX_PLACE_STATIONS||std<-720||std>840||std%15||r[14]>1u||r[15]||r[34]||r[35])goto bad;
    if(out->lat_e4<-900000||out->lat_e4>900000||out->lon_e4<-1800000||out->lon_e4>1800000)goto bad;
    if(out->grid_x>9999u||out->grid_y>9999u)goto bad;
    for(unsigned i=204;i<WX_PLACE_RECORD-4u;i++)if(r[i])goto bad;
    if(!wx_place_text(r+28,4,3,2,out->office))goto bad;  /* forecast offices are three letters */
    if(!(r[32]>='A'&&r[32]<='Z'&&r[33]>='A'&&r[33]<='Z'))goto bad;
    out->state[0]=(char)r[32];out->state[1]=(char)r[33];out->state[2]=0;
    if(!wx_place_text(r+36,WX_PLACE_NAME,1,0,out->name))goto bad;
    for(unsigned i=0;i<WX_PLACE_STATIONS;i++) {
        const uint8_t *id=r+60+i*WX_PLACE_IDLEN,*label=r+108+i*WX_PLACE_LABEL;
        if(i<n) {
            if(!wx_place_text(id,WX_PLACE_IDLEN,3,1,out->station[i])||!wx_place_text(label,WX_PLACE_LABEL,1,0,out->label[i]))goto bad;
        } else {
            for(unsigned k=0;k<WX_PLACE_IDLEN;k++)if(id[k])goto bad;
            for(unsigned k=0;k<WX_PLACE_LABEL;k++)if(label[k])goto bad;
        }
    }
    out->n_stations=(uint8_t)n;
    out->tz.std_min=std;out->tz.dst=r[14];
    out->id=crc?crc:1u;
    for(unsigned k=0;k<WX_PACKS;k++)if(!strcmp(out->state,wx_packs[k]->state))out->pack=wx_packs[k];
    if(out->pack) {
        const wx_pack *pk=out->pack;
        out->radar=(uint8_t)(out->lon_e4>=pk->radar_w&&out->lon_e4<=pk->radar_e&&out->lat_e4>=pk->radar_s&&out->lat_e4<=pk->radar_n);
        out->builtin=(uint8_t)(out->lat_e4==pk->lat_e4&&out->lon_e4==pk->lon_e4&&!strcmp(out->office,pk->office)
                               &&out->grid_x==pk->grid_x&&out->grid_y==pk->grid_y);
    }
    { /* "DENVER, CO" -> "DENVER AREA" */
        size_t k=0;
        while(out->name[k]&&out->name[k]!=',')k++;
        while(k&&out->name[k-1]==' ')k--;
        memcpy(out->area,out->name,k);out->area[k]=0;
        wx_append(out->area,sizeof(out->area)," AREA");
    }
    return 1;
bad:
    memset(out,0,sizeof(*out));
    return 0;
}
/* The record from its two copies: the good one, or of two good ones the newer sequence (A when they
 * are equal). 1 with `out` filled, 0 for "no record" (the default pack). `spare` is working room for the
 * second copy's check (a wx_place the caller lends, so nothing large lives on a stack). */
static inline int wx_place_pick(const uint8_t *a,const uint8_t *b,size_t size,wx_place *out,wx_place *spare) {
    int ga=wx_place_parse(a,size,out),gb=wx_place_parse(b,size,spare);
    if(gb&&(!ga||(int32_t)(spare->sequence-out->sequence)>0)){*out=*spare;ga=1;}
    memset(spare,0,sizeof(*spare));
    return ga;
}
/* 1.6.5: does the location pack name this place exactly (state, point, office, grid)? */
static inline int wx_lpack_binds(const wx_lpack *lp,const wx_place *p) {
    const wx_pack *k=&lp->pack;
    return p&&k->requests&&!strcmp(k->state,p->state)&&k->lat_e4==p->lat_e4&&k->lon_e4==p->lon_e4&&!strcmp(k->office,p->office)
           &&k->grid_x==p->grid_x&&k->grid_y==p->grid_y;
}
/* 1.6.5: a good location pack that names `p` becomes its pack, whole (its table, rows, maps and radar), like a built-in
 * one; the place's id then also says which pack, so values held for the place without it start clean. 1 when used. */
static inline int wx_place_use_pack(wx_place *p,const wx_lpack *lp) {
    if(!lp||!wx_lpack_binds(lp,p))return 0;
    p->pack=&lp->pack;p->builtin=1;
    p->radar=0;
    for(unsigned i=0;i<lp->pack.requests;i++)if(lp->pack.req[i].kind==WX_K_RADAR)p->radar=1;
    p->id^=lp->crc;if(!p->id)p->id=1u;
    return 1;
}

/* ---- the request list of a place (NULL: the default pack's list; a record naming a pack: that pack's list) ---- */
typedef struct {const char *host;uint8_t kind,slot;uint32_t period_s;} wx_req_info;
/* the pack whose whole table a place uses, or NULL for its own list */
static inline const wx_pack *wx_place_tables(const wx_place *p){return !p?WX_DEFAULT_PACK:p->builtin?p->pack:NULL;}
static inline unsigned wx_place_request_count(const wx_place *p) {
    const wx_pack *t=wx_place_tables(p);
    if(t)return t->requests;
    return 4u+(p->pack?(unsigned)p->pack->cities:0u)+2u*(p->n_stations-1u)+(p->radar?1u:0u);
}
/* the n-th entry of a kind in a pack's table (FORECAST counts as the home city's CITY): its index, or -1 */
static inline int wx_pack_nth(const wx_pack *pk,unsigned kind,unsigned n) {
    for(unsigned i=0;i<pk->requests;i++) {
        unsigned k=pk->req[i].kind;
        if(kind==WX_K_CITY?(k==WX_K_CITY||k==WX_K_FORECAST):k==kind){if(!n)return (int)i;n--;}
    }
    return -1;
}
static inline void wx_append_e4(char *out,size_t cap,int32_t v) {
    uint32_t u=v<0?(uint32_t)-(int64_t)v:(uint32_t)v;
    char frac[6]={'.',(char)('0'+u/1000u%10u),(char)('0'+u/100u%10u),(char)('0'+u/10u%10u),(char)('0'+u%10u),0};
    if(v<0)wx_append(out,cap,"-");
    wx_append_int(out,cap,(int)(u/10000u));wx_append(out,cap,frac);
}
/* Request `index` of place `p`: what it is (host, kind, slot, period) and its whole address
 * "https://host/path" in `url` (url NULL: what it is only). 0 when there is no such request or the
 * address does not fit. */
static inline int wx_place_request(const wx_place *p,unsigned index,wx_req_info *info,char *url,size_t cap) {
    char none[1];
    int want_url=url!=NULL;
    if(!want_url){url=none;cap=sizeof(none);}
    if(!info||(want_url&&cap<16))return 0;
    url[0]=0;
    const wx_pack *t=wx_place_tables(p);
    if(t) { /* a pack's table, byte for byte */
        if(index>=t->requests)return 0;
        const wx_request *r=&t->req[index];
        info->host=r->host;info->kind=r->kind;info->slot=r->slot;info->period_s=t->period[index];
        if(!want_url)return 1;
        wx_append(url,cap,"https://");wx_append(url,cap,r->host);
        return wx_append(url,cap,r->path)==strlen("https://")+strlen(r->host)+strlen(r->path);
    }
    if(index>=wx_place_request_count(p))return 0;
    const char *api=WX_HOST;  /* the shared host pointers (wx_location.h): a host is told apart by pointer */
    const wx_pack *pk=p->pack;
    unsigned cities=pk?(unsigned)pk->cities:0u,others=p->n_stations-1u;
    info->host=api;info->slot=0;
    wx_append(url,cap,"https://");wx_append(url,cap,api);
    if(index==0||index==3) {
        info->kind=(uint8_t)(index?WX_K_HISTORY:WX_K_OBS);info->period_s=index?3600u:600u;
        wx_append(url,cap,"/stations/");wx_append(url,cap,p->station[0]);
        wx_append(url,cap,index?"/observations?limit=40":"/observations/latest");
    } else if(index==1) {
        info->kind=WX_K_FORECAST;info->slot=255;info->period_s=1800u; /* no regional figure: the home city is the region's own request */
        wx_append(url,cap,"/gridpoints/");wx_append(url,cap,p->office);wx_append(url,cap,"/");
        wx_append_int(url,cap,p->grid_x);wx_append(url,cap,",");wx_append_int(url,cap,p->grid_y);wx_append(url,cap,"/forecast");
    } else if(index==2) {
        info->kind=WX_K_ALERTS;info->period_s=600u;
        wx_append(url,cap,"/alerts/active?point=");wx_append_e4(url,cap,p->lat_e4);wx_append(url,cap,",");wx_append_e4(url,cap,p->lon_e4);
    } else if(index<4u+cities) {
        int at=wx_pack_nth(pk,WX_K_CITY,index-4u);
        if(at<0)return 0;
        const wx_request *r=&pk->req[at];
        info->kind=WX_K_CITY;info->slot=r->kind==WX_K_FORECAST?pk->home_city:r->slot;info->period_s=3600u;
        wx_append(url,cap,r->path);
    } else if(index<4u+cities+2u*others) {
        unsigned k=index-4u-cities,recent=k>=others,slot=(recent?k-others:k)+1u;
        info->kind=(uint8_t)(recent?WX_K_RECENT:WX_K_OBS);info->slot=(uint8_t)slot;info->period_s=1800u;
        wx_append(url,cap,"/stations/");wx_append(url,cap,p->station[slot]);
        wx_append(url,cap,recent?"/observations?limit=8":"/observations/latest");
    } else {
        int at=pk?wx_pack_nth(pk,WX_K_RADAR,0):-1;
        if(at<0)return 0;
        const wx_request *r=&pk->req[at];
        info->host=r->host;info->kind=WX_K_RADAR;info->period_s=600u;
        url[0]=0;wx_append(url,cap,"https://");wx_append(url,cap,r->host);wx_append(url,cap,r->path);
    }
    if(!want_url)return 1;
    size_t n=strlen(url);
    return n+1<cap; /* a full buffer may have cut it */
}
static inline int wx_place_info(const wx_place *p,unsigned index,wx_req_info *info){return wx_place_request(p,index,info,NULL,0);}
#endif

