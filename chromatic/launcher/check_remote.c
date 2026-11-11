/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Host test of remote control (shared/remote.h, service.h, service_screen.h). No hardware.
 *   check_remote <out-dir>                      unit checks, launcher scenarios, previews
 *   check_remote serve-usb <flash> <key|none> [nocart]
 *       the console UART on stdin/stdout. Lines starting with '#' are harness directives:
 *       "#wait <ms>", "#press <A|B|MENU>" (a physical press). Simulated clock.
 *   check_remote serve-lan <port> <flash> <key>  loopback TCP 38771-style server, real clock
 * Both serve modes run the same device model: the launcher state machine against a
 * modelled stock FPGA, the service session, and the one CHR1 handler.
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#ifdef _WIN32
#include <winsock2.h>
#include <fcntl.h>
#include <io.h>
#endif
#include "shared/launcher_diag.h"
#include "shared/launcher_logic.h"
#include "shared/log_ring.h"
#include "shared/remote.h"
#include "shared/service.h"
#include "shared/service_screen.h"
#include "shared/usb_tx.h"
#include "../../pocket/ui_font.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"remote check failed at line %d\n",__LINE__);exit(1);}} while(0)

static const uint8_t test_mac[6]={0x24,0x6f,0x28,0x10,0x20,0x30}; /* made up, not the unit's */
#define TEST_FLASH_ID 0x204016u

/* ---- the device model ------------------------------------------------------------ */
typedef struct {uint8_t *data;uint32_t size,fail_at,max_call;} fake_flash;
static int fake_read(void *ctx,uint32_t offset,void *out,uint32_t size) {
    fake_flash *f=ctx;
    CHECK(size&&offset<f->size&&size<=f->size-offset); /* never outside the flash */
    if(size>f->max_call)f->max_call=size;
    if(f->fail_at&&offset<=f->fail_at&&f->fail_at<offset+size)return 1;
    memcpy(out,f->data+offset,size);return 0;
}
static uint32_t rng=0x2468aceu;
static uint32_t fake_random(void *ctx){(void)ctx;rng=rng*1664525u+1013904223u;return rng^(rng>>13);}
static uint16_t fake_fpga_version;static uint8_t fake_fpga_seen; /* 1.3.5: what the model's launcher heard from its FPGA */
static void fake_facts(void *ctx,pj_rmt_facts *f){(void)ctx;f->heap_free=180000;f->heap_min=170000;f->loop_ms=10;
    f->work_ms=2;f->wifi="off";f->reset="poweron";f->fpga_version=fake_fpga_version;f->fpga_seen=fake_fpga_seen;}
typedef struct {int cart,alive,open,pressed,pokes,frames,dirty;uint16_t held;} fpga_model;
static void model_poke(fpga_model *f,uint16_t value) {
    int pressed=(value&PJ_POKE_MENU)!=0;
    if(pressed&&!f->pressed){f->pokes++;f->dirty=f->held!=0;}
    if(f->held)f->dirty=1;
    if(f->pressed&&!pressed&&f->cart&&f->frames>=2&&!f->dirty)f->open=!f->open;
    f->pressed=pressed;
}
typedef struct {
    pj_launcher l;fpga_model f;pj_svc s;pj_remote r;pj_log log;uint16_t fb[PJ_OSD_W*PJ_OSD_H];
    int restarted,held_passes;unsigned service_frames,frames;
} device;
static void device_init(device *d,fake_flash *flash,int cart,int alive,const uint8_t *key) {
    memset(d,0,sizeof(*d));d->f.cart=cart;d->f.alive=alive;
    pj_launch_init(&d->l,0);pj_svc_init(&d->s,test_mac,TEST_FLASH_ID,3,fake_read,flash);
    pj_rmt_init(&d->r,"1.1.2",key,&d->l,&d->s,d->fb,&d->log,fake_random,fake_facts,NULL);
}
static void buttons_message(device *d,uint32_t now) {
    uint16_t v=(uint16_t)(d->f.held|(d->f.open?0:PJ_MENU_CLOSED));
    pj_rmt_physical(&d->r,v,now);pj_launch_buttons(&d->l,v,now);
}
/* One main-loop pass in the same order as launcher_main.c. */
static void pass(device *d,uint32_t now) {
    if(d->restarted)return;
    pj_svc_poll(&d->s,now);pj_rmt_poll(&d->r,now);
    pj_launch_action a=pj_launch_step(&d->l,now);
    if(pj_rmt_link(&d->r,&a,now))d->held_passes++;
    if(a.prime_frames){d->f.frames+=2;pj_launch_draw_menu(d->fb,&d->l,now,pj_font_alpha,pj_font_advance);}
    if(a.poke)model_poke(&d->f,a.poke_value);
    if(a.restart_to_stock) {
        /* The hand-over rules hold with sessions too. */
        if(!(!d->f.pressed&&(!d->f.open||!d->f.alive)&&!pj_svc_in_session(&d->s)&&!d->r.pairing))fprintf(stderr,"pressed=%d open=%d alive=%d svc=%d pairing=%d done=%u close=%u t=%lu\n",d->f.pressed,d->f.open,d->f.alive,d->s.state,d->r.pairing,d->l.done_reason,d->l.close_reason,(unsigned long)now);
        CHECK(!d->f.pressed&&(!d->f.open||!d->f.alive)&&!pj_svc_in_session(&d->s)&&!d->r.pairing);
        pj_rmt_shutdown(&d->r);d->restarted=1;return;
    }
    if(a.request_buttons&&d->f.alive)buttons_message(d,now);
    if(pj_rmt_owns_screen(&d->r)) {
        d->l.redraw=0;
        if(d->r.pairing&&pj_svc_screen_of(&d->l)==PJ_SVC_SCREEN_SHOWN) {
            pj_pair_draw(d->fb,d->r.pair_code,(PJ_RMT_PAIR_MS-(now-d->r.pair_ms))/1000u,pj_font_alpha,pj_font_advance);
        } else if(pj_svc_wants_frame(&d->s)) {
            d->s.redraw=0;d->service_frames++;pj_svc_draw(d->fb,&d->s,pj_font_alpha,pj_font_advance);
            if(d->s.state==PJ_SVC_FROZEN)pj_svc_frame_sent(&d->s);
        }
    } else if(d->l.redraw) {
        d->l.redraw=0;d->frames++;
        if(d->l.phase==PJ_L_MEDIA){const char *lines[3]={"Wi-Fi: off","Remote Pilot: USB",""};
            pj_launch_draw_media(d->fb,lines,pj_font_alpha,pj_font_advance);}
        else if(d->l.phase==PJ_L_OTHER)pj_launch_draw_other(d->fb,&d->l,pj_font_alpha,pj_font_advance);
        else pj_launch_draw_menu(d->fb,&d->l,now,pj_font_alpha,pj_font_advance);
    }
}

/* ---- a PC client in C, for the unit checks ------------------------------------------ */
typedef struct {uint8_t key[32],session[32];char dev[33],pc[33];uint32_t seq;} pc_client;
static uint8_t wire[64*1024];static size_t wire_len;
static void take(device *d,pj_link *k) { /* everything the link owes, to `wire` */
    size_t n;while((n=pj_rmt_out(&d->r,k,wire+wire_len,sizeof(wire)-wire_len-1))>0)wire_len+=n;
    wire[wire_len]=0;
}
static void forget(void){wire_len=0;wire[0]=0;}
static int saw(const char *text){return strstr((const char *)wire,text)!=NULL;}
static void send_text(device *d,pj_link *k,uint32_t now,const char *fmt,...) {
    char line[300];va_list args;va_start(args,fmt);vsnprintf(line,sizeof(line),fmt,args);va_end(args);
    for(size_t i=0;line[i];i++)pj_rmt_rx(&d->r,k,(uint8_t)line[i],now);
}
static uint32_t run(device *d,uint32_t now,uint32_t until) {for(;now<until&&!d->restarted;now+=10)pass(d,now);return now;}
static void tag(const uint8_t key[32],const char *text,char out[65]){uint8_t m[32];pj_hmac(key,text,strlen(text),NULL,0,m);pj_hex(m,32,out);}
/* Handshake on link k; returns 1 if the device accepted. */
static int pc_connect(device *d,pj_link *k,pc_client *c,const uint8_t key[32],uint32_t now) {
    forget();
    if(k->kind==PJ_LINK_USB)send_text(d,k,now,"CHR1 OPEN\n");else pj_rmt_lan_open(&d->r,now);
    take(d,k);
    char hello[40];if(sscanf((const char *)wire,"CHR1 HELLO %32s",hello)!=1)return 0;
    memcpy(c->key,key,32);strcpy(c->dev,hello);
    for(int i=0;i<16;i++)snprintf(c->pc+i*2,3,"%02x",(unsigned)(fake_random(NULL)&255));
    char text[160],mac[65];snprintf(text,sizeof(text),"CHR1 pc-auth %s %s",c->dev,c->pc);tag(key,text,mac);
    forget();send_text(d,k,now,"CHR1 AUTH %s %s\n",c->pc,mac);take(d,k);
    if(strncmp((const char *)wire,"CHR1 OK ",8))return 0;
    snprintf(text,sizeof(text),"CHR1 device-auth %s %s",c->pc,c->dev);tag(key,text,mac);
    CHECK(!strncmp((const char *)wire+8,mac,64)); /* the device proved the key too */
    CHECK(saw(" " PJ_RMT_CAPS "\n"));
    snprintf(text,sizeof(text),"CHR1 session %s %s",c->dev,c->pc);pj_hmac(key,text,strlen(text),NULL,0,c->session);
    c->seq=1;return 1;
}
/* Sends a request, runs passes until it is answered, checks the response MAC. Returns
 * the code; the body is in *body (pointing into wire). */
static const char *request(device *d,pj_link *k,pc_client *c,uint32_t *now,const char *action,const char *arg,
                           const uint8_t **body,size_t *size) {
    static char code[16];char head[128],mac[65];
    snprintf(head,sizeof(head),"REQ %lu %s %s",(unsigned long)c->seq,action,arg);tag(c->session,head,mac);mac[32]=0;
    forget();send_text(d,k,*now,"%s %s\n",head,mac);
    for(int i=0;i<2000&&!wire_len;i++){take(d,k);if(!wire_len){*now+=10;pass(d,*now);}}
    take(d,k);
    unsigned long seq,len;char their[40];
    if(sscanf((const char *)wire,"RES %lu %15s %lu %32s",&seq,code,&len,their)!=4)return NULL;
    const char *nl=memchr(wire,'\n',wire_len);CHECK(nl&&seq==c->seq&&(size_t)(nl+1-(const char *)wire)+len==wire_len);
    char check[96];int n=snprintf(check,sizeof(check),"RES %lu %s %lu\n",seq,code,len);
    uint8_t m[32];pj_hmac(c->session,check,(size_t)n,nl+1,len,m);pj_hex(m,16,mac);CHECK(!strcmp(mac,their));
    c->seq++;*body=(const uint8_t *)nl+1;*size=len;return code;
}
static int answered(device *d,pj_link *k,pc_client *c,uint32_t *now,const char *action,const char *arg,const char *want) {
    const uint8_t *b;size_t n;const char *code=request(d,k,c,now,action,arg,&b,&n);
    return code&&!strcmp(code,"ok")&&(!want||(n==strlen(want)&&!memcmp(b,want,n)));
}

/* ---- checks ------------------------------------------------------------------------ */
static void vector_checks(void) {
    uint8_t md5[16];char hex[65];pj_md5 m;
    pj_md5_init(&m);pj_md5_final(&m,md5);pj_hex(md5,16,hex);CHECK(!strcmp(hex,"d41d8cd98f00b204e9800998ecf8427e"));
    pj_md5_init(&m);pj_md5_update(&m,"abc",3);pj_md5_final(&m,md5);pj_hex(md5,16,hex);CHECK(!strcmp(hex,"900150983cd24fb0d6963f7d28e17f72"));
    uint32_t v;CHECK(pj_svc_parse_hex8("400000",&v)&&v==0x400000u);
    CHECK(!pj_svc_parse_hex8("",&v)&&!pj_svc_parse_hex8("123456789",&v)&&!pj_svc_parse_hex8("4A",&v));
}
static void pairing_checks(fake_flash *flash) {
    static device d;pc_client c;uint32_t now=0;
    device_init(&d,flash,1,1,NULL);now=run(&d,now,1500);CHECK(d.l.phase==PJ_L_MENU);
    /* No key: nothing opens, LAN is refused, noise is ignored. */
    forget();send_text(&d,&d.r.usb,now,"CHR1 OPEN\n");take(&d,&d.r.usb);CHECK(saw("CHR1 NOKEY"));
    forget();pj_rmt_lan_open(&d.r,now);take(&d,&d.r.lan);CHECK(saw("CHR1 DENY")&&d.r.lan.want_close);pj_rmt_lan_closed(&d.r);
    forget();send_text(&d,&d.r.usb,now,"hello\nREQ 1 ping - 0000\n\xc0\x01\x08\n");take(&d,&d.r.usb);CHECK(!wire_len);
    /* Pairing asked: code shown, the countdown paused, menu buttons do nothing. */
    forget();send_text(&d,&d.r.usb,now,"CHR1 PAIR %s\n","00112233445566778899aabbccddeeff");take(&d,&d.r.usb);
    char dev[40],code[10];CHECK(sscanf((const char *)wire,"CHR1 PAIRING %32s %6s",dev,code)==2&&strlen(code)==6);
    {   /* The PC computes the same code from the two nonces. */
        char text[120];uint8_t digest[32];pj_sha256 h;
        snprintf(text,sizeof(text),"CHR1 pair-code %s 00112233445566778899aabbccddeeff",dev);
        pj_sha256_init(&h);pj_sha256_update(&h,text,strlen(text));pj_sha256_final(&h,digest);
        char want[8];snprintf(want,sizeof(want),"%06lu",(unsigned long)(((uint32_t)digest[0]<<24|(uint32_t)digest[1]<<16|
            (uint32_t)digest[2]<<8|digest[3])%1000000u));CHECK(!strcmp(want,code));
    }
    now=run(&d,now,now+30000);CHECK(!d.restarted&&d.l.phase==PJ_L_MENU&&d.l.service&&!d.r.lease_on); /* holds nothing */
    d.f.held=PJ_BTN_DOWN;buttons_message(&d,now);d.f.held=0;buttons_message(&d,now+5);CHECK(d.l.selected==PJ_ITEM_GAMES);
    forget();now=run(&d,now,now+20);take(&d,&d.r.usb);CHECK(!wire_len); /* DOWN is not an answer */
    /* B: not allowed. Asking again, then A: the key is installed and sent once. */
    d.f.held=PJ_BTN_B;buttons_message(&d,now);d.f.held=0;buttons_message(&d,now+5);
    forget();now=run(&d,now,now+20);take(&d,&d.r.usb);CHECK(saw("CHR1 PAIR-DENIED")&&!d.r.key_kind);
    send_text(&d,&d.r.usb,now,"CHR1 PAIR %s\n","ffeeddccbbaa99887766554433221100");forget();take(&d,&d.r.usb);
    d.f.held=PJ_BTN_A;buttons_message(&d,now);d.f.held=0;buttons_message(&d,now+5);
    forget();now=run(&d,now,now+20);take(&d,&d.r.usb);
    char keyhex[70];CHECK(sscanf((const char *)wire,"PJ-PAIR %64s",keyhex)==1&&strlen(keyhex)==64&&d.r.key_kind==2);
    CHECK(d.l.phase==PJ_L_MENU&&d.l.selected==PJ_ITEM_GAMES); /* A answered pairing, it did not start Games */
    uint8_t key[32];for(int i=0;i<32;i++){unsigned b;sscanf(keyhex+i*2,"%2x",&b);key[i]=(uint8_t)b;}
    forget();send_text(&d,&d.r.usb,now,"CHR1 PAIR %s\n","00112233445566778899aabbccddeeff");take(&d,&d.r.usb);
    CHECK(saw("PAIR-REFUSED key-present"));
    /* The paired key works on USB and LAN; one session at a time. */
    CHECK(pc_connect(&d,&d.r.usb,&c,key,now)&&d.r.owner==&d.r.usb);
    forget();pj_rmt_lan_open(&d.r,now);take(&d,&d.r.lan);CHECK(!wire_len&&d.r.lan.want_close);pj_rmt_lan_closed(&d.r);
    CHECK(answered(&d,&d.r.usb,&c,&now,"bye","-","bye")&&!d.r.owner);
    pc_client lan;CHECK(pc_connect(&d,&d.r.lan,&lan,key,now)&&d.r.owner==&d.r.lan);
    forget();send_text(&d,&d.r.usb,now,"CHR1 OPEN\n");take(&d,&d.r.usb);CHECK(saw("CHR1 BUSY"));
    CHECK(answered(&d,&d.r.lan,&lan,&now,"ping","-","pong"));
    /* Wrong key: DENY, and one failure per second. */
    pj_rmt_lan_closed(&d.r);
    uint8_t wrong[32];memset(wrong,7,32);CHECK(!pc_connect(&d,&d.r.usb,&c,wrong,now)&&saw("CHR1 DENY"));
    forget();send_text(&d,&d.r.usb,now+100,"CHR1 OPEN\n");take(&d,&d.r.usb);CHECK(saw("CHR1 WAIT"));
    CHECK(pc_connect(&d,&d.r.usb,&c,key,now+1200));
    /* Forged MAC and replay close the link without an answer. */
    forget();send_text(&d,&d.r.usb,now,"REQ 1 ping - %032d\n",0);take(&d,&d.r.usb);CHECK(!wire_len&&d.r.usb.state==PJ_LS_IDLE);
    CHECK(pc_connect(&d,&d.r.usb,&c,key,now+1300));c.seq=1;
    {
        char head[64],mac[65];snprintf(head,sizeof(head),"REQ 1 ping -");tag(c.session,head,mac);mac[32]=0;
        send_text(&d,&d.r.usb,now,"%s %s\n",head,mac);forget();take(&d,&d.r.usb);CHECK(saw("RES 1 ok 4"));
        forget();send_text(&d,&d.r.usb,now,"%s %s\n",head,mac);take(&d,&d.r.usb);CHECK(!wire_len&&!d.r.owner);
    }
    /* Pairing times out after 60 s with no answer. */
    device_init(&d,flash,1,1,NULL);now=run(&d,0,1500);
    forget();send_text(&d,&d.r.usb,now,"CHR1 PAIR %s\n","00112233445566778899aabbccddeeff");take(&d,&d.r.usb);
    forget();now=run(&d,now,now+PJ_RMT_PAIR_MS+100);take(&d,&d.r.usb);CHECK(saw("CHR1 PAIR-DENIED")&&!d.r.key_kind);
}
static void session_checks(fake_flash *flash) {
    static device d;pc_client c;uint32_t now;const uint8_t *b;size_t n;const char *code;
    uint8_t key[32];for(int i=0;i<32;i++)key[i]=(uint8_t)(i*7+1);
    /* Remote Pilot drives the real menu, which waits for a choice; virtual presses navigate. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    now=run(&d,now,now+12000);CHECK(!d.restarted&&d.l.phase==PJ_L_MENU); /* nothing chosen by itself */
    code=request(&d,&d.r.usb,&c,&now,"status","-",&b,&n);
    CHECK(code&&!strcmp(code,"ok")&&strstr((const char *)b,"\"phase\":\"menu\"")&&strstr((const char *)b,"\"transport\":\"usb\"")&&
          strstr((const char *)b,"\"countdownS\":null")&&strstr((const char *)b,"\"key\":\"provisioned\""));
    /* 1.3.5: status names the FPGA image the launcher heard (none here), so a PC can read which chip image runs
     * without restarting anything; with a Version message heard, 18.41 with the debug bit. */
    CHECK(strstr((const char *)b,"\"fpga\":\"none\",\"fpgaDebug\":false}"));
    fake_fpga_version=(uint16_t)(18u|(41u<<6)|(1u<<12));fake_fpga_seen=1;
    code=request(&d,&d.r.usb,&c,&now,"status","-",&b,&n);
    CHECK(code&&!strcmp(code,"ok")&&strstr((const char *)b,"\"fpga\":\"18.41\",\"fpgaDebug\":true}"));
    CHECK(answered(&d,&d.r.usb,&c,&now,"console","fpga","fpga version=18.41 debug=1"));
    fake_fpga_version=(uint16_t)(18u|(14u<<6));
    CHECK(answered(&d,&d.r.usb,&c,&now,"console","fpga","fpga version=18.14 debug=0"));
    fake_fpga_seen=0;
    CHECK(answered(&d,&d.r.usb,&c,&now,"console","fpga","fpga version=none debug=0"));
    CHECK(answered(&d,&d.r.usb,&c,&now,"input","DOWN:100","queued")&&d.l.selected==PJ_ITEM_MEDIA);
    CHECK(answered(&d,&d.r.usb,&c,&now,"input","A:100","queued")&&d.l.phase==PJ_L_MEDIA);
    now=run(&d,now,now+30);
    code=request(&d,&d.r.usb,&c,&now,"screen","-",&b,&n);CHECK(code&&!strcmp(code,"ok")&&n==PJ_CHR1_SCREEN_BYTES);
    CHECK(b[0]==(uint8_t)d.fb[0]&&b[1]==(uint8_t)(d.fb[0]>>8)); /* RGB565 little-endian */
    code=request(&d,&d.r.usb,&c,&now,"video","key",&b,&n);
    CHECK(code&&!memcmp(b,"CHV1",4)&&b[12]==1&&n<PJ_CHR1_SCREEN_BYTES/4); /* a launcher screen codes small */
    {   /* Decode the RLE keyframe and compare with the framebuffer. */
        size_t at=20;unsigned px=0;
        while(at+3<=n&&px<PJ_OSD_W*PJ_OSD_H){unsigned run_len=b[at];uint16_t col=(uint16_t)(b[at+1]|b[at+2]<<8);
            for(unsigned i=0;i<run_len;i++){CHECK(d.fb[px]==col);px++;}at+=3;}
        CHECK(px==PJ_OSD_W*PJ_OSD_H&&at==n);
    }
    {   /* While a video plays (the shell sets video_slow) live frames come at most every PJ_RMT_VIDEO_SLOW_MS: an
         * earlier request waits for the slot (pending, link kept alive) and is then answered with a frame; `screen`
         * (an explicit screenshot) is never held; without video_slow frames are answered at once again. */
        uint32_t before;
        d.r.video_slow=1;before=now;
        code=request(&d,&d.r.usb,&c,&now,"video","key",&b,&n);
        CHECK(code&&!strcmp(code,"ok")&&!memcmp(b,"CHV1",4)&&now-before>=PJ_RMT_VIDEO_SLOW_MS-100&&now-before<=PJ_RMT_VIDEO_SLOW_MS+100&&d.r.video_waited==1);
        before=now;code=request(&d,&d.r.usb,&c,&now,"screen","-",&b,&n);CHECK(code&&!strcmp(code,"ok")&&n==PJ_CHR1_SCREEN_BYTES&&now-before<100);
        before=now;code=request(&d,&d.r.usb,&c,&now,"video","key",&b,&n);
        CHECK(code&&!strcmp(code,"ok")&&!memcmp(b,"CHV1",4)&&now-before>=PJ_RMT_VIDEO_SLOW_MS-200&&d.r.video_waited==2);
        d.r.video_slow=0;before=now;
        code=request(&d,&d.r.usb,&c,&now,"video","key",&b,&n);CHECK(code&&!strcmp(code,"ok")&&!memcmp(b,"CHV1",4)&&now-before<100&&d.r.video_waited==2);
    }
    CHECK(answered(&d,&d.r.usb,&c,&now,"hold","UP","held")&&d.r.hold_bits==PJ_BTN_UP);
    now=run(&d,now,now+700);CHECK(!d.r.hold_bits); /* expired 600 ms after the last refresh */
    CHECK(answered(&d,&d.r.usb,&c,&now,"hold","UP","held"));
    d.f.held=PJ_BTN_START;buttons_message(&d,now);CHECK(!d.r.hold_bits);d.f.held=0;buttons_message(&d,now+5); /* physical wins */
    CHECK(d.l.phase==PJ_L_MENU); /* START on the Jellyfin screen returns to the menu, as on the console */
    now=run(&d,now,now+3000);CHECK(!d.restarted);
    /* MENU: a hand-over, then the link closes. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    code=request(&d,&d.r.usb,&c,&now,"log","4096",&b,&n);CHECK(code&&strstr((const char *)b,"session open (usb)"));
    CHECK(answered(&d,&d.r.usb,&c,&now,"console","version","launcher 1.1.2"));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"console","cat",&b,&n),"refused"));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"input","MENU+A:100",&b,&n),"refused"));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"hold","MENU",&b,&n),"refused"));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"flash-write","0",&b,&n),"refused"));
    CHECK(answered(&d,&d.r.usb,&c,&now,"input","MENU:100","handover")&&!d.r.owner);
    now=run(&d,now,now+3000);CHECK(d.restarted&&d.l.close_reason==PJ_CLOSE_REMOTE_MENU&&!d.f.open);
    /* The lease: with no cartridge a session holds the restart for at most 10 minutes per
     * power-on, even while the PC keeps talking; then stock. */
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    for(uint32_t end=now+PJ_RMT_LEASE_MS-2000;now<end;){now=run(&d,now,now+10000);CHECK(answered(&d,&d.r.usb,&c,&now,"ping","-","pong"));}
    CHECK(!d.restarted&&d.l.phase==PJ_L_DONE&&d.r.lease_on);
    for(int i=0;i<4&&!d.restarted;i++){now=run(&d,now,now+2500);if(!d.restarted&&d.r.owner)answered(&d,&d.r.usb,&c,&now,"ping","-","pong");}
    CHECK(d.restarted&&d.r.lease_spent&&d.l.done_reason==PJ_DONE_OPEN_NO_RESPONSE);
    /* No cartridge: held for the session; 15 s without a request closes it; then stock. */
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    now=run(&d,now,8000);CHECK(!d.restarted&&d.l.phase==PJ_L_DONE&&d.l.done_reason==PJ_DONE_OPEN_NO_RESPONSE);
    code=request(&d,&d.r.usb,&c,&now,"status","-",&b,&n);CHECK(strstr((const char *)b,"\"screenVisible\":false"));
    uint32_t quiet=now;now=run(&d,now,now+30000);CHECK(d.restarted&&now-quiet<=PJ_CHR1_IDLE_MS+20);
    /* Without a session nothing is held: no cartridge goes to stock in under 6 s; with one
     * the menu waits for the owner. */
    device_init(&d,flash,0,1,key);now=run(&d,0,20000);CHECK(d.restarted&&now<6000&&!d.held_passes);
    device_init(&d,flash,1,1,key);now=run(&d,0,600000);CHECK(!d.restarted&&d.l.phase==PJ_L_MENU&&!d.held_passes);
}
static void service_checks(fake_flash *flash) {
    static device d;pc_client c;uint32_t now;const uint8_t *b;size_t n;const char *code;
    uint8_t key[32];for(int i=0;i<32;i++)key[i]=(uint8_t)(255-i);
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","0:10",&b,&n),"refused")); /* not begun */
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","backup:800000","screen=shown")&&d.l.service);
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"input","A:100",&b,&n),"busy")); /* no hidden navigation */
    code=request(&d,&d.r.usb,&c,&now,"svc-id","0011223344556677",&b,&n);
    {
        uint8_t nonce[8]={0,0x11,0x22,0x33,0x44,0x55,0x66,0x77},digest[32];char hex[65],want[120];
        pj_svc_id_digest(nonce,test_mac,TEST_FLASH_ID,digest);pj_hex(digest,32,hex);
        snprintf(want,sizeof(want),"digest=%s chip-rev=3 flash-id=204016",hex);CHECK(n==strlen(want)&&!memcmp(b,want,n));
    }
    flash->max_call=0;code=request(&d,&d.r.usb,&c,&now,"svc-read","1234:4000",&b,&n);
    CHECK(!strcmp(code,"ok")&&n==0x4000&&!memcmp(b,flash->data+0x1234,0x4000));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","3fffff:2",&b,&n),"refused"));
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","0:4001",&b,&n),"refused"));
    /* Secrets are never served: the settings partition and both provisioning sectors,
     * any overlap at all, by read or by hash (a hash would allow guessing a password). */
    {
        static const char *const secret[][2]={{"svc-read","9000:10"},{"svc-read","8fff:2"},{"svc-read","effe:10"},
            {"svc-read","3fe000:2000"},{"svc-read","3fdfff:2"},{"svc-read","3ff000:1000"},{"svc-read","3ffff0:10"},
            {"svc-hash","0:400000"},{"svc-hash","0:9001"},{"svc-hash","e000:2000"},{"svc-hash","3fd000:1001"},
            {"svc-hash","3fe000:2000"}};
        for(unsigned i=0;i<sizeof(secret)/sizeof(secret[0]);i++) {
            CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,secret[i][0],secret[i][1],&b,&n),"refused"));
            CHECK(n>=9&&!memcmp(b,"protected",9));
        }
        CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","8000:1000",&b,&n),"ok")&&n==0x1000);  /* up to NVS */
        CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","f000:1000",&b,&n),"ok")&&n==0x1000);  /* right after */
        CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","3fa000:4000",&b,&n),"ok"));          /* up to A */
    }
    flash->max_call=0;uint32_t t0=now;code=request(&d,&d.r.usb,&c,&now,"svc-hash","f000:3ef000",&b,&n);
    {
        uint8_t md5[16],sha[32];char a[33],h[65],want[200];pj_md5 m;pj_sha256 s;
        pj_md5_init(&m);pj_md5_update(&m,flash->data+0xf000,0x3ef000);pj_md5_final(&m,md5);
        pj_sha256_init(&s);pj_sha256_update(&s,flash->data+0xf000,0x3ef000);pj_sha256_final(&s,sha);
        pj_hex(md5,16,a);pj_hex(sha,32,h);snprintf(want,sizeof(want),"off=f000 len=3ef000 md5=%s sha256=%s",a,h);
        CHECK(!strcmp(code,"ok")&&n==strlen(want)&&!memcmp(b,want,n)&&flash->max_call<=PJ_SVC_HASH_STEP);
        CHECK(now-t0>=2500); /* 16 KiB per pass: bounded work between watchdog feeds */
    }
    CHECK(d.s.pct>=45&&d.s.pct<100);
    unsigned before=d.s.pct;
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-pct","40","ok")); now=run(&d,now,now+20);CHECK(d.s.pct>=before); /* never down */
    /* FREEZE: answered only after the still frame went out; nothing redraws after it. */
    unsigned frames=d.service_frames;
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-freeze","write","screen=shown wait-ms=15000")&&d.service_frames==frames+1);
    now=run(&d,now,now+5000);CHECK(d.service_frames==frames+1&&d.f.open);
    CHECK(!strcmp(request(&d,&d.r.usb,&c,&now,"svc-read","0:10",&b,&n),"refused"));
    now=run(&d,now,now+11000);CHECK(d.restarted&&d.l.close_reason==PJ_CLOSE_SERVICE_END&&d.s.end_reason==PJ_SVC_END_FREEZE_TIMEOUT);
    /* svc-end ok: the done screen, then stock. The link closing first does not cut it short. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","status:0",NULL));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-end","ok","next=stock")&&answered(&d,&d.r.usb,&c,&now,"bye","-","bye"));
    now=run(&d,now,now+PJ_SVC_DONE_MS-50);CHECK(!d.restarted&&d.s.state==PJ_SVC_FINISHING);
    now=run(&d,now,now+5000);CHECK(d.restarted&&d.s.end_reason==PJ_SVC_END_OK);
    /* The PC goes away mid-job: 15 s later the link closes, the job ends, stock follows. */
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","backup:400000","screen=opening"));
    now=run(&d,now,now+40000);CHECK(d.restarted&&d.s.end_reason==PJ_SVC_END_LINK_CLOSED);
    /* The owner presses the real menu button: the job ends and stock starts. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","backup:0",NULL));
    d.f.open=0;now=run(&d,now,now+500);CHECK(d.restarted&&d.s.end_reason==PJ_SVC_END_CONSOLE_CLOSED);
    /* The 20 minute cap, with the PC talking all along. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","status:0",NULL));
    while(!d.restarted&&now<PJ_SVC_MAX_MS+60000){now=run(&d,now,now+5000);if(!d.restarted&&d.r.owner)request(&d,&d.r.usb,&c,&now,"ping","-",&b,&n);}
    CHECK(d.restarted&&d.s.end_reason==PJ_SVC_END_TIME_CAP&&now<PJ_SVC_MAX_MS+12000);
}
/* The review's findings, each with its own check. */
static void review_checks(fake_flash *flash) {
    static device d;pc_client c,lan;uint32_t now;const uint8_t *b;size_t n;
    uint8_t key[32];for(int i=0;i<32;i++)key[i]=(uint8_t)(i*3+5);
    /* svc-* is USB only: over the LAN every service action is refused, nothing is shown. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.lan,&lan,key,now));
    static const char *const svc[][2]={{"svc-begin","backup:0"},{"svc-read","0:10"},{"svc-hash","0:10"},
        {"svc-id","0011223344556677"},{"svc-pct","5"},{"svc-freeze","write"},{"svc-end","ok"}};
    for(unsigned i=0;i<sizeof(svc)/sizeof(svc[0]);i++) {
        CHECK(!strcmp(request(&d,&d.r.lan,&lan,&now,svc[i][0],svc[i][1],&b,&n),"refused")&&strstr((const char *)b,"USB only"));
    }
    CHECK(d.s.state==PJ_SVC_OFF&&!d.l.service);
    /* perf reports the stack high-water mark. */
    CHECK(answered(&d,&d.r.lan,&lan,&now,"perf","-",NULL));
    {const char *code=request(&d,&d.r.lan,&lan,&now,"perf","-",&b,&n);CHECK(code&&strstr((const char *)b,"\"stackFreeB\":"));}
    /* A closing USB link drops a body it still owed: the shell never waits for 46 KB. */
    pj_rmt_lan_closed(&d.r);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    {
        char head[64],mac[65];snprintf(head,sizeof(head),"REQ %lu screen -",(unsigned long)c.seq);tag(c.session,head,mac);mac[32]=0;
        send_text(&d,&d.r.usb,now,"%s %s\n",head,mac);
        CHECK(d.r.usb.body_len==PJ_CHR1_SCREEN_BYTES&&pj_rmt_busy_output(&d.r.usb));
        uint8_t first[100];CHECK(pj_rmt_out(&d.r,&d.r.usb,first,sizeof(first))==sizeof(first)); /* partly sent */
        send_text(&d,&d.r.usb,now,"REQ 99 ping - %032d\n",0);              /* broken framing: the link closes */
        CHECK(!pj_rmt_busy_output(&d.r.usb)&&d.r.usb.state==PJ_LS_IDLE&&!pj_rmt_usb_quiet(&d.r)&&!d.r.owner);
    }
    /* A session cannot start while the other link still owes output (one shared buffer). */
    {
        uint8_t wrong[32];memset(wrong,1,32);
        forget();pj_rmt_lan_open(&d.r,now+2000);take(&d,&d.r.lan);  /* HELLO */
        char dev[40];CHECK(sscanf((const char *)wire,"CHR1 HELLO %32s",dev)==1);
        char text[160],m[65];snprintf(text,sizeof(text),"CHR1 pc-auth %s %032d",dev,0);tag(wrong,text,m);
        send_text(&d,&d.r.lan,now+2000,"CHR1 AUTH %032d %s\n",0,m);  /* DENY owed, not yet sent */
        CHECK(pj_rmt_busy_output(&d.r.lan));
        forget();send_text(&d,&d.r.usb,now+4000,"CHR1 OPEN\n");take(&d,&d.r.usb);CHECK(saw("CHR1 BUSY"));
        forget();take(&d,&d.r.lan);CHECK(saw("CHR1 DENY")&&d.r.lan.want_close);pj_rmt_lan_closed(&d.r);
        CHECK(pc_connect(&d,&d.r.usb,&c,key,now+4000));
        forget();pj_rmt_lan_open(&d.r,now+4000);take(&d,&d.r.lan);CHECK(!wire_len&&d.r.lan.want_close); /* USB owns it */
        pj_rmt_lan_closed(&d.r);
    }
    /* 1.3.3 (1.3.2 review): an answer that keeps moving, however slowly, keeps its session; a LAN peer that stops
     * taking bytes (half-open: the PC went to sleep) loses it 15 s after the last byte moved, and the owed answer is
     * dropped so the shell closes the socket at once and USB can open. 1.3.2 kept it while output was merely pending. */
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);CHECK(pc_connect(&d,&d.r.lan,&lan,key,now));
    {
        char head[64],mac[65];snprintf(head,sizeof(head),"REQ %lu screen -",(unsigned long)lan.seq);tag(lan.session,head,mac);mac[32]=0;
        send_text(&d,&d.r.lan,now,"%s %s\n",head,mac);lan.seq++;
        uint8_t some[200];uint32_t moved=now;
        for(uint32_t end=now+40000;now<end;) { /* 200 bytes every 2 s: 40 s with no request, never idle */
            CHECK(pj_rmt_out(&d.r,&d.r.lan,some,sizeof(some))==sizeof(some));moved=now;
            now=run(&d,now,now+2000);CHECK(d.r.owner==&d.r.lan&&pj_rmt_busy_output(&d.r.lan));
        }
        /* then the peer stops taking bytes */
        while(d.r.owner&&now-moved<30000)now=run(&d,now,now+10);
        CHECK(!d.r.owner&&now-moved+20>=PJ_CHR1_IDLE_MS&&now-moved<=PJ_CHR1_IDLE_MS+30);
        CHECK(!pj_rmt_busy_output(&d.r.lan)&&d.r.lan.want_close&&d.r.lan.state==PJ_LS_IDLE);
        static char tail[PJ_LOG_BYTES+1];size_t len=pj_log_tail(&d.log,16384,tail);tail[len]=0;
        CHECK(strstr(tail,"answer stalled for 15 s")!=NULL);
        pj_rmt_lan_closed(&d.r);
        CHECK(pc_connect(&d,&d.r.usb,&c,key,now));  /* USB is not told BUSY any more */
        CHECK(answered(&d,&d.r.usb,&c,&now,"ping","-","pong"));
    }
    /* Pairing: refused unless the menu is on screen; it never holds the restart. */
    device_init(&d,flash,0,1,NULL);now=run(&d,0,50);
    forget();send_text(&d,&d.r.usb,now,"CHR1 PAIR %s\n","00112233445566778899aabbccddeeff");take(&d,&d.r.usb);
    CHECK(saw("PAIR-REFUSED no-screen")&&!d.r.pairing);
    now=run(&d,now,20000);CHECK(d.restarted&&now<6000&&!d.held_passes);
    /* Buttons held while opening (open-buttons-held) are an owner action: not held. */
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    while(d.f.pokes<1)now=run(&d,now,now+10);
    d.f.held=PJ_BTN_A;now=run(&d,now,now+8000);
    CHECK(d.restarted&&d.l.done_reason==PJ_DONE_OPEN_BUTTONS_HELD&&!d.held_passes);
    /* Held after an opening failure: buttons are still read, and any press ends the hold. */
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    CHECK(answered(&d,&d.r.usb,&c,&now,"svc-begin","backup:0",NULL));
    now=run(&d,now,8000);CHECK(!d.restarted&&d.l.phase==PJ_L_DONE&&d.held_passes>0);
    CHECK(answered(&d,&d.r.usb,&c,&now,"ping","-","pong"));
    d.f.held=PJ_BTN_B;uint32_t pressed_at=now;now=run(&d,now,now+1000);
    CHECK(d.restarted&&now-pressed_at<=250&&d.s.end_reason==PJ_SVC_END_CONSOLE_CLOSED);
    device_init(&d,flash,0,1,key);now=run(&d,0,50);CHECK(pc_connect(&d,&d.r.usb,&c,key,now));
    now=run(&d,now,8000);CHECK(!d.restarted);
    d.f.held=PJ_BTN_START;pressed_at=now;now=run(&d,now,now+1000);CHECK(d.restarted&&now-pressed_at<=250);
}
static void fuzz_checks(fake_flash *flash) {
    static device d;uint8_t key[32];memset(key,9,32);uint32_t now=0,x=99;
    device_init(&d,flash,1,1,key);now=run(&d,0,1500);
    for(unsigned i=0;i<300000;i++) {
        x=x*1103515245u+12345u;uint8_t byte=(uint8_t)(x>>16);
        if((x>>8)%5==0)byte='\n';
        pj_rmt_rx(&d.r,(x>>4)&1?&d.r.usb:&d.r.lan,byte,now);
        if(i%64==0){forget();take(&d,&d.r.usb);take(&d,&d.r.lan);if(d.r.lan.want_close)pj_rmt_lan_closed(&d.r);}
        CHECK(!d.r.owner); /* random bytes never authenticate */
    }
    CHECK(!d.restarted||1);
}
/* ---- previews ---------------------------------------------------------------------- */
static void write_ppm(const char *dir,const char *name,const uint16_t *fb) {
    char path[512];snprintf(path,sizeof(path),"%s/%s.ppm",dir,name);
    FILE *f=fopen(path,"wb");CHECK(f);fprintf(f,"P6\n160 144\n255\n");
    for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++) {
        unsigned c=fb[i];CHECK(c!=PJ_OSD_KEY);
        fputc((int)(((c>>11)&31)*255/31),f);fputc((int)(((c>>5)&63)*255/63),f);fputc((int)((c&31)*255/31),f);
    }
    CHECK(!ferror(f));fclose(f);
}
static void screen_checks(const char *dir,fake_flash *flash) {
    static uint16_t fb[PJ_OSD_W*PJ_OSD_H];
    for(unsigned i=0;i<PJ_SVC_OP_COUNT;i++)CHECK(pj_osd_measure(pj_svc_titles[i],pj_font_advance)<=152);
    const char *lines[]={"Keep USB connected.","Don't switch off.","Updating the console","This screen stays still",
        "until the PC says done.","See the PC for details.","Pair with this PC?","Same code on the PC?","A: allow    B: no",
        "Only pair your own PC."};
    for(unsigned i=0;i<sizeof(lines)/sizeof(lines[0]);i++)CHECK(pj_osd_measure(lines[i],pj_font_advance)<=150);
    pj_svc s;pj_svc_init(&s,test_mac,TEST_FLASH_ID,3,fake_read,flash);
    s.state=PJ_SVC_ACTIVE;s.op=PJ_SVC_OP_BACKUP;s.total=100;s.pct=37;s.spin=3;
    pj_svc_draw(fb,&s,pj_font_alpha,pj_font_advance);write_ppm(dir,"ui-service-working",fb);
    s.state=PJ_SVC_FROZEN;s.kind=PJ_SVC_KIND_WRITE;pj_svc_draw(fb,&s,pj_font_alpha,pj_font_advance);write_ppm(dir,"ui-service-still",fb);
    s.state=PJ_SVC_FINISHING;s.end_reason=PJ_SVC_END_OK;pj_svc_draw(fb,&s,pj_font_alpha,pj_font_advance);write_ppm(dir,"ui-service-done",fb);
    s.end_reason=PJ_SVC_END_FAIL;pj_svc_draw(fb,&s,pj_font_alpha,pj_font_advance);write_ppm(dir,"ui-service-stopped",fb);
    pj_pair_draw(fb,"418302",42,pj_font_alpha,pj_font_advance);write_ppm(dir,"ui-remote-pairing",fb);
}

/* ---- serve: USB over stdin/stdout, LAN over loopback TCP --------------------------- */
static uint8_t *load_flash(const char *path) {
    FILE *f=fopen(path,"rb");CHECK(f);uint8_t *d=malloc(PJ_SVC_FLASH_BYTES);CHECK(d);
    CHECK(fread(d,1,PJ_SVC_FLASH_BYTES,f)==PJ_SVC_FLASH_BYTES);fclose(f);return d;
}
static int parse_key(const char *text,uint8_t key[32]) {
    if(!strcmp(text,"none"))return 0;
    CHECK(strlen(text)==64);for(int i=0;i<32;i++){unsigned v;CHECK(sscanf(text+i*2,"%2x",&v)==1);key[i]=(uint8_t)v;}
    return 1;
}
static int serve_usb(const char *flash_path,const char *key_text,int cart) {
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
#endif
    static device d;fake_flash flash={0};flash.data=load_flash(flash_path);flash.size=PJ_SVC_FLASH_BYTES;
    uint8_t key[32];int has=parse_key(key_text,key);device_init(&d,&flash,cart,1,has?key:NULL);
    uint32_t now=run(&d,0,cart?1500:50);
    char directive[64];unsigned used=0;int in_directive=0,at_start=1,c;uint8_t out[4096];
    while((c=fgetc(stdin))!=EOF) {
        if(at_start&&c=='#')in_directive=1;
        at_start=c=='\n';
        if(in_directive) {
            if(c!='\n'){if(used<sizeof(directive)-1)directive[used++]=(char)c;continue;}
            directive[used]=0;used=0;in_directive=0;
            unsigned long ms;char button[8];
            if(sscanf(directive,"#wait %lu",&ms)==1)now=run(&d,now,now+(uint32_t)ms);
            else if(sscanf(directive,"#press %7s",button)==1) {
                d.f.held=!strcmp(button,"A")?PJ_BTN_A:!strcmp(button,"B")?PJ_BTN_B:0;
                if(!strcmp(button,"MENU"))d.f.open=0;
                buttons_message(&d,now);d.f.held=0;buttons_message(&d,now+5);
            }
        } else pj_rmt_rx(&d.r,&d.r.usb,(uint8_t)c,now);
        if(c!='\n')continue;
        for(unsigned i=0;i<200000;i++) {
            size_t n;while((n=pj_rmt_out(&d.r,&d.r.usb,out,sizeof(out)))>0)fwrite(out,1,n,stdout);
            if(d.restarted){fputs("PJ-LAUNCHER-TO-STOCK\n",stdout);fflush(stdout);free(flash.data);return 0;}
            if(!d.r.usb.pending&&!d.s.hashing&&!(d.r.pairing&&d.r.pair_answer))break;
            now+=10;pass(&d,now);
        }
        now+=10;pass(&d,now);
        size_t n;while((n=pj_rmt_out(&d.r,&d.r.usb,out,sizeof(out)))>0)fwrite(out,1,n,stdout);
        fflush(stdout);
    }
    free(flash.data);return 0;
}
#ifdef _WIN32
static int serve_lan(int port,const char *flash_path,const char *key_text) {
    static device d;fake_flash flash={0};flash.data=load_flash(flash_path);flash.size=PJ_SVC_FLASH_BYTES;
    uint8_t key[32];int has=parse_key(key_text,key);device_init(&d,&flash,1,1,has?key:NULL);
    WSADATA wsa;WSAStartup(MAKEWORD(2,2),&wsa);
    SOCKET listener=socket(AF_INET,SOCK_STREAM,0);
    struct sockaddr_in at={0};at.sin_family=AF_INET;at.sin_port=htons((u_short)port);at.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    if(bind(listener,(struct sockaddr *)&at,sizeof(at))||listen(listener,2))return 3;
    printf("READY\n");fflush(stdout);
    SOCKET client=INVALID_SOCKET;uint32_t start=(uint32_t)GetTickCount(),now=0,last_activity=0;uint8_t buf[8192];
    run(&d,0,1500);uint32_t base=1500;
    for(;;) {
        now=base+((uint32_t)GetTickCount()-start);
        fd_set set;FD_ZERO(&set);FD_SET(listener,&set);if(client!=INVALID_SOCKET)FD_SET(client,&set);
        struct timeval wait={0,10000};
        if(select(0,&set,NULL,NULL,&wait)>0) {
            if(FD_ISSET(listener,&set)) {
                SOCKET extra=accept(listener,NULL,NULL);
                if(client!=INVALID_SOCKET){closesocket(extra);}  /* one client at a time */
                else{client=extra;pj_rmt_lan_open(&d.r,now);last_activity=now;}
            }
            if(client!=INVALID_SOCKET&&FD_ISSET(client,&set)) {
                int got=recv(client,(char *)buf,sizeof(buf),0);
                if(got<=0){closesocket(client);client=INVALID_SOCKET;pj_rmt_lan_closed(&d.r);}
                else{for(int i=0;i<got;i++)pj_rmt_rx(&d.r,&d.r.lan,buf[i],now);last_activity=now;}
            }
        }
        pass(&d,now);
        size_t n;
        while(client!=INVALID_SOCKET&&(n=pj_rmt_out(&d.r,&d.r.lan,buf,sizeof(buf)))>0)send(client,(const char *)buf,(int)n,0);
        if(client!=INVALID_SOCKET&&d.r.lan.want_close){closesocket(client);client=INVALID_SOCKET;pj_rmt_lan_closed(&d.r);}
        if(d.restarted||now-last_activity>60000u)break;
    }
    if(client!=INVALID_SOCKET)closesocket(client);
    closesocket(listener);WSACleanup();free(flash.data);
    return 0;
}
/* ---- serve-usb-paced: the console's USB link at the console's real pace ----------------------------------
 * The loopback socket stands for the USB serial wire; the model times it like the console (real clock times
 * `speed`): UART0 at 115200 baud (11.52 bytes/ms each way) behind its 128-byte transmit FIFO, the 4 KiB receive
 * buffer read 1 KiB per pass, and a main loop whose pass is `pass_ms` (work, then the 10 ms vTaskDelay).
 *   mode 0 (1.3.1 and earlier): usb_pump fills the FIFO once per pass, at the end of the work (plus io_pump
 *          every 5 ms of the `decode_ms` share of the work, 1.2.9's decode-time pump);
 *   mode 1 (1.3.2): usb_pump fills the shared/usb_tx.h ring, and the 5 ms drain moves it into the FIFO.
 * Jellyfin's screen is noise (album art and video do not compress), so screen and video frames are raw 46 KB;
 * `playing` sets video_slow as the launcher does while a video plays.
 * `twdt_ms` (1.3.3): the second UART0 writer the model lacked. Every twdt_ms (0: never) the task watchdog's backtrace
 * is printed with esp_rom_printf on the other core, a character at a time whenever the FIFO holds under 126, whatever
 * the log level, and concurrently with the drain: the drain reads the FIFO's room, the other core writes, the drain
 * writes what it measured, and bytes past the FIFO's 128 are lost. The harness derives twdt_ms from the launcher source
 * (a stream task that never sleeps starves an idle task, and the watchdog fires every CONFIG_ESP_TASK_WDT_TIMEOUT_S).
 * Stdout: READY, then on exit the CHR1 log ring and counters, for the harness (scripts/model_chromatic_usb_pace.py). */
static uint8_t paced_fifo[1024];static uint32_t paced_fifo_len,paced_lost,paced_racy=~0u;
static uint32_t paced_room(void) {
    if(paced_racy!=~0u)return paced_racy; /* the room the drain read before the other core wrote */
    return paced_fifo_len>=128u?0u:128u-paced_fifo_len;
}
static void paced_write(const uint8_t *p,uint32_t n) {
    for(uint32_t i=0;i<n;i++){if(paced_fifo_len<128u)paced_fifo[paced_fifo_len++]=p[i];else paced_lost++;} /* a full FIFO drops it */
    if(paced_racy!=~0u)paced_racy=paced_racy>n?paced_racy-n:0;
}
/* What the other core prints when the task watchdog fires with the log level off (debug_helpers.c esp_backtrace_print). */
static const char paced_backtrace[]="\r\n\r\nBacktrace: 0x400d5a2c:0x3ffc8e40 0x40084d6e:0x3ffc8e60 0x400e1b33:0x3ffc8e80 "
    "0x4008a1f1:0x3ffc8ea0 0x40089f6d:0x3ffc8ec0 0x400d74b2:0x3ffc8ee0 0x4008b6b9:0x3ffc8f00\r\n\r\n";
static size_t paced_rom_at=sizeof(paced_backtrace)-1;static uint32_t paced_rom_bytes,paced_rom_prints;
static void paced_rom_fill(void) { /* esp_rom_printf -> uart_tx_one_char: waits while the FIFO holds 126 or more */
    while(paced_rom_at<sizeof(paced_backtrace)-1&&paced_fifo_len<126u){paced_fifo[paced_fifo_len++]=(uint8_t)paced_backtrace[paced_rom_at++];paced_rom_bytes++;}
}
static pj_utx paced_ring;
static void paced_pump(device *d,int ring) {
    uint8_t b[256];
    if(ring) { /* as launcher_main.c usb_pump (1.3.2) */
        size_t space,n;
        while((space=pj_utx_space(&paced_ring))>0&&(n=pj_rmt_out(&d->r,&d->r.usb,b,space<sizeof(b)?space:sizeof(b)))>0)
            pj_utx_put(&paced_ring,b,n);
    } else { /* as usb_pump up to 1.3.1 */
        uint32_t room;size_t n;
        while((room=paced_room())>0&&(n=pj_rmt_out(&d->r,&d->r.usb,b,room<sizeof(b)?room:sizeof(b)))>0)paced_write(b,(uint32_t)n);
    }
}
static int serve_usb_paced(int port,const char *flash_path,const char *key_text,unsigned pass_ms,int ring,int playing,
                           unsigned decode_ms,unsigned speed,unsigned twdt_ms) {
    static device d;fake_flash flash={0};flash.data=load_flash(flash_path);flash.size=PJ_SVC_FLASH_BYTES;
    uint8_t key[32];int has=parse_key(key_text,key);device_init(&d,&flash,1,1,has?key:NULL);
    d.r.version="1.3.3";
    WSADATA wsa;WSAStartup(MAKEWORD(2,2),&wsa);
    SOCKET listener=socket(AF_INET,SOCK_STREAM,0);
    struct sockaddr_in at={0};at.sin_family=AF_INET;at.sin_port=htons((u_short)port);at.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    if(bind(listener,(struct sockaddr *)&at,sizeof(at))||listen(listener,1))return 3;
    printf("READY\n");fflush(stdout);
    SOCKET wire_sock=accept(listener,NULL,NULL);closesocket(listener);
    if(wire_sock==INVALID_SOCKET)return 4;
    {u_long nb=1;ioctlsocket(wire_sock,FIONBIO,&nb);}
    uint32_t sim=run(&d,0,1500);
    static uint8_t rxq[65536];size_t rx_len=0;          /* bytes on the wire towards the console, not yet read */
    static uint8_t txq[65536];size_t tx_len=0;          /* bytes that left the FIFO, not yet handed to the socket */
    static uint16_t noise[PJ_OSD_W*PJ_OSD_H];uint32_t x=0x9e3779b9u;
    for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++){x=x*1664525u+1013904223u;noise[i]=(uint16_t)(x>>9);}
    paced_write((const uint8_t *)"PJ-LAUNCHER-READY 1.3.3\n",24);
    LARGE_INTEGER freq,t0,t;QueryPerformanceFrequency(&freq);QueryPerformanceCounter(&t0);
    double credit=0;uint32_t base=sim,next_pass=sim+1,work_end=0,passes=0,sent_total=0,diag_at=sim+5000,twdt_at=sim+twdt_ms;
    unsigned long rx_total=0;
    for(;;) {
        QueryPerformanceCounter(&t);
        uint32_t target=base+(uint32_t)((double)(t.QuadPart-t0.QuadPart)*1000.0*speed/(double)freq.QuadPart);
        while(sim<target) {
            sim++;
            if(paced_fifo_len){credit+=11.52;uint32_t k=(uint32_t)credit;if(k>paced_fifo_len)k=paced_fifo_len;credit-=k;
                if(tx_len+k<=sizeof(txq)){memcpy(txq+tx_len,paced_fifo,k);tx_len+=k;}
                memmove(paced_fifo,paced_fifo+k,paced_fifo_len-k);paced_fifo_len-=k;sent_total+=k;}
            else credit=0;
            if(twdt_ms&&(int32_t)(sim-twdt_at)>=0){twdt_at+=twdt_ms;paced_rom_at=0;paced_rom_prints++;} /* the watchdog fires */
            if(ring&&sim%PJ_UTX_DRAIN_MS==0) {
                paced_racy=paced_room(); /* the drain reads the room (esp_timer task, CPU0) ... */
                paced_rom_fill();        /* ... the other core's esp_rom_printf writes meanwhile ... */
                pj_utx_drain(&paced_ring,paced_room,paced_write); /* ... and the drain writes what it read */
                paced_racy=~0u;
            } else paced_rom_fill();
            if(sim==next_pass) { /* pass start: receive (1 KiB at most), the handler's poll and the launcher */
                size_t take_n=rx_len<1024?rx_len:1024;
                for(size_t i=0;i<take_n;i++)pj_rmt_rx(&d.r,&d.r.usb,rxq[i],sim);
                memmove(rxq,rxq+take_n,rx_len-take_n);rx_len-=take_n;
                d.r.video_slow=(uint8_t)playing;
                pass(&d,sim);memcpy(d.fb,noise,sizeof(noise));noise[sim%(PJ_OSD_W*PJ_OSD_H)]^=0x0841u;
                passes++;uint32_t work=pass_ms>10?pass_ms-10:1;work+=(passes*7u)%5u; /* a little jitter */
                work_end=sim+work;next_pass=work_end+10;
                /* Outside a session, a diagnostics line every 5 s as printf writes it (straight into the FIFO). */
                if((int32_t)(sim-diag_at)>=0&&!pj_rmt_usb_quiet(&d.r)&&(!ring||pj_utx_idle(&paced_ring))&&!paced_fifo_len) {
                    char line[64];int n=snprintf(line,sizeof(line),"PJ-DIAG t=%lu stats model=1\n",(unsigned long)sim);
                    paced_write((const uint8_t *)line,(uint32_t)n);diag_at=sim+5000;
                }
            }
            if(sim<work_end&&decode_ms&&(sim-(work_end-(pass_ms>10?pass_ms-10:1)))<decode_ms&&sim%5==0)paced_pump(&d,ring); /* io_pump */
            if(sim==work_end)paced_pump(&d,ring);
        }
        /* The socket: whatever left the FIFO goes to the PC; what the PC sent joins the wire to the console. */
        while(tx_len) {
            int n=send(wire_sock,(const char *)txq,(int)tx_len,0);
            if(n<=0)break;
            memmove(txq,txq+n,tx_len-(size_t)n);tx_len-=(size_t)n;
        }
        uint8_t buf[4096];int got=recv(wire_sock,(char *)buf,sizeof(buf),0);
        if(got==0||(got<0&&WSAGetLastError()!=WSAEWOULDBLOCK))break;
        if(got>0){if(rx_len+(size_t)got<=sizeof(rxq)){memcpy(rxq+rx_len,buf,(size_t)got);rx_len+=(size_t)got;}rx_total+=(unsigned long)got;}
        Sleep(1);
    }
    closesocket(wire_sock);WSACleanup();
    static char tail[PJ_LOG_BYTES+1];size_t n=pj_log_tail(&d.log,16384,tail);tail[n]=0;
    printf("PASSES %lu SENT %lu RECEIVED %lu SESSIONS %lu VIDEO_WAITED %lu SIM_MS %lu WATCHDOG_PRINTS %lu ROM_BYTES %lu LOST %lu\n",
           (unsigned long)passes,(unsigned long)sent_total,rx_total,(unsigned long)d.r.sessions,(unsigned long)d.r.video_waited,
           (unsigned long)sim,(unsigned long)paced_rom_prints,(unsigned long)paced_rom_bytes,(unsigned long)paced_lost);
    fputs("LOG\n",stdout);fputs(tail,stdout);fflush(stdout);
    free(flash.data);return 0;
}
#endif

int main(int argc,char **argv) {
    if(argc>=4&&!strcmp(argv[1],"serve-usb"))return serve_usb(argv[2],argv[3],!(argc>4&&!strcmp(argv[4],"nocart")));
#ifdef _WIN32
    if(argc==5&&!strcmp(argv[1],"serve-lan"))return serve_lan(atoi(argv[2]),argv[3],argv[4]);
    if((argc==10||argc==11)&&!strcmp(argv[1],"serve-usb-paced"))
        return serve_usb_paced(atoi(argv[2]),argv[3],argv[4],(unsigned)atoi(argv[5]),atoi(argv[6]),atoi(argv[7]),
                               (unsigned)atoi(argv[8]),(unsigned)atoi(argv[9]),argc==11?(unsigned)atoi(argv[10]):0u);
#endif
    CHECK(argc==2);
    fake_flash flash={0};flash.size=PJ_SVC_FLASH_BYTES;flash.data=malloc(PJ_SVC_FLASH_BYTES);CHECK(flash.data);
    uint32_t x=0x12345678u;for(uint32_t i=0;i<PJ_SVC_FLASH_BYTES;i++){x=x*1103515245u+12345u;flash.data[i]=(uint8_t)(x>>16);}
    vector_checks();pairing_checks(&flash);session_checks(&flash);service_checks(&flash);review_checks(&flash);
    fuzz_checks(&flash);
    screen_checks(argv[1],&flash);
    free(flash.data);
    puts("PASS: remote control: USB pairing (code both sides agree, A allows, B and 60 s refuse, virtual buttons cannot "
         "answer, key sent once), provisioned key refuses pairing, one session across USB and LAN, wrong key and one failure "
         "a second, forged and replayed requests close the link; the real menu driven by input/hold, 600 ms hold lease, "
         "physical buttons win, MENU hands over, 10 min countdown lease, no-cartridge hold ended by 15 s link silence; "
         "service actions (begin, id, read, hash in bounded steps, pct, freeze answered after its frame, end) with stock "
         "after every end; review fixes (NVS and provisioning never served, svc-* USB only, owed bodies dropped "
         "on close, no session while the other link owes output, pairing needs the menu and holds nothing, "
         "open-buttons-held not held, a press ends a hold, perf stack; 1.3.3: a moving answer keeps its session, one "
         "stalled 15 s loses it and frees USB); 300000 fuzzed bytes never authenticate. "
         "Modelled, no hardware.");
    return 0;
}
