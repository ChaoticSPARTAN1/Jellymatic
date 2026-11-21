/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: the calendar, US Eastern time, and the sun and moon figures of the
 * almanac page, all worked out on the console from the clock (nothing fetched).
 *
 * Time zone: US Eastern with the daylight rule compiled in (second Sunday of March 2:00 to first
 * Sunday of November 2:00), the same rule as the PC reference (weather_data.py). 1.6.3: a weather
 * location elsewhere in the US (wx_place.h) has its own standard offset and the same US rule, or none.
 *
 * Sunrise/sunset: the short "sunrise equation" of the reference, in double precision (a few dozen
 * operations a day). Moon: the reference's low-precision series (phase times good to about an
 * hour; only the date is shown). The reference steps 20 minutes at a time until four quarter
 * crossings are found (about 2,000 evaluations); here the same crossings are found by stepping a
 * day at a time and halving, at most WX_MOON_EVALS evaluations a call, so no call is long: the
 * ESP32 has no double-precision hardware and one evaluation is about ten soft-float sines.
 * Checked against the reference by check_weather.c.
 * Portable C99, no allocation. Part of wx.h; include that.
 */
#ifndef WX_ASTRO_H
#define WX_ASTRO_H
#include <math.h>
#include <stdint.h>

#define WX_EPOCH_MIN 1767225600LL /* 2026-01-01 00:00 UTC: an earlier clock is "not set" */
#define WX_EPOCH_MAX 4102444800LL /* 2100-01-01: a later one is not believed either */
#define WX_MOON_EVALS 4           /* moon evaluations per wx_almanac_step call */
#define WX_PI 3.14159265358979323846

static inline int wx_clock_ok(int64_t epoch_s){return epoch_s>=WX_EPOCH_MIN&&epoch_s<WX_EPOCH_MAX;}

/* days since 1970-01-01 of a civil date (proleptic Gregorian) and back */
static inline int32_t wx_days_from_civil(int y,int m,int d) {
    y-=m<=2;
    int era=(y>=0?y:y-399)/400;
    unsigned yoe=(unsigned)(y-era*400),doy=(153u*(unsigned)(m+(m>2?-3:9))+2u)/5u+(unsigned)d-1u;
    unsigned doe=yoe*365u+yoe/4u-yoe/100u+doy;
    return era*146097+(int32_t)doe-719468;
}
static inline void wx_civil_from_days(int32_t z,int *y,int *m,int *d) {
    z+=719468;
    int era=(z>=0?z:z-146096)/146097;
    unsigned doe=(unsigned)(z-era*146097),yoe=(doe-doe/1460u+doe/36524u-doe/146096u)/365u;
    unsigned doy=doe-(365u*yoe+yoe/4u-yoe/100u),mp=(5u*doy+2u)/153u;
    *d=(int)(doy-(153u*mp+2u)/5u+1u);*m=(int)(mp<10?mp+3:mp-9);*y=(int)yoe+era*400+(*m<=2);
}
static inline int wx_dow(int32_t days){int w=(int)((days+4)%7);return w<0?w+7:w;} /* 0 = Sunday */
static inline int32_t wx_nth_sunday(int year,int month,int n) {
    int32_t first=wx_days_from_civil(year,month,1);
    return first+(7-wx_dow(first))%7+7*(n-1);
}
/* A US time zone (1.6.3, a weather location other than the default pack): the standard offset from UTC in
 * minutes and whether the US daylight rule applies (second Sunday of March 2:00 local standard time
 * to first Sunday of November 2:00 local daylight time, one hour ahead). US Eastern (Orlando) is {-300,1}. */
typedef struct {int16_t std_min;uint8_t dst;} wx_tz;
static inline wx_tz wx_tz_eastern(void){wx_tz z={-300,1};return z;}
/* Offset from UTC in minutes of `utc_s` in zone `z`. */
static inline int wx_tz_offset_min(int64_t utc_s,wx_tz z) {
    if(!z.dst)return z.std_min;
    int y,m,d;
    int32_t days=(int32_t)(utc_s>=0?utc_s/86400:-((-utc_s+86399)/86400));
    wx_civil_from_days(days,&y,&m,&d);
    int64_t start=(int64_t)wx_nth_sunday(y,3,2)*86400+(int64_t)(120-z.std_min)*60;  /* 2:00 standard time */
    int64_t end=(int64_t)wx_nth_sunday(y,11,1)*86400+(int64_t)(60-z.std_min)*60;    /* 2:00 daylight time */
    return utc_s>=start&&utc_s<end?z.std_min+60:z.std_min;
}
/* US Eastern offset from UTC in hours: -4 in daylight time, else -5. */
static inline int wx_eastern_offset(int64_t utc_s){return wx_tz_offset_min(utc_s,wx_tz_eastern())/60;}
typedef struct {int32_t days;int year,month,day,hour,minute,second,dow;} wx_time;
/* Local calendar fields of a UTC time in zone `z`. */
static inline void wx_local_time_z(int64_t utc_s,wx_tz z,wx_time *t) {
    int64_t local=utc_s+(int64_t)wx_tz_offset_min(utc_s,z)*60;
    int64_t days=local>=0?local/86400:-((-local+86399)/86400);
    int32_t rest=(int32_t)(local-days*86400);
    t->days=(int32_t)days;wx_civil_from_days(t->days,&t->year,&t->month,&t->day);
    t->hour=rest/3600;t->minute=rest/60%60;t->second=rest%60;t->dow=wx_dow(t->days);
}
/* Local (US Eastern) calendar fields of a UTC time. */
static inline void wx_local_time(int64_t utc_s,wx_time *t){wx_local_time_z(utc_s,wx_tz_eastern(),t);}
/* "2026-11-20T06:00:00-05:00" or "...+00:00" or "...Z" -> UTC seconds; also the date as written
 * (its own zone) in *days when wanted. 0 when the text is not a time. */
static inline int64_t wx_parse_time(const char *s,int32_t *days_written) {
    int v[6];
    static const uint8_t at[6]={0,5,8,11,14,17},len[6]={4,2,2,2,2,2};
    size_t n=0;
    while(n<26&&s[n])n++;
    if(n<19||s[4]!='-'||s[7]!='-'||s[10]!='T'||s[13]!=':'||s[16]!=':')return 0;
    for(int i=0;i<6;i++) {
        v[i]=0;
        for(int k=0;k<len[i];k++) {
            char c=s[at[i]+k];
            if(c<'0'||c>'9')return 0;
            v[i]=v[i]*10+(c-'0');
        }
    }
    if(v[0]<1970||v[0]>2199||v[1]<1||v[1]>12||v[2]<1||v[2]>31||v[3]>23||v[4]>59||v[5]>60)return 0;
    int32_t days=wx_days_from_civil(v[0],v[1],v[2]);
    int64_t t=(int64_t)days*86400+v[3]*3600+v[4]*60+v[5];
    size_t p=19;
    if(p<n&&s[p]=='.'){p++;while(p<n&&s[p]>='0'&&s[p]<='9')p++;}
    if(p+5<n+0u&&(s[p]=='+'||s[p]=='-')&&s[p+3]==':') {
        int oh=(s[p+1]-'0')*10+(s[p+2]-'0'),om=(s[p+4]-'0')*10+(s[p+5]-'0');
        if(oh<0||oh>14||om<0||om>59)return 0;
        t+=(s[p]=='-'?1:-1)*(oh*3600+om*60);
    }
    if(days_written)*days_written=days;
    return t;
}

/* An HTTP Date header, "Thu, 19 Nov 2026 23:46:36 GMT" -> UTC seconds; 0 when it is not one. */
static inline int64_t wx_parse_http_date(const char *s) {
    static const char months[]="JanFebMarAprMayJunJulAugSepOctNovDec";
    int v[5]={0,0,0,0,0},month=0;
    size_t n=0;
    while(n<40&&s[n])n++;
    if(n<29||s[3]!=','||s[4]!=' '||s[7]!=' '||s[11]!=' '||s[16]!=' '||s[19]!=':'||s[22]!=':'||s[25]!=' '||s[26]!='G'||s[27]!='M'||s[28]!='T')return 0;
    static const uint8_t at[5]={5,12,17,20,23},len[5]={2,4,2,2,2};
    for(int i=0;i<5;i++)for(int k=0;k<len[i];k++) {
        char c=s[at[i]+k];
        if(c<'0'||c>'9')return 0;
        v[i]=v[i]*10+(c-'0');
    }
    for(int m=0;m<12;m++)if(s[8]==months[m*3]&&s[9]==months[m*3+1]&&s[10]==months[m*3+2])month=m+1;
    if(!month||v[0]<1||v[0]>31||v[1]<1970||v[1]>2199||v[2]>23||v[3]>59||v[4]>60)return 0;
    return (int64_t)wx_days_from_civil(v[1],month,v[0])*86400+v[2]*3600+v[3]*60+v[4];
}

/* ---- sun ---- */
/* Sunrise and sunset (UTC seconds) of the civil date `days` at lat/lon; 0 when the sun does not
 * rise or set that day. The reference's sun_times(), same constants. */
static inline int wx_sun_times(int32_t days,double lat,double lon,int64_t *rise,int64_t *set) {
    const double rad=WX_PI/180.0;
    /* days since 2000-01-01 12:00 UTC of this date's noon */
    double n=(double)(days-10957)+0.0008;
    double js=n-lon/360.0;
    double md=fmod(357.5291+0.98560028*js,360.0);
    if(md<0)md+=360.0;
    double m=md*rad;
    double c=1.9148*sin(m)+0.02*sin(2*m)+0.0003*sin(3*m);
    double ld=fmod(md+c+180.0+102.9372,360.0);
    if(ld<0)ld+=360.0;
    double lam=ld*rad;
    double transit=js+0.0053*sin(m)-0.0069*sin(2*lam); /* days since 2000-01-01 12:00 */
    double sin_d=sin(lam)*sin(23.4397*rad),cos_d=cos(asin(sin_d)),phi=lat*rad;
    double cos_w=(sin(-0.833*rad)-sin(phi)*sin_d)/(cos(phi)*cos_d);
    if(!(cos_w>=-1.0&&cos_w<=1.0)){*rise=*set=0;return 0;}
    double w=acos(cos_w)/rad/360.0;
    /* 2000-01-01 12:00 UTC is 946728000 */
    *rise=(int64_t)floor(946728000.0+(transit-w)*86400.0);
    *set=(int64_t)floor(946728000.0+(transit+w)*86400.0);
    return 1;
}

/* ---- moon ---- */
/* Moon minus sun ecliptic longitude in degrees, 0..360 (0 new, 180 full). */
static inline double wx_moon_elongation(int64_t utc_s) {
    const double rad=WX_PI/180.0;
    double d=(double)(utc_s-946728000LL)/86400.0,t=d/36525.0;
    double g=(357.528+0.9856003*d)*rad;
    double sun=280.460+0.9856474*d+1.915*sin(g)+0.020*sin(2*g);
    double moon=218.32+481267.881*t+6.29*sin((135.0+477198.87*t)*rad)-1.27*sin((259.3-413335.36*t)*rad)
        +0.66*sin((235.7+890534.22*t)*rad)+0.21*sin((269.9+954397.74*t)*rad)-0.19*sin((357.5+35999.05*t)*rad)
        -0.11*sin((186.5+966404.03*t)*rad);
    double e=fmod(moon-sun,360.0);
    return e<0?e+360.0:e;
}
enum {WX_MOON_NEW,WX_MOON_WAXING_CRESCENT,WX_MOON_FIRST_QUARTER,WX_MOON_WAXING_GIBBOUS,WX_MOON_FULL,
      WX_MOON_WANING_GIBBOUS,WX_MOON_LAST_QUARTER,WX_MOON_WANING_CRESCENT};
static const char *const wx_moon_names[8]={"NEW","WAXING CRESCENT","FIRST QUARTER","WAXING GIBBOUS","FULL",
                                           "WANING GIBBOUS","LAST QUARTER","WANING CRESCENT"};

/* What the almanac page shows, with the working state of the moon search. */
typedef struct {
    int32_t day;             /* the local date the sun figures are for (days since 1970); 0 = none yet */
    int16_t rise[2],set[2];  /* local minutes of today and tomorrow; -1 = none */
    uint32_t rise_s,set_s;   /* today's, UTC seconds (day or night icons) */
    uint8_t dow[2];
    uint8_t moon_ready;      /* the moon figures below are complete */
    uint8_t moon_name,moon_pct;
    uint8_t next_phase[4];   /* 0 NEW, 1 FIRST, 2 FULL, 3 LAST */
    uint8_t next_month[4],next_day[4];
    /* search state */
    uint8_t found,mode;      /* phases found so far; 0 idle, 1 stepping a day at a time, 2 halving */
    uint8_t quarter;         /* quarter (0..3) at step `lo` */
    uint32_t base;           /* the search started at this UTC second */
    uint32_t lo,hi;          /* 20-minute steps from base: quarter(lo)==quarter, quarter(hi)!=quarter in mode 2 */
    wx_tz tz;                /* the zone the local figures are in */
} wx_almanac;

static inline int wx_moon_quarter(uint32_t base,uint32_t step){return (int)(wx_moon_elongation((int64_t)base+(int64_t)step*1200)/90.0)&3;}
/* Starts the almanac for `now` at lat/lon in zone `z`: the sun figures at once, the moon search
 * from wx_almanac_step. */
static inline void wx_almanac_begin_z(wx_almanac *a,int64_t utc_s,double lat,double lon,wx_tz z) {
    wx_time t;
    a->tz=z;
    wx_local_time_z(utc_s,z,&t);
    a->day=t.days;
    for(int k=0;k<2;k++) {
        int64_t r,s;
        a->dow[k]=(uint8_t)wx_dow(t.days+k);
        a->rise[k]=a->set[k]=-1;
        if(!wx_sun_times(t.days+k,lat,lon,&r,&s)) {
            if(!k)a->rise_s=a->set_s=0;
            continue;
        }
        wx_time lt;
        wx_local_time_z(r,z,&lt);a->rise[k]=(int16_t)(lt.hour*60+lt.minute);
        wx_local_time_z(s,z,&lt);a->set[k]=(int16_t)(lt.hour*60+lt.minute);
        if(!k){a->rise_s=(uint32_t)r;a->set_s=(uint32_t)s;}
    }
    a->moon_ready=0;a->found=0;a->mode=0;a->base=(uint32_t)utc_s;
}
/* The same in the default pack's zone (US Eastern: Orlando). */
static inline void wx_almanac_begin(wx_almanac *a,int64_t utc_s,double lat,double lon){wx_almanac_begin_z(a,utc_s,lat,lon,wx_tz_eastern());}
/* One slice of the moon search (at most WX_MOON_EVALS evaluations). 1 when the figures are complete. */
static inline int wx_almanac_step(wx_almanac *a) {
    if(a->moon_ready)return 1;
    int evals=0;
    if(a->mode==0) {
        double e=wx_moon_elongation((int64_t)a->base);
        double illum=(1.0-cos(e*WX_PI/180.0))/2.0*100.0;
        a->moon_pct=(uint8_t)floor(illum+0.5);
        if(e<6||e>354)a->moon_name=WX_MOON_NEW;
        else if(fabs(e-90)<6)a->moon_name=WX_MOON_FIRST_QUARTER;
        else if(fabs(e-180)<6)a->moon_name=WX_MOON_FULL;
        else if(fabs(e-270)<6)a->moon_name=WX_MOON_LAST_QUARTER;
        else a->moon_name=(uint8_t)(((int)(e/90.0)&3)*2+1);
        a->quarter=(uint8_t)((int)(e/90.0)&3);a->lo=0;a->mode=1;a->found=0;
        evals++;
    }
    while(evals<WX_MOON_EVALS&&a->found<4) {
        if(a->mode==1) { /* a day (72 steps) at a time until the quarter changes */
            uint32_t next=a->lo+72u;
            if(next>72u*400u){a->found=4;break;} /* never: a quarter is about 7.4 days */
            evals++;
            if(wx_moon_quarter(a->base,next)!=a->quarter){a->hi=next;a->mode=2;}
            else a->lo=next;
        } else { /* halve until hi is the first step in the new quarter */
            if(a->hi-a->lo<=1u) {
                int q=wx_moon_quarter(a->base,a->hi);
                wx_time t;
                evals++;
                wx_local_time_z((int64_t)a->base+(int64_t)a->hi*1200,a->tz,&t);
                a->next_phase[a->found]=(uint8_t)q;a->next_month[a->found]=(uint8_t)t.month;a->next_day[a->found]=(uint8_t)t.day;
                a->found++;a->quarter=(uint8_t)q;a->lo=a->hi;a->mode=1;
                continue;
            }
            uint32_t mid=a->lo+(a->hi-a->lo)/2u;
            evals++;
            if(wx_moon_quarter(a->base,mid)!=a->quarter)a->hi=mid;else a->lo=mid;
        }
    }
    if(a->found>=4){a->moon_ready=1;a->mode=0;}
    return a->moon_ready;
}
#endif

