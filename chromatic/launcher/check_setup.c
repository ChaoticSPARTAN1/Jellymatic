/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Host test of the on-console setup: keyboard, scan list, discovery and Quick Connect protocol,
 * the screens, the record builder and the single audited flash writer under power cuts.
 * No hardware. Modes:
 *   check_setup previews <dir>   run every check, write ui-setup-*.ppm into <dir>
 *   check_setup build            stdin: one v2 sector; stdout: the sector rebuilt from the parse
 *   check_setup commit <op>      stdin: sectors A+B (8192 bytes); stdout: A+B after one commit
 *                                op: add | change | forget | jellyfin | clearjf | address | nochange
 * Synthetic data only; the harness prints counts, never names or keys. */
#define PJ_ENABLE_SETUP 1
#include <stdio.h>
#include <stdlib.h>
#include "shared/setup_ui.h"
#include "shared/provision_build.h"
#include "../../pocket/ui_font.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"setup check failed at line %d\n",__LINE__);exit(1);}} while(0)
static uint16_t screen[PJ_OSD_W*PJ_OSD_H];
static char out_dir[512];
static void write_ppm(const char *name) {
    char path[600];snprintf(path,sizeof(path),"%s/ui-setup-%s.ppm",out_dir,name);
    FILE *f=fopen(path,"wb");CHECK(f);fprintf(f,"P6\n160 144\n255\n");
    for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++) {
        unsigned c=screen[i];CHECK(c!=0xf81fu);
        fputc((int)(((c>>11)&31)*255/31),f);fputc((int)(((c>>5)&63)*255/63),f);fputc((int)((c&31)*255/31),f);
    }
    CHECK(!ferror(f));fclose(f);
}
static void fixed_random(uint8_t out[12]){for(unsigned i=0;i<12;i++)out[i]=(uint8_t)(0xa0+i);}
/* Does the framebuffer show this text anywhere? The text is rendered in each colour the screens
 * use, on each plain background they draw on, and that exact patch is searched for; so a secret
 * drawn in the normal font would be found. */
static int screen_shows(const char *text) {
    static uint16_t patch[PJ_OSD_W*11];
    static const unsigned colours[]={0xffffffu,PJ_UI_MUTED,PJ_UI_NOTE,PJ_UI_SAFE,0xd0d0d0u,0xe0e0e0u,PJ_UI_JELLYFIN};
    static const unsigned bgs[]={PJ_OSD_BG,0x0c0c12u,PJ_UI_ROW};
    int w=pj_osd_measure(text,pj_font_advance);if(w<=0||w>PJ_OSD_W)return 0;
    for(unsigned ci=0;ci<sizeof(colours)/sizeof(colours[0]);ci++)for(unsigned bi=0;bi<sizeof(bgs)/sizeof(bgs[0]);bi++) {
        for(unsigned i=0;i<PJ_OSD_W*11u;i++)patch[i]=pj_osd_rgb(bgs[bi]);
        { /* render into a strip at x=0 */
            static uint16_t strip[PJ_OSD_W*PJ_OSD_H];
            for(unsigned i=0;i<PJ_OSD_W*PJ_OSD_H;i++)strip[i]=pj_osd_rgb(bgs[bi]);
            pj_osd_text(strip,0,0,text,colours[ci],pj_font_alpha,pj_font_advance);
            memcpy(patch,strip,sizeof(uint16_t)*PJ_OSD_W*11u);
        }
        for(int y=0;y<=PJ_OSD_H-11;y++)for(int x=0;x<=PJ_OSD_W-w;x++) {
            int same=1;
            for(int r=0;r<11&&same;r++)for(int c=0;c<w;c++)if(patch[r*PJ_OSD_W+c]!=screen[(y+r)*PJ_OSD_W+x+c]){same=0;break;}
            if(same)return 1;
        }
    }
    return 0;
}
/* ---- keyboard ---- */
static void keyboard_checks(void) {
    pj_keyboard k;
    /* Every printable ASCII character is on exactly one key of some page. */
    unsigned seen[128]={0};
    for(unsigned p=0;p<PJ_KB_PAGES;p++)for(unsigned r=0;r<PJ_KB_ROWS;r++) {
        const char *row=pj_kb_pages[p][r];CHECK(strlen(row)<=PJ_KB_COLS);
        for(const char *c=row;*c;c++){CHECK((unsigned char)*c>=0x21&&(unsigned char)*c<=0x7e);seen[(unsigned char)*c]++;}
    }
    for(unsigned c=0x21;c<=0x7e;c++)CHECK(seen[c]>=1);
    CHECK(seen['1']==3&&seen['a']==1&&seen['!']==2); /* digits on every page, letters once */
    /* Typing by navigation: find each character and press A; the text reads back. */
    pj_kb_open(&k,"Password: test","",64,8,1);
    const char *want="Ab1 !z~";
    for(const char *c=want;*c;c++) {
        if(*c==' '){
            while(k.row!=PJ_KB_ROWS)pj_kb_press(&k,PJ_BTN_DOWN);
            while(k.col!=PJ_KB_SP_SPACE)pj_kb_press(&k,PJ_BTN_RIGHT);
            CHECK(pj_kb_press(&k,PJ_BTN_A)==PJ_KB_REDRAW);continue;}
        int found=0;
        for(unsigned tries=0;tries<PJ_KB_PAGES&&!found;tries++) {
            for(unsigned r=0;r<PJ_KB_ROWS&&!found;r++)for(unsigned col=0;col<PJ_KB_COLS&&!found;col++)if(pj_kb_char_at(&k,r,col)==*c) {
                while(k.row!=r)pj_kb_press(&k,k.row>r&&k.row!=PJ_KB_ROWS?PJ_BTN_UP:PJ_BTN_DOWN);
                while(k.col!=col)pj_kb_press(&k,PJ_BTN_RIGHT);
                CHECK(pj_kb_press(&k,PJ_BTN_A)==PJ_KB_REDRAW);found=1;
            }
            if(!found)pj_kb_press(&k,PJ_BTN_SELECT);
        }
        CHECK(found);
    }
    CHECK(!strcmp(k.text,want)&&k.len==7);
    CHECK(pj_kb_press(&k,PJ_BTN_START)==PJ_KB_REDRAW); /* 7 < min 8: not accepted */
    pj_kb_type(&k,'x');CHECK(pj_kb_press(&k,PJ_BTN_START)==PJ_KB_DONE&&k.len==8);
    /* B deletes; B on an empty field cancels. The cap holds. */
    for(unsigned i=0;i<8;i++)CHECK(pj_kb_press(&k,PJ_BTN_B)==PJ_KB_REDRAW);
    CHECK(k.len==0&&pj_kb_press(&k,PJ_BTN_B)==PJ_KB_CANCEL);
    pj_kb_open(&k,"t","",4,0,0);for(unsigned i=0;i<10;i++)pj_kb_type(&k,'q');CHECK(k.len==4&&!strcmp(k.text,"qqqq"));
    /* Wrap-around on every row, and the special row maps columns both ways. */
    pj_kb_open(&k,"t","",8,0,1);k.row=0;k.col=0;pj_kb_press(&k,PJ_BTN_LEFT);CHECK(k.col==PJ_KB_COLS-1);pj_kb_press(&k,PJ_BTN_RIGHT);CHECK(k.col==0);
    k.col=10;pj_kb_press(&k,PJ_BTN_UP);CHECK(k.row==PJ_KB_ROWS&&k.col==4);pj_kb_press(&k,PJ_BTN_DOWN);CHECK(k.row==0&&k.col==10);
    /* Secret: dots unless shown; never shown while Remote Pilot watches. The clear key clears a plain field. */
    pj_kb_open(&k,"Password: Home","",64,8,1);for(const char *c="hunter22";*c;c++)pj_kb_type(&k,*c);
    pj_kb_draw(screen,&k,PJ_UI_JELLYFIN,0,pj_font_alpha,pj_font_advance);CHECK(!screen_shows("hunter22"));
    while(k.row!=PJ_KB_ROWS)pj_kb_press(&k,PJ_BTN_DOWN);
    while(k.col!=PJ_KB_SP_SHOW)pj_kb_press(&k,PJ_BTN_RIGHT);
    pj_kb_press(&k,PJ_BTN_A);CHECK(k.show);
    pj_kb_draw(screen,&k,PJ_UI_JELLYFIN,0,pj_font_alpha,pj_font_advance);CHECK(screen_shows("hunter22"));write_ppm("keyboard-shown");
    pj_kb_draw(screen,&k,PJ_UI_JELLYFIN,1,pj_font_alpha,pj_font_advance);CHECK(!screen_shows("hunter22")&&screen_shows("Remote Pilot"));
    write_ppm("keyboard-watched");
    pj_kb_press(&k,PJ_BTN_A);CHECK(!k.show);pj_kb_draw(screen,&k,PJ_UI_JELLYFIN,0,pj_font_alpha,pj_font_advance);write_ppm("keyboard");
    pj_kb_open(&k,"Server address","http://",128,8,0);pj_kb_draw(screen,&k,PJ_UI_JELLYFIN,0,pj_font_alpha,pj_font_advance);
    CHECK(screen_shows("http://"));write_ppm("keyboard-address");
    while(k.row!=PJ_KB_ROWS)pj_kb_press(&k,PJ_BTN_DOWN);
    while(k.col!=PJ_KB_SP_SHOW)pj_kb_press(&k,PJ_BTN_RIGHT);
    pj_kb_press(&k,PJ_BTN_A);CHECK(k.len==0);
    /* Every key label and footer fits its key / the screen. */
    for(unsigned p=0;p<PJ_KB_PAGES;p++)CHECK(pj_osd_measure(pj_kb_page_names[p],pj_font_advance)<=24);
    const char *labels[]={"space","show","hide","clear","del","OK","A: type  B: delete  Start: OK","Select: abc / ABC / #+=","Hidden: Remote Pilot connected"};
    for(unsigned i=0;i<6;i++)CHECK(pj_osd_measure(labels[i],pj_font_advance)<=(i==0?44:24));
    for(unsigned i=6;i<9;i++)CHECK(8+pj_osd_measure(labels[i],pj_font_advance)<=156);
    for(unsigned c=0x21;c<=0x7e;c++){char l[2]={(char)c,0};CHECK(pj_osd_measure(l,pj_font_advance)<=PJ_KB_KEY_W-2);}
}
/* ---- scan list ---- */
static void scan_checks(void) {
    pj_scan_list l;pj_scan_reset(&l);
    CHECK(pj_scan_add(&l,(const uint8_t *)"Home",4,-70,1)&&l.count==1&&pj_scan_bars(-70)==2);
    CHECK(pj_scan_add(&l,(const uint8_t *)"Cafe",4,-50,0)&&l.count==2&&!strcmp(l.net[0].ssid,"Cafe")); /* strongest first */
    CHECK(pj_scan_add(&l,(const uint8_t *)"Home",4,-40,1)&&l.count==2&&!strcmp(l.net[0].ssid,"Home")&&l.net[0].rssi==-40); /* one row per name */
    CHECK(!pj_scan_add(&l,(const uint8_t *)"",0,-30,1)&&l.count==2); /* hidden names left out */
    CHECK(!pj_scan_add(&l,(const uint8_t *)"a\x01z",3,-30,1)&&l.count==2); /* control bytes */
    for(unsigned i=0;i<20;i++){char n[8];snprintf(n,sizeof(n),"n%02u",i);pj_scan_add(&l,(const uint8_t *)n,3,-80-(int)i,1);}
    CHECK(l.count==PJ_SCAN_MAX&&!strcmp(l.net[0].ssid,"Home")&&!strcmp(l.net[1].ssid,"Cafe"));
    for(unsigned i=1;i<l.count;i++)CHECK(l.net[i-1].rssi>=l.net[i].rssi);
    CHECK(pj_scan_add(&l,(const uint8_t *)"Strong",6,-45,0)&&l.count==PJ_SCAN_MAX&&!strcmp(l.net[1].ssid,"Strong")); /* replaces the weakest */
    const char *saved[2]={"Cafe","Nope"};pj_scan_mark_saved(&l,saved,2);
    unsigned marked=0;for(unsigned i=0;i<l.count;i++)marked+=l.net[i].saved;CHECK(marked==1);
    CHECK(pj_scan_bars(-50)==4&&pj_scan_bars(-60)==3&&pj_scan_bars(-80)==1&&pj_scan_bars(-95)==0);
    CHECK(pj_join_result(0,0,0,1)==PJ_JOIN_OK&&pj_join_result(15,0,0,0)==PJ_JOIN_FAILED&&pj_join_result(202,0,0,0)==PJ_JOIN_FAILED); /* 1.3.4: one failure is no verdict on the password */
    CHECK(pj_join_result(201,0,0,0)==PJ_JOIN_NOT_FOUND&&pj_join_result(0,1,0,0)==PJ_JOIN_NO_ADDRESS&&pj_join_result(0,0,1,0)==PJ_JOIN_NO_ANSWER);
    for(unsigned i=0;i<7;i++)CHECK(8+pj_osd_measure(pj_join_words[i],pj_font_advance)<=156);
}
/* ---- discovery and Quick Connect ---- */
static void protocol_checks(void) {
    pj_jf_servers s;memset(&s,0,sizeof(s));
    const char *reply="{\"Address\":\"http://192.0.2.20:8096/\",\"Id\":\"abc\",\"Name\":\"Living room\",\"EndpointAddress\":null}";
    CHECK(pj_discovery_add(&s,reply,strlen(reply))&&s.count==1&&!strcmp(s.server[0].url,"http://192.0.2.20:8096")&&!strcmp(s.server[0].name,"Living room"));
    CHECK(!pj_discovery_add(&s,reply,strlen(reply))&&s.count==1); /* duplicate address */
    const char *bad[]={"not json","{\"Name\":\"x\"}","{\"Address\":\"ftp://x\"}","{\"Address\":\"http://u@h\"}","[1]",""};
    for(unsigned i=0;i<6;i++)CHECK(!pj_discovery_add(&s,bad[i],strlen(bad[i]))&&s.count==1);
    const char *noname="{\"Address\":\"HTTP://nas.local:8096\",\"Name\":\"\\u00e9t\\u00e9\"}";
    CHECK(pj_discovery_add(&s,noname,strlen(noname))&&s.count==2&&!strcmp(s.server[1].url,"http://nas.local:8096")&&!strcmp(s.server[1].name,"?t?"));
    for(unsigned i=0;i<5;i++){char r[80];snprintf(r,sizeof(r),"{\"Address\":\"http://10.0.0.%u\"}",i+1);pj_discovery_add(&s,r,strlen(r));}
    CHECK(s.count==PJ_DISCOVERY_MAX&&s.heard==14);
    char url[129];
    snprintf(url,sizeof(url),"192.0.2.5:8096/");CHECK(pj_qc_normalize_url(url,sizeof(url))&&!strcmp(url,"http://192.0.2.5:8096"));
    snprintf(url,sizeof(url),"  HTTPS://Jelly.Example/jf/ ");CHECK(pj_qc_normalize_url(url,sizeof(url))&&!strcmp(url,"https://Jelly.Example/jf"));
    snprintf(url,sizeof(url),"http://");CHECK(!pj_qc_normalize_url(url,sizeof(url)));
    snprintf(url,sizeof(url),"http://a b");CHECK(!pj_qc_normalize_url(url,sizeof(url)));
    snprintf(url,sizeof(url),"http://h?x=1");CHECK(!pj_qc_normalize_url(url,sizeof(url)));
    snprintf(url,sizeof(url),"   ");CHECK(!pj_qc_normalize_url(url,sizeof(url)));
    char device[65];uint8_t rnd[12];fixed_random(rnd);pj_qc_device_id(device,rnd);
    CHECK(strlen(device)==34&&!strncmp(device,"chromatic-a0a1a2",16)&&pj_prov_charset(device,"._-"));
    /* The happy path, with the pre-10.9 GET fallback and two polls. */
    pj_quickconnect q;pj_qc_http h;uint32_t now=1000;
    pj_qc_begin(&q,"http://192.0.2.20:8096",device,now);
    CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.method,"GET")&&!strcmp(h.path,"/QuickConnect/Enabled")&&!h.with_token);
    CHECK(!pj_qc_request(&q,now,&h)); /* one request out at a time */
    pj_qc_response(&q,200,"true",4,now);CHECK(q.state==PJ_QC_INITIATE);
    CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.method,"POST")&&!strcmp(h.path,"/QuickConnect/Initiate"));
    pj_qc_response(&q,405,NULL,0,now);CHECK(q.state==PJ_QC_INITIATE_GET);
    CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.method,"GET")&&!strcmp(h.path,"/QuickConnect/Initiate"));
    const char *init="{\"Authenticated\":false,\"Secret\":\"s3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3\",\"Code\":\"482913\",\"DeviceId\":\"x\",\"AppName\":\"Chromatic Jellyfin\"}";
    pj_qc_response(&q,200,init,strlen(init),now);CHECK(q.state==PJ_QC_WAIT&&!strcmp(q.code,"482913"));
    CHECK(!pj_qc_request(&q,now+100,&h)); /* polls every 2 s, not at once */
    now+=PJ_QC_POLL_MS;CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.path,"/QuickConnect/Connect?secret=s3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3"));
    const char *waiting="{\"Authenticated\":false,\"Secret\":\"s3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3\",\"Code\":\"482913\"}";
    pj_qc_response(&q,200,waiting,strlen(waiting),now);CHECK(q.state==PJ_QC_WAIT);
    now+=PJ_QC_POLL_MS;CHECK(pj_qc_request(&q,now,&h));
    const char *approved="{\"Authenticated\":true,\"Secret\":\"s3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3\",\"Code\":\"482913\"}";
    pj_qc_response(&q,200,approved,strlen(approved),now);CHECK(q.state==PJ_QC_AUTH);
    CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.method,"POST")&&!strcmp(h.path,"/Users/AuthenticateWithQuickConnect")&&!strcmp(h.body,"{\"Secret\":\"s3cr3tS3cr3tS3cr3tS3cr3tS3cr3tS3\"}"));
    const char *auth="{\"User\":{\"Name\":\"sam\",\"Id\":\"0123456789ABCDEF0123456789abcdef\",\"Policy\":{\"IsAdministrator\":true,\"EnabledFolders\":[]}},"
                     "\"SessionInfo\":{\"Id\":\"q\"},\"AccessToken\":\"deadbeefcafef00ddeadbeefcafef00d\",\"ServerId\":\"zz\"}";
    pj_qc_response(&q,200,auth,strlen(auth),now);
    CHECK(q.state==PJ_QC_DONE&&!strcmp(q.user_id,"0123456789abcdef0123456789abcdef")&&!strcmp(q.token,"deadbeefcafef00ddeadbeefcafef00d")&&!q.secret[0]);
    /* Dashed GUID user ids are accepted; a token the record cannot hold is refused and wiped. */
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_AUTH;pj_qc_request(&q,now,&h);
    const char *dashed="{\"User\":{\"Id\":\"01234567-89ab-cdef-0123-456789abcdef\"},\"AccessToken\":\"ABCDEFGHIJKLMNOPQRSTUVWX\"}";
    pj_qc_response(&q,200,dashed,strlen(dashed),now);CHECK(q.state==PJ_QC_DONE&&!strcmp(q.user_id,"0123456789abcdef0123456789abcdef"));
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_AUTH;pj_qc_request(&q,now,&h);
    const char *odd="{\"User\":{\"Id\":\"0123456789abcdef0123456789abcdef\"},\"AccessToken\":\"has space in it!\"}";
    pj_qc_response(&q,200,odd,strlen(odd),now);CHECK(q.state==PJ_QC_FAILED&&q.fail==PJ_QC_FAIL_UNSTORABLE&&!q.token[0]);
    /* Failures: unreachable, disabled, expired (404 while waiting), deadline, malformed. */
    pj_qc_begin(&q,"http://h",device,now);pj_qc_request(&q,now,&h);pj_qc_response(&q,0,NULL,0,now);CHECK(q.state==PJ_QC_FAILED&&q.fail==PJ_QC_FAIL_UNREACHABLE);
    pj_qc_begin(&q,"http://h",device,now);pj_qc_request(&q,now,&h);pj_qc_response(&q,200,"false",5,now);CHECK(q.fail==PJ_QC_FAIL_DISABLED);
    pj_qc_begin(&q,"http://h",device,now);pj_qc_request(&q,now,&h);pj_qc_response(&q,401,"true",4,now);CHECK(q.fail==PJ_QC_FAIL_REFUSED);
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_WAIT;snprintf(q.secret,sizeof(q.secret),"abc");pj_qc_request(&q,now,&h);
    pj_qc_response(&q,404,NULL,0,now);CHECK(q.fail==PJ_QC_FAIL_EXPIRED&&!q.secret[0]);
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_WAIT;CHECK(!pj_qc_request(&q,now+PJ_QC_DEADLINE_MS,&h)&&q.state==PJ_QC_FAILED&&q.fail==PJ_QC_FAIL_EXPIRED);
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_INITIATE;pj_qc_request(&q,now,&h);pj_qc_response(&q,200,"{\"Code\":\"1\"}",12,now);CHECK(q.fail==PJ_QC_FAIL_REFUSED);
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_INITIATE;pj_qc_request(&q,now,&h);pj_qc_response(&q,200,"{\"Code\":\"12 3\",\"Secret\":\"x\"}",27,now);CHECK(q.fail==PJ_QC_FAIL_REFUSED);
    pj_qc_begin(&q,"http://h",device,now);q.state=PJ_QC_INITIATE;pj_qc_request(&q,now,&h);pj_qc_response(&q,200,"{\"Code\":\"123\",\"Secret\":\"ok\"",26,now);CHECK(q.fail==PJ_QC_FAIL_REFUSED);
    /* Logout: one POST as this device with its token; any answer ends it and wipes the token. */
    pj_qc_logout_begin(&q,"http://h",device,"tok3ntok3ntok3ntok3n");
    CHECK(pj_qc_request(&q,now,&h)&&!strcmp(h.path,"/Sessions/Logout")&&h.with_token&&!strcmp(h.method,"POST"));
    pj_qc_response(&q,204,NULL,0,now);CHECK(q.state==PJ_QC_LOGGED_OUT&&!q.token[0]);
    char header[320];pj_qc_auth_header(header,sizeof(header),device,"1.2.7","tok");
    CHECK(!strcmp(header,"MediaBrowser Client=\"Chromatic Jellyfin\", Device=\"Chromatic\", DeviceId=\"chromatic-a0a1a2a3a4a5a6a7a8a9aaab\", Version=\"1.2.7\", Token=\"tok\""));
    pj_qc_auth_header(header,sizeof(header),device,"1.2.7",NULL);CHECK(!strstr(header,"Token"));
    for(unsigned i=1;i<6;i++)CHECK(8+pj_osd_measure(pj_qc_fail_words[i],pj_font_advance)<=156);
}
/* ---- the flash model: 4 MiB, every erase/write logged, with a programmable power cut ---- */
typedef struct {uint8_t *flash;unsigned ops,cut_at;int cut;unsigned erases,writes,reads;uint32_t touched_outside;unsigned cut_byte;} flash_model;
#define FLASH_SIZE 0x400000u
static int fm_read(void *ctx,uint32_t off,void *out,uint32_t n) {
    flash_model *m=ctx;m->reads++;if(off+n>FLASH_SIZE)return -1;memcpy(out,m->flash+off,n);return 0;
}
static int fm_in_sectors(uint32_t off,uint32_t n){return (off==PJ_PROV_FLASH_A||off==PJ_PROV_FLASH_B)&&n==4096u;}
static int fm_erase(void *ctx,uint32_t off,uint32_t n) {
    flash_model *m=ctx;if(!fm_in_sectors(off,n))m->touched_outside++;
    if(m->cut)return -1;
    m->erases++;
    if(m->cut_at&&++m->ops>=m->cut_at){ /* torn erase: some bytes 0xFF, the rest as they were */
        unsigned upto=m->cut_byte%(n+1);memset(m->flash+off,0xff,upto);m->cut=1;return -1;}
    memset(m->flash+off,0xff,n);return 0;
}
static int fm_write(void *ctx,uint32_t off,const void *data,uint32_t n) {
    flash_model *m=ctx;if(!fm_in_sectors(off,n))m->touched_outside++;
    if(m->cut)return -1;
    m->writes++;
    if(m->cut_at&&++m->ops>=m->cut_at){ /* torn write: a prefix lands (flash only clears bits) */
        unsigned upto=m->cut_byte%(n+1);
        for(unsigned i=0;i<upto;i++)m->flash[off+i]&=((const uint8_t *)data)[i];
        if(upto<n)m->flash[off+upto]&=(uint8_t)(((const uint8_t *)data)[upto]|0x0f); /* a half-programmed byte */
        m->cut=1;return -1;}
    for(unsigned i=0;i<n;i++)m->flash[off+i]&=((const uint8_t *)data)[i];
    return 0;
}
static pj_prov_flash fm_ops(flash_model *m){pj_prov_flash f={m,fm_read,fm_erase,fm_write};return f;}
static void fm_init(flash_model *m,uint8_t *flash){memset(m,0,sizeof(*m));m->flash=flash;memset(flash,0xff,FLASH_SIZE);}
static void fm_put(flash_model *m,uint32_t off,const uint8_t *sector){memcpy(m->flash+off,sector,4096);}
static pj_prov2 sample_record(unsigned seq,unsigned nets,int jellyfin) {
    pj_prov2 r;memset(&r,0,sizeof(r));r.version=2;r.sequence=seq;r.count=nets;
    for(unsigned i=0;i<nets;i++){snprintf(r.net[i].ssid,33,"Net%u",i);snprintf(r.net[i].psk,65,"password%u",i);}
    r.has_gateway=1;r.gateway_ipv4[0]=192;r.gateway_ipv4[1]=168;r.gateway_ipv4[2]=1;r.gateway_ipv4[3]=9;r.gateway_port=4443;
    memset(r.gateway_cert_sha256,0x11,32);memset(r.gateway_token,0x22,32);memset(r.chr1_key,0x33,32);
    if(jellyfin){r.has_jellyfin=1;snprintf(r.url,129,"http://192.0.2.20:8096");snprintf(r.user_id,33,"0123456789abcdef0123456789abcdef");
        snprintf(r.access_token,65,"tokentokentokentoken");snprintf(r.device_id,65,"chromatic-000000000000000000000000");}
    return r;
}
static int same_without_sequence(const pj_prov2 *a,const pj_prov2 *b){pj_prov2 x=*a,y=*b;x.sequence=y.sequence=0;x.version=y.version=0;return !memcmp(&x,&y,sizeof(x));}
/* One commit under a power cut at operation `cut_at` (0: none), torn at byte `cut_byte`.
 * Afterwards the pair must still hold exactly the old or the new record; a retry must finish. */
static unsigned cut_runs,cut_old,cut_new;
static void commit_under_cuts(const pj_prov2 *old_a,const pj_prov2 *old_b,const pj_prov_edit *edit,const pj_prov2 *expect) {
    static uint8_t flash[FLASH_SIZE],sa[4096],sb[4096];
    for(unsigned cut_at=0;cut_at<=4;cut_at++)for(unsigned byte=0;byte<(cut_at?4097u:1u);byte+=cut_at?(byte<16||byte>4080?1u:7u):1u) {
        flash_model m;fm_init(&m,flash);
        if(old_a){CHECK(pj_prov2_build(old_a,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);}
        if(old_b){CHECK(pj_prov2_build(old_b,sb));fm_put(&m,PJ_PROV_FLASH_B,sb);}
        m.cut_at=cut_at;m.cut_byte=byte;
        pj_prov_flash ops=fm_ops(&m);
        int rc=pj_provision_commit_with(&ops,edit);
        CHECK(!m.touched_outside);
        /* Nothing outside the two sectors changed, ever. */
        for(uint32_t off=0;off<PJ_PROV_FLASH_A;off+=4096)CHECK(flash[off]==0xff&&flash[off+4095]==0xff);
        pj_prov2 got;int have=pj_provision_current(flash+PJ_PROV_FLASH_A,flash+PJ_PROV_FLASH_B,4096,&got);
        int old_present=old_a||old_b;
        pj_prov2 old;if(old_a&&old_b)old=old_a->sequence>=old_b->sequence?*old_a:*old_b;else if(old_a)old=*old_a;else if(old_b)old=*old_b;
        cut_runs++;
        if(!m.cut) {
            uint32_t newseq=old_present?old.sequence+1:1;
            CHECK(rc==PJ_PROV_WRITE_OK&&have&&same_without_sequence(&got,expect)&&got.sequence==newseq);
            /* The old sector is erased: no stale secret survives a completed change. */
            uint32_t other=pj_provision_any(flash+PJ_PROV_FLASH_A,4096,&got)==1&&got.sequence==newseq?PJ_PROV_FLASH_B:PJ_PROV_FLASH_A;
            for(unsigned i=0;i<4096;i++)CHECK(flash[other+i]==0xff);
            CHECK(m.erases>=1&&m.writes==1);
        } else {
            CHECK(rc<0);
            if(have&&same_without_sequence(&got,expect))cut_new++;
            else if(old_present){CHECK(have&&same_without_sequence(&got,&old)&&got.sequence==old.sequence);cut_old++;}
            else CHECK(!have);
            /* Power back: the same edit completes from whatever was left. */
            m.cut=0;m.cut_at=0;
            int again=pj_provision_commit_with(&ops,edit);
            CHECK(again==PJ_PROV_WRITE_OK||again==PJ_PROV_WRITE_NO_CHANGE);
            CHECK(pj_provision_current(flash+PJ_PROV_FLASH_A,flash+PJ_PROV_FLASH_B,4096,&got)&&same_without_sequence(&got,expect));
            CHECK(!m.touched_outside);
        }
    }
}
static void writer_checks(void) {
    pj_prov2 base=sample_record(5,2,1),expect;pj_prov_edit e;
    /* Add a third network. */
    memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Cafe");snprintf(e.net.psk,65,"latte-latte");
    expect=base;expect.count=3;snprintf(expect.net[2].ssid,33,"Cafe");snprintf(expect.net[2].psk,65,"latte-latte");
    commit_under_cuts(&base,&base,&e,&expect);      /* Manager wrote both sectors */
    commit_under_cuts(&base,NULL,&e,&expect);       /* only A */
    commit_under_cuts(NULL,&base,&e,&expect);       /* only B */
    pj_prov2 older=base;older.sequence=4;commit_under_cuts(&older,&base,&e,&expect); /* B newer */
    pj_prov2 empty;memset(&empty,0,sizeof(empty));empty.version=2;expect=empty;expect.count=1;expect.net[0]=e.net;
    commit_under_cuts(NULL,NULL,&e,&expect);        /* nothing yet */
    /* Change a password, forget a network, sign in, forget the sign-in. */
    memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Net1");snprintf(e.net.psk,65,"new-password");
    expect=base;snprintf(expect.net[1].psk,65,"new-password");commit_under_cuts(&base,&base,&e,&expect);
    memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_FORGET;snprintf(e.net.ssid,33,"Net0");
    expect=base;expect.count=1;expect.net[0]=base.net[1];memset(&expect.net[1],0,sizeof(expect.net[1]));commit_under_cuts(&base,&base,&e,&expect);
    /* "Use this network now": the named network moves to the top, the rest keep their order, passwords are the
     * record's own (the edit carries none); already first or unknown: nothing is written. */
    pj_prov2 four=sample_record(9,4,1);
    memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_FIRST;snprintf(e.net.ssid,33,"Net2");
    expect=four;expect.net[0]=four.net[2];expect.net[1]=four.net[0];expect.net[2]=four.net[1];expect.net[3]=four.net[3];
    commit_under_cuts(&four,&four,&e,&expect);
    snprintf(e.net.ssid,33,"Net3");expect=four;expect.net[0]=four.net[3];expect.net[1]=four.net[0];expect.net[2]=four.net[1];expect.net[3]=four.net[2];
    commit_under_cuts(&four,NULL,&e,&expect);
    {
        static uint8_t flash[FLASH_SIZE],sa[4096];flash_model m;fm_init(&m,flash);
        CHECK(pj_prov2_build(&four,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);fm_put(&m,PJ_PROV_FLASH_B,sa);
        pj_prov_flash ops=fm_ops(&m);
        snprintf(e.net.ssid,33,"Net0");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE); /* already first */
        snprintf(e.net.ssid,33,"Nobody");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE);
        snprintf(e.net.psk,65,"not-the-password");snprintf(e.net.ssid,33,"Net1");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_OK);
        pj_prov2 got;CHECK(pj_provision_current(flash+PJ_PROV_FLASH_A,flash+PJ_PROV_FLASH_B,4096,&got)&&!strcmp(got.net[0].ssid,"Net1")&&!strcmp(got.net[0].psk,"password1"));
        CHECK(!strcmp(got.net[1].ssid,"Net0")&&got.count==4&&m.erases==2&&m.writes==1&&!m.touched_outside);
    }
    pj_prov2 plain=sample_record(7,1,0);
    memset(&e,0,sizeof(e));e.set_jellyfin=1;snprintf(e.url,129,"http://nas:8096");snprintf(e.user_id,33,"0123456789abcdef0123456789abcdef");
    snprintf(e.access_token,65,"abcdefghijklmnopqrstuvwxyz012345");snprintf(e.device_id,65,"chromatic-a0a1a2a3a4a5a6a7a8a9aaab");
    expect=plain;expect.has_jellyfin=1;memcpy(expect.url,e.url,129);memcpy(expect.user_id,e.user_id,33);memcpy(expect.access_token,e.access_token,65);memcpy(expect.device_id,e.device_id,65);
    commit_under_cuts(&plain,&plain,&e,&expect);
    memset(&e,0,sizeof(e));e.clear_jellyfin=1;expect=base;expect.has_jellyfin=0;memset(expect.url,0,129);memset(expect.user_id,0,33);memset(expect.access_token,0,65);memset(expect.device_id,0,65);
    commit_under_cuts(&base,&base,&e,&expect);
    /* Launcher 1.3.0 "Find my server again": set_address rewrites ONLY the address of the same sign-in. The token,
     * user and device id stay the record's own (the edit carries none), networks and gateway are untouched, under
     * power cuts at every operation; every layout. */
    memset(&e,0,sizeof(e));e.set_address=1;snprintf(e.url,129,"http://192.0.2.77:8096");snprintf(e.user_id,33,"0123456789abcdef0123456789abcdef");
    expect=base;memset(expect.url,0,129);snprintf(expect.url,129,"http://192.0.2.77:8096");
    commit_under_cuts(&base,&base,&e,&expect);commit_under_cuts(&base,NULL,&e,&expect);commit_under_cuts(NULL,&base,&e,&expect);
    {pj_prov2 old4=base;old4.sequence=4;commit_under_cuts(&old4,&base,&e,&expect);}
    {
        static uint8_t flash[FLASH_SIZE],sa[4096];flash_model m;fm_init(&m,flash);
        CHECK(pj_prov2_build(&base,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);fm_put(&m,PJ_PROV_FLASH_B,sa);
        pj_prov_flash ops=fm_ops(&m);
        /* Refused, nothing written: another user's id, a bad address, an unterminated address, combined with a
         * sign-in change, no sign-in on the console at all. The same address: no change. */
        snprintf(e.user_id,33,"ffffffffffffffffffffffffffffffff");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        snprintf(e.user_id,33,"0123456789abcdef0123456789abcdef");snprintf(e.url,129,"ftp://nas");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        memset(e.url,'a',129);CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        memset(e.url,0,129);snprintf(e.url,129,"http://192.0.2.77:8096");e.clear_jellyfin=1;CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        e.clear_jellyfin=0;e.set_jellyfin=1;CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);e.set_jellyfin=0;
        memset(e.url,0,129);snprintf(e.url,129,"http://192.0.2.20:8096");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE);
        CHECK(m.erases==0&&m.writes==0&&!memcmp(flash+PJ_PROV_FLASH_A,sa,4096)&&!memcmp(flash+PJ_PROV_FLASH_B,sa,4096));
        pj_prov2 nojf=sample_record(5,2,0);CHECK(pj_prov2_build(&nojf,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);fm_put(&m,PJ_PROV_FLASH_B,sa);
        memset(e.url,0,129);snprintf(e.url,129,"http://192.0.2.77:8096");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        CHECK(m.erases==0&&m.writes==0&&!m.touched_outside);
        /* Done for real: the new address, the old token, old sector erased. */
        CHECK(pj_prov2_build(&base,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);fm_put(&m,PJ_PROV_FLASH_B,sa);
        CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_OK);
        pj_prov2 got;CHECK(pj_provision_current(flash+PJ_PROV_FLASH_A,flash+PJ_PROV_FLASH_B,4096,&got)&&got.sequence==6);
        CHECK(!strcmp(got.url,"http://192.0.2.77:8096")&&!strcmp(got.access_token,"tokentokentokentoken")&&!strcmp(got.device_id,base.device_id)&&!strcmp(got.user_id,base.user_id));
        CHECK(got.count==2&&got.has_gateway&&!memcmp(got.chr1_key,base.chr1_key,32)&&m.erases==2&&m.writes==1&&!m.touched_outside);
    }
    /* A version-1 sector in B (sequence 0) is carried into version 2 with the gateway kept. */
    {
        static uint8_t flash[FLASH_SIZE],v1[4096];flash_model m;fm_init(&m,flash);
        memset(v1,0xff,4096);memset(v1,0,PJ_PROVISION_SIZE);memcpy(v1,"PJPV",4);v1[4]=1;v1[6]=(uint8_t)PJ_PROVISION_BODY;v1[7]=(uint8_t)(PJ_PROVISION_BODY>>8);
        memcpy(v1+8,"OldHome",7);memcpy(v1+41,"oldpassword",11);v1[106]=192;v1[107]=168;v1[108]=0;v1[109]=2;v1[110]=0x5c;v1[111]=0x11;
        memset(v1+112,0x44,96);uint32_t crc=pj_provision_crc32(v1,PJ_PROVISION_BODY);pj_prov_put32(v1+PJ_PROVISION_BODY,crc);
        fm_put(&m,PJ_PROV_FLASH_B,v1);
        memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Cafe");snprintf(e.net.psk,65,"latte-latte");
        pj_prov_flash ops=fm_ops(&m);CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_OK);
        pj_prov2 got;CHECK(pj_provision_current(flash+PJ_PROV_FLASH_A,flash+PJ_PROV_FLASH_B,4096,&got)&&got.version==2&&got.sequence==1&&got.count==2);
        CHECK(!strcmp(got.net[0].ssid,"OldHome")&&!strcmp(got.net[1].ssid,"Cafe")&&got.has_gateway&&got.gateway_port==0x115c&&!got.has_jellyfin);
        for(unsigned i=0;i<4096;i++)CHECK(flash[PJ_PROV_FLASH_B+i]==0xff); /* the v1 copy is gone */
        CHECK(!m.touched_outside);
    }
    /* Refusals write nothing: a fifth network, a bad password, a bad token, a read failure, no change. */
    {
        static uint8_t flash[FLASH_SIZE],sa[4096];flash_model m;fm_init(&m,flash);
        pj_prov2 full=sample_record(3,4,0);CHECK(pj_prov2_build(&full,sa));fm_put(&m,PJ_PROV_FLASH_A,sa);fm_put(&m,PJ_PROV_FLASH_B,sa);
        pj_prov_flash ops=fm_ops(&m);
        memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Fifth");snprintf(e.net.psk,65,"password5");
        CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        snprintf(e.net.ssid,33,"Net0");snprintf(e.net.psk,65,"short");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        memset(&e,0,sizeof(e));e.set_jellyfin=1;snprintf(e.url,129,"http://nas");snprintf(e.user_id,33,"nothex");snprintf(e.access_token,65,"abcdefghijklmnopqrstuvwxyz");snprintf(e.device_id,65,"d");
        CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Net0");snprintf(e.net.psk,65,"password0");
        CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE);
        memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_FORGET;snprintf(e.net.ssid,33,"Nobody");CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE);
        memset(&e,0,sizeof(e));CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_NO_CHANGE);
        memset(&e,0,sizeof(e));e.net_op=7;CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_INVALID);
        CHECK(m.erases==0&&m.writes==0&&!m.touched_outside&&!memcmp(flash+PJ_PROV_FLASH_A,sa,4096)&&!memcmp(flash+PJ_PROV_FLASH_B,sa,4096));
        pj_prov_flash broken=ops;broken.read=NULL;
        /* A read failure stops before any write. */
        struct {int (*read)(void *,uint32_t,void *,uint32_t);} dummy;(void)dummy;
        m.cut=0;
        pj_prov_flash failing=ops;failing.read=fm_read;
        (void)failing;(void)broken;
    }
    /* The writer refuses to touch any other address even if asked: the device wrappers check first.
     * (Host side: the model counts; the device side is the same pj_pw_allowed in provision_write.c.) */
}
static int fail_read(void *ctx,uint32_t off,void *out,uint32_t n){(void)ctx;(void)off;(void)out;(void)n;return -1;}
static void writer_read_failure(void) {
    static uint8_t flash[FLASH_SIZE];flash_model m;fm_init(&m,flash);
    pj_prov_flash ops=fm_ops(&m);ops.read=fail_read;
    pj_prov_edit e;memset(&e,0,sizeof(e));e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"X");snprintf(e.net.psk,65,"password0");
    CHECK(pj_provision_commit_with(&ops,&e)==PJ_PROV_WRITE_READ_FAILED&&m.erases==0&&m.writes==0);
    CHECK(pj_provision_commit_with(NULL,&e)==PJ_PROV_WRITE_READ_FAILED);
}
/* ---- the screens, played against a modeled binding ---- */
static pj_setup su;
static unsigned commits,logouts,logouts_old;static int next_commit_rc=PJ_PROV_WRITE_OK;
/* Find my server again (1.3.0): what discovery answers (NULL: nobody), and the server's answer to GET /Users/<id>. */
static const char *discover_url="http://192.0.2.20:8096";static int verify_status=200;static unsigned verifies;static pj_prov_edit last_edit;
/* 1.3.1: what GET /System/Info/Public answers (the server's own Id, name and version); infos counts those requests. */
static const char *info_id="f00dfeed-f00d-feed-f00d-feedf00dfeed";static unsigned infos;
static const char *verify_body="{\"Name\":\"owner\",\"Id\":\"01234567-89AB-cdef-0123-456789abcdef\",\"Policy\":{\"IsAdministrator\":true}}";
static void press(uint16_t b,uint32_t now){pj_setup_press(&su,b,now,fixed_random);}
static void settle(uint32_t now) { /* the binding: scan, discovery and HTTP answered at once */
    for(unsigned guard=0;guard<8;guard++) {
        pj_setup_tick(&su,now);
        int r=pj_setup_take_request(&su);
        if(r==PJ_SU_REQ_NONE)return;
        if(r==PJ_SU_REQ_SCAN){pj_scan_add(&su.scan,(const uint8_t *)"Home",4,-50,1);pj_scan_add(&su.scan,(const uint8_t *)"Cafe-open",9,-70,0);
            pj_scan_add(&su.scan,(const uint8_t *)"Neighbour 5G",12,-85,1);pj_setup_scan_done(&su,1);}
        else if(r==PJ_SU_REQ_DISCOVER) {
            char rep[160];snprintf(rep,sizeof(rep),"{\"Address\":\"%s\",\"Id\":\"f00dfeedf00dfeedf00dfeedf00dfeed\",\"Name\":\"Living room\"}",discover_url?discover_url:"");
            if(discover_url)pj_discovery_add(&su.servers,rep,strlen(rep));
            pj_setup_discover_done(&su);
        }
        else if(r==PJ_SU_REQ_COMMIT){commits++;last_edit=su.edit;pj_setup_commit_done(&su,next_commit_rc);}
        else if(r==PJ_SU_REQ_HTTP) {
            const char *p=su.http.path;int status=200;const char *body="";
            if(!strcmp(p,"/QuickConnect/Enabled"))body="true";
            else if(!strcmp(p,"/QuickConnect/Initiate"))body="{\"Code\":\"482913\",\"Secret\":\"SECRETSECRETSECRET\"}";
            else if(!strncmp(p,"/QuickConnect/Connect",21))body="{\"Authenticated\":true}";
            else if(!strcmp(p,"/Users/AuthenticateWithQuickConnect"))body="{\"User\":{\"Id\":\"0123456789abcdef0123456789abcdef\"},\"AccessToken\":\"NEWTOKENNEWTOKENNEWTOKEN\"}";
            else if(!strcmp(p,"/Sessions/Logout")){status=204;CHECK(su.http.with_token);logouts++;if(!strcmp(su.qc.token,"OLDTOKENOLDTOKENOLDTOKEN"))logouts_old++;}
            else if(!strcmp(p,"/System/Info/Public")) { /* 1.3.1: who is it? never with a token */
                CHECK(!strcmp(su.http.method,"GET")&&!su.http.with_token&&!su.qc.token[0]);infos++;
                static char info[200];snprintf(info,sizeof(info),"{\"Id\":\"%s\",\"ServerName\":\"Living room\",\"Version\":\"10.10.3\",\"ProductName\":\"Jellyfin Server\"}",info_id);
                body=info;
            }
            else if(!strncmp(p,"/Users/",7)) { /* Find my server again: one GET of this console's own user, with its own token */
                CHECK(!strcmp(su.http.method,"GET")&&su.http.with_token&&!su.http.body[0]&&!strcmp(p+7,"0123456789abcdef0123456789abcdef"));
                CHECK(!strcmp(su.qc.token,"OLDTOKENOLDTOKENOLDTOKEN")&&strcmp(su.qc.url,su.account.url));
                verifies++;status=verify_status;body=verify_status>0?verify_body:"";
            }
            else CHECK(0);
            pj_setup_http_done(&su,status,body,strlen(body),now);
        }
    }
    CHECK(0);
}
static void draw(const char *name,int watched,uint32_t now){pj_setup_draw(screen,&su,watched,now,pj_font_alpha,pj_font_advance);write_ppm(name);}
static void setup_checks(void) {
    const char *names[2]={"Home","Office"};pj_setup_account acct;memset(&acct,0,sizeof(acct));
    snprintf(acct.url,129,"http://192.0.2.20:8096");snprintf(acct.device_id,65,"chromatic-old");snprintf(acct.token,65,"OLDTOKENOLDTOKENOLDTOKEN");
    uint32_t now=5000;
    /* Wi-Fi: add a secured network from the scan, with the keyboard. */
    pj_setup_open(&su,names,2,0,NULL,1,"1.2.7");CHECK(su.screen==PJ_SU_ROOT&&su.net_count==2);
    draw("root",0,now);
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI&&pj_setup_rows(&su)==3);draw("wifi",0,now);
    press(PJ_BTN_UP,now);CHECK(su.focus==2);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_BUSY&&su.request==PJ_SU_REQ_SCAN);draw("busy-scan",0,now);
    settle(now);CHECK(su.screen==PJ_SU_SCAN&&su.scan.count==3&&su.scan.net[0].saved);draw("scan",0,now);
    press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);CHECK(su.focus==2);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_PASSWORD&&su.kb.secret);
    for(const char *c="neighbours";*c;c++)pj_kb_type(&su.kb,*c);
    draw("password",0,now);CHECK(!screen_shows("neighbours"));
    press(PJ_BTN_START,now);CHECK(su.screen==PJ_SU_BUSY&&su.request==PJ_SU_REQ_COMMIT&&su.edit.net_op==PJ_PROV_NET_SAVE&&!strcmp(su.edit.net.ssid,"Neighbour 5G")&&!strcmp(su.edit.net.psk,"neighbours"));
    draw("busy-save",0,now);CHECK(screen_shows("Do not switch off."));
    settle(now);CHECK(su.screen==PJ_SU_MESSAGE&&commits==1&&su.net_count==3&&!strcmp(su.ssid[2],"Neighbour 5G")&&su.wifi_changed&&!su.edit.net.psk[0]&&!su.kb.len);
    draw("saved",0,now);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* An open network saves straight away; a saved network from the scan goes to the keyboard too. */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);CHECK(su.focus==3);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_SCAN);
    press(PJ_BTN_DOWN,now);CHECK(su.focus==1);press(PJ_BTN_A,now);CHECK(su.request==PJ_SU_REQ_COMMIT&&!su.edit.net.psk[0]&&!strcmp(su.edit.net.ssid,"Cafe-open"));
    settle(now);CHECK(su.net_count==4);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI&&pj_setup_rows(&su)==4);draw("wifi-full",0,now);
    /* A fifth network is refused by the screen before any write. */
    su.net_count=4;pj_setup_go(&su,PJ_SU_SCAN);pj_scan_add(&su.scan,(const uint8_t *)"Fifth",5,-60,1);const char *n4[4]={su.ssid[0],su.ssid[1],su.ssid[2],su.ssid[3]};pj_scan_mark_saved(&su.scan,n4,4);
    for(unsigned i=0;i<su.scan.count;i++)if(!strcmp(su.scan.net[i].ssid,"Fifth"))su.focus=i;
    unsigned before=commits;press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_MESSAGE&&commits==before);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_SCAN);
    /* Change a password; forget a network (with confirmation, Keep first does nothing). */
    pj_setup_go(&su,PJ_SU_WIFI);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI_NET&&su.chosen==0&&pj_setup_rows(&su)==3);draw("network",0,now);
    press(PJ_BTN_DOWN,now);CHECK(su.focus==1);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_PASSWORD&&su.after==PJ_SU_WIFI_NET);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI_NET); /* B on empty: back */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);for(const char *c="newpassword";*c;c++)pj_kb_type(&su.kb,*c);press(PJ_BTN_START,now);
    CHECK(su.edit.net_op==PJ_PROV_NET_SAVE&&!strcmp(su.edit.net.ssid,"Home")&&!strcmp(su.edit.net.psk,"newpassword"));settle(now);CHECK(su.net_count==4);
    press(PJ_BTN_A,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI_NET);press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI_FORGET);draw("forget",0,now);
    press(PJ_BTN_DOWN,now);before=commits;press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI_NET&&commits==before); /* Keep */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);press(PJ_BTN_A,now);CHECK(su.edit.net_op==PJ_PROV_NET_FORGET&&!strcmp(su.edit.net.ssid,"Home"));
    settle(now);CHECK(su.net_count==3&&!strcmp(su.ssid[0],"Office"));press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* Use this network now (1.2.9): the console is on Office (marked in the list); choosing the second network commits
     * one PJ_PROV_NET_FIRST edit, the list reorders, the shell is asked to rejoin once, and the Switching screen follows
     * the shell's join: joining, then connected (green) or what went wrong with what to do about it. */
    pj_setup_set_connected(&su,"Office");CHECK(su.connected==0);press(PJ_BTN_DOWN,now);draw("wifi-connected",0,now);CHECK(screen_shows("connected"));
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI_NET&&su.chosen==1);draw("network-use",0,now);
    before=commits;press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_BUSY&&su.request==PJ_SU_REQ_COMMIT&&su.edit.net_op==PJ_PROV_NET_FIRST&&!su.edit.net.psk[0]);
    CHECK(!strcmp(su.edit.net.ssid,su.switch_name)&&strcmp(su.switch_name,"Office"));draw("busy-switch",0,now);CHECK(screen_shows("Do not switch off."));
    settle(now);CHECK(commits==before+1&&su.screen==PJ_SU_SWITCH&&su.switch_state==2&&su.wifi_changed&&su.connected==-1);
    CHECK(pj_setup_take_switch(&su)&&!pj_setup_take_switch(&su)); /* the shell takes the request once */
    CHECK(!strcmp(su.ssid[0],su.switch_name)&&!strcmp(su.ssid[1],"Office")&&su.net_count==3);
    draw("switching-joining",0,now);CHECK(screen_shows("Saved as first choice"));
    pj_setup_join_state(&su,PJ_SU_JOIN_JOINING,0);CHECK(su.switch_state==2);
    pj_setup_join_state(&su,PJ_SU_JOIN_FAILED,PJ_JOIN_WRONG_PASSWORD);CHECK(su.switch_state==4&&su.switch_fail==PJ_JOIN_WRONG_PASSWORD);
    draw("switching-wrong-password",0,now);CHECK(screen_shows("The password is wrong"));
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_PASSWORD&&su.chosen==0&&su.kb.secret); /* A: straight to the password of that network (first now) */
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI_NET);
    press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_SWITCH&&su.switch_state==2&&pj_setup_take_switch(&su));
    pj_setup_join_state(&su,PJ_SU_JOIN_CONNECTED,0);pj_setup_set_connected(&su,su.switch_name); /* the binding names the network it joined */
    CHECK(su.switch_state==3&&su.connected==0);draw("switching-connected",0,now);CHECK(screen_shows("Connected"));
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI&&su.switch_state==0);
    /* B while joining leaves the screen; the join goes on in the shell and a later result has no screen to change. */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.chosen==1);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_SWITCH&&pj_setup_take_switch(&su));
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI&&su.switch_state==0);
    pj_setup_join_state(&su,PJ_SU_JOIN_FAILED,PJ_JOIN_NOT_FOUND);CHECK(su.switch_state==0);
    /* Not in range: it stays first and joins by itself later. */
    pj_setup_go(&su,PJ_SU_WIFI_NET);su.chosen=1;press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_SWITCH&&pj_setup_take_switch(&su));
    pj_setup_join_state(&su,PJ_SU_JOIN_FAILED,PJ_JOIN_NOT_FOUND);CHECK(su.switch_state==4);draw("switching-not-found",0,now);CHECK(screen_shows("The network was not found"));
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_WIFI);
    /* Already on it: a message, nothing written. */
    pj_setup_set_connected(&su,su.ssid[0]);pj_setup_go(&su,PJ_SU_WIFI_NET);su.chosen=0;before=commits;press(PJ_BTN_A,now);
    CHECK(su.screen==PJ_SU_MESSAGE&&commits==before);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI_NET);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* Forgetting the connected network clears the mark; forgetting another keeps it right. */
    pj_setup_set_connected(&su,su.ssid[1]);CHECK(su.connected==1);
    pj_setup_go(&su,PJ_SU_WIFI_NET);su.chosen=0;press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);press(PJ_BTN_A,now);settle(now);CHECK(su.net_count==2&&su.connected==0);
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* A failed switch commit goes back to that network's screen with the old list. */
    next_commit_rc=PJ_PROV_WRITE_WRITE_FAILED;pj_setup_set_connected(&su,NULL);pj_setup_go(&su,PJ_SU_WIFI_NET);su.chosen=1;press(PJ_BTN_A,now);settle(now);
    CHECK(su.screen==PJ_SU_MESSAGE&&su.after==PJ_SU_WIFI_NET&&su.net_count==2&&su.connected==-1);next_commit_rc=PJ_PROV_WRITE_OK;press(PJ_BTN_B,now);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* Back to a three-network list for the checks below (the names only matter to the scan). */
    pj_scan_reset(&su.scan);pj_setup_go(&su,PJ_SU_SCAN);pj_scan_add(&su.scan,(const uint8_t *)"Neighbour 5G",12,-85,1);su.focus=0;press(PJ_BTN_A,now);
    for(const char *c="neighbours";*c;c++)pj_kb_type(&su.kb,*c);
    press(PJ_BTN_START,now);settle(now);CHECK(su.net_count==3);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI);
    /* A failed write keeps the list and says so. */
    next_commit_rc=PJ_PROV_WRITE_WRITE_FAILED;press(PJ_BTN_UP,now);press(PJ_BTN_A,now);settle(now);press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);
    CHECK(su.screen==PJ_SU_PASSWORD);for(const char *c="password";*c;c++)pj_kb_type(&su.kb,*c);press(PJ_BTN_START,now);settle(now);
    CHECK(su.screen==PJ_SU_MESSAGE&&su.net_count==3&&screen_shows("")==0);draw("save-failed",0,now);CHECK(screen_shows("Saving did not work."));next_commit_rc=PJ_PROV_WRITE_OK;
    press(PJ_BTN_B,now);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_ROOT);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_CLOSED);
    /* Jellyfin: discovery, Quick Connect, then the sign-in is stored and handed to the shell once. */
    commits=0;pj_setup_open(&su,names,2,0,NULL,1,"1.2.7");press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF);draw("jellyfin",0,now);
    press(PJ_BTN_A,now);CHECK(su.request==PJ_SU_REQ_DISCOVER);settle(now);CHECK(su.screen==PJ_SU_JF_SERVERS&&su.servers.count==1);draw("servers",0,now);
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF_CODE&&su.qc.state==PJ_QC_ENABLED);draw("code-asking",0,now);
    /* Step the protocol one request at a time, drawing the code while waiting. */
    pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_HTTP);pj_setup_http_done(&su,200,"true",4,now);
    pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_HTTP&&!strcmp(su.http.path,"/QuickConnect/Initiate"));
    const char *init="{\"Code\":\"482913\",\"Secret\":\"SECRETSECRETSECRET\"}";pj_setup_http_done(&su,200,init,strlen(init),now);
    CHECK(su.qc.state==PJ_QC_WAIT);draw("code",0,now);CHECK(screen_shows("Your code:")&&!screen_shows("SECRETSECRETSECRET"));
    pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_NONE); /* poll not due yet */
    now+=PJ_QC_POLL_MS;settle(now);
    CHECK(su.screen==PJ_SU_MESSAGE&&commits==1&&su.jellyfin_linked&&su.jellyfin_changed&&su.has_fresh&&!su.qc.token[0]&&!su.edit.access_token[0]);
    draw("signed-in",0,now);CHECK(!screen_shows("NEWTOKENNEWTOKENNEWTOKEN"));
    pj_setup_account fresh;CHECK(pj_setup_take_fresh(&su,&fresh)&&!strcmp(fresh.token,"NEWTOKENNEWTOKENNEWTOKEN")&&!strcmp(fresh.user_id,"0123456789abcdef0123456789abcdef")&&!strcmp(fresh.url,"http://192.0.2.20:8096")&&!strncmp(fresh.device_id,"chromatic-a0a1",14));
    CHECK(!pj_setup_take_fresh(&su,&fresh)&&!su.fresh.token[0]);
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF);draw("jellyfin-linked",0,now);
    /* Forget: one Logout as this console, then the record loses the sign-in. */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF_FORGET);draw("forget-signin",0,now);
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_BUSY&&su.logout_pending);
    pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_HTTP&&!strcmp(su.http.path,"/Sessions/Logout")&&su.http.with_token&&!strcmp(su.qc.token,"NEWTOKENNEWTOKENNEWTOKEN"));
    pj_setup_http_done(&su,204,"",0,now);settle(now);CHECK(su.screen==PJ_SU_MESSAGE&&commits==2&&!su.jellyfin_linked&&!su.account.token[0]);
    draw("signed-out",0,now);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_JF&&!su.jellyfin_linked);
    /* Manual address: a bad one is refused on the console; a good one starts Quick Connect. */
    press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF_ADDRESS&&!su.kb.secret&&!strcmp(su.kb.text,"http://"));
    pj_kb_type(&su.kb,' ');press(PJ_BTN_START,now);CHECK(su.screen==PJ_SU_MESSAGE);draw("bad-address",0,now);press(PJ_BTN_B,now);
    press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);for(const char *c="nas.local:8096";*c;c++)pj_kb_type(&su.kb,*c);press(PJ_BTN_START,now);
    CHECK(su.screen==PJ_SU_JF_CODE&&!strcmp(su.qc.url,"http://nas.local:8096"));
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_JF&&su.qc.state==PJ_QC_IDLE); /* cancel wipes the attempt */
    /* Quick Connect off on the server: a plain sentence. Unreachable: another. */
    CHECK(su.focus==0);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_JF_SERVERS);press(PJ_BTN_A,now);pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_HTTP);
    pj_setup_http_done(&su,200,"false",5,now);pj_setup_tick(&su,now);CHECK(su.screen==PJ_SU_MESSAGE);draw("qc-off",0,now);CHECK(screen_shows("Quick Connect is off on the server"));
    press(PJ_BTN_B,now);press(PJ_BTN_A,now);settle(now);press(PJ_BTN_A,now);pj_setup_tick(&su,now);pj_setup_take_request(&su);
    pj_setup_http_done(&su,0,NULL,0,now);pj_setup_tick(&su,now);CHECK(su.screen==PJ_SU_MESSAGE&&screen_shows("")==0);
    /* No Wi-Fi: Jellyfin setup explains instead of trying. Re-link logs the old console session out. */
    pj_setup_open(&su,names,2,1,&acct,0,"1.2.7");press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);draw("jellyfin-no-wifi",0,now);
    press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_MESSAGE&&su.request==PJ_SU_REQ_NONE);
    pj_setup_open(&su,names,2,1,&acct,1,"1.2.7");press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);press(PJ_BTN_A,now);settle(now);press(PJ_BTN_A,now);
    commits=0;logouts=logouts_old=0;settle(now);CHECK(su.qc.state==PJ_QC_WAIT);now+=PJ_QC_POLL_MS;settle(now);
    CHECK(commits==1&&logouts==1&&logouts_old==1&&su.screen==PJ_SU_MESSAGE&&!su.logout_pending&&!strcmp(su.account.token,"NEWTOKENNEWTOKENNEWTOKEN")&&!su.qc.token[0]);
    /* Find my server again (1.3.0): the server moved. From the Jellyfin app's error screen (from_app):
     * discovery, the owner picks it, one GET checks it knows this console, then ONLY the address is saved; the token
     * stays, nothing is logged out, and the end goes back to Jellyfin. */
    {
        pj_setup_account mine=acct;snprintf(mine.user_id,33,"0123456789abcdef0123456789abcdef");
        discover_url="http://192.0.2.77:8096";verify_status=200;commits=0;logouts=0;verifies=0;
        pj_setup_open(&su,names,2,1,&mine,1,"1.3.0");pj_setup_find_begin(&su,1);
        CHECK(su.screen==PJ_SU_BUSY&&su.request==PJ_SU_REQ_DISCOVER&&su.find_discover);draw("find-looking",0,now);CHECK(screen_shows("Looking for your server..."));
        settle(now);CHECK(su.screen==PJ_SU_FIND&&su.servers.count==1&&pj_setup_rows(&su)==3&&!su.find_discover);
        draw("find-servers",0,now);CHECK(screen_shows("Look again")&&screen_shows("Enter the address")&&screen_shows("B: Jellyfin"));
        infos=0;press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_BUSY&&su.find_verify&&su.request==PJ_SU_REQ_NONE&&!su.qc.token[0]);
        draw("find-asking",0,now);CHECK(screen_shows("Asking that server..."));
        /* 1.3.1: it is asked who it is, without a token; its Id equals what discovery heard; the owner sees its
         * name and version and says yes before any token leaves the console. */
        settle(now);CHECK(infos==1&&verifies==0&&su.screen==PJ_SU_FIND_CONFIRM&&!strcmp(su.find_name,"Living room")&&!strcmp(su.find_version,"10.10.3"));
        draw("find-confirm",0,now);CHECK(screen_shows("Living room")&&screen_shows("Jellyfin 10.10.3")&&screen_shows("No, look again"));
        press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_BUSY&&su.find_verify);draw("find-checking",0,now);CHECK(screen_shows("Checking it is yours..."));
        settle(now);
        CHECK(verifies==1&&commits==1&&logouts==0&&last_edit.set_address&&!last_edit.set_jellyfin&&!last_edit.clear_jellyfin&&last_edit.net_op==PJ_PROV_NET_KEEP);
        CHECK(!last_edit.access_token[0]&&!last_edit.device_id[0]&&!strcmp(last_edit.url,"http://192.0.2.77:8096")&&!strcmp(last_edit.user_id,mine.user_id));
        CHECK(su.screen==PJ_SU_MESSAGE&&su.after==PJ_SU_CLOSED&&su.jellyfin_changed&&su.has_fresh&&!su.qc.token[0]&&!su.edit.url[0]);
        draw("find-saved",0,now);CHECK(screen_shows("Found your server.")&&screen_shows("Its new address is saved.")&&!screen_shows("OLDTOKENOLDTOKENOLDTOKEN"));
        pj_setup_account f;CHECK(pj_setup_take_fresh(&su,&f)&&!strcmp(f.url,"http://192.0.2.77:8096")&&!strcmp(f.token,"OLDTOKENOLDTOKENOLDTOKEN")&&!strcmp(f.user_id,mine.user_id)&&!strcmp(f.device_id,"chromatic-old"));
        press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_CLOSED&&!su.account.token[0]);
        /* Answers that are not ours write nothing and say so: 401, 200 for another user, no answer, an odd reply. */
        static const int statuses[4]={401,200,0,500};static const char *const words[4]={"That server does not know","That server does not know","That server did not answer.","That server gave an"};
        for(unsigned k=0;k<4;k++) {
            pj_setup_open(&su,names,2,1,&mine,1,"1.3.0");pj_setup_find_begin(&su,1);commits=0;verifies=0;verify_status=statuses[k];
            if(k==1)verify_body="{\"Id\":\"ffffffffffffffffffffffffffffffff\"}";
            settle(now);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_FIND_CONFIRM);press(PJ_BTN_A,now);settle(now);
            CHECK(verifies==1&&commits==0&&su.screen==PJ_SU_MESSAGE&&su.after==PJ_SU_FIND&&!su.qc.token[0]&&!su.has_fresh&&!su.jellyfin_changed);
            if(k==0)draw("find-not-ours",0,now);else if(k==2)draw("find-no-answer",0,now);
            pj_setup_draw(screen,&su,0,now,pj_font_alpha,pj_font_advance);CHECK(screen_shows(words[k]));
            press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_FIND);
            verify_body="{\"Name\":\"owner\",\"Id\":\"01234567-89AB-cdef-0123-456789abcdef\",\"Policy\":{\"IsAdministrator\":true}}";
        }
        /* A failed write keeps the old address and says so; back on the list. */
        verify_status=200;next_commit_rc=PJ_PROV_WRITE_WRITE_FAILED;commits=0;press(PJ_BTN_A,now);settle(now);press(PJ_BTN_A,now);settle(now);
        CHECK(commits==1&&su.screen==PJ_SU_MESSAGE&&su.after==PJ_SU_FIND&&!su.has_fresh);next_commit_rc=PJ_PROV_WRITE_OK;press(PJ_BTN_A,now);
        /* The only server that answers is at the saved address: no request, a plain next step. */
        discover_url=mine.url;press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_FIND);
        verifies=0;press(PJ_BTN_A,now);settle(now);CHECK(verifies==0&&su.screen==PJ_SU_MESSAGE);draw("find-same-address",0,now);CHECK(screen_shows("That address is the one"));
        /* Nobody answers: the Find screen says so with the next steps; the address can be typed and is checked the same way. */
        press(PJ_BTN_A,now);discover_url=NULL;press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);settle(now);
        CHECK(su.screen==PJ_SU_FIND&&su.servers.count==0&&pj_setup_rows(&su)==2&&su.focus==0);draw("find-none",0,now);
        CHECK(screen_shows("Your server was not found"));CHECK(screen_shows("Enter the address"));
        press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF_ADDRESS&&su.after==PJ_SU_FIND);
        press(PJ_BTN_B,now);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_JF_ADDRESS); /* B deletes in the keyboard */
        pj_setup_go(&su,PJ_SU_FIND);su.focus=1;press(PJ_BTN_A,now);
        for(const char *c="192.0.2.88:8096";*c;c++)pj_kb_type(&su.kb,*c);
        commits=0;verifies=0;press(PJ_BTN_START,now);CHECK(su.screen==PJ_SU_BUSY&&su.find_verify&&!strcmp(su.qc.url,"http://192.0.2.88:8096")&&!su.qc.expect_id[0]);
        settle(now);CHECK(su.screen==PJ_SU_FIND_CONFIRM&&verifies==0);press(PJ_BTN_A,now); /* typed: no discovery Id, the owner's yes */
        settle(now);CHECK(verifies==1&&commits==1&&!strcmp(last_edit.url,"http://192.0.2.88:8096")&&last_edit.set_address);
        press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_CLOSED);
        /* B on the list goes straight back to Jellyfin when the app sent us. */
        discover_url="http://192.0.2.77:8096";pj_setup_open(&su,names,2,1,&mine,1,"1.3.0");pj_setup_find_begin(&su,1);settle(now);
        press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_CLOSED&&commits==1);
        /* From Setup's Jellyfin screen: the third row; B returns there. */
        pj_setup_open(&su,names,2,1,&mine,1,"1.3.0");press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);CHECK(su.screen==PJ_SU_JF&&pj_setup_rows(&su)==3);
        press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);CHECK(su.focus==2);draw("jellyfin-linked-find",0,now);
        press(PJ_BTN_A,now);CHECK(su.request==PJ_SU_REQ_DISCOVER&&!su.find_from_app);settle(now);CHECK(su.screen==PJ_SU_FIND);
        draw("find-from-setup",0,now);CHECK(screen_shows("B: back"));press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_JF);
        /* The ordinary "Find servers nearby" still goes to Quick Connect's list, not Find. */
        pj_setup_open(&su,names,2,0,NULL,1,"1.3.0");press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_JF_SERVERS);
        /* No Wi-Fi, or no sign-in on the console: plain words, no request. */
        pj_setup_open(&su,names,2,1,&mine,0,"1.3.0");pj_setup_find_begin(&su,1);CHECK(su.screen==PJ_SU_MESSAGE&&su.request==PJ_SU_REQ_NONE&&su.after==PJ_SU_CLOSED);
        draw("find-no-wifi",0,now);CHECK(screen_shows("Wi-Fi is not connected."));
        pj_setup_open(&su,names,2,0,NULL,1,"1.3.0");pj_setup_find_begin(&su,1);CHECK(su.screen==PJ_SU_MESSAGE&&su.request==PJ_SU_REQ_NONE&&su.after==PJ_SU_JF);
        /* 1.3.1: the address answers with another server's Id than discovery heard: nothing sent, plain words. "No, look
         * again" and B on the confirmation send no token either. */
        pj_setup_open(&su,names,2,1,&mine,1,"1.3.1");pj_setup_find_begin(&su,1);settle(now);
        info_id="ffffffffffffffffffffffffffffffff";verifies=0;infos=0;press(PJ_BTN_A,now);settle(now);
        CHECK(infos==1&&verifies==0&&su.screen==PJ_SU_MESSAGE&&su.after==PJ_SU_FIND);draw("find-not-same",0,now);CHECK(screen_shows("That address answers as"));
        press(PJ_BTN_A,now);info_id="f00dfeed-f00d-feed-f00d-feedf00dfeed";
        press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_FIND_CONFIRM);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);
        CHECK(su.screen==PJ_SU_FIND&&verifies==0&&!su.qc.token[0]);
        press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_FIND_CONFIRM);press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_FIND&&verifies==0&&!su.qc.token[0]);
        CHECK(logouts==0); /* never a sign-out, of this console or anyone else */
        discover_url="http://192.0.2.20:8096";
    }
    /* Not on any network: a saved network is tried at once, with the result in plain words. */
    pj_setup_open(&su,names,2,0,NULL,0,"1.2.9");press(PJ_BTN_A,now);press(PJ_BTN_UP,now);press(PJ_BTN_A,now);settle(now);CHECK(su.screen==PJ_SU_SCAN);
    press(PJ_BTN_DOWN,now);press(PJ_BTN_DOWN,now);press(PJ_BTN_A,now);for(const char *c="neighbours";*c;c++)pj_kb_type(&su.kb,*c);
    press(PJ_BTN_START,now);settle(now);
    CHECK(su.screen==PJ_SU_SWITCH&&su.switch_state==2&&su.switch_saved&&pj_setup_take_switch(&su)&&!strcmp(su.switch_name,"Neighbour 5G"));
    draw("saved-joining",0,now);CHECK(screen_shows("Saved")&&!screen_shows("Saved as first choice"));
    pj_setup_join_state(&su,PJ_SU_JOIN_FAILED,PJ_JOIN_NO_ADDRESS);CHECK(su.switch_state==4);draw("saved-no-address",0,now);CHECK(screen_shows("The network gave no address"));
    press(PJ_BTN_B,now);CHECK(su.screen==PJ_SU_WIFI&&su.net_count==3);
    /* Nothing in the Wi-Fi or Jellyfin screens reaches a request while the setup is closed. */
    pj_setup_close(&su);press(PJ_BTN_A,now);pj_setup_tick(&su,now);CHECK(pj_setup_take_request(&su)==PJ_SU_REQ_NONE&&!su.account.token[0]);
    /* Every fixed text fits. */
    const char *texts[]={"Wi-Fi networks","Jellyfin server","Signed in on this console","Not signed in yet","Add a network...","Four saved: forget one to add",
        "Hotel sign-in Wi-Fi: use a hotspot","Change password","Forget this network","Its password is erased here.","Only this console signs out.",
        "Your other devices stay in.","Choose a network","Scan again","No network heard","Sign in again","Forget this sign-in","Find servers nearby","Enter server address",
        "Sign in with Quick Connect: a","code you approve in any of your","Jellyfin apps. No password here.","Revoke it any time in Jellyfin:","Dashboard > Devices > Chromatic",
        "Wi-Fi is not connected yet.","Open Jellyfin once to join;","then come back here.","Servers found","Enter address instead","Quick Connect","In any of your Jellyfin apps:",
        "Settings > Quick Connect, enter","the code and approve it.","Approved, signing in...","Asking the server...","Looking for servers...","Do not switch off.",
        "Saved.","To switch to it now: open it,","then Use this network now.","The console joins it as soon","as Setup closes.","Forgotten.",
        "Use this network now","Already on this network.","Switching network","Usually 2 to 15 seconds.","B: back (it keeps joining)",
        "Jellyfin opens when you leave","Setup.","Check the password and try again.","A: change password    B: back",
        "It stays first: the console joins","it by itself when it is in range.","Top of the list joins first","Connected to this network now.",
        "Switches right away, no restart;","it also joins first from now on.","Switching...","Joining network","It stays saved: the console joins","Signed in.","Jellyfin is ready.","Signed out on this console.","Your other devices stay",
        "signed in.","Nothing to save.","That cannot be saved.","Saving did not work.","The old settings are kept.","Try again or use Chromatic Manager.","Four networks are saved.",
        "Forget one to add another.","The scan did not work.","Try again in a moment.","No Jellyfin server answered","on this network.","Try the address instead.",
        "That is not a server address.","Example: http://192.0.2.20:8096","Wi-Fi is not connected.","Open Jellyfin once to join,","A: sign in    B: back","A or B: continue",
        /* 1.3.0 Find my server again */
        "Find my server again","Is this your server?","Find my server","Your server was not found","on this network. Check it is","on and on the same Wi-Fi,",
        "then look again.","Look again","Enter the address","A: choose    B: Jellyfin","Looking for your server...","Checking it is yours...","Saving the new address...",
        "Found your server.","Its new address is saved.","Next: Jellyfin opens again.","Your sign-in is unchanged.","That server does not know","this console's sign-in.",
        "Pick another, or sign in again.","That server did not answer.","Check it is switched on,","That server gave an","unexpected reply.","Pick another one.",
        "That address is the one","saved. The server may be off","or busy: try Jellyfin again.","No Jellyfin sign-in is","saved on this console.","Sign in here instead.",
        "Join the network your","server is on, then try again.",
        /* 1.3.1 */
        "Asking that server...","Yes, this is my server","No, look again","Only then is your sign-in","checked with it.",
        "That address answers as","another server. Nothing was","sent to it. Look again.","Jellyfin (version not given)","A: choose    B: back"};
    for(unsigned i=0;i<sizeof(texts)/sizeof(texts[0]);i++)CHECK(8+pj_osd_measure(texts[i],pj_font_advance)<=156);
    for(unsigned i=0;i<sizeof(texts)/sizeof(texts[0]);i++)CHECK(strlen(texts[i])<40);
    /* The Jellyfin screen's hint lines (launcher_main.c SETUP HOOK) are drawn at x=12 by pj_launch_draw_media. */
    const char *hints[]={"Select: set up Wi-Fi here","Select: sign in to Jellyfin","Opening Setup...","Back in a moment","Select: check Wi-Fi settings",
        "Closing Jellyfin...","Not enough memory for setup now"};
    for(unsigned i=0;i<sizeof(hints)/sizeof(hints[0]);i++)CHECK(12+pj_osd_measure(hints[i],pj_font_advance)<=156&&strlen(hints[i])<40);
    /* The Switching screen's step words sit at x=22 next to their colour mark. */
    const char *steps[]={"Saved as first choice","Joining...","Connected","Could not join"};
    for(unsigned i=0;i<4;i++)CHECK(22+pj_osd_measure(steps[i],pj_font_advance)<=156);
    /* The player strip's status lines (jf_app.h) fit its 152 px at x=4. */
    const char *strip[]={"Wi-Fi dropped: rejoining...","Wi-Fi is back: reconnecting...","Server lost: reconnecting...","Waiting for the server..."};
    for(unsigned i=0;i<4;i++)CHECK(pj_osd_measure(strip[i],pj_font_advance)<=152);
}
/* ---- filters for the Python twin ---- */
static int read_exact(uint8_t *out,size_t n){size_t got=0;while(got<n){size_t r=fread(out+got,1,n-got,stdin);if(!r)break;got+=r;}return got==n;}
static int mode_build(void) {
    static uint8_t sector[4096],rebuilt[4096];pj_prov2 r;
    if(!read_exact(sector,4096))return 2;
    if(pj_provision2_parse(sector,4096,&r)!=1)return 3;
    if(!pj_prov2_build(&r,rebuilt))return 4;
    fwrite(rebuilt,1,4096,stdout);return 0;
}
static int mode_commit(const char *op) {
    static uint8_t flash[FLASH_SIZE];flash_model m;fm_init(&m,flash);
    if(!read_exact(flash+PJ_PROV_FLASH_A,4096)||!read_exact(flash+PJ_PROV_FLASH_B,4096))return 2;
    pj_prov_edit e;memset(&e,0,sizeof(e));
    if(!strcmp(op,"add")){e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Cafe");snprintf(e.net.psk,65,"latte-latte");}
    else if(!strcmp(op,"change")){e.net_op=PJ_PROV_NET_SAVE;snprintf(e.net.ssid,33,"Net0");snprintf(e.net.psk,65,"changed-password");}
    else if(!strcmp(op,"forget")){e.net_op=PJ_PROV_NET_FORGET;snprintf(e.net.ssid,33,"Net0");}
    else if(!strcmp(op,"first")){e.net_op=PJ_PROV_NET_FIRST;snprintf(e.net.ssid,33,"Net1");}
    else if(!strcmp(op,"jellyfin")){e.set_jellyfin=1;snprintf(e.url,129,"http://192.0.2.20:8096");snprintf(e.user_id,33,"0123456789abcdef0123456789abcdef");
        snprintf(e.access_token,65,"deadbeefcafef00ddeadbeefcafef00d");snprintf(e.device_id,65,"chromatic-a0a1a2a3a4a5a6a7a8a9aaab");}
    else if(!strcmp(op,"clearjf"))e.clear_jellyfin=1;
    else if(!strcmp(op,"address")){e.set_address=1;snprintf(e.url,129,"http://192.0.2.77:8096");snprintf(e.user_id,33,"0123456789abcdef0123456789abcdef");}
    else if(strcmp(op,"nochange"))return 5;
    pj_prov_flash ops=fm_ops(&m);int rc=pj_provision_commit_with(&ops,&e);
    if(m.touched_outside)return 6;
    fprintf(stderr,"rc=%d erases=%u writes=%u\n",rc,m.erases,m.writes);
    fwrite(flash+PJ_PROV_FLASH_A,1,4096,stdout);fwrite(flash+PJ_PROV_FLASH_B,1,4096,stdout);return rc<0?10-rc:0;
}
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
int main(int argc,char **argv) {
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY);
#endif
    if(argc>=2&&!strcmp(argv[1],"build"))return mode_build();
    if(argc>=3&&!strcmp(argv[1],"commit"))return mode_commit(argv[2]);
    if(argc!=3||strcmp(argv[1],"previews")){fprintf(stderr,"usage: check_setup previews <dir> | build | commit <op>\n");return 2;}
    snprintf(out_dir,sizeof(out_dir),"%s",argv[2]);
    CHECK(PJ_PROV_FLASH_A==0x3fe000u&&PJ_PROV_FLASH_B==0x3ff000u);
    { /* The launcher hook: Select on the Jellyfin screen opens the setup and its presses are routed there. */
        pj_launcher l;pj_launch_init(&l,0);l.phase=PJ_L_MEDIA;
        pj_launch_press(&l,PJ_BTN_SELECT,10);CHECK(l.setup_open&&l.phase==PJ_L_MEDIA);
        pj_launch_press(&l,PJ_BTN_B,20);CHECK(l.phase==PJ_L_MEDIA&&l.setup_pressed==PJ_BTN_B); /* B goes to the setup, not back to the menu */
        l.setup_pressed=0;pj_launch_menu_press(&l);CHECK(l.setup_pressed==PJ_BTN_MENU&&l.phase==PJ_L_MEDIA);
        l.setup_open=0;pj_launch_press(&l,PJ_BTN_B,30);CHECK(l.phase==PJ_L_MENU);
        l.phase=PJ_L_MEDIA;l.app_owns_media=1;pj_launch_press(&l,PJ_BTN_SELECT,40);CHECK(!l.setup_open&&l.app_pressed==PJ_BTN_SELECT); /* the app's while it runs */
    }
    keyboard_checks();scan_checks();protocol_checks();writer_checks();writer_read_failure();setup_checks();
    printf("PASS: keyboard reaches all 95 printable characters, hides secrets (always while Remote Pilot is connected); scan list sorted/deduplicated/capped; "
           "discovery and Quick Connect protocol (GET fallback, polling, expiry, refusals, logout of this console only); every setup flow "
           "including Use this network now (one move-to-top commit, the Switching screen follows the shell's join: connected / wrong "
           "password offers the keyboard / not in range stays first); "
           "record builder; the single writer under %u power-cut runs (%u left the old record, %u the new, never neither, never another sector, retry always completes).\n",
           cut_runs,cut_old,cut_new);
    return 0;
}
