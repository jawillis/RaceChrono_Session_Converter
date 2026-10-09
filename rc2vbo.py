#!/usr/bin/env python3
"""
rc2vbo.py - Batch-convert RaceChrono Pro session folders to Racelogic VBO files.

Reads the raw session folders RaceChrono stores internally (session.json,
sessionfragment.json, channel_* / channel2_* files) - no app export needed.
Accepts session folders, zipped session folders, or any directory tree
containing them (e.g. your NAS backup share).

    python3 rc2vbo.py /path/to/backups -o /path/to/vbo_out
    python3 rc2vbo.py session_20260523_1401 session_20260601_0930.zip

Pure Python 3.8+, no third-party packages.

Format notes (reverse-engineered from RaceChrono Pro v10 data and verified
against the app's own CSV v3 export):
  channel_<devtype>_<devid>_<pid>_<field>_<dtype>   per-device streams
  channel2_12_<devid>_<pid>_<pid>_3                 CAN-bus values (float64)
  field 1 = timestamp (int64, Unix ms); dtype 0=int32, 1=int64, 3=float64
  GPS lat/lon = int32 pair in 1/6,000,000 deg (i.e. VBO minutes * 1e5)
"""
import argparse, bisect, datetime as dt, json, math, os, re, struct, sys, tempfile, zipfile
from array import array

DTYPES = {0: 'i', 1: 'q', 3: 'd'}  # array typecodes, little-endian assumed
G = 9.80665

# GPS device (type 1) fields: fid -> (column, unit, scale divisor, decimals)
GPS_FIELDS = {
    2: ('distance', 'm', 1e3, 3),
    4: ('speed', 'm/s', 1e3, 3),
    5: ('altitude', 'm', 1e3, 2),
    6: ('bearing', 'deg', 1e3, 2),
    30002: ('sats', '', 1, 0),
    30003: ('fix_type', '', 1, 0),
    30007: ('gps_accuracy', 'm', 1e3, 3),
    46: ('device_battery', '%', 1e3, 0),
}
# IMU devices: (devtype, fid) -> (column, unit, divisor, decimals)
IMU_FIELDS = {
    (2, 9): ('x_acc', 'g', 1e3 * G, 4), (2, 10): ('y_acc', 'g', 1e3 * G, 4), (2, 11): ('z_acc', 'g', 1e3 * G, 4),
    (3, 12): ('x_rot', 'deg/s', 1e3, 3), (3, 13): ('y_rot', 'deg/s', 1e3, 3), (3, 14): ('z_rot', 'deg/s', 1e3, 3),
}
# CAN-bus channel IDs (RaceChrono channel types) -> (column, unit, multiplier, decimals)
MS_TO_KMH = 3.6
CAN_CHANNELS = {
    4: ('can_speed', 'km/h', MS_TO_KMH, 2),
    1001: ('steering_angle', 'deg', 1, 2),
    1002: ('brake_pos', '%', 1, 1),
    1003: ('clutch_pos', '%', 1, 1),
    1010: ('odometer', 'm', 1, 0),
    1020: ('fuel_level', 'l', 1, 2),
    10024: ('rpm', 'rpm', 1, 0),
    10025: ('throttle_pos', '%', 1, 1),
    10026: ('coolant_temp', 'C', 1, 1),
    10031: ('air_temp', 'C', 1, 1),
    10066: ('oil_temp', 'C', 1, 1),
    197623: ('wheel_speed_fl', 'km/h', MS_TO_KMH, 2),
    164855: ('wheel_speed_fr', 'km/h', MS_TO_KMH, 2),
    132087: ('wheel_speed_rl', 'km/h', MS_TO_KMH, 2),
    99319: ('wheel_speed_rr', 'km/h', MS_TO_KMH, 2),
}

RE_CH = re.compile(r'^channel_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)$')
RE_CH2 = re.compile(r'^channel2_(\d+)_(\d+)_(\d+)_(\d+)_(\d+)$')


def warn(msg):
    print('  WARNING: ' + msg, file=sys.stderr)


def read_arr(path, code):
    a = array(DTYPES[code])
    with open(path, 'rb') as f:
        data = f.read()
    usable = len(data) - len(data) % a.itemsize
    a.frombytes(data[:usable])
    if sys.byteorder != 'little':
        a.byteswap()
    return a


def interp(tsrc, vsrc, tdst):
    """Linear interpolation with edge hold; both time lists sorted."""
    out, n, j = [], len(tsrc), 0
    if n == 0:
        return None
    for t in tdst:
        while j < n - 1 and tsrc[j + 1] <= t:
            j += 1
        if t <= tsrc[0]:
            out.append(vsrc[0])
        elif j >= n - 1:
            out.append(vsrc[-1])
        else:
            t0, t1 = tsrc[j], tsrc[j + 1]
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            out.append(vsrc[j] + (vsrc[j + 1] - vsrc[j]) * f)
    return out


def sort_pairs(t, v):
    if all(t[i] <= t[i + 1] for i in range(len(t) - 1)):
        return list(t), list(v)
    p = sorted(zip(t, v), key=lambda x: x[0])
    return [a for a, _ in p], [b for _, b in p]


def video_timeline(frag):
    """Place each linked video on the session clock.
    Returns ({channel: [file dicts sorted by start]}, [unresolved entries])."""
    pending = [v for v in frag.get('videos') or [] if v.get('filename') and not v.get('needsResolving')]
    unresolved = [v for v in frag.get('videos') or [] if not v.get('filename') or v.get('needsResolving')]
    resolved, changed = {}, True
    while pending and changed:
        changed = False
        for v in list(pending):
            ch = v.get('videoChannelId', 0)
            st = v.get('startTimestamp')
            prev = resolved.get((ch, v.get('previousChapterFilename')))
            if st is None and prev:
                st = prev['start'] + prev['duration']  # GoPro chapter continues the previous file
            if st is None:
                continue
            resolved[(ch, v['filename'])] = {
                'filename': v['filename'], 'start': st, 'duration': v.get('duration') or 0, 'channel': ch,
                'camera': 'external camera' if v.get('cameraUid') else 'phone camera',
                'folder': v.get('cameraFolder', ''), 'rotation': v.get('rotation', 0)}
            pending.remove(v)
            changed = True
    unresolved += pending
    chans = {}
    for e in sorted(resolved.values(), key=lambda e: (e['channel'], e['start'])):
        chans.setdefault(e['channel'], []).append(e)
    return chans, unresolved


def locate_in_videos(files, gts):
    """For each timestamp: (1-based file index, ms into that file) or (0, 0) if no video."""
    idx, pos, k = [], [], 0
    for t in gts:
        while k < len(files) and t >= files[k]['start'] + files[k]['duration']:
            k += 1
        if k < len(files) and files[k]['start'] <= t:
            idx.append(k + 1); pos.append(t - files[k]['start'])
        else:
            idx.append(0); pos.append(0)
    return idx, pos


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + ' UTC'


def smooth(x, k=5):
    h, n, out = k // 2, len(x), []
    for i in range(n):
        lo, hi = max(0, i - h), min(n, i + h + 1)
        out.append(sum(x[lo:hi]) / (hi - lo))
    return out


def gradient(x, t):
    n = len(x)
    if n < 2:
        return [0.0] * n
    g = []
    for i in range(n):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        dtv = t[b] - t[a]
        g.append((x[b] - x[a]) / dtv if dtv else 0.0)
    return g


def output_path(folder, outdir):
    with open(os.path.join(folder, 'session.json')) as f:
        track = json.load(f).get('trackName') or 'session'
    base = os.path.basename(os.path.normpath(folder))
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', track).strip('_')
    return os.path.join(outdir, '%s_%s.vbo' % (base, safe))


class TooShort(Exception):
    pass


def convert(folder, outdir, video_channel=None, video_index=None, no_video=False, min_seconds=0):
    with open(os.path.join(folder, 'session.json')) as f:
        sess = json.load(f)
    with open(os.path.join(folder, 'sessionfragment.json')) as f:
        frag = json.load(f)
    extra = [n for n in os.listdir(folder) if n.endswith('.json') and n not in ('session.json', 'sessionfragment.json')]
    if extra:
        warn('unexpected extra metadata files %s - multi-fragment sessions are untested' % extra)

    gps_dev = frag.get('primaryGpsDeviceIndex', 100)
    files = {}
    for name in os.listdir(folder):
        m = RE_CH.match(name)
        if m:
            files[('ch',) + tuple(int(x) for x in m.groups())] = name
            continue
        m = RE_CH2.match(name)
        if m:
            files[('ch2',) + tuple(int(x) for x in m.groups())] = name

    def path(k):
        return os.path.join(folder, files[k])

    # --- GPS base timeline ---
    tk = ('ch', 1, gps_dev, 0, 1, 1)
    if tk not in files:
        raise RuntimeError('no GPS timestamp file for device %d' % gps_dev)
    gts = read_arr(path(tk), 1)
    ll = read_arr(path(('ch', 1, gps_dev, 0, 3, 1)), 0)
    n = min(len(gts), len(ll) // 2)
    lat_raw = ll[0:2 * n:2]
    lon_raw = ll[1:2 * n:2]
    gts = list(gts[:n])
    if n < 2 or (gts[-1] - gts[0]) / 1000.0 < min_seconds:
        raise TooShort('%.1f s of GPS data (%d samples)' % ((gts[-1] - gts[0]) / 1000.0 if n else 0, n))
    gps, gps_raw = {}, []
    for (kind, dtyp, dev, pid, fid, code), name in files.items():
        if kind != 'ch' or dtyp != 1 or dev != gps_dev or fid in (1, 3):
            continue
        if code not in DTYPES:
            warn('unknown dtype in %s' % name); continue
        a = read_arr(path((kind, dtyp, dev, pid, fid, code)), code)
        if len(a) != n:
            warn('%s has %d samples, GPS has %d' % (name, len(a), n))
        if fid not in GPS_FIELDS:
            warn('unknown GPS field %d (%s) exported raw as gps_f%d' % (fid, name, fid))
            gps_raw.append(('gps_f%d' % fid, [float(x) for x in a[:n]] + [0.0] * (n - len(a))))
            continue
        col, unit, div, dec = GPS_FIELDS[fid]
        gps[col] = [x / div for x in a[:n]]
    if 'speed' not in gps or 'bearing' not in gps:
        raise RuntimeError('GPS speed/bearing channels missing')

    tsec = [(t - gts[0]) / 1000.0 for t in gts]
    columns = []  # (name, unit, decimals, values)

    def add(name, unit, dec, vals):
        columns.append((name, unit, dec, vals))

    # lap number from session.json
    laps = sess.get('laps') or []
    starts = [l['startTimestamp'] for l in laps]
    lapcol = []
    for t in gts:
        i = bisect.bisect_right(starts, t)
        if i == 0:
            lapcol.append(0)
        else:
            lap = laps[i - 1]
            fin = lap.get('finishTimestamp')
            lapcol.append(lap['number'] if fin is None or t < fin else 0)
    if laps:
        add('lap', '', 0, lapcol)
    if 'distance' in gps:
        add('distance', 'm', 3, gps['distance'])
    if 'gps_accuracy' in gps:
        add('gps_accuracy', 'm', 3, gps['gps_accuracy'])
    for col, vals in sorted(gps_raw):
        add(col, 'raw', 3, vals)

    # GPS-derived accelerations (matches RaceChrono's calc channels' sign convention)
    v = smooth(gps['speed'])
    hdg, off, prev = [], 0.0, None
    for b in gps['bearing']:
        r = math.radians(b)
        if prev is not None:
            d = r - prev
            if d > math.pi: off -= 2 * math.pi
            elif d < -math.pi: off += 2 * math.pi
        prev = r
        hdg.append(r + off)
    yaw = gradient(smooth(hdg), tsec)
    add('lat_acc', 'g', 3, [-vi * w / G for vi, w in zip(v, yaw)])
    add('long_acc', 'g', 3, [a / G for a in gradient(v, tsec)])

    # IMU + CAN + anything else, interpolated onto GPS timestamps
    for key in sorted(k for k in files if k[0] == 'ch'):
        kind, dtyp, dev, pid, fid, code = key
        if dtyp == 1 and dev == gps_dev:
            continue
        if fid in (1, 2) or dtyp == 12:
            continue  # timestamps / distance index / CAN handled below
        tkey = ('ch', dtyp, dev, pid, 1, 1)
        if tkey not in files or code not in DTYPES:
            warn('skipping %s' % files[key]); continue
        if (dtyp, fid) in IMU_FIELDS:
            col, unit, div, dec = IMU_FIELDS[(dtyp, fid)]
        else:
            col, unit, div, dec = 'dev%d_f%d' % (dev, fid), 'raw', 1, 3
            warn('unknown channel %s exported raw as %s' % (files[key], col))
        t, vals = read_arr(path(tkey), 1), read_arr(path(key), code)
        m = min(len(t), len(vals))
        t, vals = sort_pairs(t[:m], [x / div for x in vals[:m]])
        add(col, unit, dec, interp(t, vals, gts))

    for key in sorted(k for k in files if k[0] == 'ch2'):
        kind, dtyp, dev, pid, pid2, code = key
        tkey = ('ch', dtyp, dev, pid, 1, 1)
        if tkey not in files or code not in DTYPES:
            warn('skipping %s' % files[key]); continue
        col, unit, mul, dec = CAN_CHANNELS.get(pid, ('can_%d' % pid, 'raw', 1, 3))
        if pid not in CAN_CHANNELS:
            warn('unknown CAN channel id %d exported raw as %s' % (pid, col))
        t, vals = read_arr(path(tkey), 1), read_arr(path(key), code)
        m = min(len(t), len(vals))
        if m == 0:
            continue
        t, vals = sort_pairs(t[:m], [x * mul for x in vals[:m]])
        add(col, unit, dec, interp(t, vals, gts))

    # --- video sync ---
    vchans, unresolved = ({}, []) if no_video else video_timeline(frag)
    avi_ch, avi_files = None, []
    base = os.path.basename(os.path.normpath(folder))
    if vchans:
        if video_channel in vchans:
            avi_ch = video_channel
        else:  # prefer an external camera (GoPro etc.) over the phone camera
            ext = [c for c in vchans if vchans[c][0]['camera'] == 'external camera']
            avi_ch = min(ext) if ext else min(vchans)
        for ch in sorted(vchans):
            idx, pos = locate_in_videos(vchans[ch], gts)
            add('video%d_file' % ch, '', 0, idx)
            add('video%d_time' % ch, 'ms', 0, pos)
            if ch == avi_ch:
                avi_idx, avi_pos = idx, pos
        avi_files = vchans[avi_ch]
        add('avifileindex', '', -4, avi_idx)
        add('avisynctime', 'ms', 0, avi_pos)
    elif video_channel is not None and frag.get('videos') and not no_video:
        warn('requested video channel %s has no linked files' % video_channel)
    avi_prefix = base + '_'
    avi_ext = os.path.splitext(avi_files[0]['filename'])[1].lstrip('.').lower() if avi_files else 'mp4'
    linked = []
    if avi_files and video_index is not None:
        for k, e in enumerate(avi_files, 1):
            src = video_index.get(e['filename'].lower())
            dst = os.path.join(outdir, '%s%04d.%s' % (avi_prefix, k, avi_ext))
            if not src:
                warn('video %s not found under --video-dir' % e['filename']); continue
            if os.path.lexists(dst):
                linked.append(dst); continue
            try:
                os.link(src, dst)
            except OSError:
                try:
                    os.symlink(os.path.abspath(src), dst)
                except OSError as err:
                    warn('could not link %s: %s' % (e['filename'], err)); continue
            linked.append(dst)

    # --- write VBO ---
    track = sess.get('trackName') or 'session'
    out = output_path(folder, outdir)
    created = dt.datetime.fromtimestamp(gts[0] / 1000.0, dt.timezone.utc)
    tdec = 2 if all(t % 10 == 0 for t in gts) else 3

    std = [('satellites', 'sats', ''), ('time', 'time', 'UTC hhmmss'), ('latitude', 'lat', 'minutes'),
           ('longitude', 'long', 'minutes'), ('velocity kmh', 'velocity', 'km/h'), ('heading', 'heading', 'deg'),
           ('height', 'height', 'm')]
    with open(out, 'w', newline='\r\n') as f:
        f.write('File created on %s @ %s\n\n' % (created.strftime('%d/%m/%Y'), created.strftime('%H:%M:%S')))
        f.write('[header]\n')
        for h, _, _ in std:
            f.write(h + '\n')
        for name, _, _, _ in columns:
            f.write(name + '\n')
        f.write('\n[channel units]\n')
        for _, _, u in std:
            f.write(u + '\n')
        for _, u, _, _ in columns:
            f.write(u + '\n')
        f.write('\n[comments]\nConverted from RaceChrono session %s by rc2vbo.py\nTrack: %s\n'
                'Time is UTC. lat_acc/long_acc are derived from GPS. Other channels interpolated to GPS rate.\n'
                % (base, track))
        for ch in sorted(vchans):
            for k, e in enumerate(vchans[ch], 1):
                f.write('video%d_file=%d: %s%s (%s), starts %s, %.1f s%s%s\n' % (
                    ch, k, (e['folder'] + '/') if e['folder'] else '', e['filename'], e['camera'],
                    utc(e['start']), e['duration'] / 1000.0,
                    ', rotated %d deg' % e['rotation'] if e['rotation'] else '',
                    ' -> avifileindex %04d (%s%04d.%s)' % (k, avi_prefix, k, avi_ext) if ch == avi_ch else ''))
        for v in unresolved:
            st = v.get('startTimestamp') or v.get('createdTimestamp')
            f.write('Unsynced %s recording (channel %s) started about %s - not linked in RaceChrono, '
                    'sync it manually\n' % ('external camera' if v.get('cameraUid') else 'video',
                                             v.get('videoChannelId', '?'), utc(st) if st else 'unknown time'))
        if avi_files:
            f.write('\n[avi]\n%s\n%s\n' % (avi_prefix, avi_ext.upper()))
        if laps:
            gate = start_gate(laps[0]['startTimestamp'], gts, lat_raw, lon_raw, gps['bearing'])
            if gate:
                f.write('\n[laptiming]\nStart   %s %s %s %s \u00ac Start / Finish\n' % gate)
        f.write('\n[column names]\n')
        f.write(' '.join([c for _, c, _ in std] + [c[0] for c in columns]) + '\n')
        f.write('\n[data]\n')
        sats = gps.get('sats', [0] * n)
        alt = gps.get('altitude', [0.0] * n)
        for i in range(n):
            tm = dt.datetime.fromtimestamp(gts[i] / 1000.0, dt.timezone.utc)
            secs = tm.second + tm.microsecond / 1e6
            tstr = '%02d%02d%0*.*f' % (tm.hour, tm.minute, tdec + 3, tdec, secs)
            row = ['%03d' % int(sats[i]), tstr,
                   '%+012.5f' % (lat_raw[i] / 1e5), '%+012.5f' % (-lon_raw[i] / 1e5),
                   '%07.3f' % (gps['speed'][i] * MS_TO_KMH), '%06.2f' % gps['bearing'][i],
                   '%+010.2f' % alt[i]]
            for _, _, dec, vals in columns:
                x = vals[i]
                if dec < 0:
                    row.append('%0*d' % (-dec, round(x)))
                else:
                    row.append('%d' % round(x) if dec == 0 else '%.*f' % (dec, x))
            f.write(' '.join(row) + '\n')
    return out, n, len(columns), len(laps), sum(len(v) for v in vchans.values()), len(unresolved), linked


def start_gate(t0, gts, lat_raw, lon_raw, bearing, half_width=15.0):
    i = bisect.bisect_left(gts, t0)
    if i <= 0 or i >= len(gts):
        return None
    f = (t0 - gts[i - 1]) / max(1, gts[i] - gts[i - 1])
    lat = (lat_raw[i - 1] + (lat_raw[i] - lat_raw[i - 1]) * f) / 6e6
    lon = (lon_raw[i - 1] + (lon_raw[i] - lon_raw[i - 1]) * f) / 6e6
    perp = math.radians(bearing[i] + 90)
    dlat = half_width * math.cos(perp) / 111320.0
    dlon = half_width * math.sin(perp) / (111320.0 * math.cos(math.radians(lat)))
    pts = []
    for s in (1, -1):
        pts.append('%+012.5f' % (-(lon + s * dlon) * 60))
        pts.append('%+012.5f' % ((lat + s * dlat) * 60))
    return tuple(pts)


def find_sessions(paths):
    for p in paths:
        if os.path.isfile(p) and p.lower().endswith('.zip'):
            yield ('zip', p)
        elif os.path.isdir(p):
            for root, dirs, fnames in os.walk(p):
                if 'session.json' in fnames and 'sessionfragment.json' in fnames:
                    yield ('dir', root)
                    dirs[:] = []
                else:
                    for fn in fnames:
                        if fn.lower().endswith('.zip'):
                            yield ('zip', os.path.join(root, fn))


def main():
    ap = argparse.ArgumentParser(description='Convert RaceChrono session folders to VBO.')
    ap.add_argument('inputs', nargs='+', help='session folders, .zip files, or directories to search')
    ap.add_argument('-o', '--outdir', default='vbo_out')
    ap.add_argument('--min-seconds', type=float, default=10.0,
                    help='skip sessions with less GPS data than this (default 10; 0 converts everything)')
    ap.add_argument('--no-video', action='store_true',
                    help='ignore video entirely: no video columns, [avi] section, comments or links')
    ap.add_argument('--video-channel', type=int, default=None,
                    help='which RaceChrono video channel drives avifileindex/avisynctime '
                         '(default: external camera if present, else phone)')
    ap.add_argument('--video-dir', default=None,
                    help='folder (searched recursively) holding the video files; matching files are '
                         'hard-linked (or symlinked) into the output folder with VBO-style names')
    ap.add_argument('--overwrite', action='store_true', help='re-convert sessions that already have a .vbo')
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    video_index = None
    if args.video_dir and not args.no_video:
        video_index = {}
        for root, _, fns in os.walk(args.video_dir):
            for fn in fns:
                video_index.setdefault(fn.lower(), os.path.join(root, fn))
    ok = fail = skipped = short = 0
    for kind, p in find_sessions(args.inputs):
        print(p)
        tmp = None
        try:
            if kind == 'dir':
                folders = [p]
            else:
                tmp = tempfile.TemporaryDirectory()
                with zipfile.ZipFile(p) as z:
                    z.extractall(tmp.name)
                folders = [r for r, _, fns in os.walk(tmp.name) if 'session.json' in fns and 'sessionfragment.json' in fns]
                if not folders:
                    print('  (no RaceChrono session inside, skipped)')
        except Exception as e:
            print('  FAILED: %s' % e, file=sys.stderr)
            fail += 1
            folders = []
        for folder in folders:
            try:
                expected = output_path(folder, args.outdir)
                if not args.overwrite and os.path.exists(expected):
                    print('  already converted, skipped (use --overwrite)')
                    skipped += 1
                    continue
                out, n, nc, nl, nv, nu, linked = convert(folder, args.outdir, args.video_channel, video_index,
                                                         args.no_video, args.min_seconds)
                print('  -> %s  (%d samples, %d extra channels, %d laps, %d videos)' % (out, n, nc, nl, nv))
                if nu:
                    print('  note: %d unsynced camera recording(s), listed in the VBO comments' % nu)
                for l in linked:
                    print('  video -> %s' % l)
                ok += 1
            except TooShort as e:
                print('  skipped: too short, %s' % e)
                short += 1
            except Exception as e:
                print('  FAILED: %s' % e, file=sys.stderr)
                fail += 1
        if tmp:
            tmp.cleanup()
    print('\nDone: %d converted, %d already done, %d too short, %d failed' % (ok, skipped, short, fail))


if __name__ == '__main__':
    main()
