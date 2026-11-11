/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Reads a 4096-byte sector from stdin and prints how provision.h parses it, so the
 * Python twin can be checked against the C the launcher uses. Prints no secrets:
 * only the result code, field lengths, the port and a checksum of the parsed fields. */
#include <stdio.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include "shared/provision.h"
int main(void) {
    static uint8_t sector[PJ_PROVISION_SECTOR];
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY); /* raw bytes, no CR/LF or ^Z translation */
#endif
    if(fread(sector,1,sizeof(sector),stdin)!=sizeof(sector)){puts("short");return 2;}
    pj_provision p;
    int result=pj_provision_parse(sector,sizeof(sector),&p);
    if(result!=1){printf("%d\n",result);return 0;}
    uint32_t sum=pj_provision_crc32((const uint8_t *)&p,sizeof(p));
    printf("1 %u %u %u %08lx\n",(unsigned)strlen(p.ssid),(unsigned)strlen(p.psk),p.gateway_port,(unsigned long)sum);
    return 0;
}
