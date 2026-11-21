/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: what is kept of the National Weather Service replies, the streaming
 * parse of one reply (wx_fetch) and its commit into the channel's state (wx_state).
 *
 * Units and wording follow the approved PC reference (the project's PC reference (not published)):
 * whole degrees Fahrenheit and miles an hour rounded half away from zero, pressure in inches of
 * mercury to two places, visibility in tenths of a mile, the ceiling as the lowest broken or
 * overcast layer to the nearest 100 feet, 16 compass points, calm = no direction.
 * All unit work is integer (thousandths in 64 bits); only the wind-chill and heat-index
 * formulas, used when the station sends neither, are floating point (run by the fetch task).
 *
 * A reply is parsed as it arrives and never held: wx_fetch keeps the scanner (wx_json.h), the
 * record being read and the staged result of ONE reply. Only wx_fetch_commit touches wx_state.
 * Portable C99, no allocation, no I/O. Part of wx.h; include that.
 */
#ifndef WX_DATA_H
#define WX_DATA_H
#ifndef WX_H
#error "include wx.h"
#endif

#define WX_NA (-32768)        /* "not available" in every int16 figure */
#define WX_SKY_LEN 40             /* sky wording kept, with terminator */
#define WX_TEXT 256           /* forecast text kept per period, with terminator */
#define WX_PNAME 24
#define WX_LOCAL 3            /* forecast periods shown as text */
#define WX_EXT 6              /* days of the extended forecast */
#define WX_ALERT 200          /* alert crawl text kept, with terminator */
#define WX_OBS_STALE_S 10800  /* an observation older than 3 h is not shown */
#define WX_OBS_MERGE_S 7200   /* gaps are filled from reports up to 2 h older */
#define WX_FC_STALE_S 43200   /* a forecast not refreshed for 12 h is not shown */

typedef struct {
    uint32_t time_s;          /* when the station reported (UTC seconds); 0 = no report held */
    int16_t temp_f,dew_f,feels_f,wind_mph,gust_mph,humidity;
    int16_t pressure;         /* inches of mercury x 100 */
    int16_t vis10;            /* miles x 10 */
    int16_t ceiling;          /* hundreds of feet; WX_NA = none (unlimited) */
    int8_t wind_dir;          /* 0..15 from north clockwise; -1 none */
    uint8_t feels_kind;       /* 0 none, 1 wind chill, 2 heat index */
    char sky[WX_SKY_LEN];         /* capitals; "" = none */
} wx_obs;
typedef struct {char name[WX_PNAME];char text[WX_TEXT];uint8_t is_day;int8_t pop;} wx_period;
typedef struct {char sky[2][9];int16_t hi,lo;uint8_t dow,icon;int8_t pop;uint8_t used;} wx_ext;
/* label: 0 none, 1 TODAY, 2 TONIGHT, 3..9 SUNDAY..SATURDAY. kind: 0 none, 1 HIGH, 2 LOW. */
typedef struct {int16_t temp;uint8_t icon,kind,label;int8_t pop;} wx_city;
typedef struct {wx_period local[WX_LOCAL];wx_ext ext[WX_EXT];uint8_t n_local,n_ext;} wx_forecast;
enum {WX_TREND_NONE,WX_TREND_RISING,WX_TREND_FALLING,WX_TREND_STEADY};

/* ---- small text helpers ---- */
static inline char wx_upper(char c){return c>='a'&&c<='z'?(char)(c-32):c;}
static inline void wx_copy_upper(char *dst,size_t cap,const char *src) {
    size_t n=0;
    for(;src[n]&&n+1<cap;n++)dst[n]=wx_upper(src[n]);
    dst[n]=0;
}
/* `word` (capitals) occurs in `text` (any case). */
static inline int wx_has(const char *text,const char *word) {
    for(;*text;text++) {
        size_t k=0;
        while(word[k]&&wx_upper(text[k])==word[k])k++;
        if(!word[k])return 1;
    }
    return 0;
}
static inline int wx_has_any(const char *text,const char *const *words,int n){for(int i=0;i<n;i++)if(wx_has(text,words[i]))return 1;return 0;}
static inline size_t wx_append(char *dst,size_t cap,const char *src) {
    size_t n=strlen(dst);
    while(*src&&n+1<cap)dst[n++]=*src++;
    dst[n]=0;
    return n;
}
static inline void wx_append_int(char *dst,size_t cap,int v) {
    char tmp[12];int n=0;unsigned u=v<0?(unsigned)-v:(unsigned)v;
    do{tmp[n++]=(char)('0'+u%10u);u/=10u;}while(u&&n<11);
    if(v<0)tmp[n++]='-';
    size_t at=strlen(dst);
    while(n&&at+1<cap)dst[at++]=tmp[--n];
    dst[at]=0;
}

/* ---- wording (the reference's classify_icon, short_sky and sky_words) ---- */
static inline int wx_classify_icon(const char *t,int is_day) {
    static const char *const snow[]={"SNOW","FLURR","BLIZZARD","SLEET","ICE PELLETS","WINTRY","FREEZING"};
    static const char *const rain[]={"RAIN","SHOWER","DRIZZLE","SPRINKLE"};
    static const char *const fog[]={"FOG","MIST","HAZE","SMOKE"};
    static const char *const part[]={"PARTLY","MOSTLY SUNNY","MOSTLY CLEAR","FEW CLOUDS"};
    static const char *const clear[]={"SUNNY","CLEAR","FAIR","WIND","BREEZ"};
    if(!t[0])return WX_ICON_NONE;
    if(wx_has(t,"THUNDER")||wx_has(t,"T-STORM")||wx_has(t,"TSTORM"))return WX_ICON_STORM;
    if(wx_has_any(t,snow,7)&&!wx_has(t,"FROST"))return WX_ICON_SNOW;
    if(wx_has_any(t,rain,4))return WX_ICON_RAIN;
    if(wx_has_any(t,fog,4))return WX_ICON_FOG;
    if(wx_has_any(t,part,4))return is_day?WX_ICON_PC_DAY:WX_ICON_PC_NIGHT;
    if(wx_has(t,"CLOUD")||wx_has(t,"OVERCAST"))return WX_ICON_CLOUD;
    if(wx_has_any(t,clear,5))return is_day?WX_ICON_SUN:WX_ICON_MOON;
    return WX_ICON_CLOUD;
}
/* Observation wording in at most 7 characters, for the table. */
static inline void wx_short_sky(const char *t,char out[9]) {
    const char *w=NULL;
    if(!t[0])w="--";
    else if(wx_has(t,"THUNDER"))w="TSTORM";
    else if(wx_has(t,"FREEZING"))w="FRZ RN";
    else if(wx_has(t,"SNOW")||wx_has(t,"FLURR"))w=wx_has(t,"LIGHT")?"LT SNOW":"SNOW";
    else if(wx_has(t,"SLEET")||wx_has(t,"ICE"))w="SLEET";
    else if(wx_has(t,"DRIZZLE"))w="DRIZZLE";
    else if(wx_has(t,"RAIN")||wx_has(t,"SHOWER"))w=wx_has(t,"HEAVY")?"HVY RN":wx_has(t,"LIGHT")?"LT RAIN":"RAIN";
    else if(wx_has(t,"FOG")||wx_has(t,"MIST"))w="FOG";
    else if(wx_has(t,"HAZE")||wx_has(t,"SMOKE"))w="HAZE";
    else if(wx_has(t,"OVERCAST")||!strcmp(t,"CLOUDY"))w="CLOUDY";
    else if(wx_has(t,"MOSTLY CLOUDY"))w="M CLDY";
    else if(wx_has(t,"PARTLY"))w="P CLDY";
    else if(wx_has(t,"MOSTLY CLEAR")||wx_has(t,"FEW")||wx_has(t,"MOSTLY SUNNY"))w="FAIR";
    else if(wx_has(t,"CLEAR"))w="CLEAR";
    else if(wx_has(t,"SUNNY"))w="SUNNY";
    else if(wx_has(t,"FAIR"))w="FAIR";
    else if(wx_has(t,"CLOUD"))w="CLOUDY";
    if(w){out[0]=0;wx_append(out,9,w);}
    else wx_copy_upper(out,8,t);
}
/* Forecast wording as one or two short words for the Extended page. */
static inline void wx_sky_words(const char *text,char out[2][9]) {
    char t[64];
    const char *pre=NULL,*word=NULL;
    static const char *const pairs[5][3]={{"MOSTLY CLOUDY","MOSTLY","CLOUDY"},{"PARTLY CLOUDY","PARTLY","CLOUDY"},
        {"PARTLY SUNNY","PARTLY","SUNNY"},{"MOSTLY SUNNY","MOSTLY","SUNNY"},{"MOSTLY CLEAR","MOSTLY","CLEAR"}};
    static const char *const single[6]={"CLOUDY","OVERCAST","SUNNY","CLEAR","WINDY","BREEZY"};
    wx_copy_upper(t,sizeof(t),text);
    char *then=strstr(t," THEN ");
    if(then)*then=0;
    out[0][0]=out[1][0]=0;
    if(!t[0]){wx_append(out[0],9,"--");return;}
    if(wx_has(t,"SLIGHT CHANCE"))pre="SLGT CHC";
    else if(wx_has(t,"CHANCE"))pre="CHANCE";
    else if(wx_has(t,"LIKELY"))pre="LIKELY";
    else if(wx_has(t,"SCATTERED")||wx_has(t,"ISOLATED"))pre="SCT'D";
    if(wx_has(t,"THUNDER"))word="T'STORMS";
    else if(wx_has(t,"SNOW")||wx_has(t,"FLURR"))word="SNOW";
    else if(wx_has(t,"SLEET")||wx_has(t,"WINTRY")||wx_has(t,"FREEZING"))word="ICE MIX";
    else if(wx_has(t,"SHOWER"))word="SHOWERS";
    else if(wx_has(t,"RAIN")||wx_has(t,"DRIZZLE"))word="RAIN";
    if(word) {
        if(pre){wx_append(out[0],9,pre);wx_append(out[1],9,word);}
        else wx_append(out[0],9,word);
        return;
    }
    if(wx_has(t,"FOG")){wx_append(out[0],9,"FOG");return;}
    for(int i=0;i<5;i++)if(wx_has(t,pairs[i][0])){wx_append(out[0],9,pairs[i][1]);wx_append(out[1],9,pairs[i][2]);return;}
    for(int i=0;i<6;i++)if(wx_has(t,single[i])){wx_append(out[0],9,single[i]);return;}
    t[8]=0;wx_append(out[0],9,t);
}

/* ---- numbers and units ---- */
/* A JSON number as thousandths. 0 when the text is not a number (null, true, ...). */
static inline int wx_milli(const char *s,int64_t *out) {
    int neg=0,digits=0,frac=0;
    int64_t v=0;
    if(*s=='-'){neg=1;s++;}
    while(*s>='0'&&*s<='9'){if(v<1000000000000LL)v=v*10+(*s-'0');s++;digits++;}
    if(!digits)return 0;
    v*=1000;
    if(*s=='.') {
        int64_t scale=100;
        s++;
        while(*s>='0'&&*s<='9'){if(frac<3){v+=(*s-'0')*scale;scale/=10;}frac++;s++;}
    }
    if(*s=='e'||*s=='E') { /* the service does not send exponents; taken roughly if it ever does */
        int e=0,eneg=0;
        s++;
        if(*s=='-'){eneg=1;s++;}else if(*s=='+')s++;
        while(*s>='0'&&*s<='9'){if(e<30)e=e*10+(*s-'0');s++;}
        while(e-->0){if(eneg)v/=10;else if(v<1000000000000LL)v*=10;}
    }
    *out=neg?-v:v;
    return 1;
}
static inline int wx_round_div(int64_t v,int64_t unit){return (int)(v>=0?(v+unit/2)/unit:-((-v+unit/2)/unit));}
static inline int wx_c_to_f(int64_t c_milli){return wx_round_div(c_milli*1800+32000000,1000000);}
static inline int wx_kmh_to_mph(int64_t kmh_milli){return wx_round_div(kmh_milli*621371,1000000000);}
static inline int16_t wx_clamp16(int v){return (int16_t)(v>32000?32000:v<-32000?-32000:v);}

/* ---- one reply being parsed ---- */
enum {WX_F_T=1,WX_F_TD=2,WX_F_WD=4,WX_F_WS=8,WX_F_WG=16,WX_F_P=32,WX_F_VIS=64,WX_F_RH=128,WX_F_WC=256,WX_F_HI=512,
      WX_F_CEIL=1024,WX_F_BASE=2048};
typedef struct {                 /* an observation record as sent (thousandths of the service's units) */
    int64_t t,td,wd,ws,wg,p,vis,rh,wc,hi,ceil,base;
    uint32_t time_s;
    uint16_t have;
    uint8_t layer_counts;         /* the cloud layer being read is broken, overcast or obscured */
    char sky[WX_SKY_LEN];
} wx_rec;
typedef struct {                 /* a forecast period as sent */
    char name[WX_PNAME],brief[64];
    uint32_t end_s;
    int32_t start_day;            /* the start date as written; 0 = none */
    int16_t temp;
    int8_t pop;
    uint8_t is_day,have_text;
} wx_prec;
typedef struct {char event[64];uint8_t actual,cancel,have_headline;} wx_arec;
typedef struct wx_fetch {
    wx_json js;
    uint8_t kind,slot,done,bad;
    uint8_t in_record;            /* inside a properties / period object */
    uint16_t record_depth;
    uint32_t now_s;               /* the clock when the request began; 0 = unknown */
    uint32_t bytes;
    wx_tz tz;                     /* the place's zone, for naming "TODAY" (US Eastern, both packs', unless wx_fetch_zone says otherwise) */
    union {
        struct {
            wx_rec rec;
            wx_obs out;
            int64_t p0;           /* newest pressure of the history, Pa x 1000 */
            uint32_t p0_s;
            uint8_t have_p0,trend,trend_done,merge_done;
        } obs;
        struct {
            wx_prec rec;
            wx_forecast fc;
            wx_city city;
            uint8_t accepted,await_night,city_set,city_picked;
            char text[WX_TEXT];   /* the period's detailed text until the period ends */
        } fc;
        struct {
            wx_arec rec;
            char headline[WX_ALERT];
            char text[WX_ALERT];
            uint8_t count;
        } alert;
    } u;
} wx_fetch;

/* A reply of this kind for this slot is about to be fed (1.6.3: a place's own request list,
 * wx_place.h, names kind and slot itself). */
static inline void wx_fetch_begin_as(wx_fetch *f,unsigned kind,unsigned slot) {
    memset(f,0,sizeof(*f));
    f->tz=wx_tz_eastern();
    if(kind>WX_K_RADAR||slot>255u){f->bad=1;f->kind=255;return;}
    f->kind=(uint8_t)kind;f->slot=(uint8_t)slot;
    if(f->kind==WX_K_FORECAST||f->kind==WX_K_CITY){f->u.fc.city.temp=WX_NA;f->u.fc.city.pop=-1;}
    if(f->kind==WX_K_RADAR)f->bad=1; /* a picture, not JSON: it goes through wx_gif_feed (wx_radar.h) */
}
static inline void wx_fetch_begin(wx_fetch *f,unsigned request_index) {
    if(request_index>=WX_DEFAULT_PACK->requests){wx_fetch_begin_as(f,255u,0);return;}  /* the default pack's table */
    wx_fetch_begin_as(f,WX_DEFAULT_PACK->req[request_index].kind,WX_DEFAULT_PACK->req[request_index].slot);
}
/* The place's time zone (call right after wx_fetch_begin*). */
static inline void wx_fetch_zone(wx_fetch *f,wx_tz z){f->tz=z;}
/* The clock, for skipping forecast periods that are over and naming "TODAY". Optional: without it
 * nothing is skipped. Call right after wx_fetch_begin. */
static inline void wx_fetch_clock(wx_fetch *f,int64_t epoch_s){f->now_s=wx_clock_ok(epoch_s)?(uint32_t)epoch_s:0;}
/* The parser has everything it wants: the rest of the reply need not be read. */
static inline int wx_fetch_done(const wx_fetch *f){return f->done&&!f->bad;}

static inline void wx_obs_clear(wx_obs *o) {
    memset(o,0,sizeof(*o));
    o->temp_f=o->dew_f=o->feels_f=o->wind_mph=o->gust_mph=o->humidity=o->pressure=o->vis10=o->ceiling=WX_NA;
    o->wind_dir=-1;
}
/* Gaps of `dst` filled from `src`. */
static inline void wx_obs_fill(wx_obs *dst,const wx_obs *src) {
    if(dst->temp_f==WX_NA)dst->temp_f=src->temp_f;
    if(dst->dew_f==WX_NA)dst->dew_f=src->dew_f;
    if(dst->feels_f==WX_NA){dst->feels_f=src->feels_f;dst->feels_kind=src->feels_kind;}
    if(dst->wind_mph==WX_NA){dst->wind_mph=src->wind_mph;dst->wind_dir=src->wind_dir;}
    if(dst->gust_mph==WX_NA)dst->gust_mph=src->gust_mph;
    if(dst->humidity==WX_NA)dst->humidity=src->humidity;
    if(dst->pressure==WX_NA)dst->pressure=src->pressure;
    if(dst->vis10==WX_NA)dst->vis10=src->vis10;
    if(dst->ceiling==WX_NA)dst->ceiling=src->ceiling;
    if(!dst->sky[0])memcpy(dst->sky,src->sky,WX_SKY_LEN);
    if(dst->wind_mph==WX_NA||dst->wind_mph==0)dst->wind_dir=-1; /* calm: no direction */
}
/* The record as display figures. */
static inline void wx_rec_to_obs(const wx_rec *r,wx_obs *o) {
    wx_obs_clear(o);
    o->time_s=r->time_s;
    if(r->have&WX_F_T)o->temp_f=wx_clamp16(wx_c_to_f(r->t));
    if(r->have&WX_F_TD)o->dew_f=wx_clamp16(wx_c_to_f(r->td));
    if(r->have&WX_F_WS)o->wind_mph=wx_clamp16(wx_kmh_to_mph(r->ws));
    if(r->have&WX_F_WG)o->gust_mph=wx_clamp16(wx_kmh_to_mph(r->wg));
    if(r->have&WX_F_RH)o->humidity=wx_clamp16(wx_round_div(r->rh,1000));
    if((r->have&WX_F_P)&&r->p>0)o->pressure=wx_clamp16((int)((r->p*100+1693194)/3386389));
    if((r->have&WX_F_VIS)&&r->vis>=0)o->vis10=wx_clamp16((int)((r->vis*10+804672)/1609344));
    if((r->have&WX_F_CEIL)&&r->ceil>=0)o->ceiling=wx_clamp16((int)((r->ceil*328084+5000000000LL)/10000000000LL));
    if((r->have&WX_F_WD)&&o->wind_mph!=WX_NA&&o->wind_mph!=0) {
        int64_t d=r->wd%360000;
        if(d<0)d+=360000;
        o->wind_dir=(int8_t)(((d*2+22500)/45000)%16);
    }
    if(r->have&WX_F_WC){o->feels_f=wx_clamp16(wx_c_to_f(r->wc));o->feels_kind=1;}
    else if(r->have&WX_F_HI){o->feels_f=wx_clamp16(wx_c_to_f(r->hi));o->feels_kind=2;}
    else if(r->have&WX_F_T) { /* the station sent neither: the usual formulas */
        double tf=(double)r->t/1000.0*9.0/5.0+32.0;
        if(tf<=50.0&&(r->have&WX_F_WS)&&(double)r->ws*0.000621371>3.0) {
            double w=pow((double)r->ws*0.000621371,0.16),v=35.74+0.6215*tf-35.75*w+0.4275*tf*w;
            o->feels_f=wx_clamp16((int)(v>=0?floor(v+0.5):-floor(-v+0.5)));o->feels_kind=1;
        } else if(tf>=80.0&&(r->have&WX_F_RH)) {
            double h=(double)r->rh/1000.0,t=tf;
            double v=-42.379+2.04901523*t+10.14333127*h-0.22475541*t*h-6.83783e-3*t*t-5.481717e-2*h*h
                +1.22874e-3*t*t*h+8.5282e-4*t*h*h-1.99e-6*t*t*h*h;
            if(v>t){o->feels_f=wx_clamp16((int)floor(v+0.5));o->feels_kind=2;}
        }
    }
    memcpy(o->sky,r->sky,WX_SKY_LEN);
}

static inline void wx_fetch_obs_scalar(wx_fetch *f) {
    wx_json *j=&f->js;
    wx_rec *r=&f->u.obs.rec;
    unsigned k0=wx_json_key(j,0),k1=wx_json_key(j,1);
    int64_t v;
    if(j->depth==f->record_depth) { /* directly in the properties object */
        if(k0==WX_KEY_TIMESTAMP&&j->is_string) {
            int64_t t=wx_parse_time(j->val,NULL);
            r->time_s=t>0&&t<WX_EPOCH_MAX?(uint32_t)t:0;
        } else if(k0==WX_KEY_TEXT&&j->is_string)wx_copy_upper(r->sky,WX_SKY_LEN,j->val);
        return;
    }
    if(j->is_string) {
        if(k0==WX_KEY_AMOUNT&&j->depth==f->record_depth+2u&&wx_json_key(j,2)==WX_KEY_CLOUDS) {
            r->layer_counts=(uint8_t)(!strcmp(j->val,"BKN")||!strcmp(j->val,"OVC")||!strcmp(j->val,"VV"));
            r->have|=WX_F_BASE<<1; /* this layer's amount has been read */
        }
        return;
    }
    if(k0!=WX_KEY_VALUE||!wx_milli(j->val,&v))return;
    if(j->depth==f->record_depth+1u) {
        switch(k1) {
        case WX_KEY_TEMPERATURE:r->t=v;r->have|=WX_F_T;break;
        case WX_KEY_DEWPOINT:r->td=v;r->have|=WX_F_TD;break;
        case WX_KEY_WINDDIR:r->wd=v;r->have|=WX_F_WD;break;
        case WX_KEY_WINDSPEED:r->ws=v;r->have|=WX_F_WS;break;
        case WX_KEY_WINDGUST:r->wg=v;r->have|=WX_F_WG;break;
        case WX_KEY_PRESSURE:r->p=v;r->have|=WX_F_P;break;
        case WX_KEY_VISIBILITY:r->vis=v;r->have|=WX_F_VIS;break;
        case WX_KEY_HUMIDITY:r->rh=v;r->have|=WX_F_RH;break;
        case WX_KEY_WINDCHILL:r->wc=v;r->have|=WX_F_WC;break;
        case WX_KEY_HEATINDEX:r->hi=v;r->have|=WX_F_HI;break;
        default:break;
        }
    } else if(j->depth==f->record_depth+3u&&k1==WX_KEY_BASE&&wx_json_key(j,3)==WX_KEY_CLOUDS) {
        r->base=v;r->have|=WX_F_BASE;
    }
}
/* A cloud layer object ended: the lowest broken/overcast base is the ceiling. */
static inline void wx_fetch_layer_end(wx_fetch *f) {
    wx_rec *r=&f->u.obs.rec;
    if((r->have&WX_F_BASE)&&(r->have&(WX_F_BASE<<1))&&r->layer_counts&&(!(r->have&WX_F_CEIL)||r->base<r->ceil)) {
        r->ceil=r->base;r->have|=WX_F_CEIL;
    }
    r->have&=(uint16_t)~(WX_F_BASE|(WX_F_BASE<<1));r->layer_counts=0;
}
static inline void wx_fetch_obs_end(wx_fetch *f) {
    wx_rec *r=&f->u.obs.rec;
    wx_obs o;
    if(r->time_s) {
        wx_rec_to_obs(r,&o);
        if(!f->u.obs.out.time_s)f->u.obs.out=o;                               /* the newest report */
        else if(!f->u.obs.merge_done) {
            if(f->u.obs.out.time_s>=r->time_s&&f->u.obs.out.time_s-r->time_s<=WX_OBS_MERGE_S)wx_obs_fill(&f->u.obs.out,&o);
            else f->u.obs.merge_done=1;
        }
        if(f->kind==WX_K_HISTORY&&(r->have&WX_F_P)&&!f->u.obs.trend_done) { /* pressure now against 2.5 to 4 h ago */
            if(!f->u.obs.have_p0){f->u.obs.have_p0=1;f->u.obs.p0=r->p;f->u.obs.p0_s=r->time_s;}
            else if(f->u.obs.p0_s>=r->time_s) {
                uint32_t age=f->u.obs.p0_s-r->time_s;
                if(age>=9000u&&age<=14400u) {
                    int64_t diff=f->u.obs.p0-r->p;
                    f->u.obs.trend=(uint8_t)(diff>50000?WX_TREND_RISING:diff<-50000?WX_TREND_FALLING:WX_TREND_STEADY);
                    f->u.obs.trend_done=1;
                } else if(age>14400u)f->u.obs.trend_done=1;
            }
        }
    }
    memset(r,0,sizeof(*r));
    if(f->kind==WX_K_OBS)f->done=1;
    else if(f->kind==WX_K_RECENT&&f->u.obs.merge_done)f->done=1;
    else if(f->kind==WX_K_HISTORY&&f->u.obs.merge_done&&f->u.obs.trend_done)f->done=1;
}

static inline void wx_fetch_period_scalar(wx_fetch *f) {
    wx_json *j=&f->js;
    wx_prec *r=&f->u.fc.rec;
    unsigned k0=wx_json_key(j,0);
    int64_t v;
    if(j->depth==f->record_depth) {
        switch(k0) {
        case WX_KEY_NAME:if(j->is_string)wx_copy_upper(r->name,WX_PNAME,j->val);break;
        case WX_KEY_START:if(j->is_string&&!wx_parse_time(j->val,&r->start_day))r->start_day=0;break;
        case WX_KEY_END:if(j->is_string){int64_t t=wx_parse_time(j->val,NULL);r->end_s=t>0&&t<WX_EPOCH_MAX?(uint32_t)t:0;}break;
        case WX_KEY_ISDAY:r->is_day=(uint8_t)(!j->is_string&&!strcmp(j->val,"true"));break;
        case WX_KEY_TEMPERATURE:if(!j->is_string&&wx_milli(j->val,&v))r->temp=wx_clamp16(wx_round_div(v,1000));break;
        case WX_KEY_SHORT:if(j->is_string)wx_copy_upper(r->brief,sizeof(r->brief),j->val);break;
        case WX_KEY_DETAILED:
            if(j->is_string&&f->kind==WX_K_FORECAST) {
                size_t n=j->vlen;
                wx_copy_upper(f->u.fc.text,WX_TEXT,j->val);
                if(n>=WX_JS_VAL-1u) { /* cut by the scanner: end at the last whole sentence */
                    char *dot=strrchr(f->u.fc.text,'.');
                    if(dot&&dot-f->u.fc.text>=80)dot[1]=0;
                }
                r->have_text=(uint8_t)(f->u.fc.text[0]!=0);
            }
            break;
        default:break;
        }
    } else if(j->depth==f->record_depth+1u&&k0==WX_KEY_VALUE&&wx_json_key(j,1)==WX_KEY_POP&&!j->is_string&&wx_milli(j->val,&v)) {
        int p=wx_round_div(v,1000);
        r->pop=(int8_t)(p<0?0:p>100?100:p);
    }
}
static inline void wx_fetch_period_begin(wx_fetch *f) {
    memset(&f->u.fc.rec,0,sizeof(f->u.fc.rec));
    f->u.fc.rec.temp=WX_NA;f->u.fc.rec.pop=-1;f->u.fc.text[0]=0;
}
static inline void wx_fetch_period_end(wx_fetch *f) {
    wx_prec *r=&f->u.fc.rec;
    wx_forecast *fc=&f->u.fc.fc;
    /* a reply can still start with a period that is over: skipped (30 minutes of grace) */
    if(f->now_s&&r->end_s&&r->end_s<=f->now_s+1800u)return;
    unsigned index=f->u.fc.accepted;
    if(f->u.fc.accepted<255)f->u.fc.accepted++;
    if(index<3) { /* the regional figure: the first daytime period still 3 h from its end, else the first */
        int good=r->is_day&&(!f->now_s||!r->end_s||r->end_s>f->now_s+10800u);
        if(!f->u.fc.city_set||(good&&!f->u.fc.city_picked)) {
            wx_city *c=&f->u.fc.city;
            wx_time now;
            c->temp=r->temp;c->icon=(uint8_t)wx_classify_icon(r->brief,r->is_day);c->kind=(uint8_t)(r->is_day?1:2);c->pop=r->pop;
            c->label=0;
            if(r->start_day) {
                c->label=(uint8_t)(3+wx_dow(r->start_day));
                if(f->now_s){wx_local_time_z(f->now_s,f->tz,&now);if(now.days==r->start_day)c->label=(uint8_t)(r->is_day?1:2);}
            }
            f->u.fc.city_set=1;
            if(good)f->u.fc.city_picked=1;
        }
    }
    if(f->kind==WX_K_CITY) {
        if(f->u.fc.city_picked||index>=2)f->done=1;
        return;
    }
    if(index<WX_LOCAL) {
        wx_period *p=&fc->local[index];
        memcpy(p->name,r->name,WX_PNAME);
        if(r->have_text)memcpy(p->text,f->u.fc.text,WX_TEXT);
        else{p->text[0]=0;wx_append(p->text,WX_TEXT,r->brief);}
        p->is_day=r->is_day;p->pop=r->pop;
        fc->n_local=(uint8_t)(index+1);
        return;
    }
    if(r->is_day) { /* a day: a new column; the night after it gives the low */
        if(fc->n_ext>=WX_EXT){f->done=1;return;}
        wx_ext *e=&fc->ext[fc->n_ext++];
        memset(e,0,sizeof(*e));
        e->dow=(uint8_t)(r->start_day?wx_dow(r->start_day):7);
        e->icon=(uint8_t)wx_classify_icon(r->brief,1);
        wx_sky_words(r->brief,e->sky);
        e->hi=r->temp;e->lo=WX_NA;e->pop=r->pop;e->used=1;
        f->u.fc.await_night=1;
    } else if(f->u.fc.await_night) {
        fc->ext[fc->n_ext-1].lo=r->temp;
        f->u.fc.await_night=0;
        if(fc->n_ext>=WX_EXT)f->done=1;
    }
}

static inline void wx_fetch_alert_scalar(wx_fetch *f) {
    wx_json *j=&f->js;
    wx_arec *r=&f->u.alert.rec;
    if(j->depth!=f->record_depth||!j->is_string)return;
    switch(wx_json_key(j,0)) {
    case WX_KEY_EVENT:wx_copy_upper(r->event,sizeof(r->event),j->val);break;
    case WX_KEY_HEADLINE:wx_copy_upper(f->u.alert.headline,WX_ALERT,j->val);r->have_headline=(uint8_t)(j->val[0]!=0);break;
    case WX_KEY_STATUS:r->actual=(uint8_t)!strcmp(j->val,"Actual");break;
    case WX_KEY_MSGTYPE:r->cancel=(uint8_t)!strcmp(j->val,"Cancel");break;
    default:break;
    }
}
static inline void wx_fetch_alert_end(wx_fetch *f) {
    wx_arec *r=&f->u.alert.rec;
    const char *say=r->have_headline?f->u.alert.headline:r->event;
    if(r->actual&&!r->cancel&&say[0]) {
        wx_append(f->u.alert.text,WX_ALERT-9u,say); /* room is kept for the separator */
        wx_append(f->u.alert.text,WX_ALERT,"   ***   ");
        if(f->u.alert.count<255)f->u.alert.count++;
    }
    memset(r,0,sizeof(*r));f->u.alert.headline[0]=0;
}

/* The next piece of the reply, in any split. Returns 0 when no more is wanted (everything needed
 * has been read, or the request index was bad): the caller may stop reading. */
static inline int wx_fetch_feed(wx_fetch *f,const uint8_t *data,size_t size) {
    wx_json *j=&f->js;
    if(f->bad||f->done)return 0;
    for(size_t i=0;i<size;i++) {
        int again=0,ev=wx_json_byte(j,data[i],&again);
        f->bytes++;
        if(ev==WX_EV_NONE)continue;
        int obs=f->kind==WX_K_OBS||f->kind==WX_K_RECENT||f->kind==WX_K_HISTORY;
        int fc=f->kind==WX_K_FORECAST||f->kind==WX_K_CITY;
        if(ev==WX_EV_OPEN) {
            if(!f->in_record&&j->depth>=2&&j->depth<WX_JS_DEPTH&&(j->objects>>j->depth&1u)) {
                unsigned parent=j->key[j->depth-1];
                /* an observation or alert: the "properties" object of the reply or of a feature;
                 * a forecast period: an element of "periods" */
                int start=fc?(parent==WX_KEY_ELEM&&j->depth>=3&&j->key[j->depth-2]==WX_KEY_PERIODS)
                            :(parent==WX_KEY_PROPERTIES&&(j->depth==2||(j->depth==4&&j->key[1]==WX_KEY_FEATURES)));
                if(start) {
                    f->in_record=1;f->record_depth=j->depth;
                    if(fc)wx_fetch_period_begin(f);
                    else if(obs)memset(&f->u.obs.rec,0,sizeof(f->u.obs.rec));
                    else{memset(&f->u.alert.rec,0,sizeof(f->u.alert.rec));f->u.alert.headline[0]=0;}
                }
            }
        } else if(ev==WX_EV_SCALAR) {
            if(f->in_record&&j->depth>=f->record_depth&&j->depth<WX_JS_DEPTH) {
                if(obs)wx_fetch_obs_scalar(f);
                else if(fc)wx_fetch_period_scalar(f);
                else if(f->kind==WX_K_ALERTS)wx_fetch_alert_scalar(f);
            }
        } else { /* WX_EV_CLOSE */
            if(f->in_record) {
                if(j->depth==f->record_depth) {
                    f->in_record=0;
                    if(obs)wx_fetch_obs_end(f);
                    else if(fc)wx_fetch_period_end(f);
                    else if(f->kind==WX_K_ALERTS)wx_fetch_alert_end(f);
                } else if(obs&&j->depth==f->record_depth+2u&&j->key[f->record_depth]==WX_KEY_CLOUDS)wx_fetch_layer_end(f);
            }
            wx_json_closed(j);
        }
        if(f->done)return 0;
        if(again)i--,f->bytes--;
    }
    return 1;
}
#endif

