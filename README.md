# SpecSail

SpecSail uses the seabreeze library to provide simple tools for collecting and visualizing light spectrum measurements.

## Features

- **Live viewer**: Real-time spectrum display with auto-scaling
- **Data logging**: Timed acquisition with configurable collection windows (2.5s per file)
- **Sunlight calibration**: Absolute irradiance calibration using AM1.5G solar reference
- **Visualization**: Plot spectra with statistics, peak detection, and mean calculation
- **Results archival**: Save plots and data with descriptive names
- **Data migration**: Convert legacy OceanView txt files to project CSV format

## Requirements

- Ocean Optics USB4000 spectrometer (or compatible seabreeze-supported device)
- Linux with udev rules configured for seabreeze
- [pixi](https://pixi.sh) package manager

## Installation

```bash
git clone <repository>
cd specsail
pixi install
```

For spectrometer access without root, set up udev rules:
https://python-seabreeze.readthedocs.io/en/latest/install.html#udev-rules

## Usage

### Live Viewer

```bash
pixi run run
```

Opens a real-time plot showing the current spectrum. Useful for alignment and checking signal levels. Updates every 100ms.

### Calibration

```bash
pixi run calibrate
```

Creates `calibration.json` using sunlight as a reference. Point the sensor at open sky (not directly at the sun) on a clear day. Valid calibration range: 300-900nm.

### Data Logging

```bash
pixi run log        # Run until Ctrl+C
pixi run log 10     # Collect exactly 10 files
```

Captures spectra in 2.5-second windows, saving all individual measurements (100ms integration each) to timestamped CSV files in `data/`. Audio feedback indicates capture timing.

### Plotting

```bash
pixi run plot                        # Plot spectra from data/
pixi run plot results/experiment_data  # Plot from specific directory
pixi run plot --min 0.01             # Set y-axis minimum
pixi run plot --max 1000             # Set y-axis maximum
pixi run plot --output result.png    # Save to file instead of displaying
pixi run plot --allpeaks             # Show all local maxima
```

Displays all spectra with individual traces and mean spectrum. Shows total irradiance statistics and peak wavelengths.

### Saving Results

```bash
pixi run save "Experiment name"
pixi run save "Experiment name" --min 0.01 --max 1000
pixi run save "Experiment name" --allpeaks
```

Saves the plot as PNG and moves data files to `results/Experiment name_data/`. Accepts the same `--min`, `--max`, and `--allpeaks` options as `pixi run plot`.

### Cleanup

```bash
pixi run clear
```

Deletes all CSV files in `data/`.

### Projector Calibration Runs

```bash
pixi run calrun examples/calibration_plan_gray_pilot.json --socket /tmp/citrus_local_control.sock
pixi run calrun examples/calibration_plan_omnifin0_pass1.json --socket /tmp/citrus_local_control.sock
```

Measures a projector driven by [Citrus](https://github.com/JohnsonLabJanelia/citrus) position by position. For each probe position in the plan, the run:

1. shows a placement target (crosshair overlay, or the dish outline for the gray pilot) and asks you to place the fiber;
2. steps every patch through Citrus calibration scenes, waits until Citrus reports the patch on screen plus a settle time and a stability check, then records raw scans;
3. records a projector-black dark at every integration time it used.

Citrus is restored after every run, including after errors and Ctrl+C. Integration is chosen automatically below saturation but never below `integration.min_ms`, and is rounded to whole display frames when `integration.frame_rate_hz` is set, so every scan averages complete projector frames. If a patch saturates even at the minimum, the run stops: attenuate the light rather than shortening the integration. Plans are JSON (`citrus.projector_spectral_run_plan` v1); see `examples/`. The gray pilot uses scenes Citrus has today (`uniform_gray`, one arena per position). The full-canvas plan needs the `canvas_fill` scene, which Citrus is adding.

Unlike `pixi run log`, a calibration run never applies `calibration.json`, never bins and never corrects. Each run writes a bundle in `runs/<run_id>/`:

```
capture_context.json          # identities, citations, outcome, sha256 of every file
run_plan.json
instrument/device_readout.json  # wavelengths, dark pixels, nonlinearity coefficients, TEC, limits
citrus/                       # Citrus status and canvas geometry at start and end
measurements/<id>/spectrum.csv     # pixel,wavelength_nm,counts_scan_0,... (raw counts, native pixels)
measurements/<id>/measurement.json # patch, scene status, integration, stability, saturation
log.jsonl
```

Design: Citrus `docs/projector_spectral_calibration_run_design_2026-10-19.md`.

### Data Migration

```bash
pixi run migrate
```

Converts legacy OceanView tab-separated txt files in `data/` to the CSV format used by this project. Original files are preserved.

## Output Format

CSV files contain wavelength bins (5nm steps) with one column per spectrum captured during the 2.5s window:

```
wavelength_nm,irradiance_uW_cm2_0,irradiance_uW_cm2_1,...
347.5,0.123456,0.124567,...
352.5,0.234567,0.235678,...
```

Calibrated values are in µW/cm² per 5nm bin. Without calibration, raw counts are stored instead (column prefix: `intensity_counts`).

## Calibration

The system supports two calibration sources:

1. **File calibration** (`calibration.json`): Created by `pixi run calibrate` using AM1.5G solar reference. Takes precedence.
2. **EEPROM calibration**: Factory calibration stored in spectrometer (if available).

If neither is available, raw intensity counts are logged.

## File Structure

```
specsail/
├── spectrometer.py         # Live spectrum viewer
├── spectrometer_logger.py  # Data acquisition and logging
├── spectrum_plot.py        # Shared plotting module
├── calibrate_sunlight.py   # Sunlight calibration
├── plot_spectra.py         # Plot CLI tool
├── save_results.py         # Save and archive results
├── migrate_data.py         # Legacy data migration
├── calibration_run.py      # Projector calibration runs driven through Citrus
├── citrus_control.py       # Citrus local control socket client
├── spectral_capture.py     # Raw native-pixel capture (no calibration, no binning)
├── examples/               # Calibration run plans
├── tests/                  # Unit tests (pixi run test)
├── calibration.json        # Calibration coefficients (if created)
├── data/                   # Collected spectrum CSV files
├── results/                # Saved plots and archived data
└── runs/                   # Calibration run bundles
```

## Dependencies

Managed by pixi:
- seabreeze (spectrometer communication)
- numpy (data processing)
- matplotlib (visualization)
