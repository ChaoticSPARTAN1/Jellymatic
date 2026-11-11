/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Additional terms: ADDITIONAL-TERMS.md */
#include <stdio.h>
#include <stdlib.h>
#include "media_bridge.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"MCU bridge check failed at %d\n",__LINE__);exit(1);}}while(0)
typedef struct {
    uint8_t request[1040],response[32784],recovered[32784];size_t req_size,resp_size,tx_at,rx_at;
    pj_wire_rx decoder;unsigned mode,fault,queue,max_queue,closes,games,heartbeats,writes,frames;
    uint32_t consumed;unsigned calls;int status_ready,heartbeat_ready,network_dead,qspi_fail;
} model;
static model m;
static pj_media_bridge bridge;
static int net_write(void *ctx,const uint8_t *p,size_t n) {
    model *s=ctx;if(++s->calls%5==0)return 0;if(n>17)n=17;
    CHECK(s->tx_at+n<=s->req_size&&!memcmp(p,s->request+s->tx_at,n));s->tx_at+=n;return (int)n;
}
static int net_read(void *ctx,uint8_t *p,size_t n) {
    model *s=ctx;if(s->network_dead)return -1;
    if(++s->calls%7==0||s->tx_at!=s->req_size||s->rx_at==s->resp_size)return 0;
    if(n>19)n=19;
    if(n>s->resp_size-s->rx_at)n=s->resp_size-s->rx_at;
    memcpy(p,s->response+s->rx_at,n);s->rx_at+=n;return (int)n;
}
static void net_close(void *ctx){((model *)ctx)->closes++;}
static int enter_media(void *ctx,uint32_t key){CHECK(key==PJ_MEDIA_KEY);((model *)ctx)->mode=2;return 1;}
static void game_mode(void *ctx){model *s=ctx;s->mode=0;s->games++;}
static int heartbeat(void *ctx){model *s=ctx;s->heartbeats++;return s->heartbeat_ready;}
static int status(void *ctx,uint32_t *consumed,unsigned *mode,unsigned *fault) {
    model *s=ctx;*consumed=s->consumed;*mode=s->mode;*fault=s->fault;return s->status_ready;
}
static int qspi_write(void *ctx,const uint8_t *p,size_t n) {
    model *s=ctx;if(s->qspi_fail)return -1;if(++s->calls%11==0)return 0;
    CHECK(n>=2&&n<=1024&&!(n&1));s->queue+=(unsigned)n;s->writes++;
    if(s->queue>s->max_queue)s->max_queue=s->queue;
    CHECK(s->queue<=PJ_MEDIA_CREDIT_WINDOW);
    for(size_t i=0;i<n;i++) {
        size_t size=0;int result=pj_wire_rx_byte(&s->decoder,p[i],&size);CHECK(result>=0);
        if(result){CHECK(size==s->resp_size&&!memcmp(s->recovered,s->response,size));s->frames++;}
    }
    return 1;
}
static pj_media_ops ops(void) {
    pj_media_ops result={&m,net_write,net_read,net_close,enter_media,game_mode,heartbeat,status,qspi_write};return result;
}
static void setup(uint32_t now) {
    memset(&m,0,sizeof(m));m.status_ready=m.heartbeat_ready=1;
    CHECK(pj_wire_rx_init(&m.decoder,m.recovered,sizeof(m.recovered)));
    CHECK(pj_media_init(&bridge,ops(),now));CHECK(!pj_media_step(&bridge,now));CHECK(bridge.state==PJ_MEDIA_IDLE);
}
static void send_request(uint32_t now) {
    pj_wire_tx tx;CHECK(pj_wire_tx_init(&tx,m.request,m.req_size));uint8_t byte;
    while(pj_wire_tx_next(&tx,&byte))CHECK(pj_media_pocket_bytes(&bridge,&byte,1,now)==1);
}
static uint32_t u32(FILE *f){uint8_t p[4];CHECK(fread(p,1,4,f)==4);return pj_av_u32(p);}
static void failure_checks(void) {
    setup(0);m.consumed=1;CHECK(pj_media_step(&bridge,1)==-1&&m.games==1&&m.closes==1);
    CHECK(pj_media_step(&bridge,2)==-1&&m.games==1&&m.closes==1);
    setup(0);m.status_ready=0;CHECK(pj_media_step(&bridge,1001)==-1&&m.mode==0);
    setup(0);m.heartbeat_ready=0;CHECK(pj_media_step(&bridge,1001)==-1&&m.mode==0);
    setup(0);m.network_dead=1;CHECK(pj_media_step(&bridge,1)==-1&&m.mode==0);
    setup(0);m.fault=1;CHECK(pj_media_step(&bridge,1)==-1&&m.mode==0);
    setup(0);m.mode=0;CHECK(pj_media_step(&bridge,1)==-1&&m.closes==1);
    setup(0);m.req_size=pj_rpc_encode(m.request,sizeof(m.request),1,1,"{}",2);
    m.request[m.req_size-1]^=1;
    pj_wire_tx tx;CHECK(pj_wire_tx_init(&tx,m.request,m.req_size));uint8_t byte;int result=1;
    while(pj_wire_tx_next(&tx,&byte)&&result>=0)result=pj_media_pocket_bytes(&bridge,&byte,1,1);
    CHECK(result<0&&m.mode==0);
}
int main(int argc,char **argv) {
    CHECK(argc==2);failure_checks();FILE *f=fopen(argv[1],"rb");CHECK(f);
    unsigned count=u32(f);CHECK(count>5&&count<100);uint32_t now=0xffffff00u;setup(now);
    /* Force credit and time wrap during normal transfers. */
    bridge.sent=bridge.consumed=m.consumed=0xfffffff0u;
    unsigned total_frames=0;
    for(unsigned test=0;test<count;test++) {
        m.req_size=u32(f);m.resp_size=u32(f);CHECK(m.req_size<=1040&&m.resp_size<=sizeof(m.response));
        CHECK(fread(m.request,1,m.req_size,f)==m.req_size&&fread(m.response,1,m.resp_size,f)==m.resp_size);
        m.tx_at=m.rx_at=0;unsigned before=m.frames;send_request(now);
        /* Duplicate while network work is pending must not restart it. */
        CHECK(!pj_media_step(&bridge,++now));size_t sent=m.tx_at;send_request(now);CHECK(m.tx_at==sent);
        for(unsigned tick=0;tick<20000;tick++) {
            if(tick>100&&m.queue){unsigned n=m.queue>17?17:m.queue;m.queue-=n;m.consumed+=n;}
            CHECK(!pj_media_step(&bridge,++now));
            if(bridge.state==PJ_MEDIA_IDLE&&m.frames==before+1)break;
            CHECK(tick!=19999);
        }
        CHECK(m.tx_at==m.req_size&&m.rx_at==m.resp_size&&m.frames==before+1);
        /* Cached replay: exact reply, no additional TLS write/read. */
        size_t tx=m.tx_at,rx=m.rx_at;send_request(now);
        for(unsigned tick=0;tick<20000;tick++) {
            if(m.queue){unsigned n=m.queue>17?17:m.queue;m.queue-=n;m.consumed+=n;}
            CHECK(!pj_media_step(&bridge,++now));
            if(bridge.state==PJ_MEDIA_IDLE&&m.frames==before+2)break;
            CHECK(tick!=19999);
        }
        CHECK(m.tx_at==tx&&m.rx_at==rx&&m.frames==before+2);total_frames=m.frames;
    }
    fclose(f);CHECK(m.max_queue<=1024&&m.heartbeats>0);
    pj_media_fail(&bridge);CHECK(m.mode==0&&m.closes==1);
    printf("PASS: %u actual Gateway exchanges / %u framed replies; bounded credits, duplicate cache, partial IO, wrap, stalls and GAME-on-fault. Modeled board only.\n",count,total_frames);
    return 0;
}
