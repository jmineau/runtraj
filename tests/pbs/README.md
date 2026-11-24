# The end-to-end test on PBS

`.github/workflows/pbs.yml` runs a real project as a PBS job array on a
one-node OpenPBS cluster in a container on the GitHub runner. It needs
Docker, so it runs in CI only.

- `Dockerfile`, `entrypoint.sh`: OpenPBS 23.06 (Altair's RPMs on Rocky
  Linux 8, from dask-jobqueue's PBS test image, pinned by digest), with the
  server, scheduler and MoM in one container, which is its
  one execution node. Job history is on, so `qstat -x` shows a finished job.
  The container is built in the workflow and never pushed.
- `demo/`: the kind test's two releases (`tests/kubernetes/demo/`), 3 hours
  back, 100 particles, met at `/met`, run with `execution: pbs:` as two
  subjobs, one at a time. Its HYSPLIT replays the two runs recorded for the
  kind test (`tests/recorded/kubernetes/5.1.0/`), so no HYSPLIT is downloaded.

The workflow mounts the repository in the container at the same path, so a
subjob runs with the Python that submitted it. It runs `runtraj run`, which
submits the array with `qsub` and waits for it with `qstat`, and checks that
the array script asked for `-J 0-1%1`, that each subjob wrote its log, and
that `runtraj status` counts 2 complete simulations.
