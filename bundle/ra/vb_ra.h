/* vb_ra.h - RetroAchievements module entry points (VBRecomp Tools).
 *
 * vb_ra_bind_ui() is called from the engine-patched runtime main.cpp
 * right after the host creates its standard runtime UI.
 * vb_ra_frame_tick() is called from the engine-patched runtime
 * vip.c at every guest GAME_START frame tick, next to upstream's
 * own vb_mod_runtime_frame_tick_c().
 */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void vb_ra_bind_ui(void *ui);
void vb_ra_frame_tick(void);

#ifdef __cplusplus
}
#endif
