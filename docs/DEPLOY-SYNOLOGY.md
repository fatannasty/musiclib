# Installing musiclib on a Synology NAS (step by step)

Works on DSM with the **Container Manager** package (called **Docker** on DSM 7.0
and older). The NAS builds and runs the container; every PC on the network just
uses a browser.

Requirements: an x86_64 Synology model (most "Plus" models — check with
`uname -m` in step 4), ~2 GB free RAM during the build, and your music on a
shared folder.

## 1. Create a GitHub access token (2 minutes, on any PC)

The repo is private, so the NAS needs a token to download it:

1. On github.com, click your avatar → **Settings** → **Developer settings**
   (bottom of the left menu) → **Personal access tokens → Fine-grained tokens**
2. **Generate new token**: name it `synology-deploy`, set an expiration
   (90 days is fine)
3. **Repository access** → *Only select repositories* → `musiclib`
4. **Permissions → Repository permissions → Contents: Read-only**
5. Generate and copy the token (starts with `github_pat_...`) — shown only once

## 2. Enable SSH on the Synology

**Control Panel → Terminal & SNMP → Enable SSH service → Apply**

## 3. Connect from your PC

From PowerShell (Windows) or Terminal (Mac):

```bash
ssh YOUR_ADMIN_USER@NAS_IP
```

Enter your DSM password when asked. All commands below run here.

## 4. Find your values

```bash
id          # note uid= (usually 1026) and gid= (usually 100)
ls /volume1 # find your music share, e.g. "music"
uname -m    # must say x86_64
```

## 5. Download the app

```bash
sudo mkdir -p /volume1/docker && cd /volume1/docker
sudo curl -L -H "Authorization: Bearer YOUR_TOKEN" \
    https://api.github.com/repos/fatannasty/musiclib/tarball/main \
    -o musiclib.tar.gz
sudo tar xzf musiclib.tar.gz
sudo mv fatannasty-musiclib-* musiclib
cd musiclib
```

## 6. Create the settings file

This keeps your DB/config separate from the app folder (survives updates):

```bash
sudo tee .env > /dev/null <<'EOF'
MUSIC_DIR=/volume1/music
DATA_DIR=/volume1/docker/musiclib-data
PUID=1026
PGID=100
EOF
```

Adjust: `MUSIC_DIR` = your music share path; `PUID`/`PGID` = the uid/gid from
step 4. Then create the data folder:

```bash
sudo mkdir -p /volume1/docker/musiclib-data
```

## 7. Build and start

```bash
sudo docker compose up -d --build
```

- First build takes 5–15 minutes on NAS hardware
- If `docker compose` says "not found", your DSM is older — use
  `sudo docker-compose up -d --build` instead

## 8. Open the dashboard

From any PC on the network:

```
http://NAS_IP:8000
```

Click **Scan** on the Overview tab. Done.

## Updating later

```bash
cd /volume1/docker
sudo curl -L -H "Authorization: Bearer YOUR_TOKEN" \
    https://api.github.com/repos/fatannasty/musiclib/tarball/main \
    -o musiclib-new.tar.gz
sudo tar xzf musiclib-new.tar.gz
sudo cp musiclib/.env fatannasty-musiclib-*/.env
sudo rm -rf musiclib && sudo mv fatannasty-musiclib-* musiclib
cd musiclib
sudo docker compose up -d --build
```

Your library, database, and config are untouched (they live in
`musiclib-data`, not the app folder).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Permission denied when trashing/renaming files | `PUID`/`PGID` in `.env` don't match the share owner — re-check `id` output |
| `docker compose` not found | Older DSM: use `docker-compose` |
| Build fails, `uname -m` shows armv7 | Model too old for this image; run it on your Proxmox Docker host instead |
| Port 8000 already used | Edit `docker-compose.yml` ports line, e.g. `"8001:8000"` |
| Nothing scans | Check `ls` of your `MUSIC_DIR` path — share names differ per setup |

Token hygiene: it lives in your shell history — set a short expiry or revoke it
in GitHub settings after setup (only needed again for updates; generate a fresh
one each time if you prefer).
