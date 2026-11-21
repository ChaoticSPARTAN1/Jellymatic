/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Launcher 1.6.4 host check: the apps record (shared/apps_record.h), the start-up menu with every on/off combination,
 * the console's own Hide / Show hidden apps (launcher_logic.h) with its write (provision_write.c pj_apps_commit_with)
 * under power cuts, and TV without Live TV (straight to Local Weather, no server). Run by scripts/test_chromatic_apps.py:
 *   check_apps vectors <file>   one answer per line of <file> (the Python twin's vectors), to stdout
 *   check_apps checks <folder>  every check below; previews as <folder>/<name>.ppm (160x144)
 * No hardware. */
#include <stdio.h>
#include <stdlib.h>
#include "shared/launcher_logic.h"
#include "shared/provision_write.h"
#include "../../pocket/ui_font.h"
#define CHECK(x) do {if(!(x)){fprintf(stderr,"apps check failed at line %d\n",__LINE__);exit(1);}} while(0)

/* ---- the Python twin's vectors ---- */
static int unhex(const char *text,uint8_t *out,size_t size) {
    size_t n=strlen(text);
    if(n!=size*2)return 0;
    for(size_t i=0;i<size;i++){unsigned v;if(sscanf(text+2*i,"%2x",&v)!=1)return 0;out[i]=(uint8_t)v;}
    return 1;
}
static int vectors(const char *path) {
    FILE *f=fopen(path,"r");char line[512];
    if(!f)return 2;
    while(fgets(line,sizeof(line),f)) {
        char kind=line[0],a[200],b[200];uint8_t ra[PJ_APPS_SIZE],rb[PJ_APPS_SIZE];unsigned long flags,seq;
        if(kind=='P'&&sscanf(line+2,"%199s",a)==1&&unhex(a,ra,PJ_APPS_SIZE)) {
            pj_apps r={0,0};int rc=pj_apps_parse(ra,PJ_APPS_SIZE,&r);
            printf("P %d %lu %lu\n",rc,rc==1?(unsigned long)r.flags:0ul,rc==1?(unsigned long)r.sequence:0ul);
        } else if(kind=='C'&&sscanf(line+2,"%199s %199s",a,b)==2&&unhex(a,ra,PJ_APPS_SIZE)&&unhex(b,rb,PJ_APPS_SIZE)) {
            pj_apps r;int in_b=0,found=pj_apps_current(ra,rb,PJ_APPS_SIZE,&r,&in_b);
            printf("C %d %lu %lu %d\n",found,(unsigned long)r.flags,(unsigned long)r.sequence,in_b);
        } else if(kind=='B'&&sscanf(line+2,"%lu %lu",&flags,&seq)==2) {
            uint8_t out[PJ_APPS_SIZE];
            if(!pj_apps_build((uint32_t)flags,(uint32_t)seq,out)){puts("B refused");continue;}
            printf("B ");for(unsigned i=0;i<PJ_APPS_SIZE;i++)printf("%02x",out[i]);putchar('\n');
        } else {fclose(f);return 3;}
    }
    fclose(f);return 0;
}

/* ---- previews ---- */
static uint16_t screen[PJ_OSD_W*PJ_OSD_H];
static const char *out_folder;
static unsigned previews;
static void write_ppm(const char *name) {
    char path[700];snprintf(path,sizeof(path),"%s/%s.ppm",out_folder,name);
    FILE *f=fopen(path,"wb");
    CHECK(f);
    fprintf(f,"P6\n%d %d\n255\n",PJ_OSD_W,PJ_OSD_H);
    for(int i=0;i<PJ_OSD_W*PJ_OSD_H;i++) {
        uint16_t c=screen[i];CHECK(c!=PJ_OSD_KEY); /* never the FPGA's see-through colour */
        uint8_t px[3]={(uint8_t)((c>>11)<<3|(c>>13)),(uint8_t)(((c>>5)&63)<<2|((c>>9)&3)),(uint8_t)((c&31)<<3|((c>>2)&7))};
        fwrite(px,1,3,f);
    }
    fclose(f);previews++;
}

/* ---- a modelled FPGA (the same rules as check_launcher.c) ---- */
typedef struct {int cart,alive,open,pressed,pokes,frames,dirty;uint16_t held;} fpga_model;
static void model_poke(fpga_model *f,uint16_t value) {
    int pressed=(value&PJ_POKE_MENU)!=0;
    if(pressed&&!f->pressed){f->pokes++;f->dirty=f->held!=0;}
    if(f->held)f->dirty=1;
    if(f->pressed&&!pressed&&f->cart&&f->frames>=2&&!f->dirty)f->open=!f->open;
    f->pressed=pressed;
}
static uint16_t model_buttons(const fpga_model *f){return (uint16_t)(f->held|(f->open?0:PJ_MENU_CLOSED));}
static uint32_t run(pj_launcher *l,fpga_model *f,uint32_t now,uint32_t until,int *restarted) {
    for(;now<until&&!*restarted;now+=10) {
        pj_launch_action a=pj_launch_step(l,now);
        if(a.prime_frames)f->frames+=2;
        if(a.poke)model_poke(f,a.poke_value);
        if(a.restart_to_stock){CHECK(!f->pressed&&(!f->open||!f->alive));*restarted=1;break;}
        if(a.request_buttons&&f->alive)pj_launch_buttons(l,model_buttons(f),now);
    }
    return now;
}
static void press(pj_launcher *l,fpga_model *f,uint16_t button,uint32_t now) { /* a physical press */
    f->held=button;pj_launch_buttons(l,model_buttons(f),now);
    f->held=0;pj_launch_buttons(l,model_buttons(f),now+1);
}
static void remote(pj_launcher *l,uint16_t button,uint32_t now){pj_launch_press(l,button,now);} /* as remote.h pj_rmt_press */
static uint32_t open_menu(pj_launcher *l,fpga_model *f,int *restarted,uint32_t flags,int writable) {
    memset(f,0,sizeof(*f));f->cart=f->alive=1;*restarted=0;pj_launch_init(l,0);
    pj_launch_set_apps(l,flags);l->apps_writable=(uint8_t)writable;
    uint32_t now=run(l,f,0,1200,restarted);
    CHECK(l->phase==PJ_L_MENU&&f->open&&l->selected==PJ_ITEM_GAMES); /* Games first and selected, whatever is hidden */
    return now;
}
/* Remote Pilot's names for `selected` (remote.h pj_rmt_status): must never name a hidden row */
static const char *const rmt_items[PJ_ITEM_COUNT]={"games","jellyfin","tv","chromatube","other"};

static void record_checks(void) {
    uint8_t a[PJ_APPS_SECTOR],b[PJ_APPS_SECTOR];pj_apps r;int in_b;
    memset(a,0xff,sizeof(a));memset(b,0xff,sizeof(b));
    CHECK(pj_apps_parse(a,sizeof(a),&r)==0&&!pj_apps_current(a,b,sizeof(a),&r,&in_b)&&r.flags==PJ_APPS_DEFAULTS&&r.sequence==0);
    CHECK(PJ_APPS_DEFAULTS==PJ_APPS_TV); /* a new console: TV (Local Weather only), no ChromaTube */
    for(uint32_t flags=0;flags<=PJ_APPS_KNOWN;flags++) {
        CHECK(pj_apps_build(flags,7,a)&&pj_apps_parse(a,PJ_APPS_SIZE,&r)==1&&r.flags==flags&&r.sequence==7);
        for(unsigned i=0;i<PJ_APPS_SIZE;i++)for(unsigned bit=0;bit<8;bit++) { /* every single-bit flip is refused */
            a[i]^=(uint8_t)(1u<<bit);CHECK(pj_apps_parse(a,PJ_APPS_SIZE,&r)!=1);a[i]^=(uint8_t)(1u<<bit);
        }
    }
    CHECK(!pj_apps_build(8,1,a)&&!pj_apps_build(PJ_APPS_TV,0,a)); /* unknown flag, sequence 0 */
    memset(a,0,PJ_APPS_SIZE);CHECK(pj_apps_parse(a,PJ_APPS_SIZE,&r)==-1); /* zeroed */
    /* which copy wins: newer by int32 distance, A when equal */
    memset(a,0xff,sizeof(a));memset(b,0xff,sizeof(b));
    pj_apps_build(PJ_APPS_TV,5,a);pj_apps_build(PJ_APPS_TV|PJ_APPS_TUBE,6,b);
    CHECK(pj_apps_current(a,b,sizeof(a),&r,&in_b)&&r.flags==(PJ_APPS_TV|PJ_APPS_TUBE)&&in_b);
    pj_apps_build(PJ_APPS_TUBE,0xffffffffu,a);pj_apps_build(PJ_APPS_TV,1,b);  /* wrapped: 1 is newer than 0xffffffff */
    CHECK(pj_apps_current(a,b,sizeof(a),&r,&in_b)&&r.flags==PJ_APPS_TV&&in_b);
    pj_apps_build(PJ_APPS_TUBE,9,a);pj_apps_build(PJ_APPS_TV,9,b);
    CHECK(pj_apps_current(a,b,sizeof(a),&r,&in_b)&&r.flags==PJ_APPS_TUBE&&!in_b);
    a[3]^=1;CHECK(pj_apps_current(a,b,sizeof(a),&r,&in_b)&&r.flags==PJ_APPS_TV&&in_b); /* a torn A: B */
    CHECK(PJ_APPS_FLASH_A==0x3FA000u&&PJ_APPS_FLASH_B==0x3FB000u&&PJ_APPS_FLASH_B+PJ_APPS_SECTOR==0x3FC000u);
    CHECK(PJ_APPS_FLASH_A>=PJ_PROV_FLASH_SLOT&&PJ_APPS_FLASH_B+PJ_APPS_SECTOR<=PJ_PROV_FLASH_A-0x2000u); /* below the weather pair */
}

static void menu_checks(void) {
    pj_launcher l;fpga_model f;int restarted;uint32_t now;
    for(uint32_t flags=0;flags<=PJ_APPS_KNOWN;flags++) {
        int tv=(flags&PJ_APPS_TV)!=0,tube=(flags&PJ_APPS_TUBE)!=0;
        pj_launch_item order[PJ_ITEM_COUNT];unsigned shown=0;
        for(unsigned i=0;i<PJ_ITEM_COUNT;i++)if((i!=PJ_ITEM_TV||tv)&&(i!=PJ_ITEM_TUBE||tube))order[shown++]=(pj_launch_item)i;
        CHECK(shown==3u+(unsigned)tv+(unsigned)tube&&order[0]==PJ_ITEM_GAMES&&order[shown-1]==PJ_ITEM_OTHER);
        now=open_menu(&l,&f,&restarted,flags,1);
        CHECK(l.livetv==((flags&PJ_APPS_LIVETV)!=0)&&l.hidden==((tv?0:PJ_HIDE_TV)|(tube?0:PJ_HIDE_TUBE)));
        /* Down through every shown row and around, Up back; a hidden row is never selected or named to Remote Pilot */
        for(unsigned k=1;k<=shown;k++){press(&l,&f,PJ_BTN_DOWN,now);now+=10;CHECK(l.selected==order[k%shown]&&pj_launch_shown(&l,l.selected));
            CHECK(strcmp(rmt_items[l.selected],tv?"":"tv")&&strcmp(rmt_items[l.selected],tube?"":"chromatube"));}
        for(unsigned k=shown;k>0;k--){press(&l,&f,PJ_BTN_UP,now);now+=10;CHECK(l.selected==order[(k-1)%shown]);}
        CHECK(l.selected==PJ_ITEM_GAMES);
        press(&l,&f,PJ_BTN_UP,now);now+=10;CHECK(l.selected==PJ_ITEM_OTHER); /* Up from Games wraps to Other firmware */
        /* A on every shown row does what it did in 1.6.3; B there comes back; B on the menu is Games from every row */
        for(unsigned k=0;k<shown;k++) {
            now=open_menu(&l,&f,&restarted,flags,1);
            for(unsigned j=0;j<k;j++){press(&l,&f,PJ_BTN_DOWN,now);now+=10;}
            pj_launch_item row=order[k];CHECK(l.selected==row);
            if(row==PJ_ITEM_GAMES){press(&l,&f,PJ_BTN_A,now);now=run(&l,&f,now+10,now+3000,&restarted);
                CHECK(restarted&&!f.open&&l.close_reason==PJ_CLOSE_CHOSE_GAMES);continue;}
            press(&l,&f,PJ_BTN_A,now);now+=10;
            if(row==PJ_ITEM_OTHER)CHECK(l.phase==PJ_L_OTHER);
            else CHECK(l.phase==PJ_L_MEDIA&&l.media_app==(row==PJ_ITEM_TV?PJ_APP_TV:row==PJ_ITEM_TUBE?PJ_APP_TUBE:PJ_APP_JELLYFIN));
            press(&l,&f,PJ_BTN_B,now);now+=10;CHECK(l.phase==PJ_L_MENU&&l.selected==row);
            press(&l,&f,PJ_BTN_B,now);now=run(&l,&f,now+10,now+3000,&restarted);
            CHECK(restarted&&!f.open&&l.close_reason==PJ_CLOSE_B_BUTTON);
        }
        /* a held button at power-on still skips the menu */
        memset(&f,0,sizeof(f));f.cart=f.alive=1;f.held=PJ_BTN_B;restarted=0;pj_launch_init(&l,0);pj_launch_set_apps(&l,flags);
        now=run(&l,&f,0,10000,&restarted);CHECK(restarted&&f.pokes==0&&l.done_reason==PJ_DONE_HELD_AT_POWER_ON);
        /* Select: on TV/ChromaTube (shown) it asks to hide; on Games, Jellyfin, Other firmware it is Credits */
        now=open_menu(&l,&f,&restarted,flags,1);
        press(&l,&f,PJ_BTN_SELECT,now);now+=10;CHECK(l.credits_open&&!l.apps_prompt);press(&l,&f,PJ_BTN_B,now);now+=10;CHECK(!l.credits_open);
    }
    /* No apps writable (a build without the setup writer): Select is Credits everywhere, no Show row */
    now=open_menu(&l,&f,&restarted,PJ_APPS_TV|PJ_APPS_TUBE,0);
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+5);CHECK(l.selected==PJ_ITEM_TV);
    press(&l,&f,PJ_BTN_SELECT,now+10);CHECK(l.credits_open&&!l.apps_prompt);
    pj_launch_set_apps(&l,PJ_APPS_TV);CHECK(pj_other_rows(&l)==PJ_OTHER_ROWS);
    /* With nothing hidden the five rows are exactly 1.6.3's (no record: pj_launch_init's flags) */
    pj_launch_init(&l,0);CHECK(!l.hidden&&l.livetv&&l.apps_flags==PJ_APPS_KNOWN);
    for(unsigned i=0;i<PJ_ITEM_COUNT;i++)CHECK(pj_launch_shown(&l,i));
}

/* ---- the flash model (as check_setup.c): every erase/write logged, programmable power cut ---- */
#define FLASH_SIZE 0x400000u
typedef struct {uint8_t *flash;unsigned ops,cut_at;int cut;unsigned erases,writes;uint32_t outside;unsigned cut_byte;int fail_read;} flash_model;
static int fm_mine(uint32_t off,uint32_t n){return (off==PJ_APPS_FLASH_A||off==PJ_APPS_FLASH_B)&&n==4096u;}
static int fm_read(void *ctx,uint32_t off,void *out,uint32_t n) {
    flash_model *m=ctx;if(m->fail_read||off+n>FLASH_SIZE)return -1;memcpy(out,m->flash+off,n);return 0;
}
static int fm_erase(void *ctx,uint32_t off,uint32_t n) {
    flash_model *m=ctx;if(!fm_mine(off,n))m->outside++;
    if(m->cut)return -1;
    m->erases++;
    if(m->cut_at&&++m->ops>=m->cut_at){unsigned upto=m->cut_byte%(n+1);memset(m->flash+off,0xff,upto);m->cut=1;return -1;}
    memset(m->flash+off,0xff,n);return 0;
}
static int fm_write(void *ctx,uint32_t off,const void *data,uint32_t n) {
    flash_model *m=ctx;if(!fm_mine(off,n))m->outside++;
    if(m->cut)return -1;
    m->writes++;
    if(m->cut_at&&++m->ops>=m->cut_at){
        unsigned upto=m->cut_byte%(n+1);
        for(unsigned i=0;i<upto;i++)m->flash[off+i]&=((const uint8_t *)data)[i];
        if(upto<n)m->flash[off+upto]&=(uint8_t)(((const uint8_t *)data)[upto]|0x0f);
        m->cut=1;return -1;}
    for(unsigned i=0;i<n;i++)m->flash[off+i]&=((const uint8_t *)data)[i];
    return 0;
}
static uint8_t flash[FLASH_SIZE],reference[FLASH_SIZE];
static uint32_t on_flash(int *found){pj_apps r;*found=pj_apps_current(flash+PJ_APPS_FLASH_A,flash+PJ_APPS_FLASH_B,PJ_APPS_SECTOR,&r,NULL);return r.flags;}
static void seed(const pj_apps *a,const pj_apps *b) {
    memset(flash,0x5a,FLASH_SIZE); /* everything else is "data" that must stay exactly as it was */
    memset(flash+PJ_APPS_FLASH_A,0xff,2*PJ_APPS_SECTOR);
    if(a)pj_apps_build(a->flags,a->sequence,flash+PJ_APPS_FLASH_A);
    if(b)pj_apps_build(b->flags,b->sequence,flash+PJ_APPS_FLASH_B);
    memcpy(reference,flash,FLASH_SIZE);
}
static int outside_same(void) {
    return !memcmp(flash,reference,PJ_APPS_FLASH_A)&&!memcmp(flash+PJ_APPS_FLASH_B+PJ_APPS_SECTOR,reference+PJ_APPS_FLASH_B+PJ_APPS_SECTOR,
                                                           FLASH_SIZE-PJ_APPS_FLASH_B-PJ_APPS_SECTOR);
}
static unsigned cut_runs,cut_old,cut_new;
/* One write under a power cut at every erase/write (and at many bytes of each): the console reads the old record or
 * the new one, never anything else; the same write after power-up finishes it; nothing outside the pair changes. */
static void write_under_cuts(const pj_apps *old_a,const pj_apps *old_b,uint32_t want) {
    int found;uint32_t old_flags;
    for(unsigned cut_at=0;cut_at<=3;cut_at++)for(unsigned byte=0;byte<(cut_at?4097u:1u);byte+=cut_at?(byte<40||byte>4080?1u:61u):1u) {
        seed(old_a,old_b);old_flags=on_flash(&found);int had=found;
        flash_model m;memset(&m,0,sizeof(m));m.flash=flash;m.cut_at=cut_at;m.cut_byte=byte;
        pj_prov_flash ops={&m,fm_read,fm_erase,fm_write};
        int rc=pj_apps_commit_with(&ops,want);
        CHECK(!m.outside&&outside_same());
        uint32_t now_flags=on_flash(&found);cut_runs++;
        if(!m.cut) {
            CHECK((rc==PJ_PROV_WRITE_OK||(rc==PJ_PROV_WRITE_NO_CHANGE&&had&&old_flags==want))&&found&&now_flags==want);
            if(rc==PJ_PROV_WRITE_OK)CHECK(m.erases==1&&m.writes==1); /* one sector: the copy not in force */
        } else {
            CHECK(rc<0);
            if(found&&now_flags==want)cut_new++;
            else{CHECK(found==had&&now_flags==old_flags);cut_old++;} /* the old record, or (none ever) the defaults */
            m.cut=0;m.cut_at=0;
            int again=pj_apps_commit_with(&ops,want);
            CHECK((again==PJ_PROV_WRITE_OK||again==PJ_PROV_WRITE_NO_CHANGE)&&on_flash(&found)==want&&found&&!m.outside&&outside_same());
        }
    }
}
static void writer_checks(void) {
    pj_apps a={PJ_APPS_TV|PJ_APPS_TUBE|PJ_APPS_LIVETV,5},b=a,older={PJ_APPS_TV,4};
    write_under_cuts(&a,&b,PJ_APPS_TV|PJ_APPS_LIVETV);      /* the Manager wrote both copies: hide ChromaTube */
    write_under_cuts(&a,NULL,PJ_APPS_TV|PJ_APPS_LIVETV);    /* only A */
    write_under_cuts(NULL,&b,PJ_APPS_LIVETV);                /* only B: hide TV and ChromaTube */
    write_under_cuts(&older,&b,PJ_APPS_KNOWN&~PJ_APPS_TV);   /* B newer */
    write_under_cuts(NULL,NULL,PJ_APPS_TV);                  /* nothing yet (the defaults written as a record) */
    write_under_cuts(&a,&b,a.flags);                         /* the same: nothing written */
    {pj_apps w1={PJ_APPS_TV,0xffffffffu},w2={PJ_APPS_TUBE,0xfffffffeu};write_under_cuts(&w1,&w2,PJ_APPS_KNOWN);} /* sequence wrap */
    CHECK(cut_old>0&&cut_new>0);
    /* refused before any write: unknown flags, a failed read, no memory is not modelled (calloc) */
    seed(&a,&b);flash_model m;memset(&m,0,sizeof(m));m.flash=flash;pj_prov_flash ops={&m,fm_read,fm_erase,fm_write};
    CHECK(pj_apps_commit_with(&ops,8)==PJ_PROV_WRITE_INVALID&&!m.erases&&!m.writes);
    m.fail_read=1;CHECK(pj_apps_commit_with(&ops,PJ_APPS_TV)==PJ_PROV_WRITE_READ_FAILED&&!m.erases&&!m.writes);
    CHECK(pj_apps_commit_with(NULL,PJ_APPS_TV)==PJ_PROV_WRITE_READ_FAILED);
    m.fail_read=0;CHECK(outside_same());
    /* two writes in a row alternate the sectors and keep the older copy valid (no secret: not erased) */
    seed(NULL,NULL);memset(&m,0,sizeof(m));m.flash=flash;
    CHECK(pj_apps_commit_with(&ops,PJ_APPS_TV)==PJ_PROV_WRITE_OK);
    pj_apps r;CHECK(pj_apps_parse(flash+PJ_APPS_FLASH_A,PJ_APPS_SECTOR,&r)==1&&r.sequence==1);
    CHECK(pj_apps_commit_with(&ops,PJ_APPS_TV|PJ_APPS_TUBE)==PJ_PROV_WRITE_OK);
    CHECK(pj_apps_parse(flash+PJ_APPS_FLASH_B,PJ_APPS_SECTOR,&r)==1&&r.sequence==2&&r.flags==(PJ_APPS_TV|PJ_APPS_TUBE));
    CHECK(pj_apps_parse(flash+PJ_APPS_FLASH_A,PJ_APPS_SECTOR,&r)==1&&r.sequence==1);
    CHECK(pj_apps_commit_with(&ops,PJ_APPS_TUBE)==PJ_PROV_WRITE_OK);
    CHECK(pj_apps_parse(flash+PJ_APPS_FLASH_A,PJ_APPS_SECTOR,&r)==1&&r.sequence==3&&!m.outside&&outside_same());
}

/* ---- Hide and Show on the console, end to end with the model flash (the shell's steps, launcher_main.c) ---- */
static int shell_write(pj_launcher *l,flash_model *m,uint32_t now) {
    uint32_t want;
    if(!pj_apps_write_wanted(l,&want))return 0;
    pj_prov_flash ops={m,fm_read,fm_erase,fm_write};
    int rc=pj_apps_commit_with(&ops,want);
    pj_launch_apps_saved(l,rc==PJ_PROV_WRITE_OK||rc==PJ_PROV_WRITE_NO_CHANGE,want,now);
    return rc==PJ_PROV_WRITE_OK||rc==PJ_PROV_WRITE_NO_CHANGE?1:-1;
}
static uint32_t boot_flags(void){int found;return on_flash(&found);} /* what the next boot reads */
static void prompt_checks(void) {
    pj_launcher l;fpga_model f;int restarted;uint32_t now;flash_model m;
    pj_apps both={PJ_APPS_KNOWN,3};
    /* Hide TV: Select by hand opens the question; a remote Select or A never does or answers it */
    seed(&both,&both);memset(&m,0,sizeof(m));m.flash=flash;
    now=open_menu(&l,&f,&restarted,boot_flags(),1);
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+5);now+=10;CHECK(l.selected==PJ_ITEM_TV&&pj_launch_hide_offered(&l));
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("menu-tv-focused-select-hide");
    remote(&l,PJ_BTN_SELECT,now);CHECK(!l.apps_prompt&&!l.credits_open);  /* Remote Pilot: nothing */
    press(&l,&f,PJ_BTN_SELECT,now);now+=10;CHECK(l.apps_prompt==PJ_APPS_PROMPT_HIDE&&l.apps_stage==PJ_AS_ASK&&l.apps_target==PJ_ITEM_TV);
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("hide-prompt-tv");
    remote(&l,PJ_BTN_A,now);CHECK(l.apps_stage==PJ_AS_ASK);              /* a remote A does not answer */
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_START,now+5);now+=10;CHECK(l.apps_stage==PJ_AS_ASK&&l.selected==PJ_ITEM_TV&&l.phase==PJ_L_MENU);
    press(&l,&f,PJ_BTN_B,now);now+=10;CHECK(!l.apps_prompt&&l.phase==PJ_L_MENU); /* B: cancel, not Games */
    now=run(&l,&f,now,now+3000,&restarted);CHECK(!restarted);
    CHECK(!shell_write(&l,&m,now)&&boot_flags()==PJ_APPS_KNOWN&&!m.writes); /* nothing asked, nothing written */
    press(&l,&f,PJ_BTN_SELECT,now);now+=10;press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(l.apps_stage==PJ_AS_SAVE);
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("hide-prompt-saving");
    press(&l,&f,PJ_BTN_B,now);CHECK(l.apps_stage==PJ_AS_SAVE);            /* nothing answers while it writes */
    l.menu_to_us=1;pj_launch_buttons(&l,(uint16_t)(model_buttons(&f)|PJ_BTN_MENU),now+2);pj_launch_buttons(&l,model_buttons(&f),now+3);
    CHECK(l.apps_stage==PJ_AS_SAVE);l.menu_to_us=0;
    CHECK(shell_write(&l,&m,now)==1&&!l.apps_prompt&&(l.hidden&PJ_HIDE_TV)&&l.selected==PJ_ITEM_TUBE);
    CHECK(boot_flags()==(PJ_APPS_TUBE|PJ_APPS_LIVETV)&&!m.outside&&outside_same()); /* only TV went; Live TV kept for later */
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("menu-after-hiding-tv");
    press(&l,&f,PJ_BTN_UP,now);now+=10;CHECK(l.selected==PJ_ITEM_MEDIA);  /* TV is skipped now */
    /* Hide ChromaTube too; then Other firmware offers Show hidden apps (fourth row) */
    press(&l,&f,PJ_BTN_DOWN,now);now+=10;CHECK(l.selected==PJ_ITEM_TUBE);
    press(&l,&f,PJ_BTN_SELECT,now);now+=10;CHECK(l.apps_target==PJ_ITEM_TUBE);
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("hide-prompt-chromatube");
    press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(shell_write(&l,&m,now)==1&&l.hidden==(PJ_HIDE_TV|PJ_HIDE_TUBE)&&l.selected==PJ_ITEM_OTHER);
    CHECK(boot_flags()==PJ_APPS_LIVETV);
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("menu-both-hidden-on-the-console");
    press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(l.phase==PJ_L_OTHER&&pj_other_rows(&l)==4);
    press(&l,&f,PJ_BTN_UP,now);now+=10;CHECK(l.other_selected==PJ_OTHER_SHOW);
    pj_launch_draw_other(screen,&l,pj_font_alpha,pj_font_advance);write_ppm("other-firmware-show-hidden-apps");
    remote(&l,PJ_BTN_A,now);CHECK(!l.apps_prompt);                       /* a remote A does not ask */
    press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(l.apps_prompt==PJ_APPS_PROMPT_SHOW);
    pj_launch_draw_other(screen,&l,pj_font_alpha,pj_font_advance);write_ppm("show-prompt");
    press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(shell_write(&l,&m,now)==1&&!l.hidden&&l.phase==PJ_L_MENU&&l.selected==PJ_ITEM_TV);
    CHECK(boot_flags()==PJ_APPS_KNOWN);
    /* power cut during the hide: the next boot reads the old menu or the new one; the screen says it failed */
    for(unsigned cut=1;cut<=2;cut++)for(unsigned byte=0;byte<4097u;byte+=byte<64?1u:509u) {
        seed(&both,&both);memset(&m,0,sizeof(m));m.flash=flash;m.cut_at=cut;m.cut_byte=byte;
        now=open_menu(&l,&f,&restarted,boot_flags(),1);
        press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+3);press(&l,&f,PJ_BTN_DOWN,now+6);now+=10;CHECK(l.selected==PJ_ITEM_TUBE);
        press(&l,&f,PJ_BTN_SELECT,now);press(&l,&f,PJ_BTN_A,now+5);now+=10;
        CHECK(shell_write(&l,&m,now)==-1&&l.apps_stage==PJ_AS_FAILED&&!l.hidden&&!m.outside&&outside_same());
        uint32_t next=boot_flags();CHECK(next==PJ_APPS_KNOWN||next==(PJ_APPS_TV|PJ_APPS_LIVETV));
        if(cut==1&&byte==0) {pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("hide-prompt-could-not-save");}
        press(&l,&f,PJ_BTN_B,now);CHECK(!l.apps_prompt&&l.phase==PJ_L_MENU&&l.selected==PJ_ITEM_TUBE);
        press(&l,&f,PJ_BTN_B,now+10);now=run(&l,&f,now+20,now+3000,&restarted);CHECK(restarted&&l.close_reason==PJ_CLOSE_B_BUTTON);
    }
    /* Menu (forwarded by the media FPGA) on the question is No; a physical Menu that closes the overlay is Games */
    seed(&both,&both);memset(&m,0,sizeof(m));m.flash=flash;
    now=open_menu(&l,&f,&restarted,boot_flags(),1);
    press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+3);press(&l,&f,PJ_BTN_SELECT,now+6);now+=10;CHECK(l.apps_prompt);
    l.menu_to_us=1;pj_launch_buttons(&l,(uint16_t)(model_buttons(&f)|PJ_BTN_MENU),now);pj_launch_buttons(&l,model_buttons(&f),now+1);
    CHECK(!l.apps_prompt&&l.phase==PJ_L_MENU);l.menu_to_us=0;
    press(&l,&f,PJ_BTN_SELECT,now+5);f.open=0;now=run(&l,&f,now+10,now+600,&restarted);CHECK(restarted&&l.done_reason==PJ_DONE_MENU_BUTTON);
    CHECK(!m.writes); /* nothing was written by any of that */
    /* the service screen (Chromatic Manager connected) takes every press: no question opens */
    now=open_menu(&l,&f,&restarted,PJ_APPS_KNOWN,1);press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+3);
    l.service=1;press(&l,&f,PJ_BTN_SELECT,now+6);CHECK(!l.apps_prompt);
    /* the defaults on a new console: ChromaTube hidden, so Other firmware offers Show hidden apps at once */
    now=open_menu(&l,&f,&restarted,PJ_APPS_DEFAULTS,1);
    pj_launch_draw_menu(screen,&l,now,pj_font_alpha,pj_font_advance);write_ppm("menu-new-console-defaults");
    press(&l,&f,PJ_BTN_UP,now);press(&l,&f,PJ_BTN_A,now+5);now+=10;CHECK(l.phase==PJ_L_OTHER&&pj_other_rows(&l)==4);
    /* every word fits */
    for(unsigned i=0;i<4;i++)CHECK(14+pj_osd_measure(pj_apps_hide_lines[i],pj_font_advance)<=148);
    CHECK(8+pj_osd_measure("A: start   B: Games   Select: hide",pj_font_advance)<=156);
    CHECK(31+pj_osd_measure("Hide ChromaTube?",pj_font_advance)<=148&&14+pj_osd_measure("Show hidden apps?",pj_font_advance)<=148);
    CHECK(14+pj_osd_measure("TV and ChromaTube",pj_font_advance)<=148&&8+pj_osd_measure("Hidden: TV, ChromaTube",pj_font_advance)<=156);
    CHECK(33+pj_osd_measure("Show hidden apps",pj_font_advance)<=150);
}

/* ---- TV without Live TV: straight to Local Weather, no TV app, no server ---- */
static void weather_only_checks(void) {
    pj_launcher l;fpga_model f;int restarted;uint32_t now;
    for(int livetv=0;livetv<=1;livetv++) {
        now=open_menu(&l,&f,&restarted,PJ_APPS_TV|PJ_APPS_TUBE|(livetv?PJ_APPS_LIVETV:0),1);
        press(&l,&f,PJ_BTN_DOWN,now);press(&l,&f,PJ_BTN_DOWN,now+5);press(&l,&f,PJ_BTN_A,now+10);now+=20;
        CHECK(l.phase==PJ_L_MEDIA&&l.media_app==PJ_APP_TV);
        CHECK(pj_tv_weather_only(&l)==!livetv&&pj_media_app_allowed(&l)==livetv&&pj_media_needs_sign_in(&l)==livetv);
        CHECK(!pj_tv_weather_start_wanted(&l,0,0,0));                       /* not before Wi-Fi has joined */
        CHECK(pj_tv_weather_start_wanted(&l,1,0,0)==!livetv);               /* then at once, without Live TV */
        CHECK(!pj_tv_weather_start_wanted(&l,1,1,0)&&!pj_tv_weather_start_wanted(&l,1,0,1)); /* once a visit; not while busy */
        l.app_owns_media=1;CHECK(!pj_tv_weather_start_wanted(&l,1,0,0));l.app_owns_media=0;
        press(&l,&f,PJ_BTN_B,now);CHECK(l.phase==PJ_L_MENU&&!pj_tv_weather_only(&l)&&pj_media_app_allowed(&l)==0); /* menu: no app */
    }
    /* Jellyfin and ChromaTube are never weather-only, whatever the record says */
    for(unsigned row=1;row<=3;row+=2) {
        now=open_menu(&l,&f,&restarted,PJ_APPS_TV|PJ_APPS_TUBE,1);
        for(unsigned k=0;k<row;k++){press(&l,&f,PJ_BTN_DOWN,now);now+=5;}
        press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(l.phase==PJ_L_MEDIA&&!pj_tv_weather_only(&l)&&pj_media_app_allowed(&l)&&pj_media_needs_sign_in(&l));
    }
    /* previews: the TV row on a console with Wi-Fi only (no Jellyfin) opening the channel, its banner, a failed open */
    pj_launch_draw_opening(screen,PJ_ITEM_TV,"Opening Local Weather...",NULL,pj_font_alpha,pj_font_advance);write_ppm("tv-opening-local-weather-only");
    pj_launch_draw_opening(screen,PJ_ITEM_TV,"Local Weather did not open","Weather: not enough memory",pj_font_alpha,pj_font_advance);
    write_ppm("tv-local-weather-did-not-open");
    pj_osd_fill(screen,0x1c2a6eu);pj_wx_banner_keys(screen,"B: back",pj_font_alpha,pj_font_advance);write_ppm("tv-local-weather-only-banner");
    pj_osd_fill(screen,0x1c2a6eu);pj_wx_banner(screen,pj_font_alpha,pj_font_advance);write_ppm("tv-local-weather-banner-with-live-tv");
    CHECK(pj_osd_measure("Local Weather did not open",pj_font_advance)<=152&&pj_osd_measure("Opening Local Weather...",pj_font_advance)<=152);
}
/* ---- 1.6.4: TV and ChromaTube on a console that is not (or not yet) set up: a friendly page each ---- */
static int raw_code(const char *s) { /* "-1", "500", "0x1f", "err": the kind of thing an owner must never be shown */
    for(const char *p=s;*p;p++) {
        if(*p>='0'&&*p<='9')return 1;
        if((p[0]=='e'||p[0]=='E')&&(p[1]=='r'||p[1]=='R')&&(p[2]=='r'||p[2]=='R'))return 1;
    }
    return 0;
}
static unsigned notices_seen;
static void notice_checks(void) {
    pj_launcher l;fpga_model f;int restarted;uint32_t now;
    static const char *const names[PJ_NOTICE_COUNT]={"","no-wifi","joining-wifi","wifi-did-not-connect","no-sign-in"};
    /* which page: the rows and states (TV with and without Live TV, ChromaTube); Jellyfin keeps its own screen */
    struct {uint32_t flags;unsigned downs;const char *row;} rows[]={
        {PJ_APPS_TV|PJ_APPS_TUBE,2,"tv-local-weather"},{PJ_APPS_TV|PJ_APPS_TUBE|PJ_APPS_LIVETV,2,"tv-live-tv"},
        {PJ_APPS_TV|PJ_APPS_TUBE,3,"chromatube"},{PJ_APPS_TV|PJ_APPS_TUBE,1,"jellyfin"}};
    for(unsigned r=0;r<4;r++) {
        now=open_menu(&l,&f,&restarted,rows[r].flags,1);
        for(unsigned k=0;k<rows[r].downs;k++){press(&l,&f,PJ_BTN_DOWN,now);now+=5;}
        press(&l,&f,PJ_BTN_A,now);now+=10;CHECK(l.phase==PJ_L_MEDIA);
        int jelly=r==3,weather=r==0;
        CHECK(pj_media_notice(&l,0,0,0,0)==(jelly?PJ_NOTICE_NONE:PJ_NOTICE_NO_WIFI));              /* no saved network */
        CHECK(pj_media_notice(&l,1,0,2,0)==(jelly||weather?PJ_NOTICE_NONE:PJ_NOTICE_NO_SIGN_IN));  /* Local Weather: no sign-in needed */
        CHECK(pj_media_notice(&l,1,1,1,0)==(jelly?PJ_NOTICE_NONE:PJ_NOTICE_JOINING));
        CHECK(pj_media_notice(&l,1,1,0,0)==(jelly?PJ_NOTICE_NONE:PJ_NOTICE_JOINING));             /* before the radio starts */
        CHECK(pj_media_notice(&l,1,1,2,1)==(jelly?PJ_NOTICE_NONE:PJ_NOTICE_JOINING));             /* a dropped link */
        CHECK(pj_media_notice(&l,1,1,3,0)==(jelly?PJ_NOTICE_NONE:PJ_NOTICE_WIFI_FAILED));
        CHECK(pj_media_notice(&l,1,1,2,0)==PJ_NOTICE_NONE);                                         /* set up and online: the app */
        if(jelly)continue;
        for(unsigned n=1;n<PJ_NOTICE_COUNT;n++) {
            if(n==PJ_NOTICE_NO_SIGN_IN&&weather)continue;
            for(int here=1;here>=0;here--) {
                pj_notice_text t=pj_media_notice_text(&l,n,here);
                const char *all[5]={t.title,t.line[0],t.line[1],t.line[2],t.step};
                CHECK(t.title[0]&&t.line[0][0]);
                for(unsigned k=0;k<5;k++){CHECK(pj_osd_measure(all[k],pj_font_advance)<=148);CHECK(!raw_code(all[k]));}
                /* the next step names somewhere to go: this console (Select) or Chromatic Manager */
                if(n!=PJ_NOTICE_JOINING){int names_place=0;for(unsigned k=0;k<5;k++)names_place|=strstr(all[k],"Chromatic Manager")||strstr(all[k],"Select");CHECK(names_place);}
                if(here&&n!=PJ_NOTICE_JOINING)CHECK(!strncmp(t.step,"Select: ",8));
            }
            pj_launch_draw_notice(screen,&l,n,1,pj_font_alpha,pj_font_advance);
            char name[96];snprintf(name,sizeof(name),"not-set-up-%s-%s",rows[r].row,names[n]);write_ppm(name);notices_seen++;
            for(size_t k=0;k<160*144;k++)if(screen[k]!=screen[0]){goto drawn;}
            CHECK(0); /* a blank page */
            drawn:;
        }
        /* B still leaves: the page is drawing only */
        press(&l,&f,PJ_BTN_B,now);now+=10;CHECK(l.phase==PJ_L_MENU);
    }
}
static void combination_previews(void) {
    static const struct {uint32_t flags;const char *name;} combos[]={
        {PJ_APPS_TV|PJ_APPS_TUBE|PJ_APPS_LIVETV,"menu-combo-tv-and-chromatube"},{PJ_APPS_TV,"menu-combo-tv-only-the-defaults"},
        {PJ_APPS_TUBE,"menu-combo-chromatube-only"},{0,"menu-combo-neither"}};
    pj_launcher l;
    for(unsigned k=0;k<4;k++) {
        pj_launch_init(&l,0);pj_launch_set_apps(&l,combos[k].flags);l.apps_writable=1;l.phase=PJ_L_MENU;
        pj_launch_draw_menu(screen,&l,0,pj_font_alpha,pj_font_advance);write_ppm(combos[k].name);
    }
}
int main(int argc,char **argv) {
    if(argc==3&&!strcmp(argv[1],"vectors"))return vectors(argv[2]);
    if(argc!=3||strcmp(argv[1],"checks"))return 2;
    out_folder=argv[2];
    record_checks();menu_checks();writer_checks();prompt_checks();weather_only_checks();notice_checks();combination_previews();
    printf("PASS: 1.6.4 not-set-up pages: %u friendly pages for TV (Local Weather only, with Live TV) and ChromaTube (no Wi-Fi, "
           "joining, Wi-Fi did not connect, no Jellyfin sign-in; Local Weather never asks for a sign-in; Jellyfin keeps its "
           "screen), every line fits, no raw codes, each names the next step (Select here or Chromatic Manager), B leaves\n",notices_seen);
    printf("PASS: apps record (every flag, every single-bit flip refused, copy choice incl. wrap and torn A), the start-up menu "
           "with all 8 flag combinations (Games first and selected, hidden rows skipped both ways, A/B on every shown row, B is "
           "Games, a held button skips the menu, Remote Pilot never names a hidden row), Select is Credits off TV/ChromaTube; "
           "the on-console Hide and Show (physical presses only; B, Menu cancel; nothing answers while it writes); the write "
           "under %u power cuts (old %u, new %u; each finished by the same write; nothing outside its two sectors); TV without "
           "Live TV opens Local Weather with no app and no sign-in; %u previews\n",cut_runs,cut_old,cut_new,previews);
    return 0;
}
