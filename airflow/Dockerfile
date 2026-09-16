# Airflow image for the capstone pipeline.
#
# Why a custom image at all: the official Airflow compose runs Linux
# containers, and the development venv on this host is a Windows venv
# (.venv/Scripts/python.exe). A BashOperator cannot execute Windows binaries
# from inside a Linux container, so "shell out to the host venv" is not
# available. Everything the DAG needs therefore lives in the image.
#
# What is NOT in the image: the project's own source, and any credential.
# Source is mounted so a code change does not mean a rebuild; credentials are
# mounted so they never enter a layer.

FROM apache/airflow:2.10.3

# Spark needs a JVM. 17 is what Spark 4 supports -- see the README's Spark
# section for why 21 is the only other option and 25 fails outright.
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends openjdk-17-jre-headless \
 && rm -rf /var/lib/apt/lists/*
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

USER airflow
COPY requirements-pipeline.txt /tmp/requirements-pipeline.txt
RUN python -m venv /home/airflow/venv \
 && /home/airflow/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /home/airflow/venv/bin/pip install --no-cache-dir -r /tmp/requirements-pipeline.txt

# The DAG refers to these rather than hardcoding paths, so moving the venv is
# a one-line change here instead of an edit in every task.
ENV PIPELINE_PYTHON=/home/airflow/venv/bin/python
ENV PIPELINE_DBT=/home/airflow/venv/bin/dbt
# Without this Spark launches bare `python` and the worker dies. Same failure
# as on the host, different interpreter.
ENV PYSPARK_PYTHON=/home/airflow/venv/bin/python
