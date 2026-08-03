<div align="center">
  <img src="frontend/public/scrob.png" alt="Scrob Logo" width="120" />
  <h1>Scrob</h1>
  <p>Open-source, self-hosted media tracking - your personal Letterboxd + Trakt.</p>

  [![GitHub Stars](https://img.shields.io/github/stars/ellite/scrob?style=flat-square)](https://github.com/ellite/scrob/stargazers)
  [![Docker Pulls](https://img.shields.io/docker/pulls/bellamy/scrob?style=flat-square)](https://hub.docker.com/r/bellamy/scrob)
  [![GitHub Contributors](https://img.shields.io/github/contributors/ellite/scrob?style=flat-square)](https://github.com/ellite/scrob/graphs/contributors)
  [![GitHub Sponsors](https://img.shields.io/github/sponsors/ellite?style=flat-square)](https://github.com/sponsors/ellite)
  [![Latest Release](https://img.shields.io/github/v/release/ellite/scrob?style=flat-square)](https://github.com/ellite/scrob/releases/latest)
  [![Build](https://github.com/ellite/scrob/actions/workflows/release.yml/badge.svg?branch=main)](https://github.com/ellite/scrob/actions/workflows/release.yml)
</div>

---

Scrob syncs your libraries from **Jellyfin**, **Plex**, **Emby**, **Nuvio**, and **Stremio**, tracks your watch history, ratings, and personal lists, and can push watched activity back to connected providers - all from a clean, app-like web interface that installs as a PWA on any device.

## Table of Contents

- [Features](#features)
- [Screenshots](#screenshots)
- [Getting Started](#getting-started)
  - [Docker Compose](#docker-compose)
  - [Omnibus (single container)](#omnibus-single-container)
  - [Docker Run](#docker-run)
  - [First Setup](#first-setup)
  - [Updating](#updating)
- [Configuration](#configuration)
- [Nuvio Cloud Synchronization](#nuvio-cloud-synchronization)
  - [Connect Nuvio](#connect-nuvio)
  - [Synchronization Directions](#synchronization-directions)
  - [Scheduling and Limitations](#scheduling-and-limitations)
- [Trakt Synchronization](#trakt-synchronization)
- [Stremio Synchronization](#stremio-synchronization)
  - [Connect Stremio](#connect-stremio)
  - [Stremio Synchronization Directions](#stremio-synchronization-directions)
  - [Scheduling, Full Resync, and Limitations](#scheduling-full-resync-and-limitations)
- [Simkl Synchronization](#simkl-synchronization)
- [MDBList Synchronization](#mdblist-synchronization)
- [Webhooks](#webhooks-real-time-scrobbling)
  - [Jellyfin](#jellyfin)
  - [Plex](#plex)
  - [Emby](#emby)
  - [Kodi](#kodi)
- [OIDC / Single Sign-On](#oidc--single-sign-on)
- [Email Validation & SMTP](#email-validation--smtp)
- [Contributing](#contributing)
- [Contributors](#contributors)
- [Development](#development)
- [License](#license)

## Features

- **Multi-source sync**: Import libraries, watched status, and playback progress from Jellyfin, Plex, Emby, Nuvio, and Stremio.
- **Keep providers in sync**: Keep collection membership, watched status, and playback progress synchronized between media servers, Nuvio, and Stremio. Supports multiple server instances and Nuvio profiles.
- **Real-time scrobbling**: Webhooks from Jellyfin, Plex, Emby, and Kodi update your watch state as you play - no manual sync needed.
- **Manual scrobble**: Start a watching session directly from any movie or episode page. Pause, resume, stop, or mark as watched - session progress shows live on the home screen.
- **Trakt integration**: Sync your watched history, ratings, and lists from Trakt, and push Scrob activity back to Trakt automatically. Connecting live requires a Trakt VIP subscription (a recent Trakt-side restriction) - everyone else can still import via a Trakt data export, no VIP needed. See [Trakt Synchronization](#trakt-synchronization).
- **Simkl integration**: Sync your watched history and ratings from Simkl, and push Scrob activity back to Simkl automatically.
- **MDBList integration**: Pull watched history, ratings, and watchlist items from MDBList, and optionally push Scrob changes back using an MDBList API key.
- **Watch history & ratings**: Track every movie and episode you've watched, including multiple plays with individual timestamps. Log plays manually with a custom date, or remove individual entries - all from the watched button on any movie or episode page. Rate them on a 10-point scale with optional reviews.
- **Season ratings**: Rate individual seasons separately from the overall show.
- **Personal lists**: Create and curate lists of movies and shows. Mark them public to share with other users on the same instance.
- **Comments**: Leave comments on movies, shows, seasons, and episodes.
- **Social**: Follow other users and see their activity.
- **Release schedule**: Movie pages show the full release schedule - theatrical, digital, and physical dates - sourced from TMDB.
- **TMDB integration**: Rich metadata for every title - posters, backdrops, cast, crew, trailers, collections, and more.
- **Search**: Search TMDB across movies, shows, people, and collections, merged with your local library data.
- **Pick a Movie / Pick a Show**: Get a suggestion on what to watch next from your library or your streaming services based on your preferences.
- **Trending & Airing Today**: Daily trending movies and shows from TMDB, plus episodes airing today filtered to your collection.
- **Continue Watching & Next Up**: Dashboard cards showing in-progress items and the next episode to watch in each series.
- **Season & episode tracking**: Detailed season views with per-episode watched state and progress.
- **Cast & crew pages**: Full filmography for any person, linked to your library.
- **Radarr & Sonarr integration**: Add movies and shows to Radarr/Sonarr directly from the Scrob UI.
- **Plex watchlist automation**: Automatically send items from your Plex watchlist (and selected friends' watchlists) to Radarr or Sonarr.
- **Two-Factor Authentication**: TOTP-based 2FA with backup codes, managed from the settings page.
- **OIDC / SSO**: Authenticate with any OpenID Connect provider (Authelia, Authentik, Keycloak, etc.).
- **Progressive Web App**: Install Scrob on any device - Android, iOS, or desktop - for a native app feel.
- **Single container**: Frontend and backend ship as one image on one port. No separate services to manage.
- **API documentation**: Full interactive OpenAPI docs at `/docs` (Swagger UI) and `/redoc` (ReDoc), useful if you're scripting against Scrob directly.

## Screenshots

<img src="docs/screenshots/scrobss.png" alt="Scrob" width="800">

<details>
<summary>View more screenshots</summary>

**Dashboard**
<img src="docs/screenshots/scrob-dashboard-dark.png" alt="Dashboard" width="800" />

**Explore**
<img src="docs/screenshots/scrob-explore-light.png" alt="Explore" width="800" />

**Movie**
<img src="docs/screenshots/scrob-movie-light.png" alt="Movie" width="800" />

**Show**
<img src="docs/screenshots/scrob-show-dark.png" alt="Show" width="800" />

**Season**
<img src="docs/screenshots/scrob-season-dark.png" alt="Season" width="800" />

**Episode**
<img src="docs/screenshots/scrob-episode-dark.png" alt="Episode" width="800" />

**Search**
<img src="docs/screenshots/scrob-search-light.png" alt="Search" width="800" />

**History (mobile)**
<img src="docs/screenshots/scrob-history-dark-mobile.png" alt="History mobile" width="800" />

**Lists (mobile)**
<img src="docs/screenshots/scrob-lists-light-mobile.png" alt="Lists mobile" width="800" />

**Settings**
<img src="docs/screenshots/scrob-settings-dark.png" alt="Settings" width="800" />


</details>

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- A [TMDB Read Access Token](https://www.themoviedb.org/settings/api) (free) - used for metadata, search, and images

### Docker Compose

> Images are hosted on **Docker Hub** (`bellamy/scrob`). A mirror is also available on GHCR (`ghcr.io/ellite/scrob`) if you prefer.

1. Download the compose file:

```bash
curl -o docker-compose.yaml https://raw.githubusercontent.com/ellite/scrob/main/docker-compose.yaml
```

2. Edit `docker-compose.yaml` and replace the required values:

```yaml
services:
  scrob-db:
    container_name: scrob-db
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: scrob
      POSTGRES_PASSWORD: changeme        # ← change this
      POSTGRES_DB: scrob
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U scrob -d scrob"]
      interval: 5s
      timeout: 5s
      retries: 10

  scrob:
    container_name: scrob
    image: bellamy/scrob:latest
    restart: unless-stopped
    depends_on:
      scrob-db:
        condition: service_healthy
    ports:
      - "7330:7330"
    environment:
      DATABASE_URL: postgresql+asyncpg://scrob:changeme@scrob-db:5432/scrob   # ← match password above
      SECRET_KEY: changeme               # ← generate with: openssl rand -hex 32
      TZ: UTC
    volumes:
      - scrob_data:/app/backend/data

volumes:
  db_data:
  scrob_data:
```

3. Start:

```bash
docker compose up -d
```

### Omnibus (single container)

The omnibus image bundles PostgreSQL inside the container - no separate database service needed. It's the simplest way to get started, especially on platforms like Unraid or Portainer where managing multiple containers is cumbersome.

> **Image tags:** `bellamy/scrob:latest-omnibus` / `ghcr.io/ellite/scrob:latest-omnibus`

1. Download the omnibus compose file:

```bash
curl -o docker-compose.yml https://raw.githubusercontent.com/ellite/scrob/main/docker-compose.omnibus.yml
```

2. Edit it and set your `SECRET_KEY`:

```yaml
SECRET_KEY: changeme   # ← generate with: openssl rand -hex 32
```

3. Start:

```bash
docker compose up -d
```

That's it - no database container, no `DATABASE_URL` to configure. PostgreSQL is initialised automatically on first run and persisted in the `scrob_db` volume.

**Switching to an external database later:** set `DATABASE_URL` in the environment and the embedded PostgreSQL will be skipped entirely. The omnibus image behaves identically to the standard image when `DATABASE_URL` is provided.

> **Note:** The embedded PostgreSQL version is tied to the image's base OS (Debian Bookworm ships PostgreSQL 15). Major version upgrades of the bundled database require a manual data migration. If you anticipate needing to control the database version independently, use the standard two-container setup instead.

### Docker Run

**Standard image** (requires a separate PostgreSQL container):

```bash
# Create a dedicated network
docker network create scrob-net

# Start the database
docker run -d \
  --name scrob-db \
  --network scrob-net \
  --restart unless-stopped \
  -e POSTGRES_USER=scrob \
  -e POSTGRES_PASSWORD=changeme \
  -e POSTGRES_DB=scrob \
  -v scrob_db:/var/lib/postgresql/data \
  postgres:16-alpine

# Start Scrob
docker run -d \
  --name scrob \
  --network scrob-net \
  --restart unless-stopped \
  -p 7330:7330 \
  -e DATABASE_URL="postgresql+asyncpg://scrob:changeme@scrob-db:5432/scrob" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e TZ=UTC \
  -v scrob_data:/app/backend/data \
  bellamy/scrob:latest
```

**Omnibus image** (PostgreSQL included - no separate container needed):

```bash
docker run -d \
  --name scrob \
  --restart unless-stopped \
  -p 7330:7330 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e TZ=UTC \
  -v scrob_data:/app/backend/data \
  -v scrob_db:/app/postgres/data \
  bellamy/scrob:latest-omnibus
```

### First Setup

1. Open `http://localhost:7330` and create your account.
2. Go to **Settings → General** to add your TMDB Read Access Token, then open **Connections → Media Players** to connect Jellyfin, Plex, Emby, Nuvio, or Stremio.
3. Select which libraries and synchronization directions to enable, then trigger your first sync.

For Nuvio, sign in and select one of the returned profiles. For Stremio, select **Connect Stremio**, then authorize the generated Link code or QR code in your Stremio account. See [Nuvio Cloud Synchronization](#nuvio-cloud-synchronization) and [Stremio Synchronization](#stremio-synchronization) for provider-specific behavior and limitations.

### Updating

```bash
docker compose pull && docker compose up -d
```

Database migrations run automatically on startup - no manual steps required.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | - | **Required.** JWT signing key. Generate with `openssl rand -hex 32`. |
| `DATABASE_URL` | - | **Required** (standard image). PostgreSQL connection string (`postgresql+asyncpg://...`). Optional on the omnibus image - if omitted, the embedded database is used. |
| `ENABLE_REGISTRATIONS` | `false` | Allow new users to register. The first user can always register regardless of this setting. |
| `REGISTRATION_MAX_ALLOWED_USERS` | `0` | Maximum number of registered users. `0` = unlimited. |
| `TZ` | `UTC` | Container timezone (e.g. `Europe/Lisbon`). |
| `PUID` | `1000` | User ID to run the process as. |
| `PGID` | `1000` | Group ID to run the process as. |
| `BACKEND_PORT` | `7331` | Internal port the backend binds to. Override only if `7331` conflicts on bare metal. |
| `OIDC_ENABLED` | `false` | Enable OIDC login. |
| `OIDC_DISABLE_PASSWORD_LOGIN` | `false` | Enforce OIDC-only login (disables username/password). |

See `docker-compose.yaml` for the full list of OIDC variables and other variables.

### Reverse proxy

Scrob listens on port `7330`. Place a reverse proxy (Caddy, Nginx, Traefik) in front for HTTPS - required for the PWA install prompt on non-localhost addresses.

```
# Caddyfile
scrob.yourdomain.com {
    reverse_proxy localhost:7330
}
```

### External PostgreSQL

Remove the `scrob-db` service and set `DATABASE_URL` to your existing instance:

```yaml
DATABASE_URL: postgresql+asyncpg://user:password@your-db-host:5432/scrob
```

## Nuvio Cloud Synchronization

Scrob connects to the [Nuvio public Cloud API](https://nuvio.tv/docs) at `https://api.nuvio.tv`. A TMDB Read Access Token must be configured in Scrob so Nuvio content identifiers can be matched to movies and shows.

### Connect Nuvio

1. Open **Connections → Media Players** and select **Add Connection**.
2. Choose **Nuvio**, then enter a connection name, your Nuvio email, and your Nuvio password.
3. Select **Test** to authenticate and load the profiles attached to the account.
4. Select the Nuvio profile to synchronize, choose the pull and push options, then select **Add**.

Scrob exchanges the email and password for a refresh token. The password is never persisted. Refresh-token rotation is handled automatically during connection checks and synchronization.

Each connection targets one Nuvio profile. Add another connection if you need to synchronize another profile from the same account.

### Synchronization Directions

| Direction | Setting | Behavior |
|---|---|---|
| Nuvio → Scrob | **Collection status** | Imports the profile's library movies and series. |
| Nuvio → Scrob | **Watched status** | Imports watched movies and episodes with their latest watch timestamps. |
| Nuvio → Scrob | **Playback progress** | Imports position and duration into Continue Watching. |
| Scrob → Nuvio | **Collection status** | Adds or removes library membership while preserving unrelated Nuvio items. |
| Scrob → Nuvio | **Watched status** | Pushes watched and unwatched changes made in Scrob or imported from another connected provider. |
| Scrob → Nuvio | **Playback progress** | Pushes current playback positions into Nuvio's Continue Watching state as non-destructive upserts. |

**Sync now** runs an inbound synchronization using the enabled Nuvio → Scrob settings. **Push** sends the enabled collection, watched-history, and playback-progress data from Scrob to Nuvio. Pushes use merge semantics and preserve unrelated remote items.

Ratings are not synchronized with Nuvio.

### Scheduling and Limitations

**Auto Pull** and **Auto Push** can run independently every 15 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, 24 hours, or 48 hours. Nuvio synchronization is polling-based; Nuvio does not use the media-server webhook URLs documented below.

Inbound Nuvio identifiers are normalized to TMDB for Scrob's internal matching. Before an outbound push, Scrob resolves those TMDB identifiers to Nuvio-compatible bare IMDb identifiers (`tt...`) and caches the mapping. Unsupported identifiers are skipped rather than attached to the wrong title.

## Trakt Synchronization

Trakt now requires a **Trakt VIP** subscription to create a new API application (the client ID/secret used below) - a restriction Trakt introduced on their end, not a Scrob limitation. There are two ways to get your Trakt data into Scrob depending on whether you have VIP:

| | Requires VIP | Imports | Pushes Scrob → Trakt |
|---|---|---|---|
| **OAuth connection** | Yes (to create the API app) | Watched history, ratings, lists - kept in sync automatically | Yes - watched status, ratings, collection, lists, live "now watching" |
| **Export import** | No | Watched history, ratings (including per-episode), lists - one-time snapshot per upload | No - pull only |

### OAuth connection (VIP)

1. Go to [trakt.tv/oauth/applications/new](https://trakt.tv/oauth/applications/new) and create an application to get a Client ID and Client Secret.
2. Open **Connections → Media Trackers → Trakt**, paste them in, and select **Connect Trakt**.
3. Enter the code shown at the provided URL to authorize, on trakt.tv.
4. Choose what to import under **Trakt → Scrob**, then select **Pull** (incremental) or **Full resync**.
5. Enable the desired **Scrob → Trakt** options to push watched status, ratings, collection, lists, or live scrobbling back to Trakt.

### Export import (no VIP required)

1. On trakt.tv, go to **Settings → Data** and select **Export now** to download your export zip.
2. In Scrob, open **Connections → Import**, select the **Trakt** tab, then drop the zip on the upload box (or click it to browse).
3. Choose what to import - Watched History, Ratings (including per-episode), and/or Lists, all preselected by default - then confirm. This is a one-shot, per-upload choice, independent of the **Trakt → Scrob** preferences used by the OAuth pull.

Re-uploading a newer export is safe to do any time you want to catch up on new activity - imported watch plays and ratings are deduplicated, so nothing is imported twice.

**Auto Pull** and **Auto Push** apply only to the OAuth connection and can run independently every 15 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, 24 hours, or 48 hours.

## Stremio Synchronization

Scrob uses Stremio's account datastore API at `https://api.strem.io`, the official Link flow at `https://link.stremio.com`, and Cinemeta episode metadata. Configure a TMDB Read Access Token in Scrob before synchronizing so Stremio IMDb identifiers can be mapped to Scrob media.

### Connect Stremio

1. Open **Connections → Media Players** and select **Add Connection**.
2. Choose **Stremio**, enter a connection name, and select **Connect Stremio**.
3. Open the generated authorization link or scan its QR code, then approve the connection in Stremio.
4. Return to Scrob. The page detects the authorization and creates the connection automatically.

Scrob never asks for or stores your Stremio password. The Link flow returns an account authorization key, which is stored server-side and redacted from frontend API responses. Deleting the connection logs out that Stremio session. Each Scrob user can have one Stremio connection.

Authorization links expire in the Scrob interface after 10 minutes. Select **Connect Stremio** again to generate a fresh code.

### Stremio Synchronization Directions

| Direction | Setting | Behavior |
|---|---|---|
| Stremio → Scrob | **Collection status** | Imports active Stremio library movies and series. |
| Stremio → Scrob | **Watched status** | Imports watched movies and episodes. Series episode state is decoded from Stremio's watched bitfield using Cinemeta episode order. |
| Stremio → Scrob | **Playback progress** | Imports the current movie or episode position and duration into Continue Watching. |
| Scrob → Stremio | **Collection status** | Adds local collection items and removes only items previously pushed by this Scrob connection. Items created directly in Stremio are preserved. |
| Scrob → Stremio | **Watched status** | Merges movie and episode watched state into the existing Stremio record. |
| Scrob → Stremio | **Playback progress** | Merges the current playback position, duration, and episode identifier into Stremio. |

**Sync now** performs an inbound pull. The first pull reads the complete Stremio library; later pulls use Stremio modification metadata with a five-minute overlap window. **Push** sends the complete set of enabled outbound data. Changes imported from another provider are also forwarded to Stremio when the corresponding outbound option is enabled.

Outbound writes first fetch the current Stremio record and preserve unknown fields, addon metadata, and unrelated remote items. No-op records are skipped. Ratings and Stremio addons are not synchronized.

### Scheduling, Full Resync, and Limitations

**Auto Pull** and **Auto Push** use separate schedules and can run every 15 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, 24 hours, or 48 hours.

Use **Full resync** when the incremental cursor must be rebuilt. It reads the complete Stremio library and reconciles only collection sources owned by that Stremio connection; collection entries still backed by Jellyfin, Plex, Emby, Nuvio, or another source remain in Scrob.

Stremio exposes a current watched state rather than Scrob's complete per-play history. For series, Stremio stores a watched-episode bitfield and one `lastWatched` timestamp for the item, so repeated episode plays and their individual timestamps cannot be reconstructed exactly. Playback progress represents one current movie or episode per library item.

## Simkl Synchronization

1. Create a Simkl application at [simkl.com/settings/developer](https://simkl.com/settings/developer) to get a Client ID.
2. Open **Connections → Media Trackers → Simkl**, paste the Client ID in, and select **Connect Simkl**.
3. Go to the shown URL and enter the displayed PIN to authorize, on simkl.com.
4. Choose what to import under **Simkl → Scrob**, then select **Pull**.
5. Enable the desired **Scrob → Simkl** options to push watched status, ratings, or live scrobbling back to Simkl.

Simkl uses PIN-based authentication - no client secret is needed.

| Direction | Setting | Behavior |
|---|---|---|
| Simkl → Scrob | **Watched history** | Imports watched movies and episodes. |
| Simkl → Scrob | **Ratings** | Imports ratings. |
| Simkl → Scrob | **Lists / Watchlist** | Imports "plan to watch" items into a managed **Simkl - Watchlist** list. |
| Scrob → Simkl | **Watched status** | Pushes watched and unwatched changes made in Scrob or imported from another connected provider. |
| Scrob → Simkl | **Ratings** | Pushes rating changes. |
| Scrob → Simkl | **Live scrobbling** | Pushes playback start/stop events from webhooks and manual scrobble sessions in real time. |

The manual **Push** action sends the complete enabled watched-history and ratings snapshot, in batches of 50 items per request. Collection membership and the Simkl watchlist are not pushed back to Simkl.

**Auto Pull** and **Auto Push** can run independently every 15 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, 24 hours, or 48 hours.

## MDBList Synchronization

1. Open **Connections → Media Trackers → MDBList**.
2. Copy the API key from [MDBList Preferences](https://mdblist.com/preferences), paste it into Scrob, and select **Save Changes**.
3. Choose the data to import under **MDBList → Scrob**, then select **Pull**. MDBList pulls run only when this button is selected.
4. To send changes back, enable the required **Scrob → MDBList** options. Watched-state and rating edits are pushed as they happen; edits to the managed **MDBList - Watchlist** are pushed to the MDBList watchlist.

The manual **Push** action sends the complete enabled watched, ratings, or managed-watchlist snapshot. MDBList pagination follows `next_cursor` and requests the documented maximum of 1,000 items per page.

**Auto Pull** and **Auto Push** can run independently every 15 minutes, 30 minutes, 1 hour, 3 hours, 6 hours, 12 hours, 24 hours, or 48 hours.

## Webhooks (Real-time Scrobbling)

Webhooks update your watch history and Continue Watching in real time. Each user's webhook URL is shown in **Connections** next to the relevant integration.

```
# Jellyfin, Plex, Emby - connection_id is shown in Connections next to each server
https://your-scrob-url/api/proxy/webhooks/{jellyfin|plex|emby}/{connection_id}?api_key=YOUR_API_KEY

# Kodi - no connection, just the API key
https://your-scrob-url/api/proxy/webhooks/kodi?api_key=YOUR_API_KEY
```

### Jellyfin

1. In Jellyfin, go to **Dashboard → Plugins → Catalogue**, install **Webhook**, then restart.
2. Go to **Dashboard → Plugins → Webhook → Add Generic Destination**.
3. Paste your Scrob Jellyfin webhook URL.
4. Enable notification types: `Playback Start`, `Playback Progress`, `Playback Stop`, `User Data Saved` (this is what fires when you manually mark something watched/unwatched - the plugin has no separate "Mark Played" event), `Item Added`, and `Item Deleted` (keeps your Scrob collection in sync with your library without waiting for playback or a full resync).
5. Enable item types: `Movies` and `Episodes`.
6. **Leave the Template field blank** and check **"Send all properties (ignore templates)"**.

> Do not use a custom template - Jellyfin's template engine produces invalid JSON. "Send all properties" sends a well-formed payload that Scrob parses correctly.

### Plex

Plex webhooks require a **Plex Pass** subscription.

1. Go to [plex.tv/account](https://www.plex.tv/account/) → **Webhooks → Add Webhook**.
2. Paste your Scrob Plex webhook URL.
3. In Scrob → Connections, enter your **Plex username** so events are attributed to the right account.

### Emby

1. In Emby, go to **Dashboard → Notifications → Add Notification → Webhook**.
2. Paste your Scrob Emby webhook URL.
3. Enable events: `Playback Start`, `Playback Progress`, `Playback Stop`, `Item Added`, `Item Deleted`.

### Kodi

Kodi scrobbling uses the **[scrob-kodi](https://github.com/ellite/scrob-kodi)** add-on - no manual webhook configuration needed.

1. Install the **scrob-kodi** add-on from the [scrob-kodi repository](https://github.com/ellite/scrob-kodi).
2. In the add-on settings, enter your Scrob URL and your API key (found in **Connections → API Key**).
3. The add-on will automatically send playback events to Scrob as you watch.

## OIDC / Single Sign-On

Scrob supports any OpenID Connect provider (Authelia, Authentik, Keycloak, Google, etc.).

```yaml
OIDC_ENABLED: "true"
OIDC_PROVIDER_NAME: "Authelia"
OIDC_CLIENT_ID: "scrob"
OIDC_CLIENT_SECRET: "your-secret"
OIDC_AUTH_URL: "https://auth.yourdomain.com/api/oidc/authorization"
OIDC_TOKEN_URL: "https://auth.yourdomain.com/api/oidc/token"
OIDC_USERINFO_URL: "https://auth.yourdomain.com/api/oidc/userinfo"
OIDC_REDIRECT_URL: "https://scrob.yourdomain.com/oidc-callback"
OIDC_AUTO_CREATE_USERS: "true"
# OIDC_DISABLE_PASSWORD_LOGIN: "true"  # uncomment to enforce SSO-only
```

Register Scrob as a client in your provider with redirect URI: `https://scrob.yourdomain.com/oidc-callback`

## Email Validation & SMTP

Scrob can require new users to verify their email address before logging in. Providing SMTP settings also enables the **forgot password** link on the login page.

```yaml
REQUIRE_EMAIL_VALIDATION: "true"
SERVER_URL: "https://scrob.yourdomain.com"
SMTP_ADDRESS: "smtp.gmail.com"
SMTP_PORT: "587"
SMTP_ENCRYPTION: "tls"
SMTP_USERNAME: "myemail@gmail.com"
SMTP_PASSWORD: "your-app-password"
FROM_EMAIL: "myemail@gmail.com"
```

| Variable | Default | Description |
|---|---|---|
| `REQUIRE_EMAIL_VALIDATION` | `false` | Require new users to verify their email before logging in. |
| `SERVER_URL` | - | Public URL of your Scrob instance, used to build the validation link in emails. |
| `SMTP_ADDRESS` | - | SMTP server hostname. |
| `SMTP_PORT` | `587` | SMTP server port. |
| `SMTP_ENCRYPTION` | `tls` | Encryption method - `tls` or `ssl`. |
| `SMTP_USERNAME` | - | SMTP login username. |
| `SMTP_PASSWORD` | - | SMTP login password (use an app password if using Gmail). |
| `FROM_EMAIL` | - | Address emails are sent from. |

## Contributing

Contributions are welcome - whether it's a bug report, a feature request, or a pull request.

- **Issues**: Open an issue for bugs, questions, or feature ideas.
- **Pull Requests**: Fork the repo, create a branch, and submit a PR. Please follow the existing code style (Astro components for UI, FastAPI for backend) and make sure all browser-initiated API calls go through `/api/proxy/`.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) - `feat:`, `fix:`, `chore:` - as releases and changelogs are generated automatically from them.

## Contributors

<a href="https://github.com/ellite/scrob/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ellite/scrob" />
</a>

## Development

<details>
<summary>View instructions</summary>

### Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Node.js 22+
- PostgreSQL 16 (via Docker is easiest)

### Setup

```bash
git clone https://github.com/ellite/scrob.git
cd scrob

# Start a local database
docker compose -f docker-compose-test-db.yaml up -d

# Copy and fill in the environment file
cp .env.example .env
# Edit .env - set POSTGRES_* and SECRET_KEY at minimum
```

### Backend

```bash
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn main:app --reload --port 7331
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The frontend dev server starts on `http://localhost:4321` and proxies API calls to the backend on `7331`.

</details>

## License

Scrob is licensed under the [GNU General Public License v3.0](LICENSE.md).

You are free to use, modify, and distribute Scrob, provided that any derivative works are also released under the GPLv3.

## Links

- The author: [henrique.pt](https://henrique.pt)
- Scrob Landingpage: [scrob.app](https://scrob.app)
- Join the conversation: [Discord Server](https://discord.gg/anex9GUrPW)
