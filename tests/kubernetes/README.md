# The end-to-end test on kind

`.github/workflows/kubernetes.yml` runs a real project on a kind cluster
(Kubernetes in Docker, on the GitHub runner) on every pull request. It needs
Docker, so it runs in CI only.

- `demo/`: two releases, 3 hours back, 100 particles, met at `/met`, inputs
  copied to `s3://runtraj/projects/demo` and results on `s3://runtraj/output`
  (`execution: kubernetes:` in its `config.yaml`), and `sites.csv`, one site,
  for the schedule step. A backward run also needs the hour after its
  release, so the releases end at 04:00: the met is the one 6-hour HRRR file
  `20210115_00-05`, cropped to Utah, which the workflow downloads from NOAA
  and caches.
- `cluster.yaml`: a moto S3 server, the Secret that points the pods at it
  (`AWS_ENDPOINT_URL`), and the met folder as a PersistentVolumeClaim. One
  `List`, so it is one YAML document (the `check-yaml` hook takes no more).
- `Dockerfile`: the pods' image, built in the workflow from this commit's
  wheel and a HYSPLIT folder whose programs replay recorded runs. It is loaded
  into the cluster and never pushed: runtraj publishes no image and no HYSPLIT.
- `tests/recorded/kubernetes/5.1.0/`: the two runs of `demo/` (and HYSPLIT's
  version call), recorded on HYSPLIT 5.1.0 (`tests/fixtures/replay.py`).

The workflow submits `demo/`, waits for the Job, and checks that
`runtraj status` counts 2 complete simulations and one complete Job. Then it
makes the same Job with a project that is missing and checks that it fails
with reason `PodFailurePolicy`, without retries (exit code 2). Last, it
schedules `demo/`, starts one tick from the CronJob by hand, and checks that
the tick added its hours to the bucket's `releases.csv` and that its Job
failed by the exit-code rule (`FailedIndexes`): the demo's met is from 2021,
so today's hours cannot run, as when an hour's met is not out yet.

**No HYSPLIT is downloaded.** The workflow's "Make the HYSPLIT folder" step
makes `/opt/hysplit` (`hycs_std` replays the recorded runs) and `/opt/replay`
(`replay.py` and the cases) on the runner, and the image gets both as they are.
The runner needs the folder too (`runtraj submit` checks for `hycs_std`), and
it must be the same one: `hycs_std`'s checksum is part of each run's settings
folder, so `runtraj status` on the runner would not find what the pods wrote
with another. The program is made with `--python python3`, so it does not name
the runner's interpreter. A change to what runtraj gives HYSPLIT in these runs
makes a pod fail with "no recorded hycs_std run has these inputs": record
again with `just record-kubernetes` (CONTRIBUTING.md, "Testing against real
HYSPLIT").

Two things to know:

- `AWS_ENDPOINT_URL` sends every S3 call to moto, NOAA's too, so the pods'
  met must be the mounted folder, never a `download:`.
- runtraj reads the met's first file to name the results, so wherever
  `runtraj submit` or `status` runs, the met must be at the path
  `config.yaml` names: the workflow links the runner's folder to `/met`.
