FROM python:3.12-slim AS base

# Pinned rather than left to whatever --system would pick on its own --
# a bind-mounted host directory (e.g. for storage.data_dir) needs a
# stable, documented UID/GID to be pre-chowned to (`chown -R 999:999`),
# not one that could shift on a future rebuild.
RUN groupadd --gid 999 bridge \
    && useradd --system --uid 999 --gid 999 --create-home --shell /usr/sbin/nologin bridge

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

# ENTRYPOINT is exec-form, so an argument passed to `docker run`/`docker
# compose run` is APPENDED to it, not substituted for it -- e.g.
# `docker run <image> -m bridge.appservice ...` actually runs
# `python main.py -m bridge.appservice ...`, not the one-off appservice.py
# CLI. To run that (or any other one-off command in this image) instead
# of the bridge itself, override the entrypoint explicitly:
#   docker run --rm --entrypoint python -v ...:/config/config.yaml:ro \
#     -v bridge-data:/data <image> -m bridge.appservice \
#     /config/config.yaml /data/appservice-registration.yaml
# /data is the only writable, volume-declared path in this image --
# /config is a read-only single-file bind mount -- so that's where the
# generated registration file needs to land.

# bridge.listen_host defaults to 127.0.0.1 (config.example.yaml) -- the
# mounted config.yaml MUST override this to 0.0.0.0, or nothing outside
# the container's own network namespace can reach it even with the port
# below published. listen_port defaults to 8090; EXPOSE here is
# documentation only (doesn't itself publish anything) and assumes the
# default -- adjust if config.yaml sets a different listen_port.
EXPOSE 8090

ENTRYPOINT ["python", "main.py"]
