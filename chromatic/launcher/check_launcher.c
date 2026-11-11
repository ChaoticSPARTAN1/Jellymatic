/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Host test of the boot policy, FPGA framing and menu state machine. No hardware.
 */
#include <stdio.h>
#include <stdlib.h>
#include "shared/boot_decision.h"
#include "shared/launcher_diag.h"
#include "shared/launcher_logic.h"
#include "shared/log_ring.h"
#include "shared/wifi_order.h"
#include "shared/wifi_scan.h" /* 1.3.4: pj_join_tally, the join's retries and words */
#include "shared/pocket_link_screen.h" /* 1.3.0 */
#include "../../pocket/ui_font.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"launcher check failed at line %d\n",__LINE__);exit(1);}} while(0)
/* Model of the IDF walk: try the chosen slot, then walk back to factory. */
static const char *boot_result(unsigned reason,int slot,int launcher_valid,int stock_valid) {
    for(int index=pj_boot_choose(reason,slot);index>=PJ_BOOT_STOCK;index--) {
        if(index==PJ_BOOT_LAUNCHER&&launcher_valid)return "launcher";
        if(index==PJ_BOOT_STOCK&&stock_valid)return "stock";
    }
    return "none";
}
static void boot_checks(void) {
    /* Every reason ESP32 defines, plus junk: only power-on may reach the launcher. */
    for(unsigned reason=0;reason<256;reason++) {
        CHECK(pj_boot_choose(reason,1)==(reason==1?PJ_BOOT_LAUNCHER:PJ_BOOT_STOCK));
        CHECK(pj_boot_choose(reason,0)==PJ_BOOT_STOCK); /* no launcher slot */
        CHECK(!strcmp(boot_result(reason,1,0,1),"stock")); /* damaged launcher */
    }
    CHECK(!strcmp(boot_result(1,1,1,1),"launcher"));  /* power-on */
    CHECK(!strcmp(boot_result(12,1,1,1),"stock"));    /* launcher chose Games: software reset */
    CHECK(!strcmp(boot_result(9,1,1,1),"stock"));     /* RTC watchdog, core */
    CHECK(!strcmp(boot_result(16,1,1,1),"stock"));    /* RTC watchdog, system: hung launcher */
    CHECK(!strcmp(boot_result(7,1,1,1),"stock"));     /* task group watchdog */
    CHECK(!strcmp(boot_result(15,1,1,1),"stock"));    /* brownout */
    CHECK(!strcmp(boot_result(5,1,1,1),"stock"));     /* deep sleep wake, if ever used */
}
static void framing_checks(void) {
    /* Expected bytes computed by hand from stock SetupTxBuffer and crc8_sae_j1850. */
    uint8_t out[8];
    CHECK(pj_fpga_encode(out,0,PJ_TX_POKE,0x0100)==6);
    CHECK(out[0]==0x8f&&out[1]==9&&out[2]==2&&out[3]==1&&out[4]==0);
    CHECK(pj_crc8(out,6)==0); /* stock decode: CRC over message and CRC is zero */
    CHECK(pj_fpga_encode(out,1,PJ_TX_POKE,0x0100)==4&&out[0]==0x8a&&out[2]==1&&out[3]==0);
    pj_fpga_rx rx;memset(&rx,0,sizeof(rx));uint16_t value=0;
    uint8_t message[6]={0x8f,PJ_RX_BUTTONS,2,0x02,0x08,0};message[5]=pj_crc8(message,5);
    for(unsigned i=0;i<5;i++)CHECK(!pj_fpga_rx_byte(&rx,message[i],&value));
    CHECK(pj_fpga_rx_byte(&rx,message[5],&value)&&value==0x0208);
    message[3]^=1;
    for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,message[i],&value)); /* bad CRC */
    /* A message for another address is ignored, and noise before a marker is skipped. */
    uint8_t other[6]={0x8f,5,2,1,2,0};other[5]=pj_crc8(other,5);
    for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,other[i],&value));
    CHECK(!pj_fpga_rx_byte(&rx,0x00,&value));
    uint8_t v1[4]={0x8a,PJ_RX_BUTTONS,0x04,0x08};
    for(unsigned i=0;i<3;i++)CHECK(!pj_fpga_rx_byte(&rx,v1[i],&value));
    CHECK(pj_fpga_rx_byte(&rx,v1[3],&value)&&value==((4u<<7)|8u)&&rx.v1);
    uint8_t oversize[3]={0x8f,2,200};rx.state=0;
    for(unsigned i=0;i<3;i++)CHECK(!pj_fpga_rx_byte(&rx,oversize[i],&value));
    CHECK(rx.state==0);
    /* The Version message is kept aside, never mistaken for buttons: stock 18.14 (no debug bit) is
     * not a media FPGA; the media build (debug bit, minor 40) is; a bad CRC changes nothing. */
    CHECK(!rx.version_seen);
    uint16_t stock=(uint16_t)((14u<<6)|18u),media=(uint16_t)((1u<<12)|(PJ_FPGA_MEDIA_MINOR<<6)|PJ_FPGA_MEDIA_MAJOR);
    uint8_t ver[6]={0x8f,PJ_RX_VERSION,2,(uint8_t)(stock>>8),(uint8_t)stock,0};ver[5]=pj_crc8(ver,5);
    for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,ver[i],&value));
    CHECK(rx.version_seen&&rx.version==stock&&!pj_fpga_media_audio(rx.version));
    ver[3]=(uint8_t)(media>>8);ver[4]=(uint8_t)media;ver[5]=pj_crc8(ver,5);
    for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,ver[i],&value));
    CHECK(rx.version==media&&pj_fpga_media_audio(rx.version));
    ver[4]^=0x40;
    for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,ver[i],&value)); /* bad CRC: kept the last good one */
    CHECK(rx.version==media);
    CHECK(!pj_fpga_media_audio((uint16_t)((PJ_FPGA_MEDIA_MINOR<<6)|PJ_FPGA_MEDIA_MAJOR))); /* same minor without debug: not ours */
    /* 1.3.0: the combined image 18.41 (sound + Pocket link) has sound too; only it has the link. Stock 18.14, the 18.40
     * sound image, 18.41 without the debug bit, another major or minor: no link. */
    {uint16_t link=(uint16_t)((1u<<12)|(PJ_FPGA_LINK_MINOR<<6)|PJ_FPGA_MEDIA_MAJOR);
     CHECK(PJ_FPGA_LINK_MINOR==41u&&pj_fpga_media_audio(link)&&pj_fpga_pocket_link(link));
     CHECK(!pj_fpga_pocket_link(media)&&!pj_fpga_pocket_link(stock)&&!pj_fpga_pocket_link((uint16_t)((PJ_FPGA_LINK_MINOR<<6)|PJ_FPGA_MEDIA_MAJOR)));
     CHECK(!pj_fpga_pocket_link((uint16_t)((1u<<12)|(PJ_FPGA_LINK_MINOR<<6)|19u))&&!pj_fpga_pocket_link((uint16_t)((1u<<12)|(42u<<6)|18u)));
     CHECK(!pj_fpga_media_audio((uint16_t)((1u<<12)|(42u<<6)|18u))&&!pj_fpga_media_audio((uint16_t)((1u<<12)|(39u<<6)|18u)));
     for(unsigned v=0;v<0x4000u;v++)CHECK(pj_fpga_pocket_link((uint16_t)v)==((v&0x1fffu)==link)); /* one version (bit 13 is the reserved bit, ignored as for 18.40) */
     ver[3]=(uint8_t)(link>>8);ver[4]=(uint8_t)link;ver[5]=pj_crc8(ver,5);
     for(unsigned i=0;i<6;i++)CHECK(!pj_fpga_rx_byte(&rx,ver[i],&value));
     CHECK(rx.version==link&&pj_fpga_pocket_link(rx.version)&&pj_fpga_media_audio(rx.version));}
    CHECK(pj_fpga_encode(out,0,PJ_TX_MEDIA,PJ_MEDIA_AUDIO)==6&&out[1]==0x1d&&out[3]==0&&out[4]==1&&pj_crc8(out,6)==0);
}
/* Runs the launcher against a modeled FPGA menu toggle, following stock RTL:
 * qspi_slave.v ignores the menu line until two frames arrived; system_monitor.sv
 * toggles on release, needs a cartridge, and cancels if any other button was down. */
typedef struct {int cart,alive,open,pressed,pokes,frames,dirty;uint16_t held;} fpga_model;
static void model_poke(fpga_model *f,uint16_t value) {
    int pressed=(value&PJ_POKE_MENU)!=0;
    if(pressed&&!f->pressed){f->pokes++;f->dirty=f->held!=0;}
    if(f->held)f->dirty=1;
    if(f->pressed&&!pressed&&f->cart&&f->frames>=2&&!f->dirty)f->open=!f->open;
    f->pressed=pressed;
}
static uint16_t model_buttons(const fpga_model *f) {return (uint16_t)(f->held|(f->open?0:PJ_MENU_CLOSED));}
/* The media FPGA with PJ_MEDIA_MENU set: a physical Menu press is reported (bit 8), the overlay stays. */
static void physical_menu_forwarded(pj_launcher *l,fpga_model *f,uint32_t now) {
    pj_launch_buttons(l,(uint16_t)(model_buttons(f)|PJ_BTN_MENU),now);
    pj_launch_buttons(l,model_buttons(f),now+1);
}
static uint32_t run(pj_launcher *l,fpga_model *f,uint32_t now,uint32_t until,int *restarted) {
    for(;now<until&&!*restarted;now+=10) {
        pj_launch_action a=pj_launch_step(l,now);
        if(a.prime_frames)f->frames+=2;
        if(a.poke)model_poke(f,a.poke_value);
        /* Never leaves a press held, and never hands over with the overlay open while
         * the FPGA is answering (stock would start under it). */
        if(a.restart_to_stock){CHECK(!f->pressed&&(!f->open||!f->alive));*restarted=1;break;}
        if(a.request_buttons&&f->alive)pj_launch_buttons(l,model_buttons(f),now);
    }
    return now;
}
static void press(pj_launcher *l,fpga_model *f,uint16_t button,uint32_t now) {
    f->held=button;pj_launch_buttons(l,model_buttons(f),now);
    f->held=0;pj_launch_buttons(l,model_buttons(f),now+1);
}
static void menu_checks(void) {
    pj_launcher l;fpga_model f;int restarted;uint32_t now;
    /* Unattended boot: the menu opens and waits for the owner's choice, for as long as it
     * takes (an hour here); nothing is chosen for them. A then starts Games. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);CHECK(l.phase==PJ_L_MENU&&f.open&&l.selected==PJ_ITEM_GAMES&&f.pokes==1);
    CHECK(f.frames==2); /* primed before the first press */
    now=run(&l,&f,now,3600000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MENU&&f.open&&f.pokes==1);
    press(&l,&f,PJ_BTN_A,now);now=run(&l,&f,now+10,now+3000,&restarted);
    CHECK(restarted&&!f.open&&!f.pressed);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* Without the two priming frames the stock FPGA would ignore every press. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    pj_launch_step(&l,0);CHECK(l.phase==PJ_L_OPEN_PRESS); /* prime consumed, not modelled */
    now=run(&l,&f,0,20000,&restarted);CHECK(restarted&&!f.open&&f.pokes==(int)PJ_LAUNCH_OPEN_TRIES);CHECK(l.done_reason==PJ_DONE_OPEN_NO_RESPONSE&&l.close_reason==PJ_CLOSE_NONE);
    /* No cartridge: the overlay can never open, so stock starts after bounded tries. */
    memset(&f,0,sizeof(f));f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,20000,&restarted);CHECK(restarted&&f.pokes==(int)PJ_LAUNCH_OPEN_TRIES&&now<6000);CHECK(l.done_reason==PJ_DONE_OPEN_NO_RESPONSE&&l.close_reason==PJ_CLOSE_NONE);
    /* FPGA silent: no reading at all, so no blind press that could toggle it the wrong way. */
    memset(&f,0,sizeof(f));f.cart=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,20000,&restarted);CHECK(restarted&&f.pokes==0&&now<2000);CHECK(l.done_reason==PJ_DONE_NO_FPGA_REPLY&&l.close_reason==PJ_CLOSE_NONE);
    /* Overlay already open (warm reset with stock's menu up): adopt it, never press. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;f.open=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,600,&restarted);CHECK(l.phase==PJ_L_MENU&&f.pokes==0);
    /* Choose Media: it stays (an hour here, no idle limit); B returns to the menu, which
     * also waits; B there starts Games. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);
    press(&l,&f,PJ_BTN_DOWN,now);CHECK(l.selected==PJ_ITEM_MEDIA);
    press(&l,&f,PJ_BTN_A,now+20);CHECK(l.phase==PJ_L_MEDIA);
    now=run(&l,&f,now+30,now+3600000,&restarted);CHECK(l.phase==PJ_L_MEDIA&&!restarted&&f.open);
    press(&l,&f,PJ_BTN_B,now);CHECK(l.phase==PJ_L_MENU&&l.selected==PJ_ITEM_MEDIA);
    now=run(&l,&f,now+10,now+600000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MENU);
    press(&l,&f,PJ_BTN_B,now);now=run(&l,&f,now+10,now+3000,&restarted);
    CHECK(restarted&&!f.open);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_B_BUTTON);
    /* The user closes the overlay with the real menu button. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);f.open=0;
    now=run(&l,&f,now,now+300,&restarted);CHECK(restarted);CHECK(l.done_reason==PJ_DONE_MENU_BUTTON&&l.close_reason==PJ_CLOSE_NONE);
    /* Media FPGA, PJ_MEDIA_MENU set by the shell (menu_to_us): the Menu button is forwarded, the
     * overlay stays. In the start-up menu it does nothing (Games is still the item or B); in Media
     * and Other it is Back; with the Jellyfin app owning Media it is the app's (JF_BTN_MENU). A held
     * Menu counts once. Without menu_to_us (stock FPGA) bit 8 is ignored as before. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);CHECK(l.phase==PJ_L_MENU&&f.open);
    physical_menu_forwarded(&l,&f,now);now=run(&l,&f,now+10,now+600,&restarted);
    CHECK(!restarted&&l.phase==PJ_L_MENU&&f.open&&f.pokes==1); /* stock FPGA: ignored */
    l.menu_to_us=1;
    physical_menu_forwarded(&l,&f,now);now=run(&l,&f,now+10,now+600,&restarted);
    CHECK(!restarted&&l.phase==PJ_L_MENU&&f.open&&f.pokes==1&&l.selected==PJ_ITEM_GAMES); /* menu: nothing */
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_A,now+5);CHECK(l.phase==PJ_L_MEDIA);
    physical_menu_forwarded(&l,&f,now+20);CHECK(l.phase==PJ_L_MENU&&l.selected==PJ_ITEM_MEDIA); /* Media: back */
    press(&l,&f,PJ_BTN_A,now+40);CHECK(l.phase==PJ_L_MEDIA);
    l.app_owns_media=1;l.app_pressed=0;
    pj_launch_buttons(&l,(uint16_t)(model_buttons(&f)|PJ_BTN_MENU),now+60);
    pj_launch_buttons(&l,(uint16_t)(model_buttons(&f)|PJ_BTN_MENU),now+70); /* still held: once */
    CHECK(l.app_pressed==PJ_BTN_MENU&&l.phase==PJ_L_MEDIA);
    l.app_pressed=0;pj_launch_buttons(&l,model_buttons(&f),now+80);CHECK(l.app_pressed==0);
    physical_menu_forwarded(&l,&f,now+90);CHECK(l.app_pressed==PJ_BTN_MENU); /* a new press */
    l.app_owns_media=0;l.app_pressed=0;
    physical_menu_forwarded(&l,&f,now+100);CHECK(l.phase==PJ_L_MENU); /* back to the menu */
    press(&l,&f,PJ_BTN_B,now+120);now=run(&l,&f,now+130,now+3000,&restarted);
    CHECK(restarted&&!f.open&&l.close_reason==PJ_CLOSE_B_BUTTON); /* Games still one B away */
    /* The physical Menu button closing the overlay (bit clear, or stock) still means games. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);l.menu_to_us=1;f.open=0;
    now=run(&l,&f,now,now+300,&restarted);CHECK(restarted&&l.done_reason==PJ_DONE_MENU_BUTTON);
    /* Credits (Select on the start-up menu): opens a plain overlay; B or Select returns; while it is
     * up Games/A/the arrows do nothing, and afterwards A/B/Games behave exactly as before. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);CHECK(l.phase==PJ_L_MENU&&f.open&&!l.credits_open);
    press(&l,&f,PJ_BTN_SELECT,now);CHECK(l.credits_open&&l.phase==PJ_L_MENU); /* Select opens Credits */
    press(&l,&f,PJ_BTN_DOWN,now+10);CHECK(l.credits_open&&l.selected==PJ_ITEM_GAMES); /* arrows do nothing */
    press(&l,&f,PJ_BTN_A,now+20);CHECK(l.credits_open&&l.phase==PJ_L_MENU&&!restarted); /* A does nothing: no Games */
    press(&l,&f,PJ_BTN_B,now+30);CHECK(!l.credits_open&&l.phase==PJ_L_MENU); /* B returns, not Games */
    now=run(&l,&f,now+40,now+3000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MENU&&f.open); /* still the menu */
    press(&l,&f,PJ_BTN_SELECT,now);CHECK(l.credits_open); /* open again */
    press(&l,&f,PJ_BTN_SELECT,now+10);CHECK(!l.credits_open); /* Select also closes */
    press(&l,&f,PJ_BTN_A,now+20);now=run(&l,&f,now+30,now+3000,&restarted); /* A on Games still starts Games */
    CHECK(restarted&&!f.open&&l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* Any button held at power-on skips the menu: no press at all, stock at once, so the
     * boot logo's palette shortcuts still work. */
    for(unsigned b=0;b<8;b++) {
        memset(&f,0,sizeof(f));f.cart=f.alive=1;f.held=(uint16_t)(1u<<b);restarted=0;pj_launch_init(&l,0);
        now=run(&l,&f,0,10000,&restarted);CHECK(restarted&&f.pokes==0&&!f.open&&now<1000);CHECK(l.done_reason==PJ_DONE_HELD_AT_POWER_ON&&l.close_reason==PJ_CLOSE_NONE);
    }
    /* No cartridge, and a button goes down after the first press: opening gives up once
     * the release wait runs out, without another press. */
    memset(&f,0,sizeof(f));f.alive=1;restarted=0;pj_launch_init(&l,0);now=0;
    while(f.pokes<1&&!restarted)now=run(&l,&f,now,now+10,&restarted);
    f.held=PJ_BTN_A;now=run(&l,&f,now,now+8000,&restarted);
    CHECK(restarted&&f.pokes==1&&!f.open);CHECK(l.done_reason==PJ_DONE_OPEN_BUTTONS_HELD&&l.close_reason==PJ_CLOSE_NONE);
    /* Choosing Games with A still held: the close waits for the release, then works. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);
    f.held=PJ_BTN_A;pj_launch_buttons(&l,model_buttons(&f),now);CHECK(l.phase==PJ_L_CLOSE_PRESS);
    now=run(&l,&f,now+10,now+800,&restarted);CHECK(!restarted&&f.open&&f.pokes==1);
    f.held=0;now=run(&l,&f,now,now+3000,&restarted);CHECK(restarted&&!f.open&&f.pokes==2);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* Cartridge pulled during the menu: the overlay cannot close, so no hand-over; the
     * screen says why, and reinserting the cartridge lets it finish. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);f.cart=0;press(&l,&f,PJ_BTN_A,now);
    now=run(&l,&f,now+10,now+30000,&restarted);CHECK(!restarted&&f.open&&l.close_hint);
    f.cart=1;now=run(&l,&f,now,now+3000,&restarted);CHECK(restarted&&!f.open);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* Buttons mashed while Games starts: waits, then closes when they are let go. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);press(&l,&f,PJ_BTN_A,now);
    for(unsigned i=0;i<40&&!restarted;i++) {
        f.held=(i%2)?PJ_BTN_B:0;now=run(&l,&f,now,now+100,&restarted);
    }
    CHECK(!restarted||!f.open);
    f.held=0;now=run(&l,&f,now,now+5000,&restarted);CHECK(restarted&&!f.open);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* The FPGA goes silent with the overlay open: on the menu, on Jellyfin and on Other,
     * and while closing: the owner cannot choose anything, so stock starts, without a press. */
    for(unsigned screen=0;screen<4;screen++) {
        memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
        now=run(&l,&f,0,1200,&restarted);
        if(screen==1){press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_A,now+5);CHECK(l.phase==PJ_L_MEDIA);}
        if(screen==2){press(&l,&f,PJ_BTN_UP,now);press(&l,&f,PJ_BTN_A,now+5);CHECK(l.phase==PJ_L_OTHER);}
        if(screen==3){f.cart=0;press(&l,&f,PJ_BTN_A,now);now=run(&l,&f,now+10,now+5000,&restarted);CHECK(l.close_hint);}
        now=run(&l,&f,now+10,now+60000,&restarted);CHECK(!restarted);
        int pokes=f.pokes;f.alive=0;uint32_t quiet=now;now=run(&l,&f,now,now+5000,&restarted);
        CHECK(restarted&&(screen==3||(now-quiet<=PJ_LAUNCH_SILENT_MS+20&&f.pokes==pokes)));CHECK(l.done_reason==PJ_DONE_NO_FPGA_REPLY);
    }
    /* 1.3.9 ("handover to=stock done=no-fpga-reply" 165 s into a Pocket link session): while the
     * Pocket link runs on the Media screen, readings that stop while the link brings the UART back to 115,200 (its
     * bridge fell silent: 1 s to notice, the leave at both rates, the bridge's own 3 s guard) are not a dead FPGA. A gap
     * of 1.5 s and of 5.5 s hands nothing over and the screen stays; a dead FPGA still goes to stock, after
     * PJ_LAUNCH_LINK_SILENT_MS, without a press. The same gap with the link not running (the plain Jellyfin screen)
     * still goes to stock after 1.5 s, and so does the menu with media_link left set (only Media takes the longer bound). */
    for(unsigned link_case=0;link_case<4;link_case++) {
        memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
        now=run(&l,&f,0,1200,&restarted);
        if(link_case!=3){press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_A,now+5);CHECK(l.phase==PJ_L_MEDIA);}
        l.app_owns_media=link_case!=2;l.media_link=(uint8_t)(link_case!=2);
        now=run(&l,&f,now+10,now+20000,&restarted);CHECK(!restarted);
        int pokes=f.pokes;uint32_t quiet=now;f.alive=0;
        if(link_case==0) { /* recovering: two silences shorter than the bound, readings back between them */
            now=run(&l,&f,now,now+1600,&restarted);CHECK(!restarted&&l.phase==PJ_L_MEDIA);
            f.alive=1;now=run(&l,&f,now,now+300,&restarted);CHECK(!restarted);
            f.alive=0;now=run(&l,&f,now,now+5500,&restarted);CHECK(!restarted&&l.phase==PJ_L_MEDIA&&f.pokes==pokes);
            f.alive=1;now=run(&l,&f,now,now+20000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MEDIA);
            continue;
        }
        now=run(&l,&f,now,now+10000,&restarted);CHECK(restarted&&l.done_reason==PJ_DONE_NO_FPGA_REPLY&&f.pokes==pokes);
        if(link_case==1)CHECK(now-quiet>=PJ_LAUNCH_LINK_SILENT_MS-PJ_LAUNCH_POLL_MS&&now-quiet<=PJ_LAUNCH_LINK_SILENT_MS+20); /* dead FPGA, link running */
        else CHECK(now-quiet<=PJ_LAUNCH_SILENT_MS+20);                                                       /* no link / the menu */
    }
    CHECK(PJ_LAUNCH_LINK_SILENT_MS>PJ_LAUNCH_SILENT_MS&&PJ_LAUNCH_LINK_SILENT_MS<=10000u);
    /* Up from Games wraps to Other firmware; the menu keeps waiting. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);
    press(&l,&f,PJ_BTN_UP,now);CHECK(l.selected==PJ_ITEM_OTHER);
    now=run(&l,&f,now+10,now+600000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MENU&&l.selected==PJ_ITEM_OTHER);
    /* Other firmware: stock launches; an empty slot only explains; B returns to the menu. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+20);CHECK(l.selected==PJ_ITEM_OTHER);
    press(&l,&f,PJ_BTN_A,now+40);CHECK(l.phase==PJ_L_OTHER&&l.other_selected==0);
    press(&l,&f,PJ_BTN_DOWN,now+60);press(&l,&f,PJ_BTN_A,now+80);
    CHECK(l.phase==PJ_L_OTHER&&l.other_note&&l.other_selected==1);
    now=run(&l,&f,now+90,now+5000,&restarted);CHECK(!restarted&&l.phase==PJ_L_OTHER);
    press(&l,&f,PJ_BTN_UP,now);CHECK(l.other_selected==0&&!l.other_note);
    press(&l,&f,PJ_BTN_UP,now+20);CHECK(l.other_selected==2);
    press(&l,&f,PJ_BTN_B,now+40);CHECK(l.phase==PJ_L_MENU);
    now=run(&l,&f,now+50,now+8000,&restarted);CHECK(!restarted);
    press(&l,&f,PJ_BTN_A,now);CHECK(l.phase==PJ_L_OTHER);
    press(&l,&f,PJ_BTN_A,now+20);now=run(&l,&f,now+30,now+2000,&restarted);CHECK(restarted&&!f.open);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_OTHER_STOCK);
    /* A chord of A and a direction acts on what is highlighted, without moving first. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);
    press(&l,&f,(uint16_t)(PJ_BTN_A|PJ_BTN_DOWN),now);CHECK(l.selected==PJ_ITEM_GAMES&&l.phase==PJ_L_CLOSE_PRESS);
    now=run(&l,&f,now+10,now+2000,&restarted);CHECK(restarted);CHECK(l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
    /* Jellyfin screen: the radio starts only on A, once; B returns to the menu. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_A,now+20);
    CHECK(l.phase==PJ_L_MEDIA&&!l.scan_requested);
    press(&l,&f,PJ_BTN_A,now+40);CHECK(l.scan_requested&&l.phase==PJ_L_MEDIA);
    press(&l,&f,PJ_BTN_B,now+60);now=run(&l,&f,now+70,now+3000,&restarted);CHECK(!restarted&&f.open&&l.phase==PJ_L_MENU);
    /* Start also returns from Jellyfin; the menu button closes any screen to stock. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_A,now+20);
    press(&l,&f,PJ_BTN_START,now+40);now=run(&l,&f,now+50,now+2000,&restarted);CHECK(!restarted&&l.phase==PJ_L_MENU);
    f.open=0;now=run(&l,&f,now,now+300,&restarted);CHECK(restarted&&l.done_reason==PJ_DONE_MENU_BUTTON);
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0);
    now=run(&l,&f,0,1200,&restarted);press(&l,&f,PJ_BTN_UP,now);press(&l,&f,PJ_BTN_A,now+20);
    CHECK(l.phase==PJ_L_OTHER);f.open=0;now=run(&l,&f,now+30,now+300,&restarted);CHECK(restarted);CHECK(l.done_reason==PJ_DONE_MENU_BUTTON&&l.close_reason==PJ_CLOSE_NONE);
    /* Timer wrap during the menu. */
    memset(&f,0,sizeof(f));f.cart=f.alive=1;restarted=0;pj_launch_init(&l,0xfffff000u);
    uint32_t t=0xfffff000u;int opened=0;
    for(unsigned i=0;i<900&&!restarted;i++,t+=10) {
        pj_launch_action a=pj_launch_step(&l,t);
        if(a.prime_frames)f.frames+=2;
        if(l.phase==PJ_L_MENU)opened=1;
        if(a.poke)model_poke(&f,a.poke_value);
        if(a.restart_to_stock)restarted=1;
        if(a.request_buttons)pj_launch_buttons(&l,model_buttons(&f),t);
        if(opened&&!f.held&&t==0xfffff000u+5000u){f.held=PJ_BTN_A;pj_launch_buttons(&l,model_buttons(&f),t);f.held=0;}
    }
    CHECK(restarted&&opened&&!f.open&&l.close_reason==PJ_CLOSE_CHOSE_GAMES); /* opened and closed across the wrap */
}
/* Every diagnostics event, for every phase and reason, formats into a safe line. */
static void diag_checks(FILE *sample) {
    char body[PJ_DIAG_MAX],line[PJ_DIAG_MAX];
    pj_launcher l;pj_launch_init(&l,0);
    pj_diag_counts c;memset(&c,0,sizeof(c));
    CHECK(sizeof(pj_diag_phase_names)/sizeof(pj_diag_phase_names[0])==PJ_L_DONE+1u);
    /* Worst case for length: every counter and heap figure at its maximum. */
    c.uart_bytes=c.messages=c.polls=c.pokes=c.frames=c.lines=c.unsafe=0xffffffffu;c.last_buttons=0xffff;
    l.held=0xffff;l.tries=99;
    for(unsigned phase=0;phase<=PJ_L_DONE;phase++)for(unsigned d=0;d<PJ_DONE_COUNT;d++)for(unsigned k=0;k<PJ_CLOSE_COUNT;k++) {
        l.phase=(pj_launch_phase)phase;l.done_reason=(uint8_t)d;l.close_reason=(uint8_t)k;
        pj_diag_phase(body,sizeof(body),(phase+1u)%(PJ_L_DONE+1u),&l);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        pj_diag_close(body,sizeof(body),&l);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        pj_diag_done(body,sizeof(body),&l);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        pj_diag_stats(body,sizeof(body),&c,&l,0xffffffffu,0xffffffffu);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        pj_diag_handover(body,sizeof(body),&l,(int)(k&1u),0xffffffffu);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        CHECK(strcmp(PJ_DIAG_NAME(pj_diag_done_names,d),"other")&&strcmp(PJ_DIAG_NAME(pj_diag_close_names,k),"other"));
    }
    for(unsigned reset=0;reset<40;reset++) {
        pj_diag_boot(body,sizeof(body),"1.1.1",reset,reset!=1,0xffffffffu);CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
    }
    /* Wi-Fi: hostile step and error texts collapse into one token each; still safe. */
    const char *names[]={"ESP_OK","ESP_ERR_WIFI_NOT_INIT","UNKNOWN ERROR","a=b c\td",""};
    for(unsigned i=0;i<sizeof(names)/sizeof(names[0]);i++) {
        pj_diag_wifi(body,sizeof(body),(int)(i&1u),65535,names[i],-2147483647-1,names[i]);
        CHECK(pj_diag_line(line,sizeof(line),0xffffffffu,body));
        CHECK(!strchr(strstr(line,"err-name=")+9,' '));
    }
    /* Unsafe bodies are replaced, never printed. */
    const char *bad[]={"wifi ssid=MyHome:5G","mac 0123456789abcdef","PJ-LAUNCHER-READY 9.9.9","x rst:0xc","tab\there",
        "long 0123456789ab"};
    for(unsigned i=0;i<sizeof(bad)/sizeof(bad[0]);i++) {
        CHECK(!pj_diag_line(line,sizeof(line),5,bad[i]));
        CHECK(!strcmp(line,"PJ-DIAG t=5 unsafe-line-dropped")&&pj_diag_safe(line));
    }
    char huge[400];memset(huge,'a',sizeof(huge)-1);huge[sizeof(huge)-1]=0;memcpy(huge,"stats ",6);
    CHECK(!pj_diag_line(line,sizeof(line),5,huge)); /* would not fit: dropped, not cut */
    /* The ring keeps whole lines, newest last, and wraps without growing. */
    static pj_log ring;memset(&ring,0,sizeof(ring));
    for(unsigned i=0;i<1000;i++)pj_log_add(&ring,i,"diag","stats phase=menu uart=1");
    CHECK(ring.used==PJ_LOG_BYTES);
    char tail[64];size_t n=pj_log_tail(&ring,sizeof(tail),tail);
    const char *newest="999 diag stats phase=menu uart=1\n";
    CHECK(n==sizeof(tail)&&!memcmp(tail+n-strlen(newest),newest,strlen(newest)));
    /* A sample session for the docs: boot, the menu waits, the owner presses A on Games. */
    fpga_model f;memset(&f,0,sizeof(f));f.cart=f.alive=1;
    pj_launch_init(&l,0);memset(&c,0,sizeof(c));
    pj_diag_watch w={(uint8_t)l.phase,0,0};
    pj_diag_boot(body,sizeof(body),"1.1.2",1,0,251000);pj_diag_line(line,sizeof(line),0,body);fprintf(sample,"%s\n",line);
    uint32_t now=0;int done=0;
    for(;now<9000&&!done;now+=10) {
        f.held=now>=4000&&now<4100?PJ_BTN_A:0; /* the owner chooses Games after about 3 s on the menu */
        pj_launch_action a=pj_launch_step(&l,now);
        if(a.prime_frames){f.frames+=2;c.frames+=2;}
        if(a.poke){model_poke(&f,a.poke_value);c.pokes++;}
        if(a.request_buttons&&f.alive){c.polls++;c.messages++;c.uart_bytes+=6;c.last_buttons=model_buttons(&f);
            pj_launch_buttons(&l,model_buttons(&f),now);}
        if(l.close_reason!=w.close_reason){w.close_reason=l.close_reason;pj_diag_close(body,sizeof(body),&l);
            CHECK(pj_diag_line(line,sizeof(line),now,body));fprintf(sample,"%s\n",line);}
        if(l.phase!=w.phase){pj_diag_phase(body,sizeof(body),w.phase,&l);w.phase=(uint8_t)l.phase;
            CHECK(pj_diag_line(line,sizeof(line),now,body));fprintf(sample,"%s\n",line);}
        if(l.phase==PJ_L_DONE&&!w.done_reported){w.done_reported=1;pj_diag_done(body,sizeof(body),&l);
            CHECK(pj_diag_line(line,sizeof(line),now,body));fprintf(sample,"%s\n",line);}
        if(now%1000==0&&now) {pj_diag_stats(body,sizeof(body),&c,&l,250000,248000);
            CHECK(pj_diag_line(line,sizeof(line),now,body));fprintf(sample,"%s\n",line);}
        if(a.restart_to_stock){done=1;pj_diag_handover(body,sizeof(body),&l,0,now);
            CHECK(pj_diag_line(line,sizeof(line),now,body));fprintf(sample,"%s\nPJ-LAUNCHER-TO-STOCK\n",line);}
    }
    CHECK(done&&l.done_reason==PJ_DONE_CLOSED&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);
}
static uint16_t screen[PJ_OSD_W*PJ_OSD_H];
static void write_ppm(const char *path) {
    FILE *f=fopen(path,"wb");CHECK(f);fprintf(f,"P6\n160 144\n255\n");
    for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++) {
        unsigned c=screen[i];CHECK(c!=0xf81fu); /* transparent key would show the game through */
        fputc((int)(((c>>11)&31)*255/31),f);fputc((int)(((c>>5)&63)*255/63),f);fputc((int)((c&31)*255/31),f);
    }
    CHECK(!ferror(f));fclose(f);
}
/* Every text must fit the screen with the margins it is drawn at. */
static void text_fits(void) {
    for(unsigned i=0;i<PJ_ITEM_COUNT;i++) {
        CHECK(36+pj_osd_measure(pj_launch_labels[i],pj_font_advance)<=150);
        CHECK(8+pj_osd_measure(pj_launch_notes[i],pj_font_advance)<=156);
    }
    for(unsigned i=0;i<PJ_OTHER_ROWS;i++)CHECK(36+pj_osd_measure(pj_other_labels[i],pj_font_advance)<=150);
    const char *lines[]={"A: start    B: Games","A: launch    B: back","Nothing installed there",
        "Protected, always here","Not available yet","Wi-Fi ok: 65535 networks","Pairing: not available yet",
        "Wi-Fi: no networks heard","Let go of all buttons.","No cartridge? Insert one,","or switch off and on.",
        "Starting Games...","Stock firmware did not","Connect USB and run","Wi-Fi: press A to check","Wi-Fi: listening...","A: check Wi-Fi   B: back",
        "Streaming: not built yet","Wi-Fi: check failed","Other firmware",
        "Wi-Fi dropped: rejoining...","Wi-Fi: rejoining...","Wi-Fi: connected","Wi-Fi: could not join","Closing Jellyfin...",
        "Wi-Fi is back: opening Jellyfin","Joins Wi-Fi when Setup closes","Jellyfin: not enough memory","Wi-Fi: scanning...","Wi-Fi: settings busy, retrying",PJ_JOIN_RETRY_LINE};
    for(unsigned i=0;i<sizeof(lines)/sizeof(lines[0]);i++)CHECK(12+pj_osd_measure(lines[i],pj_font_advance)<=156);
    for(unsigned i=0;i<sizeof(pj_credits_lines)/sizeof(pj_credits_lines[0]);i++)
        CHECK(8+pj_osd_measure(pj_credits_lines[i],pj_font_advance)<=156); /* Credits lines fit at x=8 */
    /* The credits keep the author line and the licence line (GPL-3.0-or-later, no warranty, where the source
     * is), and all of it ends above the "B: back" footer. */
    {
        char all[512]="";
        for(unsigned i=0;i<PJ_CREDITS_COUNT;i++){strcat(all,pj_credits_lines[i]);strcat(all,pj_credits_lines[i][0]?" ":"");}
        CHECK(strstr(all,"Created by ChaoticSPARTAN1")!=NULL);
        CHECK(strstr(all,"GPL-3.0-or-later, no warranty. Source: github.com/ ChaoticSPARTAN1/Jellymatic")!=NULL);
        CHECK(pj_credits_y(PJ_CREDITS_COUNT)<=PJ_CREDITS_FOOTER-4);
    }
}

static pj_wifi_heard heard_ap(const char *name,int rssi) {
    pj_wifi_heard h;memset(&h,0,sizeof(h));h.len=(uint8_t)strlen(name);memcpy(h.ssid,name,h.len);h.rssi=(int8_t)rssi;return h;
}
/* Saved Wi-Fi join order (wifi_order.h): home, the parents' house, home again, without waiting on networks
 * that are not there. */
static void wifi_order_checks(void) {
    const char *saved[4]={"Home","Parents","Office","Hidden"};
    uint8_t order[4];
    /* At the parents' house: only Parents is heard, so it is tried first; the rest follow in saved order. */
    pj_wifi_heard at_parents[3]={heard_ap("Neighbour",-40),heard_ap("Parents",-62),heard_ap("Cafe",-80)};
    CHECK(pj_wifi_join_order(saved,4,at_parents,3,order)==1);
    CHECK(order[0]==1&&order[1]==0&&order[2]==2&&order[3]==3);
    /* Back home: Home first again (nothing was written to remember the parents' network). */
    pj_wifi_heard at_home[2]={heard_ap("Home",-50),heard_ap("Neighbour",-70)};
    CHECK(pj_wifi_join_order(saved,4,at_home,2,order)==1&&order[0]==0&&order[1]==1&&order[2]==2&&order[3]==3);
    /* Two saved networks heard: the saved order decides, not the signal (1.2.9: the list is the owner's
     * priority list; "Use this network now" moves a network to the top). Office is much stronger here and
     * still comes after Home. One name heard on two access points still counts as heard. */
    pj_wifi_heard both[4]={heard_ap("Office",-75),heard_ap("Home",-60),heard_ap("Office",-48),heard_ap("Office",-90)};
    CHECK(pj_wifi_join_order(saved,4,both,4,order)==2&&order[0]==0&&order[1]==2&&order[2]==1&&order[3]==3);
    /* The same two with the saved list reordered (after "Use this network now" on Office): Office first. */
    const char *reordered[4]={"Office","Home","Parents","Hidden"};
    CHECK(pj_wifi_join_order(reordered,4,both,4,order)==2&&order[0]==0&&order[1]==1&&order[2]==2&&order[3]==3);
    /* Equal signals: saved order as well. */
    pj_wifi_heard tie[2]={heard_ap("Parents",-55),heard_ap("Home",-55)};
    CHECK(pj_wifi_join_order(saved,4,tie,2,order)==2&&order[0]==0&&order[1]==1);
    int f;CHECK(pj_wifi_best_rssi("Office",both,4,&f)==-48&&f); /* the strongest reading of a name, for diagnostics */
    /* Failed or empty scan: saved order, every network still tried (a hidden one never answers a scan). */
    CHECK(pj_wifi_join_order(saved,4,NULL,0,order)==0&&order[0]==0&&order[1]==1&&order[2]==2&&order[3]==3);
    /* Names match exactly: case, prefixes and 32-byte names without a terminator. */
    pj_wifi_heard near[3]={heard_ap("home",-30),heard_ap("Hom",-30),heard_ap("HomeX",-30)};
    CHECK(pj_wifi_join_order(saved,4,near,3,order)==0&&order[0]==0);
    const char *long_saved[2]={"ABCDEFGHIJKLMNOPQRSTUVWXYZ012345","Home"};
    pj_wifi_heard full;memset(&full,0,sizeof(full));memcpy(full.ssid,"ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",32);full.len=32;full.rssi=-70;
    pj_wifi_heard two[2]={heard_ap("Home",-40),full};
    CHECK(pj_wifi_join_order(long_saved,2,two,2,order)==2&&order[0]==0&&order[1]==1); /* both heard: saved order, the 32-byte name matched */
    /* An empty saved name never matches; one and zero saved networks work; the scan list is capped. */
    const char *odd[2]={"","Home"};pj_wifi_heard blank=heard_ap("",-20);
    CHECK(pj_wifi_join_order(odd,2,&blank,1,order)==0&&order[0]==0&&order[1]==1);
    CHECK(pj_wifi_join_order(saved,1,at_parents,3,order)==0&&order[0]==0);
    CHECK(pj_wifi_join_order(saved,0,at_parents,3,order)==0);
    pj_wifi_heard many[PJ_WIFI_HEARD_MAX+1];
    for(unsigned i=0;i<PJ_WIFI_HEARD_MAX;i++)many[i]=heard_ap("Neighbour",-50);
    many[PJ_WIFI_HEARD_MAX]=heard_ap("Parents",-30);
    CHECK(pj_wifi_join_order(saved,4,many,PJ_WIFI_HEARD_MAX+1,order)==0); /* past the cap: not read */
    /* Every result is a permutation of 0..count-1. */
    for(unsigned mask=0;mask<16;mask++) {
        pj_wifi_heard some[4];unsigned k=0;
        for(unsigned i=0;i<4;i++)if(mask&(1u<<i))some[k++]=heard_ap(saved[i],-40-(int)(i*7%13));
        unsigned seen=0;CHECK(pj_wifi_join_order(saved,4,some,k,order)==k);
        for(unsigned i=0;i<4;i++){CHECK(order[i]<4);seen|=1u<<order[i];}
        CHECK(seen==15u);
        for(unsigned i=0;i<k;i++)CHECK(mask&(1u<<order[i]));
    }
    puts("PASS: saved Wi-Fi join order is a priority list: networks heard in a scan first, in saved order (a stronger "
         "neighbour never jumps the queue), then the rest in saved order (hidden networks still tried); exact names; "
         "a failed scan keeps the saved order; nothing remembered between joins.");
}
/* 1.3.4: "Wi-Fi password is wrong" after pressing A too early, while the join ran after a
 * power-on; the password was right and the next start joined at once. join_task (launcher_main.c) modelled with the
 * same helpers it uses: one network, each attempt ending with a scripted driver disconnect reason (-1: an address,
 * 0: nothing heard, timed out). Returns the words the screen ends on; counts the retry lines shown and the waits. */
static int model_join(const int *reasons,unsigned n,unsigned *retry_lines,unsigned *waited_ms) {
    pj_join_tally t;pj_join_tally_begin(&t);*retry_lines=0;*waited_ms=0;
    for(unsigned k=0;k<n;k++) {
        if(reasons[k]<0)return PJ_JOIN_OK;
        pj_join_tally_add(&t,reasons[k],0,reasons[k]==0);
        if(!pj_join_retry(&t))break;
        (*retry_lines)++;*waited_ms+=PJ_JOIN_RETRY_MS(t.attempts);
    }
    return pj_join_verdict(&t);
}
static pj_launcher *join_l;static int join_running,join_done,join_starts;
static void shell_pass(void) {
    if(pj_radio_start_wanted(join_l,1,join_running,join_done,0,0,0,0)){join_starts++;join_running=1;}
}
static void join_press(uint16_t b,uint32_t *now) {
    pj_launch_buttons(join_l,b,*now);*now+=60;shell_pass();
    pj_launch_buttons(join_l,0,*now);*now+=460;shell_pass(); /* released, and a reading after the settle time */
}
static void wifi_join_checks(void) {
    /* A pressed mid-join, on every screen it can reach: the join is never restarted, stopped or doubled. */
    pj_launcher l;pj_launch_init(&l,0);uint32_t now=1000;
    l.phase=PJ_L_MENU;l.menu_open=1;l.selected=PJ_ITEM_MEDIA;l.fresh=1;
    join_l=&l;join_running=join_done=join_starts=0;
    shell_pass();CHECK(join_starts==0&&!join_running);                      /* the start-up menu: no join yet */
    join_press(PJ_BTN_A,&now);CHECK(l.phase==PJ_L_MEDIA&&join_starts==1&&join_running); /* A on Jellyfin: the one join */
    for(unsigned i=0;i<10;i++)join_press(PJ_BTN_A,&now);                           /* A, again and again, while it joins */
    CHECK(l.phase==PJ_L_MEDIA&&join_starts==1&&join_running&&l.scan_requested);
    join_press(PJ_BTN_B,&now);CHECK(l.phase==PJ_L_MENU&&join_starts==1);           /* back to the menu: the join goes on */
    join_press(PJ_BTN_DOWN,&now);join_press(PJ_BTN_UP,&now);join_press(PJ_BTN_A,&now);CHECK(l.phase==PJ_L_MEDIA&&join_starts==1);
    join_press(PJ_BTN_START,&now);join_press(PJ_BTN_A,&now);join_press(PJ_BTN_A,&now);CHECK(l.phase==PJ_L_MEDIA&&join_starts==1);
    join_running=0;join_done=1;for(unsigned i=0;i<5;i++)join_press(PJ_BTN_A,&now); /* the join ended: A starts no second one */
    CHECK(join_starts==1&&!join_running&&l.phase==PJ_L_MEDIA);
    {   /* the rest of the condition, as the shell gives it */
        pj_launcher m=l;m.phase=PJ_L_MEDIA;m.scan_requested=0;
        CHECK(pj_radio_start_wanted(&m,1,0,0,0,0,0,0)&&!pj_radio_start_wanted(&m,1,1,0,0,0,0,0)&&!pj_radio_start_wanted(&m,1,0,1,0,0,0,0));
        CHECK(!pj_radio_start_wanted(&m,1,0,0,1,0,0,0)&&pj_radio_start_wanted(&m,1,0,0,1,1,0,0));
        CHECK(!pj_radio_start_wanted(&m,1,0,0,0,0,1,0)&&!pj_radio_start_wanted(&m,1,0,0,0,0,0,1)&&!pj_radio_start_wanted(&m,0,0,0,0,0,0,0));
        m.scan_requested=1;CHECK(pj_radio_start_wanted(&m,0,0,0,0,0,0,0));
        m.phase=PJ_L_MENU;CHECK(!pj_radio_start_wanted(&m,1,0,0,0,0,0,0));
    }
    /* The join's words: one handshake failure (an access point still holding the last association after a power
     * cut) is retried and never called a wrong password; only a failed authentication on every try is. */
    unsigned lines,waited;
    const int stale_then_ok[]={15,-1},stale_twice_ok[]={204,2,-1},bad_key[]={15,15,15},auth_fail[]={202,202,202};
    const int mixed[]={205,15,204},then_gone[]={15,201},then_silent[]={15,0},not_found[]={201},no_answer[]={0};
    CHECK(model_join(stale_then_ok,2,&lines,&waited)==PJ_JOIN_OK&&lines==1&&waited==2000u);
    CHECK(model_join(stale_twice_ok,3,&lines,&waited)==PJ_JOIN_OK&&lines==2&&waited==7000u);
    CHECK(model_join(bad_key,3,&lines,&waited)==PJ_JOIN_WRONG_PASSWORD&&lines==2);
    CHECK(model_join(auth_fail,3,&lines,&waited)==PJ_JOIN_WRONG_PASSWORD&&lines==2);
    CHECK(model_join(mixed,3,&lines,&waited)==PJ_JOIN_WRONG_PASSWORD&&lines==2); /* three tries, 7 s apart in all, each refused */
    CHECK(model_join(then_gone,2,&lines,&waited)==PJ_JOIN_NOT_FOUND&&lines==1);
    CHECK(model_join(then_silent,2,&lines,&waited)==PJ_JOIN_NO_ANSWER&&lines==1);
    CHECK(model_join(not_found,1,&lines,&waited)==PJ_JOIN_NOT_FOUND&&lines==0);
    CHECK(model_join(no_answer,1,&lines,&waited)==PJ_JOIN_NO_ANSWER&&lines==0);
    for(int reason=0;reason<300;reason++) { /* no single failure, of any reason, is a wrong password; nor two */
        pj_join_tally t;pj_join_tally_begin(&t);pj_join_tally_add(&t,reason,0,0);
        CHECK(pj_join_verdict(&t)!=PJ_JOIN_WRONG_PASSWORD&&pj_join_result(reason,0,0,0)!=PJ_JOIN_WRONG_PASSWORD);
        pj_join_tally_add(&t,reason,0,0);CHECK(pj_join_verdict(&t)!=PJ_JOIN_WRONG_PASSWORD);
    }
    CHECK(PJ_JOIN_ATTEMPTS==3u&&strstr(PJ_JOIN_RETRY_LINE,"password")==NULL);
    puts("PASS: 1.3.4 Wi-Fi join: A pressed mid-join (Jellyfin screen, back to the menu and in again, Start) never "
         "restarts, stops or doubles the join, and none starts by itself after it ended; one or two handshake or "
         "authentication failures are retried (2 s, then 5 s) and never read \"The password is wrong\"; only a failure "
         "on every one of three tries does; not-found and silent networks are not retried. Modelled driver, no radio.");
}
int main(int argc,char **argv) {
    CHECK(argc==6);
    boot_checks();framing_checks();menu_checks();text_fits();wifi_order_checks();wifi_join_checks();
    FILE *sample=fopen(argv[5],"w");CHECK(sample);diag_checks(sample);CHECK(!ferror(sample));fclose(sample);
    pj_launcher l;pj_launch_init(&l,0);
    pj_launch_draw_menu(screen,&l,1500,pj_font_alpha,pj_font_advance);write_ppm(argv[1]);
    l.selected=PJ_ITEM_MEDIA;
    pj_launch_draw_menu(screen,&l,1500,pj_font_alpha,pj_font_advance);write_ppm(argv[2]);
    const char *const lines[3]={"Wi-Fi ok: 7 networks","Pairing: not available yet","Streaming: not built yet"};
    pj_launch_draw_media(screen,lines,pj_font_alpha,pj_font_advance);write_ppm(argv[3]);
    l.other_selected=1;l.other_note=1;
    pj_launch_draw_other(screen,&l,pj_font_alpha,pj_font_advance);write_ppm(argv[4]);
    /* Credits preview, written next to the first ppm (no new argv, so the Manager caller is untouched). */
    {char cpath[512];const char *slash=argv[1];
     for(const char *p=argv[1];*p;p++)if(*p=='/'||*p=='\\')slash=p+1;
     snprintf(cpath,sizeof(cpath),"%.*sui-credits.ppm",(int)(slash-argv[1]),argv[1]);
     pj_launch_draw_credits(screen,pj_font_alpha,pj_font_advance);write_ppm(cpath);
     /* 1.3.0: the Pocket link screen in each state, and every status line fits the row. */
     static const char *const names[6]={"waiting","connected","syncing","streaming","unplugged","stopped"};
     for(unsigned k=0;k<6;k++) {
         pj_plink_status s={(uint8_t)k,7,19,"link fault",0,0,0,0,{PJ_PLH_NOTHING,0,0,0}};char words[40];
         unsigned rgb=pj_plink_words(&s,words,sizeof(words));
         CHECK(28+pj_osd_measure(words,pj_font_advance)<=150);
         CHECK(rgb==(k==PJ_PLV_CONNECTED||k==PJ_PLV_STREAMING?PJ_UI_SAFE:k==PJ_PLV_UNPLUGGED||k==PJ_PLV_STOPPED?PJ_PLS_RED:PJ_UI_NOTE));
         pj_plink_draw(screen,&s,1000,pj_font_alpha,pj_font_advance);
         snprintf(cpath,sizeof(cpath),"%.*sui-plink-%s.ppm",(int)(slash-argv[1]),argv[1],names[k]);write_ppm(cpath);
     }
     {pj_plink_status s={PJ_PLV_SYNCING,7,19,"",0,0,0,0,{PJ_PLH_NOTHING,0,0,0}};char words[40];pj_plink_words(&s,words,sizeof(words));CHECK(!strcmp(words,"Syncing 7 of 19"));}
     static const char *const why[]={"FPGA did not enter link mode","fast UART not confirmed","FPGA status stale","link fault","link mode ended","x",NULL,
         "no reply from the FPGA bridge","FPGA status malformed","FPGA UART busy", /* 1.3.5: three more reasons */
         "fast UART request not sent"}; /* 1.3.6: the request that never left the transmit ring */
     for(unsigned k=0;k<11;k++)CHECK(8+pj_osd_measure(pj_plink_why_words(why[k]),pj_font_advance)<=156&&
                                      8+pj_osd_measure(pj_plink_stage_words(why[k]),pj_font_advance)<=156);
     /* 1.3.5: a stopped link says the stage it failed in; distinct words for "no reply" and "refused"; a slow link
      * (fast UART failed, 115,200 kept) reads as a note, not green, and its lines fit. */
     CHECK(!strcmp(pj_plink_stage_words("no reply from the FPGA bridge"),"Stage 1 of 5: entering.")&&
           !strcmp(pj_plink_stage_words("fast UART not confirmed"),"Stage 2 of 5: fast UART.")&&
           !strcmp(pj_plink_stage_words("fast UART request not sent"),"Stage 2 of 5: fast UART.")&&
           !strcmp(pj_plink_why_words("fast UART request not sent"),pj_plink_why_words("fast UART not confirmed"))&&
           !strcmp(pj_plink_stage_words("link fault"),"Stage 4 of 5: cable.")&&!strcmp(pj_plink_stage_words("x"),""));
     CHECK(strcmp(pj_plink_why_words("no reply from the FPGA bridge"),pj_plink_why_words("FPGA did not enter link mode")));
     {pj_plink_status s={PJ_PLV_CONNECTED,0,0,"",1,0,0,0,{PJ_PLH_NOTHING,0,0,0}};char words[40];
      CHECK(pj_plink_words(&s,words,sizeof(words))==PJ_UI_NOTE&&!strcmp(words,"Pocket link: slow"));
      pj_plink_draw(screen,&s,1000,pj_font_alpha,pj_font_advance);
      snprintf(cpath,sizeof(cpath),"%.*sui-plink-slow.ppm",(int)(slash-argv[1]),argv[1]);write_ppm(cpath);
      s.what=PJ_PLV_STOPPED;s.why="no reply from the FPGA bridge";s.slow=0;
      pj_plink_draw(screen,&s,1000,pj_font_alpha,pj_font_advance);
      snprintf(cpath,sizeof(cpath),"%.*sui-plink-stopped-stage.ppm",(int)(slash-argv[1]),argv[1]);write_ppm(cpath);}
     /* 1.3.6: while it waits, the screen says what the cable has carried: a count line and one plain sentence for
      * every finding of the link diagnostics. Every sentence fits; the count line fits with the largest numbers;
      * nothing is drawn outside the text area, into the margins or over the button, at any finding. */
     {static const struct {const char *name;uint32_t heard,good,bad;pj_pld_hint hint;const char *a,*b;} cases[]={
          {"nothing",0,0,0,{PJ_PLH_NOTHING,0,0,0},"No data from the Pocket yet.",""},
          {"checking",3,0,0,{PJ_PLH_CHECKING,0,0,0},"Pocket heard, checking...",""},
          {"good",21,1,0,{PJ_PLH_GOOD,0,72,0},"Pocket heard, data correct.",""},
          {"refused",252,0,12,{PJ_PLH_REJECTED,0,72,PJ_PLR_CRC},"Pocket heard, data correct,","check failed (bad CRC)."},
          {"notreq",4294967295u,4294967295u,4294967295u,{PJ_PLH_REJECTED,0,72,PJ_PLR_NOTREQ},"Pocket heard, data correct,","check failed (not a request)."},
          {"cut",12,0,0,{PJ_PLH_CUT,0,72,0},"Pocket heard, start correct,","message cut short."},
          {"shift",1260,0,0,{PJ_PLH_SHIFT,1,72,0},"Pocket heard, data scrambled","(shifted 1 bit late)."},
          {"shift-early",9999,0,9999,{PJ_PLH_SHIFT,-4,64,0},"Pocket heard, data scrambled","(shifted 4 bits early)."},
          {"inverted",21,0,0,{PJ_PLH_INVERTED,0,56,0},"Pocket heard, data scrambled","(every bit flipped)."},
          {"reversed",21,0,0,{PJ_PLH_REVERSED,0,72,0},"Pocket heard, data scrambled","(bit order reversed)."},
          {"slip",20,0,0,{PJ_PLH_SLIP,62,64,0},"Pocket heard, data scrambled","(lost a bit near bit 62)."},
          {"dup",21,0,0,{PJ_PLH_DUP,62,72,0},"Pocket heard, data scrambled","(extra bit near bit 62)."},
          {"zeros",9999999,0,0,{PJ_PLH_ZEROS,0,0,0},"Pocket line reads only zeros","(check the cable)."},
          {"unknown",123456,9999,10000,{PJ_PLH_UNKNOWN,0,255,0},"Pocket heard, data scrambled","(no pattern, 255 bits right)."},
         {"short",3,0,0,{PJ_PLH_SHORT,0,24,0},"Pocket heard, too few bytes","to judge."}}; /* 1.3.6 review: under four bytes */
      unsigned seen=0;uint16_t bg=pj_osd_rgb(PJ_OSD_BG);
      for(unsigned k=0;k<sizeof(cases)/sizeof(cases[0]);k++) {
          pj_plink_status s={PJ_PLV_WAITING,0,0,"",0,cases[k].heard,cases[k].good,cases[k].bad,cases[k].hint};char one[40],two[40],count[48];
          pj_pld_sentence(s.hint,one,sizeof(one),two,sizeof(two));
          CHECK(!strcmp(one,cases[k].a)&&!strcmp(two,cases[k].b));
          CHECK(pj_osd_measure(one,pj_font_advance)<=144&&pj_osd_measure(two,pj_font_advance)<=144);
          pj_plink_heard_words(&s,count,sizeof(count),pj_font_advance);
          CHECK(pj_osd_measure(count,pj_font_advance)<=144&&strstr(count," good, ")&&strstr(count," bad"));
          pj_plink_draw(screen,&s,1000,pj_font_alpha,pj_font_advance);
          for(int y=44;y<PJ_OSD_H;y++)for(int x=0;x<PJ_OSD_W;x++) { /* under the status row */
              int text=y>=47&&y<119&&x>=8&&x<152,button=y>=122&&y<142&&x>=12&&x<148;
              if(!text&&!button)CHECK(screen[y*PJ_OSD_W+x]==bg);
          }
          {int lit=0;for(int y=97;y<108;y++)for(int x=8;x<152;x++)lit|=screen[y*PJ_OSD_W+x]!=bg;CHECK(lit);} /* the sentence is there */
          seen|=1u<<cases[k].hint.what;
          snprintf(cpath,sizeof(cpath),"%.*sui-plink-waiting-%s.ppm",(int)(slash-argv[1]),argv[1],cases[k].name);write_ppm(cpath);
      }
      CHECK(seen==(1u<<PJ_PLH_COUNT)-1u); /* every finding has been drawn */
      for(unsigned reason=0;reason<PJ_PLR_COUNT;reason++) {
          pj_pld_hint h={PJ_PLH_REJECTED,0,0,(uint8_t)reason};char one[40],two[40];pj_pld_sentence(h,one,sizeof(one),two,sizeof(two));
          CHECK(two[0]&&pj_osd_measure(one,pj_font_advance)<=144&&pj_osd_measure(two,pj_font_advance)<=144);
      }
      for(int arg=-7;arg<=71;arg++)for(unsigned what=PJ_PLH_SHIFT;what<=PJ_PLH_UNKNOWN;what++) {
          pj_pld_hint h={(uint8_t)what,(int8_t)arg,255,0};char one[40],two[40];pj_pld_sentence(h,one,sizeof(one),two,sizeof(two));
          CHECK(strlen(one)<39&&strlen(two)<39&&pj_osd_measure(one,pj_font_advance)<=144&&pj_osd_measure(two,pj_font_advance)<=144);
      }
      /* the count line: plain numbers, then thousands and millions, and shorter words before it may not fit */
      {pj_plink_status s={PJ_PLV_WAITING,0,0,"",0,21,1,0,{PJ_PLH_GOOD,0,72,0}};char count[48];
       pj_plink_heard_words(&s,count,sizeof(count),pj_font_advance);CHECK(!strcmp(count,"Heard 21 bytes: 1 good, 0 bad"));
       s.heard=9999;s.good=10000;s.bad=123456;pj_plink_heard_words(&s,count,sizeof(count),pj_font_advance);CHECK(!strcmp(count,"Heard 9999: 10k good, 123k bad")); /* "bytes" gives way */
       s.heard=41472000;s.good=0;s.bad=7200;pj_plink_heard_words(&s,count,sizeof(count),pj_font_advance);CHECK(!strcmp(count,"Heard 41M bytes: 0 good, 7200 bad"));
       s.heard=s.good=s.bad=4294967295u;pj_plink_heard_words(&s,count,sizeof(count),pj_font_advance);CHECK(!strcmp(count,"4294M: 4294M good, 4294M bad"));
       /* a stopped link keeps the count above the button */
       s.what=PJ_PLV_STOPPED;s.why="link fault";s.heard=252;s.good=0;s.bad=12;
       pj_plink_draw(screen,&s,1000,pj_font_alpha,pj_font_advance);
       {int lit=0;for(int y=96;y<107;y++)for(int x=8;x<152;x++)lit|=screen[y*PJ_OSD_W+x]!=bg;CHECK(lit);}
       for(int y=107;y<111;y++)for(int x=0;x<PJ_OSD_W;x++)CHECK(screen[y*PJ_OSD_W+x]==bg);
       snprintf(cpath,sizeof(cpath),"%.*sui-plink-stopped-heard.ppm",(int)(slash-argv[1]),argv[1]);write_ppm(cpath);}
     }
     static const char *const lines135[]={"The fast connection failed:","browse and pictures only,","no playing on this link.",
         "Stage 1 of 5: entering.","Stage 2 of 5: fast UART.","Stage 3 of 5: console link.","Stage 4 of 5: cable.",
         "The console refused the link.","No reply from the console.","The console answered oddly.","The console's line was busy."};
     for(unsigned k=0;k<sizeof(lines135)/sizeof(lines135[0]);k++)CHECK(8+pj_osd_measure(lines135[k],pj_font_advance)<=156);
     static const char *const lines13[]={"1. Plug the link cable into","   both consoles.","2. Open Jellyfin on the Pocket.","The Pocket can browse your",
         "library and play from here.","Keep this screen open.","Copying pictures to the","Pocket for offline browsing.","Playing on the Pocket.",
         "Plug the cable back in.","The Pocket keeps what","it has saved.","Trying again in a moment.","B: back to Jellyfin","Opening Pocket link...",
         "Pocket link: not enough memory","Pocket link could not start"};
     for(unsigned k=0;k<sizeof(lines13)/sizeof(lines13[0]);k++)CHECK(8+pj_osd_measure(lines13[k],pj_font_advance)<=156);}
    puts("PASS: boot policy over all 256 reset reasons, damaged launcher, FPGA framing, the menu waits for a choice (no countdown, no idle limits), silent FPGA on every screen, "
         "Other firmware (stock only), chords, Jellyfin and menu-button exits, no cartridge, silent FPGA, timer wrap, "
         "text fits, a reason for every exit, safe diagnostics lines for every event; 1.3.0: only 18.41 with the debug bit runs the Pocket link (sound on 18.40 and 18.41), the Pocket link screen in six states with fitting words; 1.3.6: the waiting screen says what the link has heard, a fitting sentence for every finding, nothing drawn outside its area. Modeled FPGA, no hardware.");
    return 0;
}
