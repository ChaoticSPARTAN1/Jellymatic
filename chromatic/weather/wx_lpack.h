/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: a location pack (launcher 1.6.5): the area pages (Regional Forecast's state map and area
 * close-up, Current Radar's view) and the full observation list of one weather location, made on the PC
 * (scripts/make_location_pack.py) and written by Chromatic Manager to two 8 KiB copies at WX_LPACK_FLASH_A/_B.
 * The layout is scripts/chromatic_location_pack.py's; the two agree on the vectors of
 * scripts/test_chromatic_weather_place.py.
 *
 * Everything is checked before anything is used: magic, version, lengths, CRC-32, a non-zero sequence, reserved bytes,
 * every count and index, every string's characters, every request's kind, slot, host, period and the shape of its
 * path, every map's run-length rows (exactly 160 pixels a row, colours of the palette), every place on the screen. A
 * pack failing any check is not used (the generic pages stay). Hosts are never in a pack: the request's host is one
 * of the two compiled-in names. A good pack is used only for the weather location whose state, point, office and grid
 * it names (wx_lpack_binds); it is then that place's whole table, exactly like a built-in pack.
 *
 * The parsed pack points into the bytes it was read from: they must stay unchanged and in place while it is used. */
#ifndef WX_LPACK_H
#define WX_LPACK_H
#include <stdint.h>
#include <string.h>
#include "wx_location.h"

#define WX_LPACK_FLASH_A 0x3F6000u
#define WX_LPACK_FLASH_B 0x3F8000u
#define WX_LPACK_COPY 0x2000u      /* one copy's area (8 KiB); a pack is at most this long */
#define WX_LPACK_HEADER 80u
#define WX_LPACK_VERSION 1u
#define WX_LPACK_MAP_TOP 23
#define WX_LPACK_MAP_BOTTOM 131
#define WX_LPACK_RADAR_ROWS 97

typedef struct {
    wx_pack pack;                               /* what the pages and the request list use */
    wx_request req[WX_REQUESTS];
    uint16_t period[WX_REQUESTS];
    const char *obs_label[WX_OBS_ROWS];
    uint8_t obs_slot[WX_OBS_ROWS];
    const char *city_name[WX_CITIES];
    const char *radar_town[3];
    uint8_t radar_marks[6];
    char state[3],office[4];
    uint32_t sequence,crc;
} wx_lpack;

static inline uint32_t wx_lp_le32(const uint8_t *p){return (uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static inline uint16_t wx_lp_le16(const uint8_t *p){return (uint16_t)(p[0]|p[1]<<8);}
static inline uint32_t wx_lp_crc32(const uint8_t *p,size_t n) {
    uint32_t c=0xFFFFFFFFu;
    for(size_t i=0;i<n;i++){c^=p[i];for(int k=0;k<8;k++)c=c&1u?(c>>1)^0xEDB88320u:c>>1;}
    return ~c;
}
/* A text of the pack: lo..hi characters, a capital or digit first, then capitals, digits and " ,.'-/", no trailing
 * space (empty only when lo is 0). */
static inline int wx_lp_text(const char *t,size_t lo,size_t hi) {
    size_t n=strlen(t);
    if(n<lo||n>hi)return 0;
    for(size_t i=0;i<n;i++) {
        unsigned char c=(unsigned char)t[i];
        int cap=c>='A'&&c<='Z',dig=c>='0'&&c<='9';
        if(!(cap||dig||(i&&(c==' '||c==','||c=='.'||c=='\''||c=='-'||c=='/'))))return 0;
    }
    return !n||t[n-1]!=' ';
}
static inline int wx_lp_path_chars(const char *t) {
    size_t n=strlen(t);
    if(n<1||n>255)return 0;
    for(size_t i=0;i<n;i++) {
        unsigned char c=(unsigned char)t[i];
        if(!((c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')||strchr("/?=&.,:_%-",c)))return 0;
    }
    return 1;
}
static inline int wx_lp_prefix(const char *t,const char *p){return !strncmp(t,p,strlen(p));}
/* Run-length rows: (count >= 1, colour < 16 or 255) pairs, every row exactly 160 pixels, nothing left over. */
static inline int wx_lp_rle(const uint8_t *d,size_t n,unsigned rows) {
    size_t at=0;
    for(unsigned y=0;y<rows;y++) {
        unsigned x=0;
        while(x<160u) {
            if(at+2u>n)return 0;
            unsigned count=d[at],colour=d[at+1];
            if(!count||(colour>=16u&&colour!=255u)||x+count>160u)return 0;
            x+=count;at+=2;
        }
    }
    return at==n;
}
/* One copy (as read from flash, at least its first WX_LPACK_HEADER bytes for the length) into `out`. 1 when it is a
 * good pack; 0 and `out` cleared otherwise. */
static inline int wx_lpack_parse(const uint8_t *r,size_t size,wx_lpack *out) {
    memset(out,0,sizeof(*out));
    if(!r||size<WX_LPACK_HEADER+4u)return 0;
    if(memcmp(r,"PJLP",4)||r[4]!=WX_LPACK_VERSION)goto bad;
    uint32_t total=wx_lp_le32(r+8),seq=wx_lp_le32(r+12);
    unsigned n_req=r[5],n_cities=r[6],stations=r[7],obs_rows=r[34],home=r[35];
    if(total<WX_LPACK_HEADER+4u||total>WX_LPACK_COPY||total>size||!seq)goto bad;
    for(unsigned i=74;i<80;i++)if(r[i])goto bad;
    if(wx_lp_crc32(r,total-4u)!=wx_lp_le32(r+total-4u))goto bad;
    if(n_req<1||n_req>WX_REQUESTS||n_cities<1||n_cities>WX_CITIES||stations<1||stations>WX_STATIONS||obs_rows<1||obs_rows>WX_OBS_ROWS)goto bad;
    unsigned sn=wx_lp_le16(r+66),cn=wx_lp_le16(r+68),rmn=wx_lp_le16(r+70),strn=wx_lp_le16(r+72);
    if(WX_LPACK_HEADER+4u*n_cities+obs_rows+5u*n_req+sn+cn+rmn+strn+4u!=total)goto bad;
    int32_t lat=(int32_t)wx_lp_le32(r+16),lon=(int32_t)wx_lp_le32(r+20);
    uint16_t gx=wx_lp_le16(r+24),gy=wx_lp_le16(r+26);
    if(lat<-900000||lat>900000||lon<-1800000||lon>1800000||gx>9999u||gy>9999u)goto bad;
    for(int i=0;i<3;i++){if(r[28+i]<'A'||r[28+i]>'Z')goto bad;out->office[i]=(char)r[28+i];}
    if(r[31])goto bad;
    for(int i=0;i<2;i++){if(r[32+i]<'A'||r[32+i]>'Z')goto bad;out->state[i]=(char)r[32+i];}
    if(home>=n_cities)goto bad;
    const uint8_t *p=r+WX_LPACK_HEADER;
    const uint8_t *spots=p;p+=4u*n_cities;
    for(unsigned i=0;i<n_cities*2u;i++) {
        unsigned x=spots[i*2],y=spots[i*2+1];
        if(!(x==255u&&y==255u)&&!(x<160u&&y>=WX_LPACK_MAP_TOP&&y<=WX_LPACK_MAP_BOTTOM-24))goto bad;
    }
    for(unsigned i=0;i<obs_rows;i++){if(p[i]>=stations)goto bad;out->obs_slot[i]=p[i];}
    p+=obs_rows;
    const uint8_t *reqs=p;p+=5u*n_req;
    const uint8_t *smap=p;p+=sn;
    const uint8_t *cmap=p;p+=cn;
    const uint8_t *rmap=p;p+=rmn;
    const char *s=(const char *)p;
    if(!strn||p[strn-1u])goto bad;
    /* the strings: exactly 6 + cities + obs rows + requests, each ending in NUL */
    unsigned want=6u+n_cities+obs_rows+n_req,got=0;
    for(unsigned i=0;i<strn;i++)if(!p[i])got++;
    if(got!=want)goto bad;
    const char *strs[6+WX_CITIES+WX_OBS_ROWS+WX_REQUESTS];
    for(unsigned i=0;i<want;i++){strs[i]=s;s+=strlen(s)+1;}
    const char *name=strs[0],*area=strs[1],*title=strs[2];
    if(!wx_lp_text(name,1,23)||!wx_lp_text(area,1,29)||!wx_lp_text(title,1,23))goto bad;
    for(unsigned i=0;i<n_cities;i++){out->city_name[i]=strs[6+i];if(!wx_lp_text(out->city_name[i],1,15))goto bad;}
    for(unsigned i=0;i<obs_rows;i++){out->obs_label[i]=strs[6+n_cities+i];if(!wx_lp_text(out->obs_label[i],1,15))goto bad;}
    unsigned tx=r[36],ty=r[37],wxx=r[38],wy=r[39];
    if(tx>=160u||ty<WX_LPACK_MAP_TOP||ty>WX_LPACK_MAP_BOTTOM-9||wxx>=160u||wy<WX_LPACK_MAP_TOP||wy>WX_LPACK_MAP_BOTTOM-9)goto bad;
    unsigned sy0=r[40],srows=r[41],cy0=r[42],crows=r[43];
    if(sy0<WX_LPACK_MAP_TOP||!srows||sy0+srows>WX_LPACK_MAP_BOTTOM||!wx_lp_rle(smap,sn,srows))goto bad;
    if(cy0<WX_LPACK_MAP_TOP||!crows||cy0+crows>WX_LPACK_MAP_BOTTOM||!wx_lp_rle(cmap,cn,crows))goto bad;
    /* the requests */
    unsigned count[7]={0},obs0=0;
    for(unsigned i=0;i<n_req;i++) {
        const uint8_t *q=reqs+5u*i;
        unsigned kind=q[0],slot=q[1],host=q[2],period=wx_lp_le16(q+3);
        const char *path=strs[6+n_cities+obs_rows+i];
        if(kind>WX_K_RADAR||host>1u||(host==1u)!=(kind==WX_K_RADAR)||period<60u||!wx_lp_path_chars(path))goto bad;
        int slot_ok=kind==WX_K_OBS?slot<stations:kind==WX_K_RECENT?(slot>=1u&&slot<stations):kind==WX_K_FORECAST?slot==home:
                    kind==WX_K_CITY?(slot<n_cities&&slot!=home):slot==0u;
        const char *prefix=kind<=WX_K_HISTORY?"/stations/":kind<=WX_K_CITY?"/gridpoints/":kind==WX_K_ALERTS?"/alerts/active?point=":"/geoserver/";
        if(!slot_ok||!wx_lp_prefix(path,prefix))goto bad;
        if(kind==WX_K_RADAR) {
            size_t n=strlen(path),tail=strlen("width=160&height=97");
            if(!strstr(path,"request=GetMap")||!strstr(path,"format=image/gif")||n<tail||strcmp(path+n-tail,"width=160&height=97"))goto bad;
        }
        count[kind]++;obs0+=kind==WX_K_OBS&&slot==0u;
        out->req[i].host=host?WX_RADAR_HOST:WX_HOST;out->req[i].path=path;out->req[i].kind=(uint8_t)kind;out->req[i].slot=(uint8_t)slot;
        out->period[i]=(uint16_t)period;
    }
    if(count[WX_K_FORECAST]!=1u||count[WX_K_ALERTS]!=1u||count[WX_K_HISTORY]>1u||count[WX_K_RADAR]>1u||obs0!=1u)goto bad;
    int32_t rw=(int32_t)wx_lp_le32(r+44),re=(int32_t)wx_lp_le32(r+48),rs=(int32_t)wx_lp_le32(r+52),rn=(int32_t)wx_lp_le32(r+56);
    for(unsigned i=0;i<3;i++)out->radar_town[i]=strs[3+i];
    memcpy(out->radar_marks,r+60,6);
    if(count[WX_K_RADAR]) {
        if(!wx_lp_rle(rmap,rmn,WX_LPACK_RADAR_ROWS)||rw<-1800000||rw>=re||re>1800000||rs<-900000||rs>=rn||rn>900000)goto bad;
        for(unsigned i=0;i<3;i++) {
            if(!wx_lp_text(out->radar_town[i],1,15))goto bad;
            if(out->radar_marks[i*2]>=160u||out->radar_marks[i*2+1]<WX_LPACK_MAP_TOP||out->radar_marks[i*2+1]>=WX_LPACK_MAP_TOP+WX_LPACK_RADAR_ROWS)goto bad;
        }
    } else {
        if(rmn||rw||re||rs||rn)goto bad;
        for(unsigned i=0;i<6;i++)if(out->radar_marks[i])goto bad;
        for(unsigned i=0;i<3;i++)if(out->radar_town[i][0])goto bad;
    }
    /* good: the pack, pointing into the bytes */
    wx_pack *k=&out->pack;
    k->name=name;k->area=area;k->state=out->state;k->lat=lat/10000.0;k->lon=lon/10000.0;
    k->lat_e4=lat;k->lon_e4=lon;k->office=out->office;k->grid_x=gx;k->grid_y=gy;
    k->stations=(uint8_t)stations;k->obs_rows=(uint8_t)obs_rows;k->cities=(uint8_t)n_cities;k->home_city=(uint8_t)home;k->requests=(uint8_t)n_req;
    k->req=out->req;k->period=out->period;k->obs_label=out->obs_label;k->obs_slot=out->obs_slot;k->city_name=out->city_name;
    k->state_map=smap;k->state_map_n=(uint16_t)sn;k->state_y0=(uint8_t)sy0;k->state_rows=(uint8_t)srows;
    k->close_map=cmap;k->close_map_n=(uint16_t)cn;k->close_y0=(uint8_t)cy0;k->close_rows=(uint8_t)crows;
    k->close_title=title;k->close_title_x=(uint8_t)tx;k->close_title_y=(uint8_t)ty;k->words_x=(uint8_t)wxx;k->words_y=(uint8_t)wy;
    k->city_spots=spots;
    k->radar_map=rmn?rmap:NULL;k->radar_map_n=(uint16_t)rmn;k->radar_marks=out->radar_marks;k->radar_town=out->radar_town;
    k->radar_w=rw;k->radar_e=re;k->radar_s=rs;k->radar_n=rn;
    out->sequence=seq;out->crc=wx_lp_le32(r+total-4u);
    return 1;
bad:
    memset(out,0,sizeof(*out));
    return 0;
}
/* The length a copy says it has (from its first 16 bytes), 0 when it cannot be a pack: what to read in full. */
static inline uint32_t wx_lpack_length(const uint8_t *head) {
    if(memcmp(head,"PJLP",4)||head[4]!=WX_LPACK_VERSION)return 0;
    uint32_t total=wx_lp_le32(head+8);
    return total>=WX_LPACK_HEADER+4u&&total<=WX_LPACK_COPY&&wx_lp_le32(head+12)?total:0;
}
static inline uint32_t wx_lpack_sequence(const uint8_t *head){return wx_lp_le32(head+12);}
/* Which copy to try first, from their first 16 bytes: B when it may be a pack and is newer than A (or A cannot be one),
 * else A. The other copy is tried when the first is not good, so the newer good copy is used (A when they are equal). */
static inline int wx_lpack_b_first(const uint8_t *head_a,const uint8_t *head_b) {
    uint32_t la=wx_lpack_length(head_a),lb=wx_lpack_length(head_b);
    return lb&&(!la||(int32_t)(wx_lpack_sequence(head_b)-wx_lpack_sequence(head_a))>0);
}
#endif
