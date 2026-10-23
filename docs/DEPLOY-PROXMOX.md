# Deploying musiclib on a Proxmox cluster (Docker VM/LXC)

Use this when Docker runs inside a VM or LXC on Proxmox. The steps run on the
Docker host (SSH into the VM/LXC), not on the Proxmox node itself.

## 1. Make the music visible to the Docker host

**If your music is on a separate NAS box** — mount the share on the Docker host.

SMB/CIFS (works with any NAS):

```bash
mkdir -p /mnt/nas-music
sudo mount -t cifs //NAS-IP/music /mnt/nas-music \
    -o credentials=/root/.smbcred,uid=1000,gid=1000,iocharset=utf8
```

`/root/.smbcred` (chmod 600):

```
username=your-nas-user
password=your-nas-password
```

NFS (better if your NAS supports it — cleaner permissions):

```bash
sudo mkdir -p /mnt/nas-music
sudo mount -t nfs NAS-IP:/volume1/music /mnt/nas-music
```

Make it permanent by adding the matching line to `/etc/fstab`, then
`sudo mount -a` to verify.

**If the music lives on cluster storage already** (a VM disk, an LXC bind mount,
or a shared folder passed through to the Docker host) — just use that path
directly in step 2.

## 2. Run the container

On the Docker host:

```bash
git clone https://github.com/fatannasty/musiclib.git
cd musiclib
MUSIC_DIR=/mnt/nas-music DATA_DIR=/opt/musiclib PUID=1000 PGID=1000 \
    docker compose up -d --build
```

- `MUSIC_DIR` — path to the music as seen by the Docker host
- `DATA_DIR` — where the catalog DB and config live (keep this on the Docker
  host's local disk, not the network share, for speed)
- `PUID`/`PGID` — must match the uid/gid that owns the mounted music (the `uid=`
  from the CIFS mount, or the file owner on NFS)

## 3. Use it

From any PC on the network (including Windows):

```
http://<docker-host-ip>:8000
```

First click: **Scan** in the Overview tab.

## Portainer / Dockge users

If you manage containers with Portainer or Dockge on the cluster:

- Portainer: Stacks → Add stack → **Repository** → URL
  `https://github.com/fatannasty/musiclib.git`, compose path `/`, then set the
  `MUSIC_DIR`/`DATA_DIR`/`PUID`/`PGID` environment variables in the stack UI
- Dockge: clone the repo and point a new stack at the `docker-compose.yml`

## Updating

```bash
cd musiclib
git pull
docker compose up -d --build
```

## Troubleshooting

- **Files not writable / permission denied**: the container runs as
  `PUID`/`PGID`; the mounted share must be writable by that uid. For CIFS check
  the `uid=`/`gid=` mount options; for NFS check ownership on the NAS.
- **Empty library after scan**: `MUSIC_DIR` path inside the Docker host is wrong
  or the mount is missing (check `ls /mnt/nas-music` first).
- **Slow scans over SMB**: normal for large libraries on first scan; subsequent
  scans are incremental and only read changed files.
