
<img width="1254" height="1254" alt="custom-logo" src="https://github.com/user-attachments/assets/732625dd-1f53-4343-8af7-d4588662cf4b" />
# Plex EPG -> XMLTV Docker

Exports Plex guide data to standard XMLTV with a Web UI, channel map, and M3U-to-guide lineup matching.

## Guide sources

The setup wizard supports two sources:

- **Existing Plex DVR** — uses the guide channels already configured on your Plex Media Server.
- **Plex EPG Zipcode** — finds and selects a Plex EPG lineup using a US ZIP code.

New configurations use `EPG_SOURCE=dvr` or `EPG_SOURCE=zipcode`.

## Portainer / Docker Compose

```yaml
services:
  plex-epg:
    image: lamb0985/plex-epg:latest
    container_name: plex-epg
    restart: unless-stopped

    environment:
      PUID: "1000"
      PGID: "100"

    ports:
      - "7538:8080"

    volumes:
      - <host_path_to_config>:/config
      - <host_path_to_output>:/output
```

`8080` is the internal container port. The left side of `7538:8080` is the host port and can be changed.

## PUID / PGID

`PUID` and `PGID` are Docker environment variables. They control ownership of `/config` and `/output` and are not read from `/config/.env`.

The entrypoint prepares the mounted directories as root, then runs the application as the requested UID/GID.

## First start / Setup Wizard

The image does not create `.env.example`.

If `/config/.env` is missing, the container still starts the Web UI instead of exiting or idling. Open the published Web UI address and the **Setup Wizard** opens automatically.

The wizard configures:

- Plex server address
- Plex token
- guide source: **Existing Plex DVR** or **Plex EPG Zipcode**
- M3U local path or HTTP(S) URL
- ZIP code and lineup selection when using **Plex EPG Zipcode**
- guide days
- XMLTV ID mode
- refresh interval

The wizard validates the selected source and M3U before saving `/config/.env`, writes `/config/lineup_matches.csv`, and starts the normal guide refresh scheduler. A container restart is not required after completing the Web UI wizard.

The command-line `setup_lineup.py` remains available as an alternative for users who prefer `docker exec`.

## Configuration reference

The normal setup wizard writes the required values. A manual configuration can use:

```env
PLEX_SERVER=http://YOUR_PLEX_SERVER:32400
PLEX_TOKEN=PUT_YOUR_PLEX_TOKEN_HERE
EPG_SOURCE=dvr
EPG_M3U_PATH=http://YOUR_M3U_SERVER/playlist.m3u
EPG_DAYS=3
EPG_ID_MODE=callsign
REFRESH_INTERVAL=21600
```

For **Plex EPG Zipcode**, setup also writes values such as:

```env
EPG_SOURCE=zipcode
EPG_COUNTRY=US
EPG_POSTAL_CODE=12345
EPG_LINEUP_ID=YOUR_SELECTED_LINEUP_ID
EPG_M3U_PATH=http://YOUR_M3U_SERVER/playlist.m3u
```

Built-in paths are:

```text
/config/channel_map.csv
/config/lineup_matches.csv
/output/plex-epg.xml
```

`/config/.env` is authoritative for Plex EPG settings.

## M3U lineup matching

`EPG_M3U_PATH` is retained for both guide sources.

`/config/lineup_matches.csv` is generated for both:

- **Existing Plex DVR** — compares the M3U against the current channel list from the configured Plex DVR.
- **Plex EPG Zipcode** — compares the M3U against the selected ZIP-code lineup.

The setup wizard writes the initial file. Normal EPG refreshes rebuild it so it does not remain stale when the M3U or guide lineup changes.

The matcher uses channel number and callsign. It also treats common `HD` and `DT` suffixes as callsign variants, for example `BABY1HD` and `BABY1`.

## EPG_ID_MODE

The XMLTV channel ID should match the `tvg-id` used by the target M3U application.

| Mode | XMLTV channel ID | Example |
|---|---|---|
| `callsign` | Plex callsign | `WGHP` |
| `vcn` | Plex channel number | `10` |
| `plex` | Plex internal channel ID | Plex-specific |

## Output and URLs

With the example mapping `7538:8080`:

```text
Web UI:      http://YOUR_DOCKER_HOST:7538/
XMLTV:       http://YOUR_DOCKER_HOST:7538/plex-epg.xml
Channel map: http://YOUR_DOCKER_HOST:7538/channel_map.csv
Matches:     http://YOUR_DOCKER_HOST:7538/lineup_matches.csv
```

The container cannot know Docker's published host port before a request arrives. Startup therefore does not print a fake `:8080` host URL. When the Web UI is first opened, the actual requested host/port is logged.

## Web UI

The Dracula-themed dashboard shows:

- guide source: **Existing Plex DVR** or **Plex EPG Zipcode**
- XMLTV status, programme count, channel count, guide days, and refresh interval
- exported channel number, callsign, name, XMLTV ID, Plex ID, and dark network icons
- lineup matching and review status
- last and next refresh information
- a live **EPG processing log** while the exporter runs

Actions:

- **Setup Wizard** — create or change the Plex/guide configuration
- **Refresh EPG now**
- **Recheck lineup**

Routine HTTP polling such as `/api/status` is not printed to the Docker log. EPG processing output is streamed to both the Web UI Activity log and `docker logs -f plex-epg`.

The dashboard never displays a stored Plex token. The Setup Wizard is the only Web UI feature that writes `/config/.env`.

## Refresh behavior

The guide refreshes at startup and every `REFRESH_INTERVAL` seconds.

If a refresh fails, the last successful XMLTV file is kept.


## Setup Wizard security

The Plex token is submitted to the container when the Setup Wizard is saved or tested, but a stored token is never returned to the browser. If the Web UI is exposed outside a trusted local network, place it behind HTTPS and appropriate access controls.


## Security

The Web UI does not include built-in authentication. Keep it on a trusted network or place it behind HTTPS and appropriate access controls. The Plex token is submitted when testing or saving the Setup Wizard, but a stored token is never returned by the Web UI. See [`SECURITY.md`](SECURITY.md) for details.


## License

MIT. See [`LICENSE`](LICENSE).
