# RaceChrono_Session_Converter

Utilities for working with [RaceChrono](https://racechrono.com/) data outside the app.

RaceChrono stores each session as a folder of undocumented binary files. The tools here read those folders directly, so you can process sessions that are no longer on your phone (for example, ones backed up to a NAS) without exporting them one at a time from inside the app.

| Tool | What it does |
|------|--------------|
| [`rc2vbo.py`](rc2vbo.py) | Batch-converts RaceChrono session folders to Racelogic VBO files, including CAN-bus channels and video sync |

More tools may be added later.

## Requirements

Python 3.8 or later. No third-party packages are needed, so the tools run anywhere Python does, including on many NAS devices.

## rc2vbo.py

Converts raw RaceChrono session folders to `.vbo`, a plain-text format read by most motorsport analysis and video overlay tools, such as Circuit Tools, RaceRender, and Track Attack.

### Quick start

```sh
# Convert every session found under a backup folder
python3 rc2vbo.py /path/to/racechrono/sessions -o vbo_out

# Convert specific sessions (folders or zipped folders)
python3 rc2vbo.py session_20260523_1401 session_20260601_0930.zip -o vbo_out
```

Inputs can be session folders, `.zip` files containing a session folder, or any directory. Directories are searched recursively. Network paths work too, such as `\\server\share\sessions` on Windows.

Each session becomes one file named `<session folder>_<track name>.vbo`, for example `session_20260523_1401_Motorsport_Ranch_1.7_CCW.vbo`.

### Options

| Option | Description |
|--------|-------------|
| `-o`, `--outdir DIR` | Output folder (default: `vbo_out`). |
| `--overwrite` | Re-convert sessions that already have a `.vbo` in the output folder. Without it, already-converted sessions are skipped, so re-running on a growing backup only converts new sessions. |
| `--min-seconds N` | Skip sessions with less than `N` seconds of GPS data (default: `10`). This filters out aborted recordings. Use `0` to convert everything. |
| `--no-video` | Ignore video completely: no video columns, `[avi]` section, comments, or links. |
| `--video-channel N` | Which RaceChrono video channel drives the `avifileindex`/`avisynctime` columns. By default an external camera (such as a GoPro) is preferred over the phone camera. |
| `--video-dir DIR` | Folder (searched recursively) containing your video files. Matching files are hard-linked, or symlinked if hard links fail, into the output folder under VBO-style numbered names. Nothing is copied or moved. |

### What's in the VBO

All channels are resampled to the GPS rate, since VBO has one row per time step.

- **Standard VBO channels:** satellites, UTC time, latitude, longitude, speed (km/h), heading, and height.
- **Lap number and distance**, from RaceChrono's lap timing. A start/finish line is also written to the `[laptiming]` section.
- **GPS accuracy.**
- **Lateral and longitudinal acceleration (g)**, derived from GPS in the same way as RaceChrono's own calculated channels.
- **Raw accelerometer (g) and gyro (deg/s)**, from the phone or another IMU.
- **CAN-bus channels**, such as RPM, throttle, brake, clutch, steering angle, temperatures, fuel level, odometer, vehicle speed, and the four wheel speeds. Speeds are converted to km/h.
- **Video sync**, for each RaceChrono video channel: `videoN_file` (which file, numbered from 1, with 0 meaning no video) and `videoN_time` (milliseconds into that file). One channel is also written as the standard Video VBOX `avifileindex`/`avisynctime` columns with an `[avi]` section. GoPro chapter files are chained automatically.
- **Comments** listing each video file with its start time, length, and rotation, plus any camera recordings RaceChrono never synced.

### Verification

Decoding was checked against RaceChrono's own CSV v3 export of the same session. All GPS, accelerometer, gyro, and CAN-bus channels match at identical timestamps, apart from the CSV's rounding. Lap times measured from the generated start/finish line agree with RaceChrono's to within one GPS sample.

### Known limitations

- **Undocumented format.** RaceChrono's session format was reverse-engineered from RaceChrono Pro sessions and is not officially documented. A future app version could change it.
- **Unknown channels.** Channels the converter doesn't recognize are still exported, but as raw, unscaled columns (`gps_fNNNNN`, `devNNN_fNN`, `can_NNNNN`) with a warning. Known examples are GPS fields 30004 and 30005 from an external GPS receiver, and device type 8 fields 28–30.
- **CAN channel names.** These come from RaceChrono's built-in channel IDs. Custom channels appear as `can_<id>`.
- **Start/finish line.** RaceChrono keeps track definitions in the app, not in the session folder, so the line is reconstructed from where the first timed lap began. It is 30 m wide and perpendicular to your heading. Most analysis tools let you redefine it.
- **Unsynced external camera recordings.** These have no linked file, so they can't be placed on the timeline. Their approximate start time is noted in the VBO comments so you can sync them by hand.
- **Video rotation.** Rotation (for example, a phone mounted upside down) can't be expressed in VBO. It is noted in the comments, and you'll need to apply it in your video tool.
- **`[avi]` handling.** Different tools interpret the `[avi]` section slightly differently. The per-channel `videoN_*` columns are always present as a fallback.
- **Multi-fragment sessions.** Sessions split across several fragments haven't been seen yet and are untested. The converter warns if a session looks unusual.

## RaceChrono session format notes

These notes describe what has been learned about the raw session folders so far, for anyone extending these tools.

```
session_YYYYMMDD_HHMM/
  session.json              track name, laps (start/finish timestamps), metadata
  sessionfragment.json      devices, primary GPS device index, linked videos
  channel_<type>_<dev>_<pid>_<field>_<dtype>
  channel2_12_<dev>_<pid>_<pid>_3        CAN-bus values
  temp_*                    RaceChrono's own calculated data (not needed)
```

Every `channel*` file is a headerless little-endian array.

- **Data types (`dtype` suffix):** `0` = int32, `1` = int64, `3` = float64.
- **Timestamps:** field `1` of each stream is an int64 Unix timestamp in milliseconds. Every other field in the same stream has one value per timestamp.
- **Device types:** `1` = GPS, `2` = accelerometer, `3` = gyro, `8` = unknown three-axis sensor, `12` = CAN bus.

| Device | Field | Meaning | Encoding |
|--------|-------|---------|----------|
| GPS | 2 | Distance traveled | int64, mm |
| GPS | 3 | Latitude and longitude | int32 pair, 1/6,000,000 degree (VBO minutes × 10⁵) |
| GPS | 4 | Speed | int32, mm/s |
| GPS | 5 | Altitude | int32, mm |
| GPS | 6 | Bearing | int32, millidegrees |
| GPS | 46 | Device battery | int32, % × 1000 |
| GPS | 30002 | Satellites | int32 |
| GPS | 30003 | Fix type | int32 |
| GPS | 30007 | Accuracy | int32, mm |
| Accelerometer | 9, 10, 11 | X, Y, Z acceleration | int32, mm/s² |
| Gyro | 12, 13, 14 | X, Y, Z rotation rate | int32, millidegrees/s |
| CAN bus | — | Channel values | float64 in `channel2_*`, already in SI units |

Video entries in `sessionfragment.json` have a `startTimestamp` on the session clock. GoPro chapter files have no start time of their own; each one begins where its `previousChapterFilename` ends. Entries with `needsResolving: true` were never linked to a file.

## Contributing

Sample sessions with hardware not yet covered (external GPS receivers, other sensors, custom CAN channels) are the most useful contribution. A session folder together with RaceChrono's CSV v3 export of the same session lets new channels be identified and scaled correctly.

## Disclaimer

This project is not affiliated with or endorsed by RaceChrono. VBO and Video VBOX are formats of Racelogic Ltd.

## License

Released under the [MIT License](LICENSE).
