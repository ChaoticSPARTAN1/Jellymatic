/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Builds a version-2 provisioning sector from a pj_prov2 record: the C twin of
 * scripts/chromatic_provision.py Record.sector(), byte for byte (checked against every
 * Python vector by scripts/test_chromatic_setup.py). Pure: no flash, no allocation.
 * The result is only ever handed to the one audited writer (provision_write.c). */
#ifndef PJ_PROVISION_BUILD_H
#define PJ_PROVISION_BUILD_H
#include "provision.h"
static inline void pj_prov_put16(uint8_t *p,uint16_t v){p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8);}
static inline void pj_prov_put32(uint8_t *p,uint32_t v){p[0]=(uint8_t)v;p[1]=(uint8_t)(v>>8);p[2]=(uint8_t)(v>>16);p[3]=(uint8_t)(v>>24);}
static inline void pj_prov_put_text(uint8_t *at,size_t size,const char *text) {
    memset(at,0,size);size_t n=strlen(text);if(n>=size)n=size-1;memcpy(at,text,n);
}
/* 1 and a full 4096-byte sector (0xFF after the CRC), or 0 when the record would not parse
 * back (nothing is written for such a record). The record's own `sequence` is used. */
static inline int pj_prov2_build(const pj_prov2 *r,uint8_t *sector) {
    if(r->count>PJ_PROV2_NETWORKS)return 0;
    memset(sector,0xff,PJ_PROVISION_SECTOR);
    uint8_t *b=sector;memset(b,0,PJ_PROV2_BODY);
    memcpy(b,"PJPV",4);pj_prov_put16(b+4,PJ_PROV2_VERSION);pj_prov_put16(b+6,PJ_PROV2_BODY);
    pj_prov_put32(b+8,r->sequence);b[12]=(uint8_t)r->count;
    b[13]=(uint8_t)((r->has_gateway?PJ_PROV2_FLAG_GATEWAY:0)|(r->has_jellyfin?PJ_PROV2_FLAG_JELLYFIN:0));
    for(unsigned i=0;i<r->count;i++) {
        uint8_t *slot=b+PJ_PROV2_NETWORKS_AT+i*PJ_PROV2_SLOT;
        pj_prov_put_text(slot,33,r->net[i].ssid);pj_prov_put_text(slot+33,65,r->net[i].psk);
    }
    if(r->has_gateway) {
        uint8_t *g=b+PJ_PROV2_GATEWAY_AT;
        memcpy(g,r->gateway_ipv4,4);pj_prov_put16(g+4,r->gateway_port);pj_prov_put16(g+6,0);
        memcpy(g+8,r->gateway_cert_sha256,32);memcpy(g+40,r->gateway_token,32);memcpy(g+72,r->chr1_key,32);
    }
    if(r->has_jellyfin) {
        uint8_t *j=b+PJ_PROV2_JELLYFIN_AT;
        pj_prov_put_text(j,129,r->url);pj_prov_put_text(j+129,33,r->user_id);
        pj_prov_put_text(j+162,65,r->access_token);pj_prov_put_text(j+227,65,r->device_id);
    }
    pj_prov_put32(b+PJ_PROV2_BODY,pj_provision_crc32(b,PJ_PROV2_BODY));
    pj_prov2 back;int ok=pj_provision2_parse(sector,PJ_PROVISION_SECTOR,&back)==1;
    memset(&back,0,sizeof(back));
    if(!ok)memset(sector,0xff,PJ_PROVISION_SECTOR);
    return ok;
}
#endif
