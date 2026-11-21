/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC check of the Local Weather channel (wx.h): the streaming parse of the real National Weather
 * Service replies kept in the PC reference's kept replies (not published), every failure path, the page cycle, the
 * ticker, the almanac, the sound feed, and a render of every page from the console's own drawing
 * code. Built and run by scripts/test_chromatic_weather.py, which also compares the VAL lines
 * with the PC reference's figures and turns the frames into PNG previews. No network.
 *
 *   check_weather selftest <cache folder> <frame folder> <radar fixture folder>
 *   check_weather almanac <UTC seconds>
 */
#include <stdio.h>
#include <stdlib.h>
#include "wx.h"

static int failures,checks;
#define CHECK(cond,...) do{checks++;if(!(cond)){failures++;printf("FAIL %s:%d: ",__FILE__,__LINE__);printf(__VA_ARGS__);printf("\n");}}while(0)
#define PASS(name) printf("%s %s\n",failures==before?"PASS":"FAIL",name)

static const char *cache_dir,*out_dir,*fixture_dir;
static uint32_t rng_state=12345u;
static uint32_t rnd(void){rng_state=rng_state*1664525u+1013904223u;return rng_state>>8;}

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
/* The reference's cache file of a request (weather_data.py Fetcher._slug). */
static uint8_t *load_request(unsigned index,size_t *size) {
    char path[512],slug[200];
    const wx_request *r=wx_request_at(index);
    size_t n=0;
    int gap=0;
    snprintf(path,sizeof(path),"%s%s",r->host,r->path);
    for(const char *p=path;*p&&n+2<sizeof(slug);p++) {
        int keep=(*p>='A'&&*p<='Z')||(*p>='a'&&*p<='z')||(*p>='0'&&*p<='9')||*p=='.'||*p=='-';
        if(keep){if(gap&&n)slug[n++]='_';gap=0;slug[n++]=*p;}else gap=1;
    }
    slug[n]=0;
    snprintf(path,sizeof(path),"%s/%s.json",cache_dir,slug);
    return load(path,size);
}
static void parse(wx_fetch *f,unsigned index,int64_t epoch,const uint8_t *data,size_t size,size_t chunk) {
    wx_fetch_begin(f,index);
    wx_fetch_clock(f,epoch);
    for(size_t at=0;at<size;at+=chunk)if(!wx_fetch_feed(f,data+at,size-at<chunk?size-at:chunk))break;
}
static __attribute__((noinline)) void probe_draw(const wx_state *s,uint16_t *fb,uint32_t ms,int64_t e){wx_draw(s,fb,ms,e);}
static __attribute__((noinline)) int probe_tick(wx_state *s,uint32_t ms,int64_t e){return wx_tick(s,ms,e);}
static __attribute__((noinline)) int probe_feed(wx_fetch *f,const uint8_t *d,size_t n){return wx_fetch_feed(f,d,n);}
static __attribute__((noinline)) void probe_commit(wx_state *s,const wx_fetch *f,int st,int64_t e){wx_fetch_commit(s,f,st,e);}
static __attribute__((noinline)) void probe_audio(wx_state *s,uint32_t ms,wx_audio_write w,void *c){wx_audio_feed(s,ms,w,c);}

static uint16_t frame[WX_W*WX_H_PX+64];
static void guard_set(void){for(int i=0;i<64;i++)frame[WX_W*WX_H_PX+i]=0xA5A5;}
static int guard_ok(void){for(int i=0;i<64;i++)if(frame[WX_W*WX_H_PX+i]!=0xA5A5)return 0;return 1;}
static int frame_legal(void) { /* only palette colours, never the overlay's see-through key */
    for(int i=0;i<WX_W*WX_H_PX;i++) {
        int k=0;
        if(frame[i]==0xF81F)return 0;
        while(k<16&&wx_palette[k]!=frame[i])k++;
        if(k==16)return 0;
    }
    return 1;
}
static void save_frame(const char *prefix,int number,const char *name) {
    char path[512];
    snprintf(path,sizeof(path),"%s/%s%02d-%s.rgb565",out_dir,prefix,number,name);
    FILE *f=fopen(path,"wb");
    if(!f){failures++;printf("FAIL cannot write %s\n",path);return;}
    fwrite(frame,2,WX_W*WX_H_PX,f);
    fclose(f);
}
static const char *page_name(const wx_state *s,unsigned page) {
    static char name[40];
    static const char *const base[WX_P_KINDS]={"ident","current-conditions","current-conditions","latest-observations","almanac",
        "local-forecast","extended-forecast","regional-state","regional-close","rain-outlook","current-radar"};
    unsigned kind=s->pages[page][0],n=1;
    for(unsigned i=0;i<page;i++)if(s->pages[i][0]==kind||(kind==WX_P_CUR2&&s->pages[i][0]==WX_P_CUR1))n++;
    if(kind==WX_P_CUR1||kind==WX_P_CUR2||kind==WX_P_OBS||kind==WX_P_LOCAL||kind==WX_P_EXT)snprintf(name,sizeof(name),"%s-%u",base[kind],n);
    else snprintf(name,sizeof(name),"%s",base[kind]);
    return name;
}
/* Every page of the cycle drawn and saved; returns the number of pages. */
static unsigned render_all(wx_state *s,const char *prefix,int64_t epoch) {
    wx_enter(s,0);
    for(int i=0;i<400;i++)probe_tick(s,(uint32_t)i,epoch); /* lets the almanac finish */
    wx_enter(s,0);
    probe_tick(s,0,epoch);
    unsigned n=s->n_pages;
    uint32_t ms=0;
    for(unsigned p=0;p<n;p++) {
        while(s->page!=p){ms+=20;probe_tick(s,ms,epoch+ms/1000);}
        guard_set();
        probe_draw(s,frame,ms+1500,epoch+(ms+1500)/1000);
        CHECK(guard_ok(),"page %u wrote past the frame",p);
        CHECK(frame_legal(),"page %u drew a colour outside the palette",p);
        if(prefix)save_frame(prefix,(int)p,page_name(s,p));
    }
    return n;
}
static int64_t ref_epoch;
/* A radar fixture decoded the way the console does it (1 KB pieces) and cleaned; 1 when whole. */
static uint8_t radar_table[WX_GIF_TABLE];
static int load_radar(const char *name,uint8_t *page) {
    char path[512];
    size_t size;
    wx_gif g;
    snprintf(path,sizeof(path),"%s/%s",fixture_dir,name);
    uint8_t *data=load(path,&size);
    if(!data)return 0;
    wx_gif_begin(&g,radar_table,page,WX_RADAR_W,WX_RADAR_H);
    for(size_t at=0;at<size;at+=1024)if(!wx_gif_feed(&g,data+at,size-at<1024?size-at:1024))break;
    free(data);
    if(!wx_gif_ok(&g))return 0;
    wx_radar_despeckle(page,WX_RADAR_W,WX_RADAR_H);
    return 1;
}
/* A whole refresh from the cache, the way the console does it: each request in turn, the
 * conditional ones only when wanted. Returns the bytes read. */
static size_t refresh(wx_state *s,wx_fetch *f,size_t chunk,int *missing) {
    size_t total=0;
    for(unsigned i=0;i<wx_request_count();i++) {
        size_t size;
        if(!wx_request_wanted(s,i)||wx_request_at(i)->kind==WX_K_RADAR)continue;
        uint8_t *data=load_request(i,&size);
        if(!data){if(missing)(*missing)++;continue;}
        parse(f,i,ref_epoch,data,size,chunk);
        probe_commit(s,f,200,ref_epoch);
        total+=size;
        free(data);
    }
    return total;
}
static const char *na(int v){static char b[32][16];static int k;k=(k+1)&31;if(v==WX_NA)return "-";snprintf(b[k],16,"%d",v);return b[k];}
static void print_values(const wx_state *s) {
    static const char *const icons[]={"none","sun","moon","pc_day","pc_night","cloud","rain","storm","snow","fog"};
    static const char *const trend[]={"-","RISING","FALLING","STEADY"};
    static const char *const kinds[]={"-","HIGH","LOW"};
    const wx_obs *o=&s->obs[0];
    const char *when,*kind;
    printf("VAL cur %s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%d|%s|%s|%s\n",na(o->temp_f),na(o->dew_f),na(o->humidity),na(o->wind_mph),
           o->wind_dir<0?"-":wx_compass[o->wind_dir],na(o->gust_mph),na(o->pressure),na(o->vis10),
           o->ceiling==WX_NA?"-":na(o->ceiling*100),na(o->feels_f),o->feels_kind,o->sky,trend[s->trend],
           icons[wx_classify_icon(o->sky,!wx_is_night(s,ref_epoch))]);
    for(int i=0;i<WX_OBS_ROWS;i++) {
        char brief[9];
        o=&s->obs[WX_DEFAULT_PACK->obs_slot[i]];
        wx_short_sky(o->sky,brief);
        printf("VAL obs%d %s|%s|%s|%s|%s|%s\n",i,WX_DEFAULT_PACK->obs_label[i],na(o->temp_f),na(o->wind_mph),o->wind_dir<0?"-":wx_compass[o->wind_dir],o->sky,brief);
    }
    for(int i=0;i<s->fc.n_local;i++)printf("VAL local%d %s|%d|%s\n",i,s->fc.local[i].name,s->fc.local[i].is_day,s->fc.local[i].text);
    for(int i=0;i<s->fc.n_ext;i++) {
        const wx_ext *e=&s->fc.ext[i];
        printf("VAL ext%d %s|%s|%s/%s|%s|%s|%d\n",i,wx_days3[e->dow&7],icons[e->icon%WX_ICON_COUNT],e->sky[0],e->sky[1],na(e->hi),na(e->lo),e->pop);
    }
    for(int i=0;i<WX_CITIES;i++)printf("VAL city%d %s|%s|%s|%s\n",i,WX_DEFAULT_PACK->city_name[i],na(s->city[i].temp),icons[s->city[i].icon%WX_ICON_COUNT],kinds[s->city[i].kind%3]);
    wx_regional_words(s,&when,&kind);
    printf("VAL regional %s|%s\n",when,kind);
    printf("VAL alerts %d\n",s->alerts);
    for(unsigned i=0;i<WX_TICKER_LINES;i++){char a[64];wx_ticker_line(s,i,a,sizeof(a));for(char *p=a;*p;p++)if(*p==0x7f)*p='^';printf("VAL ticker%u %s\n",i,a);}
}
static void print_almanac(int64_t epoch) {
    static const char *const phase[4]={"NEW","FIRST","FULL","LAST"};
    wx_almanac a;
    int steps=0;
    memset(&a,0,sizeof(a));
    wx_almanac_begin(&a,epoch,WX_DEFAULT_PACK->lat,WX_DEFAULT_PACK->lon);
    while(!wx_almanac_step(&a)&&steps<10000)steps++;
    printf("ALM %s %d %d %s %d %d|%s|%d",wx_days3[a.dow[0]],a.rise[0],a.set[0],wx_days3[a.dow[1]],a.rise[1],a.set[1],wx_moon_names[a.moon_name],a.moon_pct);
    for(int i=0;i<4;i++)printf("|%s %d-%d",phase[a.next_phase[i]&3],a.next_month[i],a.next_day[i]);
    printf("|steps %d|offset %d\n",steps,wx_eastern_offset(epoch));
}

/* ---- sound sinks ---- */
typedef struct {uint8_t *bytes;uint8_t *starts;size_t n,cap;int mode;size_t most;} sink;
static size_t sink_write(void *ctx,const uint8_t *data,size_t size,int start) {
    sink *k=ctx;
    size_t take=size;
    if(size>k->most)k->most=size;
    if(k->mode==1)take=rnd()%(size+1);       /* any amount, often nothing */
    else if(k->mode==2)take=0;               /* a full ring */
    else if(k->mode==3)take=size>37?37:size; /* small pieces */
    if(k->n+take>k->cap)take=k->cap-k->n;
    for(size_t i=0;i<take;i++){k->bytes[k->n+i]=data[i];k->starts[k->n+i]=(uint8_t)(start&&i==0);}
    k->n+=take;
    return take;
}
static size_t decode_stream(const sink *k,int16_t *pcm,size_t cap) { /* what the console's sound task does (jf_audio.h) */
    jf_ima ima;
    size_t n=0;
    jf_ima_init(&ima);
    for(size_t i=0;i<k->n;i++) {
        int16_t two[3];
        if(k->starts[i])jf_ima_block_start(&ima);
        size_t got=jf_ima_feed(&ima,k->bytes+i,1,two,3);
        for(size_t j=0;j<got&&n<cap;j++)pcm[n++]=two[j];
    }
    return n;
}
static size_t decode_clip(unsigned clip,int16_t *pcm,size_t cap) { /* a stored clip, by the same independent decoder */
    jf_ima ima;
    size_t n=0;
    jf_ima_init(&ima);
    for(uint32_t at=0;at<wx_clips[clip].bytes;at+=WX_SND_BLOCK) {
        int16_t out[2*WX_SND_BLOCK+1];
        jf_ima_block_start(&ima);
        size_t got=jf_ima_feed(&ima,wx_clips[clip].data+at,WX_SND_BLOCK,out,2*WX_SND_BLOCK+1);
        for(size_t j=0;j<got&&n<cap;j++)pcm[n++]=out[j];
    }
    return n;
}
static double snr_db(const int16_t *ref,const int16_t *got,size_t n) {
    double sig=0,noise=0;
    for(size_t i=0;i<n;i++){double d=(double)ref[i]-got[i];sig+=(double)ref[i]*ref[i];noise+=d*d;}
    return noise<=0?99.0:10.0*log10(sig/noise);
}
static void run_feed(wx_state *s,sink *k,uint32_t from_ms,uint32_t to_ms,uint32_t step) {
    for(uint32_t ms=from_ms;ms<to_ms;ms+=step)probe_audio(s,ms,sink_write,k);
}

static void test_sound(void) {
    int before=failures;
    static wx_state a,b;
    enum {SECONDS=95};
    size_t cap=(size_t)SECONDS*9000u;
    sink all={malloc(cap),malloc(cap),0,cap,0,0},rand={malloc(cap),malloc(cap),0,cap,1,0},small={malloc(cap),malloc(cap),0,cap,3,0};
    /* 1: the stream does not depend on how the sink takes it */
    wx_init(&a);wx_enter(&a,0);run_feed(&a,&all,0,SECONDS*1000u,16);
    wx_init(&b);wx_enter(&b,0);run_feed(&b,&rand,0,SECONDS*1000u,4);
    size_t n=all.n<rand.n?all.n:rand.n;
    CHECK(rand.n>all.n*9/10,"a sink taking random amounts starved: %zu of %zu bytes",rand.n,all.n);
    CHECK(!memcmp(all.bytes,rand.bytes,n)&&!memcmp(all.starts,rand.starts,n),"stream differs with a sink taking random amounts");
    wx_init(&b);wx_enter(&b,0);run_feed(&b,&small,0,SECONDS*1000u,5);
    n=all.n<small.n?all.n:small.n;
    CHECK(small.n>all.n*9/10&&!memcmp(all.bytes,small.bytes,n)&&!memcmp(all.starts,small.starts,n),"stream differs with 37-byte pieces");
    for(size_t i=0;i<all.n;i++)if((all.starts[i]!=0)!=(i%WX_SND_BLOCK==0)){CHECK(0,"block start flag wrong at byte %zu",i);break;}
    CHECK(all.most<=WX_SND_BLOCK,"offered %zu bytes at once",all.most);
    /* 2: pacing against real time (16,000 samples a second, 505 samples in 256 bytes) */
    {
        static wx_state c;
        sink k={malloc(cap),malloc(cap),0,cap,0,0};
        double worst_ahead=0,worst_behind=0;
        wx_init(&c);wx_enter(&c,0);
        for(uint32_t ms=0;ms<60000u;ms+=16) {
            probe_audio(&c,ms,sink_write,&k);
            double real=(double)ms*16.0*WX_SND_BLOCK/WX_SND_PER,ahead=(double)k.n-real;
            if(ahead>worst_ahead)worst_ahead=ahead;
            if(ms>500&&-ahead+WX_SND_AHEAD>worst_behind)worst_behind=-ahead+WX_SND_AHEAD;
        }
        CHECK(worst_ahead<=WX_SND_AHEAD+1.0,"ran %.0f bytes ahead of real time (limit %u)",worst_ahead,WX_SND_AHEAD);
        CHECK(worst_behind<=WX_SND_BLOCK+16*8.2,"fell %.0f bytes behind a sink that takes everything",worst_behind);
        printf("INFO sound pacing: at most %.0f bytes ahead of real time (ring %u), at most %.0f behind full\n",worst_ahead,WX_SND_AHEAD,worst_behind);
        sink full={malloc(64),malloc(64),0,64,2,0};
        wx_init(&c);wx_enter(&c,0);
        for(uint32_t ms=0;ms<3000u;ms+=16)probe_audio(&c,ms,sink_write,&full);
        CHECK(full.n==0&&c.snd.fed==0,"a full sink was fed");
        probe_audio(&c,3016,NULL,NULL); /* no sink at all */
        free(k.bytes);free(k.starts);free(full.bytes);free(full.starts);
    }
    /* 3: what comes out, against the stored clips decoded independently */
    {
        size_t pcm_cap=(size_t)SECONDS*16200u,mn=wx_clips[WX_CLIP_MUSIC].samples;
        int16_t *got=malloc(pcm_cap*2),*music=malloc((mn+8)*2),*want=malloc(pcm_cap*2);
        size_t gn=decode_stream(&all,got,pcm_cap),dn=decode_clip(WX_CLIP_MUSIC,music,mn);
        CHECK(dn==mn,"music clip decodes to %zu samples, table says %zu",dn,mn);
        CHECK(gn>=(size_t)SECONDS*15900u,"decoded %zu samples in %d s",gn,SECONDS);
        /* the feed's music: stored sample k at output 2k+4 (the interpolator looks two stored samples
         * ahead), the 4-point value between; the loop wraps */
        for(size_t i=0;i<gn;i++) {
            long k=((long)i-4)/2;
            if((long)i<4){want[i]=0;continue;}
            #define M(j) (((j)<0)?0:music[(size_t)(j)%mn])
            if(!((i-4)&1))want[i]=M(k);
            else{int v=(9*((int)M(k)+M(k+1))-M(k-1)-M(k+2)+8)>>4;want[i]=(int16_t)(v>32767?32767:v<-32768?-32768:v);}
            #undef M
        }
        /* a: music only (the opening card has no line): seconds 1..5 */
        double plain=snr_db(want+16000,got+16000,64000);
        CHECK(plain>=25.0,"music re-encode SNR %.1f dB",plain);
        /* b: across the loop join (the loop is %u samples at 16 kHz) and well past it */
        size_t join=mn*2+4;
        CHECK(gn>join+16000,"not enough sound to reach the loop join");
        double seam=snr_db(want+join-4000,got+join-4000,8000);
        CHECK(seam>=25.0,"SNR across the loop join %.1f dB",seam);
        printf("INFO sound: music-only SNR %.1f dB; across the loop join %.1f dB (join at %.2f s)\n",plain,seam,(double)join/16000.0);
        /* the join itself is part of the tune: no step larger than the tune's own largest step */
        int step_join=0,step_max=0;
        for(size_t i=1;i<mn;i++){int d=abs(music[i]-music[i-1]);if(d>step_max)step_max=d;}
        step_join=abs(music[0]-music[mn-1]);
        CHECK(step_join<=step_max,"loop join step %d against %d inside the tune",step_join,step_max);
        printf("INFO sound: step at the loop join %d, largest step inside the tune %d\n",step_join,step_max);
        free(got);free(music);free(want);
    }
    /* 4: a line starts WX_SND_LINE_DELAY samples after it is asked for, the music ducks and comes back */
    {
        static wx_state c;
        wx_init(&c);wx_enter(&c,0);
        for(int i=0;i<20000;i++)wx_sound_sample(&c.snd);
        wx_sound_say(&c.snd,WX_CLIP_LOCAL);
        for(unsigned i=0;i<WX_SND_LINE_DELAY;i++)wx_sound_sample(&c.snd);
        CHECK(!c.snd.line.active&&c.snd.pending==WX_CLIP_LOCAL,"line started early");
        wx_sound_sample(&c.snd);
        CHECK(c.snd.line.active&&c.snd.line.clip==WX_CLIP_LOCAL&&!c.snd.pending,"line did not start on time");
        CHECK(c.snd.level==WX_SND_DUCK,"music level %u under a line (want %u)",c.snd.level,WX_SND_DUCK);
        unsigned len=0;
        while(c.snd.line.active&&len<200000u){wx_sound_sample(&c.snd);len++;}
        CHECK(len>=wx_clips[WX_CLIP_LOCAL].samples&&len<=wx_clips[WX_CLIP_LOCAL].samples+2,"line lasted %u samples, clip is %u",len,(unsigned)wx_clips[WX_CLIP_LOCAL].samples);
        for(int i=0;i<4000;i++)wx_sound_sample(&c.snd);
        CHECK(c.snd.level==256,"music did not come back up (%u)",c.snd.level);
        /* the tone is stored at half rate: twice its stored samples */
        wx_sound_say(&c.snd,WX_CLIP_ALERT);
        for(unsigned i=0;i<=WX_SND_LINE_DELAY;i++)wx_sound_sample(&c.snd);
        len=0;
        while(c.snd.line.active&&len<200000u){wx_sound_sample(&c.snd);len++;}
        CHECK(len>=wx_clips[WX_CLIP_ALERT].samples*2&&len<=wx_clips[WX_CLIP_ALERT].samples*2+4,"tone lasted %u samples",len);
        wx_sound_say(&c.snd,0);wx_sound_say(&c.snd,99); /* nonsense is ignored */
        CHECK(!c.snd.pending,"a bad clip number was taken");
    }
    /* 5: a line under the mix is still the line: mix minus ducked music against the clip */
    {
        static wx_state c;
        size_t ln=wx_clips[WX_CLIP_CURRENT].samples;
        int16_t *line=malloc(ln*2),*mix=malloc(ln*2),*bed=malloc(ln*2);
        static wx_state d;
        decode_clip(WX_CLIP_CURRENT,line,ln);
        wx_init(&c);wx_enter(&c,0);wx_init(&d);wx_enter(&d,0);
        for(int i=0;i<30000;i++){wx_sound_sample(&c.snd);wx_sound_sample(&d.snd);}
        wx_sound_say(&c.snd,WX_CLIP_CURRENT);
        for(unsigned i=0;i<=WX_SND_LINE_DELAY;i++){wx_sound_sample(&c.snd);wx_sound_sample(&d.snd);}
        mix[0]=0;bed[0]=0;
        /* the sample that started the line is the one just made; make the rest */
        size_t n=0;
        for(;n<ln-1;n++){mix[n]=(int16_t)wx_sound_sample(&c.snd);bed[n]=(int16_t)((wx_sound_sample(&d.snd)*(int)WX_SND_DUCK)>>8);}
        for(size_t i=0;i<n;i++)mix[i]=(int16_t)(mix[i]-bed[i]);
        double exact=snr_db(line+1,mix,n);
        CHECK(exact>=40.0,"line in the mix: %.1f dB",exact);
        printf("INFO sound: an announcer line in the mix matches its clip to %.1f dB before the output encode\n",exact);
        free(line);free(mix);free(bed);
    }
    free(all.bytes);free(all.starts);free(rand.bytes);free(rand.starts);free(small.bytes);free(small.starts);
    PASS("sound feed (framing, pacing, fidelity, loop join, lines)");
}

static void test_parse(void) {
    static wx_fetch f[5],g;
    static wx_state s,t;
    static const size_t chunks[5]={1,7,512,1024,1u<<24};
    int before=failures,files=0;
    size_t bytes=0;
    for(unsigned i=0;i<wx_request_count();i++) {
        size_t size;
        uint8_t *data=load_request(i,&size);
        if(!data)continue;
        files++;bytes+=size;
        for(int c=0;c<5;c++)parse(&f[c],i,ref_epoch,data,size,chunks[c]);
        for(int c=1;c<5;c++)CHECK(!memcmp(&f[0],&f[c],sizeof(wx_fetch)),"request %u (%s): chunk %zu differs from chunk 1",i,wx_request_at(i)->path,chunks[c]);
        CHECK(f[0].done||wx_json_complete(&f[0].js),"request %u (%s) neither complete nor done",i,wx_request_at(i)->path);
        /* cut at every sixteenth, in three chunk sizes: never committed unless everything wanted was read */
        for(int cut=0;cut<16;cut++)for(int c=0;c<3;c++) {
            size_t part=size*(size_t)cut/16;
            wx_init(&s);
            parse(&g,i,ref_epoch,data,part,chunks[c]);
            t=s;
            probe_commit(&s,&g,200,ref_epoch);
            if(!g.done)CHECK(!memcmp(&s,&t,sizeof(s)),"request %u cut at %d/16 was committed",i,cut);
            else{parse(&f[4],i,ref_epoch,data,size,1024);CHECK(!memcmp(&g.u,&f[4].u,sizeof(g.u)),"request %u cut at %d/16: done but differs from the whole reply",i,cut);}
        }
        free(data);
    }
    CHECK(files>=23,"only %d cached replies found in %s",files,cache_dir);
    printf("INFO parsed %d cached replies (%zu bytes) in chunks of 1, 7, 512, 1024 and whole\n",files,bytes);
    PASS("cached replies: same result in every chunk size; cut-off replies never committed");
}

static void test_values(void) {
    static wx_state s,t;
    static wx_fetch f;
    int before=failures,missing=0;
    wx_init(&s);
    size_t total=refresh(&s,&f,1024,&missing);
    wx_init(&t);
    refresh(&t,&f,3,NULL);
    for(int i=0;i<600;i++){probe_tick(&s,(uint32_t)i,ref_epoch);probe_tick(&t,(uint32_t)i,ref_epoch);}
    CHECK(!memcmp(s.obs,t.obs,sizeof(s.obs))&&!memcmp(&s.fc,&t.fc,sizeof(s.fc))&&!memcmp(s.city,t.city,sizeof(s.city)),"refresh in 3-byte chunks differs");
    CHECK(s.alm.moon_ready,"almanac not finished after 600 ticks");
    printf("INFO one refresh from the cache: %zu bytes, %d requests without a cached reply\n",total,missing);
    print_values(&s);
    print_almanac(ref_epoch);
    /* the radar picture: the kept real frame (a dry evening), taken when the reference fetched it */
    static uint8_t dry[WX_RADAR_BYTES],rain[WX_RADAR_BYTES];
    int64_t taken=wx_parse_time("2026-11-19T23:25:25+00:00",NULL);
    CHECK(load_radar("radar-dry.gif",dry)&&load_radar("radar-pattern.gif",rain),"radar fixtures did not decode");
    wx_set_radar(&s,dry,WX_RADAR_W,WX_RADAR_H,taken);
    unsigned n=render_all(&s,"weather-",ref_epoch);
    CHECK(n>=12&&n<=WX_MAX_PAGES,"%u pages",n);
    CHECK(s.pages[n-1][0]==WX_P_RADAR&&wx_page_line(&s,n-1)==WX_CLIP_RADAR,"the last page is not Current Radar with its line");
    /* made-up showers, to see the page with rain; then no picture: the rain outlook takes the page's place */
    wx_set_radar(&s,rain,WX_RADAR_W,WX_RADAR_H,ref_epoch-240);
    probe_tick(&s,0,ref_epoch);s.page=(uint8_t)(s.n_pages-1);
    guard_set();probe_draw(&s,frame,1500,ref_epoch);save_frame("weather-radar-",0,"made-up-showers-test-pattern");
    CHECK(guard_ok()&&frame_legal(),"radar frame");
    wx_set_radar(&s,rain,WX_RADAR_W+1,WX_RADAR_H,ref_epoch);
    CHECK(!s.radar,"a radar picture of the wrong size was taken");
    wx_set_radar(&s,rain,WX_RADAR_W,WX_RADAR_H,0);
    CHECK(!s.radar,"a radar picture without a time was taken");
    wx_set_radar(&s,NULL,0,0,0);
    probe_tick(&s,0,ref_epoch);
    CHECK(s.pages[s.n_pages-1][0]==WX_P_OUTLOOK&&!wx_page_line(&s,s.n_pages-1),"without a radar picture the last page is not the rain outlook");
    s.page=(uint8_t)(s.n_pages-1);
    guard_set();probe_draw(&s,frame,1500,ref_epoch);save_frame("weather-fallback-",0,"rain-outlook");
    /* a picture 46 minutes old is no longer "current" */
    wx_set_radar(&s,rain,WX_RADAR_W,WX_RADAR_H,ref_epoch-60);
    probe_tick(&s,10,ref_epoch+1);
    CHECK(s.pages[s.n_pages-1][0]==WX_P_RADAR,"a fresh radar picture is not shown");
    probe_tick(&s,20,ref_epoch+WX_RADAR_FRESH_S);
    CHECK(s.pages[s.n_pages-1][0]==WX_P_OUTLOOK,"a stale radar picture is still shown");
    wx_set_radar(&s,dry,WX_RADAR_W,WX_RADAR_H,taken);
    printf("INFO %u pages with data\n",n);
    /* ticker lines fit their bar */
    for(unsigned i=0;i<WX_TICKER_LINES;i++){char a[64];wx_ticker_line(&s,i,a,sizeof(a));CHECK(wx_text_w(a,WX_SILK)<=WX_TICKER_W,"ticker line %u is %d px wide",i,wx_text_w(a,WX_SILK));}
    PASS("a whole refresh from the cache; every page drawn");
}

static void test_failures(void) {
    static wx_state s,t;
    static wx_fetch f;
    int before=failures;
    size_t size;
    uint8_t *obs=load_request(0,&size);
    CHECK(obs!=NULL,"no cached reply for request 0");
    if(!obs)return;
    wx_init(&s);t=s;
    parse(&f,0,ref_epoch,obs,size,512);
    /* HTTP errors, a failed connection, a missing or wrong clock: nothing is committed */
    static const int bad_status[]={500,404,301,0,-1,-0x7200,204};
    for(unsigned i=0;i<sizeof(bad_status)/sizeof(bad_status[0]);i++){probe_commit(&s,&f,bad_status[i],ref_epoch);CHECK(!memcmp(&s,&t,sizeof(s)),"status %d was committed",bad_status[i]);}
    static const int64_t bad_clock[]={0,1000,WX_EPOCH_MIN-1,WX_EPOCH_MAX,-5};
    for(unsigned i=0;i<5;i++){probe_commit(&s,&f,200,bad_clock[i]);CHECK(!memcmp(&s,&t,sizeof(s)),"committed with clock %lld",(long long)bad_clock[i]);}
    /* a clock a day ahead: the observation is too old to show */
    probe_commit(&s,&f,200,ref_epoch+86400);
    CHECK(s.obs[0].temp_f==WX_NA,"a day-old observation was shown");
    wx_init(&s);
    probe_commit(&s,&f,200,ref_epoch);
    CHECK(s.obs[0].temp_f==55,"a good reply was not committed (temp %d)",s.obs[0].temp_f);
    /* it then ages away while the channel stays on */
    wx_enter(&s,0);
    for(int i=0;i<6*3600;i+=30)probe_tick(&s,(uint32_t)i*1000u,ref_epoch+i);
    CHECK(s.obs[0].temp_f==WX_NA,"an observation 6 h old is still shown");
    /* no network at all: five minutes of ticks, every page drawn, nothing but dashes */
    wx_init(&s);
    unsigned n=render_all(&s,"weather-nodata-",0);
    CHECK(n>=12,"only %u pages without data",n);
    {
        char a[32];
        wx_clock_text(0,a,sizeof(a),1);CHECK(!strcmp(a,"--:--"),"clock without time shows '%s'",a);
        wx_clock_text(1000,a,sizeof(a),1);CHECK(!strcmp(a,"--:--"),"clock in 1970 shows '%s'",a);
        wx_clock_text(ref_epoch,a,sizeof(a),1);CHECK(!strcmp(a,"6:46:36 PM"),"clock shows '%s'",a);
        CHECK(!s.alm.day&&!s.alm.moon_ready,"almanac worked out without a clock");
        /* 1.6.4: nothing held yet: a gentle word, not a bar of dashes (was "TEMP: --") */
        wx_ticker_line(&s,0,a,sizeof(a));CHECK(!strcmp(a,s.updating?"CONNECTING...":"WEATHER WILL APPEAR"),"ticker without data: '%s'",a);
        wx_ticker_line(&s,1,a,sizeof(a));CHECK(!strcmp(a,s.updating?"GETTING THE WEATHER...":"WHEN WI-FI CONNECTS"),"ticker without data: '%s'",a);
    }
    for(uint32_t ms=0;ms<300000u;ms+=16){if(probe_tick(&s,ms,0)){guard_set();probe_draw(&s,frame,ms,0);if(!guard_ok()){CHECK(0,"frame overrun");break;}}}
    CHECK(s.obs[0].temp_f==WX_NA&&!s.fc.n_local&&!s.alerts,"data appeared from nowhere");
    /* the clock known but no data: the almanac still works */
    wx_init(&s);
    render_all(&s,NULL,ref_epoch);
    CHECK(s.alm.moon_ready&&s.alm.rise[0]>0,"almanac needs only the clock");
    /* a slow network: one byte between main-loop passes, the channel running meanwhile */
    wx_init(&s);wx_enter(&s,0);
    wx_fetch_begin(&f,0);wx_fetch_clock(&f,ref_epoch);
    for(size_t i=0;i<size;i++){if(!probe_feed(&f,obs+i,1))break;if(!(i&63)){probe_tick(&s,(uint32_t)i,ref_epoch);probe_draw(&s,frame,(uint32_t)i,ref_epoch);}}
    probe_commit(&s,&f,200,ref_epoch);
    CHECK(s.obs[0].temp_f==55&&s.obs[0].pressure==3041,"trickled reply: temp %d pressure %d",s.obs[0].temp_f,s.obs[0].pressure);
    /* a bad request index */
    wx_fetch_begin(&f,9999);
    CHECK(!probe_feed(&f,obs,size)&&!wx_fetch_done(&f),"a bad request index was parsed");
    probe_commit(&s,&f,200,ref_epoch);
    free(obs);
    PASS("HTTP errors, failed connection, no clock, wrong clock, no network, slow network");
}

/* The two structs between guard bytes: anything written outside them shows. */
static struct {uint8_t a[64];wx_fetch f;uint8_t b[64];wx_state s;uint8_t c[64];} box;
static int box_ok(void){for(int i=0;i<64;i++)if(box.a[i]!=0x5a||box.b[i]!=0x5a||box.c[i]!=0x5a)return 0;return 1;}
#define s box.s
#define f box.f
static void test_garbage(void) {
    static uint8_t buf[70000];
    int before=failures;
    memset(box.a,0x5a,64);memset(box.b,0x5a,64);memset(box.c,0x5a,64);
    unsigned kinds[6]={0,1,2,3,4,22}; /* one request of each JSON kind */
    wx_init(&s);
    /* random bytes */
    for(int round=0;round<12;round++)for(int k=0;k<6;k++) {
        for(size_t i=0;i<sizeof(buf);i++)buf[i]=round&1?(uint8_t)rnd():(uint8_t)"{}[]\":,\\ aeu0159.-ntf"[rnd()%22];
        parse(&f,kinds[k],ref_epoch,buf,sizeof(buf),1+rnd()%1024);
        probe_commit(&s,&f,200,ref_epoch);
    }
    /* real replies with bytes changed, cut, and doubled */
    for(unsigned i=0;i<wx_request_count();i++) {
        size_t size;
        uint8_t *data=load_request(i,&size);
        if(!data)continue;
        for(int round=0;round<(size>60000?4:40);round++) {
            uint8_t *copy=malloc(size*2);
            memcpy(copy,data,size);memcpy(copy+size,data,size);
            size_t n=round&1?size*2:size-rnd()%(size/2);
            for(int hit=0;hit<1+(int)(rnd()%40);hit++)copy[rnd()%n]=rnd()&3?(uint8_t)"{}[]\":,\\"[rnd()%8]:(uint8_t)rnd();
            parse(&f,i,ref_epoch,copy,n,1+rnd()%1024);
            probe_commit(&s,&f,200,ref_epoch);
            free(copy);
        }
        free(data);
    }
    render_all(&s,NULL,ref_epoch); /* whatever was committed must still draw inside the frame */
    /* a 2 MB synthetic reply of each shape: thousands of records, never held in memory */
    for(int k=0;k<6;k++) {
        static const char *const head[2]={"{\"type\":\"x\",\"features\":[","{\"properties\":{\"periods\":["};
        static const char *const rec[2]={
            "{\"properties\":{\"timestamp\":\"2026-11-19T23:00:00+00:00\",\"textDescription\":\"Light Rain and Fog/Mist with a very long wording that goes on\","
            "\"temperature\":{\"value\":12.5},\"windSpeed\":{\"value\":22.3},\"windDirection\":{\"value\":275},\"barometricPressure\":{\"value\":101325},"
            "\"cloudLayers\":[{\"base\":{\"value\":300},\"amount\":\"OVC\"},{\"base\":{\"value\":90},\"amount\":\"BKN\"}],\"event\":\"Flood Warning\","
            "\"headline\":\"Flood Warning issued November 19 at 6:00PM EST until November 20 at 6:00AM EST by NWS Miami FL\",\"status\":\"Actual\",\"messageType\":\"Alert\"}},",
            "{\"number\":1,\"name\":\"Washington's Birthday Night\",\"startTime\":\"2026-11-20T06:00:00-05:00\",\"endTime\":\"2026-11-20T18:00:00-05:00\","
            "\"isDaytime\":true,\"temperature\":101,\"probabilityOfPrecipitation\":{\"value\":100},\"shortForecast\":\"Slight Chance Showers And Thunderstorms then Sunny\","
            "\"detailedForecast\":\"A chance of showers and thunderstorms. Some of the storms could produce heavy rain. Mostly cloudy, with a high near 101. "
            "Southwest wind 10 to 15 mph, with gusts as high as 30 mph. Chance of precipitation is 100%. New rainfall amounts between a quarter and half of an inch possible. "
            "Supercalifragilisticexpialidociouslylongwordwithoutanyspaceatall and more words after it to pass the limit of what is kept.\"},"};
        /* a forecast period that is already over: skipped, so the whole 2 MB really goes through */
        static const char over[]="{\"number\":1,\"name\":\"This Afternoon\",\"startTime\":\"2026-11-19T12:00:00-05:00\",\"endTime\":\"2026-11-19T18:00:00-05:00\","
            "\"isDaytime\":true,\"temperature\":64,\"shortForecast\":\"Sunny\",\"detailedForecast\":\"Sunny, with a high near 64. Over already.\"},";
        int shape=(wx_request_at(kinds[k])->kind==WX_K_FORECAST||wx_request_at(kinds[k])->kind==WX_K_CITY);
        size_t fed=0;
        wx_fetch_begin(&f,kinds[k]);
        wx_fetch_clock(&f,ref_epoch);
        probe_feed(&f,(const uint8_t *)head[shape],strlen(head[shape]));
        while(fed<2000000u) {
            const char *one=shape?over:rec[0];
            size_t n=strlen(one);
            for(size_t at=0;at<n;at+=97)probe_feed(&f,(const uint8_t *)one+at,n-at<97?n-at:97);
            fed+=n;
        }
        if(wx_request_at(kinds[k])->kind!=WX_K_OBS)CHECK(f.bytes>=2000000u&&!f.done,"the 2 MB reply was not read through (%u bytes)",(unsigned)f.bytes);
        for(int i=0;shape&&i<16;i++)probe_feed(&f,(const uint8_t *)rec[1],strlen(rec[1]));
        probe_feed(&f,(const uint8_t *)(shape?"]}}":"]}"),shape?3:2);
        CHECK(f.done||wx_json_complete(&f.js),"the 2 MB reply did not end cleanly");
        probe_commit(&s,&f,200,ref_epoch);
    }
    CHECK(s.fc.n_local==3&&strlen(s.fc.local[0].text)<WX_TEXT&&s.fc.local[0].text[strlen(s.fc.local[0].text)-1]=='.',"long forecast text not cut at a sentence: '%s'",s.fc.local[0].text);
    CHECK(s.alerts>0&&strlen(s.alert)<WX_ALERT,"alerts from the long reply: %d",s.alerts);
    render_all(&s,"weather-stress-",ref_epoch);
    /* deep nesting: 100,000 brackets, then junk inside, then wanted keys far too deep to count */
    wx_fetch_begin(&f,0);
    for(int i=0;i<100000;i++)probe_feed(&f,(const uint8_t *)(i&1?"[":"{\"properties\":"),i&1?1:14);
    probe_feed(&f,(const uint8_t *)"{\"temperature\":{\"value\":99}}",28);
    for(int i=0;i<100000;i++)probe_feed(&f,(const uint8_t *)(i&1?"}":"]"),1);
    CHECK(f.js.depth<=65535u&&f.u.obs.out.temp_f!=210,"deep junk was read as data");
    wx_init(&s);
    probe_commit(&s,&f,200,ref_epoch);
    CHECK(s.obs[0].temp_f==WX_NA,"deep junk was committed");
    CHECK(box_ok(),"something was written outside wx_fetch or wx_state");
    PASS("garbage, damaged replies, 2 MB replies, 100,000-deep nesting: bounded, nothing out of range");
}

#undef s
#undef f

static void test_cycle(void) {
    static wx_state s;
    static wx_fetch f;
    int before=failures;
    wx_init(&s);
    refresh(&s,&f,1024,NULL);
    wx_enter(&s,1000);
    unsigned page=0,changes=0,redraws=0,worst_second=0,in_second=0,lines_asked=0;
    uint32_t page_at=1000,second=0,ticker_changes=0,last_phase=wx_ticker_phase(&s,0);
    unsigned n=0;
    for(uint32_t ms=1000;ms<1000u+400000u;ms+=13) {
        int64_t epoch=ref_epoch+(ms-1000)/1000;
        uint8_t pending=s.snd.pending;
        int r=probe_tick(&s,ms,epoch);
        if(!n)n=s.n_pages;
        if(ms/1000!=second){second=ms/1000;if(in_second>worst_second)worst_second=in_second;in_second=0;}
        if(r){redraws++;in_second++;guard_set();probe_draw(&s,frame,ms,epoch);if(!guard_ok()){CHECK(0,"frame overrun");break;}}
        uint32_t phase=wx_ticker_phase(&s,ms-1000);
        if(phase!=last_phase){ticker_changes++;last_phase=phase;CHECK(r,"ticker moved without a redraw at %u ms",ms);}
        if(s.page!=page) {
            uint32_t held=ms-page_at,want=s.pages[page][0]==WX_P_IDENT?WX_IDENT_MS:WX_PAGE_MS;
            CHECK(held>=want-13&&held<=want+13,"page %u held %u ms (want %u)",page,held,want);
            CHECK(s.page==(page+1)%s.n_pages,"page went from %u to %u",page,s.page);
            CHECK(r,"page changed without a redraw");
            unsigned line=wx_page_line(&s,s.page);
            if(line){lines_asked++;CHECK(s.snd.pending==line&&pending!=line,"page %u did not ask for its line",s.page);}
            else CHECK(s.snd.pending==pending,"page %u asked for a line it has none of",s.page);
            page=s.page;page_at+=want;changes++;
        }
        if(!r&&s.page==page){guard_set();} /* nothing to do */
    }
    CHECK(changes>=n*3,"%u page changes in 400 s with %u pages",changes,n);
    CHECK(lines_asked>=12,"%u announcer lines in three cycles",lines_asked);
    CHECK(worst_second<=11,"%u redraws in one second",worst_second);
    printf("INFO cycle: %u pages, %u s a cycle; %u redraws in 400 s (%.1f a second), at most %u in one second; ticker moved %u times\n",
           n,(unsigned)((n-1)*WX_PAGE_MS+WX_IDENT_MS)/1000u,redraws,redraws/400.0,worst_second,(unsigned)ticker_changes);
    /* the ticker: five lines, four seconds each, three roll steps between */
    CHECK(wx_ticker_phase(&s,0)==3&&wx_ticker_phase(&s,3999)==3&&wx_ticker_phase(&s,4000)==4&&wx_ticker_phase(&s,4099)==4&&wx_ticker_phase(&s,4100)==5&&
          wx_ticker_phase(&s,4299)==6&&wx_ticker_phase(&s,4300)==7&&wx_ticker_phase(&s,7999)==7,"ticker phases");
    /* a late main loop (one pass 30 s long) moves on one page and does not shorten the next */
    wx_enter(&s,0);probe_tick(&s,0,ref_epoch);probe_tick(&s,30000,ref_epoch);
    CHECK(s.page==1,"after a 30 s stall the page is %u",s.page);
    probe_tick(&s,30000+WX_PAGE_MS-20,ref_epoch);CHECK(s.page==1,"the page after a stall was cut short");
    probe_tick(&s,30000+WX_PAGE_MS,ref_epoch);CHECK(s.page==2,"the page after a stall did not end");
    /* the millisecond counter wrapping round */
    wx_enter(&s,0xfffff000u);
    for(uint32_t i=0;i<20000u;i+=10)probe_tick(&s,0xfffff000u+i,ref_epoch);
    CHECK(s.page==2,"across the millisecond wrap the page is %u",s.page);
    /* an alert: the bar turns into a crawl that moves ten times a second, and the tone is asked for */
    {
        static const char alert[]="{\"features\":[{\"geometry\":{\"coordinates\":[[[1,2],[3,4]]]},\"properties\":{\"event\":\"Winter Storm Warning\","
            "\"status\":\"Actual\",\"messageType\":\"Alert\",\"description\":\"long text \\u00b0 \\\"quoted\\\"\",\"headline\":\"Winter Storm Warning issued "
            "November 19 at 3:14PM EST until November 21 at 7:00AM EST by NWS Miami FL\"}},{\"properties\":{\"event\":\"Old\",\"status\":\"Actual\","
            "\"messageType\":\"Cancel\",\"headline\":\"cancelled\"}},{\"properties\":{\"event\":\"Test Message\",\"status\":\"Test\",\"messageType\":\"Alert\"}},"
            "{\"properties\":{\"status\":\"Actual\",\"messageType\":\"Update\",\"event\":\"Flood Advisory\",\"headline\":null}}]}";
        CHECK(wx_request_at(2)->kind==WX_K_ALERTS&&wx_request_at(wx_request_count()-1)->kind==WX_K_RADAR,"request order");
        for(unsigned i=0;i+1<wx_request_count();i++)CHECK(!strcmp(wx_request_at(i)->host,WX_HOST),"request %u is not for the weather API",i);
        parse(&f,2,ref_epoch,(const uint8_t *)alert,sizeof(alert)-1,5);
        probe_commit(&s,&f,200,ref_epoch);
        CHECK(s.alerts==2,"%d alerts",s.alerts);
        CHECK(!strcmp(s.alert,"WINTER STORM WARNING ISSUED NOVEMBER 19 AT 3:14PM EST UNTIL NOVEMBER 21 AT 7:00AM EST BY NWS MIAMI FL   ***   FLOOD ADVISORY   ***   "),"alert text '%s'",s.alert);
        wx_enter(&s,0);
        CHECK(s.snd.pending==WX_CLIP_ALERT,"the alert tone was not asked for");
        int moves=0;
        for(uint32_t ms=0;ms<1000;ms+=10)moves+=probe_tick(&s,ms,ref_epoch);
        CHECK(moves>=10&&moves<=11,"alert crawl redrew %d times in a second",moves);
        guard_set();probe_draw(&s,frame,2500,ref_epoch);save_frame("weather-alert-",0,"crawl");
        probe_tick(&s,WX_IDENT_MS,ref_epoch);
        guard_set();probe_draw(&s,frame,WX_IDENT_MS+700,ref_epoch);save_frame("weather-alert-",1,"crawl-current");
        CHECK(guard_ok()&&frame_legal(),"alert frame");
    }
    /* UPDATING: a redraw when it starts and ends; the date line gives way every other second; nothing else moves */
    {
        static uint16_t plain[WX_W*WX_H_PX];
        wx_init(&s);refresh(&s,&f,1024,NULL);wx_enter(&s,0);
        for(uint32_t ms=0;ms<=WX_IDENT_MS+500u;ms+=20)probe_tick(&s,ms,ref_epoch);
        probe_draw(&s,frame,WX_IDENT_MS+500u,ref_epoch+1);
        memcpy(plain,frame,sizeof(plain));
        CHECK(!probe_tick(&s,WX_IDENT_MS+520u,ref_epoch),"a redraw with nothing new");
        wx_set_updating(&s,1);
        CHECK(probe_tick(&s,WX_IDENT_MS+540u,ref_epoch),"no redraw when UPDATING starts");
        wx_set_updating(&s,1);
        CHECK(!probe_tick(&s,WX_IDENT_MS+560u,ref_epoch),"a redraw when UPDATING is set again unchanged");
        guard_set();probe_draw(&s,frame,WX_IDENT_MS+500u,ref_epoch+1);
        int changed=0,outside=0;
        for(int y=0;y<WX_H_PX;y++)for(int x=0;x<WX_W;x++)if(frame[y*WX_W+x]!=plain[y*WX_W+x]){changed++;if(y<10||y>19||x<90)outside++;}
        CHECK(changed>50&&!outside&&guard_ok()&&frame_legal(),"UPDATING changed %d pixels, %d of them outside the date line",changed,outside);
        save_frame("weather-updating-",0,"current-conditions");
        probe_draw(&s,frame,WX_IDENT_MS+500u,ref_epoch+2);
        memcpy(plain,frame,sizeof(plain));
        wx_set_updating(&s,0);
        CHECK(probe_tick(&s,WX_IDENT_MS+580u,ref_epoch),"no redraw when UPDATING ends");
        probe_draw(&s,frame,WX_IDENT_MS+500u,ref_epoch+2);
        CHECK(!memcmp(frame,plain,sizeof(plain)),"on an even second the date is not shown during UPDATING");
        for(unsigned k=WX_P_CUR1;k<WX_P_KINDS;k++)CHECK(wx_text_w("UPDATE",WX_SILK)<=158-(39+wx_text_w(wx_titles[k][1],WX_SILK)+4),"UPDATE does not fit beside %s",wx_titles[k][1]);
        /* with no data and no clock, on the opening card and on a page */
        wx_init(&s);wx_set_updating(&s,1);wx_enter(&s,0);probe_tick(&s,0,0);
        CHECK(s.updating,"tuning in cleared UPDATING");
        guard_set();probe_draw(&s,frame,700,0);save_frame("weather-updating-",1,"opening-card-no-data");
        for(uint32_t ms=0;ms<=WX_IDENT_MS+100u;ms+=20)probe_tick(&s,ms,0);
        guard_set();probe_draw(&s,frame,WX_IDENT_MS+700u,0);save_frame("weather-updating-",2,"no-data-no-clock");
        CHECK(guard_ok()&&frame_legal(),"UPDATING frames");
    }
    /* the ticker caught in mid-roll */
    wx_init(&s);refresh(&s,&f,1024,NULL);wx_enter(&s,0);
    for(uint32_t ms=0;ms<=WX_IDENT_MS+2100u;ms+=20)probe_tick(&s,ms,ref_epoch);
    guard_set();probe_draw(&s,frame,WX_IDENT_MS+2100u,ref_epoch+8);save_frame("weather-roll-",0,"ticker-rolling");
    PASS("page cycle timing, announcer lines at their sections, ticker, alert crawl");
}

/* The radar decoder against the fixtures in chromatic/weather/fixtures/ (manifest.txt). */
static struct {uint8_t a[64];uint8_t table[WX_GIF_TABLE];uint8_t b[64];uint8_t page[WX_RADAR_BYTES];uint8_t c[64];wx_gif g;uint8_t d[64];} rbox;
static int rbox_ok(void){for(int i=0;i<64;i++)if(rbox.a[i]!=0x5a||rbox.b[i]!=0x5a||rbox.c[i]!=0x5a||rbox.d[i]!=0x5a)return 0;return 1;}
static void radar_run(const uint8_t *data,size_t size,size_t chunk) {
    memset(rbox.page,0xee,sizeof(rbox.page));
    wx_gif_begin(&rbox.g,rbox.table,rbox.page,WX_RADAR_W,WX_RADAR_H);
    for(size_t at=0;at<size;at+=chunk)if(!wx_gif_feed(&rbox.g,data+at,size-at<chunk?size-at:chunk))break;
}
static int page_untouched(void){for(size_t i=0;i<sizeof(rbox.page);i++)if(rbox.page[i]!=0xee)return 0;return 1;}
static void test_radar(void) {
    static const size_t chunks[4]={1,7,512,1u<<24};
    int before=failures,good=0,refused=0;
    char path[512],line[400];
    memset(rbox.a,0x5a,64);memset(rbox.b,0x5a,64);memset(rbox.c,0x5a,64);memset(rbox.d,0x5a,64);
    snprintf(path,sizeof(path),"%s/manifest.txt",fixture_dir);
    FILE *list=fopen(path,"r");
    CHECK(list!=NULL,"no radar fixtures in %s (chromatic/weather/fixtures)",fixture_dir);
    if(!list)return;
    while(fgets(line,sizeof(line),list)) {
        char name[100],how[20],levels[100];
        size_t size,lsize=0;
        if(sscanf(line,"%99s %19s %99s",name,how,levels)!=3)continue;
        snprintf(path,sizeof(path),"%s/%s",fixture_dir,name);
        uint8_t *data=load(path,&size),*want=NULL;
        CHECK(data!=NULL,"fixture %s missing",name);
        if(!data)continue;
        if(!strcmp(how,"ok")){snprintf(path,sizeof(path),"%s/%s",fixture_dir,levels);want=load(path,&lsize);CHECK(want&&lsize==WX_RADAR_W*WX_RADAR_H,"levels of %s",name);}
        for(int c=0;c<4;c++) {
            radar_run(data,size,chunks[c]);
            if(want&&lsize==WX_RADAR_W*WX_RADAR_H) {
                int wrong=0,removed;
                CHECK(wx_gif_ok(&rbox.g),"%s in pieces of %zu did not decode",name,chunks[c]);
                removed=wx_radar_despeckle(rbox.page,WX_RADAR_W,WX_RADAR_H);
                for(unsigned i=0;i<WX_RADAR_W*WX_RADAR_H;i++)wrong+=wx_radar_get(rbox.page,i)!=want[i];
                CHECK(!wrong,"%s in pieces of %zu: %d pixels differ from the expected levels",name,chunks[c],wrong);
                if(!c&&!strcmp(name,"radar-dry.gif")) {
                    int left=0;
                    for(unsigned i=0;i<WX_RADAR_W*WX_RADAR_H;i++)left+=wx_radar_get(rbox.page,i)!=0;
                    CHECK(left==0&&removed>=6,"clear-air speckle: %d pixels left, %d removed",left,removed);
                    printf("INFO radar: the dry frame's made-up clear-air speckle: blue and cyan dropped by colour, %d lone light pixels removed, %d left\n",removed,left);
                }
            } else if(!strcmp(how,"untouched"))CHECK(!wx_gif_ok(&rbox.g)&&!wx_gif_touched(&rbox.g)&&page_untouched(),"%s was not refused cleanly",name);
            else CHECK(!wx_gif_ok(&rbox.g)&&wx_gif_touched(&rbox.g),"%s was taken as a whole picture",name);
        }
        if(want) { /* cut at every sixteenth: never a whole picture; and with bytes changed: never outside its memory */
            good++;
            for(int cut=0;cut<16;cut++){radar_run(data,size*(size_t)cut/16,97);CHECK(!wx_gif_ok(&rbox.g),"%s cut at %d/16 was taken as whole",name,cut);}
            for(int round=0;round<300;round++) {
                uint8_t *copy=malloc(size);
                memcpy(copy,data,size);
                for(int hit=0;hit<1+(int)(rnd()%6);hit++)copy[rnd()%size]=(uint8_t)rnd();
                radar_run(copy,size,1+rnd()%700);
                if(wx_gif_ok(&rbox.g))wx_radar_despeckle(rbox.page,WX_RADAR_W,WX_RADAR_H);
                free(copy);
            }
        } else refused++;
        free(data);free(want);
    }
    fclose(list);
    for(int round=0;round<200;round++) { /* random bytes after a good header */
        static uint8_t junk[6000];
        for(size_t i=0;i<sizeof(junk);i++)junk[i]=(uint8_t)rnd();
        if(round&1){memcpy(junk,"GIF89a\xa0\x00\x61\x00\x00\x00\x00\x2c\x00\x00\x00\x00\xa0\x00\x61\x00\x00",23);junk[23]=(uint8_t)(2+round%7);}
        radar_run(junk,sizeof(junk),1+rnd()%900);
    }
    CHECK(rbox_ok(),"the radar decoder wrote outside its table, page or state");
    CHECK(good>=10&&refused>=10,"%d good and %d refused fixtures",good,refused);
    /* colours */
    CHECK(wx_radar_level(2,253,2)==1&&wx_radar_level(0,142,0)==2&&wx_radar_level(253,248,2)==3&&wx_radar_level(253,149,0)==4&&
          wx_radar_level(253,0,0)==5&&wx_radar_level(248,0,253)==5&&wx_radar_level(4,233,231)==0&&wx_radar_level(3,0,244)==0&&
          wx_radar_level(120,120,120)==0&&wx_radar_level(0,0,0)==0&&wx_radar_level(255,255,255)==0,"radar colours to levels");
    printf("INFO radar memory: page of levels %u bytes (kept while shown), LZW table %u and decoder state %zu (only while a picture arrives)\n",
           (unsigned)WX_RADAR_BYTES,(unsigned)WX_GIF_TABLE,sizeof(wx_gif));
    printf("INFO radar: %d good fixtures in pieces of 1, 7, 512 and whole, each cut at every sixteenth and damaged 300 times; %d refused\n",good,refused);
    PASS("radar picture: streaming GIF decode, levels, speckle, refusals, bounds");
}

static void test_art(void) {
    int before=failures;
    wx_gc g={frame,0,0,WX_W,WX_H_PX};
    uint32_t fnv=2166136261u;
    wx_backdrop(&g);
    for(int i=0;i<WX_W*WX_H_PX;i++){unsigned k=0;while(k<16&&wx_palette[k]!=frame[i])k++;fnv=(fnv^k)*16777619u;}
    CHECK(fnv==WX_BACKDROP_FNV,"backdrop differs from the reference (%08x, want %08x)",(unsigned)fnv,(unsigned)WX_BACKDROP_FNV);
    for(int i=0;i<16;i++)CHECK(wx_palette[i]!=0xF81F,"palette entry %d is the overlay key",i);
    for(int i=0;i<16;i++)for(int k=0;k<i;k++)CHECK(wx_palette[i]!=wx_palette[k],"palette entries %d and %d are the same RGB565",i,k);
    /* wording */
    char two[2][9],brief[9];
    wx_sky_words("Slight Chance Rain Showers then Mostly Sunny",two);CHECK(!strcmp(two[0],"SLGT CHC")&&!strcmp(two[1],"SHOWERS"),"sky words %s/%s",two[0],two[1]);
    wx_sky_words("Mostly Sunny",two);CHECK(!strcmp(two[0],"MOSTLY")&&!strcmp(two[1],"SUNNY"),"sky words %s/%s",two[0],two[1]);
    wx_sky_words("Patchy Frost then Sunny",two);CHECK(!strcmp(two[0],"PATCHY F"),"sky words %s",two[0]);
    wx_short_sky("LIGHT RAIN",brief);CHECK(!strcmp(brief,"LT RAIN"),"short sky %s",brief);
    wx_short_sky("",brief);CHECK(!strcmp(brief,"--"),"short sky %s",brief);
    CHECK(wx_classify_icon("THUNDERSTORMS AND RAIN",1)==WX_ICON_STORM&&wx_classify_icon("PATCHY FROST",1)==WX_ICON_CLOUD&&wx_classify_icon("CLEAR",0)==WX_ICON_MOON&&
          wx_classify_icon("",1)==WX_ICON_NONE&&wx_classify_icon("LIGHT SNOW",0)==WX_ICON_SNOW,"icons");
    /* units */
    int64_t v;
    CHECK(wx_milli("101325.08",&v)&&v==101325080&&wx_milli("-3.5",&v)&&v==-3500&&!wx_milli("null",&v)&&wx_milli("41.237216839054",&v)&&v==41237,"numbers");
    CHECK(wx_c_to_f(13000)==55&&wx_c_to_f(13300)==56&&wx_c_to_f(-17500)==1&&wx_c_to_f(-40000)==-40&&wx_kmh_to_mph(7416)==5&&wx_kmh_to_mph(5544)==3,"conversions");
    /* time */
    int32_t day;
    CHECK(wx_parse_time("2026-11-20T06:00:00-05:00",&day)==1795172400LL&&wx_dow(day)==5,"time parse %lld",(long long)wx_parse_time("2026-11-20T06:00:00-05:00",&day));
    CHECK(wx_parse_http_date("Thu, 19 Nov 2026 23:46:36 GMT")==wx_parse_time("2026-11-19T23:46:36Z",NULL)&&!wx_parse_http_date("yesterday")&&
          !wx_parse_http_date("Thu, 19 Xyz 2026 23:46:36 GMT")&&!wx_parse_http_date("Thu, 19 Nov 2026 23:46:36 EST"),"HTTP date");
    CHECK(wx_parse_time("garbage",NULL)==0&&wx_parse_time("2026-13-40T99:00:00Z",NULL)==0,"bad times");
    CHECK(wx_eastern_offset(1772953199LL)==-5&&wx_eastern_offset(1772953200LL)==-4&&wx_eastern_offset(1793512799LL)==-4&&wx_eastern_offset(1793512800LL)==-5,"daylight time rule 2026");
    PASS("artwork (backdrop equals the reference), palette, wording, units, time");
}

int main(int argc,char **argv) {
    if(argc==3&&!strcmp(argv[1],"almanac")){print_almanac(strtoll(argv[2],NULL,10));return 0;}
    if(argc!=5||strcmp(argv[1],"selftest")){fprintf(stderr,"usage: check_weather selftest <cache folder> <frame folder> <radar fixture folder> | almanac <UTC seconds>\n");return 2;}
    cache_dir=argv[2];out_dir=argv[3];fixture_dir=argv[4];
    ref_epoch=wx_parse_time("2026-11-19T23:46:36+00:00",NULL); /* when the reference made normalised.json */
    printf("SIZE wx_state %zu wx_fetch %zu wx_sound %zu wx_almanac %zu wx_json %zu wx_gif %zu radar_page %u gif_table %u art %d sound %u\n",sizeof(wx_state),
           sizeof(wx_fetch),sizeof(wx_sound),sizeof(wx_almanac),sizeof(wx_json),sizeof(wx_gif),(unsigned)WX_RADAR_BYTES,(unsigned)WX_GIF_TABLE,WX_ART_BYTES,(unsigned)WX_SND_BYTES);
    test_art();
    test_radar();
    test_parse();
    test_values();
    test_failures();
    test_garbage();
    test_cycle();
    test_sound();
    printf("%s %d checks, %d failed\n",failures?"FAILED":"ALL PASS",checks,failures);
    return failures?1:0;
}
