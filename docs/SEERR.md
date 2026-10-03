# Connecting Seerr (or Overseerr / Jellyseerr)

Maniac Media can stand in for Radarr **and** Sonarr in Seerr. Seerr sees two ordinary servers and
needs no plugin or patch. Requests land in Maniac Media, searches start automatically, download
progress shows in Seerr, and titles turn *Available* once they're imported.

Under the hood these are Radarr/Sonarr-compatible APIs at `/api/compat/radarr` and
`/api/compat/sonarr` (see `backend/api/compat/`).

## Before you start

- Seerr must be able to reach Maniac Media over the network. If both run in Docker, use the host's
  LAN IP or a shared Docker network; `localhost` inside the Seerr container is Seerr itself.
- Know the port Maniac Media listens on (default **8000**).

## 1. Create an API key

In Maniac Media: **Settings → Account** (or **Users**), scroll to **API keys**, click
**Create API key**, pick the preset **Request manager (Seerr/Overseerr)**, name it and generate it.

- Name the key **`Seerr`**: history entries credit requests to the key's name ("Requested by alice via Seerr").
- The preset allows the key to be sent as `?apikey=`. Seerr only ever sends it that way, so a key
  without that option fails Seerr's connection test with 401.
- The key is shown once. Copy it now. One key works for both the Radarr and the Sonarr server.

## 2. Add Maniac Media as a Radarr server

In Seerr: **Settings → Services → Add Radarr Server**

| Field | Value |
|---|---|
| Default Server | ✔ |
| 4K Server | leave off (Maniac Media keeps one file per movie) |
| Server Name | Maniac Media (Movies) |
| Hostname or IP Address | Maniac Media's host |
| Port | `8000` |
| Use SSL | only if Maniac Media is served over HTTPS |
| API Key | the key from step 1 |
| URL Base | `/api/compat/radarr` |

Click **Test**. Seerr should then show Maniac Media's quality profiles and the movie root folder.
Pick them, then:

- **Minimum Availability**: any; Maniac Media applies its own rules.
- **Enable Scan**: ✔ (this is how titles become *Available* in Seerr)
- **Enable Automatic Search**: ✔
- **Tag Requests**: ✔ if you want to see who requested what (step 4)
- **External URL**: Maniac Media's address as you open it in a browser (e.g. `http://192.168.1.10:8000`).
  This makes Seerr's *Open in Radarr* button open the movie in Maniac Media.

## 3. Add Maniac Media as a Sonarr server

**Settings → Services → Add Sonarr Server**, with the same values except:

| Field | Value |
|---|---|
| Server Name | Maniac Media (TV) |
| URL Base | `/api/compat/sonarr` |

After **Test**, pick a quality profile and the TV root folder (also for anime, unless you want a
different profile there). There is no *Language Profile* field: Maniac Media reports itself as
Sonarr v4, which has none. Turn on **Season Folders**, **Enable Scan** and **Enable Automatic Search**,
and optionally **Tag Requests** and **External URL** as above.

## 4. See who requested what (optional)

With **Tag Requests** on, Seerr attaches a per-user tag (`<seerr user id>-<name>`) to everything it
sends. Maniac Media keeps those tags and:

- shows a **Requested by …** chip on the movie or show page;
- adds a **requested** entry to the title's history, e.g. *Requested by alice via Seerr*, or for TV
  *Requested by alice via Seerr (season 2)*. When someone asks for more seasons of a show,
  the history records those seasons too.

Only requests made after the option is switched on are attributed.

## 5. Switch over from real Radarr/Sonarr

1. Add the Maniac Media servers and make them the **default** servers.
2. Disable or delete the old Radarr/Sonarr entries in Seerr.
3. In Seerr, run **Settings → Jobs & Cache → Radarr Scan** and **Sonarr Scan** once, so existing
   titles are matched to Maniac Media straight away.

## Behaviour to know about

- **Deleting**: when an admin removes a title in Seerr, Seerr always asks for its files to be
  deleted. By default Maniac Media only removes the library entry and leaves the files. To let
  Seerr delete files, turn on **Settings → Media management → Let Seerr delete files**. Even then,
  files go to the recycle bin.
- **Root folders**: Seerr sees exactly one movie folder and one TV folder (the ones in
  Settings → Media management); requests that name another path are rejected.
- **TV shows are matched by TVDB id.** A show you added to Maniac Media from TVmaze/TMDB without a
  TVDB id is matched by its other ids the first time Seerr looks it up, and the TVDB id is saved.
  Until then it doesn't appear in Seerr's scan.
- **Metadata**: movie requests still go through if metadata can't be fetched (Seerr's title is
  used). TV requests can't, because Seerr builds the season list from Maniac Media's answer. Seerr
  then reports *Series not found* and the request can be retried later. TVmaze needs no API key,
  so this is rare.
- **Searches**: a TV request searches each requested season, trying a season pack first and then
  single episodes.

## Troubleshooting

Seerr's **Test** button only says it failed. To see the actual error, call the same URL Seerr uses,
from the machine (or container) Seerr runs on:

```bash
curl "http://<host>:8000/api/compat/radarr/api/v3/system/status?apikey=<your key>"
```

| Result | Cause / fix |
|---|---|
| JSON with `"appName": "Maniac Media"` | The connection is fine. Re-check the values typed into Seerr. |
| `401 {"detail": "Invalid API key"}` (or `revoked` / `expired`) | Wrong or retired key. Create a new one from the preset. |
| `401 {"detail": "API key not allowed in query parameter"}` | The key was created without the query-parameter option. Create one from the **Request manager** preset. |
| `404` | Wrong URL Base. It must be exactly `/api/compat/radarr` or `/api/compat/sonarr`, with no trailing `/api/v3`. Behind a reverse proxy on a sub-path, put the proxy path first (e.g. `/mm/api/compat/radarr`). |
| Connection refused / timeout | Seerr can't reach that host/port. From Docker, use the LAN IP rather than `localhost`. |

Other symptoms:

- **Request fails** (*Failed to add movie/series* in Seerr's log, under `[Radarr]` / `[Sonarr]`):
  check Maniac Media's **System → Audit log**. The usual cause is a quality profile or root folder
  that changed after Seerr was set up. Open the server in Seerr, click **Test** again and re-save.
- **Request stays *Processing* after the download**: make sure **Enable Scan** is on for the server
  and that the file was imported in Maniac Media (**Activity → History**).
