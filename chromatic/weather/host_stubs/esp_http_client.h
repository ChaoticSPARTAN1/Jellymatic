/* SPDX-License-Identifier: GPL-3.0-or-later
 * Additional terms: ADDITIONAL-TERMS.md
 * PC stand-in (see host.h): the types, fields and calls of esp_http_client.h (ESP-IDF 5.3) that
 * weather_esp32.c uses, with the same names and signatures. */
#ifndef WX_STUB_ESP_HTTP_CLIENT_H
#define WX_STUB_ESP_HTTP_CLIENT_H
#include <stdbool.h>
#include "host.h"
typedef struct esp_http_client *esp_http_client_handle_t;
typedef enum {HTTP_EVENT_ERROR,HTTP_EVENT_ON_CONNECTED,HTTP_EVENT_HEADERS_SENT,HTTP_EVENT_ON_HEADER,HTTP_EVENT_ON_DATA,
              HTTP_EVENT_ON_FINISH,HTTP_EVENT_DISCONNECTED,HTTP_EVENT_REDIRECT} esp_http_client_event_id_t;
typedef struct esp_http_client_event {
    esp_http_client_event_id_t event_id;
    esp_http_client_handle_t client;
    void *data;
    int data_len;
    void *user_data;
    char *header_key,*header_value;
} esp_http_client_event_t;
typedef esp_err_t (*http_event_handle_cb)(esp_http_client_event_t *evt);
typedef enum {HTTP_METHOD_GET,HTTP_METHOD_POST} esp_http_client_method_t;
typedef struct {
    const char *url;
    const char *user_agent;
    esp_http_client_method_t method;
    int timeout_ms;
    bool disable_auto_redirect;
    http_event_handle_cb event_handler;
    int buffer_size,buffer_size_tx;
    void *user_data;
    esp_err_t (*crt_bundle_attach)(void *conf);
} esp_http_client_config_t;
#define ESP_ERR_HTTP_BASE 0x7000
#define ESP_ERR_HTTP_EAGAIN (ESP_ERR_HTTP_BASE+7)
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *config);
esp_err_t esp_http_client_set_url(esp_http_client_handle_t client,const char *url);
esp_err_t esp_http_client_set_header(esp_http_client_handle_t client,const char *key,const char *value);
esp_err_t esp_http_client_set_timeout_ms(esp_http_client_handle_t client,int timeout_ms);
esp_err_t esp_http_client_open(esp_http_client_handle_t client,int write_len);
int64_t esp_http_client_fetch_headers(esp_http_client_handle_t client);
int esp_http_client_read(esp_http_client_handle_t client,char *buffer,int len);
int esp_http_client_get_status_code(esp_http_client_handle_t client);
int64_t esp_http_client_get_content_length(esp_http_client_handle_t client);
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t client);
esp_err_t esp_http_client_close(esp_http_client_handle_t client);
esp_err_t esp_http_client_cleanup(esp_http_client_handle_t client);
#endif
