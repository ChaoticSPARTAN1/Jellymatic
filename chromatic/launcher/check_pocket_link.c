/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Host test of the launcher's Pocket-link gateway module (shared/pocket_link.h, phase 2):
 * synthetic Jellyfin replies and a synthetic MJPEG+IMA AVI in, PJB1 replies and PJAV packets
 * out, checked with the Pocket's own parsers (pocket/live_library.h, stream_reader.h,
 * shared/av_stream.h). The FPGA bridge is a byte-exact model of its UART contract, including
 * the fast-UART handshake. No network, no hardware. Built and run by scripts/test_pocket_tether.py. */
#include <stdio.h>
#include <stdlib.h>
#include "shared/pocket_link.h"
#include "shared/launcher_diag.h"
#include "shared/log_ring.h"
#include "../../pocket/live_library.h"
#include "../../pocket/stream_reader.h"
#include "../../pocket/synthetic_avi.h"
#define CHECK(x) do{if(!(x)){fprintf(stderr,"pocket link check failed at %d\n",__LINE__);exit(1);}}while(0)

/* ---- synthetic Jellyfin ---- */
#define ID_MOVIES "11111111111111111111111111111111"
#define ID_SHOWS  "22222222222222222222222222222222"
#define ID_M1 "aaaa0000000000000000000000000001"
#define ID_M2 "aaaa0000000000000000000000000002"
#define ID_M3 "aaaa0000000000000000000000000003"
#define ID_E1 "bbbb0000000000000000000000000001"
#define ID_E2 "bbbb0000000000000000000000000002"
#define ID_S1 "cccc0000000000000000000000000001"
#define ID_S2 "cccc000000000000000000000000dead" /* no images at all */
#define MOVIE(id,name,year,resume,played) "{\"Id\":\"" id "\",\"Name\":\"" name "\",\"Type\":\"Movie\",\"ProductionYear\":" year \
    ",\"RunTimeTicks\":72000000000,\"UserData\":{\"PlaybackPositionTicks\":" resume ",\"Played\":" played "},\"ImageTags\":{\"Primary\":\"p\"},\"BackdropImageTags\":[\"b\"]}"
#define EPISODE(id,name,s,e) "{\"Id\":\"" id "\",\"Name\":\"" name "\",\"Type\":\"Episode\",\"SeriesName\":\"The \\\"Quoted\\\" Show\",\"SeriesId\":\"" ID_S1 \
    "\",\"SeriesPrimaryImageTag\":\"x\",\"ParentIndexNumber\":" s ",\"IndexNumber\":" e ",\"RunTimeTicks\":14400000000,\"ImageTags\":{\"Primary\":\"p\"},\"UserData\":{\"PlaybackPositionTicks\":0,\"Played\":false}}"
#define SERIES(id,name,tags) "{\"Id\":\"" id "\",\"Name\":\"" name "\",\"Type\":\"Series\",\"IsFolder\":true," tags "\"UserData\":{\"Played\":false}}"
static const char *fake_body(const char *path,int *status) {
    *status=200;
    if(!strncmp(path,"/UserViews",10))
        return "{\"Items\":[{\"Id\":\"" ID_MOVIES "\",\"Name\":\"Movies\",\"Type\":\"CollectionFolder\",\"IsFolder\":true,\"ImageTags\":{\"Primary\":\"v\"}},"
               "{\"Id\":\"" ID_SHOWS "\",\"Name\":\"Shows\",\"Type\":\"CollectionFolder\",\"IsFolder\":true}]}";
    if(!strncmp(path,"/UserItems/Resume",17))
        return "{\"Items\":[" MOVIE(ID_M1,"Tab\\tMovie",  "1999","6000000000","false") "," EPISODE(ID_E1,"Pilot","1","1") "]}";
    if(!strncmp(path,"/Shows/NextUp",13))
        return "{\"Items\":[" EPISODE(ID_E2,"Second","1","2") "]}";
    if(strstr(path,"/Items/Latest?")&&strstr(path,"parentId=" ID_MOVIES))
        return "[" MOVIE(ID_M1,"Tab\\tMovie","1999","6000000000","false") "," MOVIE(ID_M2,"Zweiter Film \xc3\xa9","2001","0","true") "," MOVIE(ID_M3,"Third","0","0","false") "]";
    if(strstr(path,"/Items/Latest?")&&strstr(path,"parentId=" ID_SHOWS))
        return "[" SERIES(ID_S1,"The \\\"Quoted\\\" Show","\"ImageTags\":{\"Primary\":\"p\",\"Thumb\":\"t\"},") "," SERIES(ID_S2,"Artless","") "]";
    if(!strncmp(path,"/Items?",7)&&strstr(path,"ids=" ID_M1))
        return "{\"Items\":[{\"Id\":\"" ID_M1 "\",\"Overview\":\"A long \\\"quoted\\\" overview.\",\"Genres\":[\"Drama\",\"Comedy\",\"Thriller\",\"Fourth\"]}]}";
    if(!strncmp(path,"/Items?",7)&&strstr(path,"parentId=" ID_MOVIES))
        return "{\"Items\":[" MOVIE(ID_M1,"A","1999","0","false") "," MOVIE(ID_M2,"B","2001","0","true") "," MOVIE(ID_M3,"C","2003","0","false") "],\"TotalRecordCount\":30}";
    if(strstr(path,"/Images/")){if(strstr(path,ID_S2)){*status=404;return "";}return "JPEG";}
    *status=404;return "";
}
/* ---- platform model ---- */
#define MODEL_TX_ROOM 504u          /* = PJ_JL_TX_ROOM of main/jellyfin_esp32.c: the most the gateway leaves waiting for the wire */
#define MODEL_BUTTONS_REQUEST 0x2u  /* = PJ_TX_BUTTONS_REQUEST of shared/launcher_logic.h */
#define CREDIT_EARLIES 8u           /* how long before the previous step the stale status was written: see the credit review case */
#define CREDIT_FAST_TICKS 28u       /* a Home reply at 512 kHz and the fast UART: 26 passes with the rule before (fresh statuses) and with the rule now (stale ones, too); observed, not derived */
#define MODEL_HOLD_LATE_MS 1000u    /* see the never-pausing HOME runs: 650 ms is the worst of the 270 runs over every status phase; observed, not derived */
typedef struct {
    char path[320];int pending,deliver_at,calls;
    uint8_t queue[2048];unsigned level,max_level,mode;uint8_t faults;int fast,fast_pending;unsigned baud_changes;
    unsigned enters,leaves,heartbeats,sends,busy_every,fast_requests,decodes,packets,max_packet;int uart_fast;
    uint8_t jpeg[64];size_t jpeg_size;
    pj_syn_avi avi;size_t avi_at;int stream_open;unsigned stream_opens,wifi_bytes_per_tick;char stream_path[460];
    /* 1.3.1: the ESP32's transmit ring between the gateway and the FPGA (async), surely-sent leaves and their rates */
    int async,allow_overflow;uint8_t ring[1152];unsigned ring_used,uart_per_tick,budget,overflows,sure;int sure_rate[8];
    /* 1.3.6: deaf: the bridge is still at 524,288 baud and understands nothing said at 115,200;
     * unheard counts what was said to it then. fast_any: the cases that may ask for the fast rate while reply
     * bytes are still in the bridge's queue for the cable (a Pocket that never pauses, after the hold). */
    int deaf,fast_any;unsigned unheard;
    /* 1.3.6: strict: the bridge hears only what leaves the MCU at the bridge's own rate (wrong counts
     * the rest). ring_at_fast: the most that still waited in the ring when the MCU went to the fast rate; allow_ring:
     * the one run with the old rule, where that is not zero. poll: the launcher's own button request every 100 ms,
     * written past the gateway into the same ring, as launcher_main.c does (polls: those the bridge heard). */
    int strict,allow_ring,poll;unsigned wrong,ring_at_fast,polls;
    /* 1.3.6 (credit review): stale: the bridge's periodic status is one loop pass old when the launcher reads it. It
     * carries the queue level of just after the launcher's previous step: nothing the UART has delivered since.
     * stale 2: written a little before that step and on its way while the launcher read its UART: it lacks also what
     * the UART delivered in the last `early` wire bytes before the step (`tail`, payload bytes, kept by tick). */
    int stale;unsigned early,tail;
    /* 1.4.0: idle: UART byte times the async line has had nothing to send, as of the end of the last flush (the
     * binding's jl_idle_us, in the line's own byte times: see model_idle_us) */
    unsigned idle;
} platform;
static platform P;static pj_plink L;
static int request(void *c,const char *path){platform *p=c;CHECK(!p->pending);jf_copy(p->path,sizeof(p->path),path);p->pending=1;p->deliver_at=3;p->calls++;return 0;}
static int decode(void *c,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h,int row0,int rows) {
    platform *p=c;CHECK(size>=4&&jpeg[0]==0xff&&jpeg[1]==0xd8);CHECK(stride==w&&row0>=0&&rows>0&&row0+rows<=h);
    for(int y=0;y<rows;y++)for(int x=0;x<w;x++)dst[y*stride+x]=(uint16_t)(jpeg[2]*131u+(unsigned)(row0+y)*w+(unsigned)x);
    p->decodes++;return 0;
}
static int apply_packet(platform *p,const uint8_t *m,size_t n) {
    CHECK(n>=4&&m[0]==0x8f&&m[2]==n-4&&pj_crc8(m,n)==0);
    if(p->deaf){CHECK(!p->uart_fast);p->unheard++;return 1;} /* wrong rate: nothing of it is a packet to the bridge */
    if(p->strict&&p->uart_fast!=(p->fast||p->fast_pending)){p->wrong++;return 1;} /* 1.3.6: the same, either way round */
    if(m[1]==MODEL_BUTTONS_REQUEST){CHECK(p->poll&&m[2]==2);p->polls++;return 1;} /* the launcher's own request: nothing of the link's */
    if(m[1]==PJ_TX_PLINK_CTRL){CHECK(m[2]==5);
        /* 1.3.6: as the bridge (rtl/chromatic_pocket_link.v), an ENTER heard in media mode clears the
         * fault bits and keeps the rate (uart_fast_o falls only on reset and on leaving); outside it the rate is
         * 115,200 already. The model used to drop the rate on every ENTER. */
        if(m[3]==1){CHECK(m[4]==0x50&&m[7]==0x4b);p->enters++;if(p->mode!=2)p->fast=0;p->mode=2;p->faults=0;}
        else if(m[3]==2){p->leaves++;p->mode=0;p->fast=0;}
        else if(m[3]==4){CHECK(m[4]==0x50&&p->mode==2);p->fast_requests++;p->fast_pending=1;}
        else{CHECK(m[3]==3);p->heartbeats++;}
        return 1;}
    CHECK(m[1]==PJ_TX_PLINK_DATA&&m[2]>=1&&m[2]<=PJ_PLINK_DATA_BYTES&&p->mode==2);
    if(p->level+m[2]>PJ_PLINK_QUEUE){CHECK(p->allow_overflow);p->overflows++;return 1;} /* dropped by the FPGA */
    CHECK(p->level+m[2]<=PJ_PLINK_QUEUE); /* the credit rule must hold */
    memcpy(p->queue+p->level,m+3,m[2]);p->level+=m[2];if(p->level>p->max_level)p->max_level=p->level;
    p->packets++;if(m[2]>p->max_packet)p->max_packet=m[2];return 1;
}
/* The gateway's view of the UART: synchronous (each packet arrives at once), or async (1.3.1): packets wait in the
 * ESP32's transmit ring and reach the FPGA at the UART's rate, uart_per_tick bytes per 10 ms tick. */
static int fpga_send(void *c,const uint8_t *m,size_t n) {
    platform *p=c;p->sends++;
    if(p->busy_every&&p->sends%p->busy_every==0)return 0;
    if(m[1]==PJ_TX_PLINK_CTRL&&m[3]==3)CHECK(L.fast!=PJ_PF_REQUESTED); /* no heartbeat while the rates may differ */
    if(m[1]==PJ_TX_PLINK_CTRL&&m[3]==1)CHECK(L.fast!=PJ_PF_REQUESTED); /* nor the ENTER that clears a fault bit */
    /* 1.3.6: the fast rate is asked for only while nothing is owed (no request being worked,
     * no reply being sent), in every scenario of this check; and, but for the never-pausing cases, only once
     * nothing of a reply is left in the bridge's queue. Only ever at 115,200 and in link mode.
     * And never behind anything in the MCU's transmit ring, in every case but the one run of the old
     * rule: a request queued behind a reply's tail left after the MCU had changed its rate. */
    if(m[1]==PJ_TX_PLINK_CTRL&&m[3]==4) {
        CHECK(!L.tx_active&&L.work==PJ_PW_NONE&&!L.http_busy&&L.state==PJ_PL_READY&&!p->uart_fast);
        if(!p->allow_ring)CHECK(!p->ring_used);
        if(!p->fast_any)CHECK(!p->level);
    }
    /* as jl_fpga_send, nothing more once MODEL_TX_ROOM bytes wait (the ring itself is never full) */
    if(p->async){if(p->ring_used+n>MODEL_TX_ROOM)return 0;memcpy(p->ring+p->ring_used,m,n);p->ring_used+=(unsigned)n;return 1;}
    return apply_packet(p,m,n);
}
static void uart_flush(platform *p,int all) {
    if(!p->ring_used){p->budget=p->uart_per_tick;return;}
    while(p->ring_used) {
        unsigned len=p->ring[2]+4u;if(!all&&p->budget<len)break;
        apply_packet(p,p->ring,len);memmove(p->ring,p->ring+len,p->ring_used-len);p->ring_used-=len;if(!all)p->budget-=len;
    }
}
static int send_sure(void *c,const uint8_t *m,size_t n) { /* drained, sent, drained: never dropped */
    /* a ring of the last eight (it used to keep only the first eight of the whole run, so the later
     * checks read entries written long before) */
    platform *p=c;uart_flush(p,1);p->sure_rate[p->sure%8]=p->uart_fast;p->sure++;return apply_packet(p,m,n);
}
/* Before a leave whose rates are checked: no entry of the ring can pass for a new one. */
static void sure_mark(void){for(unsigned i=0;i<8;i++)P.sure_rate[i]=-1;}
/* 1.3.6 (credit review): to the byte, as the binding counts it: of the packet at the head of the ring, the bytes the
 * UART has already sent (the budget a flush leaves over) no longer wait. The bridge still queues that packet only at
 * its last byte: the credit's allowance for the packet in flight is seen by the 113-bytes-a-pass runs below.
 * Credit fix: shift_short 1 reports one byte less, as jl_driver_pending cannot see the byte in the UART's
 * shift register (and jl_line leaves out the launcher's own packets): the count can read 0 with a last byte still out. */
static unsigned shift_short;
static size_t tx_pending(void *c){platform *p=c;size_t n=p->ring_used?p->ring_used-(p->async&&p->budget<p->ring_used?p->budget:0u):0u;return n>shift_short?n-shift_short:0u;}
/* 1.4.0: how long the line has been idle (jl_idle_us). The model's UART runs uart_per_tick bytes a pass whatever rate
 * the gateway believes in, so its idle byte times are given at the rate the gateway converts with: "idle for N byte
 * times" means the same to both, which is what PJ_PLINK_IDLE_BYTES is about. 0 with anything in the ring. */
static unsigned idle_cleared;
static uint32_t model_idle_us(void *c) {
    platform *p=c;if(!p->async||p->ring_used)return 0;
    if(p->idle>=PJ_PLINK_IDLE_BYTES)idle_cleared++; /* the gateway asks only when it would clear its allowance on a yes */
    return (uint32_t)((uint64_t)p->idle*10000000u/(L.fast==PJ_PF_FAST?PJ_TETHER_UART_FAST_BAUD:PJ_TETHER_UART_SLOW_BAUD));
}
static uint32_t model_clock;static uint32_t model_micros(void *c){(void)c;return model_clock+=137u;}
static int stream_open(void *c,const char *path){platform *p=c;CHECK(!p->stream_open);jf_copy(p->stream_path,sizeof(p->stream_path),path);p->stream_open=1;p->stream_opens++;p->avi_at=0;return 0;}
static int stream_read(void *c,uint8_t *buf,size_t max){
    platform *p=c;CHECK(p->stream_open);
    if(p->avi_at>=p->avi.size)return -1;
    size_t n=p->avi.size-p->avi_at;if(n>max)n=max;if(n>p->wifi_bytes_per_tick)n=p->wifi_bytes_per_tick;
    memcpy(buf,p->avi.data+p->avi_at,n);p->avi_at+=n;return (int)n;
}
static void stream_close(void *c){platform *p=c;p->stream_open=0;}
/* 1.3.6, as jl_set_baud: to the fast rate only with nothing left to leave at the old one (the gateway
 * must not ask otherwise: checked here, in every case but the one run of the old rule); back to 115,200 once the line
 * has emptied, at the rate it was queued at. */
static void set_baud(void *c,int fast) {
    platform *p=c;
    if(fast){if(p->ring_used>p->ring_at_fast)p->ring_at_fast=p->ring_used;CHECK(p->allow_ring||!p->ring_used);}
    else if(!p->allow_ring)uart_flush(p,1);
    p->baud_changes++;p->uart_fast=fast;if(!fast)p->fast_pending=0;
}
/* The Pocket side of the cable: pops `n` bytes, reassembles PJB1 replies. */
static pj_wire_rx pocket_rx;static uint8_t pocket_frame[PJ_RPC_HEADER+PJ_RPC_LIMIT];static size_t reply_size;static int replies;
/* 1.5.0: the Pocket's side of the ACK (pocket/rpc_client.h): every byte taken off the cable counted, the anchor at the
 * closing 7E of the last whole valid reply, dropped by a damaged frame. */
static uint32_t pk_count,pk_anchor_at;static uint8_t pk_anchor_ok,pk_anchor_seq;
static void drain(unsigned n) {
    if(n>P.level)n=P.level;
    for(unsigned i=0;i<n;i++){size_t s=0;int got=pj_wire_rx_byte(&pocket_rx,P.queue[i],&s);pk_count++;
        if(got>0&&pj_rpc_validate(pocket_frame,s)){pk_anchor_ok=1;pk_anchor_seq=pocket_frame[8];pk_anchor_at=pk_count;}
        else if(got)pk_anchor_ok=0;
        if(P.allow_overflow&&(got<0||(got>0&&!pj_rpc_validate(pocket_frame,s))))continue; /* bytes the FPGA dropped: a damaged frame */
        CHECK(got>=0);if(got){CHECK(pj_rpc_validate(pocket_frame,s));reply_size=s;replies++;}}
    memmove(P.queue,P.queue+n,P.level-n);P.level-=n;
}
static void status(uint32_t now,const uint8_t *up,unsigned n) {
    uint8_t s[10]={(uint8_t)(P.mode|(P.faults<<2)|(P.fast?PJ_TETHER_STATUS_FAST_BIT:0)),(uint8_t)n,(uint8_t)(P.level>>8),(uint8_t)P.level,255,255,255,255,255,255};
    if(n)memcpy(s+4,up,n);
    CHECK(pj_plink_fpga_packet(&L,PJ_RX_PLINK_STATUS,s,10,now)==1);
}
static uint32_t now_ms;
/* 1.3.6: when the bridge's periodic status packet falls (every status_period ms, at status_phase within
 * it; 50 and 0 everywhere but in the never-pausing HOME runs, which go through every phase), and when the bridge
 * last heard a request for the fast rate (fast_heard_ms, set by tick). */
static unsigned status_period=50,status_phase,fast_heard;static uint32_t fast_heard_ms;
/* ---- launcher 1.3.6 review: what the console is offered, by the link's view ----
 * The real gateway and the real printer through pocket_link.h pj_plink_say_periodic/_event, the calls of
 * jellyfin_esp32.c. Every modelled loop pass offers every line (launcher_main.c offers the periodic ones only after
 * its 5 s line: this is stricter). Linked (connected, syncing, streaming): nothing but the short sum, at least 30 s
 * apart and without a byte in hex, and a refused frame, at least 10 s apart. */
static int gate_on;static pj_pld_printer gate_pr;static pj_pld_uart gate_uart;
static unsigned gate_saw,gate_free,gate_linked,gate_rejects;static uint32_t gate_linked_ms,gate_reject_ms;
static const char *const gate_names[10]={"plink rx bytes=","plink rx bad ","plink rx link ","plink rx uart ","plink rx cap ",
    "plink rx ring ","plink rx first ","plink cap ","plink reject ","plink rx linked "};
static void gate_line(const char *body,int linked) {
    char line[PJ_DIAG_MAX+64];unsigned k=0;
    CHECK(strlen(body)+24u<PJ_DIAG_MAX&&pj_diag_line(line,sizeof(line),0xffffffffu,body)>0);
    while(k<10&&strncmp(body,gate_names[k],strlen(gate_names[k])))k++;
    CHECK(k<10);gate_saw|=1u<<k;
    if(!linked){CHECK(k!=9);gate_free++;return;}
    CHECK(k==9||k==8);
    if(k==9) {
        CHECK(!strstr(body,"hex=")&&!strstr(body,"head=")&&strlen(body)<96);
        CHECK(!gate_linked||(uint32_t)(now_ms-gate_linked_ms)>=30000u);gate_linked++;gate_linked_ms=now_ms;
    }
    else{CHECK(!gate_rejects||(uint32_t)(now_ms-gate_reject_ms)>=10000u);gate_rejects++;gate_reject_ms=now_ms;}
}
static void gate_pass(void) {
    char body[PJ_DIAG_MAX];int linked=pj_plink_linked(&L,now_ms);
    for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)if(pj_plink_say_periodic(&L,&gate_pr,&gate_uart,now_ms,i,body,sizeof(body)))gate_line(body,linked);
    for(unsigned i=0;i<4&&pj_plink_say_event(&L,&gate_pr,now_ms,body,sizeof(body));i++)gate_line(body,linked);
}
/* (a frame this link refused earlier in the check counts as said: the launcher's printer begins with the link) */
static void gate_begin(void){pj_pld_printer_init(&gate_pr,now_ms);gate_pr.rej_seq=L.d.rej_seq;gate_saw=gate_free=gate_linked=gate_rejects=0;gate_on=1;}
static unsigned gate_once(void){gate_begin();gate_on=0;gate_pass();return gate_saw;}
/* One modelled 10 ms loop: HTTP delivery in pieces, link drain, a status packet every 50 ms. */
static void tick(unsigned drain_bytes) {
    unsigned stale_level=P.level; /* 1.3.6 (credit review): the queue as it stood just after the last step */
    if(P.stale==2)stale_level-=P.tail<stale_level?P.tail:stale_level; /* ...or `early` UART bytes before it */
    now_ms+=10;
    if(P.fast_pending){P.fast_pending=0;P.fast=1;} /* the FPGA switches within the loop period once TX is idle */
    if(P.async) {
        int was_idle=!P.ring_used;
        P.budget+=P.uart_per_tick;P.tail=0;
        if(P.stale==2&&P.ring_used) { /* the pass's UART bytes in two parts: all but the last `early`, then those */
            unsigned late=P.budget<P.early?P.budget:P.early,at;
            P.budget-=late;uart_flush(&P,0);at=P.level;P.budget+=late;if(P.ring_used)uart_flush(&P,0);
            P.tail=P.level-at;
        }
        else uart_flush(&P,0);
        /* 1.4.0: the line's idle time: what this pass's budget left unused once the ring was empty */
        if(P.ring_used)P.idle=0;
        else P.idle=(was_idle?P.idle:0u)+(P.budget<P.uart_per_tick?P.budget:P.uart_per_tick);
    }
    if(P.pending&&--P.deliver_at==0) {
        int st;const char *body=fake_body(P.path,&st);
        if(!strcmp(body,"JPEG")){P.jpeg_size=8;P.jpeg[0]=0xff;P.jpeg[1]=0xd8;P.jpeg[2]=(uint8_t)P.path[7];pj_plink_reply_data(&L,P.jpeg,P.jpeg_size);}
        else for(size_t at=0,n=strlen(body);at<n;at+=700)pj_plink_reply_data(&L,(const uint8_t *)body+at,n-at<700?n-at:700);
        P.pending=0;pj_plink_reply(&L,st);
    }
    drain(drain_bytes);
    if(now_ms%status_period==status_phase) {
        if(P.stale){unsigned real=P.level;P.level=stale_level;status(now_ms,NULL,0);P.level=real;} /* written a pass ago, read now */
        else status(now_ms,NULL,0);
    }
    /* 1.3.6: the launcher's own button request, every 100 ms, written before the gateway's step and
     * not while the link's rate changes (launcher_main.c: pj_jl_rate_changing) */
    if(P.async&&P.poll&&now_ms%100==0&&!pj_plink_rate_changing(&L)) {
        uint8_t ask[6]={0x8f,MODEL_BUTTONS_REQUEST,2,0,0,0};ask[5]=pj_crc8(ask,5);
        CHECK(P.ring_used+6u<=sizeof(P.ring));memcpy(P.ring+P.ring_used,ask,6);P.ring_used+=6;
    }
    CHECK(pj_plink_step(&L,now_ms)==0);
    if(P.fast_requests!=fast_heard){fast_heard=P.fast_requests;fast_heard_ms=now_ms;}
    if(gate_on)gate_pass();
}
static uint32_t seq;static uint8_t req[1040];static size_t req_size;
static void send_request(unsigned command,const char *json,int fresh) {
    if(fresh)req_size=pj_rpc_encode(req,sizeof(req),command,++seq,json,strlen(json));
    uint8_t wire[2100];pj_wire_tx t;CHECK(pj_wire_tx_init(&t,req,req_size));size_t n=0;
    while(pj_wire_tx_next(&t,&wire[n]))n++;
    for(size_t at=0;at<n;at+=6){unsigned k=n-at<6?(unsigned)(n-at):6;status(now_ms,wire+at,k);}
}
static const uint8_t *rpc(unsigned command,const char *json,unsigned drain_bytes,size_t *size) {
    int before=replies;send_request(command,json,1);
    for(int i=0;i<4000&&replies==before;i++)tick(drain_bytes);
    CHECK(replies==before+1);CHECK(pj_av_u32(pocket_frame+8)==seq&&pocket_frame[4]==command);
    *size=reply_size-PJ_RPC_HEADER;return pocket_frame+PJ_RPC_HEADER;
}
static pj_live_library lib;static pj_art_download art;static pj_details details;
static pj_json_token tok[64];
static uint64_t json_num(const uint8_t *p,size_t n,const char *key){pj_json j;uint64_t v=0;CHECK(pj_json_parse(&j,p,n,tok,64));CHECK(pj_json_u64(&j,pj_json_find(&j,0,key),&v));return v;}

/* PJAV consumer: the Pocket's stream_reader feeding a receiver, counting kinds. */
static pj_av_receiver rx;static unsigned n_cfg,n_vid,n_aud,n_end;static uint64_t last_vpts,last_apts;
/* 1.3.9: every picture is one the Pocket's decoder takes: a JPEG whose own size is the CONFIG's, with no Huffman tables
 * (the decoder's are Annex K, in ROM), and one that the fit takes again unchanged (so its codes are Annex K). */
static pj_fit_work push_fit;
static void pocket_ready(const uint8_t *j,size_t n) {
    static uint8_t again[PJ_PLINK_BUF];size_t at=2,m=0;unsigned w=0,h=0;
    CHECK(n>4&&n<=sizeof(again)&&j[0]==0xff&&j[1]==0xd8&&j[n-2]==0xff&&j[n-1]==0xd9);
    while(at+4<=n&&j[at]==0xff&&j[at+1]!=0xda){CHECK(j[at+1]!=0xc4);if(j[at+1]==0xc0){h=((unsigned)j[at+5]<<8)|j[at+6];w=((unsigned)j[at+7]<<8)|j[at+8];}at+=2u+(((size_t)j[at+2]<<8)|j[at+3]);}
    CHECK(w==rx.width&&h==rx.height);
    if(2u*n+PJ_FIT_HEADROOM<=sizeof(again)){memcpy(again,j,n);CHECK(pj_fit_jpeg(again,n,sizeof(again),w,h,0,&push_fit,&m)==PJ_FIT_OK&&m==n&&!memcmp(again,j,n));}
}
static int push(const uint8_t *p,size_t n){int r=pj_av_receive(&rx,p,p+32,n-32);CHECK(r>=0);
    if(p[5]==PJ_AV_CONFIG)n_cfg++;else if(p[5]==PJ_AV_VIDEO){n_vid++;last_vpts=pj_av_u64(p+16);pocket_ready(p+32,n-32);}else if(p[5]==PJ_AV_AUDIO){n_aud++;last_apts=pj_av_u64(p+16);}else n_end++;return 1;}

/* ---- launcher 1.3.6: link diagnostics (shared/pocket_link_diag.h) ----
 * Everything goes the way it does on the console: a V2 status packet with its CRC-8, byte by byte through the
 * launcher's packet parser (pj_plink_rx_byte), then pj_plink_fpga_packet; at most six upstream bytes a packet. */
static pj_plink_rx diag_rx;static unsigned diag_packets;static uint32_t diag_seq=0x00030000u;
/* damage: flip one bit of byte damage-1 of the packet. Returns the packets the gateway was given (0 or 1). */
static int status_v2(uint32_t now,const uint8_t *up,unsigned n,unsigned damage) {
    uint8_t s[10]={(uint8_t)(P.mode|(P.faults<<2)|(P.fast?PJ_TETHER_STATUS_FAST_BIT:0)),(uint8_t)n,(uint8_t)(P.level>>8),(uint8_t)P.level,255,255,255,255,255,255},m[16];
    int took=0;
    if(n)memcpy(s+4,up,n);
    size_t size=pj_plink_encode(m,PJ_RX_PLINK_STATUS,s,10);CHECK(size==14);
    if(damage)m[damage-1]^=0x10;
    for(size_t i=0;i<size;i++) {
        unsigned address=0;const uint8_t *payload=NULL;size_t len=0;
        if(pj_plink_rx_byte(&diag_rx,m[i],&address,&payload,&len)) {
            CHECK(address==PJ_RX_PLINK_STATUS&&len==10);CHECK(pj_plink_fpga_packet(&L,address,payload,len,now)==1);took++;
        }
    }
    return took;
}
/* Upstream bytes as the FPGA hands them over: 1, 2, 3, 4, 5, 6, 1, ... bytes a status packet. */
static void upstream(const uint8_t *bytes,unsigned n) {
    unsigned k=1;
    for(unsigned at=0;at<n;k=k%6u+1u) {
        unsigned c=n-at<k?n-at:k;
        CHECK(status_v2(now_ms,bytes+at,c,0)==1);diag_packets++;at+=c;
    }
}
static unsigned wire_of(const uint8_t *raw,size_t size,uint8_t *wire) {
    pj_wire_tx t;unsigned k=0;CHECK(pj_wire_tx_init(&t,raw,size));
    while(pj_wire_tx_next(&t,&wire[k]))k++;
    return k;
}
/* The Pocket's HELLO, a new sequence number each time (its low byte 0, so the bytes after the known start are
 * the same in every run). */
static void hello_raw(uint8_t *raw){diag_seq+=0x100u;CHECK(pj_rpc_encode(raw,32,PJ_RPC_HELLO,diag_seq,"{}",2)==18);}
static unsigned hello_wire(uint8_t *wire){uint8_t raw[32];hello_raw(raw);return wire_of(raw,18,wire);}
static unsigned to_bits(const uint8_t *bytes,unsigned n,uint8_t *bits) {
    for(unsigned i=0;i<n*8u;i++)bits[i]=(uint8_t)((bytes[i>>3]>>(7u-(i&7u)))&1u);
    return n*8u;
}
/* The FPGA's byte engine, written again here (not the header's): MSB first, whole bytes only, idle bytes dropped. */
static unsigned engine(const uint8_t *bits,unsigned n,uint8_t *out) {
    unsigned k=0;
    for(unsigned i=0;i+8u<=n;i+=8u) {
        unsigned v=0;
        for(unsigned j=0;j<8;j++)v=(v<<1)|bits[i+j];
        if(v!=255u)out[k++]=(uint8_t)v;
    }
    return k;
}
static void diag_fresh(void) {
    memset(&L.d,0,sizeof(L.d));CHECK(pj_wire_rx_init(&L.upstream,L.incoming,sizeof(L.incoming)));diag_packets=0;
}
static void settle(void){for(int i=0;i<30;i++)tick(64);}
/* The Pocket sends these bytes after a quiet line; 300 ms later the capture is complete. */
static pj_pld_result heard(const uint8_t *bytes,unsigned n) {
    unsigned k=n<PJ_PLD_CAP?n:PJ_PLD_CAP;
    diag_fresh();upstream(bytes,n);settle();
    CHECK(L.state==PJ_PL_READY&&L.d.up_bytes==n&&L.d.up_packets==diag_packets&&L.d.fed==n);
    CHECK(L.d.done_seq==1&&L.d.done_n==k&&!memcmp(L.d.done,bytes,k));
    return L.d.done_result;
}
static pj_pld_result heard_bits(const uint8_t *bits,unsigned n){uint8_t bytes[96];unsigned k=engine(bits,n,bytes);return heard(bytes,k);}
static int is_code(pj_pld_result r,const char *code,unsigned bits,unsigned at) {
    char text[16];
    if(strcmp(pj_pld_code(r,text,sizeof(text)),code)||r.bits!=bits||r.at!=at) {
        fprintf(stderr,"analysis: got %s bits=%u at=%u, expected %s bits=%u at=%u\n",text,(unsigned)r.bits,(unsigned)r.at,code,bits,at);return 0;
    }
    return 1;
}
static int says(pj_pld_hint h,const char *a,const char *b) {
    char x[40],y[40];pj_pld_sentence(h,x,sizeof(x),y,sizeof(y));
    if(strcmp(x,a)||strcmp(y,b)){fprintf(stderr,"sentence: got \"%s\" / \"%s\"\n",x,y);return 0;}
    return 1;
}
/* A body the launcher may print: it fits a diagnostics line and passes its rules (launcher_diag.h) at any time. */
static int line_ok(const char *body) {
    char line[PJ_DIAG_MAX+64];
    if(strlen(body)<PJ_DIAG_MAX&&pj_diag_line(line,sizeof(line),0xffffffffu,body)>0)return 1;
    fprintf(stderr,"unsafe line: %s\n",body);return 0;
}
static int is_line(const char *line,const char *want) {
    if(!strcmp(line,want)&&line_ok(line))return 1;
    fprintf(stderr,"line: got \"%s\"\n      expected \"%s\"\n",line,want);return 0;
}
static void cap_once(pj_plink_diag *d,const uint8_t *bytes,unsigned n,uint32_t t) {
    for(unsigned i=0;i<n;i++)pj_pld_byte(d,bytes[i],t);
    pj_pld_poll(d,t+PJ_PLD_SETTLE_MS);
}
static void diag_checks(pj_plink_ops ops,pj_plink_extra extra) {
    uint8_t wire[48],raw[32],bits[400],mod[420],bytes[96];unsigned n,nb;pj_pld_result r;pj_plink_status v;char line[PJ_DIAG_MAX];
    CHECK(sizeof(pj_plink_diag)<=320); /* the whole of it lives inside pj_plink: no notable heap growth */
    P.async=0;P.allow_overflow=0;P.busy_every=0;P.faults=0;P.fast=0;P.fast_pending=0;P.mode=0;P.level=0;
    pj_plink_stop(&L);pj_wire_rx_init(&pocket_rx,pocket_frame,sizeof(pocket_frame));memset(&diag_rx,0,sizeof(diag_rx));
    CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
    for(int i=0;i<12;i++)tick(64);
    CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST);

    /* Nothing heard. */
    diag_fresh();v=pj_plink_view(&L,now_ms);
    CHECK(v.what==PJ_PLV_WAITING&&v.heard==0&&v.good==0&&v.bad==0&&v.hint.what==PJ_PLH_NOTHING&&says(v.hint,"No data from the Pocket yet.",""));
    CHECK(is_code(pj_pld_analyse(NULL,0),"empty",0,0)&&is_code(pj_pld_analyse(wire,0),"empty",0,0));

    /* The Pocket's HELLO, intact: counted, captured, recognised, answered. */
    {
        int before=replies;uint8_t back[2200];
        n=hello_wire(wire);CHECK(n>=21&&n<PJ_PLD_CAP&&!memcmp(wire,pj_pld_ref,PJ_PLD_REF));
        diag_fresh();upstream(wire,n);
        CHECK(L.d.up_bytes==n&&L.d.up_packets==diag_packets&&diag_packets>=6&&L.d.flags==2&&L.d.fed==n);
        CHECK(L.d.frames_ok==1&&L.d.frames_bad==0&&L.d.torn==0&&L.d.dropped==0&&L.d.replies==1);
        v=pj_plink_view(&L,now_ms); /* the capture is still open: the accepted frame already says "correct" */
        CHECK(v.what==PJ_PLV_CONNECTED&&v.heard==n&&v.good==1&&v.bad==0&&v.hint.what==PJ_PLH_GOOD&&!L.d.done_seq);
        settle();
        CHECK(replies==before+1&&pocket_frame[4]==PJ_RPC_HELLO&&pocket_frame[5]==1&&pj_av_u32(pocket_frame+8)==diag_seq);
        CHECK(L.d.down_bytes==wire_of(pocket_frame,reply_size,back)&&L.d.replies==1);
        CHECK(L.d.done_seq==1&&L.d.done_n==n&&!memcmp(L.d.done,wire,n)&&is_code(L.d.done_result,"exact",72,0));
        v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_GOOD&&says(v.hint,"Pocket heard, data correct.",""));
    }
    /* The same HELLO with the receiver's byte boundary 1..7 bits late (idle ones before it) and 1..7 bits early (its
     * first bits lost). Late by d and early by 8-d are the same bytes: the smaller count is named. */
    {
        static const char *const late[7]={"shift+1","shift+2","shift+3","shift+4","shift-3","shift-2","shift-1"};
        static const char *const early[7]={"shift-1","shift-2","shift-3","shift+4","shift+3","shift+2","shift+1"};
        for(unsigned d=1;d<8;d++) {
            nb=to_bits(wire,hello_wire(wire),bits);
            for(unsigned i=0;i<d;i++)mod[i]=1;
            memcpy(mod+d,bits,nb);
            for(unsigned i=0;i<8u-d;i++)mod[d+nb+i]=1;
            r=heard_bits(mod,nb+8u);CHECK(is_code(r,late[d-1],72,0));
            CHECK(L.d.frames_ok==0&&L.d.replies==0&&L.d.down_bytes==0);
            v=pj_plink_view(&L,now_ms);CHECK(v.good==0&&v.hint.what==PJ_PLH_SHIFT&&v.hint.arg==(d<=4?(int)d:(int)d-8));
            if(d==1)CHECK(says(v.hint,"Pocket heard, data scrambled","(shifted 1 bit late)."));
            if(d==6)CHECK(says(v.hint,"Pocket heard, data scrambled","(shifted 2 bits early)."));
        }
        for(unsigned e=1;e<8;e++) {
            nb=to_bits(wire,hello_wire(wire),bits);
            memcpy(mod,bits+e,nb-e);
            for(unsigned i=0;i<e;i++)mod[nb-e+i]=1;
            r=heard_bits(mod,nb);CHECK(is_code(r,early[e-1],64,0));
            CHECK(L.d.frames_ok==0&&L.d.replies==0);
            v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_SHIFT&&v.hint.arg==(e<4?-(int)e:8-(int)e));
        }
    }
    /* Every bit inverted; the bit order reversed in every byte. */
    nb=to_bits(wire,hello_wire(wire),bits);
    for(unsigned i=0;i<nb;i++)mod[i]=!bits[i];
    r=heard_bits(mod,nb);CHECK(is_code(r,"inverted",56,0)&&L.d.frames_ok==0);
    v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_INVERTED&&says(v.hint,"Pocket heard, data scrambled","(every bit flipped)."));
    nb=to_bits(wire,hello_wire(wire),bits);
    for(unsigned i=0;i<nb;i++)mod[i]=bits[(i&~7u)+7u-(i&7u)];
    r=heard_bits(mod,nb);CHECK(is_code(r,"reversed",72,0)&&L.d.frames_ok==0);
    v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_REVERSED&&says(v.hint,"Pocket heard, data scrambled","(bit order reversed)."));
    /* One bit lost, one bit doubled, at every position of the known start. Bits of equal value in a row cannot be told
     * apart, so the first of the row is named. Losing the very first bit is "one bit early"; a doubled bit inside the
     * flag is "one bit late" from the second byte on; past bit 62 the known start is whole. */
    for(unsigned kind=0;kind<2;kind++)for(unsigned p=0;p<72;p++) {
        unsigned k=0,first=p;char want[16];
        nb=to_bits(wire,hello_wire(wire),bits);
        for(unsigned i=0;i<nb;i++) {
            if(!kind&&i==p)continue;
            mod[k++]=bits[i];
            if(kind&&i==p)mod[k++]=bits[i];
        }
        while(k&7u)mod[k++]=1;
        while(first&&bits[first-1]==bits[p])first--;
        r=heard_bits(mod,k);
        if(p>=63)CHECK(is_code(r,"exact",72,0));
        else if(!kind) {
            if(p)snprintf(want,sizeof(want),"slip@bit%u",first);else snprintf(want,sizeof(want),"shift-1");
            CHECK(is_code(r,want,64,0));
        }
        else if(p<=8)CHECK(is_code(r,"shift+1",64,1));
        else {snprintf(want,sizeof(want),"dup@bit%u",first);CHECK(is_code(r,want,72,0));}
        CHECK(L.d.frames_ok==0);
        if(p==13) {
            v=pj_plink_view(&L,now_ms);CHECK(first==12&&v.hint.what==(kind?PJ_PLH_DUP:PJ_PLH_SLIP)&&v.hint.arg==12);
            CHECK(says(v.hint,"Pocket heard, data scrambled",kind?"(extra bit near bit 12).":"(lost a bit near bit 12)."));
        }
    }
    /* A line stuck low; noise. */
    memset(bytes,0,8);r=heard(bytes,8);CHECK(is_code(r,"zeros",0,0)&&L.d.dropped==8&&L.d.flags==0);
    v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_ZEROS&&says(v.hint,"Pocket line reads only zeros","(check the cable)."));
    {
        static const uint8_t noise[10]={0x13,0x57,0x9b,0xdf,0x24,0x68,0xac,0xe0,0x12,0x34};
        r=heard(noise,10);CHECK(is_code(r,"none",3,5)&&L.d.dropped==10&&L.d.flags==0&&L.d.torn==0);
        v=pj_plink_view(&L,now_ms);CHECK(v.heard==10&&v.bad==0&&v.hint.what==PJ_PLH_UNKNOWN&&says(v.hint,"Pocket heard, data scrambled","(no pattern, 3 bits right)."));
    }
    /* The start arrives and the rest never does. */
    n=hello_wire(wire);r=heard(wire,12);CHECK(is_code(r,"exact",72,0)&&L.d.flags==1&&L.d.frames_ok==0&&L.d.frames_bad==0&&L.d.torn==0);
    v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_CUT&&says(v.hint,"Pocket heard, start correct,","message cut short."));
    /* 1.3.6 review: fewer than four bytes prove nothing about the rest. Three bytes of the known start are "short",
     * not "exact" (the bits that agree are still said); four are judged. */
    n=hello_wire(wire);r=heard(wire,3);CHECK(is_code(r,"short",24,0)&&L.d.flags==1&&L.d.frames_ok==0&&L.d.frames_bad==0);
    v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_SHORT&&says(v.hint,"Pocket heard, too few bytes","to judge."));
    r=heard(wire,1);CHECK(is_code(r,"short",8,0));
    r=heard(wire,4);CHECK(is_code(r,"exact",32,0));v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_CUT);
    {
        static const uint8_t two[2]={0x12,0x34},nil[3]={0,0,0};char code[16];
        r=heard(two,2);CHECK(!strcmp(pj_pld_code(r,code,sizeof(code)),"short")&&r.bits<24);
        v=pj_plink_view(&L,now_ms);CHECK(v.hint.what==PJ_PLH_SHORT);
        r=heard(nil,3);CHECK(!strcmp(pj_pld_code(r,code,sizeof(code)),"short")); /* three zeros: not yet "only zeros" */
    }

    /* Frames that arrive whole and are refused (the acceptance test itself is unchanged): each reason, with the
     * frame's size and first bytes kept. */
    {
        static const uint8_t tiny[7]={0x7e,1,2,3,4,5,0x7e};
        heard(tiny,7);
        CHECK(L.d.frames_bad==1&&L.d.reject[PJ_PLR_SHORT]==1&&L.d.rej_reason==PJ_PLR_SHORT&&L.d.rej_size==5&&L.d.rej_head_n==5&&!memcmp(L.d.rej_head,tiny+1,5)&&L.d.rej_seq==1);
        hello_raw(raw);raw[0]='X';heard(wire,wire_of(raw,18,wire));
        CHECK(L.d.frames_bad==1&&L.d.reject[PJ_PLR_MAGIC]==1&&L.d.rej_reason==PJ_PLR_MAGIC&&L.d.rej_size==18&&L.d.frames_ok==0);
        hello_raw(raw);raw[6]=5;heard(wire,wire_of(raw,18,wire));
        CHECK(L.d.frames_bad==1&&L.d.reject[PJ_PLR_LENGTH]==1&&L.d.rej_reason==PJ_PLR_LENGTH&&L.d.frames_ok==0);
        hello_raw(raw);raw[5]=2;pj_put32(raw+12,pj_av_crc_part(pj_av_crc_part(0xffffffffu,raw,12),raw+16,2)^0xffffffffu);heard(wire,wire_of(raw,18,wire));
        CHECK(L.d.frames_bad==1&&L.d.reject[PJ_PLR_NOTREQ]==1&&L.d.rej_reason==PJ_PLR_NOTREQ&&L.d.frames_ok==0&&L.d.replies==0);
        /* The case a console may show: every byte right up to the check value, the check value wrong. */
        hello_raw(raw);raw[13]^=1;r=heard(wire,wire_of(raw,18,wire));
        CHECK(is_code(r,"exact",72,0)&&L.d.frames_bad==1&&L.d.reject[PJ_PLR_CRC]==1&&L.d.rej_reason==PJ_PLR_CRC&&L.d.frames_ok==0&&L.d.replies==0);
        CHECK(L.d.rej_size==18&&L.d.rej_head_n==16&&!memcmp(L.d.rej_head,raw,16)&&L.d.torn==0&&L.d.dropped==0);
        v=pj_plink_view(&L,now_ms);CHECK(v.good==0&&v.bad==1&&v.hint.what==PJ_PLH_REJECTED&&v.hint.reason==PJ_PLR_CRC);
        CHECK(says(v.hint,"Pocket heard, data correct,","check failed (bad CRC)."));
        {
            pj_pld_printer pr;char want[PJ_DIAG_MAX],hex[80];memset(&pr,0,sizeof(pr));
            CHECK(pj_pld_event(&pr,&L.d,now_ms,0,line,sizeof(line))==1);
            snprintf(want,sizeof(want),"plink cap seq=1 n=%u code=exact bits=72 at=0 hex=%s",(unsigned)L.d.done_n,pj_pld_hex(hex,sizeof(hex),L.d.done,L.d.done_n));
            CHECK(is_line(line,want)&&!strncmp(strstr(line,"hex="),"hex=7e504a42-31010002-00",24));
            CHECK(pj_pld_event(&pr,&L.d,now_ms,0,line,sizeof(line))==1);
            snprintf(want,sizeof(want),"plink reject seq=1 reason=crc size=18 head=%s",pj_pld_hex(hex,sizeof(hex),raw,16));
            CHECK(is_line(line,want)&&!strncmp(strstr(line,"head="),"head=504a4231-01000200-",23));
            CHECK(pj_pld_event(&pr,&L.d,now_ms,0,line,sizeof(line))==0);
        }
        {
            static const char *const second[PJ_PLR_COUNT]={"check failed (refused).","check failed (too short).","check failed (bad start).",
                "check failed (bad length).","check failed (bad CRC).","check failed (not a request)."};
            for(unsigned i=0;i<PJ_PLR_COUNT;i++){pj_pld_hint h={PJ_PLH_REJECTED,0,0,(uint8_t)i};CHECK(says(h,"Pocket heard, data correct,",second[i]));}
        }
    }
    /* What the wire decoder throws away: a frame with a bad escape, a frame too short to be one, bytes outside a frame. */
    {
        static const uint8_t escape[6]={0x7e,0x50,0x7d,0x00,0x41,0x7e},runt[4]={0x7e,1,2,0x7e},stray[3]={0x11,0x22,0x7e};
        heard(escape,6);CHECK(L.d.torn==1&&L.d.dropped==3&&L.d.frames_bad==0&&L.d.frames_ok==0&&L.d.flags==2);
        v=pj_plink_view(&L,now_ms);CHECK(v.bad==1);
        heard(runt,4);CHECK(L.d.torn==1&&L.d.dropped==2&&L.d.frames_bad==0);
        heard(stray,3);CHECK(L.d.torn==0&&L.d.dropped==2&&L.d.flags==1);
    }
    /* The FPGA's fault bits are counted each time they appear, not only shown while they stick. A cable timeout
     * (bit 0) is cleared by the launcher's ENTER and comes back with the Pocket's next try; its bytes are still read. */
    {
        static const uint8_t three[3]={1,2,3};
        diag_fresh();
        P.faults=1;CHECK(status_v2(now_ms,NULL,0,0)==1);P.faults=1;CHECK(status_v2(now_ms,NULL,0,0)==1);
        CHECK(L.d.fault[0]==1&&L.state==PJ_PL_READY); /* one appearance, seen in two packets */
        P.faults=0;CHECK(status_v2(now_ms,NULL,0,0)==1);P.faults=1;CHECK(status_v2(now_ms,three,3,0)==1);
        CHECK(L.d.fault[0]==2&&L.d.up_bytes==3&&L.d.fed==3&&L.d.dropped==3);
        P.faults=0;settle();
        {unsigned enters=P.enters;P.faults=1;for(int i=0;i<60&&P.faults;i++)tick(64);CHECK(!P.faults&&P.enters==enters+1&&L.d.fault[0]==3&&L.state==PJ_PL_READY);}
        settle();CHECK(L.d.fault[0]==3&&!L.d.fault[1]&&!L.d.fault[2]&&!L.d.fault[3]);
        /* Any other fault ends the link, as before; the bytes of that packet are counted and never decoded ("unfed"). */
        P.faults=2;CHECK(status_v2(now_ms,three,3,0)==1);
        CHECK(L.state==PJ_PL_FAILED&&!strcmp(L.why,"link fault")&&L.d.fault[1]==1&&L.d.up_bytes==6&&L.d.fed==3);
        P.faults=14;CHECK(status_v2(now_ms,NULL,0,0)==1);CHECK(L.d.fault[0]==3&&L.d.fault[1]==1&&L.d.fault[2]==1&&L.d.fault[3]==1);
        {
            pj_pld_printer pr;memset(&pr,0,sizeof(pr));
            CHECK(pj_pld_periodic(&pr,&L.d,NULL,now_ms,0,0,line,sizeof(line))==1&&is_line(line,"plink rx bytes=6 pkts=2 flags=0 ok=0 bad=0 torn=0 dropped=3 unfed=3"));
            CHECK(pj_pld_periodic(&pr,&L.d,NULL,now_ms,2,0,line,sizeof(line))==1&&is_line(line,"plink rx link timeout=3 overflow=1 downdrop=1 heartbeat=1 down=0 replies=0"));
        }
        /* The launcher's retry (stop, start again) keeps the evidence; bytes that arrive while the FPGA has not
         * entered link mode yet are counted and not decoded. */
        CHECK(pj_plink_step(&L,now_ms)==-1);pj_plink_stop(&L);
        P.faults=0;P.fast=0;P.fast_pending=0;P.mode=0;
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        CHECK(L.state==PJ_PL_ARMING&&L.d.up_bytes==6&&L.d.fault[0]==3&&L.d.fault[1]==1&&L.d.up_packets==2);
        /* 1.3.6 review: a fault bit that stuck through the retry is one appearance, not two; one that went and
         * came back is a new one. */
        {
            unsigned mode=P.mode;P.mode=0;
            P.faults=14;CHECK(status_v2(now_ms,NULL,0,0)==1);
            CHECK(L.state==PJ_PL_ARMING&&L.d.fault[0]==3&&L.d.fault[1]==1&&L.d.fault[2]==1&&L.d.fault[3]==1);
            P.faults=0;CHECK(status_v2(now_ms,NULL,0,0)==1);P.faults=4;CHECK(status_v2(now_ms,NULL,0,0)==1);
            CHECK(L.state==PJ_PL_ARMING&&L.d.fault[0]==3&&L.d.fault[1]==1&&L.d.fault[2]==2&&L.d.fault[3]==1);
            P.faults=0;CHECK(status_v2(now_ms,NULL,0,0)==1);P.mode=mode;
        }
        {unsigned mode=P.mode;P.mode=0;CHECK(status_v2(now_ms,three,3,0)==1);P.mode=mode;}
        CHECK(L.state==PJ_PL_ARMING&&L.d.up_bytes==9&&L.d.fed==3);
        for(int i=0;i<12;i++)tick(64);
        CHECK(L.state==PJ_PL_READY&&L.d.up_bytes==9);
    }
    /* The packet parser counts what it throws away: a status packet whose CRC-8 fails, another packet whose CRC-8
     * fails, a length above the largest payload. A new start (the launcher's retry) keeps the counts. */
    {
        uint8_t m[16];static const uint8_t two[2]={1,2};unsigned address=0;const uint8_t *payload=NULL;size_t len=0,size;uint32_t packets=L.status_packets;
        memset(&diag_rx,0,sizeof(diag_rx));
        CHECK(status_v2(now_ms,NULL,0,5)==0&&diag_rx.crc_status==1);  /* a payload bit */
        CHECK(status_v2(now_ms,NULL,0,14)==0&&diag_rx.crc_status==2); /* a CRC bit */
        CHECK(status_v2(now_ms,NULL,0,2)==0&&diag_rx.crc_status==2&&diag_rx.crc_other==1); /* the address */
        CHECK(status_v2(now_ms,NULL,0,3)==0&&diag_rx.bad_len==1);     /* the length: 26 */
        pj_plink_rx_reset(&diag_rx);
        size=pj_plink_encode(m,PJ_RX_BUTTONS,two,2);m[4]^=1;
        for(size_t i=0;i<size;i++)CHECK(!pj_plink_rx_byte(&diag_rx,m[i],&address,&payload,&len));
        CHECK(diag_rx.crc_status==2&&diag_rx.crc_other==2&&diag_rx.bad_len==1&&L.status_packets==packets);
        CHECK(status_v2(now_ms,NULL,0,0)==1&&L.status_packets==packets+1); /* and it goes on reading */
        pj_plink_rx_reset(&diag_rx);CHECK(diag_rx.state==0&&diag_rx.crc_status==2&&diag_rx.crc_other==2&&diag_rx.bad_len==1);
    }
    /* When a capture begins and ends (also across the wrap of the millisecond clock); the ring of the last bytes. */
    {
        pj_plink_diag d;uint32_t t=0xfffffe00u;uint8_t ring[PJ_PLD_RING];
        memset(&d,0,sizeof(d));
        for(unsigned i=0;i<5;i++)pj_pld_byte(&d,(uint8_t)(i+1u),t);
        pj_pld_poll(&d,t+249u);CHECK(!d.done_seq&&d.cap_open);
        pj_pld_poll(&d,t+250u);CHECK(d.done_seq==1&&d.done_n==5&&d.done[0]==1&&d.done[4]==5&&!d.cap_open);
        pj_pld_byte(&d,6,t+300u);pj_pld_byte(&d,7,t+799u);CHECK(d.done_seq==1&&!d.cap_open&&d.up_bytes==7); /* gaps under 500 ms: the same burst */
        t+=799u+500u;CHECK(t<0x1000u);
        for(unsigned i=0;i<40;i++)pj_pld_byte(&d,(uint8_t)(0x40u+i),t); /* 32 bytes complete a capture at once */
        CHECK(d.done_seq==2&&d.done_n==32&&d.done[0]==0x40&&d.done[31]==0x5f&&!d.cap_open&&d.up_bytes==47);
        pj_pld_byte(&d,0xaa,t+1u);pj_pld_byte(&d,0xbb,t+2u);
        CHECK(pj_pld_ring_copy(&d,ring)==48&&ring[0]==2&&ring[5]==7&&ring[6]==0x40&&ring[46]==0xaa&&ring[47]==0xbb&&d.done_seq==2);
        pj_pld_byte(&d,0x11,t+600u);CHECK(d.cap_open&&d.done_seq==2);
        pj_pld_byte(&d,0x22,t+1100u);CHECK(d.cap_open&&d.done_seq==3&&d.done_n==1&&d.done[0]==0x11); /* a new burst closes the open one */
        pj_pld_poll(&d,t+1350u);CHECK(d.done_seq==4&&d.done_n==1&&d.done[0]==0x22);
        memset(&d,0,sizeof(d));for(unsigned i=0;i<3;i++)pj_pld_byte(&d,(uint8_t)(i+1u),5u);
        CHECK(pj_pld_ring_copy(&d,ring)==3&&ring[0]==1&&ring[2]==3);
    }
    /* The 5 s lines: said when they have something new, the three counter lines also every 30 s; the two immediate
     * lines: at once, at most one a second, the same finding again only every 10 s. */
    {
        pj_plink_diag d;pj_pld_printer pr;pj_pld_uart u;unsigned said;static const uint8_t start[3]={0x7e,0x50,0x4a},zeros[4]={0,0,0,0};
        memset(&d,0,sizeof(d));memset(&pr,0,sizeof(pr));memset(&u,0,sizeof(u));
#define SAY(now,uart) do{said=0;for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)if(pj_pld_periodic(&pr,&d,uart,now,i,0,line,sizeof(line))){said|=1u<<i;CHECK(line_ok(line));}}while(0)
        SAY(5000u,NULL);CHECK(said==0x0du&&is_line(line,"plink rx uart pkt-crc=0 pkt-other=0 pkt-len=0 ovf=0 full=0 parity=0 frame=0 break=0 evq=0"));
        SAY(10000u,&u);CHECK(said==0);
        SAY(29999u,&u);CHECK(said==0);
        /* "first": from the link's 30th second on, every 30 s, whether or not anything was heard */
        SAY(30000u,&u);CHECK(said==0x40u&&is_line(line,"plink rx first age=30 after=0 n=0 code=empty bits=0 at=0 hex=-"));
        SAY(34999u,&u);CHECK(said==0);
        SAY(35000u,&u);CHECK(said==0x0du);
        u.ovf=1;u.pkt_crc=2;SAY(40000u,&u);CHECK(said==0x08u&&is_line(line,"plink rx uart pkt-crc=2 pkt-other=0 pkt-len=0 ovf=1 full=0 parity=0 frame=0 break=0 evq=0"));
        cap_once(&d,start,3,40000u);
        SAY(45000u,&u);CHECK(said==0x31u&&is_line(line,"plink rx ring n=3 hex=7e504a"));
        CHECK(pj_pld_periodic(&pr,&d,&u,45000u,4,0,line,sizeof(line))==0);pr.cap_said=0;
        CHECK(pj_pld_periodic(&pr,&d,&u,45000u,4,0,line,sizeof(line))==1&&is_line(line,"plink rx cap seq=1 n=3 code=short bits=24 at=0 hex=7e504a"));
        cap_once(&d,zeros,4,46000u);
        SAY(50000u,&u);CHECK(said==0x11u&&is_line(line,"plink rx cap seq=2 n=4 code=zeros bits=0 at=0 hex=00000000")); /* the ring: not yet again */
        SAY(59999u,&u);CHECK(said==0);
        CHECK(pj_pld_periodic(&pr,&d,&u,60000u,5,0,line,sizeof(line))==1&&is_line(line,"plink rx ring n=7 hex=7e504a00-000000"));
        SAY(60000u,&u);CHECK(said==0x40u&&is_line(line,"plink rx first age=60 after=40000 n=3 code=short bits=24 at=0 hex=7e504a")); /* the FIRST capture, not the last */
        pj_pld_reject(&d,PJ_PLR_CRC,pj_pld_ref+1,8);
        SAY(64000u,&u);CHECK(said==0x03u&&is_line(line,"plink rx bad short=0 magic=0 len=0 crc=1 notreq=0"));
        SAY(64500u,&u);CHECK(said==0);
        /* 1.3.6 review: the capture line again only when no capture line was said for 30 s (the last one at 50 s) */
        SAY(79999u,&u);CHECK(said==0x0cu);
        SAY(80000u,&u);CHECK(said==0x10u&&is_line(line,"plink rx cap seq=2 n=4 code=zeros bits=0 at=0 hex=00000000"));
        SAY(90000u,&u);CHECK(said==0x40u&&is_line(line,"plink rx first age=90 after=40000 n=3 code=short bits=24 at=0 hex=7e504a"));
#undef SAY
        memset(&d,0,sizeof(d));memset(&pr,0,sizeof(pr));
        CHECK(pj_pld_event(&pr,&d,100000u,0,line,sizeof(line))==0);
        cap_once(&d,zeros,4,100000u);
        CHECK(pj_pld_event(&pr,&d,100250u,0,line,sizeof(line))==1&&is_line(line,"plink cap seq=1 n=4 code=zeros bits=0 at=0 hex=00000000"));
        CHECK(pj_pld_event(&pr,&d,100260u,0,line,sizeof(line))==0);
        cap_once(&d,zeros,4,102000u);CHECK(d.done_seq==2&&pj_pld_event(&pr,&d,102250u,0,line,sizeof(line))==0); /* the same finding 2 s later */
        cap_once(&d,pj_pld_ref,PJ_PLD_REF,104000u);
        CHECK(pj_pld_event(&pr,&d,104250u,0,line,sizeof(line))==1&&is_line(line,"plink cap seq=3 n=9 code=exact bits=72 at=0 hex=7e504a42-31010002-00"));
        cap_once(&d,zeros,4,104600u);CHECK(d.done_seq==4&&pj_pld_event(&pr,&d,104850u,0,line,sizeof(line))==0); /* a new finding, under a second after the last line */
        CHECK(pj_pld_event(&pr,&d,105250u,0,line,sizeof(line))==1&&is_line(line,"plink cap seq=4 n=4 code=zeros bits=0 at=0 hex=00000000"));
        cap_once(&d,zeros,4,110000u);CHECK(pj_pld_event(&pr,&d,110250u,0,line,sizeof(line))==0);
        cap_once(&d,zeros,4,115000u);CHECK(d.done_seq==6&&pj_pld_event(&pr,&d,115250u,0,line,sizeof(line))==1&&is_line(line,"plink cap seq=6 n=4 code=zeros bits=0 at=0 hex=00000000"));
        pj_pld_reject(&d,PJ_PLR_CRC,pj_pld_ref+1,8);
        CHECK(pj_pld_event(&pr,&d,116000u,0,line,sizeof(line))==1&&is_line(line,"plink reject seq=1 reason=crc size=8 head=504a4231-01000200"));
        pj_pld_reject(&d,PJ_PLR_CRC,pj_pld_ref+1,8);CHECK(pj_pld_event(&pr,&d,118000u,0,line,sizeof(line))==0);
        pj_pld_reject(&d,PJ_PLR_MAGIC,pj_pld_ref+2,3);
        CHECK(pj_pld_event(&pr,&d,118100u,0,line,sizeof(line))==1&&is_line(line,"plink reject seq=3 reason=magic size=3 head=4a4231"));
        pj_pld_reject(&d,PJ_PLR_MAGIC,pj_pld_ref+2,3);CHECK(pj_pld_event(&pr,&d,127000u,0,line,sizeof(line))==0);
        pj_pld_reject(&d,PJ_PLR_MAGIC,pj_pld_ref+2,3);CHECK(pj_pld_event(&pr,&d,128100u,0,line,sizeof(line))==1);
        /* 1.3.6 review: the capture said as "plink cap" at 115.25 s is not said again as "plink rx cap" before 30 s
         * have gone by; a capture first said as "plink rx cap" is not said again as "plink cap". */
        CHECK(pj_pld_periodic(&pr,&d,NULL,128200u,4,0,line,sizeof(line))==0);
        CHECK(pj_pld_periodic(&pr,&d,NULL,145249u,4,0,line,sizeof(line))==0);
        CHECK(pj_pld_periodic(&pr,&d,NULL,145250u,4,0,line,sizeof(line))==1&&is_line(line,"plink rx cap seq=6 n=4 code=zeros bits=0 at=0 hex=00000000"));
        CHECK(pj_pld_periodic(&pr,&d,NULL,145260u,4,0,line,sizeof(line))==0);
        cap_once(&d,pj_pld_ref,PJ_PLD_REF,150000u);
        CHECK(pj_pld_periodic(&pr,&d,NULL,150250u,4,0,line,sizeof(line))==1&&is_line(line,"plink rx cap seq=7 n=9 code=exact bits=72 at=0 hex=7e504a42-31010002-00"));
        CHECK(pj_pld_event(&pr,&d,150260u,0,line,sizeof(line))==0);
        /* 1.3.6 review: a link that works. One short sum every 30 s and nothing else of the periodic lines, never
         * a byte in hex; no capture line; a refused frame at once the first time, then at most one in 10 s whatever
         * its reason. A capture that completed while the link worked is not owed afterwards. */
        memset(&d,0,sizeof(d));memset(&u,0,sizeof(u));pj_pld_printer_init(&pr,200000u);
        cap_once(&d,pj_pld_ref,PJ_PLD_REF,200000u);d.frames_ok=1;d.fed=d.up_bytes;
        CHECK(pj_pld_periodic(&pr,&d,&u,200300u,0,1,line,sizeof(line))==1&&is_line(line,"plink rx linked bytes=9 ok=1 bad=0 torn=0 unfed=0 faults=0 uart=0"));
        for(uint32_t t=200300u;t<230300u;t+=10u) {
            for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)CHECK(pj_pld_periodic(&pr,&d,&u,t,i,1,line,sizeof(line))==0);
            CHECK(pj_pld_event(&pr,&d,t,1,line,sizeof(line))==0);
        }
        d.up_bytes+=21u;d.fed+=21u;d.frames_ok++;u.evq=2;
        for(unsigned i=1;i<PJ_PLD_PERIODIC;i++)CHECK(pj_pld_periodic(&pr,&d,&u,230300u,i,1,line,sizeof(line))==0);
        CHECK(pj_pld_periodic(&pr,&d,&u,230300u,0,1,line,sizeof(line))==1&&is_line(line,"plink rx linked bytes=30 ok=2 bad=0 torn=0 unfed=0 faults=0 uart=2"));
        pj_pld_reject(&d,PJ_PLR_CRC,pj_pld_ref+1,8);
        CHECK(pj_pld_event(&pr,&d,231000u,1,line,sizeof(line))==1&&is_line(line,"plink reject seq=1 reason=crc size=8 head=504a4231-01000200"));
        pj_pld_reject(&d,PJ_PLR_MAGIC,pj_pld_ref+2,3);
        for(uint32_t t=231010u;t<241000u;t+=10u)CHECK(pj_pld_event(&pr,&d,t,1,line,sizeof(line))==0);
        CHECK(pj_pld_event(&pr,&d,241000u,1,line,sizeof(line))==1&&is_line(line,"plink reject seq=2 reason=magic size=3 head=4a4231"));
        cap_once(&d,zeros,4,241000u);CHECK(d.done_seq==2);
        CHECK(pj_pld_event(&pr,&d,241260u,1,line,sizeof(line))==0);
        CHECK(pj_pld_event(&pr,&d,241270u,0,line,sizeof(line))==0); /* the link is lost: no "plink cap" for that capture */
        said=0; /* ...and every line of a link that does not work is back at once */
        for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)if(pj_pld_periodic(&pr,&d,&u,241270u,i,0,line,sizeof(line))) {
            said|=1u<<i;
            if(i==4)CHECK(is_line(line,"plink rx cap seq=2 n=4 code=zeros bits=0 at=0 hex=00000000"));
            if(i==6)CHECK(is_line(line,"plink rx first age=41 after=0 n=9 code=exact bits=72 at=0 hex=7e504a42-31010002-00"));
        }
        CHECK(said==0x7fu&&pj_pld_event(&pr,&d,241280u,0,line,sizeof(line))==0);
        for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)CHECK(pj_pld_periodic(&pr,&d,&u,241290u,i,1,line,sizeof(line))==0); /* linked again: the sum was said 11 s ago */
        /* Every line with every number at its largest still fits a diagnostics line and passes its rules. */
        memset(&d,0xff,sizeof(d));memset(&u,0xff,sizeof(u));memset(&pr,0,sizeof(pr));
        d.fed=0;d.done_n=PJ_PLD_CAP;d.rej_head_n=PJ_PLD_HEAD;d.rej_reason=PJ_PLR_NOTREQ;d.rej_size=0xffffu;
        d.done_result.kind=PJ_PLA_SLIP;d.done_result.arg=63;d.done_result.bits=255;d.done_result.at=255;
        memset(d.done,0xee,sizeof(d.done));memset(d.ring,0xee,sizeof(d.ring));memset(d.rej_head,0xee,sizeof(d.rej_head));
        d.first_n=PJ_PLD_CAP;d.first_result=d.done_result;memset(d.first,0xee,sizeof(d.first));
        for(unsigned i=0;i<PJ_PLD_PERIODIC;i++)CHECK(pj_pld_periodic(&pr,&d,&u,0xffffffffu,i,0,line,sizeof(line))==1&&line_ok(line)&&strlen(line)+24u<PJ_DIAG_MAX);
        CHECK(strstr(line,"plink rx first age=4294967 after=4294967295 n=32 code=slip@bit63 bits=255 at=255 hex=eeeeeeee-"));
        memset(&pr,0,sizeof(pr));
        CHECK(pj_pld_periodic(&pr,&d,&u,0xffffffffu,0,1,line,sizeof(line))==1&&line_ok(line)&&strlen(line)+24u<PJ_DIAG_MAX&&!strstr(line,"hex=")&&!strncmp(line,"plink rx linked bytes=4294967295 ",33));
        memset(&pr,0,sizeof(pr)); /* (the capture was said by the periodic line above: a fresh printer for the immediate ones) */
        CHECK(pj_pld_event(&pr,&d,0xffffffffu,0,line,sizeof(line))==1&&line_ok(line)&&strstr(line,"code=slip@bit63 bits=255 at=255 hex=eeeeeeee-"));
        CHECK(pj_pld_event(&pr,&d,0xffffffffu,0,line,sizeof(line))==1&&line_ok(line)&&strstr(line,"reason=notreq size=65535 head=eeeeeeee-eeeeeeee-eeeeeeee-eeeeeeee"));
    }
    /* The link screen's sentence for every finding. */
    {
        pj_plink_diag d;pj_pld_hint h;memset(&d,0,sizeof(d));
        h=pj_pld_hint_of(&d);CHECK(h.what==PJ_PLH_NOTHING);
        pj_pld_byte(&d,0x7e,1000u);h=pj_pld_hint_of(&d);CHECK(h.what==PJ_PLH_CHECKING&&says(h,"Pocket heard, checking...",""));
        d.frames_ok=1;h=pj_pld_hint_of(&d);CHECK(h.what==PJ_PLH_GOOD);d.frames_ok=0;
        pj_pld_poll(&d,1250u);h=pj_pld_hint_of(&d);CHECK(d.done_seq==1&&h.what==PJ_PLH_SHORT&&says(h,"Pocket heard, too few bytes","to judge."));
        {static const uint8_t noise[4]={0x13,0x57,0x9b,0xdf};memset(&d,0,sizeof(d));cap_once(&d,noise,4,1000u);h=pj_pld_hint_of(&d);
         CHECK(h.what==PJ_PLH_UNKNOWN&&h.bits<8);pj_pld_sentence(h,line,sizeof(line),line+48,40);CHECK(!strcmp(line,"Pocket heard, data scrambled")&&!strncmp(line+48,"(no pattern, ",13));}
        /* an old refusal does not colour a new, intact capture that no frame has ended yet */
        pj_pld_reject(&d,PJ_PLR_MAGIC,pj_pld_ref,4);cap_once(&d,pj_pld_ref,PJ_PLD_REF,3000u);
        h=pj_pld_hint_of(&d);CHECK(d.done_seq==2&&h.what==PJ_PLH_CUT);
        pj_pld_reject(&d,PJ_PLR_LENGTH,pj_pld_ref,4);h=pj_pld_hint_of(&d);CHECK(h.what==PJ_PLH_REJECTED&&says(h,"Pocket heard, data correct,","check failed (bad length)."));
        d.frames_ok++;h=pj_pld_hint_of(&d);CHECK(h.what==PJ_PLH_GOOD&&says(h,"Pocket heard, data correct.",""));
        {pj_pld_hint x={PJ_PLH_SHIFT,4,64,0};CHECK(says(x,"Pocket heard, data scrambled","(shifted 4 bits late)."));}
        {pj_pld_hint x={PJ_PLH_SHIFT,-1,64,0};CHECK(says(x,"Pocket heard, data scrambled","(shifted 1 bit early)."));}
        {pj_pld_hint x={PJ_PLH_SLIP,62,64,0};CHECK(says(x,"Pocket heard, data scrambled","(lost a bit near bit 62)."));}
        {pj_pld_hint x={PJ_PLH_DUP,62,72,0};CHECK(says(x,"Pocket heard, data scrambled","(extra bit near bit 62)."));}
        {pj_pld_hint x={PJ_PLH_UNKNOWN,0,255,0};CHECK(says(x,"Pocket heard, data scrambled","(no pattern, 255 bits right)."));}
    }
    /* The link still works after all of it. */
    {
        int before=replies;pj_wire_rx_init(&pocket_rx,pocket_frame,sizeof(pocket_frame));
        n=hello_wire(wire);diag_fresh();upstream(wire,n);settle();
        CHECK(replies==before+1&&pj_av_u32(pocket_frame+8)==diag_seq&&L.d.frames_ok==1&&pj_plink_view(&L,now_ms).what==PJ_PLV_CONNECTED);
    }
    pj_plink_stop(&L);CHECK(pj_plink_stage(&L)==PJ_PST_OFF);
}
/* ---- launcher 1.3.6 review: what a long wait on the link screen leaves in the launcher's 16 KB log ----
 * The launcher's loop (launcher_main.c) with the real gateway, diagnostics, printer wrappers and log ring
 * (shared/log_ring.h): the "plink begin" line; every 5 s the "plink state" line, then the "plink rx" lines one a
 * pass, otherwise the immediate lines; the one-a-second "stats" line (launcher_diag.h pj_diag_stats, with counters
 * as a link session makes them) and the 5 s "fpga version" line. The Pocket's HELLO arrives one bit late, every 5 s
 * (`once` 0) or one time only. Measured, not assumed: how long the log reaches back, when the "plink begin" line and
 * the first capture's own line leave it, and that "plink rx first" still tells both after 10 and 20 minutes. */
static pj_log wait_log;static unsigned long wait_all,wait_own;static uint32_t wait_cap_seq,wait_cap_ms;static unsigned wait_caps;
static void wait_say(const char *body,int own) {
    char line[256];int n=snprintf(line,sizeof(line),"%lu diag %s\n",(unsigned long)now_ms,body);
    CHECK(line_ok(body)&&n>0&&n<200);
    pj_log_add(&wait_log,now_ms,"diag",body);wait_all+=(unsigned long)n;if(own)wait_own+=(unsigned long)n;
    if(!strncmp(body,"plink cap seq=",14)||!strncmp(body,"plink rx cap seq=",17)) { /* one capture: never said twice within 30 s */
        uint32_t s=(uint32_t)strtoul(strstr(body,"seq=")+4,NULL,10);
        CHECK(!wait_caps||s!=wait_cap_seq||(uint32_t)(now_ms-wait_cap_ms)>=30000u);
        wait_caps++;wait_cap_seq=s;wait_cap_ms=now_ms;
    }
}
static const char *last_of(const char *text,const char *what){const char *at=NULL,*p=text;while((p=strstr(p,what))!=NULL){at=p;p++;}return at;}
static void log_checks(pj_plink_ops ops,pj_plink_extra extra,int once) {
    static char tail[PJ_LOG_BYTES+1];
    pj_pld_printer pr;pj_pld_uart u;pj_diag_counts counts;pj_launcher launcher;char body[PJ_DIAG_MAX],first[PJ_DIAG_MAX],hex[80];
    uint8_t wire[48],bits[400],mod[420],bytes[96],first_bytes[96];
    uint32_t begin,state_ms,stats_ms,fpga_ms,hello_ms,begin_gone=0,cap_gone=0,span=0;unsigned rx_line=PJ_PLD_PERIODIC,hellos=0,first_k=0;
    memset(&u,0,sizeof(u));memset(&counts,0,sizeof(counts));memset(&launcher,0,sizeof(launcher));launcher.phase=PJ_L_MEDIA;
    memset(&wait_log,0,sizeof(wait_log));wait_all=wait_own=0;wait_caps=0;first[0]=0;
    P.async=0;P.allow_overflow=0;P.busy_every=0;P.faults=0;P.fast=0;P.fast_pending=0;P.mode=0;P.level=0;
    pj_plink_stop(&L);memset(&L.d,0,sizeof(L.d));memset(&diag_rx,0,sizeof(diag_rx)); /* pj_jl_begin allocates the gateway anew */
    CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);pj_pld_printer_init(&pr,now_ms);
    begin=state_ms=stats_ms=fpga_ms=now_ms;hello_ms=now_ms-3000u;
    wait_say("plink begin=0 heap=61324 block=36212",0);
    for(;;) {
        uint32_t t;int second=0;
        tick(64);t=(uint32_t)(now_ms-begin);
        if((!once||!hellos)&&(uint32_t)(now_ms-hello_ms)>=5000u) { /* the Pocket tries again */
            unsigned nb=to_bits(wire,hello_wire(wire),bits),k;
            mod[0]=1;memcpy(mod+1,bits,nb);for(unsigned i=0;i<7;i++)mod[1+nb+i]=1;
            k=engine(mod,nb+8u,bytes);upstream(bytes,k);hello_ms=now_ms;
            if(!hellos++){memcpy(first_bytes,bytes,k);first_k=k;CHECK(t==2000u&&k>=20&&k<=PJ_PLD_CAP);}
        }
        if(!first[0]&&L.d.first_n) { /* the first capture is complete */
            CHECK(L.d.first_n==first_k&&!memcmp(L.d.first,first_bytes,first_k)&&is_code(L.d.first_result,"shift+1",72,0)&&L.d.first_ms==begin+2000u);
            snprintf(first,sizeof(first),"after=2000 n=%u code=shift+1 bits=72 at=0 hex=%s",first_k,pj_pld_hex(hex,sizeof(hex),first_bytes,first_k));
        }
        CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_WAITING&&!pj_plink_linked(&L,now_ms));
        /* launcher_main.c, the link's lines */
        if((uint32_t)(now_ms-state_ms)>=5000u) {
            state_ms=now_ms;
            snprintf(body,sizeof(body),"plink state=%lu view=%u level=%lu faults=%lu fast=%lu stream=%lu heap=%lu heap-min=%lu block=%lu",
                     (unsigned long)L.state,(unsigned)pj_plink_view(&L,now_ms).what,(unsigned long)L.level,(unsigned long)L.fpga_faults,
                     (unsigned long)L.fast,(unsigned long)L.stream_state,61324ul,58012ul,36212ul);
            wait_say(body,0);rx_line=0;
        }
        else if(rx_line<PJ_PLD_PERIODIC) {
            while(rx_line<PJ_PLD_PERIODIC)if(pj_plink_say_periodic(&L,&pr,&u,now_ms,rx_line++,body,sizeof(body))){wait_say(body,1);break;}
        }
        else if(pj_plink_say_event(&L,&pr,now_ms,body,sizeof(body)))wait_say(body,1);
        /* launcher_main.c, the lines of every mode */
        if((uint32_t)(now_ms-stats_ms)>=PJ_DIAG_STATS_MS) {
            stats_ms=now_ms;second=1;counts.uart_bytes+=300u;counts.messages+=21u;counts.polls+=100u;counts.frames+=2u;
            pj_diag_stats(body,sizeof(body),&counts,&launcher,61324u,58012u);wait_say(body,0);
        }
        if((uint32_t)(now_ms-fpga_ms)>=5000u){fpga_ms=now_ms;wait_say("fpga version=18.41 debug=1",0);}
        if(second) {
            size_t n=pj_log_tail(&wait_log,PJ_LOG_BYTES,tail);tail[n]=0;
            if(!begin_gone&&!strstr(tail,"plink begin="))begin_gone=t;
            if(!cap_gone&&first[0]&&!strstr(tail," plink cap seq=1 "))cap_gone=t;
            if(t==60000u||t==600000u||t==1200000u) {
                const char *f=last_of(tail,"plink rx first age="),*a,*nl=strchr(tail,'\n');unsigned long age;
                CHECK(f&&nl&&(n==PJ_LOG_BYTES)==(t>60000u)); /* one minute does not fill the log; ten do, many times */
                span=(uint32_t)(now_ms-(uint32_t)strtoul(nl+1,NULL,10)); /* the oldest whole line */
                age=strtoul(f+19,NULL,10);CHECK(age<=t/1000u&&age+36u>=t/1000u); /* said every 30 to 35 s */
                snprintf(body,sizeof(body),"%.*s",(int)strcspn(f,"\n"),f);a=strstr(body," after=");
                CHECK(a&&!strcmp(a+1,first)); /* the first capture, whatever was heard since */
                CHECK(strstr(tail,"plink rx bytes=")&&strstr(tail,"plink rx link ")&&strstr(tail,"plink rx uart ")&&strstr(tail,"plink state="));
                CHECK(strstr(tail,"plink cap seq=")||strstr(tail,"plink rx cap seq="));
                if(!once)CHECK(strstr(tail,"plink rx ring n=48 "));
                else CHECK(last_of(tail,"plink rx cap seq=1 n=")||t==60000u); /* a Pocket that spoke once: its capture is said again */
                if(t==60000u)CHECK(strstr(tail,"plink begin=0 ")&&strstr(tail," plink cap seq=1 ")); /* a short wait: the original lines too */
                else CHECK(begin_gone&&cap_gone&&!strstr(tail,"plink begin=")&&!strstr(tail," plink cap seq=1 "));
                if(t==600000u)printf("link log, Pocket %s: %lu B/s in all, %lu B/s of it the 1.3.6 link lines; the 16 KB log reaches back %lu s; "
                    "\"plink begin\" left it after %lu s, the first capture's own line after %lu s; \"plink rx first\" tells both (said every 30-35 s)\n",
                    once?"spoke once":"retrying every 5 s",wait_all/600u,wait_own/600u,(unsigned long)span/1000u,(unsigned long)begin_gone/1000u,(unsigned long)cap_gone/1000u);
            }
            if(t>=1200000u)break;
        }
    }
    CHECK(L.state==PJ_PL_READY&&hellos==(once?1u:240u));
    if(once)CHECK(wait_caps>=30&&wait_caps<=41); /* "plink cap" once, then "plink rx cap" every 30 to 35 s */
    /* the true numbers behind the doc: without "plink rx first" neither line would be there after two minutes */
    CHECK(begin_gone>=60000u&&begin_gone<=180000u&&cap_gone>=60000u&&cap_gone<=180000u&&span>=60000u&&span<=180000u);
    pj_plink_stop(&L);
}
/* ---- launcher 1.3.6, review of the 18.41 bridge (rtl/chromatic_pocket_link.v, read only here) ----
 * (b) The bridge is still in media mode at 524,288 baud when a link starts (the launcher restarted, or a leave was
 *     lost): it understands nothing said at 115,200 until its heartbeat guard expires, at most 3 s after the last
 *     heartbeat it understood. The launcher says nothing at the fast rate to end this: it says ENTER again every
 *     500 ms at 115,200 and waits 4.5 s, so the link comes up by itself and the screen never says stopped.
 * (a) A status packet the bridge had built is not sent again when the rates change: up to 6 upstream bytes may be
 *     lost. So the fast rate is asked for only while nothing is owed and the cable is quiet; a Pocket that never
 *     pauses gets it after 2 s, between a reply and its next request. */
static unsigned ring_data(void) { /* reply bytes still in the ESP32's transmit ring (a heartbeat there is no reply) */
    unsigned bytes=0;
    for(unsigned at=0;at<P.ring_used;at+=P.ring[at+2]+4u)if(P.ring[at+1]==PJ_TX_PLINK_DATA)bytes+=P.ring[at+2];
    return bytes;
}
static void bridge_checks(pj_plink_ops ops,pj_plink_extra extra) {
    size_t n;
    /* (b) every moment the guard may expire at, 0 to 3.2 s after the link began, in 100 ms steps */
    for(unsigned expiry=0;expiry<=3200;expiry+=100) {
        P.mode=2;P.fast=1;P.fast_pending=0;P.faults=0;P.deaf=1;P.unheard=0;
        unsigned enters=P.enters,leaves=P.leaves,baud=P.baud_changes;uint32_t began=now_ms,ready=0;
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        while(now_ms-began<PJ_PLINK_ARM_MS+500u) {
            if(P.deaf&&now_ms-began>=expiry){P.deaf=0;P.mode=0;P.fast=0;P.faults=8;} /* expired: the game, 115,200, fault bit 3 */
            now_ms+=10;
            if(!P.deaf&&now_ms%50==0)status(now_ms,NULL,0); /* its status packets at the fast rate are no packets to us */
            CHECK(pj_plink_step(&L,now_ms)==0&&pj_plink_view(&L,now_ms).what==PJ_PLV_WAITING);
            if(P.deaf)CHECK(L.state==PJ_PL_ARMING&&!L.status_packets&&P.enters==enters&&!P.uart_fast&&P.baud_changes==baud);
            if(!ready&&L.state==PJ_PL_READY)ready=now_ms;
            if(P.fast_pending){P.fast_pending=0;P.fast=1;}
        }
        /* heard: exactly one ENTER, within 500 ms (and a status period) of the expiry; nothing was ever said fast
         * outside media mode; the expiry's fault bit was seen and cleared; the fast rate then came up as usual */
        CHECK(ready&&ready-began<=expiry+PJ_PLINK_ENTER_MS+60u&&ready-began<PJ_PLINK_ARM_MS&&P.enters==enters+1&&P.leaves==leaves);
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST&&P.mode==2&&!P.faults&&P.uart_fast==1&&(expiry<100||P.unheard>=1+expiry/PJ_PLINK_ENTER_MS));
        const uint8_t *p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&p);
        pj_plink_stop(&L);CHECK(P.mode==0&&!P.uart_fast);
    }
    /* (a) the Pocket's HELLO arrives with the very status packets that report media mode: the fast rate waits for the
     * reply to be gone from the UART ring and the bridge's queue and for 50 ms of quiet after that. The UART is the
     * 1.3.1 ring at 115,200 (about 115 bytes a loop pass), the cable takes 16 bytes a pass. */
    {
        int was_async=P.async;unsigned was_rate=P.uart_per_tick;
        P.mode=0;P.fast=0;P.fast_pending=0;P.faults=0;P.async=1;P.uart_per_tick=115;P.budget=0;
        P.strict=1;P.wrong=0;P.ring_at_fast=0; /* from here on the bridge hears only its own rate */
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        uart_flush(&P,1);CHECK(P.mode==2&&L.state==PJ_PL_ARMING);
        unsigned fr=P.fast_requests;int before=replies;uint32_t busy=now_ms,asked=0;
        send_request(PJ_RPC_HELLO,"{}",1);CHECK(L.state==PJ_PL_READY&&L.tx_active&&L.fast==PJ_PF_SLOW);
        for(int i=0;i<200&&L.fast!=PJ_PF_FAST;i++) {
            tick(16);
            if(P.fast_requests==fr&&(L.tx_active||ring_data()||P.level))busy=now_ms;
            if(P.fast_requests!=fr&&!asked)asked=now_ms;
        }
        CHECK(replies==before+1&&P.fast_requests==fr+1&&L.fast==PJ_PF_FAST&&asked&&asked-busy>=PJ_PLINK_FAST_QUIET_MS&&asked-busy<=PJ_PLINK_FAST_QUIET_MS+60u);
        const uint8_t *p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&p);
        pj_plink_stop(&L);uart_flush(&P,1);
        /* A half frame and then silence (the cable came out in mid-request) does not hold the rate back. */
        P.mode=0;P.fast=0;P.fast_pending=0;
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);uart_flush(&P,1);
        {static const uint8_t half[5]={0x7e,'P','J','B','1'};fr=P.fast_requests;status(now_ms,half,5);asked=now_ms;}
        CHECK(L.state==PJ_PL_READY&&L.upstream.active);
        for(int i=0;i<4;i++){tick(16);CHECK(P.fast_requests==fr);}
        for(int i=0;i<12;i++)tick(16);
        CHECK(P.fast_requests==fr+1&&L.fast==PJ_PF_FAST);
        pj_plink_stop(&L);uart_flush(&P,1);
        /* A Pocket that never pauses (the next request the moment a reply is whole): not asked for during the 2 s
         * hold; after it, between a reply and the next request (fpga_send checks that nothing is owed), and the link
         * goes on at the fast rate. */
        P.mode=0;P.fast=0;P.fast_pending=0;
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);uart_flush(&P,1);
        fr=P.fast_requests;asked=0;P.fast_any=1;
        uint32_t began=now_ms;unsigned asks=0;
        while(now_ms-began<4000u) {
            const uint8_t *q=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&q);asks++;
            if(!asked&&P.fast_requests!=fr)asked=now_ms;
            if(!asked)CHECK(now_ms-L.ready_ms<PJ_PLINK_FAST_HOLD_MS+100u);
        }
        P.fast_any=0;
        CHECK(asked&&asked-began>=PJ_PLINK_FAST_HOLD_MS&&asked-began<=PJ_PLINK_FAST_HOLD_MS+200u&&P.fast_requests==fr+1&&L.fast==PJ_PF_FAST&&asks>40);
        pj_plink_stop(&L);uart_flush(&P,1);
        CHECK(!P.wrong&&!P.ring_at_fast&&P.mode==0&&!P.uart_fast);
        /* ---- 1.3.6 rate-change checks ----
         * The same Pocket with large replies (HOME: about 2 KB, so a reply's tail still waits in the MCU's transmit
         * ring when its last packet has been handed over), the cable taking 64, 256 and 1024 bytes a pass, the
         * launcher's own button requests going into the same ring. The request for the fast rate is queued only
         * into an empty ring and the MCU changes its rate only once the ring is empty again (set_baud checks it), so
         * nothing ever leaves at a rate the bridge is not at, the bridge switches, and the link is fast from then on.
         * When the request can be made depends on where the bridge's status packets and the launcher's
         * button requests fall against the replies (it needs a pass in which a reply's tail has left the ring and
         * nothing else waits in it), so the case runs through all of it: the status packet every 10, 30 and 50 ms
         * at every phase, the link begun at every 10 ms step against the 100 ms button request. The time is taken
         * when the bridge hears the request (tick), not when the next reply is whole. MODEL_HOLD_LATE_MS is the bound
         * over those runs: the largest delay this model shows plus a margin, an observed figure and not a derived
         * one (nothing in the gateway promises a window to a Pocket that never pauses). */
        static const unsigned cable[3]={64,256,1024};
        static const unsigned periods[3]={10,30,50};
        unsigned home_bytes=0,late[3]={0,0,0},late_by[3]={0,0,0},soonest=0xffffffffu,runs=0;
        CHECK(now_ms%10u==0);
        for(unsigned c=0;c<3;c++)for(unsigned sp=0;sp<3;sp++)for(unsigned phase=0;phase<periods[sp];phase+=10)for(unsigned start=0;start<100;start+=10) {
            status_period=periods[sp];status_phase=phase;while(now_ms%100u!=start)now_ms+=10;
            P.mode=0;P.fast=0;P.fast_pending=0;P.wrong=0;P.ring_at_fast=0;P.poll=1;P.polls=0;
            CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);uart_flush(&P,1);
            fr=P.fast_requests;asked=0;P.fast_any=1;began=now_ms;asks=0;unsigned fast_asks=0;
            while(now_ms-began<6000u) {
                const uint8_t *q=rpc(PJ_RPC_HOME,"{}",cable[c],&n);CHECK(pocket_frame[5]==1&&q&&n>1500);asks++;home_bytes=(unsigned)n;
                if(!asked&&P.fast_requests!=fr)asked=fast_heard_ms;
                if(L.fast==PJ_PF_FAST)fast_asks++;
                CHECK(L.fast!=PJ_PF_OFF&&!L.fast_failed&&!P.wrong&&!P.ring_at_fast);
            }
            P.fast_any=0;
            CHECK(asked&&asked-began>=PJ_PLINK_FAST_HOLD_MS&&asked-began<=PJ_PLINK_FAST_HOLD_MS+MODEL_HOLD_LATE_MS&&P.fast_requests==fr+1);
            unsigned was=asked-began-PJ_PLINK_FAST_HOLD_MS;runs++;
            if(was>late[c])late[c]=was;
            if(was>late_by[sp])late_by[sp]=was;
            if(was<soonest)soonest=was;
            CHECK(L.fast==PJ_PF_FAST&&P.fast==1&&P.uart_fast==1&&fast_asks>=4&&P.polls>=50&&!P.wrong&&!P.ring_at_fast);
            pj_plink_stop(&L);uart_flush(&P,1);P.poll=0;
            CHECK(!P.wrong&&P.mode==0&&!P.uart_fast);
        }
        status_period=50;status_phase=0;
        CHECK(runs==270);
        /* ...and the rule before this review (the ring not looked at: no tx_pending, the rate changed at once) in the
         * same run at 256 bytes a pass: bytes still wait in the ring when the MCU goes to the fast rate, the request
         * itself among them, the bridge never hears it, and the link stays at 115,200 for the session. The check
         * sees the defect it guards against. */
        unsigned old_ring;
        {
            P.mode=0;P.fast=0;P.fast_pending=0;P.wrong=0;P.ring_at_fast=0;P.allow_ring=1;P.fast_any=1;P.allow_overflow=1;
            CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);L.x.tx_pending=NULL;uart_flush(&P,1);
            fr=P.fast_requests;began=now_ms;
            while(now_ms-began<6000u&&L.fast!=PJ_PF_OFF) {
                int before=replies;send_request(PJ_RPC_HOME,"{}",1);
                for(int i=0;i<300&&replies==before&&L.fast!=PJ_PF_OFF;i++)tick(256);
            }
            CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_OFF&&L.fast_failed==1&&!strcmp(L.why,"fast UART not confirmed"));
            CHECK(P.ring_at_fast>9&&P.wrong>0&&P.fast_requests==fr&&!P.fast&&!P.uart_fast);
            old_ring=P.ring_at_fast;
            for(int i=0;i<2000&&(P.pending||L.tx_active||L.work!=PJ_PW_NONE||L.http_busy||P.ring_used||P.level);i++)tick(1024);
            CHECK(!P.pending&&!P.ring_used);
            P.strict=0;pj_plink_stop(&L);uart_flush(&P,1);P.level=0;
            P.allow_ring=0;P.fast_any=0;P.allow_overflow=0;P.strict=1;P.wrong=0;P.ring_at_fast=0;
            pj_wire_rx_init(&pocket_rx,pocket_frame,sizeof(pocket_frame));
        }
        /* A transmit ring that does not empty (a UART that is stuck): the request stays in it, the MCU never changes
         * its rate, nothing else is queued meanwhile, and PJ_PLINK_FAST_DRAIN_MS later the try is over for the
         * session: the link goes on at 115,200. (What the bridge does with a request that leaves late is its own
         * 250 ms rule, not modelled here.) */
        {
            P.mode=0;P.fast=0;P.fast_pending=0;
            CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);uart_flush(&P,1);
            fr=P.fast_requests;unsigned changes=P.baud_changes;
            for(int i=0;i<40&&L.fast!=PJ_PF_REQUESTED;i++)tick(64);
            CHECK(L.fast==PJ_PF_REQUESTED&&P.ring_used==9&&pj_plink_rate_changing(&L));
            P.uart_per_tick=0;P.budget=0;uint32_t stuck=now_ms;unsigned sends=P.sends;
            while(L.fast==PJ_PF_REQUESTED&&now_ms-stuck<1000u){tick(64);CHECK(P.ring_used==9&&P.sends==sends&&P.baud_changes==changes);}
            CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_OFF&&L.fast_failed==1&&!strcmp(L.why,"fast UART request not sent")&&!pj_plink_rate_changing(&L));
            CHECK(now_ms-stuck>=PJ_PLINK_FAST_DRAIN_MS&&now_ms-stuck<=PJ_PLINK_FAST_DRAIN_MS+10u&&!P.uart_fast&&P.fast_requests==fr&&!P.wrong);
            for(int i=0;i<30;i++){tick(64);CHECK(P.baud_changes==changes&&L.fast==PJ_PF_OFF);} /* and no second try */
            P.strict=0;P.uart_per_tick=115;pj_plink_stop(&L);uart_flush(&P,1);P.strict=1;
        }
        /* (a) The cable comes out in mid-byte (fault bit 0) while the request for the fast rate still
         * waits in the transmit ring (REQUESTED). The ENTER that clears the bit is held like the heartbeat: nothing
         * is queued (not even tried) although its 250 ms period is long over; fpga_send refuses an ENTER in that
         * state in every case of this check. Once the request has left and the MCU has changed its rate the ENTER
         * goes out at the rate the bridge is at, the bit is cleared with one ENTER, and the link is fast. (Said at
         * 115,200 behind the request it would have reached a bridge already at the fast rate: lost, and counted by
         * `wrong` here.) */
        {
            P.mode=0;P.fast=0;P.fast_pending=0;P.faults=0;
            CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);uart_flush(&P,1);
            fr=P.fast_requests;
            for(int i=0;i<40&&L.fast!=PJ_PF_REQUESTED;i++)tick(64);
            CHECK(L.fast==PJ_PF_REQUESTED&&P.ring_used==9&&P.fast_requests==fr);
            P.uart_per_tick=0;P.budget=0;P.faults=1;uint32_t held=now_ms;unsigned sends=P.sends,enters=P.enters,seen=0;
            while(now_ms-held<PJ_PLINK_FAST_DRAIN_MS-50u){tick(64);seen+=L.fpga_faults==1;CHECK(L.fast==PJ_PF_REQUESTED&&P.ring_used==9&&P.sends==sends&&P.enters==enters);}
            CHECK(seen>=5&&L.state==PJ_PL_READY&&(uint32_t)(now_ms-L.clear_ms)>=PJ_PLINK_HEARTBEAT_MS); /* due, and held */
            P.uart_per_tick=115;
            for(int i=0;i<30&&(P.faults||L.fast!=PJ_PF_FAST);i++)tick(64);
            CHECK(!P.faults&&P.enters==enters+1&&P.fast_requests==fr+1&&L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST&&!L.fast_failed);
            CHECK(P.fast==1&&P.uart_fast==1&&!P.wrong&&!P.ring_at_fast);
            for(int i=0;i<30;i++)tick(64);
            CHECK(P.enters==enters+1&&!P.wrong&&L.fast==PJ_PF_FAST&&!L.fpga_faults);
            pj_plink_stop(&L);uart_flush(&P,1);
            CHECK(!P.wrong&&P.mode==0&&!P.uart_fast);
        }
        P.strict=0;P.wrong=0;
        P.async=was_async;P.uart_per_tick=was_rate;P.mode=0;P.fast=0;P.fast_pending=0;
        printf("bridge review 2: HOME replies of %u bytes without a pause, cable 64/256/1024 bytes a pass, the launcher's "
               "button requests in the same ring, %u runs (status packet every 10/30/50 ms at every phase, every start "
               "against the button request): the bridge heard the request for the fast rate %u to %u/%u/%u ms after the "
               "%u ms hold (by status period: %u/%u/%u ms; bound %u ms, observed not derived), into an empty "
               "transmit ring, the rate changed with the ring empty, nothing sent at the wrong rate; the rule before "
               "(ring not looked at) changed the rate with %u bytes still in the ring and stayed at 115,200; a ring that "
               "does not empty ends the try after %u ms with no rate change and its own reason; a fault bit seen while "
               "the request waits: its ENTER held until the rate changed, then one ENTER at the fast rate\n",
               home_bytes,runs,soonest,late[0],late[1],late[2],PJ_PLINK_FAST_HOLD_MS,late_by[0],late_by[1],late_by[2],
               MODEL_HOLD_LATE_MS,old_ring,PJ_PLINK_FAST_DRAIN_MS);
    }
    printf("bridge review: a bridge still at 524,288 baud is entered within %u ms of its guard's expiry, by ENTER said "
           "every %u ms at 115,200 for %u ms, the screen waiting throughout; the fast rate asked for only with nothing "
           "owed, %u ms of quiet, or after %u ms between a reply and the next request\n",
           PJ_PLINK_ENTER_MS+60u,PJ_PLINK_ENTER_MS,PJ_PLINK_ARM_MS,PJ_PLINK_FAST_QUIET_MS,PJ_PLINK_FAST_HOLD_MS);
}
/* ---- launcher 1.5.0: the Pocket-acknowledged credit (shared/pocket_tether.h, pocket_link.h pj_plink_unsure) ----
 * The Pocket's ACK is made here as pocket/rpc_client.h makes it (pk_* in drain) and sent up in status packets of at
 * most six bytes. The bound down_total - acked must hold at every pass, whatever the ACK's age: what the bridge's
 * queue and the UART ring hold for it never exceeds it (ack_bound_ok). */
static unsigned ring_payload(void) {
    unsigned n=0;for(unsigned at=0;at+3u<=P.ring_used;at+=P.ring[at+2]+4u)if(P.ring[at+1]==PJ_TX_PLINK_DATA)n+=P.ring[at+2];
    return n;
}
static int ack_bound_ok(void){return !L.ack_have||P.level+ring_payload()<=L.down_total-L.acked;}
typedef struct {uint8_t ok,seq;uint32_t count;} pk_snap;
static pk_snap pk_now(void){pk_snap s={pk_anchor_ok,pk_anchor_seq,pk_count-pk_anchor_at};return s;}
static void pocket_ack(pk_snap s,uint32_t lie) {
    if(!s.ok)return;
    uint8_t a[PJ_TETHER_ACK_SIZE],w[2u+2u*PJ_TETHER_ACK_SIZE];unsigned n=0;
    pj_tether_ack_build(a,s.seq,s.count+lie);
    pj_wire_tx t;CHECK(pj_wire_tx_init(&t,a,sizeof(a)));while(n<sizeof(w)&&pj_wire_tx_next(&t,&w[n]))n++;
    for(unsigned at=0;at<n;at+=6u)status(now_ms,w+at,n-at<6u?n-at:6u);
}
static void ack_checks(pj_plink_ops ops,pj_plink_extra extra) {
    /* the frame: round trip, every one-bit change refused, wrong sizes refused, the count saturates (smaller is safe) */
    uint8_t a[PJ_TETHER_ACK_SIZE],sq;uint32_t c;
    pj_tether_ack_build(a,7,1234);CHECK(pj_tether_ack_parse(a,PJ_TETHER_ACK_SIZE,&sq,&c)&&sq==7&&c==1234);
    for(unsigned i=0;i<PJ_TETHER_ACK_SIZE;i++)for(unsigned b=0;b<8;b++){a[i]^=(uint8_t)(1u<<b);CHECK(!pj_tether_ack_parse(a,PJ_TETHER_ACK_SIZE,&sq,&c));a[i]^=(uint8_t)(1u<<b);}
    CHECK(!pj_tether_ack_parse(a,PJ_TETHER_ACK_SIZE-1u,&sq,&c)&&!pj_tether_ack_parse(a,PJ_TETHER_ACK_SIZE+1u,&sq,&c));
    pj_tether_ack_build(a,1,70000u);CHECK(pj_tether_ack_parse(a,PJ_TETHER_ACK_SIZE,&sq,&c)&&c==65535u);
    CHECK(PJ_TETHER_ACK_SIZE<16u&&2u+PJ_TETHER_ACK_SIZE<=7u); /* never a request; two bridge statuses */

    /* a fresh link at the fast rate; HELLO asks for ACKs */
    P.fast=0;P.fast_pending=0;P.mode=0;P.level=0;P.async=0;P.stale=0;uart_flush(&P,1);
    CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
    for(int i=0;i<12;i++)tick(64);
    CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST&&!L.ack_have&&!L.down_total);
    size_t n;const uint8_t *p=rpc(PJ_RPC_HELLO,"{}",64,&n);
    {char text[512];memcpy(text,p,n);text[n]=0;CHECK(strstr(text,"\"credit_ack\":1"));}
    CHECK(pk_anchor_ok&&pk_anchor_seq==(uint8_t)seq);

    /* Home replies (larger than the queue) over the fast UART with statuses written 200 UART bytes early, down cables
     * of 0 (stalled, then 512 kHz), 7, 80, 320 (256 kHz) and 640 (512 kHz) bytes a pass and one that changes rate
     * every 13 passes, ACKs every 1, 3 or 25 passes (stale) and 0, 1 or 4 passes late: the bound always holds, nothing
     * overflows (apply_packet), each reply arrives whole. */
    static const unsigned cables[6]={0,7,80,320,640,1},every[3]={1,3,25},lates[3]={0,1,4};
    unsigned runs=0,ack_passes=0,old_passes=0,peak=0;
    P.async=1;P.stale=2;P.early=200;P.uart_per_tick=524;P.budget=0;P.idle=0;
    for(unsigned cb=0;cb<6;cb++)for(unsigned e=0;e<3;e++)for(unsigned lt=0;lt<3;lt++) {
        pk_snap hist[8];for(unsigned i=0;i<8;i++)hist[i]=pk_now();
        int before=replies;unsigned ticks=0;P.max_level=0;
        send_request(PJ_RPC_HOME,"{}",1);
        while(replies==before&&ticks<4000u) {
            unsigned cable=cables[cb]==1u?((ticks/13u)%3u==0?640u:(ticks/13u)%3u==1?320u:20u):cables[cb]?cables[cb]:ticks<80u?0u:640u;
            tick(cable);ticks++;
            hist[ticks%8u]=pk_now();
            if(ticks%every[e]==0)pocket_ack(hist[(ticks+8u-lates[lt])%8u],0);
            CHECK(ack_bound_ok());
            if(L.ack_have&&pj_plink_unsure(&L)==L.down_total-L.acked&&(uint32_t)(L.now-L.ack_ms)<=PJ_PLINK_ACK_FRESH_MS)ack_passes++;else old_passes++;
        }
        CHECK(replies==before+1&&pocket_frame[5]==1&&pocket_frame[4]==PJ_RPC_HOME&&reply_size>PJ_TETHER_QUEUE);
        CHECK(P.max_level<=PJ_TETHER_QUEUE&&!L.fpga_faults&&!P.faults&&L.state==PJ_PL_READY&&!L.ack_bad);
        if(P.max_level>peak)peak=P.max_level;
        for(int i=0;i<30;i++){tick(640);pocket_ack(pk_now(),0);CHECK(ack_bound_ok());}
        CHECK(!P.level&&!L.tx_active);runs++;
    }
    CHECK(runs==54u&&ack_passes>1000u&&old_passes>100u&&peak>=PJ_TETHER_QUEUE-PJ_TETHER_MARGIN-2u*PJ_PLINK_DATA_BYTES);

    /* a bound that stands still falls back to the 1.4.0 rule after PJ_PLINK_ACK_FRESH_MS: a stalled cable with the
     * same ACK said again every pass */
    {
        int before=replies;send_request(PJ_RPC_HOME,"{}",1);
        for(int i=0;i<40&&!L.tx_active;i++){tick(0);pocket_ack(pk_now(),0);}
        CHECK(L.tx_active&&L.ack_have);
        for(int i=0;i<3;i++){tick(0);pocket_ack(pk_now(),0);}
        uint32_t still=L.acked,since=L.ack_ms;
        for(int i=0;i<30;i++){tick(0);pocket_ack(pk_now(),0);CHECK(ack_bound_ok());}
        CHECK(L.acked==still&&L.ack_ms==since&&(uint32_t)(L.now-L.ack_ms)>PJ_PLINK_ACK_FRESH_MS&&pj_plink_unsure(&L)==L.level+L.sent_since);
        while(replies==before){tick(640);pocket_ack(pk_now(),0);CHECK(ack_bound_ok());}
        for(int i=0;i<30;i++){tick(640);pocket_ack(pk_now(),0);}
    }
    /* ACKs not believed: past what was sent (bad), behind the one held, for a reply no longer remembered (old) */
    {
        uint32_t bad=L.ack_bad,old=L.ack_old,held=L.acked;
        pk_snap s=pk_now();pocket_ack(s,L.down_total);CHECK(L.ack_bad==bad+1&&L.acked==held);
        if(s.count){pk_snap back=s;back.count=0;pocket_ack(back,0);CHECK(L.ack_old==old+1&&L.acked==held);}
        pk_snap gone=s;gone.seq=(uint8_t)(s.seq+128u);pocket_ack(gone,0);CHECK(L.ack_old>=old+1&&L.acked==held);
        /* a damaged ACK (its check) is refused as before (a short frame) and counted */
        uint8_t w[16];pj_tether_ack_build(a,s.seq,s.count);a[1]^=1u;pj_wire_tx t;unsigned k=0;CHECK(pj_wire_tx_init(&t,a,sizeof(a)));
        while(pj_wire_tx_next(&t,&w[k]))k++;
        uint32_t short_before=L.d.reject[PJ_PLR_SHORT];status(now_ms,w,k<6u?k:6u);if(k>6u)status(now_ms,w+6,k-6u);
        CHECK(L.ack_bad==bad+2&&L.acked==held&&L.d.reject[PJ_PLR_SHORT]==short_before+1u);
    }
    /* a reply sent twice (the same request asked again): the first sending's end is kept, so an ACK anchored on
     * either copy only makes the bound larger */
    {
        P.async=0;P.stale=0;uart_flush(&P,1);for(int i=0;i<10;i++)tick(2048);
        p=rpc(PJ_RPC_HELLO,"{}",2048,&n);
        uint32_t first_end=0;for(unsigned i=0;i<PJ_PLINK_ACK_LOG;i++)if(L.ack_log[i].used&&L.ack_log[i].seq==seq)first_end=L.ack_log[i].end;
        CHECK(first_end&&first_end==L.down_total);
        int before=replies;send_request(PJ_RPC_HELLO,"{}",0);for(int i=0;i<20&&replies==before;i++)tick(2048);
        CHECK(replies==before+1&&L.down_total>first_end);
        unsigned copies=0;for(unsigned i=0;i<PJ_PLINK_ACK_LOG;i++)if(L.ack_log[i].used&&L.ack_log[i].seq==seq){copies++;CHECK(L.ack_log[i].end==first_end);}
        CHECK(copies==1);
        pocket_ack(pk_now(),0);CHECK(L.acked==first_end&&ack_bound_ok()); /* anchored on the second copy: counted from the first */
    }
    /* the negative control: a Pocket that claims 200 bytes more than it took breaks the bound */
    {
        p=rpc(PJ_RPC_HELLO,"{}",2048,&n); /* an anchor sent once (the repeat above makes its ACKs larger than they are) */
        unsigned broken=0;P.async=1;P.stale=2;P.early=200;P.budget=0;P.allow_overflow=1;unsigned overflows=P.overflows;
        int before=replies;send_request(PJ_RPC_HOME,"{}",1);
        for(unsigned i=0;i<300u&&replies==before;i++){tick(i<40u?0u:320u);pocket_ack(pk_now(),200u);if(!ack_bound_ok())broken++;}
        CHECK(broken>0&&P.overflows>overflows); /* the bound is broken, and the bridge's queue overflows (data dropped) */
        overflows=P.overflows-overflows;
        for(int i=0;i<60;i++)tick(640);
        P.async=0;P.stale=0;P.early=0;uart_flush(&P,1);P.allow_overflow=0;P.level=0;
        printf("ACK credit (1.5.0): %u Home replies over stalled/7/80/256 kHz/512 kHz/changing cables, ACKs every 1/3/25 passes and 0/1/4 passes late with statuses 200 UART bytes early: the bound always held, queue peak %u of %u, ACK rule %u passes, 1.4.0 rule %u; a standing bound falls back after %u ms; bad/old/damaged ACKs refused; a repeated reply counted from its first sending; negative control: +200 B broke the bound %u times and overflowed the bridge's queue %u times\n",
               runs,peak,(unsigned)PJ_TETHER_QUEUE,ack_passes,old_passes,(unsigned)PJ_PLINK_ACK_FRESH_MS,broken,overflows);
    }
    pj_plink_stop(&L);
}
int main(void) {
    printf("sizeof(pj_plink)=%u pieces=%u+%u+%u+%u total=%u largest=%u\n",(unsigned)sizeof(pj_plink),(unsigned)sizeof(pj_plink),
           (unsigned)PJ_PLINK_BUF,(unsigned)PJ_PLINK_RESPONSE,(unsigned)sizeof(pj_plink_scratch),(unsigned)PJ_PLINK_MEMORY,(unsigned)pj_plink_piece_max());
    pj_wire_rx_init(&pocket_rx,pocket_frame,sizeof(pocket_frame));
    P.wifi_bytes_per_tick=4096;
    pj_plink_ops ops={&P,request,decode,fpga_send,stream_open,stream_read,stream_close,set_baud};
    /* 1.4.0: a clock for "plink speed"; the line's idle time is given only where its runs say so (credit review) */
    const pj_plink_extra extra={send_sure,tx_pending,model_micros,NULL};
    CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);CHECK(P.enters==1&&L.state==PJ_PL_ARMING);
    for(int i=0;i<12;i++){tick(64);}
    CHECK(L.state==PJ_PL_READY);
    /* fast UART handshake: request at 115200, platform switched after the FPGA, confirmed by a status packet */
    CHECK(P.fast_requests==1&&P.baud_changes==1&&L.fast==PJ_PF_FAST);
    /* HELLO advertises the stream and the link clock */
    size_t n;const uint8_t *p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1);
    {char text[512];memcpy(text,p,n);text[n]=0;CHECK(strstr(text,"chromatic-link")&&strstr(text,"PJAV-JPEG-IMA")&&strstr(text,"\"link_khz\":512"));}
    /* HOME: five Jellyfin requests become one Gateway-shaped reply the Pocket parses */
    p=rpc(PJ_RPC_HOME,"{}",64,&n);
    CHECK(pocket_frame[5]==1&&P.calls==5); /* HELLO needs no server request */
    CHECK(pj_library_load(&lib,p,n,1));
    CHECK(lib.row_count==5&&!strcmp(lib.rows[0].title,"My Media")&&!strcmp(lib.rows[1].title,"Continue Watching")&&
          !strcmp(lib.rows[2].title,"Next Up")&&!strcmp(lib.rows[3].title,"Latest Movies")&&!strcmp(lib.rows[4].title,"Latest Shows"));
    CHECK(lib.rows[0].shape==PJ_ART_THUMB&&lib.rows[3].shape==PJ_ART_POSTER&&lib.rows[0].count==2&&lib.rows[1].count==2&&lib.rows[3].count==3&&lib.rows[4].count==2);
    const pj_library_item *m1=&lib.items[lib.rows[1].first],*e1=&lib.items[lib.rows[1].first+1],*s2=&lib.items[lib.rows[4].first+1];
    CHECK(!strcmp(m1->title,"Tab Movie")&&m1->year==1999&&m1->resume_ticks==6000000000ull&&m1->duration_ticks==72000000000ull&&!m1->played&&!m1->folder);
    CHECK(!strcmp(e1->title,"The \"Quoted\" Show")&&!strcmp(e1->subtitle,"S1:E1 - Pilot")&&e1->duration_ticks==14400000000ull);
    CHECK(s2->folder&&!strcmp(s2->title,"Artless"));
    CHECK(lib.items[lib.rows[3].first].handle==m1->handle); /* the same movie in two rows: one handle */
    CHECK(lib.items[lib.rows[3].first+1].played&&!strcmp(lib.items[lib.rows[3].first+1].title,"Zweiter Film \xc3\xa9"));
    unsigned h_m1=m1->handle,h_e1=e1->handle; /* lib is reused by BROWSE/HOME below; keep the handles */
    unsigned s2_handle=s2->handle,folder_handle=lib.items[lib.rows[0].first].handle;
    /* art keys: the movie's thumb-row key and poster-row key differ (different pictures); the artless series has key 0 */
    {pj_json j;CHECK(pj_json_parse(&j,p,n,lib.tokens,4096));int rows=pj_json_find(&j,0,"rows");
     unsigned seen=0;uint64_t key_thumb=0,key_poster=0,key_hero=0,key_artless=1;
     for(unsigned r=(unsigned)rows+1;r<j.tokens[rows].next;r=j.tokens[r].next) {
         int items=pj_json_find(&j,(int)r,"items");uint64_t shape=0;pj_json_u64(&j,pj_json_find(&j,(int)r,"shape"),&shape);
         for(unsigned i=(unsigned)items+1;i<j.tokens[items].next;i=j.tokens[i].next) {
             uint64_t h=0,a=0,hero=0;CHECK(pj_json_u64(&j,pj_json_find(&j,(int)i,"handle"),&h)&&pj_json_u64(&j,pj_json_find(&j,(int)i,"art"),&a)&&pj_json_u64(&j,pj_json_find(&j,(int)i,"hero"),&hero));
             if(h==h_m1){if(shape==PJ_ART_THUMB)key_thumb=a;else key_poster=a;key_hero=hero;}
             if(h==s2_handle)key_artless=a;
             seen++;
         }
     }
     CHECK(seen==lib.count&&key_thumb&&key_poster&&key_thumb!=key_poster&&key_hero&&key_artless==0);
     CHECK(key_poster==pj_tether_art_key(ID_M1,JF_IMG_PRIMARY)&&key_hero==pj_tether_art_key(ID_M1,JF_IMG_BACKDROP));}
    /* ART: poster of the movie in three chunks, exact decoded pixels, art fetched once, decoded once */
    char body[96];int calls=P.calls;unsigned decodes=P.decodes;
    pj_art_begin(&art,h_m1,PJ_ART_POSTER);
    for(unsigned off=0,guard=0;!art.complete&&guard<8;off=(unsigned)art.used,guard++) { /* the Pocket asks from what it has */
        snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":1,\"offset\":%u}",h_m1,off);
        p=rpc(PJ_RPC_ART,body,64,&n);CHECK(pocket_frame[5]==1&&pj_art_chunk(&art,p,n));
    }
    /* 1.3.0: every artwork is decoded in windows of at most 4 KB (the scratch area): a 64x96 poster is 3 windows of 32 rows */
    CHECK(art.complete&&P.calls==calls+1&&P.decodes==decodes+(PJ_ART_POSTER_H+pj_plink_window_rows(PJ_ART_POSTER)-1)/pj_plink_window_rows(PJ_ART_POSTER));
    uint16_t expect=(uint16_t)('a'*131u+0*PJ_ART_POSTER_W+5);CHECK(art.bytes[10]==(expect&255)&&art.bytes[11]==(expect>>8));
    /* thumb of the episode: its own still (Primary), 10,368 bytes in 3 windows of 21 rows */
    pj_art_begin(&art,h_e1,PJ_ART_THUMB);
    for(unsigned off=0,guard=0;!art.complete&&guard<8;off=(unsigned)art.used,guard++) {
        snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":0,\"offset\":%u}",h_e1,off);
        p=rpc(PJ_RPC_ART,body,64,&n);CHECK(pj_art_chunk(&art,p,n));
    }
    CHECK(art.complete&&art.total==PJ_ART_THUMB_W*PJ_ART_THUMB_H*2&&strstr(P.path,"/Items/" ID_E1 "/Images/Primary?fillWidth=96&fillHeight=54"));
    /* backdrop of the movie: 76,800 bytes in 6-row windows (1.3.0), one fetch, one decode per window, exact pixels in order */
    calls=P.calls;decodes=P.decodes;pj_art_begin(&art,h_m1,PJ_ART_BACKDROP);unsigned rounds=0;
    while(!art.complete&&rounds<40) {
        snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":2,\"offset\":%lu}",h_m1,(unsigned long)art.used);
        p=rpc(PJ_RPC_ART,body,256,&n);CHECK(pocket_frame[5]==1&&pj_art_chunk(&art,p,n));rounds++;
    }
    CHECK(art.complete&&art.total==PJ_ART_BACKDROP_W*PJ_ART_BACKDROP_H*2&&P.calls==calls+1&&P.decodes==decodes+(PJ_ART_BACKDROP_H+PJ_PLINK_HERO_ROWS-1)/PJ_PLINK_HERO_ROWS);
    CHECK(strstr(P.path,"/Images/Backdrop?fillWidth=320&fillHeight=120"));
    for(unsigned y=0;y<PJ_ART_BACKDROP_H;y+=17){unsigned x=(y*7)%PJ_ART_BACKDROP_W;uint16_t v=(uint16_t)('a'*131u+y*PJ_ART_BACKDROP_W+x);
        CHECK(art.bytes[(y*PJ_ART_BACKDROP_W+x)*2]==(v&255)&&art.bytes[(y*PJ_ART_BACKDROP_W+x)*2+1]==(v>>8));}
    /* no art: a valid empty reply; the 404 item too */
    snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":1}",s2_handle);calls=P.calls;
    p=rpc(PJ_RPC_ART,body,64,&n);CHECK(pocket_frame[5]==1&&n==8&&pj_av_u32(p)==0&&P.calls==calls);
    pj_art_begin(&art,s2_handle,PJ_ART_POSTER);CHECK(pj_art_chunk(&art,p,n)&&art.complete&&art.total==0);
    /* unknown handle / kind -> error replies, never a crash */
    p=rpc(PJ_RPC_ART,"{\"handle\":77,\"kind\":1}",64,&n);CHECK(pocket_frame[5]==3);
    snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":2}",s2_handle);
    p=rpc(PJ_RPC_ART,body,64,&n);CHECK(pocket_frame[5]==1&&n==8&&pj_av_u32(p)==0); /* backdrop of an art-less item: valid empty */
    p=rpc(PJ_RPC_ART,"{\"handle\":1,\"kind\":4}",64,&n);CHECK(pocket_frame[5]==3); /* an out-of-range kind */
    /* DETAILS: overview and three genres, parsed by the Pocket's pj_details_load */
    snprintf(body,sizeof(body),"{\"handle\":%u}",h_m1);
    p=rpc(PJ_RPC_DETAILS,body,64,&n);CHECK(pocket_frame[5]==1&&strstr(P.path,"ids=" ID_M1 "&fields=Overview,Genres"));
    CHECK(pj_details_load(&details,lib.tokens,h_m1,p,n)&&!strcmp(details.overview,"A long \"quoted\" overview.")&&!strcmp(details.genres,"Drama, Comedy, Thriller"));
    p=rpc(PJ_RPC_DETAILS,"{\"handle\":77}",64,&n);CHECK(pocket_frame[5]==3);
    /* BROWSE a library: items, start/next/total */
    snprintf(body,sizeof(body),"{\"handle\":%u,\"start\":0}",folder_handle);
    p=rpc(PJ_RPC_BROWSE,body,64,&n);CHECK(pocket_frame[5]==1&&pj_library_load(&lib,p,n,0)&&lib.count==3&&lib.next==3&&lib.total==30);
    /* duplicate while pending: no second HTTP request; duplicate after: cached replay, byte-identical */
    int before=replies;calls=P.calls;send_request(PJ_RPC_HOME,"{}",1);tick(0);tick(0);send_request(PJ_RPC_HOME,"{}",0);
    for(int i=0;i<400&&replies==before;i++)tick(64);
    CHECK(replies==before+1&&P.calls==calls+5);
    uint8_t copy[8192];size_t copy_size=reply_size;memcpy(copy,pocket_frame,copy_size);
    send_request(PJ_RPC_HOME,"{}",0);for(int i=0;i<400&&replies==before+1;i++)tick(64);
    CHECK(replies==before+2&&P.calls==calls+5&&reply_size==copy_size&&!memcmp(copy,pocket_frame,copy_size));
    /* a damaged frame (bad CRC) is ignored; the next request is served */
    before=replies;req_size=pj_rpc_encode(req,sizeof(req),PJ_RPC_HELLO,++seq,"{}",2);req[16]^=1;send_request(PJ_RPC_HELLO,"{}",0);
    for(int i=0;i<30;i++)tick(64);
    CHECK(replies==before);
    p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1);
    /* credit: a slow Pocket (6 bytes per 10 ms) never overflows the FPGA queue; a busy UART is retried; 64-byte packets */
    P.max_level=0;P.busy_every=3;p=rpc(PJ_RPC_HOME,"{}",6,&n);CHECK(pocket_frame[5]==1&&P.max_level<=PJ_PLINK_QUEUE-PJ_PLINK_MARGIN+PJ_PLINK_DATA_BYTES);P.busy_every=0;
    CHECK(P.max_packet==PJ_PLINK_DATA_BYTES);
    /* heartbeats about every 250 ms */
    unsigned hb=P.heartbeats;for(int i=0;i<100;i++)tick(64);
    CHECK(P.heartbeats>=hb+3&&P.heartbeats<=hb+5);
    /* ---- playback: START opens the MJPEG+IMA transcode, READ returns PJAV the Pocket's reader accepts ---- */
    pj_syn_avi_build2(&P.avi,128,96,30000,1001,1024,900,4,1); /* 4 s at 29.97 fps, 900-byte pictures, 1 KiB IMA blocks */
    rpc(PJ_RPC_HOME,"{}",64,&n); /* fresh handles after the browse */
    snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-low-30\",\"start_seconds\":12}",h_m1);
    p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1&&P.stream_opens==1);
    CHECK(strstr(P.stream_path,"/Videos/" ID_M1 "/stream.avi?")&&strstr(P.stream_path,"videoCodec=mjpeg")&&strstr(P.stream_path,"adpcm_ima_wav")&&
          strstr(P.stream_path,"&maxWidth=128&maxHeight=96&maxFramerate=30")&&strstr(P.stream_path,"startTimeTicks=120000000")&&!strstr(P.stream_path,"deviceId")&&
          !strstr(P.stream_path,"&width=")&&!strstr(P.stream_path,"&height=")); /* 1.3.9: a box the server fits the title's shape into */
    uint32_t sess=(uint32_t)json_num(p,n,"session");CHECK(sess&&json_num(p,n,"start_seconds")==12);
    /* READ loop with the Pocket's stream_reader: offsets in step, kinds 1 then 3, replays byte-identical */
    pj_stream_reader reader;pj_stream_reader_init(&reader,sess);memset(&rx,0,sizeof(rx));n_cfg=n_vid=n_aud=n_end=0;
    uint64_t offset=0;unsigned reads=0,waits=0,bytes=0,largest=0;int eof=0;
    /* 1.4.0: one stream step fits at most PJ_PLINK_FITS_PER_STEP pictures, however much room the ring has (a pass is
     * not stretched by a run of fits); the ring still fills over a few steps. The check takes what the ring holds
     * itself, as a READ would (into the same reader, the offset kept in step), and then steps the stream by hand. */
    {
        static uint8_t taken[PJ_PLINK_RING];unsigned k=L.ring_count;size_t consumed=0;
        pj_plink_ring_take(&L,taken,k);L.stream_offset+=k;
        CHECK(pj_stream_reader_feed(&reader,taken,k,&consumed,push)==1&&consumed==k);offset+=k;bytes+=k;
        unsigned most=0;uint32_t f0=L.sp.fits;
        for(unsigned i=0;i<12&&L.ring_count<PJ_PLINK_RING-1100u;i++){uint32_t f=L.sp.fits;pj_plink_stream_step(&L);if(L.sp.fits-f>most)most=L.sp.fits-f;}
        CHECK(most==PJ_PLINK_FITS_PER_STEP&&L.sp.fits-f0>PJ_PLINK_FITS_PER_STEP&&L.ring_count>=PJ_PLINK_RING-1100u);
    }
    while(!eof&&reads<6000) {
        snprintf(body,sizeof(body),"{\"session\":%lu,\"offset\":%llu}",(unsigned long)sess,(unsigned long long)offset);
        p=rpc(PJ_RPC_READ,body,512,&n);CHECK(pocket_frame[5]==1&&n>=16&&pj_av_u32(p)==sess&&pj_av_u64(p+4)==offset&&pj_av_u16(p+14)==n-16);
        reads++;
        if(p[12]==2){waits++;CHECK(n==16);continue;}
        if(p[12]==3){CHECK(n==16);eof=1;break;}
        CHECK(p[12]==1&&n>16);
        size_t consumed=0;int r=pj_stream_reader_feed(&reader,p+16,n-16,&consumed,push);CHECK(r==1&&consumed==n-16);
        offset+=n-16;bytes+=(unsigned)(n-16);if(n-16>largest)largest=(unsigned)(n-16);
        if(reads==3){ /* the Pocket re-requests the same READ (a lost reply): byte-identical cached replay */
            uint8_t again[16384];size_t again_size=reply_size;memcpy(again,pocket_frame,again_size);int rb=replies;
            send_request(PJ_RPC_READ,body,0);for(int i=0;i<200&&replies==rb;i++)tick(512);
            CHECK(replies==rb+1&&reply_size==again_size&&!memcmp(again,pocket_frame,again_size));
        }
    }
    CHECK(eof&&pj_stream_reader_eof(&reader)&&rx.closed);
    CHECK(n_cfg==1&&n_end==1&&n_vid==P.avi.frames&&n_aud==P.avi.audio_blocks&&rx.width==128&&rx.height==96&&rx.fps==30&&rx.block==1024&&rx.rate==16000);
    CHECK(last_vpts>0&&last_apts>0&&last_vpts==(uint64_t)(P.avi.frames-1)*(1001000000u/30000u)*9u/100u&&last_apts==(uint64_t)(P.avi.audio_blocks-1)*(1u+2u*(1024u-4u))*45u/8u);
    /* 1.4.0: with the server ahead, a READ carries more than one ring: the ring, filled again and taken again, up to
     * PJ_PLINK_READ_MAX (the response buffer's room) */
    CHECK(largest>PJ_PLINK_RING&&largest<=PJ_PLINK_READ_MAX&&PJ_RPC_HEADER+16u+PJ_PLINK_READ_MAX<=PJ_PLINK_RESPONSE);
    {   /* 1.4.0: the "plink speed" line of this title: READs with data, their sizes, fits; bounded for the console line */
        char speed[PJ_DIAG_MAX],line[PJ_DIAG_MAX+64];
        CHECK(pj_plink_say_speed(&L,now_ms+1000u,0,NULL,speed,sizeof(speed))&&pj_diag_line(line,sizeof(line),0xffffffffu,speed)>0);
        unsigned long avg=0,mx=0,fits=0,st=0;const char *a=strstr(speed," avg="),*m=strstr(speed," max=");
        CHECK(!strncmp(speed,"plink speed down=",17)&&a&&m&&sscanf(a," avg=%lu",&avg)==1&&sscanf(m," max=%lu",&mx)==1);
        CHECK(mx==largest&&avg>0&&avg<=mx);
        CHECK(pj_plink_say_speed(&L,now_ms+1000u,1,NULL,speed,sizeof(speed))&&pj_diag_line(line,sizeof(line),0xffffffffu,speed)>0);
        const char *f=strstr(speed," fits="),*s=strstr(speed," st=");
        CHECK(!strncmp(speed,"plink speed pass=",17)&&f&&s&&sscanf(f," fits=%lu",&fits)==1&&sscanf(s," st=%lu",&st)==1);
        CHECK(fits>=n_vid&&st>0&&strstr(speed," fit=")&&!strstr(speed," fit=0/0 ")&&strstr(speed," uart=0/0/0/0/0/0"));
        CHECK(!pj_plink_say_speed(&L,now_ms+1500u,0,NULL,speed,sizeof(speed))); /* under a second since the last line: nothing */
        /* every field at its largest still makes lines the console takes (each is capped at 99,999) */
        pj_plink_speed keep=L.sp;uint32_t drops=L.fit_drops,down=L.d.down_bytes,stp=L.status_packets;memset(&L.sp,0xff,sizeof(L.sp));
        L.sp.since_ms=now_ms;L.sp.down_base=0;L.sp.fit_drop_base=0;L.sp.status_base=0;L.sp.passes=1;L.sp.ring_samples=1;L.sp.fits=1;
        L.d.down_bytes=0xffffffffu;L.fit_drops=0xffffffffu;L.status_packets=0xffffffffu;
        pj_pld_uart big;memset(&big,0xff,sizeof(big));
        for(int w=0;w<2;w++) {
            CHECK(pj_plink_say_speed(&L,now_ms+1000u,w,&big,speed,sizeof(speed)));
            CHECK(strlen(speed)<PJ_DIAG_MAX-24u&&pj_diag_line(line,sizeof(line),0xffffffffu,speed)>0);
            CHECK(w?strstr(speed," uart=99999/99999/99999/99999/99999/99999")!=NULL:strstr(speed," reads=999.9 ")&&strstr(speed," max=99999 "));
        }
        L.sp=keep;L.fit_drops=drops;L.d.down_bytes=down;L.status_packets=stp;
    }
    printf("stream: %u PJAV bytes in %u READ replies (%u held and answered empty, largest %u B), %u pictures, %u audio blocks, END at %llu ticks\n",
           bytes,reads,waits,largest,n_vid,n_aud,(unsigned long long)(last_vpts>last_apts?last_vpts:last_apts));
    /* a READ out of step or for another session is an error; the title stays open until STOP */
    p=rpc(PJ_RPC_READ,"{\"session\":1,\"offset\":0}",64,&n);CHECK(pocket_frame[5]==3);
    snprintf(body,sizeof(body),"{\"session\":%lu,\"offset\":5}",(unsigned long)sess);p=rpc(PJ_RPC_READ,body,64,&n);CHECK(pocket_frame[5]==3);
    /* ART while a stream is open is refused politely (the frame buffer is in use) */
    snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":1,\"offset\":0}",h_e1);p=rpc(PJ_RPC_ART,body,64,&n);CHECK(pocket_frame[5]==3);
    p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1&&!P.stream_open);
    /* 1.3.9 (every START failed "picture size not supported on the Pocket"): the server keeps the
     * title's shape, so a 16:9 title asked for in the 128x96 box comes as 128x72, a 2.39:1 film as 128x54. Each is played
     * letterboxed at 128x96 (the CONFIG says so, every picture is 128x96 with Annex K codes and no tables: pocket_ready),
     * every picture the server sent arrives. 160x90 (an older 160-wide profile) is played at 160x96. */
    {
        static const unsigned shapes[3][3]={{128,72,96},{128,54,96},{160,90,96}};
        for(unsigned s=0;s<3;s++) {
            pj_syn_avi_free(&P.avi);pj_syn_avi_build2(&P.avi,shapes[s][0],shapes[s][1],24000,1001,1024,0,2,1);
            snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-512-24\",\"start_seconds\":0}",h_m1);
            p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1);sess=(uint32_t)json_num(p,n,"session");
            pj_stream_reader_init(&reader,sess);memset(&rx,0,sizeof(rx));n_cfg=n_vid=n_aud=n_end=0;offset=0;eof=0;
            for(unsigned guard=0;!eof&&guard<6000;guard++) {
                snprintf(body,sizeof(body),"{\"session\":%lu,\"offset\":%llu}",(unsigned long)sess,(unsigned long long)offset);
                p=rpc(PJ_RPC_READ,body,512,&n);CHECK(pocket_frame[5]==1&&n>=16);
                if(p[12]==2)continue;
                if(p[12]==3){eof=1;break;}
                size_t consumed=0;CHECK(pj_stream_reader_feed(&reader,p+16,n-16,&consumed,push)==1&&consumed==n-16);offset+=n-16;
            }
            CHECK(eof&&n_cfg==1&&n_end==1&&rx.width==shapes[s][0]&&rx.height==shapes[s][2]&&n_vid==P.avi.frames&&L.fit_drops==0&&rx.fps==24);
            p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1);
        }
    }
    /* a title the server sends in a size the Pocket cannot take even letterboxed (narrower than 4:3: the box's height
     * is reached first, so the width is not 128) is refused at the first READ, with a reason */
    pj_syn_avi_free(&P.avi);pj_syn_avi_build2(&P.avi,120,96,30,1,1024,0,1,1);
    snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-min-24\",\"start_seconds\":0}",h_m1);
    p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1);sess=(uint32_t)json_num(p,n,"session");
    snprintf(body,sizeof(body),"{\"session\":%lu,\"offset\":0}",(unsigned long)sess);p=rpc(PJ_RPC_READ,body,64,&n);
    CHECK(pocket_frame[5]==3&&strstr((const char *)p,"picture size"));
    p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1);
    /* 1.3.9: pictures the decoder could not take (here not JPEGs at all) are left out; 30 in a row end the stream with
     * a reason, never a picture the Pocket would fault on */
    pj_syn_avi_free(&P.avi);pj_syn_avi_build(&P.avi,128,96,30,1,1024,600,2);
    snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-min-24\",\"start_seconds\":0}",h_m1);
    p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1);sess=(uint32_t)json_num(p,n,"session");
    pj_stream_reader_init(&reader,sess);memset(&rx,0,sizeof(rx));n_cfg=n_vid=n_aud=n_end=0;offset=0;
    {
        int failed=0;
        for(unsigned guard=0;!failed&&guard<2000;guard++) {
            snprintf(body,sizeof(body),"{\"session\":%lu,\"offset\":%llu}",(unsigned long)sess,(unsigned long long)offset);
            p=rpc(PJ_RPC_READ,body,512,&n);
            if(pocket_frame[5]==3){CHECK(strstr((const char *)p,"pictures not in a form"));failed=1;break;}
            if(p[12]==2)continue;
            CHECK(p[12]==1);size_t consumed=0;CHECK(pj_stream_reader_feed(&reader,p+16,n-16,&consumed,push)==1);offset+=n-16;
        }
        CHECK(failed&&n_vid==0&&L.fit_drops==PJ_PLINK_FIT_FAILS&&L.stream_state==PJ_PS_FAILED);
    }
    p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1);
    /* 1.3.6 (credit review; shared/pocket_tether.h now has eight profiles and a link rate in each): every row is found
     * by its name and its own size, frame rate and bit rate go to the server; a name the table does not have, or no
     * name at all, gets row 0, the smallest (256 kHz, 24 fps), and the reply says which was taken. */
    {
        unsigned rows=0;const pj_tether_profile *t=pj_tether_profiles(&rows);char want[160];
        CHECK(rows==8u&&PJ_TETHER_LINK_PROFILES<=rows&&!strcmp(t[0].name,"pocket-link-256-24")&&t[0].link_khz==256u);
        for(unsigned i=0;i<rows+2u;i++) {
            const pj_tether_profile *r=i<rows?&t[i]:&t[0];
            CHECK(strlen(r->name)<40u&&pj_tether_profile_find(r->name)==r);
            if(i<rows)snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"%s\",\"start_seconds\":0}",h_m1,r->name);
            else if(i==rows)snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-1000-60\",\"start_seconds\":0}",h_m1);
            else snprintf(body,sizeof(body),"{\"handle\":%u,\"start_seconds\":0}",h_m1);
            p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1&&n<sizeof(want));
            snprintf(want,sizeof(want),"&maxWidth=%u&maxHeight=%u&maxFramerate=%u&videoBitRate=%u&startTimeTicks=0",r->width,r->height,r->fps,r->video_kbit*1000u);
            CHECK(strstr(P.stream_path,want)&&!strstr(P.stream_path,"khz"));
            {char text[160];memcpy(text,p,n);text[n]=0;snprintf(want,sizeof(want),"\"profile\":\"%s\"",r->name);CHECK(strstr(text,want));}
            p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1&&!P.stream_open);
        }
    }
    /* an unknown handle at START is refused */
    p=rpc(PJ_RPC_START,"{\"handle\":77,\"profile\":\"x\"}",64,&n);CHECK(pocket_frame[5]==3);
    /* ---- launcher 1.3.0 ---- */
    {
        /* Memory: the gateway the launcher allocates is about 40 KB (was 69 KB): one scratch area for tokens / an artwork
         * window / the PJAV ring, ids as 16 bytes, a 12 KB reply buffer. 1.3.7: in four heap pieces, none above 24 KB
         * (the console had 90 KB free but no block above 36,864 B), summing to the whole the launcher checks for. */
        CHECK(PJ_PLINK_MEMORY<=41u*1024u&&sizeof(pj_plink_scratch)==PJ_PLINK_TOKENS*sizeof(pj_json_token)&&sizeof(pj_plink_scratch)>=PJ_PLINK_ART_WINDOW);
        CHECK(sizeof(pj_plink_handle)==69u&&PJ_PLINK_RING<=sizeof(pj_plink_scratch)&&L.buf&&L.response&&L.scratch);
        CHECK(PJ_PLINK_PIECE_LIMIT==24u*1024u&&pj_plink_piece_max()<=PJ_PLINK_PIECE_LIMIT&&pj_plink_piece_max()<=16u*1024u&&
              PJ_PLINK_MEMORY==sizeof(pj_plink)+PJ_PLINK_BUF+PJ_PLINK_RESPONSE+sizeof(pj_plink_scratch)&&
              pj_plink_piece_max()==(sizeof(pj_plink)>PJ_PLINK_BUF?sizeof(pj_plink):PJ_PLINK_BUF));
        uint8_t packed[16];char hex[33];CHECK(pj_plink_pack(packed,ID_M1)&&!strcmp(pj_plink_unpack(hex,packed),ID_M1));
        CHECK(!pj_plink_pack(packed,"0123")&&!pj_plink_pack(packed,"ABCD0000000000000000000000000001"));
        for(unsigned i=0;i<16;i++)CHECK(packed[i]==0);
        /* BROWSE pages are 6 items, so a page of the owner's library fits the 12 KB buffer (the console app pages 8) */
        p=rpc(PJ_RPC_HOME,"{}",64,&n);CHECK(pj_library_load(&lib,p,n,1));unsigned folder=lib.items[lib.rows[0].first].handle;
        snprintf(body,sizeof(body),"{\"handle\":%u,\"start\":0}",folder);p=rpc(PJ_RPC_BROWSE,body,64,&n);
        CHECK(pocket_frame[5]==1&&strstr(P.path,"&limit=6&"));
        gate_begin(); /* 1.3.6 review: from here to the end of the held stream the console is offered every line on every pass */
        /* The status line: Home counted the tiles with a picture; ART makes it "syncing N of M"; quiet for 3 s:
         * "connected"; no request for 9 s: "unplugged"; a HELLO again: "connected"; a stream: "streaming". */
        p=rpc(PJ_RPC_HOME,"{}",64,&n);CHECK(pj_library_load(&lib,p,n,1));
        unsigned with_art=0;for(unsigned i=0;i<lib.count;i++)if(lib.items[i].art_key)with_art++;
        CHECK(L.sync_total==with_art&&with_art>0&&L.sync_done==0);
        pj_plink_status v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_CONNECTED);
        unsigned first=lib.items[lib.rows[3].first].handle; /* a poster with art */
        snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":1,\"offset\":0}",first);p=rpc(PJ_RPC_ART,body,64,&n);
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_SYNCING&&v.done==1&&v.total==with_art);
        snprintf(body,sizeof(body),"{\"handle\":%u,\"kind\":1,\"offset\":4096}",first);p=rpc(PJ_RPC_ART,body,64,&n);
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_SYNCING&&v.done==1); /* later windows of the same picture: still 1 */
        for(int i=0;i<310;i++)tick(64);
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_CONNECTED);
        CHECK(gate_linked==1&&!gate_free&&!gate_rejects&&gate_saw==1u<<9); /* connected, syncing, connected: the one short sum */
        for(int i=0;i<600;i++)tick(64);
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_UNPLUGGED&&L.state==PJ_PL_READY); /* the gateway itself keeps waiting */
        /* unplugged: the lines of a link that does not work are back (counters, last capture, ring); the captures
         * that completed while it worked are not said as "plink cap" now */
        CHECK(gate_linked==1&&gate_free>=5&&(gate_saw&0x3du)==0x3du&&!(gate_saw&0x80u)&&!gate_rejects);
        unsigned free_lines=gate_free;
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_CONNECTED);
        pj_syn_avi_free(&P.avi);pj_syn_avi_build2(&P.avi,128,96,30000,1001,1024,900,2,1);
        snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-low-30\",\"start_seconds\":0}",first);
        p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1&&P.stream_open);
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_STREAMING);
        /* 1.3.6 review: a stream held for over a minute (the Pocket asks every 2 s; here a READ the gateway refuses,
         * so the title stays open). The console is offered every line on every pass and gets one short sum every
         * 30 s: no capture, no ring, no hex. Of two frames refused 2 s apart the first is said at once, the second
         * 10 s after it. */
        {
            uint8_t raw[32],wire[48];
            CHECK(gate_free==free_lines&&gate_linked==1);
            for(int round=0;round<33;round++) {
                p=rpc(PJ_RPC_READ,"{\"session\":1,\"offset\":0}",64,&n);CHECK(pocket_frame[5]==3);
                if(round==3){hello_raw(raw);raw[13]^=1;upstream(wire,wire_of(raw,18,wire));}
                if(round==4){hello_raw(raw);raw[0]='X';upstream(wire,wire_of(raw,18,wire));}
                for(int i=0;i<200;i++){tick(64);CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_STREAMING);}
                if(round==3||round==4)CHECK(gate_rejects==1&&L.d.frames_bad>=(round==3?1u:2u));
            }
            CHECK(P.stream_open&&gate_free==free_lines&&gate_linked>=3&&gate_linked<=4&&gate_rejects==2&&!(gate_saw&0x80u));
            gate_on=0;
        }
        /* The Pocket leaves playback without STOP and asks for Home: the stream closes first (its ring is the tokens' room). */
        p=rpc(PJ_RPC_HOME,"{}",64,&n);CHECK(pocket_frame[5]==1&&!P.stream_open&&L.stream_state==PJ_PS_NONE&&pj_library_load(&lib,p,n,1)&&lib.row_count==5);
        /* The Pocket moves on while a server request is out: that answer is dropped when it comes, and until then nothing
         * new is fetched and no stream opens (both would use the buffer the late answer is written into). */
        snprintf(body,sizeof(body),"{\"handle\":%u}",first);
        int r0=replies;send_request(PJ_RPC_DETAILS,body,1);tick(64);CHECK(P.pending&&L.http_busy);P.deliver_at=150; /* a slow server */
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&L.http_stale&&!L.http_busy&&P.pending);
        snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-low-30\",\"start_seconds\":0}",first);
        unsigned opens=P.stream_opens;p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==3&&strstr((const char *)p,"Busy")&&P.stream_opens==opens);
        int calls_before=P.calls;p=rpc(PJ_RPC_BROWSE,"{\"start\":0}",64,&n);CHECK(pocket_frame[5]==3&&P.calls==calls_before);
        for(int i=0;i<200&&P.pending;i++)tick(64);
        CHECK(!P.pending&&!L.http_stale&&replies==r0+3); /* HELLO, START refused, BROWSE refused: the late DETAILS answer made no reply */
        p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1&&P.stream_opens==opens+1);
        p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1&&!P.stream_open);
    }
    /* an FPGA fault ends the mode; the module asks for GAME and returns the UART to 115200; a new init recovers */
    /* ---- launcher 1.3.1 ---- */
    {
        /* F: the cable comes out in mid-byte (the FPGA's only fault is the link-byte timeout): "unplugged", not a failure;
         * ENTER clears the sticky bit; the Pocket's next request is "connected" again. */
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);unsigned enters=P.enters;
        P.faults=1;status(now_ms,NULL,0);
        CHECK(L.state==PJ_PL_READY&&pj_plink_view(&L,now_ms).what==PJ_PLV_UNPLUGGED&&P.enters==enters+1&&P.faults==0&&P.mode==2);
        for(int i=0;i<30;i++){tick(64);}
        CHECK(L.state==PJ_PL_READY&&P.enters==enters+1);
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&pj_plink_view(&L,now_ms).what==PJ_PLV_CONNECTED);
        /* B: the credit counts what still waits in the ESP32's transmit ring. A slow UART (6 KB/s) under a Home reply,
         * a slow Pocket: no byte dropped at the FPGA, and the reply arrives whole. */
        P.async=1;P.uart_per_tick=60;P.budget=0;P.allow_overflow=1;P.overflows=0;P.max_level=0;
        p=rpc(PJ_RPC_HOME,"{}",6,&n);CHECK(pocket_frame[5]==1&&P.overflows==0&&P.max_level<=PJ_PLINK_QUEUE&&pj_library_load(&lib,p,n,1));
        /* ...and the 1.3.0 rule (status resets the count, the ring ignored) overflows the FPGA's queue in the same run:
         * the test sees the bug it guards against. */
        L.x.tx_pending=NULL;P.overflows=0;int before=replies;send_request(PJ_RPC_HOME,"{}",1);
        for(int i=0;i<1500&&replies==before;i++)tick(6);
        CHECK(P.overflows>0);
        L.x.tx_pending=tx_pending;P.async=0;uart_flush(&P,1);
        for(int i=0;i<400;i++){now_ms+=10;drain(512);if(now_ms%50==0)status(now_ms,NULL,0);pj_plink_step(&L,now_ms);}
        P.allow_overflow=0;pj_wire_rx_init(&pocket_rx,pocket_frame,sizeof(pocket_frame));
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1); /* the link is healthy again */
        /* 1.3.6 (credit review): a status that is one loop pass old. The bridge wrote it just after the launcher's last
         * step; the launcher reads it a pass later, when what the UART delivered in between is in the bridge's queue but
         * in neither the status's level nor what the transmit ring still holds. Home replies (1976 bytes: twice the
         * queue) down a cable that has stopped for 800 ms, crawls at 7 bytes a pass, or runs at 16/64/256/512 kHz
         * (20/80/320/640 bytes a pass), the UART carrying 113, 115 or 524 bytes a pass (115,200 baud, a little under
         * it, and 524,288 baud), a status every pass and every 50 ms at each phase: the queue never above
         * PJ_TETHER_QUEUE (apply_packet stops the check at the first byte too many: nothing may overflow here), no
         * fault, each reply whole. Checked on copies (docs/Design-notes.md): the rule before (only what the
         * ring holds NOW counted at a status) and "the ring now plus what the previous pass handed over" both fail at
         * the first run (113 bytes a pass, stopped cable); the rule without its packet in flight fails at 113 bytes a
         * pass down the 7-byte cable.
         * And a status older than a pass (P.stale 2): the bridge wrote it 20, 52 or 68 UART bytes BEFORE the launcher's
         * previous step (0.4 to 1.3 ms at 524,288 baud, 1.7 to 5.9 ms at 115,200: its own 14 bytes on the wire and the
         * driver's receive time), so it lacks also the packet that finished leaving the UART in that time. All of the
         * above again for each. The rule without its `busy` term fails at 20 bytes (115 bytes a pass, the 7-byte
         * cable); the rule as it is holds in every order tried up to 68 bytes (one packet on the wire, so one packet end
         * at most in the time: its reasoned limit), in some up to 83, and fails in others from 82 on.
         * Credit fix: on the console a status can be ~155 UART bytes older than that step or more (arbiter
         * wait, its own 14 bytes, the receive FIFO, scheduling), so it is run written 76, 100, 155 and 200 bytes before
         * it too, each also with the transmit count one byte short (shift_short: the byte in the shift register). The
         * one-packet rule (busy 16) fails here from 76 bytes on; the three-packet rule (busy 144) holds at every one.
         * The model's early tail is taken from one pass's UART bytes, so at 113/115 bytes a pass 155 and 200 mean that
         * whole pass. */
        {
            static const unsigned uarts[3]={113,115,524},cables[6]={0,7,20,80,320,640};
            static const unsigned earlies[CREDIT_EARLIES]={0,20,52,68,76,100,155,200};
            unsigned runs=0,peak=0,fast_ticks=0;
            CHECK(L.state==PJ_PL_READY&&!P.allow_overflow&&L.x.tx_pending==tx_pending);
            P.async=1;P.stale=1;P.budget=0;P.idle=0;
            /* 1.4.0: every run twice, without and with the line's idle time (pocket_link.h PJ_PLINK_IDLE_BYTES: the
             * allowance for a late status is dropped after 400 idle byte times, twice the 200 of the latest status here) */
            for(unsigned w=0;w<2;w++)for(unsigned s=0;s<2;s++)for(unsigned e=0;e<CREDIT_EARLIES;e++)for(unsigned u=0;u<3;u++)for(unsigned c=0;c<6;c++)for(unsigned ph=0;ph<6;ph++) {
                L.x.idle_us=w?model_idle_us:NULL;
                shift_short=s;P.stale=earlies[e]?2:1;P.early=earlies[e];
                status_period=ph?50:10;status_phase=ph?(ph-1u)*10u:0;
                P.uart_per_tick=uarts[u];P.max_level=0;int before=replies;unsigned ticks=0,overflows=P.overflows;
                send_request(PJ_RPC_HOME,"{}",1);
                while(replies==before&&ticks<4000u){tick(cables[c]?cables[c]:ticks<80u?0u:320u);ticks++;}
                CHECK(replies==before+1&&pocket_frame[5]==1&&pocket_frame[4]==PJ_RPC_HOME&&pj_av_u32(pocket_frame+8)==seq&&reply_size>PJ_TETHER_QUEUE);
                CHECK(P.max_level<=PJ_TETHER_QUEUE&&P.overflows==overflows&&!L.fpga_faults&&!P.faults&&L.state==PJ_PL_READY);
                if(P.max_level>peak)peak=P.max_level;
                if(u==2&&c==5&&ticks>fast_ticks)fast_ticks=ticks;
                for(int i=0;i<60;i++)tick(640);
                CHECK(P.ring_used<=9u&&!P.level&&!L.tx_active);runs++; /* at most a heartbeat on its way */
            }
            /* and the rule costs the fast path nothing it had: a whole Home reply in a handful of passes (observed: the
             * bound is what this model gave before the rule changed, plus two passes) */
            CHECK(runs==2u*2u*108u*CREDIT_EARLIES&&peak>=PJ_TETHER_QUEUE/2u&&fast_ticks<=CREDIT_FAST_TICKS);
            CHECK(idle_cleared>100u); /* the idle rule was really used in these runs */
            shift_short=0;L.x.idle_us=NULL;
            printf("credit review: a status one pass old, and written 20/52/68/76/100/155/200 UART bytes before the pass before, each also with the transmit count one byte short (shift register), and each without and with the line's idle time (1.4.0: the allowance dropped %u times), %u Home replies down a stopped cable, a 7-byte-a-pass cable and 16/64/256/512 kHz, the UART at 113/115/524 bytes a pass: queue peak %u of %u, nothing dropped, no fault; at 512 kHz and the fast UART a reply takes at most %u passes\n",
                   idle_cleared,runs,peak,(unsigned)PJ_TETHER_QUEUE,fast_ticks);
            P.stale=0;P.early=P.tail=0;P.async=0;status_period=50;status_phase=0;uart_flush(&P,1);
            for(int i=0;i<40;i++){now_ms+=10;drain(512);if(now_ms%50==0)status(now_ms,NULL,0);pj_plink_step(&L,now_ms);}
            p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1);
        }
        /* A: the leave is never dropped, and said at both rates. At the fast rate: fast, then 115,200. */
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST&&P.uart_fast==1);
        unsigned sure=P.sure;P.busy_every=1; /* a full transmit ring: every ordinary send refused */
        sure_mark();pj_plink_stop(&L);P.busy_every=0;
        CHECK(P.sure==sure+2&&P.sure_rate[sure%8]==1&&P.sure_rate[(sure+1)%8]==0&&P.sure_rate[(sure+2)%8]==-1&&P.uart_fast==0&&P.mode==0&&L.fast==PJ_PF_SLOW);
        /* Just after asking for the fast rate (the FPGA may be fast already, the MCU not): 115,200, fast, 115,200. */
        P.fast=0;P.fast_pending=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        for(int i=0;i<3;i++){now_ms+=10;status(now_ms,NULL,0);pj_plink_step(&L,now_ms);if(L.fast==PJ_PF_REQUESTED)break;}
        CHECK(L.fast==PJ_PF_REQUESTED);sure=P.sure;
        sure_mark();pj_plink_stop(&L);
        CHECK(P.sure==sure+3&&P.sure_rate[(sure)%8]==0&&P.sure_rate[(sure+1)%8]==1&&P.sure_rate[(sure+2)%8]==0&&P.sure_rate[(sure+3)%8]==-1&&P.uart_fast==0);
        /* back to a running link for the checks below (the count of sure sends runs on: sure_rate is a ring) */
        P.fast=0;P.fast_pending=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);for(int i=0;i<12;i++){tick(64);}
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST);
    }
    P.faults=4;status(now_ms,NULL,0);CHECK(L.state==PJ_PL_FAILED);CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_STOPPED&&!strcmp(pj_plink_view(&L,now_ms).why,"link fault"));
    CHECK((gate_once()&0x20du)==0x0du); /* 1.3.6 review: stopped: the lines of a link that does not work */
    unsigned bc=P.baud_changes;CHECK(pj_plink_step(&L,now_ms)==-1&&P.leaves>=2&&P.baud_changes==bc+1);
    P.faults=0;P.fast=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
    CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_WAITING); /* arming */
    for(int i=0;i<12;i++){tick(64);}
    CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_WAITING); /* ready, no Pocket yet */
    CHECK((gate_once()&0x20du)==0x0du); /* 1.3.6 review: waiting: the same */
    CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST);
    /* stale status (no packet for a second) ends the mode. 1.3.9 (handed to stock mid-session): the
     * step that finds it leaves at once, at both rates, and puts the UART back to 115,200 in that same step; the
     * launcher steps a failed link no more (jellyfin_esp32.c pj_jl_tick), so a leave left for "the next step" waited
     * for the 3 s retry with the MCU at 524,288 baud and the launcher's button readings unreadable. */
    {
        unsigned bc=P.baud_changes,lv=P.leaves;int r=0;uint32_t last=now_ms;
        for(int i=0;i<110&&r==0;i++){now_ms+=10;r=pj_plink_step(&L,now_ms);}
        CHECK(r==-1&&L.state==PJ_PL_FAILED&&!strcmp(L.why,"FPGA status stale")&&now_ms-last>PJ_PLINK_STATUS_STALE_MS&&now_ms-last<=PJ_PLINK_STATUS_STALE_MS+20u);
        CHECK(P.uart_fast==0&&P.baud_changes==bc+1&&P.leaves>=lv+2&&L.fast==PJ_PF_SLOW&&P.mode==0);
    }
    pj_plink_stop(&L);CHECK(L.state==PJ_PL_OFF&&L.fast==PJ_PF_SLOW);
    /* ---- launcher 1.3.5 (console trial: "Pocket link" failed fast) ---- */
    {
        /* The fast switch never confirmed (the first 18.41 image could not switch under the open overlay's stock
         * stream): the link now KEEPS running at 115,200 instead of ending. The platform is put back to the stock
         * rate, a heartbeat goes out at once, the reason is kept, the stage reads "waiting for the Pocket". */
        P.fast=0;P.fast_pending=0;unsigned bc0=P.baud_changes,hb0=P.heartbeats;
        CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        CHECK(pj_plink_stage(&L)==PJ_PST_ENTER);
        for(int i=0;i<60;i++){now_ms+=10;if(now_ms%50==0){uint8_t s[10]={2,0,0,0,255,255,255,255,255,255};pj_plink_fpga_packet(&L,PJ_RX_PLINK_STATUS,s,10,now_ms);}P.fast_pending=0;if(pj_plink_step(&L,now_ms)<0)break;
                              if(L.fast==PJ_PF_REQUESTED||L.fast==PJ_PF_SWITCHED)CHECK(pj_plink_stage(&L)==PJ_PST_FAST);}
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_OFF&&!strcmp(L.why,"fast UART not confirmed")&&L.fast_failed==1);
        CHECK(P.uart_fast==0&&P.baud_changes==bc0+2&&P.heartbeats>hb0); /* up, then back down; a heartbeat at the stock rate at once */
        CHECK(pj_plink_stage(&L)==PJ_PST_POCKET&&L.status_packets>0);
        pj_plink_status v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_WAITING&&v.slow==1);
        /* No second attempt at the fast rate this session; the status packets keep the link alive at 115,200. */
        unsigned fr=P.fast_requests;for(int i=0;i<40;i++)tick(64);
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_OFF&&P.fast_requests==fr&&P.uart_fast==0);
        /* HELLO then advertises no stream (115,200 cannot carry it); the library still works; START is refused with
         * a reason the Pocket can show; the status reads "slow" once the Pocket is there. */
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1);
        {char text[512];memcpy(text,p,n);text[n]=0;CHECK(strstr(text,"\"compressed_stream\":\"none\"")&&strstr(text,"\"pocket_decoder_ready\":false")&&!strstr(text,"PJAV"));}
        v=pj_plink_view(&L,now_ms);CHECK(v.what==PJ_PLV_CONNECTED&&v.slow==1&&pj_plink_stage(&L)==PJ_PST_LINKED);
        p=rpc(PJ_RPC_HOME,"{}",64,&n);CHECK(pocket_frame[5]==1&&pj_library_load(&lib,p,n,1)&&lib.row_count==5);
        unsigned slow_h=lib.items[lib.rows[1].first].handle;
        snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-low-30\",\"start_seconds\":0}",slow_h);
        unsigned opens=P.stream_opens;p=rpc(PJ_RPC_START,body,64,&n);
        CHECK(pocket_frame[5]==3&&strstr((const char *)p,"too slow")&&P.stream_opens==opens&&L.stream_state==PJ_PS_NONE);
        /* Leaving a slow link: one sure leave at 115,200, no rate change (both ends are there already). */
        unsigned sure=P.sure;bc0=P.baud_changes;sure_mark();pj_plink_stop(&L);
        CHECK(sure>=8&&P.sure==sure+1&&P.sure_rate[sure%8]==0&&P.sure_rate[(sure+1)%8]==-1&&P.baud_changes==bc0&&P.mode==0&&L.state==PJ_PL_OFF);
        /* Stage 1 failures are told apart: never a status packet (wrong image, or the UART delivered nothing) vs
         * packets whose mode never became 2 (the bridge heard the ENTER and refused it). */
        P.fast=0;P.fast_pending=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        /* 1.3.6: not given up before 4.5 s, ENTER said again every 500 ms meanwhile (ten in all), the screen waiting */
        {uint32_t began=now_ms;unsigned enters=P.enters;
         for(int i=0;i<600&&pj_plink_step(&L,now_ms)==0;i++){CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_WAITING&&pj_plink_stage(&L)==PJ_PST_ENTER);now_ms+=10;}
         CHECK(now_ms-began==PJ_PLINK_ARM_MS+10u&&P.enters-enters==PJ_PLINK_ARM_MS/PJ_PLINK_ENTER_MS&&PJ_PLINK_ARM_MS>=4000u);}
        CHECK(L.state==PJ_PL_FAILED&&!strcmp(L.why,"no reply from the FPGA bridge")&&L.status_packets==0&&pj_plink_stage(&L)==PJ_PST_ENTER);
        CHECK(pj_plink_view(&L,now_ms).what==PJ_PLV_STOPPED&&!strcmp(pj_plink_view(&L,now_ms).why,"no reply from the FPGA bridge"));
        pj_plink_stop(&L);
        P.fast=0;P.fast_pending=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        for(int i=0;i<600;i++){now_ms+=10;if(now_ms%50==0){uint8_t s[10]={0,0,0,0,255,255,255,255,255,255};pj_plink_fpga_packet(&L,PJ_RX_PLINK_STATUS,s,10,now_ms);}if(pj_plink_step(&L,now_ms)<0)break;}
        CHECK(L.state==PJ_PL_FAILED&&!strcmp(L.why,"FPGA did not enter link mode")&&L.status_packets>0&&pj_plink_stage(&L)==PJ_PST_ENTER);
        pj_plink_stop(&L);
        bridge_checks(ops,extra);
        /* The stages of a healthy link: entering, fast UART, waiting for the Pocket, linked, streaming. */
        P.fast=0;P.fast_pending=0;P.mode=0;CHECK(pj_plink_init(&L,ops,"0123456789abcdef0123456789abcdef",now_ms));pj_plink_extras(&L,extra);
        CHECK(pj_plink_stage(&L)==PJ_PST_ENTER);
        {unsigned seen_fast=0;for(int i=0;i<12;i++){tick(64);if(pj_plink_stage(&L)==PJ_PST_FAST)seen_fast=1;}CHECK(seen_fast);}
        CHECK(L.state==PJ_PL_READY&&L.fast==PJ_PF_FAST&&pj_plink_stage(&L)==PJ_PST_POCKET);
        p=rpc(PJ_RPC_HELLO,"{}",64,&n);CHECK(pocket_frame[5]==1&&pj_plink_stage(&L)==PJ_PST_LINKED);
        {char text[512];memcpy(text,p,n);text[n]=0;CHECK(strstr(text,"PJAV-JPEG-IMA"));} /* at the fast rate the stream is back */
        pj_syn_avi_free(&P.avi);pj_syn_avi_build2(&P.avi,128,96,30000,1001,1024,900,1,1);
        p=rpc(PJ_RPC_HOME,"{}",64,&n);CHECK(pj_library_load(&lib,p,n,1));
        snprintf(body,sizeof(body),"{\"handle\":%u,\"profile\":\"pocket-link-jpeg-low-30\",\"start_seconds\":0}",lib.items[lib.rows[1].first].handle);
        p=rpc(PJ_RPC_START,body,64,&n);CHECK(pocket_frame[5]==1&&pj_plink_stage(&L)==PJ_PST_STREAM);
        p=rpc(PJ_RPC_STOP,"{}",64,&n);CHECK(pocket_frame[5]==1&&pj_plink_stage(&L)==PJ_PST_LINKED);
        pj_plink_stop(&L);CHECK(pj_plink_stage(&L)==PJ_PST_OFF);
    }
    diag_checks(ops,extra);
    log_checks(ops,extra,0);log_checks(ops,extra,1);
    ack_checks(ops,extra);
    pj_syn_avi_free(&P.avi);
    printf("PASS: Pocket-link gateway v2: fast UART handshake, HELLO with PJAV + link 512 kHz, Home with art keys, poster/thumb/backdrop windows, details, errors, browse, cached replay, bad CRC, 64-byte credit, heartbeats, START/READ/STOP PJAV stream from a synthetic AVI, unsupported size refused, fault/stale exits; 1.3.5: an unconfirmed fast rate keeps the link at 115,200 (no stream advertised, START refused with a reason), stage 1 reasons no-reply vs refused, stages 1-5; 1.3.0: 40 KB gateway (shared scratch, packed ids, 6-item pages), status line waiting/connected/syncing N of M/streaming/unplugged/stopped, Home closes an abandoned stream, an abandoned server request is dropped and blocks new fetches and streams until it returns; 1.3.1: mid-byte unplug shows unplugged, credit counts the ESP32 TX ring (the old rule overflows the same run), the leave is sent surely at both rates, no heartbeat while the rates may differ; 1.3.6: link diagnostics through the real packet parser in status packets of 1..6 bytes (HELLO intact, late and early by 1..7 bits, inverted, bit-reversed, one bit lost or doubled at every position, zeros, noise, cut short; every refusal reason with the frame head; torn frames and stray bytes; fault bits counted per appearance; unfed bytes; packet CRC and length counters; capture gap, settle and ring; line gating and worst-case line length; every screen sentence); 1.3.6 review: a capture under four bytes reads short, a fault bit that stuck through a retry is one appearance, the real gateway offered every line on every pass says one short sum per 30 s and no hex while connected/syncing/streaming (a stream held 66 s) and a refused frame at most once in 10 s, all lines back when unplugged/stopped/waiting, a capture is never said under both names within 30 s, and after 10 and 20 minutes of waiting the 16 KB log still holds the link's age and first capture (plink rx first); 1.3.6 credit review: the credit counts from the previous pass (statuses one pass old, a stopped and a crawling cable, 16 to 512 kHz, a byte-exact transmit ring: nothing dropped);: three packets allowed for, statuses written up to 200 UART bytes before the previous step and a transmit count one byte short (shift register) drop nothing; every row of the 8-row profile table taken by name and an unknown name taken as row 0; 1.5.0: the Pocket-acknowledged credit bound holds over stale/late/sparse ACKs and every cable, falls back to the 1.4.0 rule when it stands still, refuses bad/old/damaged ACKs, counts a repeated reply from its first sending, and a +200 B lie breaks it\n");
    return 0;
}
