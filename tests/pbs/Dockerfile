# The one-node PBS cluster the end-to-end PBS test runs on
# (.github/workflows/pbs.yml): OpenPBS 23.06, from Altair's RPMs on Rocky
# Linux 8, with its server, scheduler and MoM in one container. The OpenPBS
# install is dask-jobqueue's PBS test image, pinned by digest: Altair's own
# download times out from GitHub's runners. Only its OpenPBS is used; this
# entrypoint replaces its own. It is built in CI and never pushed. runtraj,
# Python and HYSPLIT's replay folder are not in it: the workflow mounts the
# repository and installs them as the test's user.
FROM daskdev/dask-jobqueue:pbs@sha256:3b09eb1942fc07a18187f102afdd8ad4c0b648905f1c6a52c0deba07de31a355
ENV PATH=/opt/pbs/bin:$PATH
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["bash", "/entrypoint.sh"]
