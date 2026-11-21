# The image the end-to-end test on kind runs (.github/workflows/kubernetes.yml).
# It is built in CI and loaded into the kind cluster, never pushed. The build
# context holds dist/ (runtraj's wheel from this commit) and hysplit/ (the
# HYSPLIT folder .github/scripts/install_hysplit.sh puts in /opt/hysplit).
FROM python:3.13-slim
COPY dist/ /tmp/dist/
RUN pip install --no-cache-dir /tmp/dist/*.whl s3fs && rm -rf /tmp/dist
COPY hysplit/ /opt/hysplit/
ENV PATH=/opt/hysplit/exec:$PATH
