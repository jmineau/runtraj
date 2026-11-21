# HYSPLIT trajectory files for the reader's tests

Made by NOAA ARL's public HYSPLIT 5.4.2 (`hyts_std`, RHEL 8 build), with the
spaces at the ends of their lines trimmed, on two
cropped HRRR blocks of 2021-07-15 around Salt Lake City.

- `forward`: from this folder's `CONTROL` (the met directory shortened to
  `<met_cache>/`) and `SETUP.CFG`. Three trajectories from 02:00 UTC, 6 hours
  forward, a row every 30 minutes, with every value `hyts_std` can write along
  a trajectory. The first two start at one place, 10 m and 500 m above ground.
- `backward`: one trajectory from 40.5, -112.0, 10 m at 10:30 UTC, 6 hours
  backward with only `KAGL=1` in `SETUP.CFG`, so a row every hour and pressure
  alone. It leaves the met's domain after 4 hours, where its rows end.
