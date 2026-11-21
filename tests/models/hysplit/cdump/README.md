# HYSPLIT concentration files for the reader's tests

Made by NOAA ARL's public HYSPLIT 5.4.2 (`hycs_std`, RHEL 8 build) from this
folder's `CONTROL` (the met directory shortened to `<met_cache>/`: two cropped
HRRR blocks of 2021-07-15 around Salt Lake City), with `numpar = 2000` and
`cpack` 1 or 0 in `SETUP.CFG`. A release at 01:30 UTC, 4 hours forward, two
pollutants (`TRCA`, 1 per hour for 1 hour; `TRCB`, 5 per hour for 0.5 hours)
and two grids:

- `average_cpack1`, `average_cpack0`: grid 1, centered on the release, 0.05°
  cells over 1°, levels 0 and 100 m, hourly averages; packed (`CPACK=1`) and
  full (`CPACK=0`). The same values either way.
- `snapshot_cpack1`: grid 2, centered at 40.7, -111.9, 0.1° cells over 2°, one
  level at 500 m, a snapshot every 2 hours.

`*.con2asc.txt` are HYSPLIT's own `con2asc -s -x` dumps of the packed files:
each sample's end hour, and the cells that are not zero, to 4 digits.

## Files for the `ichem` values

Made the same way (5.4.2, `numpar = 2000`, the same release time and met, 4
hours forward), each from `CONTROL.ichem` with the changes named: one
pollutant `TRCA` (1 per hour for 1 hour), one grid centered on the release
(0.05° cells over 1°, levels 0 and 100 m, hourly averages), three release
locations.

| File | `SETUP.CFG` | Change to `CONTROL.ichem` |
|---|---|---|
| `ichem1_3loc` | `ichem = 1` | none |
| `ichem0_3loc` | `ichem = 0` | none: the same run counted together |
| `ichem0` | `ichem = 0` | the first location only |
| `ichem6` | `ichem = 6` | the first location only |
| `ichem10_qcycle1` | `ichem = 10`, `qcycle = 1.0` | the first location only |
| `ichem0_qcycle1` | `ichem = 0`, `qcycle = 1.0` | the first location only: the same emissions counted together |
| `ichem9_levels50` | `ichem = 9` | the first location only; levels `50 1000` |
| `ichem0_levels50` | `ichem = 0` | the first location only; levels `50 1000` |
