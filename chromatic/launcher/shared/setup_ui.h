/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * On-console setup (docs/CHROMATIC-LAUNCHER-1.2.md "Setup on the console, no PC"): Wi-Fi
 * networks and the Jellyfin sign-in, drawn in the launcher menu's own look. No hardware calls:
 * the screens ask for work through pj_setup_take_request() (scan, discover, HTTP, commit) and
 * the ESP32 binding (setup_esp32.c) answers with the *_done() calls; the host test plays the
 * binding. Presses arrive through pj_setup_press() from the launcher (pj_launcher.setup_pressed).
 *
 * Nothing here keeps a Wi-Fi password or token longer than the one edit it belongs to, and
 * nothing formats one into text other than the record itself. Saved networks are known by
 * name only. */
#ifndef PJ_SETUP_UI_H
#define PJ_SETUP_UI_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "launcher_logic.h"
#include "keyboard.h"
#include "wifi_scan.h"
#include "quickconnect.h"
#include "provision_write.h"
typedef enum {
    PJ_SU_ROOT,PJ_SU_WIFI,PJ_SU_WIFI_NET,PJ_SU_WIFI_FORGET,PJ_SU_SCAN,PJ_SU_PASSWORD,
    PJ_SU_JF,PJ_SU_JF_SERVERS,PJ_SU_JF_ADDRESS,PJ_SU_JF_CODE,PJ_SU_JF_FORGET,
    PJ_SU_SWITCH, /* "Use this network now": saved, joining, connected / what went wrong (launcher 1.2.9) */
    PJ_SU_FIND,   /* "Find my server again" (launcher 1.3.0): the servers that answered, Look again, Enter the address */
    PJ_SU_FIND_CONFIRM, /* 1.3.1: the picked server's own name and version: "Yes, this is my server" before any token */
    PJ_SU_BUSY,PJ_SU_MESSAGE,PJ_SU_CLOSED
} pj_setup_screen;
enum {PJ_SU_REQ_NONE,PJ_SU_REQ_SCAN,PJ_SU_REQ_DISCOVER,PJ_SU_REQ_HTTP,PJ_SU_REQ_COMMIT};
/* What the shell gave us about the console's sign-in, for the one Logout this console may send. */
typedef struct {char url[129],user_id[33],device_id[65],token[65];} pj_setup_account;
typedef struct {
    uint8_t screen,after,request,busy,wifi_connected,jellyfin_linked;
    uint8_t wifi_changed,jellyfin_changed; /* for the shell: the record changed while open */
    unsigned focus,scroll;unsigned net_count;char ssid[PJ_PROV2_NETWORKS][33];
    unsigned chosen; /* saved network index (WIFI_NET) or scan row (PASSWORD) */
    char busy_line[40],message[3][40];
    pj_keyboard kb;pj_scan_list scan;pj_jf_servers servers;pj_quickconnect qc;pj_qc_http http;
    pj_prov_edit edit;int commit_rc;
    pj_setup_account account,fresh; /* fresh: the new sign-in, for the shell to take once */
    uint8_t has_fresh,logout_pending,commit_after_logout;
    char version[12];
    /* The network the shell has joined (index into ssid[], -1 none): the list marks it "connected". */
    int8_t connected;
    /* "Use this network now": the shell takes switch_request once (pj_setup_take_switch) and rejoins at once;
     * it reports the join back through pj_setup_join_state. switch_state: 0 none, 2 joining, 3 connected,
     * 4 failed (switch_fail: pj_join_words index). */
    uint8_t switch_request,switch_state,switch_fail,switch_saved;char switch_name[33]; /* switch_saved: a Save being tried, not a move to the top */
    /* "Find my server again" (launcher 1.3.0, docs/Design-notes.md): find_discover routes the next
     * discovery answer to the Find screen; find_verify: the one check (pj_qc_verify_begin) is out; find_from_app: the
     * Jellyfin app's error screen opened us, so B and the end go straight back to Jellyfin. */
    uint8_t find_discover,find_verify,find_from_app;
    /* 1.3.1: what /System/Info/Public said about the picked server, shown for the owner to confirm (no token sent yet) */
    char find_url[129],find_name[32],find_version[16];
} pj_setup;
enum {PJ_SU_JOIN_IDLE,PJ_SU_JOIN_JOINING,PJ_SU_JOIN_CONNECTED,PJ_SU_JOIN_FAILED}; /* the shell's join state, for pj_setup_join_state */
static inline void pj_setup_open(pj_setup *s,const char *const *names,unsigned count,int jellyfin_linked,
                                 const pj_setup_account *account,int wifi_connected,const char *version) {
    memset(s,0,sizeof(*s));
    for(unsigned i=0;i<count&&i<PJ_PROV2_NETWORKS;i++)snprintf(s->ssid[s->net_count++],33,"%s",names[i]);
    s->jellyfin_linked=(uint8_t)(jellyfin_linked!=0);s->wifi_connected=(uint8_t)(wifi_connected!=0);
    if(account)s->account=*account;
    snprintf(s->version,sizeof(s->version),"%s",version?version:"1.2");
    s->connected=-1;
    s->screen=PJ_SU_ROOT;
}
/* The shell names the network it is on (NULL: none); known by name only, marked in the list. */
static inline void pj_setup_set_connected(pj_setup *s,const char *ssid) {
    s->connected=-1;
    if(ssid)for(unsigned i=0;i<s->net_count;i++)if(!strcmp(s->ssid[i],ssid))s->connected=(int8_t)i;
}
static inline int pj_setup_take_switch(pj_setup *s){int r=s->switch_request;s->switch_request=0;return r;}
/* The shell's join, every pass: the Switching screen follows it (pj_join_words for a failure). */
static inline void pj_setup_join_state(pj_setup *s,int state,int fail) {
    if(s->screen!=PJ_SU_SWITCH||s->switch_state!=2)return;
    if(state==PJ_SU_JOIN_CONNECTED)s->switch_state=3; /* which network: the binding names it (pj_setup_set_connected) */
    else if(state==PJ_SU_JOIN_FAILED){s->switch_state=4;s->switch_fail=(uint8_t)(fail>=0&&fail<=PJ_JOIN_FAILED?fail:PJ_JOIN_FAILED);}
}
static inline void pj_setup_close(pj_setup *s) {
    pj_qc_wipe(&s->qc);memset(&s->edit,0,sizeof(s->edit));memset(&s->kb,0,sizeof(s->kb));
    memset(&s->account,0,sizeof(s->account));if(!s->has_fresh)memset(&s->fresh,0,sizeof(s->fresh));
    s->screen=PJ_SU_CLOSED;
}
static inline int pj_setup_take_request(pj_setup *s){int r=s->request;s->request=PJ_SU_REQ_NONE;return r;}
/* The new sign-in after a successful Quick Connect, once; wiped after. 1 when there was one. */
static inline int pj_setup_take_fresh(pj_setup *s,pj_setup_account *out) {
    if(!s->has_fresh)return 0;
    *out=s->fresh;memset(&s->fresh,0,sizeof(s->fresh));s->has_fresh=0;return 1;
}
static inline void pj_setup_go(pj_setup *s,pj_setup_screen screen){s->screen=(uint8_t)screen;s->focus=0;s->scroll=0;}
static inline void pj_setup_busy(pj_setup *s,const char *line,int request) {
    snprintf(s->busy_line,sizeof(s->busy_line),"%s",line);s->busy=1;s->request=(uint8_t)request;pj_setup_go(s,PJ_SU_BUSY);
}
static inline void pj_setup_say(pj_setup *s,pj_setup_screen after,const char *l0,const char *l1,const char *l2) {
    snprintf(s->message[0],40,"%s",l0?l0:"");snprintf(s->message[1],40,"%s",l1?l1:"");snprintf(s->message[2],40,"%s",l2?l2:"");
    s->after=(uint8_t)after;s->busy=0;pj_setup_go(s,PJ_SU_MESSAGE);
}
static inline void pj_setup_commit(pj_setup *s,const char *line){pj_setup_busy(s,line,PJ_SU_REQ_COMMIT);}
static inline unsigned pj_setup_rows(const pj_setup *s) {
    switch(s->screen) {
    case PJ_SU_ROOT:return 2;
    case PJ_SU_WIFI:return s->net_count+(s->net_count<PJ_PROV2_NETWORKS?1u:0u);
    case PJ_SU_WIFI_NET:return 3;
    case PJ_SU_WIFI_FORGET:case PJ_SU_JF_FORGET:return 2;
    case PJ_SU_SCAN:return s->scan.count+1u;
    case PJ_SU_JF:return s->jellyfin_linked?3u:2u;
    case PJ_SU_JF_SERVERS:return s->servers.count+1u;
    case PJ_SU_FIND:return s->servers.count+2u; /* the servers, Look again, Enter the address */
    case PJ_SU_FIND_CONFIRM:return 2u;            /* Yes, this is my server / No, look again */
    default:return 0;
    }
}
#define PJ_SU_VISIBLE 5u
static inline void pj_setup_move(pj_setup *s,int down) {
    unsigned rows=pj_setup_rows(s);if(!rows)return;
    s->focus=down?(s->focus+1u)%rows:(s->focus+rows-1u)%rows;
    if(s->focus<s->scroll)s->scroll=s->focus;
    if(s->focus>=s->scroll+PJ_SU_VISIBLE)s->scroll=s->focus-PJ_SU_VISIBLE+1u;
}
/* Quick Connect against `url`: the code screen until the owner approves. */
static inline void pj_setup_start_quickconnect(pj_setup *s,const char *url,uint32_t now,const uint8_t random12[12]) {
    char device[65];pj_qc_device_id(device,random12);
    pj_qc_begin(&s->qc,url,device,now);
    memset(device,0,sizeof(device));
    pj_setup_go(s,PJ_SU_JF_CODE);
}
/* "Find my server again": look on this network for the server this console signed in to. Only with a saved
 * sign-in (the token is never shown, and only goes to the one server the owner picks) and Wi-Fi up. */
static inline void pj_setup_find_look(pj_setup *s) {
    memset(&s->servers,0,sizeof(s->servers));s->find_discover=1;pj_setup_busy(s,"Looking for your server...",PJ_SU_REQ_DISCOVER);
}
static inline void pj_setup_find_begin(pj_setup *s,int from_app) {
    s->find_from_app=(uint8_t)(from_app!=0);
    pj_setup_screen back=from_app?PJ_SU_CLOSED:PJ_SU_JF;
    if(!s->jellyfin_linked||!s->account.token[0]||!s->account.user_id[0]){pj_setup_say(s,PJ_SU_JF,"No Jellyfin sign-in is","saved on this console.","Sign in here instead.");return;}
    if(!s->wifi_connected){pj_setup_say(s,back,"Wi-Fi is not connected.","Join the network your","server is on, then try again.");return;}
    pj_setup_find_look(s);
}
/* The owner picked `url` (expect_id: what discovery heard from it, "" when typed). Step 1 (1.3.1, review D): ask it who
 * it is without a token (/System/Info/Public: its Id must match the discovery reply's); the owner sees its own name and
 * version and confirms. Step 2: only then one GET of this console's own user with the token (pj_qc_verify_begin). */
static inline void pj_setup_find_check(pj_setup *s,const char *url,const char *expect_id) {
    if(!strcmp(url,s->account.url)){pj_setup_say(s,PJ_SU_FIND,"That address is the one","saved. The server may be off","or busy: try Jellyfin again.");return;}
    pj_qc_info_begin(&s->qc,url,s->account.device_id,expect_id);
    s->find_verify=1;snprintf(s->busy_line,sizeof(s->busy_line),"Asking that server...");s->busy=1;pj_setup_go(s,PJ_SU_BUSY);
}
static inline void pj_setup_find_confirmed(pj_setup *s) {
    pj_qc_verify_begin(&s->qc,s->find_url,s->account.device_id,s->account.token,s->account.user_id);
    s->find_verify=1;snprintf(s->busy_line,sizeof(s->busy_line),"Checking it is yours...");s->busy=1;pj_setup_go(s,PJ_SU_BUSY);
}
static inline void pj_setup_find_leave(pj_setup *s){if(s->find_from_app)pj_setup_close(s);else pj_setup_go(s,PJ_SU_JF);}
/* The shell supplies 12 random bytes for a fresh DeviceId (esp_random); the host test a fixed pattern. */
typedef void (*pj_setup_random_fn)(uint8_t out[12]);
static inline void pj_setup_press(pj_setup *s,uint16_t pressed,uint32_t now,pj_setup_random_fn random12) {
    if(!pressed||s->screen==PJ_SU_CLOSED)return;
    uint8_t rnd[12];
    switch((pj_setup_screen)s->screen) {
    case PJ_SU_ROOT:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_close(s);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START))pj_setup_go(s,s->focus==0?PJ_SU_WIFI:PJ_SU_JF);
        else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_WIFI:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_ROOT);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus<s->net_count){s->chosen=s->focus;pj_setup_go(s,PJ_SU_WIFI_NET);}
            else{pj_scan_reset(&s->scan);pj_setup_busy(s,"Scanning...",PJ_SU_REQ_SCAN);}
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_WIFI_NET:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_WIFI);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus==0) { /* Use this network now: to the top of the list (one commit), then the shell rejoins */
                if(s->connected==(int8_t)s->chosen){pj_setup_say(s,PJ_SU_WIFI_NET,"Already on this network.",NULL,NULL);break;}
                memset(&s->edit,0,sizeof(s->edit));s->edit.net_op=PJ_PROV_NET_FIRST;
                snprintf(s->edit.net.ssid,33,"%s",s->ssid[s->chosen]);snprintf(s->switch_name,33,"%s",s->ssid[s->chosen]);
                pj_setup_commit(s,"Switching...");
            } else if(s->focus==1) {
                char title[28];snprintf(title,sizeof(title),"Password: %.16s",s->ssid[s->chosen]);
                pj_kb_open(&s->kb,title,"",64,8,1);s->after=PJ_SU_WIFI_NET;pj_setup_go(s,PJ_SU_PASSWORD);
            } else pj_setup_go(s,PJ_SU_WIFI_FORGET);
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_SWITCH:
        /* Joining: B leaves the screen, the join goes on (the Wi-Fi list shows the result). Done: A or B back to the
         * list; a wrong password offers the keyboard for that network (now first in the list) with A. */
        if(s->switch_state==2){if(pressed&(PJ_BTN_B|PJ_BTN_MENU)){s->switch_state=0;pj_setup_go(s,PJ_SU_WIFI);}break;}
        if(s->switch_state==4&&s->switch_fail==PJ_JOIN_WRONG_PASSWORD&&(pressed&(PJ_BTN_A|PJ_BTN_START))) {
            char title[28];s->chosen=0;for(unsigned i=0;i<s->net_count;i++)if(!strcmp(s->ssid[i],s->switch_name))s->chosen=i;
            snprintf(title,sizeof(title),"Password: %.16s",s->ssid[s->chosen]);
            pj_kb_open(&s->kb,title,"",64,8,1);s->after=PJ_SU_WIFI_NET;s->switch_state=0;pj_setup_go(s,PJ_SU_PASSWORD);break;
        }
        if(pressed&(PJ_BTN_A|PJ_BTN_B|PJ_BTN_START|PJ_BTN_MENU)){s->switch_state=0;pj_setup_go(s,PJ_SU_WIFI);}
        break;
    case PJ_SU_WIFI_FORGET:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_WIFI_NET);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus==0) {
                memset(&s->edit,0,sizeof(s->edit));s->edit.net_op=PJ_PROV_NET_FORGET;
                snprintf(s->edit.net.ssid,33,"%s",s->ssid[s->chosen]);
                pj_setup_commit(s,"Saving...");
            } else pj_setup_go(s,PJ_SU_WIFI_NET);
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_SCAN:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_WIFI);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus>=s->scan.count){pj_scan_reset(&s->scan);pj_setup_busy(s,"Scanning...",PJ_SU_REQ_SCAN);break;}
            const pj_scan_entry *e=&s->scan.net[s->focus];
            if(!e->saved&&s->net_count>=PJ_PROV2_NETWORKS){pj_setup_say(s,PJ_SU_SCAN,"Four networks are saved.","Forget one to add another.",NULL);break;}
            s->chosen=s->focus;
            if(e->secured) {
                char title[28];snprintf(title,sizeof(title),"Password: %.16s",e->ssid);
                pj_kb_open(&s->kb,title,"",64,8,1);s->after=PJ_SU_SCAN;pj_setup_go(s,PJ_SU_PASSWORD);
            } else {
                memset(&s->edit,0,sizeof(s->edit));s->edit.net_op=PJ_PROV_NET_SAVE;
                snprintf(s->edit.net.ssid,33,"%s",e->ssid);pj_setup_commit(s,"Saving...");
            }
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_PASSWORD: {
        int r=pj_kb_press(&s->kb,pressed);
        if(r==PJ_KB_CANCEL){memset(&s->kb,0,sizeof(s->kb));pj_setup_go(s,s->after==PJ_SU_WIFI_NET?PJ_SU_WIFI_NET:PJ_SU_SCAN);}
        else if(r==PJ_KB_DONE) {
            memset(&s->edit,0,sizeof(s->edit));s->edit.net_op=PJ_PROV_NET_SAVE;
            const char *name=s->after==PJ_SU_WIFI_NET?s->ssid[s->chosen]:s->scan.net[s->chosen].ssid;
            snprintf(s->edit.net.ssid,33,"%s",name);memcpy(s->edit.net.psk,s->kb.text,64);s->edit.net.psk[64]=0;
            memset(&s->kb,0,sizeof(s->kb));pj_setup_commit(s,"Saving...");
        }
        break;
    }
    case PJ_SU_JF:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_ROOT);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->jellyfin_linked&&s->focus==1){pj_setup_go(s,PJ_SU_JF_FORGET);break;}
            if(s->jellyfin_linked&&s->focus==2){pj_setup_find_begin(s,0);break;} /* Find my server again (1.3.0) */
            if(!s->wifi_connected){pj_setup_say(s,PJ_SU_JF,"Wi-Fi is not connected.","Open Jellyfin once to join,","then come back here.");break;}
            if(s->focus==0){memset(&s->servers,0,sizeof(s->servers));pj_setup_busy(s,"Looking for servers...",PJ_SU_REQ_DISCOVER);}
            else{pj_kb_open(&s->kb,"Server address","http://",128,8,0);pj_setup_go(s,PJ_SU_JF_ADDRESS);}
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_JF_SERVERS:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_JF);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus>=s->servers.count){pj_kb_open(&s->kb,"Server address","http://",128,8,0);pj_setup_go(s,PJ_SU_JF_ADDRESS);break;}
            random12(rnd);pj_setup_start_quickconnect(s,s->servers.server[s->focus].url,now,rnd);
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_JF_ADDRESS: {
        int r=pj_kb_press(&s->kb,pressed),find=s->after==PJ_SU_FIND; /* typed from Find my server again: checked, not signed in */
        if(r==PJ_KB_CANCEL)pj_setup_go(s,find?PJ_SU_FIND:PJ_SU_JF);
        else if(r==PJ_KB_DONE) {
            char url[129];snprintf(url,sizeof(url),"%s",s->kb.text);
            if(!pj_qc_normalize_url(url,sizeof(url)))pj_setup_say(s,find?PJ_SU_FIND:PJ_SU_JF,"That is not a server address.","Example: http://192.0.2.20:8096",NULL);
            else if(find){memset(&s->kb,0,sizeof(s->kb));pj_setup_find_check(s,url,"");}
            else{random12(rnd);pj_setup_start_quickconnect(s,url,now,rnd);}
        }
        break;
    }
    case PJ_SU_FIND:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_find_leave(s);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus<s->servers.count)pj_setup_find_check(s,s->servers.server[s->focus].url,s->servers.server[s->focus].id);
            else if(s->focus==s->servers.count)pj_setup_find_look(s);
            else{pj_kb_open(&s->kb,"Server address","http://",128,8,0);s->after=PJ_SU_FIND;pj_setup_go(s,PJ_SU_JF_ADDRESS);}
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_FIND_CONFIRM: /* the owner says whether the name and version shown are their server */
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU)){memset(&s->qc,0,sizeof(s->qc));pj_setup_go(s,PJ_SU_FIND);}
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus==0)pj_setup_find_confirmed(s);
            else{memset(&s->qc,0,sizeof(s->qc));pj_setup_go(s,PJ_SU_FIND);}
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_JF_CODE:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU)){pj_qc_wipe(&s->qc);memset(&s->qc,0,sizeof(s->qc));pj_setup_go(s,PJ_SU_JF);}
        break;
    case PJ_SU_JF_FORGET:
        if(pressed&(PJ_BTN_B|PJ_BTN_MENU))pj_setup_go(s,PJ_SU_JF);
        else if(pressed&(PJ_BTN_A|PJ_BTN_START)) {
            if(s->focus!=0){pj_setup_go(s,PJ_SU_JF);break;}
            memset(&s->edit,0,sizeof(s->edit));s->edit.clear_jellyfin=1;
            if(s->account.token[0]&&s->wifi_connected) { /* log out this console's own session first */
                pj_qc_logout_begin(&s->qc,s->account.url,s->account.device_id,s->account.token);
                s->logout_pending=1;s->commit_after_logout=1;
                snprintf(s->busy_line,sizeof(s->busy_line),"Signing out...");s->busy=1;pj_setup_go(s,PJ_SU_BUSY);
            } else pj_setup_commit(s,"Saving...");
        } else if(pressed&(PJ_BTN_UP|PJ_BTN_DOWN))pj_setup_move(s,pressed&PJ_BTN_DOWN);
        break;
    case PJ_SU_MESSAGE:
        if(pressed&(PJ_BTN_A|PJ_BTN_B|PJ_BTN_START|PJ_BTN_MENU)) {
            if(s->after==PJ_SU_CLOSED)pj_setup_close(s); /* back to Jellyfin (Find my server again) */
            else pj_setup_go(s,(pj_setup_screen)s->after);
        }
        break;
    case PJ_SU_BUSY: /* nothing cancels a write; scans and lookups end on their own */
    default:break;
    }
    memset(rnd,0,sizeof(rnd));
}
/* Screens that animate (a repaint four times a second is enough). */
static inline int pj_setup_animates(const pj_setup *s){return s->screen==PJ_SU_BUSY||s->screen==PJ_SU_JF_CODE||(s->screen==PJ_SU_SWITCH&&s->switch_state==2);}
/* Timers and the Quick Connect poll: call every loop. May set a request. */
static inline void pj_setup_tick(pj_setup *s,uint32_t now) {
    if(s->screen==PJ_SU_CLOSED||s->request!=PJ_SU_REQ_NONE)return;
    if(s->find_verify&&!s->qc.pending) { /* Find my server again: who it is, the owner's yes, the one check, the commit */
        if(s->qc.state==PJ_QC_INFO_OK) { /* no token has left the console: the owner confirms what the server says it is */
            s->find_verify=0;snprintf(s->find_url,sizeof(s->find_url),"%s",s->qc.url);
            snprintf(s->find_name,sizeof(s->find_name),"%s",s->qc.server_name);snprintf(s->find_version,sizeof(s->find_version),"%s",s->qc.server_version);
            memset(&s->qc,0,sizeof(s->qc));pj_setup_go(s,PJ_SU_FIND_CONFIRM);return;
        }
        if(s->qc.state==PJ_QC_VERIFIED) {
            s->find_verify=0;memset(&s->edit,0,sizeof(s->edit));s->edit.set_address=1;
            memcpy(s->edit.url,s->qc.url,sizeof(s->edit.url));memcpy(s->edit.user_id,s->account.user_id,sizeof(s->edit.user_id));
            memset(&s->qc,0,sizeof(s->qc));pj_setup_commit(s,"Saving the new address...");return;
        }
        if(s->qc.state==PJ_QC_FAILED) {
            int why=s->qc.fail;s->find_verify=0;memset(&s->qc,0,sizeof(s->qc));
            if(why==PJ_QC_FAIL_NOT_OURS)pj_setup_say(s,PJ_SU_FIND,"That server does not know","this console's sign-in.","Pick another, or sign in again.");
            else if(why==PJ_QC_FAIL_NOT_SAME)pj_setup_say(s,PJ_SU_FIND,"That address answers as","another server. Nothing was","sent to it. Look again.");
            else if(why==PJ_QC_FAIL_UNREACHABLE)pj_setup_say(s,PJ_SU_FIND,"That server did not answer.","Check it is switched on,","then look again.");
            else pj_setup_say(s,PJ_SU_FIND,"That server gave an","unexpected reply.","Pick another one.");
            return;
        }
        if(pj_qc_request(&s->qc,now,&s->http))s->request=PJ_SU_REQ_HTTP;
        return;
    }
    if((s->screen==PJ_SU_JF_CODE||s->logout_pending)&&!s->qc.pending) {
        if(s->qc.state==PJ_QC_FAILED){pj_setup_say(s,PJ_SU_JF,pj_qc_fail_words[s->qc.fail],"",NULL);memset(&s->qc,0,sizeof(s->qc));return;}
        if(s->qc.state==PJ_QC_DONE) {
            memset(&s->edit,0,sizeof(s->edit));s->edit.set_jellyfin=1;
            memcpy(s->edit.url,s->qc.url,sizeof(s->edit.url));memcpy(s->edit.user_id,s->qc.user_id,sizeof(s->edit.user_id));
            memcpy(s->edit.access_token,s->qc.token,sizeof(s->edit.access_token));memcpy(s->edit.device_id,s->qc.device_id,sizeof(s->edit.device_id));
            pj_qc_wipe(&s->qc);pj_setup_commit(s,"Saving...");return;
        }
        if(s->qc.state==PJ_QC_LOGGED_OUT) {
            s->logout_pending=0;memset(&s->qc,0,sizeof(s->qc)); /* the account: replaced on re-link, cleared by the forget commit */
            if(s->commit_after_logout){s->commit_after_logout=0;pj_setup_commit(s,"Saving...");}
            else pj_setup_say(s,PJ_SU_JF,"Signed in.","Jellyfin is ready.",NULL);
            return;
        }
        if(pj_qc_request(&s->qc,now,&s->http)){s->request=PJ_SU_REQ_HTTP;return;}
        if(s->qc.state==PJ_QC_FAILED)pj_setup_tick(s,now); /* the deadline passed inside pj_qc_request */
    }
}
/* ---- answers from the binding ---- */
static inline void pj_setup_scan_done(pj_setup *s,int ok) {
    s->busy=0;
    if(!ok){pj_setup_say(s,PJ_SU_WIFI,"The scan did not work.","Try again in a moment.",NULL);return;}
    const char *names[PJ_PROV2_NETWORKS];for(unsigned i=0;i<s->net_count;i++)names[i]=s->ssid[i];
    pj_scan_mark_saved(&s->scan,names,s->net_count);
    pj_setup_go(s,PJ_SU_SCAN);
}
static inline void pj_setup_discover_done(pj_setup *s) {
    s->busy=0;
    if(s->find_discover){s->find_discover=0;pj_setup_go(s,PJ_SU_FIND);return;} /* none found: the Find screen says so, with next steps */
    if(!s->servers.count)pj_setup_say(s,PJ_SU_JF,"No Jellyfin server answered","on this network.","Try the address instead.");
    else pj_setup_go(s,PJ_SU_JF_SERVERS);
}
static inline void pj_setup_http_done(pj_setup *s,int status,const void *body,size_t len,uint32_t now) {
    pj_qc_response(&s->qc,status,body,len,now);memset(&s->http,0,sizeof(s->http));
}
static inline void pj_setup_commit_done(pj_setup *s,int rc) {
    s->commit_rc=rc;s->busy=0;
    pj_prov_edit *e=&s->edit;
    if(rc==PJ_PROV_WRITE_OK||rc==PJ_PROV_WRITE_NO_CHANGE) {
        if(e->net_op==PJ_PROV_NET_SAVE) {
            unsigned at=s->net_count;for(unsigned i=0;i<s->net_count;i++)if(!strcmp(s->ssid[i],e->net.ssid))at=i;
            if(at==s->net_count&&s->net_count<PJ_PROV2_NETWORKS)s->net_count++;
            if(at<PJ_PROV2_NETWORKS)snprintf(s->ssid[at],33,"%s",e->net.ssid);
            s->wifi_changed=1;
            if(s->wifi_connected)pj_setup_say(s,PJ_SU_WIFI,"Saved.","To switch to it now: open it,","then Use this network now.");
            else { /* not on any network: try it now and show the result (a saved network is always tried) */
                snprintf(s->switch_name,33,"%s",e->net.ssid);s->switch_request=1;s->switch_state=2;s->switch_saved=1;s->connected=-1;pj_setup_go(s,PJ_SU_SWITCH);
            }
        } else if(e->net_op==PJ_PROV_NET_FIRST) {
            /* The list is the join priority: that network goes first here too, then the shell rejoins now. */
            for(unsigned i=0;i<s->net_count;i++)if(!strcmp(s->ssid[i],e->net.ssid)) {
                char first[33];memcpy(first,s->ssid[i],33);
                for(unsigned k=i;k>0;k--)memcpy(s->ssid[k],s->ssid[k-1],33);
                memcpy(s->ssid[0],first,33);
                if(s->connected==(int8_t)i)s->connected=0;else if(s->connected>=0&&s->connected<(int8_t)i)s->connected++;
                break;
            }
            s->wifi_changed=1;s->switch_request=1;s->switch_state=2;s->switch_saved=0;s->connected=-1;pj_setup_go(s,PJ_SU_SWITCH);
        } else if(e->net_op==PJ_PROV_NET_FORGET) {
            for(unsigned i=0;i<s->net_count;i++)if(!strcmp(s->ssid[i],e->net.ssid)) {
                for(unsigned k=i;k+1<s->net_count;k++)memcpy(s->ssid[k],s->ssid[k+1],33);
                memset(s->ssid[--s->net_count],0,33);
                if(s->connected==(int8_t)i)s->connected=-1;else if(s->connected>(int8_t)i)s->connected--;
                break;
            }
            s->wifi_changed=1;pj_setup_say(s,PJ_SU_WIFI,"Forgotten.",NULL,NULL);
        } else if(e->set_jellyfin) {
            s->jellyfin_linked=1;s->jellyfin_changed=1;
            snprintf(s->fresh.url,sizeof(s->fresh.url),"%s",e->url);snprintf(s->fresh.device_id,sizeof(s->fresh.device_id),"%s",e->device_id);
            snprintf(s->fresh.user_id,sizeof(s->fresh.user_id),"%s",e->user_id);
            snprintf(s->fresh.token,sizeof(s->fresh.token),"%s",e->access_token);s->has_fresh=1;
            if(s->account.token[0]&&s->wifi_connected&&strcmp(s->account.token,e->access_token)) {
                /* Re-link: the previous console session goes too (only that one). */
                pj_qc_logout_begin(&s->qc,s->account.url,s->account.device_id,s->account.token);
                s->logout_pending=1;s->commit_after_logout=0;
                snprintf(s->busy_line,sizeof(s->busy_line),"Signing in...");s->busy=1;pj_setup_go(s,PJ_SU_BUSY);
            } else pj_setup_say(s,PJ_SU_JF,"Signed in.","Jellyfin is ready.",NULL);
            s->account=s->fresh;
        } else if(e->set_address) { /* the same sign-in at its new address: the shell restarts Jellyfin with it */
            snprintf(s->account.url,sizeof(s->account.url),"%s",e->url);s->fresh=s->account;s->has_fresh=1;s->jellyfin_changed=1;
            pj_setup_say(s,s->find_from_app?PJ_SU_CLOSED:PJ_SU_JF,"Found your server.","Its new address is saved.",s->find_from_app?"Next: Jellyfin opens again.":"Your sign-in is unchanged.");
        } else if(e->clear_jellyfin) {
            s->jellyfin_linked=0;s->jellyfin_changed=1;memset(&s->account,0,sizeof(s->account));
            pj_setup_say(s,PJ_SU_JF,"Signed out on this console.","Your other devices stay","signed in.");
        } else pj_setup_say(s,PJ_SU_ROOT,"Nothing to save.",NULL,NULL);
    } else {
        const char *why=rc==PJ_PROV_WRITE_INVALID?"That cannot be saved.":rc==PJ_PROV_WRITE_NO_MEMORY?"Not enough memory to save.":"Saving did not work.";
        pj_setup_screen back=e->set_address?PJ_SU_FIND:e->set_jellyfin||e->clear_jellyfin?PJ_SU_JF:e->net_op==PJ_PROV_NET_FIRST?PJ_SU_WIFI_NET:PJ_SU_WIFI;
        pj_setup_say(s,back,why,rc==PJ_PROV_WRITE_INVALID?"":"The old settings are kept.","Try again or use Chromatic Manager.");
    }
    memset(e,0,sizeof(*e));
}
/* ---- drawing, in the launcher menu's look ---- */
#define PJ_SU_ROW_Y 22
#define PJ_SU_ROW_H 16
#define PJ_SU_ROW_PITCH 18
static inline void pj_su_row(uint16_t *fb,int y,const char *label,int focused,unsigned accent,int dim,
                             const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_rect(fb,8,y,144,PJ_SU_ROW_H,focused?accent:PJ_UI_EDGE);
    pj_osd_rect(fb,9,y+1,142,PJ_SU_ROW_H-2,focused?pj_osd_mix(accent,0x101016u,56u):PJ_UI_ROW);
    pj_osd_text(fb,30,y+3,label,dim?PJ_UI_MUTED:focused?accent:0xffffffu,alpha,advance);
    if(focused)pj_osd_rect(fb,3,y+4,3,8,accent);
}
static inline void pj_su_bars(uint16_t *fb,int x,int y,unsigned bars,unsigned accent) {
    for(unsigned i=0;i<4;i++)pj_osd_rect(fb,x+(int)i*3,y+8-(int)(i+1)*2,2,(int)(i+1)*2,i<bars?accent:0x3a3a48u);
}
static inline void pj_su_header(uint16_t *fb,const char *title,unsigned accent,const unsigned char *alpha,const unsigned char *advance) {
    pj_osd_fill(fb,PJ_OSD_BG);pj_launch_tabbar(fb);
    pj_icon_jellyfin(fb,6,3,0);pj_osd_text(fb,24,4,title,accent,alpha,advance);
}
/* A name cut to fit a row: at most `width` pixels, with ".." when cut. */
static inline void pj_su_fit(char *out,size_t size,const char *name,int width,const unsigned char *advance) {
    snprintf(out,size,"%s",name);
    if(pj_osd_measure(out,advance)<=width)return;
    size_t n=strlen(out);
    while(n>1&&pj_osd_measure(out,advance)>width){out[--n]=0;if(n>=2){out[n-1]='.';out[n-2]='.';}}
}
/* Big digits for the Quick Connect code: the normal glyphs at twice the size. */
static inline void pj_su_text2x(uint16_t *fb,int x,int y,const char *text,unsigned rgb,
                                const unsigned char *alpha,const unsigned char *advance) {
    for(unsigned i=0;text[i];i++) {
        unsigned c=(unsigned char)text[i];if(c<32||c>126)c='?';unsigned glyph=c-32;
        for(int row=0;row<11;row++)for(int col=0;col<13;col++) {
            unsigned a=alpha[glyph*143u+(unsigned)row*13u+(unsigned)col];if(!a)continue;
            for(int dy=0;dy<2;dy++)for(int dx=0;dx<2;dx++) {
                int px=x+col*2+dx,py=y+row*2+dy;
                if(px<0||px>=PJ_OSD_W||py<0||py>=PJ_OSD_H)continue;
                fb[py*PJ_OSD_W+px]=pj_osd_rgb(pj_osd_mix(rgb,pj_osd_unpack(fb[py*PJ_OSD_W+px]),a));
            }
        }
        x+=advance[glyph]*2;
    }
}
static inline void pj_setup_draw(uint16_t *fb,const pj_setup *s,int watched,uint32_t now,
                                 const unsigned char *alpha,const unsigned char *advance) {
    const unsigned accent=PJ_UI_JELLYFIN;char line[48];
    switch((pj_setup_screen)s->screen) {
    case PJ_SU_ROOT:
        pj_su_header(fb,"Setup",accent,alpha,advance);
        pj_su_row(fb,PJ_SU_ROW_Y,"Wi-Fi networks",s->focus==0,accent,0,alpha,advance);
        pj_su_row(fb,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH,"Jellyfin server",s->focus==1,accent,0,alpha,advance);
        pj_su_bars(fb,13,PJ_SU_ROW_Y+4,4,s->focus==0?accent:PJ_UI_MUTED);pj_icon_jellyfin(fb,12,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH+2,0);
        snprintf(line,sizeof(line),"%u network%s saved",s->net_count,s->net_count==1?"":"s");
        pj_osd_text(fb,8,66,s->focus==0?line:s->jellyfin_linked?"Signed in on this console":"Not signed in yet",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,132,"A: open    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    case PJ_SU_WIFI: {
        pj_su_header(fb,"Wi-Fi",accent,alpha,advance);
        unsigned rows=pj_setup_rows(s);
        for(unsigned i=s->scroll;i<rows&&i<s->scroll+PJ_SU_VISIBLE;i++) {
            int y=PJ_SU_ROW_Y+(int)(i-s->scroll)*PJ_SU_ROW_PITCH;
            if(i<s->net_count) {
                int on=s->connected==(int8_t)i,tw=on?pj_osd_measure("connected",advance):0;
                pj_su_fit(line,sizeof(line),s->ssid[i],on?116-tw:112,advance);pj_su_row(fb,y,line,s->focus==i,accent,0,alpha,advance);
                pj_icon_lock(fb,12,y+2,s->focus==i?accent:PJ_UI_MUTED);
                if(on)pj_osd_text(fb,150-tw,y+3,"connected",PJ_UI_SAFE,alpha,advance); /* the one the console is on, in green */
            }
            else{pj_su_row(fb,y,"Add a network...",s->focus==i,accent,0,alpha,advance);pj_osd_text(fb,15,y+3,"+",s->focus==i?accent:PJ_UI_MUTED,alpha,advance);}
        }
        if(!rows)pj_osd_text(fb,8,PJ_SU_ROW_Y+4,"No network saved yet",PJ_UI_MUTED,alpha,advance);
        if(s->net_count>=PJ_PROV2_NETWORKS)pj_osd_text(fb,8,100,"Four saved: forget one to add",PJ_UI_NOTE,alpha,advance);
        else if(s->net_count>1)pj_osd_text(fb,8,100,"Top of the list joins first",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,115,"Hotel sign-in Wi-Fi: use a hotspot",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,131,"A: open    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_WIFI_NET: {
        int on=s->connected==(int8_t)s->chosen;
        pj_su_fit(line,sizeof(line),s->ssid[s->chosen],128,advance);pj_su_header(fb,line,accent,alpha,advance);
        pj_su_row(fb,PJ_SU_ROW_Y,"Use this network now",s->focus==0,accent,on,alpha,advance);
        pj_su_row(fb,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH,"Change password",s->focus==1,accent,0,alpha,advance);
        pj_su_row(fb,PJ_SU_ROW_Y+2*PJ_SU_ROW_PITCH,"Forget this network",s->focus==2,accent,0,alpha,advance);
        pj_su_bars(fb,13,PJ_SU_ROW_Y+4,4,s->focus==0?accent:PJ_UI_MUTED);
        pj_icon_lock(fb,12,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH+2,s->focus==1?accent:PJ_UI_MUTED);
        pj_osd_text(fb,14,PJ_SU_ROW_Y+2*PJ_SU_ROW_PITCH+3,"x",s->focus==2?accent:PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,84,on?"Connected to this network now.":s->focus==0?"Switches right away, no restart;":"",on?PJ_UI_SAFE:PJ_UI_MUTED,alpha,advance);
        if(!on&&s->focus==0)pj_osd_text(fb,8,97,"it also joins first from now on.",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,132,"A: choose    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_SWITCH: {
        /* Every step in plain words with its colour: done (green), happening (yellow, animated), failed (red). */
        pj_su_header(fb,s->switch_saved?"Joining network":"Switching network",accent,alpha,advance);
        pj_su_fit(line,sizeof(line),s->switch_name,144,advance);pj_osd_text(fb,8,24,line,0xffffffu,alpha,advance);
        pj_osd_rect(fb,8,42,144,1,PJ_UI_EDGE);
        pj_osd_rect(fb,10,50,5,5,PJ_UI_SAFE);pj_osd_text(fb,22,47,s->switch_saved?"Saved":"Saved as first choice",PJ_UI_SAFE,alpha,advance);
        if(s->switch_state==2) {
            pj_osd_rect(fb,10,66,5,5,((now/250u)&1u)?PJ_UI_NOTE:PJ_UI_EDGE);
            snprintf(line,sizeof(line),"Joining%.*s",(int)((now/500u)%4u),"...");pj_osd_text(fb,22,63,line,PJ_UI_NOTE,alpha,advance);
            pj_osd_text(fb,8,100,"Usually 2 to 15 seconds.",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,132,"B: back (it keeps joining)",PJ_UI_MUTED,alpha,advance);
        } else if(s->switch_state==3) {
            pj_osd_rect(fb,10,66,5,5,PJ_UI_SAFE);pj_osd_text(fb,22,63,"Connected",PJ_UI_SAFE,alpha,advance);
            pj_osd_text(fb,8,100,"Jellyfin opens when you leave",PJ_UI_MUTED,alpha,advance);pj_osd_text(fb,8,113,"Setup.",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,132,"A or B: continue",PJ_UI_MUTED,alpha,advance);
        } else {
            pj_osd_rect(fb,10,66,5,5,PJ_UI_OTHER);pj_osd_text(fb,22,63,"Could not join",PJ_UI_OTHER,alpha,advance);
            pj_osd_text(fb,8,82,pj_join_words[s->switch_fail<=PJ_JOIN_FAILED?s->switch_fail:PJ_JOIN_FAILED],0xd0d0d0u,alpha,advance);
            if(s->switch_fail==PJ_JOIN_WRONG_PASSWORD){pj_osd_text(fb,8,100,"Check the password and try again.",PJ_UI_MUTED,alpha,advance);
                pj_osd_text(fb,8,132,"A: change password    B: back",PJ_UI_MUTED,alpha,advance);}
            else{pj_osd_text(fb,8,100,s->switch_saved?"It stays saved: the console joins":"It stays first: the console joins",PJ_UI_MUTED,alpha,advance);
                pj_osd_text(fb,8,113,"it by itself when it is in range.",PJ_UI_MUTED,alpha,advance);
                pj_osd_text(fb,8,132,"A or B: continue",PJ_UI_MUTED,alpha,advance);}
        }
        break;
    }
    case PJ_SU_WIFI_FORGET:case PJ_SU_JF_FORGET: {
        int jf=s->screen==PJ_SU_JF_FORGET;
        pj_su_header(fb,jf?"Forget sign-in?":"Forget network?",accent,alpha,advance);
        if(jf){pj_osd_text(fb,8,24,"Only this console signs out.",0xd0d0d0u,alpha,advance);pj_osd_text(fb,8,38,"Your other devices stay in.",PJ_UI_MUTED,alpha,advance);}
        else{pj_su_fit(line,sizeof(line),s->ssid[s->chosen],144,advance);pj_osd_text(fb,8,24,line,0xd0d0d0u,alpha,advance);pj_osd_text(fb,8,38,"Its password is erased here.",PJ_UI_MUTED,alpha,advance);}
        pj_su_row(fb,60,"Forget",s->focus==0,PJ_UI_OTHER,0,alpha,advance);
        pj_su_row(fb,60+PJ_SU_ROW_PITCH,"Keep",s->focus==1,PJ_UI_SAFE,0,alpha,advance);
        pj_osd_text(fb,8,132,"A: choose    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_SCAN: {
        pj_su_header(fb,"Choose a network",accent,alpha,advance);
        unsigned rows=pj_setup_rows(s);
        for(unsigned i=s->scroll;i<rows&&i<s->scroll+PJ_SU_VISIBLE;i++) {
            int y=PJ_SU_ROW_Y+(int)(i-s->scroll)*PJ_SU_ROW_PITCH;
            if(i<s->scan.count) {
                const pj_scan_entry *e=&s->scan.net[i];
                pj_su_fit(line,sizeof(line),e->ssid,e->saved?84:100,advance);
                pj_su_row(fb,y,line,s->focus==i,accent,0,alpha,advance);
                pj_su_bars(fb,12,y+4,pj_scan_bars(e->rssi),s->focus==i?accent:PJ_UI_MUTED);
                if(e->secured)pj_icon_lock(fb,138,y+2,s->focus==i?accent:PJ_UI_MUTED);
                if(e->saved)pj_osd_text(fb,104,y+3,"saved",PJ_UI_SAFE,alpha,advance);
            } else{pj_su_row(fb,y,"Scan again",s->focus==i,accent,0,alpha,advance);pj_osd_text(fb,13,y+3,"~",s->focus==i?accent:PJ_UI_MUTED,alpha,advance);}
        }
        if(!s->scan.count)pj_osd_text(fb,8,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH+4,"No network heard",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,115,"Hotel sign-in Wi-Fi: use a hotspot",PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,131,"A: join    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_PASSWORD:case PJ_SU_JF_ADDRESS:pj_kb_draw(fb,&s->kb,accent,watched,alpha,advance);break;
    case PJ_SU_JF:
        pj_su_header(fb,"Jellyfin",accent,alpha,advance);
        if(s->jellyfin_linked) {
            pj_su_row(fb,PJ_SU_ROW_Y,"Sign in again",s->focus==0,accent,0,alpha,advance);
            pj_su_row(fb,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH,"Forget this sign-in",s->focus==1,accent,0,alpha,advance);
            pj_su_row(fb,PJ_SU_ROW_Y+2*PJ_SU_ROW_PITCH,"Find my server again",s->focus==2,accent,0,alpha,advance); /* 1.3.0 */
            pj_icon_jellyfin(fb,12,PJ_SU_ROW_Y+2*PJ_SU_ROW_PITCH+2,0);
            pj_osd_text(fb,8,80,"Signed in on this console.",PJ_UI_SAFE,alpha,advance);
            pj_osd_text(fb,8,94,"Revoke it any time in Jellyfin:",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,107,"Dashboard > Devices > Chromatic",PJ_UI_MUTED,alpha,advance);
        } else {
            pj_su_row(fb,PJ_SU_ROW_Y,"Find servers nearby",s->focus==0,accent,0,alpha,advance);
            pj_su_row(fb,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH,"Enter server address",s->focus==1,accent,0,alpha,advance);
            pj_osd_text(fb,8,66,s->wifi_connected?"Sign in with Quick Connect: a":"Wi-Fi is not connected yet.",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,80,s->wifi_connected?"code you approve in any of your":"Open Jellyfin once to join;",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,93,s->wifi_connected?"Jellyfin apps. No password here.":"then come back here.",PJ_UI_MUTED,alpha,advance);
        }
        pj_icon_jellyfin(fb,12,PJ_SU_ROW_Y+2,0);pj_osd_text(fb,13,PJ_SU_ROW_Y+PJ_SU_ROW_PITCH+3,s->jellyfin_linked?"x":"#",s->focus==1?accent:PJ_UI_MUTED,alpha,advance);
        pj_osd_text(fb,8,132,"A: choose    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    case PJ_SU_JF_SERVERS: {
        pj_su_header(fb,"Servers found",accent,alpha,advance);
        unsigned rows=pj_setup_rows(s);
        for(unsigned i=s->scroll;i<rows&&i<s->scroll+PJ_SU_VISIBLE;i++) {
            int y=PJ_SU_ROW_Y+(int)(i-s->scroll)*PJ_SU_ROW_PITCH;
            if(i<s->servers.count){pj_su_fit(line,sizeof(line),s->servers.server[i].name,112,advance);pj_su_row(fb,y,line,s->focus==i,accent,0,alpha,advance);pj_icon_jellyfin(fb,12,y+2,0);}
            else{pj_su_row(fb,y,"Enter address instead",s->focus==i,accent,0,alpha,advance);pj_osd_text(fb,13,y+3,"#",s->focus==i?accent:PJ_UI_MUTED,alpha,advance);}
        }
        if(s->focus<s->servers.count){pj_su_fit(line,sizeof(line),s->servers.server[s->focus].url,144,advance);pj_osd_text(fb,8,115,line,PJ_UI_MUTED,alpha,advance);}
        pj_osd_text(fb,8,131,"A: sign in    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_FIND: { /* Find my server again (1.3.0): guided, plain next steps when nothing answered */
        unsigned n=s->servers.count,rows=pj_setup_rows(s);
        pj_su_header(fb,n?"Is this your server?":"Find my server",accent,alpha,advance);
        int top=n?PJ_SU_ROW_Y:86;
        if(!n) {
            pj_osd_text(fb,8,24,"Your server was not found",0xffffffu,alpha,advance);
            pj_osd_text(fb,8,38,"on this network. Check it is",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,51,"on and on the same Wi-Fi,",PJ_UI_MUTED,alpha,advance);
            pj_osd_text(fb,8,64,"then look again.",PJ_UI_MUTED,alpha,advance);
        }
        for(unsigned i=s->scroll;i<rows&&i<s->scroll+(n?PJ_SU_VISIBLE:2u);i++) {
            int y=top+(int)(i-s->scroll)*PJ_SU_ROW_PITCH;
            if(i<n){pj_su_fit(line,sizeof(line),s->servers.server[i].name,112,advance);pj_su_row(fb,y,line,s->focus==i,accent,0,alpha,advance);pj_icon_jellyfin(fb,12,y+2,0);}
            else if(i==n){pj_su_row(fb,y,"Look again",s->focus==i,accent,0,alpha,advance);pj_osd_text(fb,13,y+3,"@",s->focus==i?accent:PJ_UI_MUTED,alpha,advance);}
            else{pj_su_row(fb,y,"Enter the address",s->focus==i,accent,0,alpha,advance);pj_osd_text(fb,13,y+3,"#",s->focus==i?accent:PJ_UI_MUTED,alpha,advance);}
        }
        if(n&&s->focus<n){pj_su_fit(line,sizeof(line),s->servers.server[s->focus].url,144,advance);pj_osd_text(fb,8,115,line,PJ_UI_MUTED,alpha,advance);}
        pj_osd_text(fb,8,131,s->find_from_app?"A: choose    B: Jellyfin":"A: choose    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_FIND_CONFIRM: { /* 1.3.1: the server's own words, then the owner's yes */
        pj_su_header(fb,"Is this your server?",accent,alpha,advance);
        pj_su_fit(line,sizeof(line),s->find_name,144,advance);pj_osd_text(fb,8,24,line,0xffffffu,alpha,advance);
        snprintf(line,sizeof(line),"Jellyfin %s",s->find_version[0]?s->find_version:"(version not given)");
        pj_osd_text(fb,8,38,line,PJ_UI_MUTED,alpha,advance);
        {char wide[132];pj_su_fit(wide,sizeof(wide),s->find_url,144,advance);pj_osd_text(fb,8,51,wide,PJ_UI_MUTED,alpha,advance);}
        pj_su_row(fb,68,"Yes, this is my server",s->focus==0,accent,0,alpha,advance);
        pj_su_row(fb,68+PJ_SU_ROW_PITCH,"No, look again",s->focus==1,accent,0,alpha,advance);
        pj_osd_text(fb,8,110,"Only then is your sign-in",PJ_UI_NOTE,alpha,advance);
        pj_osd_text(fb,8,121,"checked with it.",PJ_UI_NOTE,alpha,advance);
        pj_osd_text(fb,8,133,"A: choose    B: back",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_JF_CODE: {
        pj_su_header(fb,"Quick Connect",accent,alpha,advance);
        if(s->qc.state==PJ_QC_WAIT||s->qc.state==PJ_QC_AUTH||s->qc.state==PJ_QC_DONE) {
            pj_osd_text(fb,8,24,"Your code:",PJ_UI_MUTED,alpha,advance);
            int w=0;for(const char *p=s->qc.code;*p;p++)w+=advance[((unsigned char)*p<32||(unsigned char)*p>126?'?':(unsigned char)*p)-32]*2;
            pj_osd_rect(fb,8,38,144,30,PJ_UI_EDGE);pj_osd_rect(fb,9,39,142,28,0x0c0c12u);
            pj_su_text2x(fb,80-w/2,42,s->qc.code,0xffffffu,alpha,advance);
            pj_osd_text(fb,8,74,"In any of your Jellyfin apps:",0xd0d0d0u,alpha,advance);
            pj_osd_text(fb,8,87,"Settings > Quick Connect, enter",0xd0d0d0u,alpha,advance);
            pj_osd_text(fb,8,100,"the code and approve it.",0xd0d0d0u,alpha,advance);
            snprintf(line,sizeof(line),"Waiting%.*s",(int)((now/500u)%4u),"...");
            pj_osd_text(fb,8,116,s->qc.state==PJ_QC_WAIT?line:"Approved, signing in...",s->qc.state==PJ_QC_WAIT?PJ_UI_NOTE:PJ_UI_SAFE,alpha,advance);
        } else {
            snprintf(line,sizeof(line),"Asking the server%.*s",(int)((now/500u)%4u),"...");
            pj_osd_text(fb,8,40,line,PJ_UI_NOTE,alpha,advance);
        }
        pj_osd_text(fb,8,132,"B: cancel",PJ_UI_MUTED,alpha,advance);
        break;
    }
    case PJ_SU_BUSY: {
        pj_su_header(fb,"Setup",accent,alpha,advance);
        pj_osd_text(fb,8,60,s->busy_line,0xffffffu,alpha,advance);
        for(unsigned i=0;i<4;i++)pj_osd_rect(fb,8+(int)i*8,78,5,5,i==(now/250u)%4u?accent:PJ_UI_EDGE);
        if(s->request==PJ_SU_REQ_COMMIT||!strncmp(s->busy_line,"Saving",6)||!strncmp(s->busy_line,"Switching",9))pj_osd_text(fb,8,96,"Do not switch off.",PJ_UI_NOTE,alpha,advance);
        break;
    }
    case PJ_SU_MESSAGE:
        pj_su_header(fb,"Setup",accent,alpha,advance);
        for(unsigned i=0;i<3;i++)if(s->message[i][0])pj_osd_text(fb,8,40+(int)i*14,s->message[i],i?PJ_UI_MUTED:0xffffffu,alpha,advance);
        pj_osd_text(fb,8,132,"A or B: continue",PJ_UI_MUTED,alpha,advance);
        break;
    default:pj_osd_fill(fb,PJ_OSD_BG);break;
    }
}
#endif
