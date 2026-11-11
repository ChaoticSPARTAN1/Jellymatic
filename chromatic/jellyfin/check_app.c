/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md */
/* PC harness for the Chromatic Jellyfin app (jf_app.h). No hardware, no network of its own.
 *
 * A driver (scripts/test_chromatic_player.py) answers over stdin/stdout:
 *   harness -> "REQ <METHOD> <path>\n" [+ "BODY <n>\n<n bytes>"]   driver -> "REP <status> <n>\n<n bytes>"
 *   harness -> "STREAM <path>\n"                                    driver -> "FILE <n>\n<local path>"
 * The stream file is then read at a simulated network rate (RATE bytes per simulated second,
 * GAP_EVERY/GAP_MS for Wi-Fi dips) through a NET_BUF-byte buffer in NET_CHUNK reads, like the
 * ESP32's stream task. JPEGs decode with libjpeg, fitted and centred like the ESP32 binding.
 * Time is simulated: each loop advances LOOP_MS (the console's main loop runs every 10 ms),
 * every decoded picture costs DECODE_MS and every frame sent to the overlay FRAME_SEND_MS, so
 * the console's real budget can be modelled (decoding at PC speed hid it).
 * AUDIO=1 adds a model of the ESP32 sound binding (jellyfin_esp32.c): an AUDIO_RING-byte ring of
 * ADPCM pieces, a consumer that decodes them (jf_audio.h) into an I2S DMA running at the sample
 * rate in simulated time (AUDIO_DRIFT_PPM off the video's clock), silence and an underrun count
 * when the ring is empty, AUDIO_STALL_AT_MS after which the consumer stops (a dead sink), and
 * AUDIO_OUT, the PCM it played, for a byte-for-byte check against FFmpeg's decode;
 * AUDIO_REFUSE_UNTIL_MS makes the sink refuse to start until then (its memory not back yet).
 * AUDIO_WAIT=0 models the sound task of launchers up to 1.2.6: an empty ring put a buffer of silence
 * into the DMA at once, even while the DMA still held sound (24 ms of lead thrown away per gap);
 * the default (1) is 1.2.7's task, which waits for the ring while the DMA plays what it has.
 * STALL_EVERY / STALL_MS hold the main loop (not the network or the DMA) after a picture.
 * Script (argv[1]): comma list of  wait:<ms>  press:<A|B|UP|DOWN|LEFT|RIGHT|START|SELECT>
 *   shot:<name> (writes <outdir>/<name>.ppm)  expect:<screen number>  state (prints a line)
 */
#include "jf_app.h"
#include "../../pocket/ui_font.h"
#include <jpeglib.h>
#include <setjmp.h>
#include <stdlib.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

static jf_app app;
static uint16_t fb[JF_W*JF_H];
static FILE *stream_file;static uint32_t stream_rate=400000,gap_every=0,gap_ms=0,step_ms=5;
static uint64_t stream_budget_us;static uint32_t now_ms_sim=1000;
static const char *outdir=".";
static unsigned long requests,streams,decodes;
/* LIVE_DRAW=1: draw whenever a tick asks, like the ESP32 loop, and check every label-only
 * redraw (a scrolling name) against a full redraw: same pixels, no artwork decoded. */
static int live_draw;static unsigned long draws_full,draws_label,label_decodes,label_mismatch;
static uint16_t fb_check[JF_W*JF_H];
static void live_frame(void) {
    if(app.screen==JF_S_PLAYER)return; /* video decodes into the screen itself */
    unsigned long before=decodes;uint8_t part=app.draw_part;
    jf_app_draw(&app,fb);
    if(part!=JF_DRAW_LABEL){draws_full++;return;}
    draws_label++;if(decodes!=before)label_decodes++;
    app.draw_part=JF_DRAW_FULL;jf_app_draw(&app,fb_check);app.draw_part=part;decodes=before;
    if(memcmp(fb,fb_check,sizeof(fb)))label_mismatch++;
}

static int req(void *ctx,const char *method,const char *path,const char *body) {
    (void)ctx;requests++;
    printf("REQ %s %s\n",method?method:"GET",path);
    if(body)printf("BODY %u\n%s",(unsigned)strlen(body),body);
    fflush(stdout);
    return 0;
}
/* Like the ESP32 binding: artwork and the video buffer are never held at once. */
static uint8_t art_arena[JF_ART_MAX];static uint16_t art_cap_sim;static int art_wanted,art_swaps;
/* The ESP32 stream task: NET_CHUNK-byte HTTP reads into a NET_BUF-byte stream buffer, filled
 * only while the network has delivered the bytes (RATE) and there is room (back-pressure). */
static uint8_t netbuf[16384+1024];static size_t net_have,net_cap=8192,net_chunk=1024;static int net_end;
static uint32_t decode_ms,frame_send_ms,waits;
/* STALL_EVERY / STALL_MS: every STALL_EVERY ms of simulated time the main loop is held for STALL_MS right
 * after a picture went out (a Wi-Fi or lwIP task hogging the CPU, a long diagnostics write): the network
 * and the sound DMA run on meanwhile, the player does not. */
static uint32_t stall_every,stall_ms,stall_next;static unsigned long stalls;
static uint32_t delay_ms;
static int held,held_status;static unsigned held_n;static uint32_t held_due;static uint8_t held_buf[JF_BUF+1];
/* The driver's answer to the outstanding request, read now (it is held until due, like Wi-Fi). */
static void take_reply(void) {
    char line[64];
    if(!fgets(line,sizeof(line),stdin)||sscanf(line,"REP %d %u",&held_status,&held_n)!=2){fprintf(stderr,"driver gone\n");exit(3);}
    if(held_n>sizeof(held_buf)||fread(held_buf,1,held_n,stdin)!=held_n){fprintf(stderr,"bad reply\n");exit(3);}
    held=1;held_due=now_ms_sim+delay_ms;
}
static int stream_open(void *ctx,const char *path) {
    (void)ctx;streams++;
    /* A stream opened from inside a tick (1.3.2: a reopen without subtitles) while a request is out: the driver has
     * already answered it, and that answer comes first on the pipe (the console's stream has its own connection). */
    if(app.request!=JF_R_NONE&&!held)take_reply();
    if(app.art_bytes){jf_art_reset(&app,NULL,0);art_wanted=1;art_swaps++;}
    if(stream_file){fclose(stream_file);stream_file=NULL;}
    printf("STREAM %s\n",path);fflush(stdout);
    char line[64];unsigned n;
    if(!fgets(line,sizeof(line),stdin)||sscanf(line,"FILE %u",&n)!=1||n>=1024)return -1;
    char name[1024];if(fread(name,1,n,stdin)!=n)return -1;name[n]=0;
    stream_file=fopen(name,"rb");stream_budget_us=0;net_have=0;net_end=0;
    return stream_file?0:-1;
}
static void stream_close(void *ctx){(void)ctx;if(stream_file){fclose(stream_file);stream_file=NULL;}}
static uint32_t fail_at;static int failed_once;
static void net_pump(void) {
    if(!stream_file||net_end)return;
    if(fail_at&&now_ms_sim>=fail_at&&!failed_once){failed_once=1;net_end=-2;return;} /* the connection drops once */
    if(gap_every&&(now_ms_sim%gap_every)<gap_ms)return; /* a Wi-Fi dip */
    while(net_have<net_cap&&net_have+net_chunk<=sizeof(netbuf)) {
        size_t allowed=(size_t)(stream_budget_us*stream_rate/1000000u);
        if(!allowed)break; /* nothing has arrived yet */
        size_t n=fread(netbuf+net_have,1,allowed<net_chunk?allowed:net_chunk,stream_file);
        if(!n){net_end=feof(stream_file)?-1:-2;break;}
        stream_budget_us-=(uint64_t)n*1000000u/stream_rate;net_have+=n;
    }
}
static int stream_read(void *ctx,uint8_t *buf,size_t max) {
    (void)ctx;
    if(!stream_file)return -2;
    net_pump();
    if(net_have) {
        size_t n=net_have<max?net_have:max;
        memcpy(buf,netbuf,n);memmove(netbuf,netbuf+n,net_have-n);net_have-=n;
        return (int)n;
    }
    if(net_end)return net_end;
    waits++;return 0;
}
struct err{struct jpeg_error_mgr pub;jmp_buf jump;};
static void on_error(j_common_ptr c){longjmp(((struct err *)c->err)->jump,1);}
static int decode(void *ctx,const uint8_t *jpeg,size_t size,uint16_t *dst,int stride,int w,int h) {
    (void)ctx;decodes++;
    now_ms_sim+=decode_ms;stream_budget_us+=decode_ms*1000u; /* the console's decoder is busy; the network is not */
    struct jpeg_decompress_struct c;struct err e;static uint8_t rgb[640*480*3];
    c.err=jpeg_std_error(&e.pub);e.pub.error_exit=on_error;
    if(setjmp(e.jump)){jpeg_destroy_decompress(&c);return -1;}
    jpeg_create_decompress(&c);jpeg_mem_src(&c,jpeg,(unsigned long)size);
    if(jpeg_read_header(&c,TRUE)!=JPEG_HEADER_OK||c.progressive_mode){jpeg_destroy_decompress(&c);return -1;} /* TJpgDec: baseline only */
    c.out_color_space=JCS_RGB;jpeg_start_decompress(&c);
    int sw=(int)c.output_width,sh=(int)c.output_height;
    if(sw>640||sh>480){jpeg_destroy_decompress(&c);return -1;}
    while(c.output_scanline<c.output_height){uint8_t *row=rgb+c.output_scanline*sw*3;jpeg_read_scanlines(&c,&row,1);}
    jpeg_finish_decompress(&c);jpeg_destroy_decompress(&c);
    /* fit, keep aspect, centre, black bars (same rule as the ESP32 binding) */
    int fw=w,fh=sh*w/sw;if(fh>h){fh=h;fw=sw*h/sh;}
    int ox=(w-fw)/2,oy=(h-fh)/2;
    for(int y=0;y<h;y++)for(int x=0;x<w;x++)dst[y*stride+x]=0;
    for(int y=0;y<fh;y++)for(int x=0;x<fw;x++){
        const uint8_t *p=rgb+((y*sh/fh)*sw+(x*sw/fw))*3;
        dst[(oy+y)*stride+ox+x]=(uint16_t)(((p[0]>>3)<<11)|((p[1]>>2)<<5)|(p[2]>>3));
    }
    return 0;
}
/* ---------- sound sink model ---------- */
static int audio_model;static uint32_t audio_ring_cap=1536,audio_rate;static int32_t audio_drift_ppm;static uint32_t audio_stall_at;
static uint8_t aring[4096];static size_t aring_used; /* ADPCM pieces: [len lo][len hi | 0x80 block start][bytes] */
static int16_t apcm[2*512+1];static size_t apcm_have,apcm_at;static jf_ima aima;
/* The I2S DMA: AUDIO_DMA frames (default 384 = 24 ms at 16 kHz, 3 x 128-frame buffers like the ESP32
 * binding) filled greedily from the ring, played out at the sample rate. */
static int16_t dmaq[2048];static size_t dma_head,dma_count,audio_dma=384;
static int audio_paused,audio_started;static uint64_t audio_frames_due_acc;static uint32_t audio_t0_ms;
static unsigned long audio_played_frames,audio_silence_frames,audio_underruns,audio_pieces,audio_bytes;static int audio_in_silence;
static int audio_wait=1;static unsigned long audio_pushed; /* AUDIO_WAIT=0: silence buffers written while the ring was empty */
static uint8_t dmaz[2048]; /* marks the DMA frames that are pushed silence */
static FILE *audio_out;
static void audio_fill(void) {
    while(dma_count<audio_dma) {
        if(apcm_at==apcm_have) {
            apcm_at=apcm_have=0;
            if(!audio_wait&&!audio_paused&&aring_used<2&&audio_played_frames) { /* the 1.2.6 task: silence now, even with sound queued */
                for(int i=0;i<128&&dma_count<audio_dma;i++){dmaq[(dma_head+dma_count)%2048]=0;dmaz[(dma_head+dma_count)%2048]=1;dma_count++;}
                audio_pushed++;continue;
            }
            if(audio_paused||aring_used<2)return;
            size_t len=aring[0]|((aring[1]&0x7f)<<8);int first=aring[1]&0x80;
            if(first)jf_ima_block_start(&aima);
            apcm_have=jf_ima_feed(&aima,aring+2,len,apcm,sizeof(apcm)/sizeof(apcm[0]));
            memmove(aring,aring+2+len,aring_used-2-len);aring_used-=2+len;
        }
        while(apcm_at<apcm_have&&dma_count<audio_dma){dmaq[(dma_head+dma_count)%2048]=apcm[apcm_at++];dmaz[(dma_head+dma_count)%2048]=0;dma_count++;}
    }
}
/* The DMA has played every frame due since the start: real sound from its buffers, else silence. */
static void audio_pump(void) {
    if(!audio_started)return;
    if(audio_stall_at&&now_ms_sim>=audio_stall_at)return; /* a dead sink: nothing moves */
    audio_fill();
    uint64_t total=(uint64_t)(now_ms_sim-audio_t0_ms)*audio_rate*(uint64_t)(1000000+audio_drift_ppm)/1000000u/1000u;
    while(audio_frames_due_acc<total) {
        if(!dma_count)audio_fill();
        if(dma_count) {
            int16_t v=dmaq[dma_head];dma_head=(dma_head+1)%2048;dma_count--;
            if(audio_out)fwrite(&v,2,1,audio_out);
            if(dmaz[(dma_head+2047)%2048]) { /* a pushed silence frame */
                if(!audio_in_silence&&!audio_paused){audio_underruns++;audio_in_silence=1;}
                audio_silence_frames++;audio_frames_due_acc++;continue;
            }
            audio_played_frames++;audio_frames_due_acc++;audio_in_silence=0;
        } else { /* silence */
            if(!audio_in_silence&&audio_played_frames&&!audio_paused){audio_underruns++;audio_in_silence=1;}
            audio_silence_frames++;audio_frames_due_acc++;
        }
    }
}
static uint32_t audio_refuse_until; /* AUDIO_REFUSE_UNTIL_MS: the sink cannot start before this (memory not back yet) */
static int audio_start(void *ctx,uint32_t rate) {
    (void)ctx;
    if(getenv("AUDIO_REFUSE"))return -1;
    if(audio_refuse_until&&now_ms_sim<audio_refuse_until)return -1;
    audio_rate=rate;audio_started=1;audio_t0_ms=now_ms_sim;audio_frames_due_acc=0;aring_used=0;apcm_have=apcm_at=0;
    audio_played_frames=0;audio_paused=0;dma_count=0;jf_ima_init(&aima);
    if(getenv("AUDIO_OUT")&&!audio_out)audio_out=fopen(getenv("AUDIO_OUT"),"wb");
    return 0;
}
static size_t audio_write(void *ctx,const uint8_t *data,size_t size,int first) {
    (void)ctx;audio_pump();
    if(!audio_started)return size;
    size_t space=audio_ring_cap>aring_used?audio_ring_cap-aring_used:0;
    if(space<3)return 0;
    if(size>space-2)size=space-2;
    if(size>512)size=512;
    aring[aring_used]=(uint8_t)size;aring[aring_used+1]=(uint8_t)((size>>8)|(first?0x80:0));
    memcpy(aring+aring_used+2,data,size);aring_used+=2+size;audio_pieces++;audio_bytes+=size;
    return size;
}
static uint32_t audio_played(void *ctx){(void)ctx;audio_pump();return (uint32_t)audio_played_frames;}
static void audio_pause(void *ctx,int paused){(void)ctx;audio_pump();audio_paused=paused;}
static void audio_flush(void *ctx){(void)ctx;audio_pump();aring_used=0;apcm_have=apcm_at=0;dma_count=0;audio_played_frames=0;}
static void audio_stop(void *ctx){(void)ctx;audio_pump();audio_started=0;aring_used=0;apcm_have=apcm_at=0;dma_count=0;}

static void shot(const char *name) {
    app.draw_part=JF_DRAW_FULL;jf_app_draw(&app,fb);
    char path[512];snprintf(path,sizeof(path),"%s/%s.ppm",outdir,name);
    FILE *f=fopen(path,"wb");if(!f)return;
    fprintf(f,"P6\n%d %d\n255\n",JF_W,JF_H);
    for(int i=0;i<JF_W*JF_H;i++){uint16_t c=fb[i];uint8_t px[3]={(uint8_t)((c>>11)<<3|(c>>13)),(uint8_t)(((c>>5)&63)<<2|((c>>9)&3)),(uint8_t)((c&31)<<3|((c>>2)&7))};fwrite(px,1,3,f);}
    fclose(f);
}
static void state(void) {
    uint32_t us=jf_avi_frame_us(&app.avi);
    fprintf(stderr,"STATE screen=%u rows=%u folder=%u art=%u playing=%u paused=%u shown=%lu dropped=%lu pos=%lu frame=%lu requests=%lu streams=%lu bitrate=%lu softened=%u swaps=%d "
        "fps=%lu cap=%lu cycle=%lu rebased=%lu slowed=%u audio=%lu waits=%lu stalls=%lu t=%lu reconnects=%u link_down=%u exit_to=%u msg=%s\n",
        app.screen,app.row_count,app.folder.count,(unsigned)app.art_used,app.playing,app.paused,(unsigned long)app.shown,(unsigned long)app.dropped,
        (unsigned long)jf_position_s(&app),(unsigned long)app.avi.frame_index,requests,streams,(unsigned long)app.bitrate,app.softened,art_swaps,
        (unsigned long)(us?(1000000u+us/2)/us:0),(unsigned long)app.fps_cap,(unsigned long)app.cycle_ms,(unsigned long)app.rebased,app.slowed,
        (unsigned long)app.avi.audio,(unsigned long)waits,stalls,(unsigned long)now_ms_sim,app.reconnects,app.link_down,app.exit_to,app.message);
    if(audio_model) {
        audio_pump();
        fprintf(stderr,"SOUND on=%u clock=%u dead=%u played=%lu silence=%lu underruns=%lu blocks=%lu fallbacks=%lu pieces=%lu bytes=%lu ring=%lu rate=%lu "
            "fmt=%u ch=%u retries=%lu base=%lu starved=%lu ratecap=%lu deficit=%lu pushed=%lu\n",app.audio_on,app.audio_clock,app.audio_dead,audio_played_frames,audio_silence_frames,audio_underruns,
            (unsigned long)app.audio_blocks,(unsigned long)app.audio_fallbacks,audio_pieces,audio_bytes,(unsigned long)aring_used,
            (unsigned long)audio_rate,app.avi.audio_format,app.avi.audio_channels,(unsigned long)app.audio_retries,(unsigned long)app.audio_base_ms,
            (unsigned long)app.starved,(unsigned long)app.rate_cap,(unsigned long)jf_audio_deficit(&app),audio_pushed);
    }
    {   /* 1.3.2: the launcher's track diagnostics line, and the note on the picture */
        char tl[96];jf_tracks_line(&app,tl,sizeof(tl));
        fprintf(stderr,"TRACKS %s\nTRACKSEQ %lu tracks_ok=%u audio_count=%u sub_count=%u subs_failed=%u switching=%u\n",tl,
                (unsigned long)app.tracks_seq,app.tracks_ok,app.audio_count,app.sub_count,app.subs_failed,app.switching);
        fprintf(stderr,"NOTE warn=%u on=%u text=%s\n",app.note_warn,app.note_until!=0,app.note);
    }
    jf_label l;int has=jf_focus_label(&app,&l);
    fprintf(stderr,"UI screen=%u menu_sel=%u menu_page=%u big=%u audio_lang=%u subs=%u aidx=%d sidx=%d scroll=%d over=%d draws_full=%lu draws_label=%lu label_decodes=%lu label_mismatch=%lu label=%s\n",
        app.screen,app.menu_sel,app.menu_page,app.big_cards,app.audio_lang,app.subs,(int)app.audio_index,(int)app.sub_index,app.scroll_px,has?l.over:0,draws_full,draws_label,label_decodes,label_mismatch,has?l.text:"");
}
/* Runs the app for `ms` simulated milliseconds, answering requests from the driver. With
 * DELAY_MS the reply is held that long (like Wi-Fi), so presses can land while it is out. */
static void run(uint32_t ms) {
    for(uint32_t t=0;t<ms;t+=step_ms) {
        now_ms_sim+=step_ms;stream_budget_us+=step_ms*1000u;
        if(stream_budget_us>2000000u)stream_budget_us=2000000u; /* a socket buffer, not infinite */
        if(audio_model)audio_pump(); /* the DMA keeps playing while the loop was busy */
        if(art_wanted&&!app.playing&&app.request==JF_R_NONE){art_wanted=0;jf_art_reset(&app,art_arena,art_cap_sim);}
        if(jf_app_tick(&app,now_ms_sim)) { /* the overlay frame goes out */
            if(live_draw)live_frame();
            now_ms_sim+=frame_send_ms;stream_budget_us+=frame_send_ms*1000u;
            if(stall_every&&app.playing&&(int32_t)(now_ms_sim-stall_next)>=0) {
                stall_next=now_ms_sim+stall_every;now_ms_sim+=stall_ms;stream_budget_us+=stall_ms*1000u;stalls++;
            }
        }
        if(app.request!=JF_R_NONE&&!held)take_reply();
        if(held&&(int32_t)(now_ms_sim-held_due)>=0) {
            held=0;int status=held_status;
            if(!jf_app_reply_data(&app,held_buf,held_n))status=-3;
            jf_app_reply(&app,status);
        }
    }
}
int main(int argc,char **argv) {
    if(argc<3)return 2;
#ifdef _WIN32
    _setmode(_fileno(stdin),_O_BINARY);_setmode(_fileno(stdout),_O_BINARY); /* replies carry JPEG bytes */
#endif
    outdir=argv[2];
    if(getenv("RATE"))stream_rate=(uint32_t)atoi(getenv("RATE"));
    if(getenv("DELAY_MS"))delay_ms=(uint32_t)atoi(getenv("DELAY_MS"));
    if(getenv("FAIL_AT_MS"))fail_at=now_ms_sim+(uint32_t)atoi(getenv("FAIL_AT_MS"));
    if(getenv("GAP_EVERY"))gap_every=(uint32_t)atoi(getenv("GAP_EVERY"));
    if(getenv("GAP_MS"))gap_ms=(uint32_t)atoi(getenv("GAP_MS"));
    live_draw=getenv("LIVE_DRAW")!=NULL;
    if(getenv("LOOP_MS"))step_ms=(uint32_t)atoi(getenv("LOOP_MS"));
    if(getenv("DECODE_MS"))decode_ms=(uint32_t)atoi(getenv("DECODE_MS"));
    if(getenv("FRAME_SEND_MS"))frame_send_ms=(uint32_t)atoi(getenv("FRAME_SEND_MS"));
    if(getenv("STALL_EVERY"))stall_every=(uint32_t)atoi(getenv("STALL_EVERY"));
    if(getenv("STALL_MS"))stall_ms=(uint32_t)atoi(getenv("STALL_MS"));
    stall_next=now_ms_sim+stall_every;
    if(getenv("NET_BUF"))net_cap=(size_t)atoi(getenv("NET_BUF"));
    if(getenv("NET_CHUNK"))net_chunk=(size_t)atoi(getenv("NET_CHUNK"));
    if(net_cap>16384)net_cap=16384;
    if(!net_chunk||net_chunk>1024)net_chunk=1024;
    if(!step_ms)step_ms=1;
    jf_platform io={0,req,stream_open,stream_close,stream_read,decode,NULL,NULL,NULL,NULL,NULL,NULL};
    jf_config cfg;memset(&cfg,0,sizeof(cfg));
    cfg.pocket_link=getenv("POCKET_LINK")!=NULL; /* 1.3.0: the link FPGA (18.41): the menu offers Pocket link */
    audio_model=getenv("AUDIO")!=NULL;
    if(audio_model) {
        io.audio_start=audio_start;io.audio_write=audio_write;io.audio_played=audio_played;
        io.audio_pause=audio_pause;io.audio_flush=audio_flush;io.audio_stop=audio_stop;cfg.audio=1;
        if(getenv("AUDIO_RING"))audio_ring_cap=(uint32_t)atoi(getenv("AUDIO_RING"));
        if(audio_ring_cap<64||audio_ring_cap>sizeof(aring))audio_ring_cap=1536;
        if(getenv("AUDIO_DRIFT_PPM"))audio_drift_ppm=atoi(getenv("AUDIO_DRIFT_PPM"));
        if(getenv("AUDIO_DMA"))audio_dma=(size_t)atoi(getenv("AUDIO_DMA"));
        if(audio_dma<16||audio_dma>2048)audio_dma=384;
        if(getenv("AUDIO_STALL_AT_MS"))audio_stall_at=now_ms_sim+(uint32_t)atoi(getenv("AUDIO_STALL_AT_MS"));
        if(getenv("AUDIO_REFUSE_UNTIL_MS"))audio_refuse_until=now_ms_sim+(uint32_t)atoi(getenv("AUDIO_REFUSE_UNTIL_MS"));
        if(getenv("AUDIO_WAIT"))audio_wait=atoi(getenv("AUDIO_WAIT"));
    }
    jf_copy(cfg.server_name,sizeof(cfg.server_name),"Jellyfin");
    jf_copy(cfg.user,sizeof(cfg.user),getenv("JF_USER")?getenv("JF_USER"):"0123456789abcdef0123456789abcdef");
    jf_copy(cfg.device,sizeof(cfg.device),"chromatic-test");
    cfg.font_alpha=pj_font_alpha;cfg.font_advance=pj_font_advance;cfg.fb=fb;cfg.scratch=fb;cfg.scratch_bytes=sizeof(fb);art_cap_sim=getenv("ART")?(uint16_t)atoi(getenv("ART")):(uint16_t)sizeof(art_arena);cfg.art=art_arena;cfg.art_cap=art_cap_sim;
    fprintf(stderr,"SIZE app=%u\n",(unsigned)sizeof(jf_app));
    jf_app_start(&app,&io,&cfg,now_ms_sim);
    char script[4096];jf_copy(script,sizeof(script),argv[1]);
    for(char *step=strtok(script,",");step;step=strtok(NULL,",")) {
        if(!strncmp(step,"wait:",5))run((uint32_t)atoi(step+5));
        else if(!strncmp(step,"press:",6)) {
            static const char *const names[]={"START","SELECT","B","A","UP","RIGHT","LEFT","DOWN","MENU"};
            for(unsigned b=0;b<9;b++)if(!strcmp(step+6,names[b]))jf_app_press(&app,(uint16_t)(1u<<b));
            run(50);
        } else if(!strncmp(step,"link:",5)){jf_app_set_link(&app,atoi(step+5));run(step_ms);} /* the Wi-Fi link down (0) / up (1) */
        else if(!strncmp(step,"shot:",5))shot(step+5);
        else if(!strcmp(step,"state"))state();
        else if(!strncmp(step,"expect:",7)&&app.screen!=(unsigned)atoi(step+7)){state();fprintf(stderr,"EXPECT FAILED %s\n",step);return 1;}
    }
    state();
    if(audio_out)fclose(audio_out);
    printf("END\n");fflush(stdout);
    return 0;
}
