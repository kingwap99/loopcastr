# loopcastr operations manual

From architecture and deployment to daily operation, troubleshooting and known limits. Design trade-offs
and measured numbers are recorded in the [changelog](changelog.md).

> This document describes the current state and how to operate it. Day-by-day measurements, changes and
> overturned assumptions live in the [changelog](changelog.md), which is in Traditional Chinese.

## Architecture in one line

    media/<mode>/*.mp4 --concat--> playout.sh (one ffmpeg) --> MediaMTX --yt_publish.sh (one long-lived ffmpeg) --> YouTube ingest

Local files go through concat; only relaying an external YouTube live stream (where the source itself has to
change) falls back to the takeover relay in `relay.py`.

## Files

Install and configuration:

| Path | Role |
|---|---|
| `install.sh` | **Install / upgrade**: copy the programs, substitute the plist placeholders, generate `mediamtx.yml`, register the launchd services |
| `src/settings.json` | General settings: quality, fps, bitrate, fade seconds, watermark and marquee, black-tail threshold. The programs read it as their **defaults** and the command line can override |
| `src/modes.json` | Broadcast mode definitions: source channel, length cap, shorts pool and rescan interval per mode |
| `ui.lang` in `src/settings.json` | Language: `zh` / `en` (switchable at the top right of the console); affects both the console interface and the on-screen captions |
| `src/playlist.example.json` | **Example** master list (3 fake ids). The real `playlist.json` is produced by `build_playlist.py` and is in `.gitignore` |
| `mediamtx.example.yml` | Example MediaMTX configuration. It deliberately uses a **path allow-list** (only `live/main`) rather than the open-everything MediaMTX default |

Building content:

| Path | Role |
|---|---|
| `src/build_playlist.py` | Scan a playlist or channel and produce the master list `playlist.json` (with each duration) |
| `src/build_local_content.py` | Land the master list as local `media/<mode>/<id>.mp4`: normalise, verify duration, detect black tails, watermark and countdown, fade in/out |
| `src/build_transitions.py` | Build the per-episode transition `media/<mode>/_tr_<id>.mp4` (its QR points at that episode); can use a rotating shorts pool |
| `src/build_test_edition.py` | Produce a shortened test edition: each episode cut to a fixed length with a sequence number burned in at the top right |
| `src/mode_build.py` | Run the whole chain for a mode from `modes.json`: scan -> land -> transitions -> deploy -> rebuild list |
| `src/wmtext.py`, `src/make_qr_png.py` | Generate the watermark text and QR code PNGs (the machine has no freetype, so they are drawn by hand) |

Where the content lives (under `media/`):

| Path | What it is | Shared? |
|---|---|---|
| `media/<mode>/<id>.mp4` | The normalised video | One per mode (the same video can have a different length cap per mode) |
| `media/<mode>/_tr_<id>.mp4` | The pass-1 transition; `_tr_<id>_p2.mp4` is pass 2 | Same |
| `media/<mode>/manifest.json` | Duration and black-tail record for that mode | Same |
| `media/.raw/<id>.mp4` | The original download (`--keep-raw`) | **Shared**: it is an input and has nothing to do with the mode |
| `media/short-<id>.mp4` | The shorts pool | **Shared**: same |

Playout:

| Path | Role |
|---|---|
| `src/make_concat_list.py` | Turn `playlist-local.json` into the ffmpeg concat list `concat.txt` |
| `src/playout.sh` | **Playout**: one concat process looping the list, pushing to MediaMTX. Supervised by `com.loopcastr.playout` |
| `src/yt_publish.sh` | **Publisher**: one long-lived ffmpeg pushing MediaMTX to YouTube ingest. Supervised by `com.loopcastr.publish` |
| `src/switch_edition.sh` | Switch which list is broadcast (production / each mode / test), rebuilding the list, editing the plist and restarting the service |
| `src/relay.py` | Relay and multi-source handover engine (gapless takeover, watchdog, source URL lifetime) |

Observation and maintenance:

| Path | Role |
|---|---|
| `src/webui.py` | **Local console** (standard library only): status, settings editing, build actions |
| `src/healthcheck.py` | Health check (every 60 seconds): ready, has readers, traffic growing; alerts only on state changes and can heal automatically |
| `src/loopwatch.py` | Loop-boundary observation: measure whether there is a gap at the moment the list wraps |
| `src/gapwatch.py` | Standalone acceptance observer: poll the MediaMTX API and report receiver-offline windows and `bytesReceived` zero-growth windows |
| `src/refreshwatch.py` | Rescan periodically according to `refresh_seconds` in `modes.json`; when new content appears, hand over at the next segment boundary |
| `src/yt_side_monitor.py` | Long-running monitoring of the YouTube side: pull the live stream and run blackdetect / freezedetect |

Service definitions (templates; `__HOME__` / `__USER__` / `__YT_VIDEO_ID__` are substituted by `install.sh`):

| Path | Role |
|---|---|
| `launchd/com.loopcastr.mediamtx.plist` | Media hub (with an 8192 fd ResourceLimits) |
| `launchd/com.loopcastr.playout.plist` | Playout |
| `launchd/com.loopcastr.publish.plist` | Publisher |
| `launchd/com.loopcastr.health.plist` | Health monitoring (runs as root so it can kickstart the system domain) |
| `launchd/com.loopcastr.refresh.plist` | Automatic rescan |

## Why the playout uses concat rather than a relay

The same "3 local files in a loop":

| Playout style | Gap at a segment change | Gap at the wrap back to the top | Cold-start gap |
|---|---|---|---|
| `relay.py` handover (one publisher per segment) | 2.0-3.1 seconds per segment | yes | 3.07 seconds |
| `playout.sh` concat (one publisher throughout) | **0** | **0** | 2.55 seconds (once only) |

The reason the relay breaks is concrete: **the file ends in EOF, MediaMTX immediately drops the publisher, and
the receiver is offline while the next publisher warms up**. Neither `--url-max-age` nor a longer `tail` helps,
because it is the **source** side that ends first, not the output side.

Measured evidence (target machine, 2026-09-16 05:13, 55 seconds covering two rounds of the list):

    receiver offline windows: 0 (continuous throughout)
    bytesReceived zero-growth windows: 0, longest 0.000s

The log also shows DTS `48000` and `57000`, proving it really did wrap into the second round and that the wrap
point had no offline window.

**The only side effect**: the concat demuxer emits `Non-monotonic DTS` warnings at every seam (audio packet
boundaries are rounded, measured overlap about 11 ms). ffmpeg clamps them back automatically and there is no
audible effect. If you need a perfectly clean timeline, splice offline into one file first:

    python3 src/make_concat_list.py playlist-local.json -o concat.txt --base-dir .
    ffmpeg -hide_banner -f concat -safe 0 -i concat.txt -c copy media/all-in-one.mp4

## Deployment

Requirements: macOS with Python 3, plus `ffmpeg`, `yt-dlp` and `mediamtx` on `PATH`
(`brew install ffmpeg yt-dlp mediamtx`) and the Python packages `qrcode` and `pillow`
(`python3 -m pip install --user qrcode pillow`). `install.sh` checks all of them and prints what is missing.

`install.sh` copies the programs to the install directory, substitutes the `__HOME__` / `__USER__`
placeholders in the plists, generates `mediamtx.yml` and registers the launchd services:

    ./install.sh --dry-run
    ./install.sh
    ./install.sh --agents

The first is a dry run and changes nothing. The second installs to `~/loopcastr` with LaunchDaemons and
needs sudo; `--agents` installs LaunchAgents instead, which need no root but do need a graphical login.

Safe to run repeatedly; an existing `mediamtx.yml` and `stream.key` are never overwritten.

**To upgrade**: `git pull` and then run `./install.sh` again. The pull only updates `src/`; what runs is
the flattened copy in the install directory, and `install.sh` is what refreshes it.

**The services are registered only when there is content to play.** On a first install there is none, so that
run just copies the files and generates the plists and nothing starts - otherwise launchd would restart a
process that is bound to fail. Build the content (see "Adding new episodes"), then run `./install.sh` again:
the second run registers and starts the services. `--force-services` skips the wait.

The console can also start a not-loaded service, but only for a gui-domain (LaunchAgent) install; with
LaunchDaemons the services have to be registered by `./install.sh` again, or started with `sudo launchctl`.

Acceptance check (while broadcasting, in another terminal):

    python3 ~/loopcastr/gapwatch.py http://127.0.0.1:9997 live/main 120

## Check before going live

- `playlist-local.json` **must exist and every segment file must be on disk**, otherwise `playout.sh` stops
  while building the concat list (by design, so half a list is never broadcast).
- `stream.key` must be mode `600` and must never be committed.
- Both the local and the target machine are on `TZ=Asia/Taipei`, so log timestamps line up directly.

## Operations

### The daily glance

    ssh <USER>@<TARGET_HOST>
    launchctl list | grep loopcastr
    tail -3 ~/loopcastr/logs/health.log
    tail -3 ~/loopcastr/logs/alerts.jsonl

`launchctl list` should show the expected services (if not, see the Services block in the console);
`health.log` says whether the chain is healthy and `alerts.jsonl` whether anything has alerted.

`health.log` writes one line every 60 seconds. A line like `OK chain healthy (traffic +N bytes / 6s)` means
healthy; N is around 2,000,000. In Chinese mode the same line reads `OK 全鏈路正常（流量 +N bytes / 6s）`.

### Changing the stream key

`yt_publish.sh` reads the key **only at startup**, so the service must be restarted after the file changes:

    printf %s <new key> > ~/loopcastr/stream.key && chmod 600 ~/loopcastr/stream.key
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.publish

### Adding new episodes

Normally just use the console: fill in the mode's source URL and press "Build and switch (go live)". It runs
the whole chain from `modes.json` (scan -> land -> transitions -> deploy -> rebuild list -> switch).

The same thing on the command line:

    cd ~/loopcastr
    python3 mode_build.py --mode news --switch
    python3 mode_build.py --mode news --scan-only

The first runs the whole chain (scan, land, transitions, deploy, rebuild the list) and then switches the
playout; `--scan-only` just rescans the master list.

You only touch the `segments` of the master list `playlist-<mode>.json` (`type: vod`, `url`, `seconds`) when
hand-placing a few clips of your own, and then land and rebuild the list yourself:

    python3 build_local_content.py --playlist playlist-news.json --target 720 --keep-raw \
      --media-dir media/news --out-playlist playlist-news-local.json
    python3 make_concat_list.py playlist-news-local.json -o concat-news.txt --base-dir ~/loopcastr
    ./switch_edition.sh news

`build_local_content.py --status` shows at any time which episodes are still missing.

### Rescanning black tails only (without downloading again)

    python3 build_local_content.py --rescan

### Starting and stopping services

First work out which kind of install this is: the launchd domain and the paths differ.

> **Machines installed before the rename**: this system was renamed from `ytpl2ytstream` to `loopcastr` on
> 2026-09-21. Machines installed before that have the directory `~/ytpl` and the services `com.ytpl.*` (the
> paths and labels in the commands below then have to follow the old names). The programs accept both (the
> label prefix is read from the plist actually present in the directory), so updating the programs does not
> break anything; switching to the new names needs a reinstall (or moving the directory and renaming the
> labels by hand).

| Install style | Domain | Plist location | sudo? |
|---|---|---|---|
| `install.sh --agents` | `gui/$(id -u)` | `~/Library/LaunchAgents/` | no |
| `install.sh` | `system` | `/Library/LaunchDaemons/` | yes |

With a LaunchAgent (gui) install:

    launchctl bootout gui/$(id -u)/com.loopcastr.publish
    launchctl bootout gui/$(id -u)/com.loopcastr.playout
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loopcastr.publish.plist
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loopcastr.playout.plist
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.playout

The first pair stops the two services, the second pair starts them again, and the last line restarts the
playout without unloading it.

For a LaunchDaemon install, replace `gui/$(id -u)` with `system`, the paths with `/Library/LaunchDaemons/`,
and prefix the commands with `sudo`.

The Services block in the console only handles the gui domain (it does not run as root), so the system domain is
up to you.

Restarting the playout starts again from the first segment and leaves a cold-start gap of about 2.5 seconds.

### Local console (webui.py)

An interface for people who would rather not ssh in and memorise commands. Standard library only, so nothing has
to be installed.

    cd ~/loopcastr
    python3 webui.py
    python3 webui.py --port 9000
    nohup python3 ~/loopcastr/webui.py > ~/loopcastr/logs/webui.log 2>&1 &

The first serves on `http://127.0.0.1:8787`, the second on another port, and the third keeps it running in
the background.

Blocks (top to bottom is also the order of operation):

| Block | Contents |
|---|---|
| 1 Sources | One card per mode: playlist URL, shorts URL, video count and length cap, rescan interval. The mode's status chip sits at the top right of the card |
| 2 Go live | Build / scan only / build and switch, rebuild concat, check for missing files, **stop build**. Runs in the background and reports progress (a build can take tens of minutes) |
| Playout status / services / content / logs | Service processes, MediaMTX ready / readers / traffic, round length and next loop time, missing concat entries, media size, the tail of health and alerts |
| Quality and layout | A form generated from the schema (bitrate, preset, text and QR sizes, fade seconds, black-tail threshold...); saving takes effect on the next build |
| Advanced settings | The raw JSON of `settings.json` and `modes.json`. The JSON is validated before saving and the previous version is kept as `.bak` |
| Services | Every launchd service is running / loaded but not running / not loaded; a not-loaded one can be started (see the Services section below) |

#### Stop build

A build can run for tens of minutes, so press "Stop build" when you want to change parameters halfway through.

- It stops the **whole process tree**: `mode_build.py` -> `build_local_content.py` -> `ffmpeg`. Killing only the
  top level leaves the other two orphaned, still writing the same file.
- SIGTERM first, then SIGKILL if it has not finished within 3 seconds (measured: ffmpeg does not take SIGTERM
  under an overlay command).
- **Interrupted output files are always deleted.** ffmpeg sometimes finishes cleanly on SIGTERM and leaves a file
  that reads fine but is only tens of seconds long; keeping it would make the next round broadcast it as complete.
  Files that were already finished are untouched and the next build fills in what is missing (the originals in
  `media/.raw/` mean nothing is downloaded again).
- Pressing start while the stop is still finishing is refused (the buttons above also grey out); wait until the
  state reads done / idle.

#### The title and "watch the stream"

The project name in the page title (`loopcastr`) links to the GitHub project in a new tab.
The "watch the stream" button below the title opens the MediaMTX HLS page:

    http://<host>:<port of hlsAddress>/<path>/

Locally that is `http://127.0.0.1:8888/live/main/` by default.

The host name is filled in by the browser, so opening the console from another machine works too.
The address and whether the button appears at all are read from `mediamtx.yml`: with `hls: no` (the repo default)
the button is replaced by a grey line explaining why there is none. To enable it, set `hls: yes` and restart
mediamtx.

#### Security design

- **Binds to `127.0.0.1` by default.** Exposing it requires a token, otherwise it refuses to start:

      openssl rand -hex 16 > ~/loopcastr/webui-token && chmod 600 ~/loopcastr/webui-token
      python3 webui.py --host 0.0.0.0

  Then access it with `?token=<value>` or the `X-Ytpl-Token` header.
- **Every write requires the custom header `X-Ytpl: 1`**: a cross-site form cannot set it, which blocks CSRF.
- **It never runs as root and stores no password.** When a system-domain service has to be restarted it only tries
  `sudo -n` (non-interactive) and, on failure, prints the sudoers line to add instead of feeding a password into
  the program:

      # /etc/sudoers.d/loopcastr-webui
      <your account> ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/com.loopcastr.playout
- **The stream key is write-only**: it can be written, but the page never displays it.
- Actions only call existing scripts (an argv list, never through a shell); a mode name has to exist in
  `modes.json` and arbitrary paths are rejected.

#### What settings.json is

It is the copy meant to be edited by hand, and it is what the console edits. The programs read it as their
**defaults**, the command line can always override per run, and when the file is absent the behaviour is exactly
as if the mechanism did not exist.

| Section | Contents |
|---|---|
| `media` | `dir` (where the library lives; empty = the `media/` folder beside the programs), `target` (720 / 1080 / 480), `fps`, `venc`, `abr`, `audio_fade` (fade in/out seconds at a segment change), `max_seconds` |
| `overlay` | `date_label`, position (`overlay_y` / `overlay_margin` / `band_left`), marquee (`marquee_speed` / `marquee_gap`), button (`link_button` / `link_caption`), `countdown`, `transition_caption`, and the sponsor block (`sponsor_qr_image` / `sponsor_url` / `sponsor_show` / `sponsor_caption`). **Every caption has an `_en` counterpart** (for example `date_label_en`) used when `ui.lang=en` |
| `ui` | `lang`: `zh` (Chinese) / `en` (English). Both the console interface and the on-screen captions follow it, and **only one language is shown at a time**; the Chinese / English switch at the top right of the console changes it |
| `content` | `black_tail_min` (how many seconds of black at the tail count as a black tail) |

To change *what* is broadcast, edit `modes.json`; do not put source information into `settings.json`, or the two
sources of truth will fight each other.

#### Putting the library on another disk

`media.dir` moves the whole content library - episodes, transitions, the shorts pool and the raw downloads -
to another volume. This is the supported way to keep a 24/7 library off the system disk:

1. Set `media.dir` to the new location (an absolute path, for example `/Volumes/media/loopcastr`).
2. Copy the files there: `rsync -a ~/loopcastr/media/ /Volumes/media/loopcastr/`. Copying rather than
   moving keeps the library that is on air intact until the switch is verified.
3. Rebuild the concat list (the console button, or `make_concat_list.py`) so its paths point at the new
   location, then restart the playout.
4. Delete the old copy once the stream is confirmed healthy.

The playout itself never reads `media.dir`: it follows the absolute paths in the concat list, which is why
step 3 matters. Every builder resolves the setting, and `--media-dir` still overrides it for a single run.

### Troubleshooting

| Symptom | Look at first | Usual cause |
|---|---|---|
| YouTube shows nothing but the console's "watch the stream" has content | whether `com.loopcastr.publish` is in `launchctl list` | **the publisher service is not loaded** (a valid stream key is useless if nothing uses it). Press start in the console Services block |
| Viewers see a black picture | `python3 build_local_content.py --rescan` | one of the videos itself has a long black tail |
| Black picture but health is fine | run blackdetect against the local HLS | a content-layer problem: neither the transfer nor the timeline shows it |
| YouTube shows nothing while local is fine and publish is running | `tail ~/loopcastr/logs/publish.log` | the key expired, the live event ended, or ingest was refused |
| health keeps failing | `cat ~/loopcastr/logs/health-state.json` | read the `problems` field; three failures in a row restart the matching service automatically |

### Known limitations

- **A reboot needs a human to unlock the disk**: the target machine has FileVault on and no automatic login (see D16 in the spec).
- **About 49.7 days of continuous playout** hits the FLV 32-bit timestamp wrap, so restarting the playout monthly is recommended.
- **A segment change leaves about 11 ms of overlapping audio timestamps** (the `Non-monotonic DTS` warning). ffmpeg clamps it back automatically and there is no audible effect. Removing it entirely needs an offline splice into one file.

### Services: system domain (a LaunchDaemon install from install.sh)

With that install style the services live in /Library/LaunchDaemons/, the commands need sudo and the domain is
`system` rather than `gui`:

    sudo launchctl list | grep loopcastr
    sudo launchctl kickstart -k system/com.loopcastr.playout
    sudo launchctl bootout system/com.loopcastr.playout
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.playout.plist

playout, publish and mediamtx run as <USER>; health runs as root, because only root can kickstart the system
domain (which is a precondition for the automatic healing).

When moving to a LaunchDaemon install, move the old LaunchAgent versions away first (for example to
`~/loopcastr/launchagents-backup/`), otherwise both get loaded and fight over the same stream.

### Loop-boundary observation (loopwatch.py)

The playout is one concat process with `-stream_loop -1` and its timeline accumulates continuously, so DTS does
not fall back to 0 at the wrap and DTS cannot be used as the signal. This script derives the loop point from
"start time + one-round length" and samples the MediaMTX API at high frequency around it:

    python3 loopwatch.py --lead 45 --tail 90
    python3 loopwatch.py --at 04:19:07

The second form names the moment to measure directly.

The output lists the receiver-offline windows and the `bytesReceived` zero-growth windows across the loop point.
The round length comes from playlist-local.json (including any outpoint trims), so changing the list or the
length cap needs no parameter change.

### Transition clips (one per episode)

A transition is inserted after every episode, including after the last one. **There is one per episode**:
`media/<mode>/_tr_<video id>.mp4`, containing a rotating short (or the clean base) plus a title bar and a QR code
pointing at **that episode's URL**, so viewers can scan through to the episode that just finished.

The name uses the **video id**, not an index: indices overwrite each other when the list changes and switching back
to an old list would pick up the wrong QR. When a round runs several passes (`--passes`), pass 2 and later add
`_pN`, because the same video pairs with a different short in each pass.

`build_local_content.py` inserts the matching `_tr_<id>.mp4` after each episode; if an episode has no dedicated
file it falls back to the shared `media/<mode>/_transition.mp4` (the older mechanism, still usable).

    python3 build_transitions.py --parallel 3 --verify 5

    python3 build_local_content.py --transition <YouTube URL or video id>
    python3 build_local_content.py --no-transition

`--verify 5` spot-checks five of them after the rebuild. The last two lines are the older mechanism - one
shared transition for everything, only needed when there is no per-episode QR - and `--no-transition`
temporarily disables transitions (the files stay, they are simply not inserted).

Transitions are normalised like every other segment (1280x720 / H.264 High L3.1 / 30fps / AAC-LC 48k stereo), so
concat stays a plain `-c copy` with no transcoding.

### Switching between production and test editions

The test list is temporary, so switch back after testing or the channel keeps playing test clips.

    ./switch_edition.sh
    ./switch_edition.sh live
    SUDO_PASS=xxx ./switch_edition.sh test

With no argument it shows which list is active, `live` switches back to production (`playlist-local.json`)
and `test` switches to the test edition.

Switching rebuilds the concat list, rewrites the playout plist, restarts the service and reattaches loopwatch (a
different round length means the loop point is recomputed). Measured: the publisher only drops for 4 seconds and
the YouTube side stays `is_live`.

### Long-running monitoring of the YouTube side (T-12)

Every "zero gap" measurement is taken at MediaMTX. This script watches the transcoding and distribution layer on
the YouTube side:

    python3 yt_side_monitor.py --id <video id> --hours 3

It keeps pulling the YouTube live stream and runs blackdetect and freezedetect, stamping every event with the
wall-clock time so it can be matched against local events. When the live address expires it resolves a new one and
continues.

### Which QR codes appear on screen

| Position | Contents | When |
|---|---|---|
| Top right | The episode's original `https://youtu.be/<id>` with the caption "▶ 看原片" (watch original) on episodes and "去追劇" (watch more) on transitions | On episodes and transitions, with **identical** position and format |
| Bottom right | The sponsor QR: `overlay.sponsor_qr_image` (a QR picture, given as a path or a URL) or, when that is empty, a QR generated from `overlay.sponsor_url` | Only on transitions, and only while `overlay.sponsor_show` is ticked. The shipped defaults are the author's, so a fresh install shows it; clear both to remove it. Any operator can drop in their own QR picture - that is the point of the image setting - and the console previews whatever is configured and says whether it is currently drawn |

The sponsor QR is an ordinary setting rather than something baked in, and it is drawn by
`build_transitions.py` alone: changing the link or the switch only rebuilds the transitions (a few
minutes), never the episodes. The console shows the QR exactly as it appears on screen, together with
whether it is currently shown.

Two ways to supply the picture, and the image wins when both are set:

- `overlay.sponsor_qr_image` - a QR picture, either a path (`assets/sponsor-qr.png`) or a URL
  (`https://raw.githubusercontent.com/<user>/<repo>/main/assets/sponsor-qr.png`). A URL is fetched
  once at build time and cached in `media/.raw/`, so a later build reuses it offline.
- `overlay.sponsor_url` - the payment link. When there is no picture, a plain QR is generated from
  it; if the picture cannot be fetched either, the build falls back to this link.

The picture is scaled to fit the panel and centred, never cropped, so a square QR image of any size
works. The panel around it (rounded, translucent, with the caption) is the same one the episode
buttons use.

The QR PNGs are produced by `make_qr_png.py` (the machine has no qrencode, so only the pure-Python `qrcode`
package is used for the matrix and the PNG is written by hand with zlib + struct):

    python3 make_qr_png.py "<url>" out.png --scale 8 --border 4

The transition QR is overlaid fresh by `build_transitions.py` on every rebuild, so change the parameters and rerun
rather than editing an already-overlaid file (that gets muddier every time):

    python3 build_transitions.py --button-caption "去追劇" --parallel 3

Always verify by decoding a frame from the **encoded video**, not by looking at the picture:

   python3 -c "import cv2,subprocess;subprocess.run(['ffmpeg','-y','-ss','10','-i','media/news/_tr_<id>.mp4','-frames:v','1','/tmp/f.png']);print(cv2.QRCodeDetector().detectAndDecode(cv2.imread('/tmp/f.png'))[0])"

### Using a rotating shorts pool as transitions

A transition does not have to be one fixed clip; it can rotate through a pool of shorts:

    python3 build_transitions.py --playlist playlist-tucheng3.json \
      --shorts-url "https://www.youtube.com/<SHORTS_CHANNEL>/shorts" \
      --shorts-count 3 --seconds 90 --parallel 3 --verify 3

It fetches the first N shorts, normalises them to the same parameters as every other segment (1280x720 / H.264
High L3.1 / 30fps / AAC-LC) and overlays each episode's QR in turn. The pairing is "pass p, episode i uses pool
entry `(p x episodes + i - 1) % N`", so 30 videos with 50 shorts over 2 passes continues from short 31 in pass 2
instead of starting the rotation over.

**Vertical shorts are scaled down and pillarboxed**, never cropped or distorted. Measured: a 1080x1920 short
becomes 404x720 with about 438px of black on each side.

`--seconds` is the length cap of each transition; a shorter clip is used at its own length.

Transitions also carry the **title marquee** (the same style as episodes) but with the left bound at 0: a vertical
short is pillarboxed anyway, so no space has to be left for a logo as on episodes.

The transition QR uses the **same component**, the **same position** (top right) and the same layout as episodes,
with no distinction at all; only the caption differs: "watch original" on episodes and "**去追劇**" (watch more) on
transitions (`--button-caption` changes it, `--no-button` turns it off entirely).

### The remaining-time countdown

Below the episode's QR button there is a "remaining MM:SS" badge that updates every second.

Text cannot be drawn onto the video directly (no freetype) and the countdown has to change over time, so every
second is pre-rendered into one tall strip and overlaid with a time-based crop. A one-image-per-second sequence
input was tried first and does not work: a 1 fps sequence does not line up with the 30 fps main picture inside
overlay, the whole layer disappears, and ffmpeg reports no error at all (measured).

There is deliberately **no "n of total" prefix**. That number describes a position inside the list, and the list
is not part of the encode fingerprint, so it would either go stale the moment new videos arrive or force every
video to be re-encoded whenever the list changes. The remaining time depends on the video alone.

Disable it with `--no-countdown`.

The rotation pool takes the newest **30** shorts by default (30 is also the maximum). That is the download cost:
filling all 30 takes roughly 10-15 minutes, and anything already fetched is skipped.

WARNING: the overlay uses `-loop 1`, so the length cap must be the **smaller** of the requested cap and the length
of the base itself. With only a `-t` cap a shorter base is stretched and its tail becomes a frozen last frame
(measured: a 53-second short became 90 seconds).

### The watermark: title plus first-air date

Every video shows "**original title　First aired: YYYY-MM-DD**" at the top right. When the title is too long (wider
than the frame minus the margins) it **automatically becomes a marquee**, scrolling right to left at 120 px/s.

The implementation is in `build_local_content.py`:

- the title comes from `segments[].title` in `playlist-*.json` (fetched per video by `build_playlist.py`)
- the text is rendered to a PNG by `wmtext.py` (PIL plus the system STHeiti font, so CJK works)
- when it fits it uses `overlay=x=W-w-40`; when it does not it becomes `overlay=x='W-mod(t*120,W+w+220)'`

To check that the marquee really moves, take the first few characters of the title as a template, match it at
different times to find its position, and compare against the formula.

**Always add `--keep-raw` while tuning the watermark**: the original downloads stay in `media/.raw/`, so files can
be re-encoded without downloading again and without another quality loss.

### The on-screen button: linking to the original video

Below the title bar (top right) there is a "▶ 看原片" (watch original) button containing a small QR and the short
URL youtu.be/<id>.

**The limitation has to be stated plainly**: the pixels of a live video cannot be clicked, so this is a **visual
hint** rather than a real button. Making a viewer arrive in one click can only come from YouTube itself:

| Method | Clickable | What it needs |
|---|---|---|
| A link in the description | yes | set in Studio, or the YouTube Data API (OAuth) |
| A link in the chat | yes | the YouTube Data API (OAuth) |
| An info card | yes | only by hand in Studio; the API does not support it |
| On-screen button plus QR (this system) | no | already present; scan the code or type the short URL |

The marquee's visible range is "left bound to the left edge of the button":

- **The left bound always frees 1/7 of the frame width** (1280 / 7 is about 182 px) for the source's top-left logo.
  It is not detected automatically because that misjudges easily; override it with `--band-left <pixels>`.
- **The right bound** is the left edge of the link button, which differs slightly per video with the width of the
  URL's characters.

In practice, clamping the start of the text is not enough: as the text scrolls out to the left it still paints over
the logo. So it is a "seamlessly scrollable strip plus a fixed crop window" and the text can never be drawn outside
the window. Verify by running the same filter chain over an all-black clip and checking that no non-black pixel of
any frame falls outside the window.

Disable it with `--no-link-button`. The marquee y moves from 40 to 10 (half a line up).

    python3 build_local_content.py --playlist playlist-tucheng3.json \
      --target 720 --max-seconds 180 --keep-raw

**Verification must decode the encoded video**; pixels on screen are not proof:

`build_transitions.py --verify 5` samples and reports on its own.

The QR holds the short URL `https://youtu.be/<id>` (shorter than watch?v=, fewer modules, easier to scan).

After changing transitions, rebuild the list and restart the playout (the console's "rebuild concat" plus
"restart playout" does the same):

    cd ~/loopcastr
    python3 make_concat_list.py playlist-<mode>-local.json -o concat-<mode>.txt --base-dir ~/loopcastr
    launchctl kickstart -k gui/$(id -u)/com.loopcastr.playout

For a LaunchDaemon install use `system/` instead of `gui/$(id -u)` and prefix the command with `sudo`.

Restarting the playout **starts again from the first segment**, so viewers see the content jump back to the top.

### Audio fade in and out at a segment change

The playout is concat plus `-c copy`, so no filter can be inserted at the seam on the fly: the fade **has to be
baked into every file at landing time**, 2.5 seconds in at the start and 2.5 seconds out at the end
(`AUDIO_FADE` in `build_local_content.py`).

Episodes and transitions **all carry it**, so both sides of a seam close cleanly: the previous segment fades out,
the next fades in, and the wrap back to the top behaves the same. Sources without an audio track are skipped, and
clips of 3 seconds or less get no fade at all so that nothing is left but fades.

Measured (target machine, 2026-09-19): decoding the output audio to PCM, computing RMS every 0.1 seconds and
subtracting the un-faded source window by window gives the actual gain curve:

    fade in   -29.2  -23.9  -20.9  -16.3  -14.8  -13.1  ...  (-7.5 at 1.0 s)  ...  0 dB
    fade out  ...  (-7.6 one second before the end)  ...  last window -24.4 dB

All six segments (3 episodes plus 3 transitions) measured **-7.3 to -7.8 dB at 1.0 second into the fade**, matching
the theoretical -7.5 dB of a 2.5-second linear fade (20*log10(t)). That checkpoint is what distinguishes the
duration: **a 1-second fade would read 0 dB here and a 2.5-second fade reads -7.5 dB**, a 7.5 dB difference that
content variation cannot hide. The transition's title-bar pixels match the previous version (20961 vs 20958 and so
on) and every QR still decodes to the right URL.

Changing the duration only means editing `AUDIO_FADE`; afterwards rerun `build_local_content.py` (with
`--keep-raw` no download is needed; about 100 seconds for 3 episodes) and `build_transitions.py` (about 15 seconds
for 3 transitions).

#### Replacing files while on air: staging first, then an atomic swap

`build_local_content.py --out-dir` and `build_transitions.py --out-dir` write their output to a given directory.
The playout is one concat process with `-stream_loop -1` that **reopens files every loop**, so overwriting a file
that is on air would let it read a half-written file (no moov, ffmpeg cannot open it, and the playout restarts
from the first segment). Write to a staging directory first and `mv` into `media/` after verification: on the same
volume `mv` is an atomic replace, so the playout only ever sees a complete old or new file.

    python3 build_local_content.py --playlist playlist-tucheng3.json --target 720 \
      --max-seconds 180 --keep-raw --out-dir /tmp/stage-ep --out-playlist /tmp/pl.json
    python3 build_transitions.py --playlist playlist-tucheng3.json \
      --shorts-url "https://www.youtube.com/<SHORTS_CHANNEL>/shorts" --shorts-count 30 \
      --seconds 90 --button-caption 去追劇 --parallel 2 --out-dir /tmp/stage-tr

Once verification passes, move the staged files in:

    mv /tmp/stage-ep/*.mp4 media/
    mv /tmp/stage-tr/_tr_*.mp4 media/

WARNING: **keep the staging directory in `/tmp`, not under `media/`.** Measured on 2026-09-19: writing to
`media/.stage*/` made ffmpeg stall while finishing three times in a row (file size frozen, 0% CPU, no moov, main
thread parked in `sch_wait`), while the same work written to `/tmp` finished in 13 seconds. At the same time
`mediaanalysisd` was burning **111% CPU** re-analysing the mp4 files we kept rewriting; `/tmp` is outside the
Spotlight index.

The countermeasure is to add `~/loopcastr` to the Spotlight privacy list. That also gets rid of the constantly
running `mediaanalysisd` (measured: 346 minutes of accumulated CPU time), all of which is wasted when several
channels run.

Also, if a single build gets stuck halfway, **rerunning without `--force` resumes**: whether work has to be done
is decided by whether the file is already in the output directory, so finished episodes are skipped and only the
unfinished one is redone.

### Capacity and resources (fd limit, HLS)

Before pushing up the number of simultaneous channels, deal with two settings that bite around 100 channels.

| Item | Before | Now | Why |
|---|---|---|---|
| `maxfiles` | 256 | **8192** | MediaMTX adds about 2 fds per extra path plus reader (measured: 61 for 1 path, 85 for 13), so 256 blows up at roughly 97 channels |
| MediaMTX `hls` | `yes` plus `hlsAlwaysRemux: yes` | **`no`** | nobody uses HLS, yet every path still does one wasted remux (measured: about +1.1% CPU per path) |

The `hls: no` above is a **multi-channel capacity** trade-off (and happens to be the repo default). For a
single-channel setup, turning `hls: yes` back on adds a "watch the stream" button under the console title so the
output can be viewed directly, at the cost of that ~+1.1% CPU per path.

```bash
sudo launchctl limit maxfiles 8192 unlimited
sudo launchctl bootout system/com.loopcastr.mediamtx
sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.mediamtx.plist
```

The first line raises the system default; only the service layer (the plist) survives a reboot, so the
mediamtx plist also needs `SoftResourceLimits` / `HardResourceLimits` with `NumberOfFiles = 8192`. The last
two lines restart mediamtx so it takes effect.

Measured verification (2026-09-19 06:47-06:50):

- `ulimit -n` in a new shell went from 256 to **8192**
- `:8888` is no longer listening (HLS really is off)
- the YouTube side dropped for about **10-20 seconds** while MediaMTX restarted: health recorded one
  `FAIL no readers` at 06:47:58 and recovered to `OK` at 06:49:04. Both playout and publish reconnected by
  themselves through launchd, with **no manual kickstart**
- WARNING: restarting the playout **starts again from the first segment** (viewers see the content jump back to the
  top). That is the existing behaviour of a playout restart, not something this change introduced

Capacity planning for many channels or for running this as a service for other people is out of scope for this repo.

### The three broadcast modes

Modes are defined in `modes.json` and the whole content chain is built by `mode_build.py`:

| Mode | Videos | Length per video | Shorts pool | Automatic rescan |
|---|---|---|---|---|
| `news` | the newest N videos of the source channel | full length | newest 15 | every 300 seconds |
| `promotion` | the newest 30 videos of the source channel | at most 450 seconds (need not finish) | newest 50 | every 1800 seconds |
| `test` | 3 | 180 seconds | 6 | no rescan |

The table shows the **example values** in `src/modes.json`; the real values are what you fill in under block 1 of
the console and are stored in the deployed `modes.json` (a change takes effect on the next build).

Every mode also has `max_age_hours` (0 means no limit): **only videos first aired within N hours are played**. The
order is "take the first N with `video_limit`, then filter by age", so "only the last 24 hours" needs a
`video_limit` large enough. When the filter leaves nothing, the scan fails outright (`exit 2`) and **does not
overwrite** the existing list: an empty concat list would stop the playout, so it is better to skip this round.

The first-air time on screen uses `YYYY/MM/DD HH:MM` in the machine's time zone, taken from yt-dlp's
`release_timestamp`; failing that it falls back to `timestamp`, and failing that to the date plus `00:00`.

    python3 mode_build.py --mode promotion
    python3 mode_build.py --mode promotion --switch
    python3 mode_build.py --mode promotion --scan-only

    ./switch_edition.sh promotion

The first runs scan, land, transitions, deploy and rebuild; `--switch` then moves the playout over, and
`switch_edition.sh` on its own is enough when the list is already built.

`mode_build.py` deliberately stages: videos go to `/tmp/stage-ep-<mode>/` and transitions to
`/tmp/stage-tr-<mode>/`, and each file is verified for duration before being moved into `media/<mode>/` (why not
write straight to `media/`: the playout is one concat process and overwriting a file that is on air would let it
read a half-written file with no moov). If a build gets stuck or interrupted, **rerunning without `--force`
resumes automatically** (whether work is needed is decided by whether the file is in the staging directory).

#### Going live while still transcoding: `first_batch`

A whole batch has to finish before going on air (55 full-length news videos take hours). With `first_batch` set:

    scan (everything) -> build the first N plus their transitions -> deploy -> rebuild the list from the ready
    subset -> **go live**
      -> next batch -> deploy -> rebuild the list -> wait for the next segment boundary -> restart the playout -> ...

The console field "go on air after this many videos (0 = wait for everything)" is this value. Measured on the
`test` mode (`first_batch=2`): the first batch was on air after about 25 seconds, then each batch extended it,
restarting at the **next segment boundary** every time, so the viewer sees a few videos first and the full list
later without the content jumping back to the top.

Note that **the concat list is read once when the playout starts** (measured: segments appended to the list while
it is playing are never played), so every extension has to restart the playout. Restarting at a boundary is about
not cutting the viewer off, not about avoiding the restart.

The cost: every extension restarts the playout once and **the publisher (YouTube) reconnects once as well** (a few
seconds). Larger batches (`batch_size`, default 10) mean fewer restarts; measured on `.22`, the publish log shows
`connect #N` climbing, which is normal.

#### A rebuild does not re-encode what is already done

Every video and transition records an "encoding parameter fingerprint plus file size". On a rebuild, if the copy in
media is still there with the same fingerprint and the same size it is skipped. Changing quality, watermark or a
length cap changes the fingerprint, and only those files are redone.

    python3 mode_build.py --mode news
    python3 mode_build.py --mode news --force

The first fills in what is missing (unchanged fingerprints are not redone); `--force` redoes everything.

#### Rescanning and handing over

`refreshwatch.py` rescans the source in flat mode according to `refresh_seconds` (a few seconds), compares video
and shorts ids and **rebuilds only when something changed**:

    sudo python3 refreshwatch.py --mode promotion

That runs it in the foreground; it is normally installed as the `com.loopcastr.refresh` service instead.

After a rebuild it **waits for the next segment boundary** before restarting the playout. The concat list is read
only when ffmpeg starts, so an update has to restart the process; waiting for the boundary keeps viewers from
seeing a segment cut in half. The new list is then **rotated** so that the restart continues at the segment that
was next in the old list, which is what keeps the channel from jumping back to the first video. The boundary is
derived from "start time plus summed segment lengths" (the same calculation as `loopwatch.py`).

If the current position cannot be mapped into the new list, nothing is restarted at all: the playout keeps running
the list that is on air and the build log says why. That is deliberate - a channel that keeps playing the old list
is better than one that suddenly restarts from the top.

Restarting a system-domain service needs root, so run this as root (the same reason as `com.loopcastr.health`); as
a non-root user it falls back to `SUDO_PASS`.

#### Known trade-offs

1. **Content is already one copy per mode** (`media/<mode>/`, see "Where the content lives" above). The same video
   can have a different length cap per mode without overwriting anything. Only the inputs are **shared**:
   `media/.raw/` and the shorts pool.
2. **The shorts rotation has a ceiling.** The pairing is "pass p, episode i uses pool entry
   `(p x episodes + i - 1) % N`", and `passes` defaults to `ceil(pool / episodes)` capped at 5. So the first
   "episodes x passes" entries always come up, while the rest do not appear in that round (for example 30 episodes
   with 200 shorts uses only the first 150). To rotate through everything, raise `shorts_passes` or lower
   `shorts_count`.
3. **Playback order is a fixed loop.** A rebuild takes the first `video_limit` videos in source order (newest first
   for a channel) and every round after that keeps the same order; new uploads only appear after a rescan, and when
   they do the whole order shifts forward.

### Services: check whether they are loaded first

`com.loopcastr.publish` is mandatory for anything to reach YouTube. Without it the picture only reaches MediaMTX:
the console's "watch the stream" shows content while YouTube stays black, because nothing is connected to the
YouTube ingest at all.

The Services block at the bottom of the console shows three states:

| Display | Meaning | What you can do |
|---|---|---|
| running pid N | launchd has the job and the process is there | restart |
| loaded (not running) | the job is there and KeepAlive is bringing the process up | restart |
| not loaded | launchd does not have the job at all | **start**: copy `~/loopcastr/<label>.plist` to `~/Library/LaunchAgents` and `launchctl bootstrap` it |

"Not loaded" means the plist is in the data directory but was never registered with launchd (a minimal install
that only set up three services, for example). Check it yourself with:

    launchctl list | grep loopcastr

`com.loopcastr.health` and `com.loopcastr.refresh` were designed as system-domain services (restarting the playout
needs root) and the console does not run as root, so those two are up to you:

    sudo cp ~/loopcastr/com.loopcastr.health.plist /Library/LaunchDaemons/
    sudo launchctl bootstrap system /Library/LaunchDaemons/com.loopcastr.health.plist

After changing the stream key the publisher has to be restarted for it to take effect (`yt_publish.sh` reads the
key file once at startup): press restart on the publish service in the Services block, or

    launchctl kickstart -k gui/$(id -u)/com.loopcastr.publish
