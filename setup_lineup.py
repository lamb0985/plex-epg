#!/usr/bin/env python3
"""Interactive Plex EPG configuration wizard (Python standard library only)."""
import argparse
import csv
import getpass
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import Request, urlopen

BASE = "https://epg.provider.plex.tv"


def read_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][\w]*)\s*=\s*(.*)$", line)
            if m:
                value = m[2].strip()
                if value.startswith('"'):
                    try:
                        value = json.JSONDecoder().raw_decode(value)[0]
                    except ValueError:
                        value = value[1:].split('"', 1)[0]
                elif value.startswith("'"):
                    value = value[1:].split("'", 1)[0]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
                values[m[1]] = value
    return values




def pms_get(server, path, token):
    url = server.rstrip("/") + path
    req = Request(url, headers={
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": "plex-epg-lineup-setup",
        "X-Plex-Product": "Plex EPG Setup",
    })
    try:
        with urlopen(req, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Plex server returned HTTP {exc.code} for {path}") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f"Plex server request failed for {path}") from None
    container = data.get("MediaContainer")
    if not isinstance(container, dict):
        raise RuntimeError("Unexpected Plex server response")
    return container


def existing_dvr_channels(server, token):
    container = pms_get(server, "/livetv/dvrs", token)
    dvrs = container.get("Dvr") or []
    if isinstance(dvrs, dict):
        dvrs = [dvrs]
    dvrs = [d for d in dvrs if isinstance(d, dict) and d.get("lineup") and d.get("key")]
    if not dvrs:
        raise RuntimeError("No existing Plex DVR with guide data was found")
    dvr = dvrs[0]
    lineup = str(dvr.get("lineup", ""))
    m = re.search(r"tv\.plex\.providers\.epg\.([A-Za-z0-9_-]+)", lineup)
    if not m:
        raise RuntimeError(f"Could not determine the guide provider for Plex DVR lineup: {lineup}")
    provider = f"/tv.plex.providers.epg.{m.group(1)}:{dvr['key']}"
    channels = pms_get(server, f"{provider}/lineups/dvr/channels", token).get("Channel") or []
    if isinstance(channels, dict):
        channels = [channels]
    channels = [c for c in channels if isinstance(c, dict)]
    if not channels:
        raise RuntimeError("Existing Plex DVR returned zero guide channels")
    return channels


def plex_epg_get(path, token, params=None):
    url = BASE + path + (("?" + urlencode(params)) if params else "")
    req = Request(url, headers={"Accept": "application/json", "X-Plex-Token": token,
        "X-Plex-Client-Identifier": "plex-epg-lineup-setup", "X-Plex-Product": "Plex EPG Setup"})
    try:
        with urlopen(req, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Plex EPG service returned HTTP {exc.code} for {path}") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f"Plex EPG service request failed for {path}") from None
    container = data.get("MediaContainer")
    if not isinstance(container, dict):
        raise RuntimeError("Unexpected Plex EPG response")
    return container


def identifier(value):
    value = value.strip().upper()
    # Restrict matching to station identifiers, never channel numbers or titles.
    return value if re.fullmatch(r"[A-Z][A-Z0-9&+._-]*", value) else ""


def canonical(value):
    value = identifier(value)
    # Only known transport suffixes; do not trim a bare H, W, E or a digit.
    return re.sub(r"(?:[._-]?HD|[._-]?DT)$", "", value) if len(value) > 4 else value


def read_m3u(location):
    location = str(location)
    if urlsplit(location).scheme in ("http", "https"):
        # A playlist host must never receive the Plex token.
        try:
            with urlopen(Request(location, headers={"User-Agent": "Plex-EPG-Setup"}), timeout=30) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise RuntimeError("M3U exceeds the 16 MiB size limit")
            text = data.decode("utf-8-sig")
        except (URLError, TimeoutError, UnicodeError):
            raise RuntimeError("Unable to download M3U; check its URL and access") from None
    else:
        text = Path(location).expanduser().read_text(encoding="utf-8-sig")
    networks = {}
    for line in text.splitlines():
        if not line.startswith("#EXTINF:"):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        candidates = [identifier(attrs.get(k, "")) for k in ("tvg-id", "tvg-name")]
        display = line.rsplit(",", 1)[-1].strip()
        candidates.append(identifier(re.sub(r"^\d+(?:\.\d+)?\s+", "", display)))
        candidates = list(dict.fromkeys(x for x in candidates if x))
        if not candidates:
            raise RuntimeError(f"No callsign identifier found for M3U entry: {display}")
        key = canonical(candidates[0])
        row = networks.setdefault(key, {"callsign": candidates[0], "aliases": set(), "number": attrs.get("tvg-chno", "")})
        row["aliases"].update(candidates)
        row.setdefault("numbers", set()).update([attrs["tvg-chno"]] if attrs.get("tvg-chno") else [])
        row.setdefault("ids", set()).update([identifier(attrs.get("tvg-id", ""))] if identifier(attrs.get("tvg-id", "")) else [])
    if not networks:
        raise RuntimeError("M3U contains no channel entries")
    return list(networks.values())


def channel_number(value):
    value = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return ""
    return ".".join(str(int(part)) for part in value.split("."))


def match_channels(networks, channels):
    """Number first, then tvg-id callsign; number-only matches require review."""
    numbered = {}
    for c in channels:
        number = channel_number(c.get("vcn", ""))
        if number:
            numbered.setdefault(number, []).append(c)
    matches = []
    for network in networks:
        ids = network.get("ids") or network["aliases"]
        numbers = network.get("numbers") or {network.get("number", "")}
        pool = [c for number in numbers for c in numbered.get(channel_number(number), [])]
        by_number = bool(pool)
        if not pool:
            pool = channels
        exact = [c for c in pool if identifier(c.get("callSign", "")) in ids]
        suffix = [c for c in pool if canonical(c.get("callSign", "")) in {canonical(x) for x in ids}]
        hits = exact or suffix or (pool if by_number else [])
        hits = {str(c.get("gridKey") or c.get("id") or (str(c.get("vcn")) + c.get("callSign", ""))): c for c in hits}
        chosen = None
        if hits and len({canonical(c.get("callSign", "")) for c in hits.values()}) == 1:
            chosen = sorted(hits.values(), key=lambda c: (not c.get("isHd", False), str(c.get("vcn", "")), str(c.get("id", ""))))[0]
        if not chosen:
            method = "ambiguous" if hits else "unmatched"
        elif exact:
            method = "number + callsign" if by_number else "callsign"
        elif suffix:
            method = "number + HD/DT suffix" if by_number else "HD/DT suffix"
        else:
            method = "number only — review callsign"
        matches.append({"network": network, "channel": chosen, "method": method})
    return matches


def write_env(path, updates, remove=()):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    output = []
    for line in lines:
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][\w]*)\s*=", line)
        if m and m[1] in remove:
            continue
        if m and m[1] in updates:
            if m[1] in pending:
                output.append(f"{m[1]}={pending.pop(m[1])}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ask(label, default="", validate=None):
    while True:
        value = input(label + (f" [{default}]" if default else "") + ": ").strip() or str(default)
        if value and (validate is None or validate(value)):
            return value
        print("Please enter a valid value.")


def select(label, count, default=1):
    return int(ask(label, str(default), lambda v: v.isdigit() and 1 <= int(v) <= count))


def server_url(value):
    """Normalize a Plex server address without breaking reverse-proxy URLs.

    Accepted examples:
      10.0.0.10:32400
      http://10.0.0.10:32400
      http://plex.example.com
      https://plex.example.com

    A bare host/IP with no scheme and no port uses Plex's normal direct
    connection default: http://HOST:32400.

    If http:// or https:// is supplied explicitly and no port is supplied,
    the URL is preserved without adding :32400. This allows standard-port
    reverse proxies (HTTP 80 / HTTPS 443).
    """
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("Enter a Plex server address")

    had_scheme = "://" in value
    if not had_scheme:
        value = "http://" + value

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(
            "Enter IP:PORT or a valid http:// / https:// Plex server URL"
        ) from None

    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Enter IP:PORT or a valid http:// / https:// Plex server URL "
            "without credentials or a path"
        )

    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname

    if port is not None:
        return f"{parsed.scheme}://{host}:{port}"
    if had_scheme:
        return f"{parsed.scheme}://{host}"
    return f"http://{host}:32400"


def rank_lineups(token, postal, networks, provider=None):
    lineups = plex_epg_get("/lineups", token, {"country": "US", "postalCode": postal}).get("Lineup", [])
    if isinstance(lineups, dict):
        lineups = [lineups]
    if provider:
        lineups = [x for x in lineups if provider.casefold() in x.get("title", "").casefold()]
    ranked, failures = [], []
    for lineup in lineups:
        print(f"Checking {lineup['title']} ...", flush=True)
        try:
            channels = plex_epg_get(f"/lineups/{quote(lineup['id'], safe='')}/channels", token).get("Channel", [])
            if isinstance(channels, dict):
                channels = [channels]
            matches = match_channels(networks, channels)
            score = sum(m["channel"] is not None for m in matches)
            exact = sum(m["method"] in ("number + callsign", "callsign") for m in matches)
            ranked.append((score, exact, lineup, matches))
        except RuntimeError as exc:
            failures.append(lineup["title"])
            print(f"  Unavailable: {exc}")
    ranked.sort(key=lambda x: (-x[0], -x[1], x[2]["title"], x[2]["id"]))
    if not ranked:
        raise RuntimeError("No available lineups; .env was not changed")
    print(f"\nDistinct M3U networks: {len(networks)}")
    for i, (score, exact, lineup, lineup_matches) in enumerate(ranked, 1):
        suggestion = " — Recommended" if i == 1 and score > 0 and not failures else ""
        if suggestion and len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
            suggestion = " — Tied for best match"
        review_count = sum(m["method"] == "number only — review callsign" for m in lineup_matches)
        print(f"{i}. {lineup['title']}: {score}/{len(networks)} matched ({exact} exact callsigns; {review_count} number-only for review){suggestion}")
    if failures:
        print("Some lineups could not be checked; the ranking is incomplete. Retry before selecting.")
        raise RuntimeError("Incomplete lineup comparison; .env was not changed")
    if ranked[0][0] == 0:
        raise RuntimeError("No callsign matches; .env was not changed")
    return ranked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("/config/.env"))
    parser.add_argument("--m3u", help="Local playlist path or HTTP(S) URL")
    parser.add_argument("--zip", dest="postal")
    parser.add_argument("--provider", help="Only compare lineup titles containing this text")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing files")
    args = parser.parse_args()
    config = read_env(args.env)
    print("Plex EPG setup\n")
    while True:
        try:
            server = server_url(ask(
                "Plex server (IP:PORT or http:// / https:// URL)",
                os.getenv("PLEX_SERVER") or config.get("PLEX_SERVER", "")
            ))
            break
        except ValueError as exc:
            print(exc)
    existing_token = os.getenv("PLEX_TOKEN") or config.get("PLEX_TOKEN", "")
    token = ""
    while not token:
        token = getpass.getpass("Plex token" + (" [Enter to keep existing]" if existing_token else "") + ": ").strip() or existing_token
    print("\n1. Existing Plex DVR — use the guide channels already configured in Plex")
    print("2. Plex EPG Zipcode — select guide data from Plex EPG using a US ZIP code")
    existing_source = config.get("EPG_SOURCE", "dvr").lower()
    default_source = 2 if args.postal or existing_source == "zipcode" else 1
    source = "dvr" if select("Guide source", 2, default_source) == 1 else "zipcode"
    updates = {"PLEX_SERVER": json.dumps(server), "PLEX_TOKEN": json.dumps(token), "EPG_SOURCE": source}

    location = args.m3u or ask("M3U local path or web URL", config.get("EPG_M3U_PATH", ""))
    networks = read_m3u(location)
    if urlsplit(str(location)).scheme not in ("http", "https"):
        location = str(Path(location).expanduser().resolve())
    updates["EPG_M3U_PATH"] = json.dumps(location)

    if source == "dvr":
        channels = existing_dvr_channels(server, token)
        matches = match_channels(networks, channels)
        print(f"Existing Plex DVR: {sum(m['channel'] is not None for m in matches)}/{len(matches)} M3U channels matched")
    else:
        postal = args.postal or ask("US ZIP code", config.get("EPG_POSTAL_CODE", ""), lambda v: bool(re.fullmatch(r"\d{5}", v)))
        if not re.fullmatch(r"\d{5}", postal):
            raise RuntimeError("Enter a five-digit US ZIP code")
        ranked = rank_lineups(token, postal, networks, args.provider)
        score, exact, chosen, matches = ranked[select("Select lineup", len(ranked)) - 1]
        if not score:
            raise RuntimeError("Selected lineup has no matches; .env was not changed")
        print(f"Selected Plex EPG Zipcode lineup: {chosen['title']} ({score}/{len(networks)} matched)")
        print("Unmatched: " + (", ".join(m["network"]["callsign"] for m in matches if m["channel"] is None) or "none"))
        review = [m["network"]["callsign"] + " → " + m["channel"].get("callSign", "")
                  for m in matches if m["method"] == "number only — review callsign"]
        if review:
            print("Number-only matches (review): " + ", ".join(review))
        updates.update(EPG_COUNTRY="US", EPG_POSTAL_CODE=postal, EPG_LINEUP_ID=chosen["id"])
    days = ask("Days of guide data (1–14)", config.get("EPG_DAYS", "3"), lambda v: v.isdigit() and 1 <= int(v) <= 14)
    mode = "callsign"
    def valid_hours(value):
        try:
            return 1 / 60 <= float(value) <= 8760
        except ValueError:
            return False
    try:
        default_hours = str(int(config.get("REFRESH_INTERVAL", "21600")) / 3600).rstrip("0").rstrip(".")
    except ValueError:
        default_hours = "6"
    hours = ask("Refresh guide every how many hours", default_hours, valid_hours)
    updates.update(EPG_DAYS=days, EPG_ID_MODE=mode, REFRESH_INTERVAL=str(round(float(hours) * 3600)))
    if args.dry_run:
        print("Preview complete. No files changed; token hidden.")
        return
    remove = {"HTTP_PORT", "CONFIG_DIR", "OUTPUT_DIR", "EPG_OUTPUT", "CHANNEL_MAP_OUTPUT"}
    if source == "dvr":
        remove.update({"EPG_COUNTRY", "EPG_POSTAL_CODE", "EPG_LINEUP_ID"})
    write_env(args.env, updates, remove)

    report = args.env.parent / "lineup_matches.csv"
    if matches is not None:
        with report.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["M3U_callsign", "M3U_number", "Guide_callsign", "Guide_number", "Method"])
            for m in matches:
                c = m["channel"] or {}
                writer.writerow([m["network"]["callsign"], m["network"]["number"], c.get("callSign", ""), c.get("vcn", ""), m["method"]])
    source_label = "Existing Plex DVR" if source == "dvr" else "Plex EPG Zipcode"
    print(f"Saved {args.env}. Source: {source_label}; days: {days}; IDs: {mode}; refresh: {hours} hours.")
    print("Restart the EPG container to apply the settings.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Setup cancelled.")
