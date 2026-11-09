# Installing musiclib on a Synology NAS (step by step)

Works on DSM 7.2+ with **Container Manager** (DSM 7.0/7.1: the **Docker** package).
The NAS builds and runs the container; every PC on the network just uses a browser.

**Requirements**
- An x86_64 Synology model (most "Plus" models; step 4 checks this)
- ~2 GB free RAM during the build
- Your music in a shared folder
- Container Manager installed (DSM **Package Center** → search "Container Manager" → Install)

Each step ends with a **Check** line. If a check fails, stop and see
[Troubleshooting](#troubleshooting) before continuing.

## 1. Create a GitHub token (on any PC)

The repo is private, so the NAS needs a token to download it.

1. github.com → avatar → **Settings** → **Developer settings** (bottom of left menu)
   → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**
2. Name: `synology-deploy`. Expiration: 90 days.
3. **Repository access** → *Only select repositories* → `musiclib`
4. **Repository permissions** → **Contents: Read-only**
5. **Generate token** and copy it (starts with `github_pat_`). It is shown only once.

## 2. Enable SSH on the Synology

DSM → **Control Panel** → **Terminal & SNMP** → tick **Enable SSH service** → **Apply**.

## 3. Connect from your PC

```bash
ssh YOUR_ADMIN_USER@NAS_IP
```

Use your DSM admin username and password. Everything below runs in this SSH session.
To get root once, run `sudo -i` (enter your password); then you can skip `sudo` below.

## 4. Find your values

```bash
id
ls /volume1
uname -m
```

Write down:
- `uid=` number → your **PUID** (often 1026)
- `gid=` number → your **PGID** (often 100)
- your music share name from `ls /volume1` (e.g. `music`)

**Check:** `uname -m` must print `x86_64`. If not, see Troubleshooting.

## 5. Download the app

```bash
sudo mkdir -p /volume1/docker
cd /volume1/docker
read -s -p "GitHub token: " GH_TOKEN; echo
sudo curl -4 -fL -H "Authorization: Bearer $GH_TOKEN" \
    https://api.github.com/repos/fatannasty/musiclib/tarball/main \
    -o musiclib.tar.gz
unset GH_TOKEN
sudo mkdir musiclib-next
sudo tar xzf musiclib.tar.gz -C musiclib-next --strip-components=1
ls -l musiclib-next/Dockerfile musiclib-next/docker-compose.yml
```

Enter the real token at the hidden prompt. `curl -f` stops on HTTP errors instead
of saving an error page as a tarball. The two files listed at the end must exist.
If `musiclib-next` already exists, choose a new empty folder name and use it in
the commands below; do not extract over an existing release.

## 6. Create the settings file

Edit the three values to match step 4, then run it as-is:

```bash
sudo mkdir -p /volume1/docker/musiclib-data
cd /volume1/docker/musiclib-next
sudo tee .env > /dev/null <<'EOT'
MUSIC_DIR=/volume1/music
DATA_DIR=/volume1/docker/musiclib-data
PUID=1026
PGID=100
EOT
cat .env
```

`MUSIC_DIR` must be a real folder (`ls /volume1/music` should list your files).

## 7. Build and start

```bash
sudo docker compose up -d --build
```

Older DSM that says `docker: 'compose' is not a command`: use `sudo docker-compose up -d --build`.

The first build takes 5–15 minutes. Wait for it to return to the prompt.

**Check:**
```bash
sudo docker ps
sudo docker logs musiclib
```
`docker ps` should list `musiclib` with status `Up`; the logs should end with a line
like `Uvicorn running on http://0.0.0.0:8000`.

You can also watch it in DSM → **Container Manager** → **Container**.

## 8. Open the dashboard

In a browser on any PC on your network: `http://NAS_IP:8000`

Open the dashboard at `http://NAS_IP:8000`. Click **Scan** on the Overview tab.
Done.

## Updating later (staged and reversible)

Download and extract the release into a separate folder first. This leaves the
current installation available for rollback.

```bash
cd /volume1/docker
read -s -p "GitHub token: " GH_TOKEN; echo
sudo curl -4 -fL -H "Authorization: Bearer $GH_TOKEN" \
    https://api.github.com/repos/fatannasty/musiclib/tarball/main \
    -o musiclib-new.tar.gz
unset GH_TOKEN
sudo mkdir musiclib-next
sudo tar xzf musiclib-new.tar.gz -C musiclib-next --strip-components=1
sudo cp musiclib/.env musiclib-next/.env
ls -l musiclib-next/Dockerfile musiclib-next/docker-compose.yml
```

Verify both files exist. Stop the current app, then start the staged replacement
using the same Compose project name and the existing `.env` values:

```bash
sudo docker compose -p musiclib -f /volume1/docker/musiclib/docker-compose.yml down
sudo docker compose -p musiclib -f /volume1/docker/musiclib-next/docker-compose.yml \
    up -d --build
sudo docker ps
sudo docker logs musiclib
```

Open the dashboard at the configured NAS port. Keep the old source folder until
the dashboard and library data are confirmed. If startup fails, stop the staged
service and start Compose from the old folder again. Do not remove the data
directory; it contains the database and settings.

## Troubleshooting

| Symptom | Fix |
|---|---|
| curl prints 404/401, or the tarball is tiny | Token wrong, expired, or not granted `Contents: Read-only` on `musiclib`. Make a new one. `cat musiclib.tar.gz` shows the error text. |
| `tar` says "not in gzip format" | Same cause: the download was an error message, not the app. |
| `permission denied` running docker | Put `sudo` in front, or use `sudo -i`. |
| `docker compose` not found | Use `docker-compose` (older DSM). |
| `uname -m` is not `x86_64` (e.g. armv7/aarch64) | Image is only tested on x86_64; run on the Proxmox Docker host instead. |
| Build fails with out-of-memory or `killed` | Close other containers/apps, then re-run step 7. |
| Build fails fetching packages | NAS has no internet/DNS; check DSM network settings and retry. |
| Container keeps restarting | `sudo docker logs musiclib` and read the last lines. |
| Page not loading | Check `sudo docker ps` shows `Up`, and that no firewall blocks port 8000. |
| Port 8000 already used | Edit `docker-compose.yml` ports to `"8001:8000"` and open `:8001`. |
| Permission denied when trashing/renaming | `PUID`/`PGID` in `.env` don't match the share owner; re-check `id`, then `sudo docker compose up -d`. |
| Nothing scans | `ls` your `MUSIC_DIR`; share names differ per setup. |

Token hygiene: the token stays in your shell history. Revoke it in GitHub settings after
setup, or generate a fresh one for each update. Run `history -c` to clear the session.
