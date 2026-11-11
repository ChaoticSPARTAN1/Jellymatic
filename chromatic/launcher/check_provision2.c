/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Reads sectors from stdin and prints how provision.h (v1 + v2, newest wins) reads them,
 * so scripts/test_chromatic_provision_c.py can check the launcher's C against the Python
 * twin's vectors. Prints no secrets: counts, the port and CRC-32s of the parsed fields.
 *   check_provision2 one   < 4096-byte sector    -> absent | refused | ok ...
 *   check_provision2 pair  < sector A, sector B  -> none | ok ...  (the record the launcher uses)
 */
#include <stdio.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include "shared/provision.h"
static void show(const pj_prov2 *r) {
    uint32_t names=0,open=0;
    for(unsigned i=0;i<r->count;i++)if(!r->net[i].psk[0])open++;
    /* CRC-32 of the SSIDs joined with "\n", computed in one pass. */
    char joined[PJ_PROV2_NETWORKS*34];size_t used=0;
    for(unsigned i=0;i<r->count;i++){size_t n=strlen(r->net[i].ssid);if(i)joined[used++]='\n';memcpy(joined+used,r->net[i].ssid,n);used+=n;}
    names=pj_provision_crc32((const uint8_t *)joined,used);
    printf("ok version=%u seq=%lu count=%u names=%08lx open=%lu port=",r->version,(unsigned long)r->sequence,r->count,
           (unsigned long)names,(unsigned long)open);
    if(r->has_gateway)printf("%u",r->gateway_port);else printf("-");
    printf(" key=%08lx url=",r->has_gateway?(unsigned long)pj_provision_crc32(r->chr1_key,32):0ul);
    if(r->has_jellyfin)printf("%08lx\n",(unsigned long)pj_provision_crc32((const uint8_t *)r->url,strlen(r->url)));
    else printf("-\n");
}
int main(int argc,char **argv) {
    static uint8_t sectors[2][4096];
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY);
#endif
    int pair=argc>1&&!strcmp(argv[1],"pair");
    for(int i=0;i<1+pair;i++)if(fread(sectors[i],1,4096,stdin)!=4096){puts("short");return 2;}
    pj_prov2 r;
    if(pair){if(pj_provision_current(sectors[0],sectors[1],4096,&r))show(&r);else puts("none");return 0;}
    int got=pj_provision_any(sectors[0],4096,&r);
    if(got==1)show(&r);else puts(got?"refused":"absent");
    return 0;
}
