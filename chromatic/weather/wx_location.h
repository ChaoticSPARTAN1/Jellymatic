/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * Local Weather channel: the built-in place ("pack") and its request list. WRITTEN BY
 * chromatic/weather/tools/make_location.py from the US National Weather Service replies kept in
 * chromatic/weather/fixtures/places/orlando/. Do not edit by hand.
 * With no weather location record the channel shows WX_DEFAULT_PACK (Orlando, FL). Any other place's area pages
 * (Regional Forecast maps, Current Radar) come from a location pack Chromatic Manager writes (wx_lpack.h). */
#ifndef WX_LOCATION_H
#define WX_LOCATION_H
#include <stdint.h>
#include "wx_art_fl.h"

/* named arrays, so every table shares the same two host pointers (weather_esp32.c tells hosts apart by pointer) */
static const char wx_host_api[]="api.weather.gov",wx_host_radar[]="opengeo.ncep.noaa.gov";
#define WX_HOST wx_host_api
#define WX_RADAR_HOST wx_host_radar
/* the largest tables a pack may have (built-in or a location pack); the channel's state is sized by them */
#define WX_STATIONS 11
#define WX_OBS_ROWS 12
#define WX_CITIES 9
#define WX_REQUESTS 33
enum {WX_K_OBS,WX_K_RECENT,WX_K_HISTORY,WX_K_FORECAST,WX_K_CITY,WX_K_ALERTS,WX_K_RADAR};
typedef struct {const char *host,*path;uint8_t kind,slot;} wx_request;
/* ---- Orlando, FL ---- */
static const wx_request wx_fl_requests[33]={
    {WX_HOST,"/stations/KMCO/observations/latest",WX_K_OBS,0},
    {WX_HOST,"/gridpoints/MLB/26,68/forecast",WX_K_FORECAST,5},
    {WX_HOST,"/alerts/active?point=28.5383,-81.3792",WX_K_ALERTS,0},
    {WX_HOST,"/stations/KMCO/observations?limit=40",WX_K_HISTORY,0},
    {WX_HOST,"/gridpoints/MLB/29,80/forecast",WX_K_CITY,6},
    {WX_HOST,"/gridpoints/MLB/26,57/forecast",WX_K_CITY,7},
    {WX_HOST,"/gridpoints/MLB/48,74/forecast",WX_K_CITY,8},
    {WX_HOST,"/gridpoints/MOB/84,57/forecast",WX_K_CITY,0},
    {WX_HOST,"/gridpoints/TAE/84,86/forecast",WX_K_CITY,1},
    {WX_HOST,"/gridpoints/JAX/66,65/forecast",WX_K_CITY,2},
    {WX_HOST,"/gridpoints/TBW/71,98/forecast",WX_K_CITY,3},
    {WX_HOST,"/gridpoints/MFL/110,50/forecast",WX_K_CITY,4},
    {WX_HOST,"/stations/KORL/observations/latest",WX_K_OBS,1},
    {WX_HOST,"/stations/KSFB/observations/latest",WX_K_OBS,2},
    {WX_HOST,"/stations/KISM/observations/latest",WX_K_OBS,3},
    {WX_HOST,"/stations/KLEE/observations/latest",WX_K_OBS,4},
    {WX_HOST,"/stations/KTIX/observations/latest",WX_K_OBS,5},
    {WX_HOST,"/stations/KDAB/observations/latest",WX_K_OBS,6},
    {WX_HOST,"/stations/KMLB/observations/latest",WX_K_OBS,7},
    {WX_HOST,"/stations/KLAL/observations/latest",WX_K_OBS,8},
    {WX_HOST,"/stations/KTPA/observations/latest",WX_K_OBS,9},
    {WX_HOST,"/stations/KJAX/observations/latest",WX_K_OBS,10},
    {WX_HOST,"/stations/KORL/observations?limit=8",WX_K_RECENT,1},
    {WX_HOST,"/stations/KSFB/observations?limit=8",WX_K_RECENT,2},
    {WX_HOST,"/stations/KISM/observations?limit=8",WX_K_RECENT,3},
    {WX_HOST,"/stations/KLEE/observations?limit=8",WX_K_RECENT,4},
    {WX_HOST,"/stations/KTIX/observations?limit=8",WX_K_RECENT,5},
    {WX_HOST,"/stations/KDAB/observations?limit=8",WX_K_RECENT,6},
    {WX_HOST,"/stations/KMLB/observations?limit=8",WX_K_RECENT,7},
    {WX_HOST,"/stations/KLAL/observations?limit=8",WX_K_RECENT,8},
    {WX_HOST,"/stations/KTPA/observations?limit=8",WX_K_RECENT,9},
    {WX_HOST,"/stations/KJAX/observations?limit=8",WX_K_RECENT,10},
    {WX_RADAR_HOST,"/geoserver/kmlb/ows?service=WMS&version=1.1.1&request=GetMap&layers=kmlb_sr_bref&styles=&format=image/gif&transparent=true&srs=EPSG:4326&bbox=-82.3000,27.9725,-79.9000,29.2500&width=160&height=97",WX_K_RADAR,0},
};
static const uint16_t wx_fl_request_period[33]={600,1800,600,3600,3600,3600,3600,3600,3600,3600,3600,3600,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,1800,600};
static const char *const wx_fl_obs_label[11]={"ORLANDO","ORLANDO EXEC","SANFORD","KISSIMMEE","LEESBURG","TITUSVILLE","DAYTONA BEACH","MELBOURNE","LAKELAND","TAMPA","JACKSONVILLE"};
static const uint8_t wx_fl_obs_slot[11]={0,1,2,3,4,5,6,7,8,9,10};
static const char *const wx_fl_city_name[9]={"PENSACOLA","TALLAHASSEE","JACKSONVILLE","TAMPA","MIAMI","ORLANDO","SANFORD","KISSIMMEE","TITUSVILLE"};
/* A place's pack: its tables, its maps and its radar view (built in, or a location pack: wx_lpack.h). */
typedef struct wx_pack {
    const char *name,*area,*state;           /* "ORLANDO, FL", the opening card's area line, "FL" */
    double lat,lon;                           /* home point (almanac) */
    int32_t lat_e4,lon_e4;const char *office;uint16_t grid_x,grid_y; /* a record naming exactly these is this pack */
    uint8_t stations,obs_rows,cities,home_city,requests;
    const wx_request *req;const uint16_t *period;const char *const *obs_label;const uint8_t *obs_slot;
    const char *const *city_name;
    const uint8_t *state_map;uint16_t state_map_n;uint8_t state_y0,state_rows;
    const uint8_t *close_map;uint16_t close_map_n;uint8_t close_y0,close_rows;
    const char *close_title;uint8_t close_title_x,close_title_y,words_x,words_y;const uint8_t *city_spots;
    const uint8_t *radar_map;uint16_t radar_map_n;const uint8_t *radar_marks;const char *const *radar_town;
    int32_t radar_w,radar_e,radar_s,radar_n;  /* the radar rows' view, 1e-4 degrees */
} wx_pack;
static const wx_pack wx_pack_fl={
    "ORLANDO, FL","ORLANDO AREA","FL",28.5383,-81.3792,285383,-813792,"MLB",26,68,
    11,11,9,5,33,
    wx_fl_requests,wx_fl_request_period,wx_fl_obs_label,wx_fl_obs_slot,wx_fl_city_name,
    wx_fl_state_map,sizeof(wx_fl_state_map),WX_FL_STATE_Y0,WX_FL_STATE_ROWS,wx_fl_close_map,sizeof(wx_fl_close_map),WX_FL_CLOSE_Y0,WX_FL_CLOSE_ROWS,
    "CENTRAL FLORIDA",157,106,4,106,wx_fl_city_spots,
    wx_fl_radar_map,sizeof(wx_fl_radar_map),wx_fl_radar_marks,wx_fl_radar_town,
    -823000,-799000,279725,292500};
/* PC checks only: one more compile-time pack from a header outside these sources (-DWX_EXTRA_PACK_HEADER="..."
 * defining WX_EXTRA_PACK), to model an older launcher's built-in place. Never set for the console. */
#ifdef WX_EXTRA_PACK_HEADER
#include WX_EXTRA_PACK_HEADER
#endif
/* The place shown with no weather location record: Orlando, FL. */
#ifndef WX_DEFAULT_PACK
#define WX_DEFAULT_PACK (&wx_pack_fl)
#endif
#ifdef WX_EXTRA_PACK
#define WX_PACKS 2
static const wx_pack *const wx_packs[WX_PACKS]={&wx_pack_fl,WX_EXTRA_PACK};
#else
#define WX_PACKS 1
static const wx_pack *const wx_packs[WX_PACKS]={&wx_pack_fl};
#endif
#endif
