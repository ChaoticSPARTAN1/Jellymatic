/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: a streaming JSON scanner for National Weather Service replies.
 *
 * shared/bounded_json.h tokenizes a whole reply held in memory; the replies here are 4 to 150 KB
 * and the console has no room for one, so this scanner takes the reply a byte at a time and
 * keeps only: the nesting depth, which of a short list of known keys leads to the present value
 * (for the first WX_JS_DEPTH levels), and the present scalar value, cut to WX_JS_VAL characters.
 * It reports three events: a scalar ended, a container opened, a container is closing. It does
 * not validate: anything that is not JSON simply never matches a wanted path. Nothing it is fed
 * can make it write outside its own struct (every index is checked against a fixed bound), and
 * the depth counter saturates.
 *
 * Strings: escapes are resolved to plain ASCII (\n, \r, \t become a space; ° and other
 * non-ASCII characters are dropped); bytes of 0x80 and above are dropped.
 * Portable C99, no allocation, no I/O. Part of wx.h; include that.
 */
#ifndef WX_JSON_H
#define WX_JSON_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define WX_JS_DEPTH 10   /* levels whose key is remembered (the replies need 6) */
#define WX_JS_VAL 256    /* longest scalar kept, with its terminator */
#define WX_JS_KEYLEN 28  /* longest known key ("probabilityOfPrecipitation") plus terminator and one spare */

/* known keys; WX_KEY_ELEM marks "an element of an array" */
enum {WX_KEY_NONE,WX_KEY_ELEM,WX_KEY_PROPERTIES,WX_KEY_PERIODS,WX_KEY_FEATURES,WX_KEY_TIMESTAMP,WX_KEY_TEXT,
      WX_KEY_TEMPERATURE,WX_KEY_DEWPOINT,WX_KEY_WINDDIR,WX_KEY_WINDSPEED,WX_KEY_WINDGUST,WX_KEY_PRESSURE,
      WX_KEY_VISIBILITY,WX_KEY_HUMIDITY,WX_KEY_WINDCHILL,WX_KEY_HEATINDEX,WX_KEY_CLOUDS,WX_KEY_BASE,WX_KEY_AMOUNT,
      WX_KEY_VALUE,WX_KEY_NAME,WX_KEY_START,WX_KEY_END,WX_KEY_ISDAY,WX_KEY_POP,WX_KEY_SHORT,WX_KEY_DETAILED,
      WX_KEY_EVENT,WX_KEY_HEADLINE,WX_KEY_STATUS,WX_KEY_MSGTYPE,WX_KEY_COUNT};
static const char *const wx_js_keys[WX_KEY_COUNT]={
    "","","properties","periods","features","timestamp","textDescription","temperature","dewpoint","windDirection",
    "windSpeed","windGust","barometricPressure","visibility","relativeHumidity","windChill","heatIndex","cloudLayers",
    "base","amount","value","name","startTime","endTime","isDaytime","probabilityOfPrecipitation","shortForecast",
    "detailedForecast","event","headline","status","messageType"};

enum {WX_EV_NONE,WX_EV_SCALAR,WX_EV_OPEN,WX_EV_CLOSE};
enum {WX_JS_BETWEEN,WX_JS_INKEY,WX_JS_INSTR,WX_JS_INLIT};

typedef struct {
    uint32_t objects;             /* bit d: the container at depth d (1..31) is an object */
    uint16_t depth;               /* containers open now; saturates at 65535 */
    uint16_t vlen;                /* characters in val */
    uint8_t state,esc,want_key,klen;
    uint8_t is_string;            /* the scalar just reported was a string (else a number, true, false or null) */
    uint8_t opened;               /* a container has been opened at all */
    uint8_t key[WX_JS_DEPTH];     /* key[d]: the known key (or ELEM) being read inside the container at depth d */
    uint16_t ucode;               /* \uXXXX being read */
    char kbuf[WX_JS_KEYLEN];
    char val[WX_JS_VAL];
} wx_json;

static inline void wx_json_init(wx_json *j){memset(j,0,sizeof(*j));}
/* The whole document has ended: its outermost container was opened and closed. */
static inline int wx_json_complete(const wx_json *j){return j->opened&&j->depth==0&&j->state==WX_JS_BETWEEN;}
/* The key leading to the present value, `up` levels above it (0 = its own key). */
static inline unsigned wx_json_key(const wx_json *j,unsigned up) {
    if(j->depth<up+1u||j->depth-up>=WX_JS_DEPTH)return WX_KEY_NONE;
    return j->key[j->depth-up];
}
static inline void wx_json_putc(wx_json *j,char c){if(j->vlen<WX_JS_VAL-1u)j->val[j->vlen++]=c;}
static inline int wx_json_in_object(const wx_json *j){return j->depth>0&&j->depth<32&&((j->objects>>j->depth)&1u);}

/* One byte in. Returns an event. *again is set when the same byte must be fed once more (a number
 * ended at a bracket or comma: the scalar is reported first). On WX_EV_SCALAR the text is j->val
 * (terminated), j->vlen long; on WX_EV_OPEN the new container is at j->depth; on WX_EV_CLOSE the
 * container at j->depth is about to close (j->depth drops on the next call). */
static inline int wx_json_byte(wx_json *j,uint8_t b,int *again) {
    *again=0;
    switch(j->state) {
    case WX_JS_INKEY:
    case WX_JS_INSTR: {
        int key=j->state==WX_JS_INKEY;
        if(j->esc==1) {
            j->esc=0;
            if(b=='u'){j->esc=2;j->ucode=0;return WX_EV_NONE;}
            char c=(b=='n'||b=='r'||b=='t'||b=='b'||b=='f')?' ':(char)b;
            if(b>=0x80)return WX_EV_NONE;
            if(key){if(j->klen<WX_JS_KEYLEN-1u)j->kbuf[j->klen++]=c;else j->klen=WX_JS_KEYLEN;}
            else wx_json_putc(j,c);
            return WX_EV_NONE;
        }
        if(j->esc>=2) {
            unsigned u=b,h=u>='0'&&u<='9'?u-'0':u>='a'&&u<='f'?u-'a'+10u:u>='A'&&u<='F'?u-'A'+10u:0u;
            j->ucode=(uint16_t)((j->ucode<<4)|h);
            if(++j->esc==6) {
                j->esc=0;
                if(j->ucode>=0x20&&j->ucode<0x7f&&!key)wx_json_putc(j,(char)j->ucode);
                else if(j->ucode==0x2019&&!key)wx_json_putc(j,'\''); /* a typographic apostrophe */
            }
            return WX_EV_NONE;
        }
        if(b=='\\'){j->esc=1;return WX_EV_NONE;}
        if(b=='"') {
            j->state=WX_JS_BETWEEN;
            if(key) {
                unsigned id=WX_KEY_NONE;
                if(j->klen<WX_JS_KEYLEN) {
                    j->kbuf[j->klen]=0;
                    for(unsigned i=2;i<WX_KEY_COUNT;i++)if(!strcmp(j->kbuf,wx_js_keys[i])){id=i;break;}
                }
                if(j->depth<WX_JS_DEPTH)j->key[j->depth]=(uint8_t)id;
                return WX_EV_NONE;
            }
            j->val[j->vlen]=0;j->is_string=1;
            return WX_EV_SCALAR;
        }
        if(b<0x20||b>=0x7f)return WX_EV_NONE; /* control and non-ASCII bytes are dropped */
        if(key){if(j->klen<WX_JS_KEYLEN-1u)j->kbuf[j->klen++]=(char)b;else j->klen=WX_JS_KEYLEN;}
        else wx_json_putc(j,(char)b);
        return WX_EV_NONE;
    }
    case WX_JS_INLIT:
        if(b==','||b=='}'||b==']'||b==' '||b=='\n'||b=='\r'||b=='\t'||b==':'||b=='"'||b=='{'||b=='[') {
            j->state=WX_JS_BETWEEN;j->val[j->vlen]=0;j->is_string=0;*again=1;
            return WX_EV_SCALAR;
        }
        if(b>=0x21&&b<0x7f)wx_json_putc(j,(char)b);
        return WX_EV_NONE;
    default:
        break;
    }
    switch(b) {
    case ' ':case '\n':case '\r':case '\t':return WX_EV_NONE;
    case '{':case '[':
        if(j->depth<65535u)j->depth++;
        if(j->depth<32) {
            if(b=='{')j->objects|=1u<<j->depth;else j->objects&=~(1u<<j->depth);
        }
        if(j->depth<WX_JS_DEPTH)j->key[j->depth]=(uint8_t)(b=='['?WX_KEY_ELEM:WX_KEY_NONE);
        j->want_key=(uint8_t)(b=='{');j->opened=1;
        return WX_EV_OPEN;
    case '}':case ']':
        j->want_key=0;
        return WX_EV_CLOSE; /* the caller calls wx_json_closed() after handling it */
    case ',':
        j->want_key=(uint8_t)wx_json_in_object(j);
        return WX_EV_NONE;
    case ':':
        j->want_key=0;
        return WX_EV_NONE;
    case '"':
        j->esc=0;
        if(j->want_key&&wx_json_in_object(j)){j->state=WX_JS_INKEY;j->klen=0;}
        else{j->state=WX_JS_INSTR;j->vlen=0;}
        return WX_EV_NONE;
    default:
        if(b<0x21||b>=0x7f)return WX_EV_NONE;
        j->state=WX_JS_INLIT;j->vlen=0;wx_json_putc(j,(char)b);
        return WX_EV_NONE;
    }
}
/* After a WX_EV_CLOSE was handled. */
static inline void wx_json_closed(wx_json *j){if(j->depth)j->depth--;}
#endif
