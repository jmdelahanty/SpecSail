# Projector calibration run: operator checklist

A step-by-step checklist for measuring a Citrus-driven projector with SpecSail, arena by arena. It was written
after the first omnifin0 runs (2026-10-19/20); every item marked **Why** comes from something that went wrong or
nearly did. Tick the boxes as you go (GitHub renders them as checkboxes).

Rig-specific values below are for **omnifin0** on **pancake0**: BenQ X3100i projector, Ocean Insight QE Pro
(QEP04819) with a P50-1-VIS-NIR fiber, IR light arrays on PDU outlets 7 and 8. Adjust them for another rig.

---

## 1. Days before

- [ ] **Pick or write the plan** (`citrus.projector_spectral_run_plan` v1, see `examples/`). Run
      `python -c "import calibration_run as c; c.load_plan('PLAN.json')"` to check it.
  - Positions: one per arena centre, plus any repeats.
  - Patches: primaries at 255, R/G/B/gray ramps, the protocol grays your experiments use.
  - `drift_reference.every_n_patches`: **10** (a white check every 10 patches).
  - `stability.max_attempts`: at least **4**, so the first minute after placement can settle.
  - Integration: `min_ms` at least one display frame, `frame_rate_hz` = the display's refresh rate.
- [ ] **No recordings or builds booked on the rig** for the run (ask Orange). The run takes about 4 hours for
      four arenas at 10 s integrations, plus placement time.
- [ ] **A Citrus build with the `canvas_fill` calibration scene** is available.
- [ ] **A rigid fiber mount**: articulated arm on a magnetic base, a fiber clamp, a small **bubble level** on the
      clamp, tape. A fixed fixture is better when one exists (`fixture.fish_imaging.fish_plane_probe` in
      hardware_library).
  - **Why:** a hand-held or loosely held fiber drifted 29% in 40 minutes in the first attempt.

## 2. Rig preparation (on the day)

- [ ] **Room lights off.**
- [ ] **IR light arrays off.** Switch only outlets 7 and 8:
  ```bash
  cd ~/pancakebatter && conda activate rig_control
  ./pdu.py --host 192.168.20.177 --action status
  ./pdu.py --host 192.168.20.177 --action off --outlet 7,8 --verify
  ```
  - **Always give `--outlet 7,8`.** Without it the script switches every outlet, cameras included.
  - Don't use the `cams-on` or `reboot-cams` aliases or `reboot_cams.sh` until the run ends; they turn all
    outlets back on.
  - **Why:** the arrays add an 850 nm peak to every spectrum and a background under every patch.
- [ ] **Projector warmed up for at least 30 minutes**, and **not put into standby** since.
  - **Why:** standby makes X re-place the displays and moves the Citrus canvas off the projector.
- [ ] **Projector settings recorded as found.** Photograph every menu page before changing anything:
  - Game & Pic: Picture Mode, Brightness, Contrast, Sharpness, Details Adjustment, Auto Game Mode, Digital Zoom.
  - Advanced Color Settings: Gamma, Color Temperature + Tuning, Color Management, **Light Source Mode**, HDR
    Brightness, Noise Reduction.
  - Display: Aspect Ratio, 4K Upscaling, 3D, HDMI Settings (Signal Format, Equalizer, EDID).
  - Installation: Projector Position, 2D Keystone, Test Pattern, High Altitude, Baud Rate.
  - Information: resolution and refresh, colour system, dynamic range (SDR), light-source hours, firmware.
- [ ] **Nothing on the projector changes with content:**
  - Light Source Mode **Normal** (not Dynamic, which changes LED power with picture content);
  - Details Adjustment **Off**;
  - Auto Game Mode **Off**;
  - SDR, 3D Off.
- [ ] **GPU output checked** (read-only):
  ```bash
  export DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority
  nvidia-settings --config=/dev/null -t -q '[dpy:DP-3]/CurrentColorRange'   # 0 = full range
  xrandr --verbose | grep -A30 '^DP-3 connected' | grep -E 'Gamma|Brightness'  # 1.0:1.0:1.0 / 1.0
  ```
  Never open the nvidia-settings window while Citrus runs.
- [ ] **Projector menu closed.** **Why:** the menu is projected, and the fiber would measure it.

## 3. Citrus

- [ ] **Start Citrus with the control socket**, from a tmux window, with the display environment set:
  ```bash
  tmux new-window -n citrus 'env DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority \
    XDG_RUNTIME_DIR=/run/user/1000 XDG_SESSION_TYPE=x11 \
    CITRUS_GUI_LOCAL_CONTROL_SOCKET=/tmp/citrus_local_control.sock <path-to-citrus>; read -p "Citrus exited"'
  ```
  A worktree build also needs `CITRUS_PROJECT_ROOT_OVERRIDE=<main checkout>` to find the rig configs.
- [ ] **Load the rig and canvas** in the GUI (Rig Directory, then Canvas Config, e.g. `omnifin0` / `shadow`).
  - **Why:** without a loaded canvas the first scene fails with `arena_not_found_or_ambiguous`.
- [ ] **For single-arena plans (`uniform_gray` backend): clear every preview** in the GUI for all arenas (preview
      mode None, and no protocol-step, arena-definition or experimental-area preview), then check by eye that only
      the target arena lights up.
  - **Why:** a one-arena scene leaves the other arenas showing their previews, which adds stray light.
    `canvas_fill` (and the planned `arena_fills`) control every arena and don't need this.

## 4. Fiber mount

- [ ] Fiber **perpendicular** to the surface: the **bubble centred**.
- [ ] Tip about **1 mm** above the surface (or the standoff your plan states), the **same at every arena**.
- [ ] **Spectrometer on a stable surface at about shelf height, its fiber cable slack and taped 5–15 cm behind the
      connector. Never move the box or touch the connector during a run.**
  - **Why:** the fiber end sits against a slit about as wide as its 50 µm core. Reseating the connector moved white by up
    to 3× with a taut cable, and 12% with a slack one. Nudging the box between arenas dropped the level about 3.5×
    mid-run (2026-10-20).
- [ ] **Cable taped to the arm**, with a little slack between the tape and the connector, so no cable weight
      hangs on the tip.
  - **Why:** a free cable loop can slowly lever the tip over minutes.
- [ ] Write down **what surface the fiber is over**: bare film, film plus an empty dish, or water. **Never put
      the patch cord in water**; it is not an immersion probe.

## 5. Starting the run

```bash
cd ~/SpecSail
pixi run calrun runs/plans/PLAN.json --socket /tmp/citrus_local_control.sock
```

- [ ] At the warm-up question, type **`y`** explicitly. **An empty answer counts as no** and aborts the run.

## 6. At every placement prompt

- [ ] Move the arm so the tip is over the **crosshair** of this arena.
- [ ] Check the **bubble** is centred.
- [ ] **Take photos now, before confirming**: one top-down showing the bubble, one from the side.
  - **Why:** the run starts measuring the moment you confirm. A phone's focus light, or you leaning over the rig,
    swung a settling check by 45% at arena_3.
- [ ] Holder prompt: press **Enter** (the default, `clamped`). Leave the geometry note **blank** unless you saw
      something worth recording.
  - **Why:** typing `y` at these text prompts records "y" as the answer.
- [ ] Confirm, then **step away** and stay away from the rig until the next prompt.

## 7. While it runs

- [ ] **Watch the drift references** (white, every 10 patches). Within about **±1%** of each other is good.
  - A one-off "unstable" flag on the first reference after placement usually means the light was still
    settling. Check that the recorded scans are steady.
  - A steady climb or fall above about **2%** means the mount is moving. Let the current arena finish, then
    re-secure at the next placement prompt.
- [ ] **Keep every window off the projector display.** The projector is part of the desktop, so a window dragged
      past the edge of the main screen lands on the canvas.
  - **Why:** a window dragged onto the projector for about a second cut one 10 s scan by 11% (2026-10-20).
- [ ] **Don't take desktop screenshots, open the Activities overview or lock the screen during a run.** GNOME's
      screenshot tool dims every monitor, the projector included.
  - **Why:** a screenshot during a green patch cut one scan by 27% (2026-10-20).
    Background on window stacking and presentation: Citrus
    `docs/stimulus_window_stacking_and_presentation_2026-10-20.md` (Citrus PR #66).
- [ ] **Stopping is safe.** Ctrl+C restores Citrus, and the bundle keeps everything measured so far, marked
      interrupted.
- [ ] Glance at a spectrum now and then:
  - no **~850 nm peak** (that would mean the IR arrays are on);
  - nothing saturated.

## 8. After the last arena

- [ ] **Closure check:** return to the first arena and re-measure white, the primaries and the protocol grays
      (a one-position plan). It should match the first arena within 1–2%.
- [ ] **Re-placement repeatability:** lift and re-place the fiber on one arena five times, with white each time
      (a five-position plan on one arena). The spread is your placement uncertainty.
- [ ] **IR arrays back on before any recording:**
  ```bash
  ./pdu.py --host 192.168.20.177 --action on --outlet 7,8 --verify
  ```
- [ ] **Operator notes in the bundle** (`runs/<run_id>/operator_notes.json`):
  - fiber mount and standoff, and the surface it was over;
  - room lights, IR arrays and outlet state (with the time read);
  - when the projector was powered on;
  - projector light-source hours;
  - a link to the as-found settings photos;
  - anything unusual.

  Put the photos in `runs/<run_id>/operator_photos/`.
- [ ] Tell the people who use the result: the rig owner (Pancake Batter records), Palette (dataset intake),
      and whoever owns the protocols. If any projector setting changed, record it with **old → new, who, when**.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `arena_not_found_or_ambiguous:arena_1` at the first scene | Canvas not loaded in Citrus | Load the rig and canvas in the GUI, then restart the run |
| `GLFW Error 65544: DISPLAY missing` when starting Citrus | Started from a shell without the display environment | Use the tmux command in section 3 |
| Run aborts right after starting | Empty answer to the warm-up question | Answer `y` explicitly |
| Peak at ~850 nm in every spectrum | IR light arrays on | Switch off outlets 7 and 8 |
| White drift reference climbs or falls for many minutes | Mount, cable or film settling | Re-secure at the next placement; tape the cable to the arm |
| "unstable" flag on the first reference after placement | Light still settling, or someone at the rig | Check the recorded scans; take photos before confirming next time |
| Canvas moved off the projector | Projector was put in standby | Don't use standby during a session; restore the display layout and restart Citrus |
| Low drive levels all read the same as black | Projector settings (e.g. low Brightness) crush the dark end | Not a run fault; it is what the calibration is for. Record the setting |

## Related reading

- Citrus `docs/projector_spectral_calibration_run_design_2026-10-19.md`: design of the run.
- Citrus `docs/stimulus_window_stacking_and_presentation_2026-10-20.md`: why a window can cover the canvas, and
  how frames reach the projector.
- hardware_library `docs/learning/`:
  - video range, TV timings and dithering;
  - cosine correctors and fiber collection;
  - where a brightness correction can live.
