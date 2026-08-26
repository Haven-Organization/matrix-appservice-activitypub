FROM python:3.12-slim AS base

RUN useradd --system --create-home --shell /usr/sbin/nologin bridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge/ ./bridge/
COPY main.py .

# storage.data_dir defaults to the relative "./data" (config.example.yaml),
# which resolves against WORKDIR /app -- owned by root, not the "bridge"
# user below, so sqlite fails to start with a PermissionError unless the
# mounted config.yaml explicitly sets storage.data_dir to this path.
# Confirmed live: only actually written to when storage.backend is sqlite,
# but harmless to create unconditionally for the postgresql case.
RUN mkdir -p /data && chown bridge:bridge /data
VOLUME ["/data"]

USER bridge
ENV BRIDGE_CONFIG=/config/config.yaml

# bridge.listen_host defaults to 127.0.0.1 (config.example.yaml) -- the
# mounted config.yaml MUST override this to 0.0.0.0, or nothing outside
# the container's own network namespace can reach it even with the port
# below published. listen_port defaults to 8090; EXPOSE here is
# documentation only (doesn't itself publish anything) and assumes the
# default -- adjust if config.yaml sets a different listen_port.
EXPOSE 8090

ENTRYPOINT ["python", "main.py"]
