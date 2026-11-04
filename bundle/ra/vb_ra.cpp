/* vb_ra.cpp - RetroAchievements for vbrecomp games.
 *
 * Wired in by VBRecomp Tools when a conversion runs with
 * RetroAchievements on. Upstream vbrecomp ships no RA support; the
 * engine adds this module to the staged project and patches two
 * staged framework files (marker-guarded, restored byte-identical
 * when RA is off):
 *
 *  - vbrecomp/runtime/src/vip.c: vb_ra_frame_tick() is invoked at
 *    every guest GAME_START frame tick, beside upstream's own
 *    vb_mod_runtime_frame_tick_c() call.
 *  - vbrecomp/runtime/src/main.cpp: vb_ra_bind_ui() is called with
 *    the host's RecompRuntimeUi right after the host creates it,
 *    so login / achievement toasts draw through the same runtime
 *    UI the in-game menu uses (recomp_runtime_ui_set_toast).
 *
 * Family design (same as the GB/SNES/GBA/Genesis/SMS-GG modules):
 *
 *  - Identity: the ROM image the runtime itself loaded and
 *    CRC32-verified (vb_rom_data()/vb_rom_size() from the runtime's
 *    memory.h), hashed with rcheevos' own Virtual Boy algorithm
 *    (rc_hash_generate_from_buffer; RC_CONSOLE_VIRTUAL_BOY = 28 in
 *    the vendored rc_consoles.h; the VB method is the whole-file
 *    MD5). NOTE: the rc_hash API returns NON-ZERO on success
 *    (unlike rc_client's RC_OK == 0) - its header says so.
 *  - Memory: rcheevos' Virtual Boy map (rc_consoleinfo.c):
 *    0x00000-0x0FFFF is System RAM (the runtime's 64 KiB WRAM,
 *    vb_wram_data()) and 0x10000-0x1FFFF is Cartridge RAM (the
 *    runtime's 64 KiB cart-RAM buffer, vb_cart_ram_data()).
 *  - Server calls run on detached threads (ra_http: OpenSSL on
 *    Linux, WinHTTP on Windows); responses are delivered back on
 *    the game thread from the frame tick.
 *  - Toasts: the runtime-UI setter owns the toast's lifetime (the
 *    UI model keeps no clock), so the frame tick re-asserts a live
 *    toast and clears it after ~4 seconds.
 *  - Login: retroachievements.toml next to the exe (username +
 *    Connect api_token). Absent file = RA off, game runs normally.
 *    The game load is chained after a successful login so a
 *    "Login required" load failure cannot overwrite the
 *    login-failed toast the player needs to see.
 *
 * Hardcore note: rc_client is told the toml's hardcore flag, but
 * RetroAchievements counts hardcore only from approved clients; a
 * recomp client is an unknown emulator, so unlocks register as
 * Casual/softcore. Never spoof another client's identity.
 */
#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "rc_client.h"
#include "rc_consoles.h"
#include "rc_hash.h"
#include "ra_http.h"

#include "vb_ra.h"

#include "memory.h"              /* vb_rom_data / vb_wram_data / vb_cart_ram_data */
#include "recomp_runtime_ui.h"   /* host toast channel */

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <unistd.h>
#include <limits.h>
#endif

namespace {

/* ------------------------------------------------------------------ */
/* Config: retroachievements.toml next to the exe                      */
/* ------------------------------------------------------------------ */

struct RaConfig {
    std::string username;
    std::string api_token;
    bool hardcore = false;
    bool present = false;
};

std::string ra_exe_dir() {
#ifdef _WIN32
    char path[MAX_PATH];
    DWORD n = GetModuleFileNameA(NULL, path, (DWORD)sizeof(path));
    if (n == 0 || n >= sizeof(path)) return std::string();
    std::string s(path);
    size_t slash = s.find_last_of("\\/");
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#else
    char path[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", path, sizeof(path) - 1);
    if (n <= 0) return std::string();
    path[n] = '\0';
    std::string s(path);
    size_t slash = s.find_last_of('/');
    return (slash == std::string::npos) ? std::string() : s.substr(0, slash);
#endif
}

std::string ra_trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r' || s[a] == '\n')) a++;
    while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r' || s[b - 1] == '\n')) b--;
    return s.substr(a, b - a);
}

std::string ra_unquote(const std::string& s) {
    if (s.size() >= 2 && s.front() == '"' && s.back() == '"')
        return s.substr(1, s.size() - 2);
    if (s.size() >= 2 && s.front() == '\'' && s.back() == '\'')
        return s.substr(1, s.size() - 2);
    return s;
}

RaConfig ra_read_config(const std::string& exe_dir) {
    RaConfig cfg;
    std::string path = exe_dir + "/retroachievements.toml";
    FILE* f = fopen(path.c_str(), "r");
    char line[1024];
    if (!f) return cfg;
    cfg.present = true;
    while (fgets(line, sizeof(line), f)) {
        std::string s = ra_trim(line);
        if (s.empty() || s[0] == '#') continue;
        size_t eq = s.find('=');
        if (eq == std::string::npos) continue;
        std::string key = ra_trim(s.substr(0, eq));
        std::string val = ra_unquote(ra_trim(s.substr(eq + 1)));
        if (key == "username") cfg.username = val;
        else if (key == "api_token") cfg.api_token = val;
        else if (key == "hardcore") cfg.hardcore = (val == "true" || val == "1");
    }
    fclose(f);
    return cfg;
}

/* ------------------------------------------------------------------ */
/* Memory peek: rcheevos' Virtual Boy map translated to the runtime's  */
/* storage (rc_consoleinfo.c rc_memory_regions_virtualboy):            */
/*   rc 0x00000-0x0FFFF -> WRAM (real 0x05000000), vb_wram_data()      */
/*   rc 0x10000-0x1FFFF -> Cartridge RAM (real 0x06000000),            */
/*                         vb_cart_ram_data() (a full 64 KiB buffer)   */
/* ------------------------------------------------------------------ */

uint32_t ra_read_memory(uint32_t address, uint8_t* buffer, uint32_t num_bytes,
                        rc_client_t* /*client*/) {
    if (!buffer || num_bytes == 0) return 0;
    const uint8_t* wram = vb_wram_data();
    const uint8_t* cram = vb_cart_ram_data();
    for (uint32_t i = 0; i < num_bytes; i++) {
        uint32_t a = address + i;
        uint8_t v = 0;
        if (a < 0x10000u) v = wram ? wram[a] : 0;
        else if (a < 0x20000u) v = cram ? cram[a - 0x10000u] : 0;
        buffer[i] = v;
    }
    return num_bytes;
}

/* ------------------------------------------------------------------ */
/* Server calls: detached thread + game-thread delivery                */
/* ------------------------------------------------------------------ */

struct RaPending {
    rc_client_server_callback_t callback = nullptr;
    void* callback_data = nullptr;
    char* body = nullptr;
    size_t body_len = 0;
    int http_status = 0;
};

std::mutex g_pending_mutex;
std::vector<RaPending> g_pending;

void ra_server_call(const rc_api_request_t* request,
                    rc_client_server_callback_t callback, void* callback_data,
                    rc_client_t* /*client*/) {
    std::string url = request->url ? request->url : "";
    std::string post = request->post_data ? request->post_data : "";
    std::string ctype = request->content_type ? request->content_type : "";
    std::thread([url, post, ctype, callback, callback_data]() {
        char* body = nullptr;
        size_t body_len = 0;
        int status = 0;
        if (ra_http_post(url.c_str(), post.c_str(), ctype.c_str(),
                         &body, &body_len, &status) != 0) {
            body = nullptr;
            body_len = 0;
            status = 0;
        }
        RaPending p;
        p.callback = callback;
        p.callback_data = callback_data;
        p.body = body;
        p.body_len = body_len;
        p.http_status = status;
        {
            std::lock_guard<std::mutex> lock(g_pending_mutex);
            g_pending.push_back(p);
        }
    }).detach();
}

void ra_deliver_pending() {
    std::vector<RaPending> due;
    {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        due.swap(g_pending);
    }
    for (auto& p : due) {
        rc_api_server_response_t resp;
        memset(&resp, 0, sizeof(resp));
        resp.body = p.body ? p.body : "";
        resp.body_length = p.body_len;
        resp.http_status_code = p.http_status;
        if (p.callback) p.callback(&resp, p.callback_data);
        free(p.body);
    }
}

/* ------------------------------------------------------------------ */
/* Toasts: through the runtime UI the host created. The setter owns    */
/* the toast's lifetime (the UI model keeps no clock), so the frame    */
/* tick re-asserts a live toast and clears it after ~4 seconds.        */
/* ------------------------------------------------------------------ */

void* g_ui = nullptr;

struct RaToast {
    std::string title;
    std::string body;
    std::chrono::steady_clock::time_point until{};
    bool active = false;
};
RaToast g_toast;

void ra_toast_apply() {
    if (!g_ui) return;
    RecompRuntimeUi* ui = (RecompRuntimeUi*)g_ui;
    if (g_toast.active)
        recomp_runtime_ui_set_toast(ui, g_toast.title.c_str(),
                                    g_toast.body.c_str());
    else
        recomp_runtime_ui_set_toast(ui, NULL, NULL);
}

void ra_toast_show(const std::string& title, const std::string& body) {
#ifdef RA_DISABLE_POPUPS
    /* VBRecomp Tools option: pop-ups hidden at build time. This
     * also hides the login toast, which uses the same channel. */
    (void)title; (void)body;
    return;
#else
    g_toast.title = title;
    g_toast.body = body;
    g_toast.until = std::chrono::steady_clock::now() +
                    std::chrono::milliseconds(4000);
    g_toast.active = true;
    ra_toast_apply();
#endif
}

void ra_toast_pump() {
    if (!g_toast.active) return;
    if (std::chrono::steady_clock::now() >= g_toast.until) {
        g_toast.active = false;
        ra_toast_apply(); /* clears */
        return;
    }
    ra_toast_apply(); /* re-assert while live */
}

/* ------------------------------------------------------------------ */
/* Events + async callbacks                                            */
/* ------------------------------------------------------------------ */

rc_client_t* g_client = nullptr;
char g_rom_hash[33] = "";

void ra_event_handler(const rc_client_event_t* event, rc_client_t* /*client*/) {
    if (!event) return;
    switch (event->type) {
        case RC_CLIENT_EVENT_ACHIEVEMENT_TRIGGERED:
            if (event->achievement) {
                fprintf(stdout,
                        "[RA] Achievement unlocked: %s (%u points) - %s\n",
                        event->achievement->title ? event->achievement->title : "?",
                        event->achievement->points,
                        event->achievement->description
                            ? event->achievement->description
                            : "");
                fflush(stdout);
                ra_toast_show(
                    event->achievement->title ? event->achievement->title : "?",
                    event->achievement->description
                        ? event->achievement->description
                        : "");
            }
            break;
        case RC_CLIENT_EVENT_GAME_COMPLETED:
            fprintf(stdout, "[RA] All achievements for this game earned!\n");
            fflush(stdout);
            ra_toast_show("RetroAchievements", "Game mastered!");
            break;
        case RC_CLIENT_EVENT_SERVER_ERROR:
            if (event->server_error) {
                fprintf(stderr, "[RA] Server error (%s): %s\n",
                        event->server_error->api ? event->server_error->api : "?",
                        event->server_error->error_message
                            ? event->server_error->error_message
                            : "?");
            }
            break;
        case RC_CLIENT_EVENT_DISCONNECTED:
            fprintf(stderr, "[RA] Disconnected: unlocks will queue until reconnect.\n");
            break;
        case RC_CLIENT_EVENT_RECONNECTED:
            fprintf(stdout, "[RA] Reconnected: queued unlocks submitted.\n");
            fflush(stdout);
            break;
        default:
            break;
    }
}

void ra_load_callback(int result, const char* error_message,
                      rc_client_t* client, void* /*userdata*/) {
    if (result != RC_OK) {
        fprintf(stdout, "[RA] Game load failed: %s\n",
                error_message ? error_message : rc_error_str(result));
        fflush(stdout);
        ra_toast_show("RetroAchievements",
                      "Game load failed - playing without achievements");
        return;
    }
    const rc_client_game_t* game = rc_client_get_game_info(client);
    if (game && game->id != 0) {
        fprintf(stdout, "[RA] Game loaded: %s (id %u)\n", game->title, game->id);
        fflush(stdout);
        ra_toast_show("RetroAchievements", game->title);
    } else {
        fprintf(stdout, "[RA] Game not recognised by RetroAchievements "
                        "(no achievement set for this hash)\n");
        fflush(stdout);
        ra_toast_show("RetroAchievements", "Game not in the RA database");
    }
}

void ra_login_callback(int result, const char* error_message,
                       rc_client_t* client, void* /*userdata*/) {
    /* rcheevos reports RC_OK (0) on success, nonzero on failure. */
    if (result != RC_OK) {
        fprintf(stdout, "[RA] Login failed: %s\n",
                error_message ? error_message : rc_error_str(result));
        fflush(stdout);
        ra_toast_show("RetroAchievements",
                      "Login failed - check retroachievements.toml");
    } else {
        const rc_client_user_t* user = rc_client_get_user_info(client);
        fprintf(stdout, "[RA] Logged in as %s\n",
                (user && user->display_name) ? user->display_name : "?");
        fflush(stdout);
        ra_toast_show("RetroAchievements", "Logged in");
    }
    /* Chain the load only on success: after a failed login the load
     * can only fail with "Login required", and its toast would
     * instantly overwrite the login-failed toast the player needs
     * to see. */
    if (result == RC_OK && g_rom_hash[0]) {
        rc_client_begin_load_game(client, g_rom_hash,
                                  ra_load_callback, nullptr);
    }
}

/* ------------------------------------------------------------------ */
/* Init (lazy, first frame tick)                                       */
/* ------------------------------------------------------------------ */

bool g_init_done = false;

void ra_init() {
    std::string exe_dir = ra_exe_dir();
    /* Dev-only test hook (NOT shipped in packages): if
     * ra_test_toast.txt sits next to the exe, pop its first two
     * lines as a toast so the pop-up channel can be verified
     * without a real RA unlock. */
    {
        std::string test_path = exe_dir + "/ra_test_toast.txt";
        FILE* tf = fopen(test_path.c_str(), "r");
        if (tf) {
            char tline[256] = "";
            char bline[512] = "";
            std::string t = "Test Achievement";
            std::string b = "In-game pop-ups are working!";
            if (fgets(tline, sizeof(tline), tf)) {
                std::string s = ra_trim(tline);
                if (!s.empty()) t = s;
                if (fgets(bline, sizeof(bline), tf)) {
                    std::string s2 = ra_trim(bline);
                    if (!s2.empty()) b = s2;
                }
            }
            fclose(tf);
            ra_toast_show(t, b);
            fprintf(stdout, "[RA] test toast armed: %s\n", t.c_str());
            fflush(stdout);
        }
    }
    RaConfig cfg = ra_read_config(exe_dir);
    if (!cfg.present) {
        fprintf(stdout,
                "[RA] Disabled: no retroachievements.toml next to the exe.\n"
                "[RA] To enable: put retroachievements.toml with your\n"
                "[RA] username and Connect token next to the game, then "
                "restart.\n");
        fflush(stdout);
        return;
    }

    const uint8_t* rom = vb_rom_data();
    uint32_t rom_size = vb_rom_size();
    if (!rom || rom_size == 0) {
        fprintf(stderr, "[RA] Disabled: the runtime has no ROM loaded.\n");
        return;
    }
    memset(g_rom_hash, 0, sizeof(g_rom_hash));
    /* NOTE: the rc_hash API returns NON-ZERO on success (unlike
     * rc_client's RC_OK == 0) - its header says so explicitly. */
    if (!rc_hash_generate_from_buffer(g_rom_hash, RC_CONSOLE_VIRTUAL_BOY,
                                      rom, rom_size)) {
        fprintf(stderr, "[RA] Disabled: could not hash the loaded ROM.\n");
        return;
    }
    fprintf(stdout, "[RA] ROM hash: %s (console %u)\n", g_rom_hash,
            (unsigned)RC_CONSOLE_VIRTUAL_BOY);

    g_client = rc_client_create(ra_read_memory, ra_server_call);
    if (!g_client) {
        fprintf(stderr, "[RA] Disabled: rc_client_create failed.\n");
        return;
    }
    rc_client_set_event_handler(g_client, ra_event_handler);
    rc_client_set_hardcore_enabled(g_client, cfg.hardcore ? 1 : 0);
    if (cfg.username.empty() || cfg.api_token.empty()) {
        fprintf(stdout,
                "[RA] No username/token in retroachievements.toml "
                "- loading game without login.\n");
        fflush(stdout);
        rc_client_begin_load_game(g_client, g_rom_hash,
                                  ra_load_callback, nullptr);
        return;
    }
    fprintf(stdout, "[RA] Logging in as %s (hardcore %s)...\n",
            cfg.username.c_str(), cfg.hardcore ? "on" : "off");
    fflush(stdout);
    rc_client_begin_login_with_token(g_client, cfg.username.c_str(),
                                     cfg.api_token.c_str(),
                                     ra_login_callback, nullptr);
}

} /* namespace */

/* Called from the patched runtime main.cpp with the runtime UI the
 * host created for this process. */
extern "C" void vb_ra_bind_ui(void* ui) {
    g_ui = ui;
}

/* Called from the patched runtime vip.c at every guest GAME_START
 * frame tick. */
extern "C" void vb_ra_frame_tick(void) {
    if (!g_init_done) {
        g_init_done = true;
        ra_init();
    }
    ra_deliver_pending();
    if (g_client) rc_client_do_frame(g_client);
    ra_toast_pump();
}
