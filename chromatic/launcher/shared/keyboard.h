/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * On-screen keyboard for the Chromatic's eight buttons, in the launcher's own pixel style.
 * D-pad moves over a grid, A types the key, B deletes (B on an empty field cancels), Start
 * confirms, Select switches the page (abc / ABC / #+=). Every printable ASCII character
 * (0x20..0x7E) is reachable, so any Wi-Fi password or server address can be typed.
 *
 * Secret fields show dots. The show/hide key reveals the text, but never while Remote Pilot
 * is connected (`watched`): the screen action would carry the password off the console. */
#ifndef PJ_KEYBOARD_H
#define PJ_KEYBOARD_H
#include <stdint.h>
#include <string.h>
#include "launcher_logic.h"
#define PJ_KB_COLS 11u
#define PJ_KB_ROWS 4u
#define PJ_KB_PAGES 3u
#define PJ_KB_SPECIALS 5u
#define PJ_KB_TEXT_MAX 128u
enum {PJ_KB_NONE,PJ_KB_REDRAW,PJ_KB_DONE,PJ_KB_CANCEL};
enum {PJ_KB_SP_PAGE,PJ_KB_SP_SPACE,PJ_KB_SP_SHOW,PJ_KB_SP_DELETE,PJ_KB_SP_OK};
/* Rows of at most 11 keys; a shorter row leaves blank keys. Together: all 95 printable ASCII. */
static const char *const pj_kb_pages[PJ_KB_PAGES][PJ_KB_ROWS]={
    {"1234567890-","abcdefghijk","lmnopqrstuv","wxyz._@:/+!"},
    {"1234567890_","ABCDEFGHIJK","LMNOPQRSTUV","WXYZ,;#&*=?"},
    {"!\"#$%&'()*+",",-./:;<=>?@","[\\]^_`{|}~","1234567890"}};
static const char *const pj_kb_page_names[PJ_KB_PAGES]={"abc","ABC","#+="};
typedef struct {
    char text[PJ_KB_TEXT_MAX+1];unsigned len,cap,min;
    uint8_t secret,show,page,row,col,last_col; /* row PJ_KB_ROWS is the special row, col 0..4 */
    char title[28];
} pj_keyboard;
static inline void pj_kb_open(pj_keyboard *k,const char *title,const char *initial,unsigned cap,unsigned min,int secret) {
    memset(k,0,sizeof(*k));
    if(cap>PJ_KB_TEXT_MAX)cap=PJ_KB_TEXT_MAX;
    k->cap=cap;k->min=min;k->secret=(uint8_t)(secret!=0);
    snprintf(k->title,sizeof(k->title),"%s",title?title:"");
    if(initial){snprintf(k->text,sizeof(k->text),"%.*s",(int)cap,initial);k->len=(unsigned)strlen(k->text);}
    k->row=1;k->col=0;k->last_col=0;
}
static inline char pj_kb_char_at(const pj_keyboard *k,unsigned row,unsigned col) {
    if(row>=PJ_KB_ROWS||col>=PJ_KB_COLS)return 0;
    const char *r=pj_kb_pages[k->page][row];
    return col<strlen(r)?r[col]:0;
}
static inline int pj_kb_type(pj_keyboard *k,char c) {
    if(k->len>=k->cap)return PJ_KB_REDRAW; /* full: nothing happens, no loss */
    k->text[k->len++]=c;k->text[k->len]=0;return PJ_KB_REDRAW;
}
static inline int pj_kb_delete(pj_keyboard *k) {
    if(!k->len)return PJ_KB_CANCEL;
    k->text[--k->len]=0;return PJ_KB_REDRAW;
}
static inline int pj_kb_press(pj_keyboard *k,uint16_t pressed) {
    if(!pressed)return PJ_KB_NONE;
    if(pressed&PJ_BTN_START)return k->len>=k->min?PJ_KB_DONE:PJ_KB_REDRAW;
    if(pressed&PJ_BTN_B)return pj_kb_delete(k);
    if(pressed&PJ_BTN_SELECT){k->page=(uint8_t)((k->page+1u)%PJ_KB_PAGES);return PJ_KB_REDRAW;}
    if(pressed&PJ_BTN_A) {
        if(k->row<PJ_KB_ROWS){char c=pj_kb_char_at(k,k->row,k->col);return c?pj_kb_type(k,c):PJ_KB_REDRAW;}
        switch(k->col) {
        case PJ_KB_SP_PAGE:k->page=(uint8_t)((k->page+1u)%PJ_KB_PAGES);return PJ_KB_REDRAW;
        case PJ_KB_SP_SPACE:return pj_kb_type(k,' ');
        case PJ_KB_SP_SHOW:if(k->secret)k->show=!k->show;else{k->len=0;k->text[0]=0;}return PJ_KB_REDRAW;
        case PJ_KB_SP_DELETE:return k->len?pj_kb_delete(k):PJ_KB_REDRAW;
        default:return k->len>=k->min?PJ_KB_DONE:PJ_KB_REDRAW;
        }
    }
    /* Movement wraps; the special row has 5 wide keys, so the column maps across. */
    if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN)) {
        unsigned rows=PJ_KB_ROWS+1u;
        unsigned next=(pressed&PJ_BTN_UP)?(k->row+rows-1u)%rows:(k->row+1u)%rows;
        if(next==PJ_KB_ROWS&&k->row<PJ_KB_ROWS){k->last_col=k->col;k->col=(uint8_t)(k->col*PJ_KB_SPECIALS/PJ_KB_COLS);}
        else if(k->row==PJ_KB_ROWS&&next<PJ_KB_ROWS)k->col=k->last_col;
        k->row=(uint8_t)next;return PJ_KB_REDRAW;
    }
    unsigned cols=k->row==PJ_KB_ROWS?PJ_KB_SPECIALS:PJ_KB_COLS;
    if(pressed&PJ_BTN_LEFT){k->col=(uint8_t)((k->col+cols-1u)%cols);return PJ_KB_REDRAW;}
    if(pressed&PJ_BTN_RIGHT){k->col=(uint8_t)((k->col+1u)%cols);return PJ_KB_REDRAW;}
    return PJ_KB_NONE;
}
/* What the field shows: the text, or one dot per character. `watched` keeps a secret hidden. */
static inline int pj_kb_revealed(const pj_keyboard *k,int watched){return !k->secret||(k->show&&!watched);}
#define PJ_KB_X0 3
#define PJ_KB_KEY_W 13
#define PJ_KB_KEY_H 12
#define PJ_KB_PITCH 14
#define PJ_KB_Y0 42
static inline void pj_kb_key(uint16_t *fb,int x,int y,int w,const char *label,int focused,unsigned accent,
                             const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_rect(fb,x,y,w,PJ_KB_KEY_H,focused?accent:PJ_UI_EDGE);
    pj_osd_rect(fb,x+1,y+1,w-2,PJ_KB_KEY_H-2,focused?pj_osd_mix(accent,0x101016u,72u):PJ_UI_ROW);
    int tw=pj_osd_measure(label,advance);
    pj_osd_text(fb,x+(w-tw)/2,y+1,label,focused?0xffffffu:0xe0e0e0u,alpha,advance);
}
static inline void pj_kb_draw(uint16_t *fb,const pj_keyboard *k,unsigned accent,int watched,
                              const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_osd_text(fb,8,4,k->title,accent,alpha,advance);
    /* The field: a box with the text's tail, or dots. */
    pj_osd_rect(fb,6,21,148,17,PJ_UI_EDGE);pj_osd_rect(fb,7,22,146,15,0x0c0c12u);
    if(pj_kb_revealed(k,watched)) {
        const char *shown=k->text;
        while(*shown&&pj_osd_measure(shown,advance)>134)shown++;
        pj_osd_text(fb,10,24,shown,0xffffffu,alpha,advance);
        pj_osd_rect(fb,10+pj_osd_measure(shown,advance)+1,25,2,9,accent);
    } else {
        unsigned dots=k->len>28u?28u:k->len;
        for(unsigned i=0;i<dots;i++)pj_osd_rect(fb,10+(int)i*5,28,3,3,0xffffffu);
        pj_osd_rect(fb,10+(int)dots*5+1,25,2,9,accent);
    }
    for(unsigned row=0;row<PJ_KB_ROWS;row++)for(unsigned col=0;col<PJ_KB_COLS;col++) {
        char c=pj_kb_char_at(k,row,col);char label[2]={c,0};
        int x=PJ_KB_X0+(int)col*PJ_KB_PITCH,y=PJ_KB_Y0+(int)row*PJ_KB_PITCH;
        if(!c){pj_osd_rect(fb,x,y,PJ_KB_KEY_W,PJ_KB_KEY_H,0x0e0e13u);continue;}
        pj_kb_key(fb,x,y,PJ_KB_KEY_W,label,k->row==row&&k->col==col,accent,alpha,advance);
    }
    /* Special row: page | space | show/hide (or clear) | del | OK */
    {
        int y=PJ_KB_Y0+(int)PJ_KB_ROWS*PJ_KB_PITCH;
        const char *labels[PJ_KB_SPECIALS]={pj_kb_page_names[(k->page+1u)%PJ_KB_PAGES],"space",
            k->secret?(k->show?"hide":"show"):"clear","del","OK"};
        const int widths[PJ_KB_SPECIALS]={26,46,26,26,26};int x=PJ_KB_X0;
        for(unsigned i=0;i<PJ_KB_SPECIALS;i++) {
            pj_kb_key(fb,x,y,widths[i],labels[i],k->row==PJ_KB_ROWS&&k->col==i,accent,alpha,advance);
            x+=widths[i]+1;
        }
    }
    if(k->secret&&k->show&&watched)pj_osd_text(fb,8,116,"Hidden: Remote Pilot connected",PJ_UI_NOTE,alpha,advance);
    else pj_osd_text(fb,8,116,"A: type  B: delete  Start: OK",PJ_UI_MUTED,alpha,advance);
    pj_osd_text(fb,8,130,"Select: abc / ABC / #+=",PJ_UI_MUTED,alpha,advance);
}
#endif
