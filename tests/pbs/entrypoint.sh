#!/bin/bash
# Start PBS on this one host (server, scheduler, MoM), make it the one
# execution node, and add the test's user with the runner's uid, so the
# files it writes in the mounted repository are the runner's.
set -euo pipefail
host=$(hostname)
sed -i "s/^PBS_SERVER=.*/PBS_SERVER=$host/; s/^PBS_START_MOM=.*/PBS_START_MOM=1/" /etc/pbs.conf
grep -q '^PBS_START_MOM=' /etc/pbs.conf || echo "PBS_START_MOM=1" >> /etc/pbs.conf
echo "\$clienthost $host" > /var/spool/pbs/mom_priv/config
/etc/init.d/pbs start
qmgr -c "create node $host" 2>/dev/null || true
qmgr -c "set server scheduler_iteration = 5"
qmgr -c "set server job_history_enable = True"
useradd --non-unique --uid "${RUNNER_UID:-1001}" --create-home runner
# The repository is mounted under /home/runner, so Docker made that folder,
# as root, before the user existed.
chown runner: /home/runner
touch /tmp/pbs-ready
exec sleep infinity
