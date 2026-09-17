# The pipeline runner: one always-on process (runner/main.py).
#
# Replaces the Airflow image. Java stays because the history jobs are Spark;
# everything else is a plain Python process. Source is mounted, not copied, so
# a code change is a container restart, not a rebuild. The warehouse lives in
# a named volume (/data), not the Windows bind mount: DuckDB's file locking
# and random I/O are unreliable and slow across the Docker Desktop file share.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless tini \
 && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

COPY runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --timeout 300 --retries 5 -r /tmp/requirements.txt

ENV PYTHONPATH=/opt/project/src:/opt/project \
    PYTHONUNBUFFERED=1 \
    PYSPARK_PYTHON=/usr/local/bin/python \
    DUCKDB_PATH=/data/arbibet.duckdb \
    LAKE_PATH=/data/lake
WORKDIR /opt/project

# tini forwards SIGTERM, so `docker stop` lets the runner finish its current
# job and checkpoint the database instead of being killed mid-write.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "runner.main"]
