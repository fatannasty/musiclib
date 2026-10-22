# Deploying musiclib on a Docker-capable NAS

Run musiclib directly on your NAS so the dashboard is always available from any
device on your network (Windows, macOS, phone) with nothing to install on the
clients.

## What you get

- Dashboard on `http://<nas-ip>:8000` from any PC or phone on the LAN
- The catalog database lives on the NAS and survives container restarts/updates
- ffmpeg + chromaprint are baked into the image, so deep corruption checks and
  audio-fingerprint duplicate detection work out of the box
- Deleted files go to `<music-share>/.musiclib/trash/` on the NAS itself

## Setup

1. SSH into the NAS (or use the Container Station / Container Manager UI to
   run the commands below in a shell on the NAS).

2. Clone the repo somewhere persistent on the NAS:

   ```bash
   git clone https://github.com/fatannasty/musiclib.git
   cd musiclib
   ```

3. Point it at your music share. Either export an environment variable:

   ```bash
   export MUSIC_DIR=/volume1/music     # Synology example
   # QNAP: /share/Multimedia
   # UGREEN: /volume1/music ... adjust to your NAS
   export DATA_DIR=/volume1/docker/musiclib   # where the DB and config live
   ```

   ...or edit `docker-compose.yml` and set the two volume paths directly.

4. Start it:

   ```bash
   docker compose up -d --build
   ```

5. Open `http://<nas-ip>:8000` from any machine on the network.

## Permissions (recommended)

NAS shares are often owned by a specific user. Run the container as that user so
it can write (trash, renames, folder art) to the share:

```bash
PUID=1026 PGID=100 docker compose up -d --build
```

Find the values for your NAS user with `id` on the NAS. If you skip this the
container runs as root (works, but files it writes will be root-owned).

## Configuration

The config is created automatically on first start at `$DATA_DIR/config.toml`
(with `library = /music`). Edit it there to add your SABnzbd and newznab
indexers, then restart the container:

```bash
docker compose restart
```

## Updating

Since the app repo auto-syncs from the dev machines to GitHub, updating the NAS
deployment is:

```bash
cd musiclib
git pull
docker compose up -d --build
```

Your database, config, and library are untouched (they live in volumes, not the
image).

## Notes

- The container stays running across NAS reboots (`restart: unless-stopped`)
- To run the CLI against the NAS library (scan, doctor, etc.) instead of the
  dashboard, exec into the running container:

  ```bash
  docker exec -it musiclib musiclib stats --config /data/config.toml
  docker exec -it musiclib musiclib doctor --config /data/config.toml
  ```
- If SABnzbd also runs on the NAS, use its container URL in the config, e.g.
  `http://sabnzbd:8080` if they share a docker network, or the NAS IP.
- macOS Docker Desktop displays bind-mount ownership oddly; on a real Linux NAS
  the PUID/PGID ownership behaves normally.
