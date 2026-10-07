#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

from dotenv import load_dotenv

# /config/.env is the authoritative configuration for this application.
load_dotenv('/config/.env', override=True)

CONFIG_DIR = Path(os.getenv('CONFIG_DIR', '/config'))
OUTPUT_DIR = Path(os.getenv('OUTPUT_DIR', '/output'))
EPG_OUTPUT = Path(os.getenv('EPG_OUTPUT', str(OUTPUT_DIR / 'plex-epg.xml')))
CHANNEL_MAP_OUTPUT = Path(os.getenv('CHANNEL_MAP_OUTPUT', str(CONFIG_DIR / 'channel_map.csv')))
LINEUP_MATCHES_OUTPUT = CONFIG_DIR / 'lineup_matches.csv'
ENV_FILE = CONFIG_DIR / '.env'
STATIC_DIR = Path(os.getenv('STATIC_DIR', '/app/static'))
REFRESH_INTERVAL = int(os.getenv('REFRESH_INTERVAL', '21600'))
HTTP_PORT = int(os.getenv('HTTP_PORT', '8080'))

_state_lock = threading.Lock()
_export_lock = threading.Lock()
_lineup_lock = threading.Lock()
_scheduler_lock = threading.Lock()
_scheduler_started = False
_state = {
    'export_running': False,
    'lineup_running': False,
    'last_export_started': None,
    'last_export_finished': None,
    'last_export_ok': None,
    'last_export_message': 'Waiting for first refresh',
    'last_export_duration': None,
    'export_log': [],
    'next_refresh': None,
    'last_lineup_started': None,
    'last_lineup_finished': None,
    'last_lineup_ok': None,
    'last_lineup_message': None,
}


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def env_values() -> dict[str, str]:
    try:
        from setup_lineup import read_env
        return read_env(ENV_FILE)
    except Exception:
        return {}


def set_state(**updates) -> None:
    with _state_lock:
        _state.update(updates)


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def append_export_log(line: str) -> None:
    line = line.rstrip('\r\n')
    if not line:
        return
    with _state_lock:
        lines = list(_state.get('export_log') or [])
        lines.append(line)
        _state['export_log'] = lines[-500:]


def run_export() -> bool:
    if not _export_lock.acquire(blocking=False):
        return False

    started = time.monotonic()
    set_state(
        export_running=True,
        last_export_started=iso_now(),
        last_export_message='Refreshing guide…',
        export_log=[],
    )
    print('[plex-epg] Refreshing guide...', flush=True)

    try:
        process = subprocess.Popen(
            [sys.executable, '-u', '/app/plex_epg_xmltv.py'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is not None:
            for line in process.stdout:
                clean = line.rstrip('\r\n')
                if clean:
                    append_export_log(clean)
                    print(clean, flush=True)
        returncode = process.wait()
        duration = round(time.monotonic() - started, 1)

        if returncode == 0:
            message = 'Guide refresh completed successfully.'
            set_state(last_export_ok=True, last_export_message=message, last_export_duration=duration)
            print(f'[plex-epg] {message}', flush=True)
            return True

        message = f'Guide refresh failed with exit code {returncode}. Existing XMLTV file was kept.'
        set_state(last_export_ok=False, last_export_message=message, last_export_duration=duration)
        print(f'[plex-epg] {message}', flush=True)
        return False
    except Exception as exc:
        duration = round(time.monotonic() - started, 1)
        message = f'Guide refresh failed: {exc}'
        set_state(last_export_ok=False, last_export_message=message, last_export_duration=duration)
        print(f'[plex-epg] {message}', file=sys.stderr, flush=True)
        return False
    finally:
        set_state(export_running=False, last_export_finished=iso_now())
        _export_lock.release()


def normalize_source(value: str) -> str:
    return (value or "dvr").strip().lower()


def setup_status_payload() -> dict:
    config = env_values()
    source = normalize_source(config.get("EPG_SOURCE", "dvr"))
    try:
        hours = str(int(config.get("REFRESH_INTERVAL", "21600")) / 3600).rstrip("0").rstrip(".")
    except ValueError:
        hours = "6"
    return {
        "configured": ENV_FILE.exists(),
        "plex_server": config.get("PLEX_SERVER", ""),
        "has_token": bool(config.get("PLEX_TOKEN", "")),
        "source": source if source in ("dvr", "zipcode") else "dvr",
        "m3u_path": config.get("EPG_M3U_PATH", ""),
        "postal_code": config.get("EPG_POSTAL_CODE", ""),
        "lineup_id": config.get("EPG_LINEUP_ID", ""),
        "days": config.get("EPG_DAYS", "3"),
        "id_mode": config.get("EPG_ID_MODE", "callsign"),
        "refresh_hours": hours,
    }


def _setup_values(payload: dict) -> tuple[dict, str, str, str, str]:
    from setup_lineup import server_url
    existing = env_values()
    server = server_url(str(payload.get("plex_server") or existing.get("PLEX_SERVER") or ""))
    token = str(payload.get("plex_token") or existing.get("PLEX_TOKEN") or "").strip()
    source = normalize_source(str(payload.get("source") or existing.get("EPG_SOURCE") or "dvr"))
    location = str(payload.get("m3u_path") or existing.get("EPG_M3U_PATH") or "").strip()
    if not token:
        raise RuntimeError("Plex token is required.")
    if source not in ("dvr", "zipcode"):
        raise RuntimeError("Guide source must be Existing Plex DVR or Plex EPG Zipcode.")
    if not location:
        raise RuntimeError("M3U path or URL is required for lineup matching.")
    return existing, server, token, source, location


def setup_preview(payload: dict) -> dict:
    from setup_lineup import existing_dvr_channels, match_channels, plex_epg_get, rank_lineups, read_m3u
    _, server, token, source, location = _setup_values(payload)
    networks = read_m3u(location)
    if source == "dvr":
        channels = existing_dvr_channels(server, token)
        matches = match_channels(networks, channels)
        matched = sum(1 for m in matches if m["channel"] is not None)
        exact = sum(1 for m in matches if m["method"] in ("number + callsign", "callsign"))
        review = sum(1 for m in matches if m["method"] == "number only — review callsign")
        return {"ok": True, "source": source, "matched": matched, "total": len(matches), "exact": exact, "review": review, "lineups": []}

    postal = str(payload.get("postal_code") or "").strip()
    if not re.fullmatch(r"\d{5}", postal):
        raise RuntimeError("Enter a five-digit US ZIP code.")
    ranked = rank_lineups(token, postal, networks, payload.get("provider") or None)
    lineups = []
    for score, exact, lineup, matches in ranked:
        lineups.append({
            "id": lineup.get("id", ""),
            "title": lineup.get("title", ""),
            "matched": score,
            "total": len(networks),
            "exact": exact,
            "review": sum(1 for m in matches if m["method"] == "number only — review callsign"),
        })
    return {"ok": True, "source": source, "lineups": lineups}


def _write_matches(matches) -> None:
    temporary = LINEUP_MATCHES_OUTPUT.with_suffix('.csv.tmp')
    LINEUP_MATCHES_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['M3U_callsign', 'M3U_number', 'Guide_callsign', 'Guide_number', 'Method'])
        for match in matches:
            channel = match['channel'] or {}
            writer.writerow([
                match['network']['callsign'], match['network']['number'],
                channel.get('callSign', ''), channel.get('vcn', ''), match['method'],
            ])
    temporary.replace(LINEUP_MATCHES_OUTPUT)


def setup_save(payload: dict) -> dict:
    from setup_lineup import existing_dvr_channels, match_channels, plex_epg_get, read_m3u, write_env
    _, server, token, source, location = _setup_values(payload)
    networks = read_m3u(location)

    days = str(payload.get("days") or "3").strip()
    if not days.isdigit() or not 1 <= int(days) <= 14:
        raise RuntimeError("Guide days must be between 1 and 14.")
    try:
        hours = float(str(payload.get("refresh_hours") or "6"))
    except ValueError:
        raise RuntimeError("Refresh interval must be a number of hours.") from None
    if not (1 / 60 <= hours <= 8760):
        raise RuntimeError("Refresh interval is outside the supported range.")
    id_mode = str(payload.get("id_mode") or "callsign").strip().lower()
    if id_mode not in ("callsign", "vcn", "plex"):
        raise RuntimeError("ID mode must be callsign, vcn, or plex.")

    updates = {
        "PLEX_SERVER": json.dumps(server),
        "PLEX_TOKEN": json.dumps(token),
        "EPG_SOURCE": source,
        "EPG_M3U_PATH": json.dumps(location),
        "EPG_DAYS": days,
        "EPG_ID_MODE": id_mode,
        "REFRESH_INTERVAL": str(round(hours * 3600)),
    }
    remove = {"HTTP_PORT", "CONFIG_DIR", "OUTPUT_DIR", "EPG_OUTPUT", "CHANNEL_MAP_OUTPUT"}

    if source == "dvr":
        channels = existing_dvr_channels(server, token)
        matches = match_channels(networks, channels)
        remove.update({"EPG_COUNTRY", "EPG_POSTAL_CODE", "EPG_LINEUP_ID"})
        source_label = "Existing Plex DVR"
    else:
        postal = str(payload.get("postal_code") or "").strip()
        lineup_id = str(payload.get("lineup_id") or "").strip()
        if not re.fullmatch(r"\d{5}", postal):
            raise RuntimeError("Enter a five-digit US ZIP code.")
        if not lineup_id:
            raise RuntimeError("Select a Plex EPG Zipcode lineup first.")
        container = plex_epg_get(f'/lineups/{quote(lineup_id, safe="")}/channels', token)
        channels = container.get('Channel', [])
        if isinstance(channels, dict):
            channels = [channels]
        matches = match_channels(networks, channels)
        if not any(m['channel'] is not None for m in matches):
            raise RuntimeError("Selected Plex EPG Zipcode lineup has no M3U matches.")
        updates.update(EPG_COUNTRY="US", EPG_POSTAL_CODE=postal, EPG_LINEUP_ID=lineup_id)
        source_label = "Plex EPG Zipcode"

    write_env(ENV_FILE, updates, remove)
    _write_matches(matches)
    load_dotenv(str(ENV_FILE), override=True)
    ensure_scheduler()
    matched = sum(1 for m in matches if m['channel'] is not None)
    print(f'[plex-epg] Setup wizard saved: {source_label}; {matched}/{len(matches)} M3U channels matched.', flush=True)
    return {"ok": True, "message": f"Configuration saved for {source_label}. Guide refresh started.", "matched": matched, "total": len(matches)}


def recheck_lineup() -> bool:
    if not _lineup_lock.acquire(blocking=False):
        return False

    set_state(lineup_running=True, last_lineup_started=iso_now(), last_lineup_message='Rechecking selected lineup…')
    try:
        from setup_lineup import plex_epg_get, existing_dvr_channels, match_channels, read_m3u

        source = configured('EPG_SOURCE', 'dvr').lower()
        token = configured('PLEX_TOKEN')
        server = configured('PLEX_SERVER')
        location = configured('EPG_M3U_PATH')
        lineup_id = configured('EPG_LINEUP_ID')

        if not token:
            raise RuntimeError('PLEX_TOKEN is missing.')
        if not location:
            raise RuntimeError('EPG_M3U_PATH is missing. Run setup_lineup.py first.')

        networks = read_m3u(location)
        if source == 'zipcode':
            if not lineup_id:
                raise RuntimeError('EPG_LINEUP_ID is missing. Run setup_lineup.py first.')
            container = plex_epg_get(f'/lineups/{quote(lineup_id, safe="")}/channels', token)
            channels = container.get('Channel', [])
            if isinstance(channels, dict):
                channels = [channels]
        else:
            if not server:
                raise RuntimeError('PLEX_SERVER is missing.')
            channels = existing_dvr_channels(server, token)

        matches = match_channels(networks, channels)

        LINEUP_MATCHES_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        temporary = LINEUP_MATCHES_OUTPUT.with_suffix('.csv.tmp')
        with temporary.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['M3U_callsign', 'M3U_number', 'Guide_callsign', 'Guide_number', 'Method'])
            for match in matches:
                channel = match['channel'] or {}
                writer.writerow([
                    match['network']['callsign'], match['network']['number'],
                    channel.get('callSign', ''), channel.get('vcn', ''), match['method'],
                ])
        temporary.replace(LINEUP_MATCHES_OUTPUT)

        matched = sum(1 for match in matches if match['channel'] is not None)
        exact = sum(1 for match in matches if match['method'] in ('number + callsign', 'callsign'))
        review = sum(1 for match in matches if match['method'] == 'number only — review callsign')
        message = f'{matched}/{len(matches)} matched; {exact} exact; {review} need review.'
        set_state(last_lineup_ok=True, last_lineup_message=message)
        print(f'[plex-epg] Lineup recheck: {message}', flush=True)
        return True
    except Exception as exc:
        message = str(exc)
        set_state(last_lineup_ok=False, last_lineup_message=message)
        print(f'[plex-epg] Lineup recheck failed: {message}', file=sys.stderr, flush=True)
        return False
    finally:
        set_state(lineup_running=False, last_lineup_finished=iso_now())
        _lineup_lock.release()


def scheduler() -> None:
    while True:
        run_export()
        try:
            wait_seconds = max(60, int(configured('REFRESH_INTERVAL', '21600')))
        except ValueError:
            wait_seconds = 21600
        next_time = datetime.now().astimezone() + timedelta(seconds=wait_seconds)
        set_state(next_refresh=next_time.isoformat(timespec='seconds'))
        time.sleep(wait_seconds)


def ensure_scheduler() -> None:
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started or not ENV_FILE.exists():
            return
        _scheduler_started = True
        threading.Thread(target=scheduler, daemon=True).start()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open(newline='', encoding='utf-8-sig') as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def xmltv_stats(path: Path) -> dict:
    stats = {
        'exists': path.exists(), 'size_bytes': 0, 'modified': None,
        'channels': 0, 'programmes': 0, 'earliest': None, 'latest': None,
    }
    if not path.exists():
        return stats
    try:
        file_stat = path.stat()
        stats['size_bytes'] = file_stat.st_size
        stats['modified'] = datetime.fromtimestamp(file_stat.st_mtime).astimezone().isoformat(timespec='seconds')
        earliest = latest = None
        for _, elem in ET.iterparse(path, events=('end',)):
            if elem.tag == 'channel':
                stats['channels'] += 1
            elif elem.tag == 'programme':
                stats['programmes'] += 1
                start, stop = elem.attrib.get('start'), elem.attrib.get('stop')
                if start and (earliest is None or start < earliest):
                    earliest = start
                if stop and (latest is None or stop > latest):
                    latest = stop
            elem.clear()
        stats['earliest'], stats['latest'] = earliest, latest
    except (OSError, ET.ParseError):
        stats['parse_error'] = True
    return stats


def lineup_summary(rows: list[dict[str, str]]) -> dict:
    total = len(rows)
    matched = sum(1 for row in rows if row.get('Guide_callsign') or row.get('Guide_number'))
    exact = sum(1 for row in rows if row.get('Method') in ('number + callsign', 'callsign'))
    review = sum(1 for row in rows if row.get('Method') == 'number only — review callsign')
    return {'total': total, 'matched': matched, 'unmatched': total - matched, 'exact': exact, 'review': review}


def configured(key: str, default: str = '') -> str:
    """Return /config/.env first, then process environment, then default."""
    config = env_values()
    return str(config.get(key) or os.getenv(key) or default).strip()


def public_config() -> dict:
    return {
        'plex_server': configured('PLEX_SERVER'),
        'source': configured('EPG_SOURCE', 'dvr').lower(),
        'lineup_id': configured('EPG_LINEUP_ID'),
        'postal_code': configured('EPG_POSTAL_CODE'),
        'days': configured('EPG_DAYS', '7'),
        'id_mode': configured('EPG_ID_MODE', 'callsign'),
        'refresh_interval': int(configured('REFRESH_INTERVAL', '21600')),
        'epg_output': configured('EPG_OUTPUT', str(EPG_OUTPUT)),
        'channel_map_output': configured('CHANNEL_MAP_OUTPUT', str(CHANNEL_MAP_OUTPUT)),
        'm3u_path': configured('EPG_M3U_PATH'),
    }


def current_source() -> str:
    return configured('EPG_SOURCE', 'dvr').lower()


def status_payload() -> dict:
    matches = read_csv(LINEUP_MATCHES_OUTPUT)
    return {
        'setup_required': not ENV_FILE.exists(),
        'state': get_state(),
        'config': public_config(),
        'xmltv': xmltv_stats(EPG_OUTPUT),
        'channel_count': len(read_csv(CHANNEL_MAP_OUTPUT)),
        'match_summary': lineup_summary(matches),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'PlexEPG/1.0'
    _logged_public_url = False

    def _send_bytes(self, data: bytes, content_type: str, status: int = 200, extra_headers=None):
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Normal client disconnect: the browser abandoned the request
            # before the response finished. Do not spam the container log.
            return

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self._send_bytes(data, 'application/json; charset=utf-8', status)

    def _text(self, text: str, status=200):
        self._send_bytes(text.encode('utf-8'), 'text/plain; charset=utf-8', status)

    def _file(self, path: Path, content_type: str | None = None):
        if not path.exists() or not path.is_file():
            self._text(f'{path.name} has not been generated yet\n', 404)
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self._text(str(exc), 500)
            return
        ctype = content_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self._send_bytes(data, ctype)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except ValueError:
            length = 0
        if length <= 0 or length > 2 * 1024 * 1024:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError('Invalid JSON request.') from None
        if not isinstance(payload, dict):
            raise RuntimeError('JSON request must be an object.')
        return payload

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            if not Handler._logged_public_url:
                host = self.headers.get('Host', '').strip()
                if host:
                    scheme = self.headers.get('X-Forwarded-Proto', 'http').split(',')[0].strip() or 'http'
                    print(f'[plex-epg] Web UI accessed at {scheme}://{host}/', flush=True)
                    Handler._logged_public_url = True
            self._file(STATIC_DIR / 'index.html', 'text/html; charset=utf-8')
        elif path == '/static/app.css':
            self._file(STATIC_DIR / 'app.css', 'text/css; charset=utf-8')
        elif path == '/api/status':
            self._json(status_payload())
        elif path == '/api/setup/status':
            self._json(setup_status_payload())
        elif path == '/api/channels':
            self._json({'channels': read_csv(CHANNEL_MAP_OUTPUT)})
        elif path == '/api/matches':
            active = bool(configured('EPG_M3U_PATH'))
            rows = read_csv(LINEUP_MATCHES_OUTPUT) if active else []
            self._json({
                'active': active,
                'matches': rows,
                'summary': lineup_summary(rows),
            })
        elif path == '/plex-epg.xml':
            self._file(EPG_OUTPUT, 'application/xml; charset=utf-8')
        elif path == '/channel_map.csv':
            self._file(CHANNEL_MAP_OUTPUT, 'text/csv; charset=utf-8')
        elif path == '/lineup_matches.csv':
            self._file(LINEUP_MATCHES_OUTPUT, 'text/csv; charset=utf-8')
        else:
            self._text('Not found\n', 404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/api/setup/preview':
            try:
                self._json(setup_preview(self._read_json()))
            except Exception as exc:
                self._json({'ok': False, 'message': str(exc)}, 400)
        elif path == '/api/setup/save':
            try:
                self._json(setup_save(self._read_json()))
            except Exception as exc:
                self._json({'ok': False, 'message': str(exc)}, 400)
        elif path == '/api/refresh':
            if _export_lock.locked():
                self._json({'ok': False, 'message': 'A guide refresh is already running.'}, 409)
                return
            threading.Thread(target=run_export, daemon=True).start()
            self._json({'ok': True, 'message': 'Guide refresh started.'}, 202)
        elif path == '/api/lineup/recheck':
            if _lineup_lock.locked():
                self._json({'ok': False, 'message': 'A lineup recheck is already running.'}, 409)
                return
            threading.Thread(target=recheck_lineup, daemon=True).start()
            self._json({'ok': True, 'message': 'Lineup recheck started.'}, 202)
        else:
            self._text('Not found\n', 404)

    def log_message(self, format, *args):
        # Suppress routine HTTP access logs.
        return



def healthcheck() -> int:
    if not ENV_FILE.exists():
        return 0
    return 0 if EPG_OUTPUT.exists() and EPG_OUTPUT.stat().st_size >= 100 else 1


def serve() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ensure_scheduler()
    httpd = ThreadingHTTPServer(('', HTTP_PORT), Handler)
    print('[plex-epg] Web UI ready', flush=True)
    if not ENV_FILE.exists():
        print('[plex-epg] Setup required: open the Web UI and choose Setup Wizard.', flush=True)
    print('[plex-epg] XMLTV URL: /plex-epg.xml', flush=True)
    print('[plex-epg] Channel map URL: /channel_map.csv', flush=True)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    if args.healthcheck:
        return healthcheck()
    serve()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
