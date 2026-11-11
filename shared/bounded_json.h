/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Read-only bounded JSON tokens. Caller owns bytes/tokens; no allocation. */
#ifndef PJ_BOUNDED_JSON_H
#define PJ_BOUNDED_JSON_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
enum {PJ_JSON_OBJECT=1,PJ_JSON_ARRAY,PJ_JSON_STRING,PJ_JSON_NUMBER,PJ_JSON_LITERAL};
typedef struct {uint16_t start,end,next;uint8_t type;} pj_json_token;
typedef struct {
    const uint8_t *data;size_t size,pos;pj_json_token *tokens;unsigned count,capacity;
} pj_json;
static inline void pj_json_space(pj_json *j) {
    while(j->pos<j->size&&strchr(" \t\r\n",j->data[j->pos])&&j->data[j->pos])j->pos++;
}
static inline int pj_json_hex(unsigned c) {
    if(c>='0'&&c<='9')return (int)c-'0';
    if(c>='a'&&c<='f')return (int)c-'a'+10;
    if(c>='A'&&c<='F')return (int)c-'A'+10;
    return -1;
}
static inline int pj_json_value(pj_json *j,unsigned depth) {
    pj_json_space(j);
    if(depth>16||j->pos>=j->size||j->count>=j->capacity)return -1;
    unsigned index=j->count++;pj_json_token *t=&j->tokens[index];
    t->start=(uint16_t)j->pos;t->type=0;
    unsigned c=j->data[j->pos++];
    if(c=='{'||c=='[') {
        t->type=c=='{'?PJ_JSON_OBJECT:PJ_JSON_ARRAY;
        unsigned close=c=='{'?'}':']';pj_json_space(j);
        if(j->pos<j->size&&j->data[j->pos]==close)j->pos++;
        else for(;;) {
            if(c=='{') {
                int key=pj_json_value(j,depth+1);
                if(key<0||j->tokens[key].type!=PJ_JSON_STRING)return -1;
                pj_json_space(j);
                if(j->pos>=j->size||j->data[j->pos++]!=':')return -1;
            }
            if(pj_json_value(j,depth+1)<0)return -1;
            pj_json_space(j);
            if(j->pos>=j->size)return -1;
            unsigned end=j->data[j->pos++];if(end==close)break;
            if(end!=',')return -1;
        }
    } else if(c=='"') {
        t->type=PJ_JSON_STRING;int closed=0;
        while(j->pos<j->size) {
            c=j->data[j->pos++];if(c=='"'){closed=1;break;}
            if(c<32)return -1;
            if(c=='\\') {
                if(j->pos>=j->size)return -1;
                c=j->data[j->pos++];
                if(c=='u') {
                    if(j->size-j->pos<4)return -1;
                    for(unsigned k=0;k<4;k++)if(pj_json_hex(j->data[j->pos++])<0)return -1;
                } else if(!c||!strchr("\"\\/bfnrt",c))return -1;
            }
        }
        if(!closed)return -1;
    } else if(c=='-'||(c>='0'&&c<='9')) {
        t->type=PJ_JSON_NUMBER;size_t p=t->start;
        if(j->data[p]=='-')p++;
        if(p>=j->size)return -1;
        if(j->data[p]=='0')p++;
        else {
            if(j->data[p]<'1'||j->data[p]>'9')return -1;
            while(p<j->size&&j->data[p]>='0'&&j->data[p]<='9')p++;
        }
        if(p<j->size&&j->data[p]=='.') {
            size_t first=++p;while(p<j->size&&j->data[p]>='0'&&j->data[p]<='9')p++;
            if(p==first)return -1;
        }
        if(p<j->size&&(j->data[p]=='e'||j->data[p]=='E')) {
            p++;if(p<j->size&&(j->data[p]=='+'||j->data[p]=='-'))p++;
            size_t first=p;while(p<j->size&&j->data[p]>='0'&&j->data[p]<='9')p++;
            if(p==first)return -1;
        }
        j->pos=p;
    } else {
        const char *word=c=='t'?"true":c=='f'?"false":c=='n'?"null":"";
        size_t n=strlen(word);if(!n||j->size-t->start<n||memcmp(j->data+t->start,word,n))return -1;
        t->type=PJ_JSON_LITERAL;j->pos=t->start+n;
    }
    t->end=(uint16_t)j->pos;t->next=(uint16_t)j->count;return (int)index;
}
static inline int pj_json_parse(pj_json *j,const void *data,size_t size,pj_json_token *tokens,unsigned capacity) {
    if(!j||!data||!tokens||!size||size>32768||!capacity||capacity>65534)return 0;
    j->data=data;j->size=size;j->pos=0;j->tokens=tokens;j->count=0;j->capacity=capacity;
    if(pj_json_value(j,0)!=0)return 0;
    pj_json_space(j);return j->pos==j->size;
}
static inline int pj_json_find(const pj_json *j,int object,const char *key) {
    if(object<0||(unsigned)object>=j->count||j->tokens[object].type!=PJ_JSON_OBJECT)return -1;
    for(unsigned i=(unsigned)object+1;i<j->tokens[object].next;) {
        const pj_json_token *t=&j->tokens[i];unsigned value=t->next;
        if(value>=j->count)return -1;
        if(t->type==PJ_JSON_STRING&&(size_t)(t->end-t->start)==strlen(key)+2&&
           !memcmp(j->data+t->start+1,key,strlen(key)))return (int)value;
        i=j->tokens[value].next;
    }
    return -1;
}
static inline int pj_json_u64(const pj_json *j,int token,uint64_t *out) {
    if(token<0||(unsigned)token>=j->count||j->tokens[token].type!=PJ_JSON_NUMBER)return 0;
    const pj_json_token *t=&j->tokens[token];uint64_t n=0;
    for(unsigned i=t->start;i<t->end;i++) {
        unsigned c=j->data[i];if(c<'0'||c>'9'||n>(UINT64_MAX-(c-'0'))/10)return 0;
        n=n*10+c-'0';
    }
    *out=n;return 1;
}
static inline int pj_json_true(const pj_json *j,int token) {
    return token>=0&&(unsigned)token<j->count&&j->tokens[token].type==PJ_JSON_LITERAL&&
        j->tokens[token].end-j->tokens[token].start==4&&!memcmp(j->data+j->tokens[token].start,"true",4);
}
/* Decode display text to UTF-8, replacing control characters. Truncate only at
 * a codepoint boundary. Existing bitmap font still covers ASCII only. */
static inline int pj_json_text(const pj_json *j,int token,char *out,size_t capacity) {
    if(!capacity)return 0;
    out[0]=0;
    if(token<0||(unsigned)token>=j->count||j->tokens[token].type!=PJ_JSON_STRING)return 0;
    const pj_json_token *t=&j->tokens[token];size_t used=0;unsigned p=t->start+1,end=t->end-1;
    while(p<end) {
        uint32_t cp=j->data[p++];unsigned count=1;uint8_t encoded[4];
        if(cp=='\\') {
            cp=j->data[p++];
            if(cp=='u') {
                cp=0;for(unsigned k=0;k<4;k++)cp=cp*16+(unsigned)pj_json_hex(j->data[p++]);
                if(cp>=0xd800&&cp<=0xdbff) {
                    if(end-p<6||j->data[p]!='\\'||j->data[p+1]!='u')return 0;
                    p+=2;uint32_t low=0;
                    for(unsigned k=0;k<4;k++)low=low*16+(unsigned)pj_json_hex(j->data[p++]);
                    if(low<0xdc00||low>0xdfff)return 0;
                    cp=0x10000+((cp-0xd800)<<10)+low-0xdc00;
                } else if(cp>=0xdc00&&cp<=0xdfff)return 0;
            } else if(cp=='b'||cp=='f'||cp=='n'||cp=='r'||cp=='t')cp=' ';
        } else if(cp>=128) {
            unsigned extra=cp>=0xc2&&cp<=0xdf?1:cp>=0xe0&&cp<=0xef?2:cp>=0xf0&&cp<=0xf4?3:99;
            if(extra>3||extra>end-p)return 0;
            uint32_t minimum=extra==1?128:extra==2?2048:65536;cp&=(1u<<(6-extra))-1;
            for(unsigned k=0;k<extra;k++) {
                unsigned c=j->data[p++];if((c&0xc0)!=0x80)return 0;cp=(cp<<6)|(c&63);
            }
            if(cp<minimum||cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff))return 0;
        }
        if(cp<32||cp==127)cp=' ';
        if(cp<128)encoded[0]=(uint8_t)cp;
        else if(cp<2048){count=2;encoded[0]=(uint8_t)(0xc0|(cp>>6));encoded[1]=(uint8_t)(0x80|(cp&63));}
        else if(cp<65536){count=3;encoded[0]=(uint8_t)(0xe0|(cp>>12));encoded[1]=(uint8_t)(0x80|((cp>>6)&63));encoded[2]=(uint8_t)(0x80|(cp&63));}
        else {count=4;encoded[0]=(uint8_t)(0xf0|(cp>>18));encoded[1]=(uint8_t)(0x80|((cp>>12)&63));encoded[2]=(uint8_t)(0x80|((cp>>6)&63));encoded[3]=(uint8_t)(0x80|(cp&63));}
        if(used+count>=capacity)break;
        memcpy(out+used,encoded,count);used+=count;
    }
    out[used]=0;return 1;
}
#endif
