# 24/7 YouTube live channel system specification v1.2

> This specification is in English. The day-by-day measurements behind it are in the
> [changelog](changelog.md), which is in Traditional Chinese.

## 0. Document information

| Item | Contents |
|---|---|
| Document | 24/7 YouTube live channel system specification |
| Version | v1.2 (work in progress; supersedes v1.0 and v1.1) |
| Date | 2026-09-16 |
| Status | **Work in progress**: M1 / M2 / M3 are measured, passed and deployed to the target machine; **M4 is under way** (53 videos being landed) |
| Basis | v1.0 and v1.1 plus this round's measurements on the target machine (2026-09-16 20:00-20:15) |
| Writing rule | Only **measured** or **decided** content; unmeasured items are marked [to verify], derived ones [inferred] |

The four substantive corrections in v1.2 over v1.1:

1. **R-09 corrected (the important one)**: v1.1 said anonymous resolution was always blocked and only cookies could get through. That is **wrong**. The same batch of videos failed completely at 05:11 and succeeded at 20:02 under identical conditions, so it is a **temporary IP flag**, not a permanent block, and **no cookie is needed**. There is a fallback during a block: `player_client=android` (capped at 360p).
2. **A necessary step was missing: content normalisation.** Measured: the source parameters are inconsistent (1280x720 / 1280x718 / another batch at 480p) and concat's `-c copy` cannot splice them directly. Landing has to convert to uniform parameters (3.6).
3. **Two silent bugs fixed**: ffmpeg reading stdin truncated the output (137s / 163s with no error message at all); the HLS (m3u8) path covered only half a video.
4. **M4 started**: 53 videos being landed.

---

## 1. Project goals and scope

### 1.1 Goals

Turn the licensed YouTube work of a single group (a single creator) into a **24/7 uninterrupted** live channel, with the ability to **relay** an external live stream into it.

### 1.2 In scope

1. Obtain the source media from YouTube. After R-09 this means **landing it as local files** first and broadcasting from there.
2. Play continuously in playlist order, around the clock without interruption.
3. No interruption at the receiver between segments, or between one round and the next.
4. Recover automatically when a source fails, stalls or has to be resolved again.
5. Push the output to the YouTube live ingest.
6. Relaying: pull in a YouTube signal that is already live and push it out again.

### 1.3 Out of scope (explicitly excluded, not re-evaluated)

| Excluded | Reason | Source |
|---|---|---|
| SPX-GC | the user decided against it | updated requirements |
| CasparCG | no Apple Silicon support | updated requirements |
| OBS plugins (existing or self-written) | the user decided to implement it directly | v3 conclusion |
| Liquidsoap as the **decision layer** | it passed all three gates but bought no extra capability and needed 10 dependency upgrades on the work machine | v4.5 |
| Building a transcoding farm | the target machine is a single 8 GB M1 | - |

---

## 2. Glossary

| Term | Definition |
|---|---|
| **source** | One piece of media to be broadcast; in relay mode it is a direct URL resolved by yt-dlp |
| **segment** | One entry in the playlist, of type `vod` / `live` / `filler` / `file` |
| **resolve** | Turn a videoId or watch URL into a directly playable media URL with yt-dlp |
| **relay** | Each segment is published by **its own** ffmpeg process in turn |
| **concat** | **One** ffmpeg reads the whole list in order, with no change of publisher |
| **landing** | Fetch a source to a local file `media/<id>.mp4` in advance, so playout never touches a network source |
| **takeover** | The new publisher attaches first and the old one is then dropped; MediaMTX takeover happens within the same second |
| **filler** | A backup clip used when a source is unavailable |
| **publisher chain** | The fixed three hops `playout.sh -> MediaMTX -> yt_publish.sh -> YouTube` |
| **work machine** | The user's daily MacBook (`<WORK_HOST>`) |
| **target machine** | The target machine (hostname `<HOSTNAME>.local`, <MODEL>, M1, 8 GB, macOS 27.0) |

---

## 3. System architecture

### 3.1 Components

| Component | Implementation | Role |
|---|---|---|
| Master list | `playlist.json` | Describes what to play, the order, and each segment's source and length (the source is a YouTube URL) |
| Landing | `build_local_content.py` | Fetch the master list as local `media/<id>.mp4`, measure with ffprobe, produce `playlist-local.json` |
| List conversion | `make_concat_list.py` | `playlist-local.json` -> ffmpeg `concat.txt` |
| **Playout** | `playout.sh` | One concat process looping the list -> MediaMTX |
| **Publisher** | `yt_publish.sh` | One long-lived ffmpeg, MediaMTX -> YouTube ingest |
| Relay engine (relaying) | `relay.py` | Resolve sources, schedule, takeover handover, watchdog, URL lifetime, event log |
| Resolver | `yt-dlp` | watch URL -> direct media URL (**currently blocked by R-09**) |
| Local media hub | `MediaMTX` | Accept RTMP, provide the takeover that makes handover gapless, provide HLS for acceptance and an HTTP API for observation |
| Encoding / muxing | `ffmpeg` | Mostly remux (`-c copy`), transcoding only when necessary |
| Acceptance observation | `gapwatch.py` | Independently poll the API and report receiver-offline windows and `bytesReceived` zero-growth windows |
| Process supervision | `launchd` | `com.loopcastr.mediamtx` / `com.loopcastr.playout` / `com.loopcastr.publish` / `com.loopcastr.health` |
| Health monitoring | `healthcheck.py` | Every 60 seconds check that things are really moving: path ready, has readers, `bytesReceived` growing. Alerts on state changes, re-reminds every 30 minutes while a fault persists; the YouTube side is checked at most every 15 minutes (too many public lookups get the bot flag back) |
| Captions / transitions (optional) | Liquidsoap or HTML->PNG->overlay | see FR-11 in v1.0 |

### 3.2 Data flow

**Main line (local files, the normal 24/7 path)**

    playlist.json (YouTube URLs)
         |
         |  build_local_content.py  <- needs cookies.txt (R-09), done once offline
         v
    media/<id>.mp4  +  playlist-local.json
         |
         |  make_concat_list.py
         v
    concat.txt
         |
         |  playout.sh: one ffmpeg
         |    -re -f concat -safe 0 -stream_loop -1 -i concat.txt -c copy
         v
    MediaMTX  rtmp://127.0.0.1:1935/live/main
         |          +-- HTTP API :9997 (observation)
         |          +-- HLS :8888 (acceptance)
         |          +-- yt_publish.sh: one long-lived ffmpeg -c copy
         v
    YouTube ingest  rtmp://a.rtmp.youtube.com/live2/<stream key>

**Branch line (relaying, or when the source has to change)**

    relay.py --resolve--> yt-dlp --> direct URL
         |
         | ffmpeg (copy), one publisher per segment, handed over by takeover
         v
    MediaMTX --> yt_publish.sh --> YouTube

[measured] The two lines can coexist: `relay.py` and `playout.sh` both publish to the same `live/main`, and the MediaMTX takeover completes the handover within the same second.

[measured] "Pushing to YouTube" **must** be started downstream of MediaMTX; the playout engine must not point its target straight at YouTube. Reason: `relay.py`'s `api_path_of()` only takes the takeover path for `127.0.0.1` / `localhost`, so pointing the target at YouTube degrades the handover to `cut`, and the YouTube ingest does not accept two publishers at once.

### 3.3 Deployment topology

| Role | Machine | Notes |
|---|---|---|
| Development / test | work machine `<WORK_HOST>` | development, off-air tests |
| **Production** | **target machine** | always on; playout, publisher and media hub all live here |
| External | both share one NAT | the public IP is dynamic; measured this round as `<PUBLIC_IP>` |

[measured] Both machines share the gateway (`<GATEWAY>`), the NAT and the public IP. **So the R-09 bot block has nothing to do with trying another machine**: moving to the target machine is blocked just the same.

[measured] The public IP is dynamic: earlier the same day it was `<PUBLIC_IP>` and this round `<PUBLIC_IP>`. Once the content is landed this risk **no longer applies to the main line** (playout no longer depends on a source URL); it only affects the relaying branch.

### 3.4 Architecture decision record

| Decision | Outcome | Reason |
|---|---|---|
| Build it ourselves | yes | user instruction |
| Replace the control layer with a playout engine | no | `relay.py` already implements and has measured event-driven handover |
| Use MediaMTX as the output hub | **required** | takeover is only available in MediaMTX and completes within the same second (`closing existing publisher` and `stream is available and online` in the same second) |
| Push straight to YouTube RTMP | no | see the note in 3.2 |
| **concat or relay for local files** | **concat** | [measured] the relay leaves a 2.0-3.1 second gap per segment; concat leaves 0 at both a segment change and the wrap. See 3.5 |
| **Land the content offline** | **yes** | resolution is blocked **intermittently** (R-09), and landing also removes the 6-hour URL expiry and mid-stream googlevideo resets. Landing must normalise, see 3.6 |
| **A single long-lived publisher** | **yes** | any handover upstream happens inside MediaMTX, so YouTube only ever sees one connection |
| Does the caption layer need freetype | no | neither ffmpeg has freetype, so it takes path A (HTML -> PNG -> overlay) |

### 3.5 Choosing the playout engine: concat vs relay (the core measurement of this round)

The same "3 local files, 36 seconds total, looping" measured side by side:

| Playout style | Measured gaps | Measurement window |
|---|---|---|
| `relay.py` handover | 4 gaps totalling **7.793 seconds** (cold start 3.066 / segment change 2.053 / segment change 2.040 / filler takeover 0.635) | about 40 seconds |
| `playout.sh` concat | **1 gap, 2.546 seconds (cold start only)**; 0 at a segment change and at the wrap | 70 seconds (two rounds) |
| `playout.sh` concat (target machine) | **0 gaps** | 55 seconds (two rounds) |

Root cause: [measured] `PUBLISH_TAIL` (originally meant to extend the output destination's `-t`) **cannot extend a local file source**. The file ends at EOF -> MediaMTX drops the publisher immediately -> the receiver is offline for 2 seconds while the next publisher warms up. It is the **source** side that ends first, not the output side, so no output parameter can save it.

[measured] The concat wrap really happens: the log shows `Non-monotonic DTS ... 48000` and `57000` (36+12 and 36+21 seconds, the t1->t2 boundary of the second round), and there is no offline window there.

**Known side effect**: the concat demuxer emits `Non-monotonic DTS` warnings at every seam; the measured audio timestamp overlap is about **11 ms**, ffmpeg clamps it back and there is no audible effect. When a perfectly clean timeline is required, splice offline instead:

    ffmpeg -hide_banner -f concat -safe 0 -i concat.txt -c copy media/all-in-one.mp4

Playout then reads the single file (`-stream_loop -1 -i media/all-in-one.mp4 -c copy`) and the live path never touches the concat demuxer at all.

### 3.6 Landing normalisation (a necessary step added this round)

[measured] The source encoding parameters of this content are **inconsistent**:

| Video | Resolution |
|---|---|
| `Ig3vtqtXowY` | 1280x720 |
| `YlC65MH0xoc` | **1280x718** |
| most episodes in the example list | 640x360 (native 480p class) |

The concat demuxer with `-c copy` requires every segment to have identical parameters; mixing them produces errors at the seam or fails the whole run. So the conversion to uniform parameters has to happen **at landing time** rather than being left to the playout (transcoding in the playout would keep an 8 GB target machine fully loaded around the clock):

    python3 build_local_content.py --target 720
    # output is uniform: H.264 High L3.1 / 1280x720 / yuv420p / 30fps / AAC-LC 48kHz stereo / +faststart

[measured] After the fix, 5 files were spot-checked (including 720p and 718p sources): the output parameters are identical and `-c copy` splices correctly.

### 3.7 Two silent traps in the landing pipeline (fixed this round)

| Trap | Symptom | Root cause | Fix |
|---|---|---|---|
| ffmpeg reads stdin | **the same video came out at 137s and at 163s**, with no error message and exit code 0 | the script often runs through an ssh heredoc, so ffmpeg inherits the script itself as stdin; ffmpeg treats it as interactive commands and exits normally on reading `q` | add `-nostdin` and pass `stdin=DEVNULL` to `subprocess.run` |
| the HLS path gives only half a video | `Ig3vtqtXowY` fetched only 137s while the DASH path gives the full 270.374s | the same video offers both `m3u8` (HLS) and `https` (DASH) streams, and the HLS one is incomplete | restrict the format with `[protocol^=https]` |

A **duration comparison after download** was added as well: a difference of more than 5% (or 10 seconds) from the length the list claims triggers one automatic refetch, and failing that a switch to the `android` client, so half a video cannot slip into concat.

### 3.8 A content-layer problem: black tails (added this round)

The three layers of the playout chain each see something different, and this is worth spelling out because the first problem of this round slipped past the first two:

| Check | What it measures | Sees | Cannot see |
|---|---|---|---|
| `gapwatch.py` | MediaMTX `ready` / `bytesReceived` | a break in the transfer | the picture content |
| PTS / packet analysis | the concat output timeline | a hole in the timeline | the picture content |
| `blackdetect` | actual pixel brightness | black frames | - |

[measured] `8jtdcMDuV_A` has **67.4 seconds** of pure black at the tail (starting at 551.57s). The playout is perfectly fine: the transfer is continuous, 436,600 packets with no hole, `framesInError=0`. The viewer just sees black.

And its recovery point sits almost exactly at the end of the video (551.57+67.4 = 618.97 against a total of 618.9), so it **looks like it was caused by a segment change** and is easily misread as a splice problem.

Countermeasure: `build_local_content.py` has built-in black-tail detection (`--black-tail-min`, default 5 seconds) which runs at landing and during `--rescan`. When it finds one it records it in the manifest and writes an `outpoint` into `playlist-local.json`, which `make_concat_list.py` turns into a concat demuxer trim command, **with no re-encoding**. Use `--no-auto-trim` to mark without trimming.

[measured] After applying it the segment's playing length dropped from 618.9s to 551.57s and the whole list from 14,530s to **14,487s**; rerunning the timeline analysis gave 436,600 -> 434,583 packets (2,017 frames fewer, about 67.2 seconds) with the **forward gap still 0**.

Note: an early version of `make_concat_list.py` rounded each segment's seconds and then summed them, accumulating about 24 seconds of error and reporting 14,463s; it now sums in floating point and matches the real playing time.

---

## 4. Functional requirements

Priority: **P0** = required for production; **P1** = after launch; **P2** = optional. Status updated to 2026-09-17.

| Id | Requirement | Priority | Status | Acceptance basis |
|---|---|---|---|---|
| FR-01 | Resolve a watch URL / videoId into a direct media URL, with client fallback | P0 | **pass** | resolution takes 2.26-2.65 seconds; during R-09 it falls back to the `android` client automatically |
| FR-02 | Describe the content in `playlist.json`, supporting `vod` / `live` / `filler` / `file` | P0 | **pass** | schema in 6.2 |
| FR-03 | Play continuously in order and return to the first segment automatically | P0 | **pass** | [measured] multiple rounds loop without a seam |
| FR-04 | A handover or segment change must not break the receiver | P0 | **pass (concat)** | [measured] 0 gaps at a segment change and at the wrap; in relay mode 2.0-3.1 seconds per segment |
| FR-05 | Let a filler take over the picture when a source stalls | P1 | **not needed on the main line** | the main line plays local files, so "a stalled source" does not exist; this only applies to the relaying branch (`relay.py`) and is pending T-08 |
| FR-06 | Watchdog: reopen a segment when the output stalls past the threshold | P0 | **replaced by other mechanisms on the main line** | the main line's three layers of protection are `launchd KeepAlive` (bring a dead process back), `healthcheck.py --heal` (restart the playout after 3 minutes without traffic growth) and the MediaMTX `readTimeout`. `relay.py`'s watchdog is only needed on the relaying branch |
| FR-07 | Source URL lifetime management | P0 | **not needed on the main line** | after landing, no source URL is involved; the relaying branch still needs it |
| FR-08 | Relay mode: long `type=live` sources with periodic handover | P1 | **testable (R-09 lifted)** | anonymous resolution works again, so `relay.py`'s `type=live` can really be verified; needs D6 to name the source |
| FR-09 | JSONL event log plus API traffic observation | P1 | **pass** | `relay-events.jsonl` / `gapwatch.py` |
| FR-10 | Push the final signal to the YouTube ingest | P0 | **pass** | see T-14 |
| FR-11 | Captions / bug / subtitles | P2 | **possible, but it overturns copy-only** | neither machine has freetype, so only path A (HTML -> PNG -> overlay) works, and overlay has to **decode and re-encode**, which would keep an 8 GB target machine fully loaded. This trade-off has to be decided first |
| FR-12 | Alerts on repeated failure | P2 | **code ready** | `healthcheck.py` already writes `alerts.jsonl` and the outbound hook exists (`~/loopcastr/alert_webhook`); waiting on D8 for a URL |
| **FR-13** | **Land the sources as local files, normalise them and verify their duration** | **P0** | **done** | all 53 landed (4.7 GB), parameters uniform, durations verified, black-tail detection in place (T-16 / T-17 / T-18) |
| **FR-14** | **A single long-lived publisher on the YouTube side** | **P0** | **pass** | measured over 45 seconds without a break |
| **FR-15** | **Playout and publisher each supervised by launchd and starting at boot** | **P0** | **pass** | moved to LaunchDaemons; after a reboot the whole chain recovers with nobody logged in (T-11) |
| **FR-16** | **Black-tail detection and trimming** | **P0** | **pass** | `--rescan` plus `outpoint`; see 3.8 and T-18 |
| **FR-17** | **Transition clips (after every episode)** | **P1** | **pass** | inserted automatically when `media/_transition.mp4` exists; see 6.2 and T-22 |

---

## 5. Non-functional requirements

| Id | Item | Requirement | Basis |
|---|---|---|---|
| NFR-01 | Memory | 8 GB on the target machine; only one main encoding or copy path at a time | [measured] 8 GB target machine |
| NFR-02 | Disk | [measured] one landed round is **4.7 GB** (106 segments, 15,549 seconds at 720p); 26 GiB free on the target machine | [measured] 2026-09-17 |
| NFR-03 | Bandwidth | upload must accommodate more than 1.5x the output bitrate | v4.3 |
| NFR-04 | Availability | 24/7. Recovery on the main line is defined by three mechanisms: `KeepAlive` brings a dead process straight back; `healthcheck --heal` restarts after three consecutive failures (about 3 minutes) without traffic growth; a reboot recovers automatically (measured interruption 25 seconds) | updated 2026-09-17 |
| NFR-05 | Resolution latency | a single resolution takes 2.26-2.65 seconds | [measured] |
| NFR-06 | Boot start | `launchd` **LaunchDaemon**: starts at boot (**no graphical login needed**) and restarts on exit | [measured] T-11 |
| NFR-07 | Security | the stream key must never be committed | section 9 |
| NFR-08 | Observability | structured event log and a traffic query endpoint | FR-09 |

---

## 6. Interface specification

### 6.1 `relay.py` command line (relaying / multi-source handover)

New or retained in v1.1:

| Option | Default | Purpose |
|---|---|---|
| `--cookies` | auto-finds `<project>/cookies.txt` | pass cookies.txt when a source is blocked by the bot check |
| `--check` | off | do not broadcast; resolve every source once and report availability (exit code 2 when everything fails) |
| `--loop` | `1` | how many rounds through the whole list (`0` = forever); rounds use the same takeover |
| `--playlist` / `--target` / `--clients` / `--only` | - | list and target overrides |
| `--observe` / `--overlap` / `--resolve-lead` | `0.0` / `4.0` | observation and handover lead |
| `--watchdog` / `--startup-grace` / `--flow-threshold` | `3.0` / `12.0` / `5.0` | the three watchdog parameters |
| `--max-restarts` / `--retry-interval` | `6` / `8.0` | retry policy |
| `--filler-on-stall` | `assets/transition.mp4` | the filler used on a stall |
| `--url-max-age` / `--url-expiry-margin` | `1800.0` / `300.0` | URL lifetime |
| `--restart-mode` | `auto` | `takeover` / `cut` / `auto` |
| `--dry-run` | off | print without executing |

**Recommended production values** (measured in v4.3):

    --watchdog 10 --startup-grace 15 --flow-threshold 12 \
    --url-max-age 1800 --url-expiry-margin 600 \
    --max-restarts 20 --retry-interval 8

cookies precedence: command line > the playlist's `cookies` field > `<project>/cookies.txt`.

### 6.2 `playlist.json` schema

    {
      "target": "rtmp://127.0.0.1:1935/live/main",
      "clients": ["web_embedded", "mweb", "default", "android_vr"],
      "segments": [
        { "id": "v1", "type": "vod",    "url": "https://www.youtube.com/watch?v=<id>",
          "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b",
          "mode": "copy", "seconds": 271 },
        { "id": "f1", "type": "filler", "path": "assets/transition.mp4", "seconds": 4 },
        { "id": "l1", "type": "live",   "url": "https://www.youtube.com/watch?v=<id>",
          "format": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b",
          "mode": "copy", "seconds": 40 },
        { "id": "t1", "type": "file",   "path": "media/t1.mp4", "mode": "copy", "seconds": 12 }
      ]
    }

| Field | Notes |
|---|---|
| `segments[].type` | `vod` (finite material) / `live` (live source) / `filler` (local file, no handover) / **`file` (local file, handed over by takeover)** |
| `segments[].format` | the yt-dlp format selector. **It must contain `[ext=mp4]+ba[ext=m4a]`**, or it picks opus audio which cannot go into FLV |
| `segments[].seconds` | **required**. The scheduler derives `stop_at` / `start_at` with `float(spec["seconds"])` and raises `KeyError` without it. Put the real length for a VOD; a longer value makes ffmpeg end early -> `early_exit` -> switch to filler -> replay the same segment |
| `segments[].path` | for `file` / `filler`, relative to the project root |
| `segments[].outpoint` | optional, paired with `file`: stop reading that file at this second (a concat demuxer command). Used to trim a black tail, see 3.8; **no re-encoding needed** |
| **Transition clips** | not in `playlist.json`; their presence is decided by whether `media/_transition.mp4` exists. When it does, `build_local_content.py` inserts one **after every episode** (including after the last, so the wrap has one too). Replace it with `--transition <url\|id\|path>`, disable it with `--no-transition` |

### 6.3 Playout `playout.sh` (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `PLAYLIST` | `<dir>/playlist-local.json` | input list |
| `LIST` | `<dir>/concat.txt` | the generated concat list |
| `DEST` | `rtmp://127.0.0.1:1935/live/main` | output |
| `LOG` | `<dir>/logs/playout.log` | log |
| `NORMALIZE` | `0` | `1` = re-encode to uniform parameters (only when sources differ) |
| `VENC` / `AENC` | `libx264 ...2.5M` / `aac 128k` | only used when `NORMALIZE=1` |

Behaviour: it builds the concat list at startup and **stops if any segment file is missing (exit code 78)**, so half a list is never broadcast.

### 6.4 Publisher `yt_publish.sh` (environment variables)

| Variable | Default | Notes |
|---|---|---|
| `SRC` | `rtmp://127.0.0.1:1935/live/main` | upstream |
| `KEYFILE` | `<dir>/stream.key` | stream key file (`chmod 600`) |
| `PUSH_URL` | `rtmp://a.rtmp.youtube.com/live2` | the ingest endpoint |
| `API` / `PATH_NAME` | `http://127.0.0.1:9997` / `live/main` | used to wait for the upstream to be ready |

Behaviour: it waits for the upstream to be `ready:true` before connecting to YouTube (**so the ingest is not hit every 3 seconds while there is no content**); `-c copy`, `-flvflags no_duration_filesize`; reconnects 3 seconds after a drop.

### 6.5 Event log

[measured] `emit()` appends JSONL line by line, each entry carrying `ts` (epoch seconds), `wall` (hh:mm:ss.mmm) and the fields of that event.

### 6.6 MediaMTX configuration (measured on the target machine)

| Item | Value |
|---|---|
| API | `127.0.0.1:9997` |
| RTMP | `:1935` |
| HLS | `:8888` (`hlsVariant: lowLatency`, `hlsAlwaysRemux: yes`) |
| `readTimeout` | `30s` (deliberately raised so the playout owns the handover instead of MediaMTX firing first) |
| Version | `v1.21.0` |

### 6.7 YouTube input and output interface

| Item | Value |
|---|---|
| Channel account | `<GOOGLE_ACCOUNT>` |
| Channel name | the user specified a name; [measured] the channel's **actual display name on YouTube differs from the specified one** (`<CHANNEL_ID>`) and its `@handle` and `/c/` path both return 404 -> see D15 |
| Live control room | `https://studio.youtube.com/video/<LIVE_VIDEO_ID>/livestreaming` |
| RTMP endpoint | `rtmp://a.rtmp.youtube.com/live2` |
| stream key | stored at `target machine:~/loopcastr/stream.key` (`chmod 600`). **Never written into this document and never committed**; this document also deliberately records no fingerprint or length characteristics. Note that `yt_publish.sh` **reads it once at startup**, so a new key needs `launchctl kickstart -k` |
| Public broadcast | [measured 2026-09-16 21:02] **achieved**: `<LIVE_VIDEO_ID>` returns `live_status=is_live` and `is_live=True`; YouTube has built the full 144p-720p transcoding ladder (720p at 2448 kbps), proving it is receiving real picture |
| Key rotation | [measured] after overwriting the key at 21:01:39 and running `kickstart`, it reconnected to the ingest **within 20 seconds**; a following 120-second measurement showed 0 offline windows |

---

## 7. Parameter baseline table (all measured)

| Parameter | Measured value | Meaning |
|---|---|---|
| Source URL TTL | 21600 seconds (6 hours) | expires after that (no longer used by the main line) |
| Source URL binding | the public IP at resolve time | a new egress invalidates it (no longer used by the main line) |
| Single resolution | 2.26-2.65 seconds | lower bound for the handover lead |
| MediaMTX takeover completion | within the same second | the precondition for a gapless handover |
| **concat segment change / wrap gap** | **0 seconds** | 55 seconds covering two rounds, 0 offline windows |
| **relay segment change gap** | **2.0-3.1 seconds per segment** | the file ends at EOF and the publisher is dropped |
| concat cold-start gap | 2.55 seconds | once at the opening |
| relay cold-start gap | 3.07 seconds | - |
| concat seam DTS overlap | about 11 ms | ffmpeg clamps it back; no audible effect |
| Watchdog lower bound / recommended | 6 seconds / 10 seconds | below 6 seconds it misfires |
| Startup grace | 15 seconds | `--startup-grace` |
| Flow stall threshold | 12 seconds | `--flow-threshold` |
| Test material output bitrate | 3.21 Mbps | testsrc2 compresses very badly; real video is much lower |
| Disk needed for one landed round | about 4.5-5.5 GB | 14,582 seconds at 720p [inferred] |
| Public IP | `<PUBLIC_IP>` (earlier the same day `<PUBLIC_IP>`) | dynamic |

**Known unfixed defect**: during startup the same URL is resolved twice, wasting about 2 seconds each time.

---

## 8. Environment and hardware

### 8.1 Target machine (measured 2026-09-16 05:11)

| Item | Value |
|---|---|
| Model | `<MODEL>` (hostname `<HOSTNAME>.local`) |
| Chip / architecture | Apple M1 / `arm64` (8 CPU) |
| Memory | 8 GB |
| OS | macOS 27.0 |
| Disk | 228 GiB total, **37 GiB** free, 26% used |
| Homebrew | `7.0.2` |
| `ffmpeg` | `9.0.1`; **has libx264 / libx265 / h264_videotoolbox / aac** (correcting v1.0: the earlier "no libx264" was wrong) |
| `yt-dlp` | `2026.08.19` |
| `python3` | `3.14.7` (Homebrew) |
| `node` | `v26.8.2` |
| `mediamtx` | installed, `launchd com.loopcastr.mediamtx` resident (v1.21.0) |
| `streamlink` | **not installed, and not needed** |
| ffmpeg freetype | none (`drawtext` returns 0) -> the caption layer takes path A |
| Deployed to `~/loopcastr` | `relay.py`, `playout.sh`, `yt_publish.sh`, `make_concat_list.py`, `build_local_content.py`, `gapwatch.py`, `mediamtx.yml`, `stream.key`, `playlist.json` |
| Service management | [measured 2026-09-17] the four services were moved from LaunchAgents to **LaunchDaemons** (`/Library/LaunchDaemons/`). `mediamtx` / `playout` / `publish` set `UserName=<USER>`; `health` **runs as root**, because only root can `kickstart` the system domain. They start at boot with no graphical login |
| FileVault | [measured] **off** (2026-09-17 00:07), so a boot no longer needs a human to unlock the disk |
| Sleep | [measured] `pmset -a sleep 0 disksleep 0 disablesleep 1`, `SleepDisabled 1`. It used to be `sleep 1`, held off only by a `caffeinate` |
| Outstanding | `assets/transition.mp4` (D5, only needed by the relaying branch), and an actual reboot verification (D17) |
| POT provider | `~/pot` (bgutil 2.0.0) and the plugin are installed, **but measurement proves it does nothing against this block** (R-09) |

### 8.2 Work machine (development and acceptance)

| Item | Value |
|---|---|
| Address | `<WORK_HOST>` |
| `ffmpeg` | `9.0.1` (**no freetype**, has libx264) |
| `yt-dlp` | `2026.08.19` |
| `mediamtx` | installed (started locally during acceptance) |
| `python3` | `3.14.7` |
| `node` | `v22.23.0` |
| Homebrew | `7.0.2`; the user ran `brew upgrade` and **this round's recheck found no brew process running** |
| Chrome profile | [measured] it exists but **macOS TCC blocks it** (`Operation not permitted`), so `--cookies-from-browser chrome` fails |

---

## 9. Security and compliance

1. **Licensing**: the user states that every YouTube video used is properly licensed. Landing them as local files is **for playout stability**, not to circumvent licensing.
2. **Stream key handling**:
   - never written into `playlist.json` or any committed file;
   - provided as a separate file with `chmod 600` (`target machine:~/loopcastr/stream.key`);
   - masked in logs; the full RTMP URL must never be printed. `yt_publish.sh` only writes the name of the connection target, never the key.
3. **cookies.txt handling**: equally sensitive (it is equivalent to a logged-in session). `chmod 600`, never committed, and rotated when it has been used.
4. **Accounts**: the channel account is `<GOOGLE_ACCOUNT>`. This document stores no password, key or cookie.
5. **Compliance**: responsibility for content copyright and any reporting rests with the channel owner.

---

## 10. Test plan

### 10.1 Test resources

| Resource | Contents |
|---|---|
| Playlist | `https://www.youtube.com/playlist?list=<PLAYLIST_ID>` ("example list", channel <CHANNEL_NAME> `<CHANNEL_HANDLE>`) |
| Segments / total | 53 segments / 14,582 seconds (4 hours 3 minutes 2 seconds) |
| Segment length spread | shortest 91 seconds / longest 619 seconds / average 275 seconds |
| Test material | `target machine:~/loopcastr/media/t1.mp4` (12 seconds), `t2.mp4` (9 seconds), `t3.mp4` (15 seconds), `testsrc2` 1280x720@30 plus a sine tone, uniform parameters so `-c copy` works |

### 10.2 Test items and results

| Id | Item | Method | Result |
|---|---|---|---|
| T-01 | Single segment playout | push one local clip to MediaMTX | **pass** |
| T-02 | Consecutive handover | relay 3 segments with `--observe` | **pass, but exposes the defect**: 2.0-3.1 seconds per segment change |
| T-03 | Handover measurement | MediaMTX API | `inboundFramesInError: 0` |
| T-04 | Watchdog | cut the source by hand | [to verify] |
| T-05 | Filler takeover | stall the source | [to verify] (material undecided, D5) |
| T-06 | URL lifetime | run past `--url-max-age` | not needed on the main line |
| T-07 | A real list through a full round | test edition, 106 segments, 2 hours 56 minutes | **pass (and measured)**: [measured] at the loop point of 2026-09-17 05:45:56, `loopwatch.py` took 650 samples over 135 seconds and found **0 receiver-offline windows and 0 `bytesReceived` zero-growth windows**. The test edition ran about 10 hours (3.4 rounds), confirming that repeated wraps are fine. It was switched back to production afterwards with `switch_edition.sh live` |
| T-08 | Relaying | pull a live source with `type=live` | **pass**: measured against the TTV News 24-hour live stream (`9iRAqBMakXY`), 3 rounds x 120 seconds. **The two handovers themselves left 0 gaps**; the 6.206 seconds of offline measured was the cold-start period of "stop the playout -> relay finishes resolving", not the handover |
| T-09 | YouTube output | push to the ingest | **pass** (the ingest accepted the stream, held for over 45 seconds) |
| T-10 | Long-run stability | 72 consecutive hours | [to verify] |
| T-11 | Boot start | an actual reboot with nobody logging in | **pass**. The three original obstacles (FileVault on, no automatic login, `sleep 1`) were removed. After the reboot: `up 3 mins, **0 users**` (no graphical login at all), all four services came up by themselves, the processes run as `<USER>`, the chain reports `ready=True err=0 readers=1` and YouTube recovered to `is_live`. **Playout was interrupted for about 25 seconds** (command at 00:17:37 -> publisher reconnected at 00:18:04) |
| T-12 | Final gap on the YouTube side | long-running picture monitoring of the YouTube side | **in progress**: `yt_side_monitor.py` has been monitoring the YouTube output continuously for 3 hours since 2026-09-17 12:38 (blackdetect plus freezedetect), covering dozens of segment changes. It has already caught one 4.2-second frame freeze; whether that is a still picture in the content or a real freeze is still to be determined |
| **T-13** | **Gapless concat loop** | 55 consecutive seconds covering two rounds on the target machine | **pass**: 0 offline windows, 0 zero-growth windows |
| **T-14** | **End-to-end publishing** | the whole chain on the target machine with the real stream key | **pass**: `ready/online=true`, `readers=1`, publisher continuous |
| **T-15** | **Anonymous resolution availability** | the same batch of videos tested with `yt-dlp --simulate` at 05:11 and at 20:02 | **everything failed at 05:11 and everything succeeded at 20:02** -> R-09 is a temporary flag. During a failure `player_client=android` still works (360p) |
| **T-16** | **Landing parameter consistency** | compare the codec parameters of every landed segment | **pass**: H.264 High L3.1 / 1280x720 / yuv420p / 30fps / AAC-LC 48k, spliceable with `-c copy` |
| **T-17** | **Landing duration correctness** | compare against the length the list claims after download | **pass (after the fix)**: with the two bugs from 3.7 fixed, `Ig3vtqtXowY` went from 137s to **270.374s**, matching the source |
| **T-18** | **Black-tail detection and trimming** | run `blackdetect` over all 53, apply `outpoint`, then rerun the timeline analysis | **pass**: caught the 67.4s black tail of `8jtdcMDuV_A` and trimmed it; packets 436,600 -> 434,583 (about 67.2s) with the forward gap still **0** |
| **T-19** | **Playout and YouTube-side measurement** | run `blackdetect` on both the local and the YouTube side for 330 seconds either side of a segment change | **pass**: no black frames on either side, so the segment change itself is clean and the earlier black screen was confirmed to be a content problem |
| **T-20** | **Loop boundary (the 4-hour wrap back to the first segment)** | splice `concat.txt` into two copies and scan the timeline at the seam | **pass**: 869,166 packets, **0 timeline resets, 0 forward gaps**; the timestamps accumulate continuously (the end of round two is 28,973.9s, about 2x14,487s plus a fixed offset). Because the timestamps never return to zero, `loopwatch.py` derives the loop point from "start time plus one-round length" and samples at high frequency for 45 / 90 seconds around it |
| **T-22** | **Transition insertion** | scan the timeline of the whole list and compare the audio parameters | **pass**: 53 episodes plus 53 transitions (`lj9nUq97uzQ`, 20s, 4K downscaled to 720p) = 106 segments, 466,436 packets, **forward gap 0 and 0 timeline resets**; the transition audio `aac fltp 48000 2` matches the episodes exactly. One round went from 14,487s to **15,549s (4h19m)** |
| **T-21** | **Anomaly detection in health monitoring** | run once normally, then once against a non-existent path | **pass**: normally `OK` (+1.95 MB / 6s); on the anomaly it reports `FAIL`, exits 1 and writes `logs/alerts.jsonl` |

### 10.3 Acceptance order

T-01 -> T-13 -> T-14 -> **T-11** -> T-16 to T-18 -> T-20 -> T-22 -> **T-07 (in progress)** -> **T-12 (long run)** -> T-08 (waiting on D6) -> T-10 (72 hours).

---

## 11. Milestones and work breakdown

| Milestone | Contents | Status |
|---|---|---|
| M1 | Build the target machine environment | **done** (toolchain complete, MediaMTX resident, programs deployed) |
| M2 | One segment end to end | **done** (T-01) |
| M3 | Continuous and gapless | **done** (T-13: 0 gaps at a concat segment change and at the wrap) |
| M4 | A real list on air | **done**: 53 landed, black tails trimmed, transitions inserted, all on air |
| M5 | Push to YouTube | **done**: publicly live since 2026-09-16 21:02 (`is_live`) |
| M6 | Relay mode | **can start**: R-09 is lifted, waiting on D6 to name the source |
| M7 | Long-run stability | **in progress**: the 72-hour clock started at the reboot of 2026-09-17 00:18 |

---

## 12. Risk register

| Id | Risk | Impact | Mitigation |
|---|---|---|---|
| R-01 | source URLs expire after 6 hours and are bound to a dynamic public IP | playout interruption | **removed from the main line by landing the content**; the relaying branch keeps it within 30 minutes with `--url-max-age 1800` plus a 600 margin |
| R-02 | only 8 GB on the target machine | dropped frames | limit concurrent encoding; prefer `-c copy` |
| R-03 | yt-dlp resolution breaks after a YouTube change | cannot obtain sources | client fallback list; no effect on playout once the content is landed |
| R-04 | stream key leak | channel hijacked | section 9 |
| R-05 | upstream policy risk against 24/7 replay | the channel is affected | the user has declared the licence; keep the playout records |
| R-06 | adopting Liquidsoap forces dependency upgrades | 10 packages upgraded on the work machine | if it is really wanted, install it on the target machine (one formula) |
| R-07 | memory accumulating over a long run | process crash | T-07 / T-10 are part of acceptance |
| R-08 | work machine and target machine differ in version | acceptance results cannot be extrapolated | do the main acceptance on the target machine |
| **R-09** | **anonymous resolution intermittently returns `LOGIN_REQUIRED`** | an in-progress landing or relay is interrupted | [corrected in v1.1] this is **not** a permanent block: the same batch failed at 05:11 and succeeded at 20:02, so it is a temporary IP-level flag and **no cookie is needed**. The fallback during a block is `player_client=android` (capped at 360p), wired into `build_local_content.py` as an automatic retry. The real fix is still landing the content (FR-13): afterwards the main line never touches a source URL |
| **R-12** | **silent download truncation** | half a video slips into concat and the broadcast jumps mid-way | both root causes are fixed (3.7): ffmpeg without `-nostdin`, and the incomplete HLS (m3u8) path. A post-download duration comparison with a 5% / 10-second tolerance refetches automatically |
| **R-13** | **a source video contains a long black tail** (measured 66-67 seconds) | viewers see a long black picture while **every transfer and timeline monitor reports normal** | detect it at landing and trim it with `outpoint` (3.8). Note that the first two monitoring layers cannot see this class of problem; only pixel-level `blackdetect` can |
| **R-14** | **FLV 32-bit timestamp wrap** | after about **49.7 days** of continuous playout the timestamp wraps and YouTube may drop the stream | [measured] `-stream_loop -1` timestamps **accumulate continuously** rather than resetting each round, so the timeline grows without bound. Mitigation: restart `com.loopcastr.playout` monthly, which resets the timeline |
| **R-15** | **A reboot cannot be unattended** (FileVault on plus no automatic login) | a power cut, a crash or any reboot needs **a human at the machine** to unlock it; remote recovery is impossible | done: `pmset -a sleep 0 disksleep 0 disablesleep 1` (so sleep cannot cause a false outage). Outstanding: turn FileVault off (a security trade-off, see D16) and move the services from LaunchAgents to **LaunchDaemons** - a LaunchAgent only starts after a graphical login, which does not suit an unattended machine |
| **R-10** | **landed content needs disk (4.5-5.5 GB per round)** | out of disk | 37 GiB free on the target machine is enough; several backup rounds would need a cleanup first |
| **R-11** | **DTS overlap at a concat seam (about 11 ms)** | an imperfect timeline | ffmpeg clamps it back; splice offline into one file if necessary |

---

## 13. Open decisions

| Id | Decision | Who | Blocking |
|---|---|---|---|
| **D1** | ~~YouTube stream key~~ | - | **resolved**: obtained and stored at `target machine:~/loopcastr/stream.key` |
| D2 | ~~Output resolution and bitrate~~ | - | **settled in practice**: landing is uniformly 1280x720, playout at 2500 kbps and YouTube actually builds 720p at 2448 kbps. Revisit if it needs to change |
| D3 | ~~Whether to adopt Liquidsoap~~ | - | **no longer needed**: it was for captions, and path A (ffmpeg overlay) is enough without another DSL layer. Revisit if advanced scheduling is ever needed |
| D4 | ~~Playback order policy~~ **fixed-order loop** (decided by the user 2026-09-17) | - | **resolved** |
| D5 | Filler material | user | FR-05 |
| D6 | Relay source list | user | M6 |
| D7 | ~~Whether to install Homebrew / MediaMTX on the target machine~~ | - | **resolved** |
| D8 | ~~Alert channel~~ | - | **resolved (2026-09-17 03:14)**: switched to the Telegram bot `<BOT_NAME>`. The token lives in `~/loopcastr/telegram.json` (`chmod 600`, never committed) and the chat_id is discovered from `getUpdates` and written back. Both `--test-alert` and a simulated failure drill delivered successfully |
| D9 | Channel name and description have to be set by hand in Studio (the API does not support it) | user | before launch |
| **D14** | ~~The live event state in YouTube Studio~~ | - | **resolved**: with `<LIVE_VIDEO_ID>` and the new key it went publicly live successfully at 2026-09-16 21:02 |
| **D15** | The live title has to be set in Studio or through the YouTube Data API (RTMP cannot carry a title) | user | outward appearance |
| **D16** | ~~Whether to turn FileVault off on the target machine~~ | - | **resolved (2026-09-17 00:07)**: FileVault is off, sleep is permanently disabled and the four services are LaunchDaemons. Only the actual reboot verification was left |
| **D17** | ~~Actual reboot verification~~ | - | **resolved (2026-09-17 00:20)**: after the reboot the whole chain recovered by itself with nobody logged in, interrupted for about 25 seconds |
| D10 | Whether a fixed egress IP is needed | user | downgraded (the main line no longer depends on it) |
| **D11** | ~~Provide a YouTube login cookie~~ | - | **no longer needed**: R-09 was corrected to a temporary flag and the `android` client is the fallback. Revisit if a long block ever appears |
| **D13** | Whether to keep the stack of normalised copies after landing (one re-encode means one quality loss) | user | quality policy |
| **D12** | Whether to adopt "splice offline into one file" to remove the DTS warnings | user | R-11 |

---

## Appendix A: where the numbers come from

| Number | Source |
|---|---|
| URL TTL / resolution time / watchdog thresholds | measured in v4.3 |
| 6 handovers at 0.000 seconds, MediaMTX takeover within the same second | measured in v4.3 |
| Liquidsoap's three gates, ffmpeg without freetype | measured in v4.5 |
| Relay gaps of 3.066 / 2.053 / 2.040 / 0.635 seconds | this round, `logs/local-relay2.out` |
| 0 gaps at a concat segment change and at the wrap | this round, 55 seconds of gapwatch on the target machine (506 samples, api_fail 0) |
| concat cold start 2.546 seconds | this round, 70 seconds of gapwatch on the work machine (641 samples) |
| DTS overlap 11 ms | this round, `logs/playout.log` |
| Target machine tool versions, libx264 present, 37 GiB free | this round, measured over ssh |
| Chrome cookies blocked by TCC | this round, `yt-dlp --cookies-from-browser chrome` |
| All four videos returned `LOGIN_REQUIRED` | this round, measured with yt-dlp (`Ig3vtqtXowY` / `cJx8-vsH14E` / `7xJR7o1gB8c` / `aqz-KE-bpKQ`) |
| End-to-end publishing succeeded | this round, target machine at 05:13 (T-14) |

## Appendix B: verification commands

    # While broadcasting, measure gaps at the receiver (in another terminal)
    python3 ~/loopcastr/gapwatch.py http://127.0.0.1:9997 live/main 120

    # Local hub status
    curl -s http://127.0.0.1:9997/v3/paths/get/live/main

    # Is anonymous resolution still blocked? (exit 2 means everything failed)
    python3 ~/loopcastr/relay.py --playlist ~/loopcastr/playlist.json --check

    # Landing progress
    python3 ~/loopcastr/build_local_content.py --status

    # Service status
    launchctl list | grep loopcastr
    tail -f ~/loopcastr/logs/playout.log ~/loopcastr/logs/publish.log

    # Does this ffmpeg have freetype? (0 means no drawtext)
    ffmpeg -hide_banner -filters | grep -c drawtext
