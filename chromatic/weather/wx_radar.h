/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: the radar picture. A small radar-only GIF (asked of the National Weather
 * Service map server already at the page's size, WX_RADAR_W x WX_RADAR_H, see-through where there
 * is no echo) is decoded WHILE it downloads, straight into a page of precipitation levels, 4 bits
 * a pixel (0 none, 1 light .. 5 heavy): WX_RADAR_BYTES bytes. Neither the file nor an RGB picture
 * is ever held.
 *
 * Memory, all given by the caller: the level page (kept while the picture is shown), and only
 * while a picture is arriving the decoder state (wx_gif, under 400 bytes) and the LZW string
 * table, WX_GIF_TABLE bytes: 4,096 entries of 3 bytes (12-bit prefix, 8-bit last colour). GIF
 * allows 12-bit codes and the sender chooses, so the table cannot be smaller. There is no output
 * stack: a string's length is found by walking its prefixes, then it is written backwards into
 * the page (two walks a code instead of a 4 KB stack).
 *
 * Colours are turned into levels as the colour table goes by (never stored), by hue, after the
 * PC reference's rule (render.py radar_levels) but for the usual reflectivity colours of a
 * radar-only layer: bright green 1, dark green 2, yellow 3, orange 4, red and magenta 5; cyan,
 * blue, grey, pale and dark colours 0. The cut at cyan and blue (under about 20 dBZ) is the
 * clear-air cut-off: it drops the faint returns round the radar on a dry day;
 * wx_radar_despeckle then removes light pixels standing alone. The real layer's colours have
 * not been seen by this code: the rule must be checked against a real reply.
 *
 * Refused cleanly, the page not touched: anything that is not a GIF, a picture of another size
 * than asked for, a bad code size. Stopped with the page marked not good: a code that cannot
 * exist, data that ends early. Nothing fed can make it write outside the page or the table.
 * Interlaced pictures, a local colour table and a see-through index are handled.
 * Portable C99, no allocation. Part of wx.h; include that.
 */
#ifndef WX_RADAR_INCLUDED
#define WX_RADAR_INCLUDED
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define WX_RADAR_W 160
#define WX_RADAR_H 97
#define WX_RADAR_BYTES ((WX_RADAR_W*WX_RADAR_H+1)/2)   /* 7,760 */
#define WX_GIF_TABLE (4096u*3u)                        /* 12,288 */
#define WX_RADAR_FRESH_S 2700u                         /* a picture older than 45 minutes is not "current" */

enum {WX_GIF_HEAD,WX_GIF_SCREEN,WX_GIF_COLOURS,WX_GIF_BLOCK,WX_GIF_EXT_LABEL,WX_GIF_EXT_SIZE,WX_GIF_EXT_DATA,WX_GIF_IMAGE,
      WX_GIF_CODE_SIZE,WX_GIF_DATA_SIZE,WX_GIF_DATA,WX_GIF_DONE,WX_GIF_BAD};
#define WX_GIF_NONE 0xffffu

typedef struct {
    uint8_t *table,*out;          /* WX_GIF_TABLE bytes; (w*h+1)/2 bytes */
    uint16_t w,h;
    uint32_t pos,total;           /* pixels written, pixels of the picture */
    uint32_t acc;                 /* code bits waiting */
    uint16_t need;                /* bytes of the present fixed part still to come */
    uint16_t colours,colour_at;   /* colour table: entries, next entry */
    uint16_t clear,next,prev;
    uint8_t state,after_colours,touched,interlaced,acc_bits,code_bits,min_bits;
    uint8_t have_clear,clear_index,sub_left,ext_label,ext_at,got;
    uint8_t part[10],rgb[3];
    uint8_t level[256];           /* colour index -> precipitation level */
} wx_gif;

/* A radar colour as a precipitation level 0..5. */
static inline uint8_t wx_radar_level(int r,int g,int b) {
    int mx=r>g?(r>b?r:b):(g>b?g:b),mn=r<g?(r<b?r:b):(g<b?g:b),d=mx-mn,hue,level=0;
    if(d<=0)return 0;
    if(mx==r){hue=60*(g-b)/d;if(hue<0)hue+=360;}
    else if(mx==g)hue=120+60*(b-r)/d;
    else hue=240+60*(r-g)/d;
    if(2*d>mx&&mx>120) { /* vivid colours only */
        if(hue>=80&&hue<165)level=mx>190?1:2;
        else if(hue>=50&&hue<80)level=3;
        else if(hue>=20&&hue<50&&r>200)level=4;
    }
    if(g<10&&b<10&&r>150)level=5;
    if(hue>=262&&hue<335&&100*d>12*mx&&r>110&&b>180)level=5;
    return (uint8_t)level;
}
static inline unsigned wx_radar_get(const uint8_t *page,unsigned index){return index&1u?page[index>>1]&15u:page[index>>1]>>4;}
static inline void wx_radar_put(uint8_t *page,unsigned index,unsigned level) {
    uint8_t *b=page+(index>>1);
    *b=(uint8_t)(index&1u?(*b&0xf0u)|(level&15u):(*b&0x0fu)|((level&15u)<<4));
}

static inline void wx_gif_begin(wx_gif *g,uint8_t *table,uint8_t *out,int w,int h) {
    memset(g,0,sizeof(*g));
    g->table=table;g->out=out;g->w=(uint16_t)w;g->h=(uint16_t)h;g->total=(uint32_t)w*(uint32_t)h;
    g->state=WX_GIF_HEAD;g->need=6;
    if(!table||!out||w<=0||h<=0||w>1024||h>1024)g->state=WX_GIF_BAD;
}
/* The whole picture has been written. */
static inline int wx_gif_ok(const wx_gif *g){return g->state==WX_GIF_DONE;}
/* The page has been written to (so whatever it held before is gone). */
static inline int wx_gif_touched(const wx_gif *g){return g->touched;}
/* Pixel `index` in the order sent -> its place in the picture (interlaced pictures send rows 0, 8, 16 ...,
 * then 4, 12 ..., then 2, 6 ..., then the odd rows). */
static inline uint32_t wx_gif_place(const wx_gif *g,uint32_t index) {
    if(!g->interlaced)return index;
    uint32_t row=index/g->w,x=index%g->w,h=g->h,n1=(h+7u)/8u,n2=(h+3u)/8u,n3=(h+1u)/4u;
    if(row<n1)row=row*8u;
    else if(row<n1+n2)row=(row-n1)*8u+4u;
    else if(row<n1+n2+n3)row=(row-n1-n2)*4u+2u;
    else row=(row-n1-n2-n3)*2u+1u;
    return row<h?row*g->w+x:g->total;
}
static inline void wx_gif_pixel(wx_gif *g,uint32_t index,unsigned colour) {
    if(index>=g->total)return;
    index=wx_gif_place(g,index);
    if(index<g->total)wx_radar_put(g->out,index,g->level[colour&255u]);
}
static inline unsigned wx_gif_prefix(const wx_gif *g,unsigned code){const uint8_t *e=g->table+code*3u;return (unsigned)e[0]|((unsigned)(e[1]&15u)<<8);}
/* One LZW code. 0 when the data is wrong. */
static inline int wx_gif_code(wx_gif *g,unsigned code) {
    unsigned end=g->clear+1u,first,len=1,c,last=0,extra=0;
    if(code==g->clear){g->code_bits=(uint8_t)(g->min_bits+1u);g->next=(uint16_t)(end+1u);g->prev=WX_GIF_NONE;return 1;}
    if(code==end)return 0; /* the end before the last pixel: not a whole picture */
    if(g->prev==WX_GIF_NONE) {
        if(code>=g->clear)return 0;
        wx_gif_pixel(g,g->pos++,code);g->prev=(uint16_t)code;
        return 1;
    }
    if(code<g->next)c=code;
    else if(code==g->next){c=g->prev;extra=1;} /* the string being defined: the last one plus its own first colour */
    else return 0;
    for(first=c;first>end;first=wx_gif_prefix(g,first))if(++len>4096u)return 0; /* a prefix is always a lower code */
    if(first>=g->clear)return 0;
    last=c;
    for(uint32_t at=g->pos+len-1u;last>end;last=wx_gif_prefix(g,last),at--)wx_gif_pixel(g,at,g->table[last*3u+2u]);
    wx_gif_pixel(g,g->pos,first);
    if(extra)wx_gif_pixel(g,g->pos+len,first);
    g->pos+=len+extra;
    if(g->next<4096u) {
        uint8_t *e=g->table+(unsigned)g->next*3u;
        e[0]=(uint8_t)(g->prev&255u);e[1]=(uint8_t)(g->prev>>8);e[2]=(uint8_t)first;
        g->next++;
        if(g->next==(1u<<g->code_bits)&&g->code_bits<12u)g->code_bits++;
    }
    g->prev=(uint16_t)code;
    return 1;
}
/* The next piece of the file, in any split. Returns 0 when no more is wanted: the picture is
 * whole (wx_gif_ok) or the file was refused. */
static inline int wx_gif_feed(wx_gif *g,const uint8_t *data,size_t size) {
    for(size_t i=0;i<size;i++) {
        uint8_t b=data[i];
        switch(g->state) {
        case WX_GIF_HEAD:
        case WX_GIF_SCREEN:
        case WX_GIF_IMAGE:
            g->part[g->got++]=b;
            if(--g->need)break;
            g->got=0;
            if(g->state==WX_GIF_HEAD) {
                if(memcmp(g->part,"GIF87a",6)&&memcmp(g->part,"GIF89a",6)){g->state=WX_GIF_BAD;return 0;}
                g->state=WX_GIF_SCREEN;g->need=7;
            } else if(g->state==WX_GIF_SCREEN) {
                g->state=WX_GIF_BLOCK;
                if(g->part[4]&0x80u){g->colours=(uint16_t)(2u<<(g->part[4]&7u));g->colour_at=0;g->state=WX_GIF_COLOURS;g->after_colours=WX_GIF_BLOCK;}
            } else { /* image descriptor: left, top, width, height, flags */
                unsigned left=g->part[0]|((unsigned)g->part[1]<<8),top=g->part[2]|((unsigned)g->part[3]<<8);
                unsigned w=g->part[4]|((unsigned)g->part[5]<<8),h=g->part[6]|((unsigned)g->part[7]<<8);
                if(left||top||w!=g->w||h!=g->h){g->state=WX_GIF_BAD;return 0;} /* not the picture asked for */
                g->interlaced=(uint8_t)((g->part[8]>>6)&1u);
                g->state=WX_GIF_CODE_SIZE;
                if(g->part[8]&0x80u){g->colours=(uint16_t)(2u<<(g->part[8]&7u));g->colour_at=0;g->state=WX_GIF_COLOURS;g->after_colours=WX_GIF_CODE_SIZE;}
            }
            break;
        case WX_GIF_COLOURS:
            g->rgb[g->got++]=b;
            if(g->got<3)break;
            g->got=0;
            if(g->colour_at<256u)g->level[g->colour_at]=wx_radar_level(g->rgb[0],g->rgb[1],g->rgb[2]);
            if(++g->colour_at>=g->colours)g->state=g->after_colours;
            break;
        case WX_GIF_BLOCK:
            if(b==0x2cu){g->state=WX_GIF_IMAGE;g->need=9;g->got=0;}
            else if(b==0x21u)g->state=WX_GIF_EXT_LABEL;
            else if(b==0x3bu){g->state=WX_GIF_BAD;return 0;} /* the end, and no picture came */
            else if(b){g->state=WX_GIF_BAD;return 0;}
            break;
        case WX_GIF_EXT_LABEL:
            g->ext_label=b;g->ext_at=0;g->state=WX_GIF_EXT_SIZE;
            break;
        case WX_GIF_EXT_SIZE:
            g->sub_left=b;
            g->state=(uint8_t)(b?WX_GIF_EXT_DATA:WX_GIF_BLOCK);
            break;
        case WX_GIF_EXT_DATA:
            /* graphic control: flags, delay (2), see-through index */
            if(g->ext_label==0xf9u&&g->ext_at<4u){if(g->ext_at==0)g->have_clear=(uint8_t)(b&1u);if(g->ext_at==3)g->clear_index=b;g->ext_at++;}
            if(!--g->sub_left)g->state=WX_GIF_EXT_SIZE;
            break;
        case WX_GIF_CODE_SIZE:
            if(b<2u||b>8u){g->state=WX_GIF_BAD;return 0;}
            if(g->have_clear)g->level[g->clear_index]=0;
            g->min_bits=b;g->clear=(uint16_t)(1u<<b);g->code_bits=(uint8_t)(b+1u);g->next=(uint16_t)(g->clear+2u);g->prev=WX_GIF_NONE;
            g->acc=0;g->acc_bits=0;g->pos=0;
            g->touched=1; /* from here the page is being written */
            memset(g->out,0,(g->total+1u)/2u);
            g->state=WX_GIF_DATA_SIZE;
            break;
        case WX_GIF_DATA_SIZE:
            if(!b){g->state=WX_GIF_BAD;return 0;} /* the data ended before the last pixel */
            g->sub_left=b;g->state=WX_GIF_DATA;
            break;
        case WX_GIF_DATA:
            g->acc|=(uint32_t)b<<g->acc_bits;g->acc_bits=(uint8_t)(g->acc_bits+8u);
            while(g->acc_bits>=g->code_bits) {
                unsigned code=g->acc&((1u<<g->code_bits)-1u);
                g->acc>>=g->code_bits;g->acc_bits=(uint8_t)(g->acc_bits-g->code_bits);
                if(!wx_gif_code(g,code)){g->state=WX_GIF_BAD;return 0;}
                if(g->pos>=g->total){g->state=WX_GIF_DONE;return 0;}
            }
            if(!--g->sub_left)g->state=WX_GIF_DATA_SIZE;
            break;
        default:
            return 0;
        }
    }
    return 1;
}
/* Light pixels (levels 1 and 2) with fewer than two neighbours showing anything are removed:
 * the speckle a radar shows in clear air. In place; two rows of working space on the stack. */
static inline int wx_radar_despeckle(uint8_t *page,int w,int h) {
    uint8_t above[WX_RADAR_W],here[WX_RADAR_W];
    int removed=0;
    if(w<=0||w>WX_RADAR_W||h<=0)return 0;
    memset(above,0,sizeof(above));
    for(int y=0;y<h;y++) {
        for(int x=0;x<w;x++)here[x]=(uint8_t)wx_radar_get(page,(unsigned)(y*w+x));
        for(int x=0;x<w;x++) {
            if(here[x]!=1u&&here[x]!=2u)continue;
            int n=0;
            for(int dx=-1;dx<=1;dx++) {
                if(x+dx<0||x+dx>=w)continue;
                n+=above[x+dx]!=0;
                if(dx)n+=here[x+dx]!=0;
                if(y+1<h)n+=wx_radar_get(page,(unsigned)((y+1)*w+x+dx))!=0;
            }
            if(n<2){wx_radar_put(page,(unsigned)(y*w+x),0);removed++;}
        }
        memcpy(above,here,sizeof(above));
    }
    return removed;
}
#endif
