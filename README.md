# Wisp's e621 Mod Ticket Bot

![Application logo](assets/e6ticket.png)

A portable, read-only Windows monitor for pending, unclaimed e621 moderator tickets.
It checks for newly encountered ticket IDs, plays a three-second generated chime,
shows a Windows notification, and animates a smooth blue/yellow halo on every monitor.
Visual alerts wait while fullscreen is detected; sound can still play.

## Choose how to run

### Pre-built Windows application (no Python installation)

1. Download the Windows x64 ZIP from [Releases](https://github.com/WispTheHusky/wisps-e621-mod-ticket-bot/releases/latest).
2. Extract the **entire folder** to a writable location of your choice.
3. Run `E621TicketBot.exe`. Keep the `_internal` folder next to it.
4. Click **Login**, enter your e621 username and API key, then press **Start**.

Python, Tk and Pillow are included. This is a portable folder rather than a
self-extracting EXE, so it does not unpack its runtime into Windows' temporary
folder. It does not need administrator rights. Do not run from inside the ZIP
or put it in a protected folder such as Program Files.

### Readable Python source

Download `ticket_bot.pyw` from the release, inspect it, and run that same file.
The icon is embedded and the chime is synthesized; no companion assets are needed.
On Windows, install Python 3.10+ with Tcl/Tk support and Pillow:

```powershell
py -m pip install Pillow==12.3.0
pyw ticket_bot.pyw
```

The official Windows Python installer normally includes Tcl/Tk. If several Python
installations are present, install Pillow into the one associated with `.pyw` files.
A missing graphics dependency stops startup with setup instructions; it does not
silently replace the halo or indicators with degraded graphics.

## Everyday use

- **Start / Stop** controls polling. **Pause / Resume** preserves the remaining wait.
- **Apply** saves a refresh interval from 5 to 3600 seconds (default: 5).
- **Settings** contains Alert Test, Ticket Test, and sound/halo switches.
- **Ticket Test** pauses scanning, shows fake ticket information and an alert,
  then restores the real display after ten seconds. Fake data is never persisted.
- The latest ten real tickets, their IDs/reasons and last-found time survive restarts.
- Ticket headings open the corresponding ticket in your normal browser.
- **Logout** stops polling, deletes that account's encrypted key file and clears
  its display. The last username is remembered for the next login.
- **Close Bot** asks for confirmation. The title-bar close button is disabled;
  other normal close requests also use confirmation.

The first successful scan records a quiet baseline. Several new tickets in one
scan produce one alert with the count. Rejected credentials stop retries;
connection failures and rate limits use delayed retries. Scans never overlap.
The dashboard starts stopped, including at Windows sign-in.

## Portable storage

All files created or read **for the bot's own persistent data** are inside `data/`
next to `ticket_bot.pyw` or `E621TicketBot.exe`:

- `config.json`: remembered username, refresh interval and alert preferences.
- `state*.json`: seen ticket IDs, the latest ten detections, last-found time and
  pending visual alerts. These contain private moderator information.
- `credentials/*.bin`: API keys encrypted with Windows DPAPI for the Windows user
  who saved them. Copying the folder to another computer/account requires logging
  in again. Encryption is not protection against software already running as you.
- `resources/`: embedded icons and the Windows toast helper, extracted only when
  a Windows API needs a filename. The chime is generated and played in memory;
  no WAV file is written.

The bot never falls back to AppData, your home folder, the Windows temporary folder
or Credential Manager. A non-writable installation folder causes an error instead.
The source launcher disables Python bytecode writes after startup.

**Windows integration boundaries:** Windows manages its own DPAPI master keys,
notification history, registry and system caches. The program uses Windows APIs;
it cannot relocate or prevent those operating-system-managed records. Windows
notifications register the app's name/icon under its own HKCU AppUserModelId key.
Optional sign-in startup uses its own HKCU Run entry. Source execution also reads
its installed Python/Tk/Pillow runtime; the EXE bundle includes that runtime locally.
Opening a ticket uses your browser and its normal storage.

To share the application, send a clean release download, **never your `data/`
folder**. The build script includes only public source and runtime components.
When updating, close the app and replace its program files while preserving `data/`.
Old AppData installations are not automatically inspected or imported.

## Read-only network boundary

Authenticated requests are GET-only to the fixed pending/unclaimed queue:
`https://e621.net/tickets.json?search[status]=pending_unclaimed`.
Other HTTP methods, request bodies, redirects, changed endpoints and unapproved
parameters are rejected before transmission. The code contains no ticket claim,
assignment, edit or delete action. Public avatar requests have a separate GET
allowlist and never receive the API key. Raw responses/credentials are not logged.
An incomplete scan never becomes a successful empty scan or partial baseline.

## Verify downloads and inspect the build

Releases include `SHA256SUMS.txt`, covering the ZIP, `.pyw`, and executable inside
the extracted folder. In PowerShell, for example:

```powershell
Get-FileHash .\E621TicketBot-1.0.0-windows-x64.zip -Algorithm SHA256
Get-FileHash .\E621TicketBot\E621TicketBot.exe -Algorithm SHA256
```

Compare the result with the corresponding line in the release's checksum file.
`MD5SUMS.txt` is also provided for compatibility, but SHA-256 is recommended.
**A matching checksum confirms the downloaded file matches the published file.
It does not prove that an executable implements the published source.**

For source assurance, inspect and run the `.pyw`, build it yourself, or review the
public Windows build workflow and its tagged source. `BUILD_INFO.json` inside the
ZIP records dependency versions and source/executable SHA-256 hashes. Different
build environments can produce different binary hashes; this project does not
claim bit-for-bit reproducible builds or code-sign its Windows executable.

## Build and test from source (Windows)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -B tools\build_release.py
```

The build runs the automated tests, creates `dist/E621TicketBot/`, then produces
the ZIP and checksum files in `dist/`. Runtime and build dependencies are pinned.
The `tests/` directory is for development; recipients do not need it. Tests use
synthetic credentials/records and isolated storage, never a real moderator key.
The GitHub workflow builds branch/PR changes and publishes tagged `v*` releases.

## Optional Windows sign-in startup

From the application folder:

```powershell
.\E621TicketBot.exe --startup enable
.\E621TicketBot.exe --startup disable
```

For source, use `py ticket_bot.pyw --startup enable` or `--startup disable`.
Startup points at that exact installed copy. Disable it before moving/removing
that copy, then enable it from the new location. Do not run multiple copies.

## Licence

Application code: [MIT](LICENSE), copyright 2026 WispTheHusky.
See [third-party notices](THIRD_PARTY_NOTICES.md) for bundled components and artwork.
This is an unofficial tool; e621 names/logos remain with their respective owners.
