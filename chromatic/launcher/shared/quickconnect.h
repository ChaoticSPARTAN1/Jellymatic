/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Jellyfin on the console without a PC: server discovery and Quick Connect sign-in, as a
 * protocol state machine with no sockets (setup_esp32.c moves the bytes; the host test feeds
 * canned replies). Same requests as scripts/chromatic_jellyfin_link.py makes from the PC.
 *
 * Discovery: a UDP datagram "who is JellyfinServer?" to the broadcast address, port 7359;
 * every server answers {"Address":"http://host:8096","Id":"...","Name":"..."}.
 * Quick Connect: GET /QuickConnect/Enabled -> POST /QuickConnect/Initiate (GET on servers
 * before 10.9) gives Code and Secret -> the console shows the code, the owner approves it in
 * any of their Jellyfin apps -> GET /QuickConnect/Connect?secret=.. until Authenticated ->
 * POST /Users/AuthenticateWithQuickConnect {"Secret":..} gives the console's own AccessToken.
 * Forget: POST /Sessions/Logout as this device with its token: only this console's session.
 *
 * Secrets (Secret, AccessToken) live in this struct only until the record is written, then are
 * wiped; nothing here formats them into any text but the request itself. */
#ifndef PJ_QUICKCONNECT_H
#define PJ_QUICKCONNECT_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "provision.h"
#include "../../../shared/bounded_json.h"
#define PJ_DISCOVERY_PORT 7359u
#define PJ_DISCOVERY_MESSAGE "who is JellyfinServer?"
#define PJ_DISCOVERY_MAX 4u
#define PJ_QC_POLL_MS 2000u
#define PJ_QC_DEADLINE_MS 300000u /* a Quick Connect code lives about 5 minutes on the server */
#define PJ_QC_JSON_TOKENS 512u
#define PJ_QC_BODY_MAX 8192u
#define PJ_JF_CLIENT "Chromatic Jellyfin"
#define PJ_JF_DEVICE "Chromatic"
typedef struct {char url[129],name[32],id[33];} pj_jf_server; /* id: the reply's server Id, 32 lower-case hex, or "" */
typedef struct {pj_jf_server server[PJ_DISCOVERY_MAX];unsigned count,heard;} pj_jf_servers;
static inline int pj_qc_json(pj_json *j,pj_json_token *tokens,const void *body,size_t len) {
    return body&&len&&len<=PJ_QC_BODY_MAX&&pj_json_parse(j,body,len,tokens,PJ_QC_JSON_TOKENS)&&j->count&&j->tokens[0].type==PJ_JSON_OBJECT;
}
/* Lower-cases the scheme, adds http:// when no scheme was typed, drops a trailing '/'.
 * 1 when the result is a URL the record accepts. */
static inline int pj_qc_normalize_url(char *url,size_t size) {
    size_t n=strlen(url);
    while(n&&(url[n-1]=='/'||url[n-1]==' '))url[--n]=0;
    while(url[0]==' ')memmove(url,url+1,n--);
    if(!n)return 0;
    char *colon=strstr(url,"://");
    if(colon){for(char *p=url;p<colon;p++)if(*p>='A'&&*p<='Z')*p=(char)(*p+32);}
    else {
        if(n+7>=size)return 0;
        memmove(url+7,url,n+1);memcpy(url,"http://",7);
    }
    return strlen(url)<=128&&pj_prov_url(url);
}
/* Display text for a server name: ASCII the font has, cut to the field. */
static inline void pj_qc_display(char *out,size_t size,const char *in) {
    size_t n=0;
    for(;*in&&n+1<size;in++){unsigned char c=(unsigned char)*in;if(c>=0x20&&c<0x7f)out[n++]=(char)c;else if(c>=0x80&&(c&0xc0)!=0x80&&n+1<size)out[n++]='?';}
    out[n]=0;
}
/* One discovery reply. Duplicate addresses merge; at most PJ_DISCOVERY_MAX servers kept. */
static inline int pj_discovery_add(pj_jf_servers *s,const void *body,size_t len) {
    pj_json j;pj_json_token tokens[64];
    s->heard++;
    if(!body||!len||len>2048||!pj_json_parse(&j,body,len,tokens,64)||j.tokens[0].type!=PJ_JSON_OBJECT)return 0;
    pj_jf_server server;memset(&server,0,sizeof(server));
    char raw_name[64]={0};
    if(!pj_json_text(&j,pj_json_find(&j,0,"Address"),server.url,sizeof(server.url)))return 0;
    pj_json_text(&j,pj_json_find(&j,0,"Name"),raw_name,sizeof(raw_name));
    if(!pj_qc_normalize_url(server.url,sizeof(server.url)))return 0;
    pj_qc_display(server.name,sizeof(server.name),raw_name);
    {char raw_id[48]={0};unsigned n=0; /* 1.3.1: the server's Id, normalised as Jellyfin user ids are (no dashes, lower case) */
     if(pj_json_text(&j,pj_json_find(&j,0,"Id"),raw_id,sizeof(raw_id)))
         for(const char *p=raw_id;*p&&n<32;p++){char c=*p;if(c=='-')continue;if(c>='A'&&c<='F')c=(char)(c+32);if(!((c>='0'&&c<='9')||(c>='a'&&c<='f'))){n=0;break;}server.id[n++]=c;}
     server.id[n==32?32:0]=0;if(n!=32)server.id[0]=0;}
    if(!server.name[0])snprintf(server.name,sizeof(server.name),"Jellyfin");
    for(unsigned i=0;i<s->count;i++)if(!strcmp(s->server[i].url,server.url))return 0;
    if(s->count>=PJ_DISCOVERY_MAX)return 0;
    s->server[s->count++]=server;return 1;
}
/* "chromatic-" + 24 hex digits from 12 random bytes: the record's DeviceId charset. */
static inline void pj_qc_device_id(char out[65],const uint8_t random12[12]) {
    static const char hex[]="0123456789abcdef";
    memcpy(out,"chromatic-",10);
    for(unsigned i=0;i<12;i++){out[10+i*2]=hex[random12[i]>>4];out[11+i*2]=hex[random12[i]&15];}
    out[34]=0;
}
enum {PJ_QC_IDLE,PJ_QC_ENABLED,PJ_QC_INITIATE,PJ_QC_INITIATE_GET,PJ_QC_WAIT,PJ_QC_AUTH,PJ_QC_DONE,PJ_QC_FAILED,
      PJ_QC_LOGOUT,PJ_QC_LOGGED_OUT,PJ_QC_VERIFY,PJ_QC_VERIFIED,PJ_QC_INFO,PJ_QC_INFO_OK};
enum {PJ_QC_FAIL_NONE,PJ_QC_FAIL_UNREACHABLE,PJ_QC_FAIL_DISABLED,PJ_QC_FAIL_REFUSED,PJ_QC_FAIL_EXPIRED,PJ_QC_FAIL_UNSTORABLE,
      PJ_QC_FAIL_NOT_OURS,PJ_QC_FAIL_NOT_SAME};
static const char *const pj_qc_fail_words[8]={"","The server could not be reached","Quick Connect is off on the server",
    "The server gave an unexpected reply","The code expired: try again","The sign-in cannot be stored here",
    "That server does not know this console","That address answers as another server"};
typedef struct {const char *method;char path[200];char body[160];uint8_t with_token;} pj_qc_http;
typedef struct {
    uint8_t state,fail;
    char code[12];char secret[97];
    char url[129],device_id[65],user_id[33],token[65];
    char expect_id[33],server_id[33],server_name[32],server_version[16]; /* 1.3.1: /System/Info/Public, before any token */
    uint32_t started_ms,next_poll_ms;uint8_t pending; /* a request is out */
    pj_json_token tokens[PJ_QC_JSON_TOKENS]; /* JSON scratch: lives with the (heap-allocated) setup, no static DRAM */
} pj_quickconnect;
static inline void pj_qc_wipe(pj_quickconnect *q){memset(q->secret,0,sizeof(q->secret));memset(q->token,0,sizeof(q->token));}
static inline void pj_qc_begin(pj_quickconnect *q,const char *url,const char *device_id,uint32_t now) {
    memset(q,0,sizeof(*q));
    snprintf(q->url,sizeof(q->url),"%s",url);snprintf(q->device_id,sizeof(q->device_id),"%s",device_id);
    q->state=PJ_QC_ENABLED;q->started_ms=now;q->next_poll_ms=now;
}
static inline void pj_qc_fail(pj_quickconnect *q,int why){q->state=PJ_QC_FAILED;q->fail=(uint8_t)why;pj_qc_wipe(q);}
/* The next request to send, when one is due. 1 and *out filled; 0 nothing to do now. */
static inline int pj_qc_request(pj_quickconnect *q,uint32_t now,pj_qc_http *out) {
    if(q->pending)return 0;
    memset(out,0,sizeof(*out));
    switch(q->state) {
    case PJ_QC_ENABLED:out->method="GET";snprintf(out->path,sizeof(out->path),"/QuickConnect/Enabled");break;
    case PJ_QC_INITIATE:out->method="POST";snprintf(out->path,sizeof(out->path),"/QuickConnect/Initiate");break;
    case PJ_QC_INITIATE_GET:out->method="GET";snprintf(out->path,sizeof(out->path),"/QuickConnect/Initiate");break;
    case PJ_QC_WAIT:
        if((int32_t)(now-q->started_ms)>=(int32_t)PJ_QC_DEADLINE_MS){pj_qc_fail(q,PJ_QC_FAIL_EXPIRED);return 0;}
        if((int32_t)(now-q->next_poll_ms)<0)return 0;
        out->method="GET";snprintf(out->path,sizeof(out->path),"/QuickConnect/Connect?secret=%s",q->secret);break;
    case PJ_QC_AUTH:out->method="POST";snprintf(out->path,sizeof(out->path),"/Users/AuthenticateWithQuickConnect");
        snprintf(out->body,sizeof(out->body),"{\"Secret\":\"%s\"}",q->secret);break;
    case PJ_QC_LOGOUT:out->method="POST";snprintf(out->path,sizeof(out->path),"/Sessions/Logout");out->with_token=1;break;
    /* Find my server again: this console's own user, with its own token, on the server the owner picked. A GET:
     * it changes nothing on the server and signs nobody out. */
    case PJ_QC_VERIFY:out->method="GET";snprintf(out->path,sizeof(out->path),"/Users/%s",q->user_id);out->with_token=1;break;
    /* 1.3.1: who is this? Unauthenticated: no token leaves the console until the owner has seen the answer. */
    case PJ_QC_INFO:out->method="GET";snprintf(out->path,sizeof(out->path),"/System/Info/Public");break;
    default:return 0;
    }
    q->pending=1;return 1;
}
static inline int pj_qc_charset(const char *s,const char *extra){return s[0]&&pj_prov_charset(s,extra);}
/* The reply to the last request. status <= 0: no reply (unreachable). */
static inline void pj_qc_response(pj_quickconnect *q,int status,const void *body,size_t len,uint32_t now) {
    pj_json_token *tokens=q->tokens;pj_json j;
    if(!q->pending)return;
    q->pending=0;
    if(q->state==PJ_QC_LOGOUT){q->state=PJ_QC_LOGGED_OUT;pj_qc_wipe(q);return;} /* any answer: the token is gone from here */
    if(status<=0){pj_qc_fail(q,PJ_QC_FAIL_UNREACHABLE);return;}
    switch(q->state) {
    case PJ_QC_INFO: { /* the server's own Id, name and version; its Id must be the one discovery heard from that address */
        if(status!=200||!pj_qc_json(&j,tokens,body,len)){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        char raw[48]={0},raw_name[64]={0};unsigned n=0;
        if(!pj_json_text(&j,pj_json_find(&j,0,"Id"),raw,sizeof(raw))){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        for(const char *p=raw;*p&&n<32;p++){char c=*p;if(c=='-')continue;if(c>='A'&&c<='F')c=(char)(c+32);q->server_id[n++]=c;}
        q->server_id[n<33?n:32]=0;
        if(n!=32){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        if(q->expect_id[0]&&strcmp(q->expect_id,q->server_id)){pj_qc_fail(q,PJ_QC_FAIL_NOT_SAME);return;}
        pj_json_text(&j,pj_json_find(&j,0,"ServerName"),raw_name,sizeof(raw_name));
        pj_qc_display(q->server_name,sizeof(q->server_name),raw_name);if(!q->server_name[0])snprintf(q->server_name,sizeof(q->server_name),"Jellyfin");
        char raw_version[24]={0};pj_json_text(&j,pj_json_find(&j,0,"Version"),raw_version,sizeof(raw_version));
        pj_qc_display(q->server_version,sizeof(q->server_version),raw_version);
        q->state=PJ_QC_INFO_OK;return;
    }
    case PJ_QC_VERIFY: { /* the server knows this console: 200 and the user it answers as is ours */
        pj_qc_wipe(q); /* the token is not needed past this one request */
        if(status==401||status==403){pj_qc_fail(q,PJ_QC_FAIL_NOT_OURS);return;}
        if(status!=200||!pj_qc_json(&j,tokens,body,len)){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        char raw[48]={0},id[33]={0};unsigned n=0;
        if(!pj_json_text(&j,pj_json_find(&j,0,"Id"),raw,sizeof(raw))){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        for(const char *p=raw;*p&&n<32;p++){char c=*p;if(c=='-')continue;if(c>='A'&&c<='F')c=(char)(c+32);id[n++]=c;}
        if(n!=32||strcmp(id,q->user_id)){pj_qc_fail(q,PJ_QC_FAIL_NOT_OURS);return;}
        q->state=PJ_QC_VERIFIED;return;
    }
    case PJ_QC_ENABLED:
        if(status!=200||!body||len<4||len>8||memcmp(body,"true",4)) {pj_qc_fail(q,status==200?PJ_QC_FAIL_DISABLED:PJ_QC_FAIL_REFUSED);return;}
        q->state=PJ_QC_INITIATE;return;
    case PJ_QC_INITIATE:case PJ_QC_INITIATE_GET:
        if(q->state==PJ_QC_INITIATE&&(status==404||status==405)){q->state=PJ_QC_INITIATE_GET;return;}
        if(status!=200||!pj_qc_json(&j,tokens,body,len)){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        if(!pj_json_text(&j,pj_json_find(&j,0,"Code"),q->code,sizeof(q->code))||
           !pj_json_text(&j,pj_json_find(&j,0,"Secret"),q->secret,sizeof(q->secret))||!q->code[0]||!q->secret[0]||
           !pj_qc_charset(q->code,NULL)||!pj_qc_charset(q->secret,"._-")){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        q->state=PJ_QC_WAIT;q->next_poll_ms=now+PJ_QC_POLL_MS;return;
    case PJ_QC_WAIT:
        if(status==404){pj_qc_fail(q,PJ_QC_FAIL_EXPIRED);return;}
        if(status!=200||!pj_qc_json(&j,tokens,body,len)){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        if(pj_json_true(&j,pj_json_find(&j,0,"Authenticated")))q->state=PJ_QC_AUTH;
        else q->next_poll_ms=now+PJ_QC_POLL_MS;
        return;
    case PJ_QC_AUTH: {
        if(status!=200||!pj_qc_json(&j,tokens,body,len)){pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        char raw_user[48]={0};
        int user=pj_json_find(&j,pj_json_find(&j,0,"User"),"Id");
        if(!pj_json_text(&j,user,raw_user,sizeof(raw_user))||!pj_json_text(&j,pj_json_find(&j,0,"AccessToken"),q->token,sizeof(q->token)))
            {pj_qc_fail(q,PJ_QC_FAIL_REFUSED);return;}
        unsigned n=0;
        for(const char *p=raw_user;*p&&n<33;p++){char c=*p;if(c=='-')continue;if(c>='A'&&c<='F')c=(char)(c+32);q->user_id[n++]=c;}
        q->user_id[n<33?n:32]=0;
        int good=n==32&&strlen(q->token)>=16&&strlen(q->token)<=64&&pj_qc_charset(q->token,NULL);
        for(unsigned i=0;good&&i<32;i++){char c=q->user_id[i];if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))good=0;}
        memset(raw_user,0,sizeof(raw_user));
        if(!good){memset(q->user_id,0,sizeof(q->user_id));pj_qc_fail(q,PJ_QC_FAIL_UNSTORABLE);return;}
        memset(q->secret,0,sizeof(q->secret));q->state=PJ_QC_DONE;return;
    }
    default:return;
    }
}
/* Forget: one request that logs out only this console's own session. */
static inline void pj_qc_logout_begin(pj_quickconnect *q,const char *url,const char *device_id,const char *token) {
    memset(q,0,sizeof(*q));
    snprintf(q->url,sizeof(q->url),"%s",url);snprintf(q->device_id,sizeof(q->device_id),"%s",device_id);
    snprintf(q->token,sizeof(q->token),"%s",token);q->state=PJ_QC_LOGOUT;
}
/* Find my server again, step 1 (1.3.1): ask the server at `url` who it is, without a token. `expect_id`: the Id the
 * discovery reply from that address gave ("" for a typed address). */
static inline void pj_qc_info_begin(pj_quickconnect *q,const char *url,const char *device_id,const char *expect_id) {
    memset(q,0,sizeof(*q));
    snprintf(q->url,sizeof(q->url),"%s",url);snprintf(q->device_id,sizeof(q->device_id),"%s",device_id);
    snprintf(q->expect_id,sizeof(q->expect_id),"%s",expect_id?expect_id:"");q->state=PJ_QC_INFO;
}
/* Find my server again (launcher 1.3.0): does the server at `url` (picked by the owner from the discovery
 * replies) know this console's own sign-in? One authenticated GET of this console's own user. The token is kept
 * here only for that request and wiped with its answer; nothing is logged out or changed on any server. */
static inline void pj_qc_verify_begin(pj_quickconnect *q,const char *url,const char *device_id,const char *token,const char *user_id) {
    memset(q,0,sizeof(*q));
    snprintf(q->url,sizeof(q->url),"%s",url);snprintf(q->device_id,sizeof(q->device_id),"%s",device_id);
    snprintf(q->token,sizeof(q->token),"%s",token);snprintf(q->user_id,sizeof(q->user_id),"%s",user_id);q->state=PJ_QC_VERIFY;
}
/* The Authorization header Jellyfin expects from this device (with the token only for Logout
 * and the app itself). Built here so the binding never formats a token on its own. */
static inline void pj_qc_auth_header(char *out,size_t size,const char *device_id,const char *version,const char *token) {
    if(token&&token[0])snprintf(out,size,"MediaBrowser Client=\"" PJ_JF_CLIENT "\", Device=\"" PJ_JF_DEVICE "\", DeviceId=\"%s\", Version=\"%s\", Token=\"%s\"",device_id,version,token);
    else snprintf(out,size,"MediaBrowser Client=\"" PJ_JF_CLIENT "\", Device=\"" PJ_JF_DEVICE "\", DeviceId=\"%s\", Version=\"%s\"",device_id,version);
}
#endif
