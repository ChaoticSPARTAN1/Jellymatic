/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Provisioning record: Wi-Fi and pairing for launcher 1.2, written by Chromatic Manager
 * over USB into the last sector of the launcher slot (0x3FF000). The launcher only
 * reads it. Python twin: scripts/chromatic_provision.py. Owner data: never logged.
 *
 * Little-endian: "PJPV" | u16 version | u16 length (bytes before the CRC) |
 *   ssid[33] | psk[65] | gateway_ipv4[4] | u16 gateway_port | gateway_cert_sha256[32] |
 *   gateway_token[32] | chr1_key[32] | u32 crc32 (IEEE, over everything before it)
 * No Jellyfin server address or credentials: those stay on the PC.
 */
#ifndef PJ_PROVISION_H
#define PJ_PROVISION_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define PJ_PROVISION_OFFSET 0x19f000u  /* within the launcher partition (starts 0x260000) */
#define PJ_PROVISION_SECTOR 4096u
#define PJ_PROVISION_VERSION 1u
#define PJ_PROVISION_BODY (4u + 2u + 2u + 33u + 65u + 4u + 2u + 32u + 32u + 32u)
#define PJ_PROVISION_SIZE (PJ_PROVISION_BODY + 4u)
typedef struct {
    char ssid[33];       /* NUL-terminated, 1..32 bytes */
    char psk[65];        /* NUL-terminated, empty for an open network, else 8..64 */
    uint8_t gateway_ipv4[4];
    uint16_t gateway_port;
    uint8_t gateway_cert_sha256[32];
    uint8_t gateway_token[32];
    uint8_t chr1_key[32];
} pj_provision;

static inline uint32_t pj_provision_crc32(const uint8_t *data,size_t size) {
    uint32_t crc=0xffffffffu;
    for(size_t i=0;i<size;i++) {
        crc^=data[i];
        for(int bit=0;bit<8;bit++)crc=(crc>>1)^(0xedb88320u&(0u-(crc&1u)));
    }
    return crc^0xffffffffu;
}
static inline uint16_t pj_prov_u16(const uint8_t *p){return (uint16_t)(p[0]|(p[1]<<8));}
static inline uint32_t pj_prov_u32(const uint8_t *p){return (uint32_t)p[0]|((uint32_t)p[1]<<8)|((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);}
static inline int pj_prov_private_ipv4(const uint8_t ip[4]) {
    return ip[0]==10||(ip[0]==172&&(ip[1]&0xf0u)==16)||(ip[0]==192&&ip[1]==168);
}
/* 1 valid record (copied into *out), 0 absent (erased sector), -1 damaged or invalid.
 * Every field is checked, so a damaged sector can never be half-used. */
static inline int pj_provision_parse(const uint8_t *sector,size_t size,pj_provision *out) {
    if(size<PJ_PROVISION_SIZE)return -1;
    int erased=1;
    for(size_t i=0;i<PJ_PROVISION_SIZE;i++)if(sector[i]!=0xffu){erased=0;break;}
    if(erased)return 0;
    if(memcmp(sector,"PJPV",4)||pj_prov_u16(sector+4)!=PJ_PROVISION_VERSION||
       pj_prov_u16(sector+6)!=PJ_PROVISION_BODY)return -1;
    if(pj_provision_crc32(sector,PJ_PROVISION_BODY)!=pj_prov_u32(sector+PJ_PROVISION_BODY))return -1;
    pj_provision p;
    const uint8_t *at=sector+8;
    memcpy(p.ssid,at,33);at+=33;
    memcpy(p.psk,at,65);at+=65;
    memcpy(p.gateway_ipv4,at,4);at+=4;
    p.gateway_port=pj_prov_u16(at);at+=2;
    memcpy(p.gateway_cert_sha256,at,32);at+=32;
    memcpy(p.gateway_token,at,32);at+=32;
    memcpy(p.chr1_key,at,32);
    if(p.ssid[32]||p.psk[64])return -1;
    size_t ssid=strlen(p.ssid),psk=strlen(p.psk);
    if(!ssid||ssid>32||(psk&&(psk<8||psk>64)))return -1;
    if(!pj_prov_private_ipv4(p.gateway_ipv4)||!p.gateway_port)return -1;
    *out=p;
    return 1;
}
/* ---- version 2 (docs/Design-notes.md; Python twin scripts/chromatic_provision.py) ----
 * Two sectors at the end of the launcher slot, A at 0x3FE000 and B at 0x3FF000 (slot
 * offsets below). Chromatic Manager writes the same record to both. The newest valid
 * record wins, a tie goes to A, and a damaged sector counts as empty. A version-1 sector
 * reads as sequence 0 with one network and a gateway. Read only: the launcher never
 * writes either sector. Stricter than Python in one place: a gateway address must be
 * RFC 1918 (10/8, 172.16/12, 192.168/16). */
#define PJ_PROVISION_OFFSET_A 0x19e000u
#define PJ_PROVISION_OFFSET_B 0x19f000u
#define PJ_PROV2_VERSION 2u
#define PJ_PROV2_NETWORKS 4u
#define PJ_PROV2_FLAG_GATEWAY 0x01u
#define PJ_PROV2_FLAG_JELLYFIN 0x02u
#define PJ_PROV2_NETWORKS_AT 16u
#define PJ_PROV2_SLOT (33u+65u)
#define PJ_PROV2_GATEWAY_AT (PJ_PROV2_NETWORKS_AT+PJ_PROV2_NETWORKS*PJ_PROV2_SLOT) /* 408 */
#define PJ_PROV2_JELLYFIN_AT (PJ_PROV2_GATEWAY_AT+4u+2u+2u+32u+32u+32u)        /* 512 */
#define PJ_PROV2_BODY (PJ_PROV2_JELLYFIN_AT+129u+33u+65u+65u+4u)                /* 808 */
#define PJ_PROV2_SIZE (PJ_PROV2_BODY+4u)
typedef struct {char ssid[33],psk[65];} pj_prov_network;
typedef struct {
    uint32_t sequence;unsigned version,count;pj_prov_network net[PJ_PROV2_NETWORKS];
    int has_gateway;uint8_t gateway_ipv4[4];uint16_t gateway_port;
    uint8_t gateway_cert_sha256[32],gateway_token[32],chr1_key[32];
    int has_jellyfin;char url[129],user_id[33],access_token[65],device_id[65];
} pj_prov2;

static inline int pj_prov_zero(const uint8_t *p,size_t n) {for(size_t i=0;i<n;i++)if(p[i])return 0;return 1;}
/* Strict UTF-8 (no overlongs, no surrogates), as Python's decode('utf-8') accepts. */
static inline int pj_prov_utf8(const uint8_t *p,size_t n) {
    for(size_t i=0;i<n;) {
        uint8_t c=p[i];size_t extra;uint32_t cp;
        if(c<0x80){i++;continue;}
        if((c&0xe0)==0xc0){extra=1;cp=c&0x1f;}else if((c&0xf0)==0xe0){extra=2;cp=c&0x0f;}
        else if((c&0xf8)==0xf0){extra=3;cp=c&0x07;}else return 0;
        if(i+extra>=n)return 0;
        for(size_t k=1;k<=extra;k++){if((p[i+k]&0xc0)!=0x80)return 0;cp=cp<<6|(p[i+k]&0x3fu);}
        if((extra==1&&cp<0x80)||(extra==2&&cp<0x800)||(extra==3&&(cp<0x10000||cp>0x10ffff))||(cp>=0xd800&&cp<=0xdfff))return 0;
        i+=extra+1;
    }
    return 1;
}
/* A NUL-terminated field: text, then only zero bytes. Copies it; length or -1. */
static inline int pj_prov_text(const uint8_t *raw,size_t size,char *out) {
    size_t end=0;while(end<size&&raw[end])end++;
    if(end==size||!pj_prov_zero(raw+end,size-end)||!pj_prov_utf8(raw,end))return -1;
    memcpy(out,raw,end);out[end]=0;return (int)end;
}
static inline int pj_prov_charset(const char *s,const char *extra) {
    for(;*s;s++){char c=*s;if(!((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||(extra&&strchr(extra,c))))return 0;}
    return 1;
}
/* http(s)://host[:port][/path]: printable ASCII, no user info, query, fragment or
 * trailing slash, a host, and a port of 1..65535 when given. */
static inline int pj_prov_url(const char *url) {
    const char *host;
    if(!strncmp(url,"http://",7))host=url+7;else if(!strncmp(url,"https://",8))host=url+8;else return 0;
    size_t n=strlen(url);
    if(!*host||url[n-1]=='/')return 0;
    for(const char *p=url;*p;p++)if(*p<=0x20||*p>=0x7f||*p=='@'||*p=='?'||*p=='#'||*p=='\\')return 0;
    const char *end=strchr(host,'/');size_t hl=end?(size_t)(end-host):strlen(host);
    const char *colon=memchr(host,':',hl);
    if(colon) {
        if(colon==host)return 0;
        unsigned long port=0;const char *d=colon+1;
        if(d==host+hl)return 0;
        for(;d<host+hl;d++){if(*d<'0'||*d>'9')return 0;port=port*10u+(unsigned)(*d-'0');if(port>65535u)return 0;}
        if(!port)return 0;
    } else if(!hl)return 0;
    return 1;
}
/* 1 valid record (into *out), 0 erased, -1 damaged or invalid. */
static inline int pj_provision2_parse(const uint8_t *sector,size_t size,pj_prov2 *out) {
    if(size<PJ_PROV2_SIZE)return -1;
    int erased=1;for(size_t i=0;i<PJ_PROV2_SIZE;i++)if(sector[i]!=0xffu){erased=0;break;}
    if(erased)return 0;
    pj_prov2 r;memset(&r,0,sizeof(r));
    if(memcmp(sector,"PJPV",4)||pj_prov_u16(sector+4)!=PJ_PROV2_VERSION||pj_prov_u16(sector+6)!=PJ_PROV2_BODY)return -1;
    if(pj_provision_crc32(sector,PJ_PROV2_BODY)!=pj_prov_u32(sector+PJ_PROV2_BODY))return -1;
    unsigned count=sector[12],flags=sector[13];
    if(pj_prov_u16(sector+14)||count>PJ_PROV2_NETWORKS||(flags&~(PJ_PROV2_FLAG_GATEWAY|PJ_PROV2_FLAG_JELLYFIN))||
       !pj_prov_zero(sector+PJ_PROV2_BODY-4,4))return -1;
    r.sequence=pj_prov_u32(sector+8);r.version=2;r.count=count;
    for(unsigned i=0;i<PJ_PROV2_NETWORKS;i++) {
        const uint8_t *slot=sector+PJ_PROV2_NETWORKS_AT+i*PJ_PROV2_SLOT;
        if(i>=count){if(!pj_prov_zero(slot,PJ_PROV2_SLOT))return -1;continue;}
        int ssid=pj_prov_text(slot,33,r.net[i].ssid),psk=pj_prov_text(slot+33,65,r.net[i].psk);
        if(ssid<1||ssid>32||psk<0||(psk&&(psk<8||psk>64)))return -1;
    }
    const uint8_t *g=sector+PJ_PROV2_GATEWAY_AT;
    if(flags&PJ_PROV2_FLAG_GATEWAY) {
        memcpy(r.gateway_ipv4,g,4);r.gateway_port=pj_prov_u16(g+4);
        if(pj_prov_u16(g+6)||!pj_prov_private_ipv4(r.gateway_ipv4)||!r.gateway_port)return -1;
        memcpy(r.gateway_cert_sha256,g+8,32);memcpy(r.gateway_token,g+40,32);memcpy(r.chr1_key,g+72,32);r.has_gateway=1;
    } else if(!pj_prov_zero(g,PJ_PROV2_JELLYFIN_AT-PJ_PROV2_GATEWAY_AT))return -1;
    const uint8_t *j=sector+PJ_PROV2_JELLYFIN_AT;
    if(flags&PJ_PROV2_FLAG_JELLYFIN) {
        if(pj_prov_text(j,129,r.url)<1||pj_prov_text(j+129,33,r.user_id)!=32||pj_prov_text(j+162,65,r.access_token)<16||
           pj_prov_text(j+227,65,r.device_id)<1)return -1;
        if(!pj_prov_url(r.url)||!pj_prov_charset(r.access_token,NULL)||!pj_prov_charset(r.device_id,"._-"))return -1;
        for(int i=0;i<32;i++){char c=r.user_id[i];if(!((c>='0'&&c<='9')||(c>='a'&&c<='f')))return -1;}
        if(strlen(r.access_token)>64||strlen(r.device_id)>64)return -1;
        r.has_jellyfin=1;
    } else if(!pj_prov_zero(j,PJ_PROV2_BODY-4-PJ_PROV2_JELLYFIN_AT))return -1;
    *out=r;
    memset(&r,0,sizeof(r));
    return 1;
}
/* Version 1 or 2 in one shape: 1 valid, 0 erased, -1 damaged. */
static inline int pj_provision_any(const uint8_t *sector,size_t size,pj_prov2 *out) {
    if(size>=8&&!memcmp(sector,"PJPV",4)&&pj_prov_u16(sector+4)==PJ_PROVISION_VERSION) {
        pj_provision v1;int got=pj_provision_parse(sector,size,&v1);
        if(got!=1)return got;
        memset(out,0,sizeof(*out));out->version=1;out->count=1;
        memcpy(out->net[0].ssid,v1.ssid,33);memcpy(out->net[0].psk,v1.psk,65);
        out->has_gateway=1;memcpy(out->gateway_ipv4,v1.gateway_ipv4,4);out->gateway_port=v1.gateway_port;
        memcpy(out->gateway_cert_sha256,v1.gateway_cert_sha256,32);memcpy(out->gateway_token,v1.gateway_token,32);
        memcpy(out->chr1_key,v1.chr1_key,32);
        memset(&v1,0,sizeof(v1));
        return 1;
    }
    return pj_provision2_parse(sector,size,out);
}
/* The record the launcher uses: newest valid of A and B (tie: A). 1 found, 0 none. */
static inline int pj_provision_current(const uint8_t *a,const uint8_t *b,size_t size,pj_prov2 *out) {
    pj_prov2 ra,rb;int ga=pj_provision_any(a,size,&ra)==1,gb=pj_provision_any(b,size,&rb)==1;
    if(ga&&(!gb||ra.sequence>=rb.sequence))*out=ra;else if(gb)*out=rb;
    memset(&ra,0,sizeof(ra));memset(&rb,0,sizeof(rb));
    return ga||gb;
}
#endif
