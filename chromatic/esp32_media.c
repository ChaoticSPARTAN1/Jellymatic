/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * ESP-IDF v5.3 TLS adapter. No credential logging or console/cartridge writes.
 */
#include "esp32_media.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"
#include "mbedtls/sha256.h"
#include "mbedtls/platform_util.h"
#if !CONFIG_MBEDTLS_SSL_KEEP_PEER_CERTIFICATE
#error "Exact certificate pinning requires retained peer certificate"
#endif
static uint32_t now_ms(void){return (uint32_t)(esp_timer_get_time()/1000);}
static int wait_code(int n){return n==ESP_TLS_ERR_SSL_WANT_READ||n==ESP_TLS_ERR_SSL_WANT_WRITE;}
static void net_close(void *context) {
    pj_esp_media *t=context;
    if(t->tls){esp_tls_conn_destroy(t->tls);t->tls=NULL;}
}
static int net_write(void *context,const uint8_t *data,size_t size) {
    pj_esp_media *t=context;if(!t->tls)return -1;
    int n=(int)esp_tls_conn_write(t->tls,data,size);return wait_code(n)?0:n>0?n:-1;
}
static int net_read(void *context,uint8_t *data,size_t size) {
    pj_esp_media *t=context;if(!t->tls)return -1;
    int n=(int)esp_tls_conn_read(t->tls,data,size);return wait_code(n)?0:n>0?n:-1;
}
static int enter_media(void *context,uint32_t key) {
    pj_esp_media *t=context;return t->board.enter_media(t->board.context,key);
}
static void game_mode(void *context){pj_esp_media *t=context;t->board.game_mode(t->board.context);}
static int heartbeat(void *context){pj_esp_media *t=context;return t->board.heartbeat(t->board.context);}
static int status(void *context,uint32_t *used,unsigned *mode,unsigned *faults) {
    pj_esp_media *t=context;return t->board.status(t->board.context,used,mode,faults);
}
static int qspi_write(void *context,const uint8_t *p,size_t n) {
    pj_esp_media *t=context;return t->board.qspi_write(t->board.context,p,n);
}
static int private_ipv4(const char *host) {
    struct in_addr address;
    if(!host||strlen(host)>15||inet_pton(AF_INET,host,&address)!=1)return 0;
    uint32_t v=ntohl(address.s_addr);
    return (v>>24)==10||(v>>20)==0xac1||(v>>16)==0xc0a8;
}
int pj_esp_media_connect(pj_esp_media *t,pj_media_bridge *bridge,const pj_esp_media_config *cfg,pj_media_ops board) {
    if(!t||!bridge||!cfg||!private_ipv4(cfg->pc_ipv4)||!cfg->port||
       !cfg->certificate_der||cfg->certificate_size<128||cfg->certificate_size>8192||
       !cfg->pairing||cfg->pairing_size!=32||!board.enter_media||!board.game_mode||
       !board.heartbeat||!board.status||!board.qspi_write)return 0;
    /* Fresh zero-initialized transport; caller closes before reuse. */
    if(t->tls||t->bridge)return 0;
    t->board=board;t->tls=esp_tls_init();if(!t->tls)return 0;
    esp_tls_cfg_t tls_cfg={0};tls_cfg.cacert_buf=cfg->certificate_der;
    tls_cfg.cacert_bytes=(unsigned)cfg->certificate_size;
    tls_cfg.common_name="pocket-media.local";tls_cfg.timeout_ms=10000;
    tls_cfg.non_block=true;tls_cfg.tls_version=ESP_TLS_VER_TLS_1_2;
    uint32_t began=now_ms();int result=0;
    do {
        result=esp_tls_conn_new_async(cfg->pc_ipv4,(int)strlen(cfg->pc_ipv4),cfg->port,&tls_cfg,t->tls);
        if(result!=0)break;
        vTaskDelay(pdMS_TO_TICKS(5));
    } while((uint32_t)(now_ms()-began)<10000);
    if(result!=1)goto fail;
    mbedtls_ssl_context *ssl=esp_tls_get_ssl_context(t->tls);
    const mbedtls_x509_crt *peer=ssl?mbedtls_ssl_get_peer_cert(ssl):NULL;
    uint8_t expected[32],actual[32];
    if(!peer||!peer->raw.p||mbedtls_sha256(cfg->certificate_der,cfg->certificate_size,expected,0)||
       mbedtls_sha256(peer->raw.p,peer->raw.len,actual,0))goto fail;
    unsigned difference=0;for(unsigned i=0;i<32;i++)difference|=expected[i]^actual[i];
    if(difference)goto fail;
    /* Pairing value is independent of Jellyfin; send only after trust + pin. */
    uint8_t auth[36];memcpy(auth,"PJT1",4);memcpy(auth+4,cfg->pairing,32);
    size_t offset=0;began=now_ms();
    while(offset<sizeof(auth)&&(uint32_t)(now_ms()-began)<10000) {
        int n=net_write(t,auth+offset,sizeof(auth)-offset);
        if(n<0)break;
        offset+=(size_t)n;if(!n)vTaskDelay(pdMS_TO_TICKS(5));
    }
    mbedtls_platform_zeroize(auth,sizeof(auth));
    if(offset!=sizeof(auth))goto fail;
    uint8_t ack[4];offset=0;began=now_ms();
    while(offset<sizeof(ack)&&(uint32_t)(now_ms()-began)<10000) {
        int n=net_read(t,ack+offset,sizeof(ack)-offset);
        if(n<0)break;
        offset+=(size_t)n;if(!n)vTaskDelay(pdMS_TO_TICKS(5));
    }
    if(offset!=sizeof(ack)||memcmp(ack,"PJA1",4))goto fail;
    pj_media_ops ops={t,net_write,net_read,net_close,enter_media,game_mode,heartbeat,status,qspi_write};
    t->bridge=bridge;
    if(!pj_media_init(bridge,ops,now_ms())){t->bridge=NULL;goto fail;}
    return 1;
fail:net_close(t);return 0;
}
int pj_esp_media_step(pj_esp_media *t,uint32_t now) {
    return t&&t->bridge?pj_media_step(t->bridge,now):-1;
}
int pj_esp_media_pocket_bytes(pj_esp_media *t,const uint8_t *p,size_t n,uint32_t now) {
    return t&&t->bridge?pj_media_pocket_bytes(t->bridge,p,n,now):-1;
}
void pj_esp_media_close(pj_esp_media *t) {
    if(!t)return;
    if(t->bridge){pj_media_fail(t->bridge);t->bridge=NULL;}
    net_close(t);
}
int pj_esp_media_qspi_describe(spi_transaction_t *tx,const uint8_t *data,size_t n) {
    if(!tx||!data||n<2||n>1024||(n&1))return 0;
    memset(tx,0,sizeof(*tx));tx->tx_buffer=data;tx->length=n*8;
    tx->flags=SPI_TRANS_MODE_QIO;tx->cmd=(uint16_t)(0x400|(n-1));tx->addr=0x504a0000u;
    tx->user=PJ_MEDIA_QSPI_TAG;return 1;
}
