/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC check of the weather location (wx_place.h, launcher 1.6.3), driven by
 * scripts/test_chromatic_weather_place.py, which compares what this prints with the Python writer
 * (scripts/chromatic_weather_location.py) and with the compiled-in tables (wx_location.h's packs):
 *
 *   records <file>                 each 256-byte record of the file: parsed or refused, and its fields
 *   pick <file>                    each pair of records (A, B): which one the console uses
 *   requests [<record file>]       the request list (no file: the default pack's compiled-in one)
 *   tz <std_min> <dst> <epoch>...  local time and offset in that zone
 *   pages <record file|-> <replies folder> <frames folder> <epoch>
 *                                  every request answered from the folder (files named as the model
 *                                  of the ESP32 side names them), committed, then every page drawn
 * 1.6.5, location packs (wx_lpack.h):
 *   lpacks <file>                  each framed pack (u32 length, bytes) of the file: parsed or refused
 *   lpick <file>                   each framed pair (A, B): which copy the console uses (as weather_esp32.c)
 *   --pack=<file>                  (with requests / pages) the pack the console holds: used when it names the record
 *
 * No network, no flash, nothing private.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "wx.h"

static uint8_t pack_bytes[WX_LPACK_COPY];
static wx_lpack pack_now;
static int pack_given,pack_good;
/* a pack given with --pack=, used for the record when it names it (as weather_esp32.c's place_read) */
static void attach(wx_place *p) {
    if(!pack_given||!p)return;
    int used=pack_good&&wx_place_use_pack(p,&pack_now);
    printf("PACK good=%d used=%d\n",pack_good,used);
}
static uint8_t *load(const char *path,size_t *size) {
    FILE *f=fopen(path,"rb");
    if(!f)return NULL;
    fseek(f,0,SEEK_END);
    long n=ftell(f);
    fseek(f,0,SEEK_SET);
    uint8_t *data=malloc((size_t)n+1);
    *size=fread(data,1,(size_t)n,f);
    fclose(f);
    return data;
}
static void show(const wx_place *p) {
    printf("name=%s area=%s state=%s lat=%ld lon=%ld tz=%d dst=%u office=%s grid=%u,%u n=%u radar=%u seq=%lu",p->name,p->area,
           p->state,(long)p->lat_e4,(long)p->lon_e4,p->tz.std_min,p->tz.dst,p->office,p->grid_x,p->grid_y,p->n_stations,p->radar,
           (unsigned long)p->sequence);
    printf(" pack=%s builtin=%u",p->pack?p->pack->state:"-",p->builtin);  /* 1.6.4: the region pack, and an exact pack home */
    for(unsigned i=0;i<p->n_stations;i++)printf(" st%u=%s|%s",i,p->station[i],p->label[i]);
    printf("\n");
}
/* the name the model of the ESP32 side gives the reply to a request (check_weather_glue.c) */
static void slug(const char *url,char *out,size_t cap) {
    const char *p=strstr(url,"://");
    size_t n=0;
    int gap=0;
    for(p=p?p+3:url;*p&&n+2<cap;p++) {
        int keep=(*p>='A'&&*p<='Z')||(*p>='a'&&*p<='z')||(*p>='0'&&*p<='9')||*p=='.'||*p=='-';
        if(keep){if(gap&&n)out[n++]='_';gap=0;out[n++]=*p;}else gap=1;
    }
    out[n]=0;
}
static void write_frame(const char *dir,const char *name,const uint16_t *fb) {
    char path[600];
    snprintf(path,sizeof(path),"%s/%s.rgb565",dir,name);
    FILE *f=fopen(path,"wb");
    if(f){fwrite(fb,2,WX_W*WX_H_PX,f);fclose(f);}
}
static const char *const kind_name[WX_P_KINDS]={"ident","current-conditions","current-conditions","latest-observations","almanac",
    "local-forecast","extended-forecast","regional-state","regional-close","rain-outlook","current-radar"};

int main(int argc,char **argv) {
    for(int a=1;a<argc;a++)if(!strncmp(argv[a],"--pack=",7)) {
        size_t n=0;uint8_t *data=load(argv[a]+7,&n);
        if(!data){printf("BAD pack file\n");return 2;}
        memcpy(pack_bytes,data,n<sizeof(pack_bytes)?n:sizeof(pack_bytes));free(data);
        pack_given=1;pack_good=wx_lpack_parse(pack_bytes,n<sizeof(pack_bytes)?n:sizeof(pack_bytes),&pack_now);
        for(int b=a;b+1<argc;b++)argv[b]=argv[b+1];
        argc--;a--;
    }
    if(argc<2)return 2;
    if(!strcmp(argv[1],"lpacks")&&argc==3) {
        size_t n=0;uint8_t *data=load(argv[2],&n);
        if(!data)return 2;
        static wx_lpack lp;
        unsigned k=0;
        for(size_t at=0;at+4<=n;k++) {
            uint32_t len=wx_lp_le32(data+at);at+=4;
            if(at+len>n)break;
            int ok=wx_lpack_parse(data+at,len,&lp);
            if(ok)printf("LP %u 1 seq=%lu crc=%08lx name=%s reqs=%u cities=%u rows=%u\n",k,(unsigned long)lp.sequence,(unsigned long)lp.crc,
                         lp.pack.name,lp.pack.requests,lp.pack.cities,lp.pack.obs_rows);
            else {
                printf("LP %u 0\n",k);
                const uint8_t *b=(const uint8_t *)&lp;for(size_t i=0;i<sizeof(lp);i++)if(b[i]){printf("FAIL refused pack %u left data\n",k);break;}
            }
            at+=len;
        }
        free(data);
        return 0;
    }
    if(!strcmp(argv[1],"lpick")&&argc==3) {
        size_t n=0;uint8_t *data=load(argv[2],&n);
        if(!data)return 2;
        static wx_lpack lp;
        unsigned k=0;
        for(size_t at=0;at+4<=n;k++) {
            const uint8_t *copy[2];uint32_t len[2];
            for(int c=0;c<2;c++){len[c]=wx_lp_le32(data+at);at+=4;copy[c]=data+at;at+=len[c];}
            if(at>n)break;
            uint8_t head[2][16];
            for(int c=0;c<2;c++){memset(head[c],0xff,16);memcpy(head[c],copy[c],len[c]<16?len[c]:16);}
            int first=wx_lpack_b_first(head[0],head[1]),which=-1;
            for(int t=0;t<2&&which<0;t++){int c=t?!first:first;if(wx_lpack_parse(copy[c],len[c],&lp))which=c;}
            printf("LPICK %u %s %lu\n",k,which<0?"-":which?"B":"A",which<0?0ul:(unsigned long)lp.sequence);
        }
        free(data);
        return 0;
    }
    if(!strcmp(argv[1],"records")&&argc==3) {
        size_t n=0;uint8_t *data=load(argv[2],&n);
        if(!data)return 2;
        for(size_t i=0;i+WX_PLACE_RECORD<=n;i+=WX_PLACE_RECORD) {
            wx_place p;
            int ok=wx_place_parse(data+i,WX_PLACE_RECORD,&p);
            printf("REC %u %d ",(unsigned)(i/WX_PLACE_RECORD),ok);
            if(ok)show(&p);else printf("\n");
            /* a refused record leaves nothing behind */
            if(!ok){const uint8_t *b=(const uint8_t *)&p;for(size_t k=0;k<sizeof(p);k++)if(b[k]){printf("FAIL refused record %u left data\n",(unsigned)(i/WX_PLACE_RECORD));break;}}
        }
        free(data);
        return 0;
    }
    if(!strcmp(argv[1],"pick")&&argc==3) {
        size_t n=0;uint8_t *data=load(argv[2],&n);
        if(!data)return 2;
        for(size_t i=0;i+2*WX_PLACE_RECORD<=n;i+=2*WX_PLACE_RECORD) {
            wx_place p,spare;
            memset(&spare,0x5a,sizeof(spare));
            int ok=wx_place_pick(data+i,data+i+WX_PLACE_RECORD,WX_PLACE_RECORD,&p,&spare);
            printf("PICK %u %d %lu %s\n",(unsigned)(i/(2*WX_PLACE_RECORD)),ok,ok?(unsigned long)p.sequence:0ul,ok?p.name:"-");
        }
        free(data);
        return 0;
    }
    if(!strcmp(argv[1],"requests")) {
        wx_place p;
        const wx_place *use=NULL;
        if(argc==3) {
            size_t n=0;uint8_t *data=load(argv[2],&n);
            if(!data||n<WX_PLACE_RECORD||!wx_place_parse(data,n,&p)){printf("BAD record\n");return 1;}
            use=&p;free(data);attach(&p);
        }
        char url[304];
        unsigned count=wx_place_request_count(use);
        for(unsigned i=0;i<count;i++) {
            wx_req_info info,info2;
            if(!wx_place_request(use,i,&info,url,sizeof(url))||!wx_place_info(use,i,&info2)||info2.kind!=info.kind||info2.slot!=info.slot
               ||info2.period_s!=info.period_s||info2.host!=info.host){printf("FAIL request %u\n",i);return 1;}
            int host_ok=info.host==wx_host_api||info.host==wx_host_radar;
            printf("REQ %u %u %u %lu %d %s\n",i,info.kind,info.slot,(unsigned long)info.period_s,host_ok,url);
        }
        wx_req_info none;
        printf("END %u %d\n",count,wx_place_request(use,count,&none,url,sizeof(url)));
        return 0;
    }
    if(!strcmp(argv[1],"tz")&&argc>=5) {
        wx_tz z={(int16_t)atoi(argv[2]),(uint8_t)atoi(argv[3])};
        for(int a=4;a<argc;a++) {
            int64_t e=strtoll(argv[a],NULL,10);
            wx_time t;
            wx_local_time_z(e,z,&t);
            printf("TZ %lld %d %04d-%02d-%02d %02d:%02d:%02d %d\n",(long long)e,wx_tz_offset_min(e,z),t.year,t.month,t.day,t.hour,t.minute,t.second,t.dow);
        }
        return 0;
    }
    if(!strcmp(argv[1],"nodata")&&argc==5) { /* 1.6.4: nothing held yet (no Wi-Fi, or the first round still out) */
        static wx_state s;
        static uint16_t fb[WX_W*WX_H_PX];
        wx_init(&s);wx_set_place(&s,NULL);s.updating=(uint8_t)atoi(argv[4]);
        int64_t epoch=strtoll(argv[3],NULL,10);
        char line[64],name[96];
        for(unsigned i=0;i<6;i++){wx_ticker_line(&s,i,line,sizeof(line));printf("TICKER %u %s|%d\n",i,line,wx_text_w(line,WX_SILK));}
        wx_enter(&s,0);
        for(unsigned i=0;i<s.n_pages&&i<3;i++) {
            s.page=(uint8_t)i;wx_draw(&s,fb,1000,epoch);
            snprintf(name,sizeof(name),"nodata-%u-%02u",(unsigned)s.updating,i);write_frame(argv[2],name,fb);
        }
        return 0;
    }
    if(!strcmp(argv[1],"pages")&&argc==6) {
        static wx_state s;
        static wx_fetch f;
        static uint16_t fb[WX_W*WX_H_PX];
        wx_place p;
        const wx_place *use=NULL;
        if(strcmp(argv[2],"-")) {
            size_t n=0;uint8_t *data=load(argv[2],&n);
            if(!data||n<WX_PLACE_RECORD||!wx_place_parse(data,n,&p)){printf("BAD record\n");return 1;}
            use=&p;free(data);attach(&p);
        }
        int64_t epoch=strtoll(argv[5],NULL,10);
        wx_init(&s);wx_set_place(&s,use);
        char url[304],name[320],path[900];
        int fetched=0,committed=0,missing=0;
        for(int round=0;round<2;round++) /* the second round asks the "recent reports" the first showed were needed */
        for(unsigned i=0;i<wx_place_request_count(use);i++) {
            wx_req_info info;
            if(!wx_place_request(use,i,&info,url,sizeof(url))||info.kind==WX_K_RADAR)continue;
            if(!wx_kind_wanted(&s,info.kind,info.slot)||(round&&info.kind!=WX_K_RECENT))continue;
            slug(url,name,sizeof(name));
            snprintf(path,sizeof(path),"%s/%s.json",argv[3],name);
            size_t n=0;uint8_t *body=load(path,&n);
            if(!body){printf("MISSING %s\n",url);missing++;continue;}
            wx_fetch_begin_as(&f,info.kind,info.slot);
            if(use)wx_fetch_zone(&f,use->tz);
            wx_fetch_clock(&f,epoch);
            for(size_t at=0;at<n;at+=1024)if(!wx_fetch_feed(&f,body+at,n-at<1024?n-at:1024))break;
            wx_state before=s;
            wx_fetch_commit(&s,&f,200,epoch);
            committed+=memcmp(&before,&s,sizeof(s))!=0;
            fetched++;
            free(body);
        }
        printf("FETCHED %d COMMITTED %d MISSING %d\n",fetched,committed,missing);
        { /* 1.6.4: a radar picture kept beside the replies (radar.gif: its long address would pass Windows' path limit)
           * is decoded and shown, taken 4 minutes before, when this place asks for a radar picture at all */
            static uint8_t table[WX_GIF_TABLE],page[WX_RADAR_BYTES];
            for(unsigned i=0;i<wx_place_request_count(use);i++) {
                wx_req_info info;
                if(!wx_place_request(use,i,&info,url,sizeof(url))||info.kind!=WX_K_RADAR)continue;
                snprintf(path,sizeof(path),"%s/radar.gif",argv[3]);
                size_t n=0;uint8_t *gif=load(path,&n);
                if(!gif)continue;
                wx_gif g;
                wx_gif_begin(&g,table,page,WX_RADAR_W,WX_RADAR_H);
                for(size_t at=0;at<n;at+=1024)if(!wx_gif_feed(&g,gif+at,n-at<1024?n-at:1024))break;
                if(wx_gif_ok(&g)){wx_radar_despeckle(page,WX_RADAR_W,WX_RADAR_H);wx_set_radar(&s,page,WX_RADAR_W,WX_RADAR_H,epoch-240);}
                printf("RADAR %d\n",wx_gif_ok(&g));
                free(gif);
            }
        }
        wx_enter(&s,0);
        uint32_t now=0;
        for(int k=0;k<400;k++)wx_tick(&s,now,epoch); /* the almanac's moon search */
        printf("PAGES %u",s.n_pages);
        for(unsigned i=0;i<s.n_pages;i++)printf(" %s",kind_name[s.pages[i][0]]);
        printf("\n");
        printf("HOME temp=%d sky=%s local=%u ext=%u alerts=%u rise=%d set=%d moon=%u\n",s.obs[0].temp_f,s.obs[0].sky,s.fc.n_local,s.fc.n_ext,s.alerts,
               s.alm.rise[0],s.alm.set[0],s.alm.moon_ready);
        printf("NAME %s|%s|%s|%s\n",wx_home_name(&s),wx_home_area(&s),wx_tables(&s)?wx_tables(&s)->state:"-",wx_region(&s)?wx_region(&s)->state:"-");
        for(unsigned i=0;i<wx_obs_rows(&s);i++)printf("ROW %u %s %d %s\n",i,wx_obs_label_at(&s,i),s.obs[wx_obs_slot_at(&s,i)].temp_f,s.obs[wx_obs_slot_at(&s,i)].sky);
        char clock[24];
        wx_clock_text_z(epoch,wx_state_tz(&s),clock,sizeof(clock),1);
        printf("CLOCK %s\n",clock);
        unsigned seen[WX_P_KINDS]={0};
        for(unsigned i=0;i<s.n_pages;i++) {
            s.page=(uint8_t)i;
            unsigned kind=s.pages[i][0];
            wx_draw(&s,fb,1000,epoch);
            for(size_t k=0;k<WX_W*WX_H_PX;k++)if(fb[k]==0xF81Fu){printf("FAIL see-through key drawn on page %u\n",i);break;}
            snprintf(name,sizeof(name),"place-%02u-%s-%u",i,kind_name[kind],++seen[kind]);
            write_frame(argv[4],name,fb);
        }
        return 0;
    }
    return 2;
}
