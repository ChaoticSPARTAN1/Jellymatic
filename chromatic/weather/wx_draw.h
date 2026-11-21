/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: the pages, drawn straight into the 160x144 RGB565 overlay frame.
 *
 * A port of the approved PC reference (the project's PC reference renderer (not published)): same 16 colours, same
 * coordinates, the same two pixel fonts and icons (wx_art.h). Each frame is drawn in full:
 * the backdrop by rule (23,040 pixels, a table look-up each), then the page, the header with the
 * clock, and the ticker. No frame is kept, nothing is blended, no floating point. A frame is
 * roughly 150,000 to 250,000 simple operations (the backdrop is most of it): a few milliseconds
 * on the ESP32, less than the 46 KB transfer of the frame to the display.
 * Colours come from wx_palette only, which never holds the overlay's see-through key (0xF81F;
 * make_art.py refuses to write it).
 * Portable C99, no allocation. Part of wx.h; include that.
 */
#ifndef WX_DRAW_H
#define WX_DRAW_H
#ifndef WX_H
#error "include wx.h"
#endif

#define WX_W 160
#define WX_H_PX 144
#define WX_DEG "\x7f"           /* the degree sign in the strings drawn here */
enum {WX_SILK,WX_PS};
enum {WX_LEFT,WX_RIGHT,WX_CENTRE};
#define WX_NOSHADOW 255

typedef struct {uint16_t *fb;int x0,y0,x1,y1;} wx_gc; /* the frame and the area that may be drawn (x1, y1 exclusive) */

static inline void wx_px(const wx_gc *g,int x,int y,unsigned c){if(x>=g->x0&&x<g->x1&&y>=g->y0&&y<g->y1)g->fb[y*WX_W+x]=wx_palette[c&15u];}
/* corners inclusive, like the reference's rectangle() */
static inline void wx_fill(const wx_gc *g,int x0,int y0,int x1,int y1,unsigned c) {
    if(x0<g->x0)x0=g->x0;
    if(y0<g->y0)y0=g->y0;
    if(x1>=g->x1)x1=g->x1-1;
    if(y1>=g->y1)y1=g->y1-1;
    uint16_t v=wx_palette[c&15u];
    for(int y=y0;y<=y1;y++){uint16_t *row=g->fb+y*WX_W;for(int x=x0;x<=x1;x++)row[x]=v;}
}
static inline void wx_box(const wx_gc *g,int x0,int y0,int x1,int y1,unsigned c) {
    wx_fill(g,x0,y0,x1,y0,c);wx_fill(g,x0,y1,x1,y1,c);wx_fill(g,x0,y0,x0,y1,c);wx_fill(g,x1,y0,x1,y1,c);
}
static inline void wx_panel(const wx_gc *g,int x0,int y0,int x1,int y1) {
    wx_fill(g,x0,y0,x1,y1,WX_NAVY);
    wx_fill(g,x0,y0,x1,y0,WX_SKY);wx_fill(g,x0,y0,x0,y1,WX_SKY);
    wx_fill(g,x0,y1,x1,y1,WX_BLACK);wx_fill(g,x1,y0,x1,y1,WX_BLACK);
}
static inline unsigned wx_glyph_of(char ch) {
    unsigned c=(unsigned char)wx_upper(ch);
    if(c==0x7fu)return WX_FONT_DEGREE;
    if(c<WX_FONT_FIRST||c>=WX_FONT_FIRST+WX_FONT_DEGREE)return '?'-WX_FONT_FIRST;
    return c-WX_FONT_FIRST;
}
static inline int wx_text_w(const char *s,int font) {
    const uint8_t *adv=font==WX_PS?wx_ps_adv:wx_silk_adv;
    int w=0;
    for(;*s;s++)w+=adv[wx_glyph_of(*s)];
    return w;
}
/* Letters with their capital tops at y; scale 1 or more. Returns the x after the last letter. */
static inline int wx_glyphs(const wx_gc *g,int x,int y,const char *s,int font,int scale,unsigned colour) {
    const uint8_t *adv=font==WX_PS?wx_ps_adv:wx_silk_adv,*bits=font==WX_PS?wx_ps_bits:wx_silk_bits;
    int rows=font==WX_PS?WX_PS_ROWS:WX_SILK_ROWS,top=font==WX_PS?WX_PS_TOP:WX_SILK_TOP;
    for(;*s;s++) {
        unsigned gi=wx_glyph_of(*s);
        if(x+8*scale>g->x0&&x<g->x1) {
            const uint8_t *b=bits+gi*(unsigned)rows;
            for(int r=0;r<rows;r++) {
                unsigned m=b[r];
                for(int k=0;m;k++,m=(m<<1)&255u) {
                    if(!(m&128u))continue;
                    if(scale==1)wx_px(g,x+k,y+top+r,colour);
                    else wx_fill(g,x+k*scale,y+(top+r)*scale,x+k*scale+scale-1,y+(top+r)*scale+scale-1,colour);
                }
            }
        }
        x+=adv[gi]*scale;
    }
    return x;
}
/* Text as the reference draws it: a black shadow one pixel down and right unless told otherwise. */
static inline int wx_text(const wx_gc *g,int x,int y,const char *s,int font,unsigned colour,unsigned shadow,int anchor) {
    int w=wx_text_w(s,font);
    if(anchor==WX_RIGHT)x-=w;else if(anchor==WX_CENTRE)x-=w/2;
    if(shadow!=WX_NOSHADOW)wx_glyphs(g,x+1,y+1,s,font,1,shadow);
    wx_glyphs(g,x,y,s,font,1,colour);
    return x+w;
}
/* A 4-bit picture (0 = see-through); mono < 16 draws every pixel in that colour (a shadow). */
static inline void wx_icon(const wx_gc *g,const uint8_t *data,int w,int h,int x,int y,int scale,unsigned mono) {
    for(int j=0;j<h;j++)for(int i=0;i<w;i++) {
        unsigned n=(unsigned)(j*w+i),c=n&1u?data[n>>1]&15u:data[n>>1]>>4;
        if(!c)continue;
        if(mono<16u)c=mono;
        if(scale==1)wx_px(g,x+i,y+j,c);
        else wx_fill(g,x+i*scale,y+j*scale,x+i*scale+scale-1,y+j*scale+scale-1,c);
    }
}
/* Run-length rows from wx_art.h, full width, starting at row y0. */
static inline void wx_layer(const wx_gc *g,const uint8_t *data,size_t size,int y0,int rows) {
    size_t at=0;
    for(int y=y0;y<y0+rows;y++) {
        for(int x=0;x<WX_W&&at+1<size;at+=2) {
            int n=data[at];
            if(data[at+1]!=255u)wx_fill(g,x,y,x+n-1,y,data[at+1]);
            x+=n;
        }
    }
}
/* The dithered deep-blue-to-purple sky with its four clouds: the gradient by rule, the cloud
 * shapes from wx_clouds; a cloud lightens every second pixel by one shade. */
static inline void wx_backdrop(const wx_gc *g) {
    static const uint8_t bayer[4][4]={{0,8,2,10},{12,4,14,6},{3,11,1,9},{15,7,13,5}};
    static const uint8_t stop_y[5]={0,26,70,112,150},stop_c[5]={WX_NAVY,WX_DBLUE,WX_BLUE,WX_PURPLE,WX_NAVY};
    static const uint8_t lighter[16]={0,WX_DBLUE,WX_BLUE,WX_SKY,WX_VIOLET,5,6,7,8,9,10,11,12,13,14,15};
    size_t at=0;
    int band=0;
    for(int y=0;y<WX_H_PX;y++) {
        while(band<3&&y>=stop_y[band+1])band++;
        int num=16*(y-stop_y[band]),den=stop_y[band+1]-stop_y[band];
        uint8_t pat[4];
        for(int k=0;k<4;k++)pat[k]=bayer[y&3][k]*den<num?stop_c[band+1]:stop_c[band];
        uint16_t *row=g->fb+y*WX_W;
        int x=0;
        while(x<WX_W&&at+1<sizeof(wx_clouds)) {
            int n=wx_clouds[at],cloud=wx_clouds[at+1]==1u;
            at+=2;
            for(int end=x+n;x<end&&x<WX_W;x++) {
                unsigned c=pat[x&3];
                if(cloud&&!((x+y)&1))c=lighter[c];
                row[x]=wx_palette[c];
            }
        }
        for(;x<WX_W;x++)row[x]=wx_palette[pat[x&3]];
    }
}

/* ---- wording ---- */
static inline void wx_fmt(char *out,size_t cap,int v,const char *suffix) {
    out[0]=0;
    if(v==WX_NA){wx_append(out,cap,"--");return;}
    wx_append_int(out,cap,v);wx_append(out,cap,suffix);
}
static const char *const wx_compass[16]={"N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"};
static const char *const wx_days3[8]={"SUN","MON","TUE","WED","THU","FRI","SAT","--"};
static const char *const wx_days_full[7]={"SUNDAY","MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY"};
static const char *const wx_months3[12]={"JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"};
static inline void wx_wind_text(const wx_obs *o,char *out,size_t cap) {
    out[0]=0;
    if(o->wind_mph==WX_NA){wx_append(out,cap,"--");return;}
    if(o->wind_mph==0){wx_append(out,cap,"CALM");return;}
    wx_append(out,cap,o->wind_dir>=0&&o->wind_dir<16?wx_compass[o->wind_dir]:"VRB");
    wx_append(out,cap," ");wx_append_int(out,cap,o->wind_mph);wx_append(out,cap," MPH");
}
static inline void wx_pressure_text(const wx_obs *o,char *out,size_t cap) {
    out[0]=0;
    if(o->pressure==WX_NA){wx_append(out,cap,"--");return;}
    wx_append_int(out,cap,o->pressure/100);wx_append(out,cap,".");
    if(o->pressure%100<10)wx_append(out,cap,"0");
    wx_append_int(out,cap,o->pressure%100);wx_append(out,cap," IN");
}
static inline void wx_vis_text(const wx_obs *o,char *out,size_t cap) {
    out[0]=0;
    if(o->vis10==WX_NA){wx_append(out,cap,"--");return;}
    if(o->vis10>=30)wx_append_int(out,cap,(o->vis10+5)/10);
    else{wx_append_int(out,cap,o->vis10/10);if(o->vis10%10){wx_append(out,cap,".");wx_append_int(out,cap,o->vis10%10);}}
    wx_append(out,cap," MI");
}
static inline void wx_clock_text_z(int64_t epoch_s,wx_tz z,char *out,size_t cap,int spaced) {
    out[0]=0;
    if(!wx_clock_ok(epoch_s)){wx_append(out,cap,"--:--");return;}
    wx_time t;
    wx_local_time_z(epoch_s,z,&t);
    wx_append_int(out,cap,t.hour%12?t.hour%12:12);wx_append(out,cap,":");
    if(t.minute<10)wx_append(out,cap,"0");
    wx_append_int(out,cap,t.minute);wx_append(out,cap,":");
    if(t.second<10)wx_append(out,cap,"0");
    wx_append_int(out,cap,t.second);wx_append(out,cap,spaced?" ":"");wx_append(out,cap,t.hour<12?"AM":"PM");
}
static inline void wx_clock_text(int64_t epoch_s,char *out,size_t cap,int spaced){wx_clock_text_z(epoch_s,wx_tz_eastern(),out,cap,spaced);}
static inline void wx_minutes_text(int minutes,char *out,size_t cap) {
    out[0]=0;
    if(minutes<0){wx_append(out,cap,"--");return;}
    int h=minutes/60,m=minutes%60;
    wx_append_int(out,cap,h%12?h%12:12);wx_append(out,cap,":");
    if(m<10)wx_append(out,cap,"0");
    wx_append_int(out,cap,m);wx_append(out,cap,h<12?" AM":" PM");
}
/* The next line of `text` from *pos that fits `max_w` pixels (a word too long for a line is cut).
 * Returns its length; 0 at the end of the text. */
static inline int wx_wrap_next(const char *text,int *pos,char *line,int cap,int font,int max_w) {
    const uint8_t *adv=font==WX_PS?wx_ps_adv:wx_silk_adv;
    int p=*pos,len=0,w=0;
    while(text[p]==' ')p++;
    while(text[p]) {
        int q=p,ww=0,wl=0;
        while(text[q]&&text[q]!=' '){ww+=adv[wx_glyph_of(text[q])];q++;wl++;}
        int space=len?adv[0]:0;
        if(len&&(w+space+ww>max_w||len+1+wl>=cap))break;
        if(!len&&(ww>max_w||wl>=cap)) { /* one long word: as much as fits */
            while(text[p]&&text[p]!=' '&&len+1<cap&&w+adv[wx_glyph_of(text[p])]<=max_w){w+=adv[wx_glyph_of(text[p])];line[len++]=text[p++];}
            if(!len&&text[p])line[len++]=text[p++];
            break;
        }
        if(len)line[len++]=' ';
        memcpy(line+len,text+p,(size_t)wl);
        len+=wl;w+=space+ww;p=q;
        while(text[p]==' ')p++;
    }
    line[len]=0;*pos=p;
    return len;
}
static inline int wx_wrap_count(const char *text,int font,int max_w) {
    char line[40];
    int pos=0,n=0;
    while(wx_wrap_next(text,&pos,line,sizeof(line),font,max_w)&&n<200)n++;
    return n;
}

/* ---- the pages ---- */
enum {WX_P_IDENT,WX_P_CUR1,WX_P_CUR2,WX_P_OBS,WX_P_ALMANAC,WX_P_LOCAL,WX_P_EXT,WX_P_STATE,WX_P_TRI,WX_P_OUTLOOK,WX_P_RADAR,WX_P_KINDS};
static const char *const wx_titles[WX_P_KINDS][2]={{"",""},{"CURRENT","CONDITIONS"},{"CURRENT","CONDITIONS"},
    {"LATEST","OBSERVATIONS"},{"TODAY'S","ALMANAC"},{"LOCAL","FORECAST"},{"EXTENDED","FORECAST"},
    {"REGIONAL","FORECAST"},{"REGIONAL","FORECAST"},{"RAIN","OUTLOOK"},{"CURRENT","RADAR"}};
#define WX_LOCAL_LINES 9      /* lines of forecast text a page */
#define WX_LOCAL_W 152

static inline int wx_is_night(const wx_state *s,int64_t epoch_s) {
    if(!wx_clock_ok(epoch_s)||!s->alm.day||!s->alm.rise_s)return 0;
    return !((uint32_t)epoch_s>=s->alm.rise_s&&(uint32_t)epoch_s<=s->alm.set_s);
}
/* label left, value right in the large font (the small one when it does not fit) */
static inline void wx_row(const wx_gc *g,int y,const char *label,const char *value,int lx,int rx) {
    int font=WX_PS;
    wx_text(g,lx,y+2,label,WX_SILK,WX_WHITE,WX_BLACK,WX_LEFT);
    if(wx_text_w(value,WX_PS)>rx-lx-wx_text_w(label,WX_SILK)-4){font=WX_SILK;y+=2;}
    wx_text(g,rx,y,value,font,WX_YELLOW,WX_BLACK,WX_RIGHT);
}
static inline void wx_page_ident(const wx_gc *g,const wx_state *s,int updating) {
    int bw=WX_BADGE_W*3;
    const char *name=wx_home_name(s);
    wx_icon(g,wx_badge,WX_BADGE_W,WX_BADGE_H,80-bw/2+3,21,3,WX_BLACK);
    wx_icon(g,wx_badge,WX_BADGE_W,WX_BADGE_H,80-bw/2,18,3,16);
    wx_fill(g,27,82,132,82,WX_YELLOW);wx_fill(g,27,83,132,83,WX_ORANGE);wx_fill(g,27,84,132,84,WX_RED);
    if(wx_tables(s)||wx_text_w(name,WX_PS)<=156)wx_text(g,80,92,name,WX_PS,WX_WHITE,WX_BLACK,WX_CENTRE);
    else wx_text(g,80,94,name,WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE); /* a long place name: the small letters */
    wx_text(g,80,106,wx_home_area(s),WX_SILK,WX_YELLOW,WX_BLACK,WX_CENTRE);
    wx_text(g,80,117,"YOUR LOCAL FORECAST",WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
    if(updating)wx_text(g,80,125,"UPDATING",WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
}
static inline void wx_page_current_1(const wx_gc *g,const wx_state *s,int64_t epoch_s) {
    const wx_obs *o=&s->obs[0];
    char a[48],line[40];
    a[0]=0;wx_append(a,sizeof(a),"AT ");wx_append(a,sizeof(a),wx_obs_label_at(s,0));
    wx_text(g,80,26,a,WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
    wx_icon(g,wx_icons+128u*(unsigned)wx_classify_icon(o->sky,!wx_is_night(s,epoch_s)),16,16,12,36,2,16);
    wx_fmt(a,sizeof(a),o->temp_f,"");
    wx_glyphs(g,59,41,a,WX_PS,3,WX_BLACK);
    int x=wx_glyphs(g,58,40,a,WX_PS,3,WX_YELLOW);
    if(o->temp_f!=WX_NA){wx_box(g,x+2,40,x+9,47,WX_BLACK);wx_box(g,x+1,39,x+8,46,WX_YELLOW);wx_box(g,x+2,40,x+7,45,WX_YELLOW);}
    const char *sky=o->sky[0]?o->sky:"--";
    int font=wx_text_w(sky,WX_PS)<=156?WX_PS:WX_SILK,lines=wx_wrap_count(sky,font,156),pos=0;
    if(lines>2)lines=2;
    for(int i=0;i<lines;i++) {
        wx_wrap_next(sky,&pos,line,sizeof(line),font,156);
        wx_text(g,80,74+i*10-(lines>1?5:0),line,font,WX_WHITE,WX_BLACK,WX_CENTRE);
    }
    wx_wind_text(o,a,sizeof(a));wx_row(g,92,"WIND",a,12,148);
    wx_fmt(a,sizeof(a),o->humidity,"%");wx_row(g,104,"HUMIDITY",a,12,148);
    if(o->feels_f!=WX_NA&&o->feels_kind){wx_fmt(a,sizeof(a),o->feels_f,WX_DEG "F");wx_row(g,116,o->feels_kind==1?"WIND CHILL":"HEAT INDEX",a,12,148);}
    else{wx_fmt(a,sizeof(a),o->dew_f,WX_DEG "F");wx_row(g,116,"DEW POINT",a,12,148);}
}
static inline void wx_page_current_2(const wx_gc *g,const wx_state *s) {
    static const char *const trend[4]={"--","RISING","FALLING","STEADY"};
    const wx_obs *o=&s->obs[0];
    char a[32];
    int have=o->time_s!=0;
    wx_panel(g,3,25,156,128);
    wx_fmt(a,sizeof(a),o->dew_f,WX_DEG "F");wx_row(g,31,"DEW POINT",a,9,150);
    wx_pressure_text(o,a,sizeof(a));wx_row(g,47,"PRESSURE",a,9,150);
    wx_row(g,63,"TREND",trend[have&&s->trend<4?s->trend:0],9,150);
    if(!have)wx_row(g,79,"GUSTS","--",9,150);
    else if(o->gust_mph==WX_NA||o->gust_mph==0)wx_row(g,79,"GUSTS","NONE",9,150);
    else{wx_fmt(a,sizeof(a),o->gust_mph," MPH");wx_row(g,79,"GUSTS",a,9,150);}
    wx_vis_text(o,a,sizeof(a));wx_row(g,95,"VISIBILITY",a,9,150);
    if(!have)wx_row(g,111,"CEILING","--",9,150);
    else if(o->ceiling==WX_NA)wx_row(g,111,"CEILING","UNLIMITED",9,150);
    else{a[0]=0;wx_append_int(a,sizeof(a),o->ceiling*100);wx_append(a,sizeof(a)," FT");wx_row(g,111,"CEILING",a,9,150);}
}
static inline void wx_page_obs(const wx_gc *g,const wx_state *s,int first) {
    char a[24],wind[20],brief[9];
    wx_panel(g,1,24,158,129);
    for(int i=0;i<6&&first+i<(int)wx_obs_rows(s);i++) {
        const wx_obs *o=&s->obs[wx_obs_slot_at(s,(unsigned)(first+i))];
        int y=27+i*17;
        if(i)wx_fill(g,4,y-2,155,y-2,WX_SLATE);
        wx_text(g,5,y,wx_obs_label_at(s,(unsigned)(first+i)),WX_PS,WX_WHITE,WX_BLACK,WX_LEFT);
        wx_fmt(a,sizeof(a),o->temp_f,WX_DEG);
        wx_text(g,156,y,a,WX_PS,WX_YELLOW,WX_BLACK,WX_RIGHT);
        wx_wind_text(o,wind,sizeof(wind));
        wx_short_sky(o->sky,brief);
        const char *sky=o->sky[0]&&wx_text_w(o->sky,WX_SILK)<=140-wx_text_w(wind,WX_SILK)?o->sky:brief;
        wx_text(g,11,y+9,sky,WX_SILK,WX_CYAN,WX_NOSHADOW,WX_LEFT);
        wx_text(g,155,y+9,wind,WX_SILK,WX_GREY,WX_NOSHADOW,WX_RIGHT);
    }
}
static inline void wx_page_almanac(const wx_gc *g,const wx_state *s) {
    static const char *const phase[4]={"NEW","FIRST","FULL","LAST"};
    static const int cols[2]={86,132};
    const wx_almanac *m=&s->alm;
    char a[32];
    wx_panel(g,3,24,156,70);
    for(int k=0;k<2;k++)wx_text(g,cols[k],28,m->day?wx_days3[m->dow[k]%7]:"--",WX_PS,WX_ORANGE,WX_BLACK,WX_CENTRE);
    for(int j=0;j<2;j++) {
        int y=41+j*14;
        wx_text(g,8,y+1,j?"SUNSET":"SUNRISE",WX_SILK,WX_WHITE,WX_BLACK,WX_LEFT);
        for(int k=0;k<2;k++) {
            wx_minutes_text(m->day?(j?m->set[k]:m->rise[k]):-1,a,sizeof(a));
            wx_text(g,cols[k],y+1,a,WX_SILK,WX_YELLOW,WX_BLACK,WX_CENTRE);
        }
    }
    wx_text(g,6,76,"MOON",WX_PS,WX_ORANGE,WX_BLACK,WX_LEFT);
    if(!m->day||!m->moon_ready) {
        wx_text(g,155,78,"--",WX_SILK,WX_WHITE,WX_BLACK,WX_RIGHT);
        wx_text(g,80,100,m->day?"WORKING IT OUT":"CLOCK NOT SET",WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
        return;
    }
    a[0]=0;wx_append(a,sizeof(a),wx_moon_names[m->moon_name&7]);wx_append(a,sizeof(a)," ");
    wx_append_int(a,sizeof(a),m->moon_pct);wx_append(a,sizeof(a),"%");
    wx_text(g,155,78,wx_text_w(a,WX_SILK)<=110?a:wx_moon_names[m->moon_name&7],WX_SILK,WX_WHITE,WX_BLACK,WX_RIGHT);
    for(int i=0;i<4;i++) {
        int x=20+i*40;
        wx_icon(g,wx_moons+WX_MOON_BYTES*(m->next_phase[i]&3u),WX_MOON_SIZE,WX_MOON_SIZE,x-6,90,1,16);
        wx_text(g,x,107,phase[m->next_phase[i]&3],WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
        a[0]=0;wx_append(a,sizeof(a),wx_months3[(m->next_month[i]+11)%12]);wx_append(a,sizeof(a)," ");wx_append_int(a,sizeof(a),m->next_day[i]);
        wx_text(g,x,117,a,WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
    }
}
static inline void wx_page_local(const wx_gc *g,const wx_state *s,unsigned arg) {
    unsigned period=arg>>4,part=arg&15u;
    char title[40],line[24];
    if(period>=s->fc.n_local) {
        wx_text(g,80,60,"DATA NOT AVAILABLE",WX_SILK,WX_YELLOW,WX_BLACK,WX_CENTRE);
        wx_text(g,80,72,"THE FORECAST WILL SHOW",WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
        wx_text(g,80,81,"WHEN IT HAS BEEN FETCHED",WX_SILK,WX_CYAN,WX_BLACK,WX_CENTRE);
        return;
    }
    const wx_period *p=&s->fc.local[period];
    int font=WX_PS,pos=0;
    for(size_t keep=strlen(p->name);;keep--) { /* a very long name loses letters, never the "(CONT)" */
        title[0]=0;wx_append(title,keep+1<sizeof(title)?keep+1:sizeof(title),p->name);
        if(part)wx_append(title,sizeof(title)," (CONT)");
        font=wx_text_w(title,WX_PS)<=152?WX_PS:WX_SILK;
        if(!keep||wx_text_w(title,font)<=152)break;
    }
    wx_text(g,4,26,title,font,WX_YELLOW,WX_BLACK,WX_LEFT);
    wx_fill(g,4,36,4+wx_text_w(title,font),36,WX_ORANGE);
    for(unsigned skip=part*WX_LOCAL_LINES;skip;skip--)if(!wx_wrap_next(p->text,&pos,line,sizeof(line),WX_PS,WX_LOCAL_W))break;
    for(int i=0;i<WX_LOCAL_LINES;i++) {
        if(!wx_wrap_next(p->text,&pos,line,sizeof(line),WX_PS,WX_LOCAL_W))break;
        wx_text(g,4,41+i*10,line,WX_PS,WX_WHITE,WX_BLACK,WX_LEFT);
    }
}
static inline void wx_page_extended(const wx_gc *g,const wx_state *s,int first) {
    char a[12];
    for(int i=0;i<3;i++) {
        int x0=2+i*53,cx=x0+25;
        const wx_ext *e=first+i<s->fc.n_ext?&s->fc.ext[first+i]:NULL;
        wx_gc in=*g;
        wx_panel(g,x0,24,x0+50,129);
        if(in.x0<x0)in.x0=x0;
        if(in.x1>x0+51)in.x1=x0+51;
        wx_text(&in,cx,28,e?wx_days3[e->dow&7]:"--",WX_PS,WX_ORANGE,WX_BLACK,WX_CENTRE);
        wx_icon(&in,wx_icons+128u*(e?e->icon%WX_ICON_COUNT:WX_ICON_NONE),16,16,cx-16,39,2,16);
        if(e) {
            int one=!e->sky[1][0];
            wx_text(&in,cx,75+(one?4:0),e->sky[0],WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
            if(!one)wx_text(&in,cx,83,e->sky[1],WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
        } else wx_text(&in,cx,79,"--",WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
        wx_text(&in,x0+5,97,"HI",WX_SILK,WX_ORANGE,WX_BLACK,WX_LEFT);
        wx_fmt(a,sizeof(a),e?e->hi:WX_NA,"");wx_text(&in,x0+47,95,a,WX_PS,WX_YELLOW,WX_BLACK,WX_RIGHT);
        wx_text(&in,x0+5,113,"LO",WX_SILK,WX_CYAN,WX_BLACK,WX_LEFT);
        wx_fmt(a,sizeof(a),e?e->lo:WX_NA,"");wx_text(&in,x0+47,111,a,WX_PS,WX_WHITE,WX_BLACK,WX_RIGHT);
    }
}
/* A city call-out: its name over an icon and the temperature; x is the centre. */
static inline void wx_stack(const wx_gc *g,int x,int y,const char *name,const wx_city *c) {
    char a[12];
    wx_text(g,x,y,name,WX_SILK,WX_WHITE,WX_BLACK,WX_CENTRE);
    wx_fmt(a,sizeof(a),c->temp,"");
    if(x>WX_W-3-wx_text_w(a,WX_PS))x=WX_W-3-wx_text_w(a,WX_PS); /* three figures stay on screen */
    wx_icon(g,wx_icons+128u*(c->icon%WX_ICON_COUNT),16,16,x-17,y+7,1,16);
    wx_text(g,x+1,y+11,a,WX_PS,WX_YELLOW,WX_BLACK,WX_LEFT);
}
/* "FRIDAY" / "TODAY" / "TONIGHT" and "HIGH" / "LOW" of the regional figures (the home city's when it has them). */
static inline void wx_regional_words(const wx_state *s,const char **when,const char **kind) {
    const wx_pack *r=wx_region(s);
    const wx_city *c=&s->city[r?r->home_city:0];
    *when="";*kind="HIGH";
    if(!c->kind)for(int i=0;i<(r?r->cities:0);i++)if(s->city[i].kind){c=&s->city[i];break;}
    if(!c->kind)return;
    *kind=c->kind==2?"LOW":"HIGH";
    *when=c->label==1?"TODAY":c->label==2?"TONIGHT":c->label>=3&&c->label<=9?wx_days_full[c->label-3]:"";
}
/* 1.6.4: the region's maps (wx_region: the built-in Florida pack, or a location pack); triangle 1 is its close-up. */
static inline void wx_page_regional(const wx_gc *g,const wx_state *s,int triangle) {
    const wx_pack *r=wx_region(s);
    const char *when,*kind;
    char a[32];
    if(!r)return;
    if(triangle)wx_layer(g,r->close_map,r->close_map_n,r->close_y0,r->close_rows);
    else wx_layer(g,r->state_map,r->state_map_n,r->state_y0,r->state_rows);
    for(int i=0;i<r->cities;i++) {
        const uint8_t *spot=r->city_spots+i*4+(triangle?2:0);
        if(spot[0]!=255u)wx_stack(g,spot[0],spot[1],r->city_name[i],&s->city[i]);
    }
    wx_regional_words(s,&when,&kind);
    if(triangle) {
        wx_text(g,r->close_title_x,r->close_title_y,r->close_title,WX_SILK,WX_WHITE,WX_BLACK,WX_RIGHT);
        a[0]=0;wx_append(a,sizeof(a),when);if(when[0])wx_append(a,sizeof(a)," ");
        wx_append(a,sizeof(a),kind);wx_append(a,sizeof(a),"S");
        wx_text(g,r->close_title_x,r->close_title_y+9,a,WX_SILK,WX_ORANGE,WX_BLACK,WX_RIGHT);
    } else {
        wx_text(g,r->words_x,r->words_y,when,WX_SILK,WX_ORANGE,WX_BLACK,WX_LEFT);
        a[0]=0;wx_append(a,sizeof(a),kind);wx_append(a,sizeof(a),"S");
        wx_text(g,r->words_x,r->words_y+8,a,WX_SILK,WX_ORANGE,WX_BLACK,WX_LEFT);
    }
}
/* Current Radar: the dark county map from flash, the picture's precipitation levels over it in
 * five colours, the three towns, the light-to-heavy key and the time the picture was taken. */
static inline void wx_page_radar(const wx_gc *g,const wx_state *s) {
    static const uint8_t colour[6]={0,WX_DGREEN,WX_GREEN,WX_YELLOW,WX_ORANGE,WX_RED};
    const wx_pack *r=wx_region(s);   /* the place's pack: the built-in Orlando view, or a location pack's */
    char a[16];
    int any=0,key=WX_RADAR_Y0+WX_RADAR_ROWS;
    if(r&&r->radar_map)wx_layer(g,r->radar_map,r->radar_map_n,WX_RADAR_Y0,WX_RADAR_ROWS);
    for(int y=0;y<WX_RADAR_H&&y<WX_RADAR_ROWS;y++)for(int x=0;x<WX_RADAR_W;x++) {
        unsigned level=wx_radar_get(s->radar,(unsigned)(y*WX_RADAR_W+x));
        if(level&&level<6u){wx_px(g,x,WX_RADAR_Y0+y,colour[level]);any=1;}
    }
    for(int i=0;r&&r->radar_map&&i<3;i++) {
        int x=r->radar_marks[i*2],y=r->radar_marks[i*2+1];
        wx_fill(g,x-1,y-1,x+1,y+1,WX_BLACK);wx_px(g,x,y,WX_WHITE);
        wx_text(g,i?x-4:x+4,y-2,r->radar_town[i],WX_SILK,WX_WHITE,WX_BLACK,i?WX_RIGHT:WX_LEFT);
    }
    if(!any)wx_text(g,3,key-9,"NO RAIN IN THE AREA",WX_SILK,WX_CYAN,WX_BLACK,WX_LEFT);
    wx_fill(g,0,key,159,130,WX_NAVY);wx_fill(g,0,key,159,key,WX_SKY);
    int x=wx_text(g,3,key+3,"LIGHT",WX_SILK,WX_WHITE,WX_NOSHADOW,WX_LEFT)+3;
    for(int i=1;i<6;i++,x+=9){wx_fill(g,x,key+3,x+7,key+8,colour[i]);wx_box(g,x,key+3,x+7,key+8,WX_BLACK);}
    wx_text(g,x+2,key+3,"HEAVY",WX_SILK,WX_WHITE,WX_NOSHADOW,WX_LEFT);
    if(s->radar_at) {
        wx_time t;
        wx_local_time_z((int64_t)s->radar_at,wx_state_tz(s),&t);
        a[0]=0;wx_append_int(a,sizeof(a),t.hour%12?t.hour%12:12);wx_append(a,sizeof(a),":");
        if(t.minute<10)wx_append(a,sizeof(a),"0");
        wx_append_int(a,sizeof(a),t.minute);wx_append(a,sizeof(a),t.hour<12?"AM":"PM");
        wx_text(g,157,key+3,a,WX_SILK,WX_CYAN,WX_NOSHADOW,WX_RIGHT);
    }
}
/* The page shown while no radar picture is held: the chance of rain for each coming period at
 * home, from the forecast already held. Not a radar picture, and it says what it is. */
static inline void wx_outlook_label(const char *name,char *out,size_t cap) {
    out[0]=0;
    for(int d=0;d<7;d++) {
        size_t n=strlen(wx_days_full[d]);
        if(!strncmp(name,wx_days_full[d],n)){wx_append(out,cap,wx_days3[d]);if(wx_has(name+n,"NIGHT"))wx_append(out,cap," NIGHT");return;}
    }
    if(wx_has(name,"NIGHT"))wx_append(out,cap,"TONIGHT");
    else if(wx_has(name,"AFTERNOON")||wx_has(name,"TODAY"))wx_append(out,cap,"TODAY");
    else{wx_append(out,cap<10?cap:10,name);}
}
static inline void wx_page_outlook(const wx_gc *g,const wx_state *s) {
    static const uint8_t level[5]={WX_DGREEN,WX_GREEN,WX_YELLOW,WX_ORANGE,WX_RED};
    char a[16];
    int rows=0;
    wx_panel(g,3,24,156,129);
    { /* "CHANCE OF RAIN - ORLANDO, FL"; a name too long for the line: the words alone */
        char line[48];
        line[0]=0;wx_append(line,sizeof(line),"CHANCE OF RAIN - ");wx_append(line,sizeof(line),wx_home_name(s));
        wx_text(g,80,27,wx_tables(s)||wx_text_w(line,WX_SILK)<=152?line:"CHANCE OF RAIN",WX_SILK,WX_CYAN,WX_NOSHADOW,WX_CENTRE);
    }
    for(int i=0;i<WX_LOCAL+WX_EXT&&rows<9;i++) {
        int pop;
        if(i<WX_LOCAL){if(i>=s->fc.n_local)continue;pop=s->fc.local[i].pop;wx_outlook_label(s->fc.local[i].name,a,sizeof(a));}
        else{if(i-WX_LOCAL>=s->fc.n_ext)continue;pop=s->fc.ext[i-WX_LOCAL].pop;a[0]=0;wx_append(a,sizeof(a),wx_days3[s->fc.ext[i-WX_LOCAL].dow&7]);}
        int y=38+rows*10;
        rows++;
        wx_text(g,8,y,a,WX_SILK,WX_WHITE,WX_NOSHADOW,WX_LEFT);
        wx_fill(g,62,y,125,y+5,WX_SLATE);
        if(pop>0)wx_fill(g,62,y,62+(pop>100?100:pop)*63/100,y+5,level[pop>=80?4:pop/20]);
        a[0]=0;
        if(pop<0)wx_append(a,sizeof(a),"--");else{wx_append_int(a,sizeof(a),pop);wx_append(a,sizeof(a),"%");}
        wx_text(g,152,y,a,WX_SILK,WX_YELLOW,WX_NOSHADOW,WX_RIGHT);
    }
    if(!rows) {
        wx_text(g,80,66,"DATA NOT AVAILABLE",WX_SILK,WX_YELLOW,WX_BLACK,WX_CENTRE);
        wx_text(g,80,78,"NO RADAR PICTURE YET",WX_SILK,WX_GREY,WX_NOSHADOW,WX_CENTRE);
    }
}

/* ---- header and ticker ---- */
static inline void wx_header(const wx_gc *g,unsigned kind,int64_t epoch_s,int updating,wx_tz z) {
    char a[20],b[20];
    wx_icon(g,wx_badge,WX_BADGE_W,WX_BADGE_H,1,1,1,16);
    wx_text(g,39,3,wx_titles[kind][0],WX_SILK,WX_YELLOW,WX_BLACK,WX_LEFT);
    wx_text(g,39,11,wx_titles[kind][1],WX_SILK,WX_YELLOW,WX_BLACK,WX_LEFT);
    wx_clock_text_z(epoch_s,z,a,sizeof(a),1);
    if(wx_text_w(a,WX_SILK)>158-(39+wx_text_w(wx_titles[kind][0],WX_SILK)+4))wx_clock_text_z(epoch_s,z,a,sizeof(a),0);
    wx_text(g,158,3,a,WX_SILK,WX_WHITE,WX_BLACK,WX_RIGHT);
    /* while a round of requests runs the date line says so, every other second (always, without a clock) */
    if(updating&&(!wx_clock_ok(epoch_s)||(epoch_s&1))) { /* the shorter word beside a long title */
        int room=158-(39+wx_text_w(wx_titles[kind][1],WX_SILK)+4);
        wx_text(g,158,12,wx_text_w("UPDATING",WX_SILK)<=room?"UPDATING":"UPDATE",WX_SILK,WX_WHITE,WX_BLACK,WX_RIGHT);
    } else if(wx_clock_ok(epoch_s)) {
        wx_time t;
        wx_local_time_z(epoch_s,z,&t);
        b[0]=0;wx_append(b,sizeof(b),wx_months3[(t.month+11)%12]);wx_append(b,sizeof(b)," ");wx_append_int(b,sizeof(b),t.day);
        a[0]=0;wx_append(a,sizeof(a),wx_days3[t.dow]);wx_append(a,sizeof(a)," ");wx_append(a,sizeof(a),b);
        wx_text(g,158,12,wx_text_w(a,WX_SILK)<=158-(39+wx_text_w(wx_titles[kind][1],WX_SILK)+4)?a:b,WX_SILK,WX_CYAN,WX_BLACK,WX_RIGHT);
    }
    wx_fill(g,0,20,159,20,WX_YELLOW);wx_fill(g,0,21,159,21,WX_ORANGE);wx_fill(g,0,22,159,22,WX_RED);
}
#define WX_TICKER_LINES 5
#define WX_TICKER_W 142
/* Line `i` of the bottom bar: the first wording that fits. */
static inline void wx_ticker_line(const wx_state *s,unsigned i,char *out,size_t cap) {
    static const char *const trend[4]={NULL,"RISING","FALLING","STEADY"};
    const wx_obs *o=&s->obs[0];
    char a[24],b[24];
    out[0]=0;
    if(!o->time_s&&o->temp_f==WX_NA) { /* 1.6.4: nothing held yet: a gentle word instead of a bar of dashes */
        wx_append(out,cap,s->updating?(i%2u?"GETTING THE WEATHER...":"CONNECTING..."):
                                       (i%2u?"WHEN WI-FI CONNECTS":"WEATHER WILL APPEAR"));
        return;
    }
    switch(i%WX_TICKER_LINES) {
    case 0:
        wx_fmt(a,sizeof(a),o->temp_f,WX_DEG "F");
        if(o->feels_f!=WX_NA&&o->feels_kind) {
            const char *k=o->feels_kind==1?"WIND CHILL":"HEAT INDEX";
            wx_fmt(b,sizeof(b),o->feels_f,WX_DEG "F");
            wx_append(out,cap,"TEMP: ");wx_append(out,cap,a);wx_append(out,cap,"   ");wx_append(out,cap,k);wx_append(out,cap,": ");wx_append(out,cap,b);
            if(wx_text_w(out,WX_SILK)<=WX_TICKER_W)return;
            out[0]=0;wx_append(out,cap,"TEMP ");wx_append(out,cap,a);wx_append(out,cap,"  ");wx_append(out,cap,k);wx_append(out,cap," ");wx_append(out,cap,b);
            return;
        }
        wx_append(out,cap,"TEMP: ");wx_append(out,cap,a);
        if(o->sky[0]) {
            size_t keep=strlen(out);
            wx_append(out,cap,"   ");wx_append(out,cap,o->sky);
            if(wx_text_w(out,WX_SILK)>WX_TICKER_W)out[keep]=0;
        }
        return;
    case 1:
        wx_fmt(a,sizeof(a),o->humidity,"%");wx_fmt(b,sizeof(b),o->dew_f,WX_DEG "F");
        wx_append(out,cap,"HUMIDITY: ");wx_append(out,cap,a);wx_append(out,cap,"   DEW POINT: ");wx_append(out,cap,b);
        if(wx_text_w(out,WX_SILK)<=WX_TICKER_W)return;
        out[0]=0;wx_append(out,cap,"HUMIDITY ");wx_append(out,cap,a);wx_append(out,cap,"  DEW PT ");wx_append(out,cap,b);
        return;
    case 2:
        wx_pressure_text(o,a,sizeof(a));
        if(o->time_s&&s->trend&&s->trend<4) {
            wx_append(out,cap,"PRESSURE: ");wx_append(out,cap,a);wx_append(out,cap," ");wx_append(out,cap,trend[s->trend]);
            if(wx_text_w(out,WX_SILK)<=WX_TICKER_W)return;
            out[0]=0;
        }
        wx_append(out,cap,"BAROMETRIC PRESSURE: ");wx_append(out,cap,a);
        if(wx_text_w(out,WX_SILK)<=WX_TICKER_W)return;
        out[0]=0;wx_append(out,cap,"PRESSURE: ");wx_append(out,cap,a);
        return;
    case 3:
        wx_wind_text(o,a,sizeof(a));
        wx_append(out,cap,"WIND: ");wx_append(out,cap,a);
        if(o->gust_mph!=WX_NA&&o->gust_mph>0) {
            size_t keep=strlen(out);
            wx_append(out,cap,"   GUSTS TO ");wx_append_int(out,cap,o->gust_mph);
            if(wx_text_w(out,WX_SILK)>WX_TICKER_W)out[keep]=0;
        }
        return;
    default:
        wx_vis_text(o,a,sizeof(a));
        wx_append(out,cap,"VISIBILITY: ");wx_append(out,cap,a);
        if(o->ceiling!=WX_NA&&o->ceiling>0) {
            size_t keep=strlen(out);
            wx_append(out,cap,"   CEILING: ");wx_append_int(out,cap,o->ceiling*100);wx_append(out,cap," FT");
            if(wx_text_w(out,WX_SILK)>WX_TICKER_W)out[keep]=0;
        }
        return;
    }
}
#define WX_TICKER_HOLD_MS 4000u
#define WX_TICKER_ROLL_MS 300u
#define WX_TICKER_STEP_MS 100u   /* the bar moves at most ten times a second */
/* Where the bar is at `since_ms` after tuning in: a number that changes exactly when the picture
 * of the bar does (wx_tick redraws on a change). */
static inline uint32_t wx_ticker_phase(const wx_state *s,uint32_t since_ms) {
    if(s->alerts)return 0x80000000u|(since_ms/WX_TICKER_STEP_MS);
    uint32_t line=since_ms/WX_TICKER_HOLD_MS,in=since_ms%WX_TICKER_HOLD_MS;
    return line*4u+(line&&in<WX_TICKER_ROLL_MS?in/WX_TICKER_STEP_MS:3u);
}
static inline void wx_ticker(const wx_gc *g,const wx_state *s,uint32_t since_ms) {
    char a[64];
    wx_gc in=*g;
    int alert=s->alerts&&s->alert[0];
    wx_fill(g,0,132,159,143,alert?WX_RED:WX_NAVY);
    wx_fill(g,0,131,159,131,alert?WX_YELLOW:WX_ORANGE);
    wx_icon(g,wx_glyph,WX_GLYPH_W,WX_GLYPH_H,2,134,1,16);
    if(in.x0<16)in.x0=16;
    if(in.x1>159)in.x1=159;
    if(in.y0<133)in.y0=133;
    if(alert) { /* the headline crawls, 40 pixels a second in steps of 4 */
        int w=wx_text_w(s->alert,WX_SILK);
        if(w<=0)return;
        int x=16-(int)((since_ms/WX_TICKER_STEP_MS*4u)%(uint32_t)w);
        for(;x<159;x+=w)wx_glyphs(&in,x,136,s->alert,WX_SILK,1,WX_WHITE);
        return;
    }
    uint32_t phase=wx_ticker_phase(s,since_ms),line=phase/4u,step=phase%4u;
    wx_ticker_line(s,line,a,sizeof(a));
    if(step<3u) { /* the next line rolls up into place */
        int dy=step==0?11:step==1?7:4;
        wx_glyphs(&in,17,136+dy,a,WX_SILK,1,WX_WHITE);
        wx_ticker_line(s,line+WX_TICKER_LINES-1u,a,sizeof(a));
        wx_glyphs(&in,17,136-11+dy,a,WX_SILK,1,WX_WHITE);
    } else wx_glyphs(&in,17,136,a,WX_SILK,1,WX_WHITE);
}
#endif
