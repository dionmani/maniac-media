# Installing Maniac Media on Unraid

This guide installs Maniac Media from the template in [`unraid/maniac-media.xml`](../unraid/maniac-media.xml)
using **Docker → Add Container → User templates**. Importing this template does not create a Community
Applications listing; it is a personal template stored on your Unraid flash drive.

| Setting | Default |
|---|---|
| Image | `ghcr.io/dionmani/maniac-media:latest` (linux/amd64) |
| Web UI | `http://<server-ip>:8193` (host 8193 → container 8000) |
| Appdata | `/mnt/user/appdata/maniac-media` → `/config` |
| Media + downloads | `/mnt/user/data` → `/data` |
| Master key | none – **required**, you generate it |
| PUID / PGID / UMASK | `99` / `100` / `002` |

## 1. Prerequisites

- Unraid with Docker enabled and the array (or a pool) started; an amd64 host.
- An unused host port (default 8193).
- An appdata share and a data share (existing or new).

## 2. Prepare folders

Default layout under `/mnt/user/data`:

```
/mnt/user/data/media/movies
/mnt/user/data/media/tv
/mnt/user/data/downloads/complete
```

Create any that are missing from the Unraid terminal:

```bash
mkdir -p /mnt/user/appdata/maniac-media \
         /mnt/user/data/media/movies /mnt/user/data/media/tv \
         /mnt/user/data/downloads/complete
```

If you already have a different layout, point the **Data** path at your share and adjust the folder
settings later in the app. The completed-downloads folder **must exist before the container starts**,
otherwise the folder watcher does not start.

**Permissions.** The app runs as `PUID:PGID` (default `99:100`, nobody:users). These directories must be
writable by that user. The container only fixes ownership of `/config`, not `/data`. Don't run blanket
recursive `chown`/`chmod` against an existing media library; fix only what you need.

## 3. Generate the master key

```bash
openssl rand -base64 32
```

Save the output somewhere safe (a password manager). You paste it into the template and need the **same
key** whenever you recreate the container. Changing it is not a supported rotation and makes stored
indexer / download-client credentials unreadable. Treat the template and flash backups as sensitive
because the key lives in them.

## 4. Install the template

Unraid reads personal templates from `/boot/config/plugins/dockerMan/templates-user/`. Use either method
(open the Unraid terminal, or SSH in).

**A. Copy from a local checkout** (e.g. via an SMB share or `scp`):

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
cp maniac-media.xml /boot/config/plugins/dockerMan/templates-user/my-maniac-media.xml
```

**B. Download from GitHub** (requires the repository and file to be publicly reachable):

```bash
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl -fsSL -o /tmp/my-maniac-media.xml \
  https://raw.githubusercontent.com/dionmani/maniac-media/main/unraid/maniac-media.xml
test -e /boot/config/plugins/dockerMan/templates-user/my-maniac-media.xml \
  && echo "Already exists - not overwriting" \
  || mv /tmp/my-maniac-media.xml /boot/config/plugins/dockerMan/templates-user/my-maniac-media.xml
```

`-f` makes the download fail on HTTP errors; the check avoids overwriting a template you already
configured (existing installs keep their port, paths and key).

## 5. Create the container

1. Refresh the Unraid web page.
2. Open **Docker → Add Container**.
3. In the **Template** drop-down, under **[ User templates ]**, select **maniac-media**.
4. Review the fields: WebUI port, Config path, Data path, and paste your key into **Master key**.
   Toggle **Advanced View** for PUID/PGID/UMASK and first-run folder variables.
5. Click **Apply**. Unraid pulls the image and starts the container.
6. From the Docker tab click the container icon → **WebUI**, or browse to `http://<server-ip>:8193`
   (or your chosen host port).

Health check: `http://<server-ip>:8193/api/health`.

## 6. First-run setup in the app

1. Create the administrator account on first visit. Do this on a trusted LAN; don't expose the app
   directly to the internet.
2. **Settings → Media management**: confirm the movie, TV and completed-downloads folders.
   The `MANIAC_*_DIR` variables only seed these on first run; later changes belong here.
3. Configure metadata providers, indexers and download clients under **Settings**.

### Download clients

- On the default bridge network use the Unraid server's LAN IP and the client's published port.
  `localhost` inside Maniac Media is the Maniac Media container itself.
- Best practice: give your download client the same `/data` mapping (`/mnt/user/data → /data`), so both
  apps see identical paths.
- If your client reports e.g. `/downloads/...`, add a remote path mapping in the download-client
  settings: **Client path** `/downloads` → **Local path** `/data/downloads`, provided both refer to the
  same host directory.
- Hardlinks need source and destination on the same filesystem/mount; otherwise imports fall back to copying.

## 7. Maintenance

- Enable **Autostart** for the container in the Docker tab if desired.
- Updates and recreation: keep the same `/config` path and the same master key.
- Back up `/mnt/user/appdata/maniac-media` and your template file. For SQLite, use the app's backup
  feature or stop the container before copying database files.

## Troubleshooting

| Problem | Fix |
|---|---|
| Template not listed | Check the path and name exactly (`/boot/config/plugins/dockerMan/templates-user/*.xml`), that the XML is valid, then refresh the Add Container page. |
| Image pull denied / not found | Check the image spelling `ghcr.io/dionmani/maniac-media:latest`, that the image is published and its package is public. |
| Port already in use | Change only the **host** port in the template; the container port stays 8000. |
| Permission denied | Verify the host paths and that `PUID`/`PGID` can write to them. |
| Download folder missing / watcher not running | Create `downloads/complete`, verify the path in Settings → Media management, restart the container. |
| Imports can't find downloads | Compare the path your client reports with the path visible inside the container; add a remote path mapping. |
| Files copied instead of hardlinked | Source and destination must be on the same filesystem and writable; a shared parent path alone doesn't guarantee hardlinks. |
| Folder variables ignored | They only seed a new install; edit the saved folders in the app. |
| Settings/data "disappeared" | You probably mounted a different (empty) appdata path, which creates a fresh install. |
