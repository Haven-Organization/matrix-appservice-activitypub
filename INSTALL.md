# Installing matrix-appservice-activitypub

Setting the bridge up, natively or in Docker. For what the bridge does, see [README.md](README.md).

## Contents

- [Requirements](#requirements)
- [1. Install and configure](#1-install-and-configure)
- [2. Reverse proxy](#2-reverse-proxy)
- [3. Register the AppService with your homeserver](#3-register-the-appservice-with-your-homeserver)
- [4. Run the bridge](#4-run-the-bridge)
- [5. Verify](#5-verify)

## Requirements

- Python 3.11 or newer, or Docker.
- A reverse proxy (nginx in the example) in front of the bridge, if you want its ActivityPub surface public on the same domain as your homeserver (recommended; see step 2).

## 1. Install and configure

**Natively:**

```sh
./scripts/setup.sh
```

This creates a virtualenv, installs `requirements.txt`, and generates `config.yaml` from `config.example.yaml` with fresh random AppService tokens.

**In Docker:** copy `config.example.yaml` to `config.yaml` yourself and replace the two `*_replace_me` tokens (`python3 -c 'import secrets; print(secrets.token_hex(32))'` generates one).

Then edit `config.yaml`. At minimum set:

- `bridge.domain` and `bridge.public_base_url`.
- `synapse.base_url` and `synapse.server_name` (these fields are named after Synapse, but just mean "your homeserver"; see the README's "What homeservers are supported?" section).
- `synapse.admin_token` (an access token for a homeserver account with `admin: true`), unless you've explicitly turned off `bridge.use_synapse_admin_api` (see [CONFIG.md](CONFIG.md)).

`config.example.yaml` documents each of its own settings inline. It's deliberately short: everything else has a default, and [CONFIG.md](CONFIG.md) is the full reference (storage backend, logging level, federation timeouts, backfill defaults, MSC4501 behavior, and more).

## 2. Reverse proxy

`deploy/nginx.conf.example` shows a config for exposing this bridge's ActivityPub surface on the same public domain as your homeserver. Recommended, since it's what makes a user's Matrix ID and fediverse handle the exact same string (`@alice:example.org` == `@alice@example.org`). Copy it in, adjust the upstream ports, `server_name`, and certificate paths to match your `config.yaml` and existing setup, then `nginx -t && systemctl reload nginx`.

AppService transaction delivery (Synapse -> bridge) isn't proxied here: by default, `config.yaml`'s `bridge.internal_base_url` has Synapse deliver those directly to the bridge's own `listen_host`/`listen_port`, bypassing the public internet entirely. Only the federation-facing surface needs to be public.

## 3. Register the AppService with your homeserver

**Natively:**

```sh
.venv/bin/python -m bridge.appservice config.yaml appservice-registration.yaml
```

**In Docker**, the same module as a one-off run with the entrypoint overridden (see step 4 below for the mounts this assumes):

```sh
docker run --rm --entrypoint python \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v bridge-data:/data \
  ghcr.io/haven-organization/matrix-appservice-activitypub:latest \
  -m bridge.appservice /config/config.yaml /data/appservice-registration.yaml
```

Add the resulting file's path to your homeserver's own application-service registration config (`app_service_config_files` in `homeserver.yaml`, if you're running Synapse), then restart it.

## 4. Run the bridge

**Natively:**

```sh
.venv/bin/python main.py
```

Or install `deploy/matrix-appservice-activitypub.service` to run it under systemd (see that file for the expected user/paths).

**In Docker:** the `Dockerfile` runs the exact same code, so every feature and both storage backends work identically; it's just a different way to run it. Two settings in `config.yaml` need a container-specific value first, both confirmed live:

- `bridge.listen_host` must be `0.0.0.0`, not the native default `127.0.0.1`. Otherwise nothing outside the container's own network namespace can reach it, even with the port published.
- If `storage.backend` is `sqlite`, `storage.data_dir` must be `/data` (not the native default `./data`), since that's the volume the image actually creates and can write to as its non-root user.

If Synapse can't reach the bridge at its default address (for example, Synapse is also containerized on a shared Docker network), also set `bridge.internal_base_url` (see [CONFIG.md](CONFIG.md)) to wherever it can, e.g. `http://bridge:8090`, before running step 3, since it's written into the registration file.

The container runs as a fixed, non-root `bridge` user, UID/GID `999:999`. A named volume (like `bridge-data` in the examples above) is chowned automatically and needs nothing extra. A bind mount instead of a named volume needs the host directory owned by that same UID/GID first (`chown -R 999:999 ./data`); permission alone (even `chmod 777`) isn't enough on a host running SELinux in enforcing mode (common on Fedora/RHEL/CentOS/Rocky), so add the `:z` suffix to that mount instead (`-v ./data:/data:z`, or the same in compose) to relabel it for container access.

A prebuilt multi-arch image (amd64/arm64) is published to `ghcr.io/haven-organization/matrix-appservice-activitypub` on every push to `main`. Simplest form, standalone:

```sh
docker run -d --name matrix-appservice-activitypub \
  -p 8090:8090 \
  -v $(pwd)/config.yaml:/config/config.yaml:ro \
  -v bridge-data:/data \
  ghcr.io/haven-organization/matrix-appservice-activitypub:latest
```

Or copy `docker-compose.example.yml` to `docker-compose.yml` and adjust it to your setup (it bundles a Postgres container and wires in an existing containerized Synapse via an external network; see the file's own comments for both), then:

```sh
docker compose up -d
```

To build from your own checkout instead of pulling the published image (e.g. to test an unreleased change), `docker build -t matrix-appservice-activitypub .` and use that tag in place of the `ghcr.io/...` one above, or swap `docker-compose.example.yml`'s `image:` line for `build: .`.

## 5. Verify

```sh
.venv/bin/python scripts/simulate_remote_follow.py --username <a-linked-profile>
```
