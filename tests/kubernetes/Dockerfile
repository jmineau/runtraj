# The image the end-to-end test on kind runs (.github/workflows/kubernetes.yml).
# It is built in CI and loaded into the kind cluster, never pushed. The build
# context holds dist/ (runtraj's wheel from this commit), hysplit/ and replay/
# (the folders the workflow's "Make the HYSPLIT folder" step made in
# /opt/hysplit and /opt/replay): HYSPLIT's programs replay runs recorded
# beforehand, so the image holds no HYSPLIT. python3 runs the replay.
FROM python:3.13-slim
COPY dist/ /tmp/dist/
RUN pip install --no-cache-dir /tmp/dist/*.whl s3fs && rm -rf /tmp/dist
COPY replay/ /opt/replay/
COPY hysplit/ /opt/hysplit/
ENV PATH=/opt/hysplit/exec:$PATH
