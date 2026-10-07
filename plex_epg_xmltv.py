#!/usr/bin/env python3
"""
Plex EPG -> XMLTV exporter

Uses Plex Media Server's authenticated Live TV / EPG endpoints to export
the guide data Plex already has into a standard XMLTV file.

Environment variables:
    PLEX_SERVER=http://10.0.0.10:32400
    PLEX_TOKEN=xxxxxxxxxxxxxxxxxxxx
    EPG_DAYS=7
    EPG_OUTPUT=/output/plex-epg.xml
    CHANNEL_MAP_OUTPUT=/config/channel_map.csv
    EPG_ID_MODE=callsign

EPG_ID_MODE:
    callsign  -> use Plex callSign when available (recommended for M3U tvg-id)
    plex      -> use Plex's internal EPG channel id
    vcn       -> use channel number
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


CLIENT_IDENTIFIER = "plex-epg-xmltv-exporter"
CLIENT_PRODUCT = "Plex EPG XMLTV Exporter"
CLIENT_VERSION = "1.0.0"

REQUEST_TIMEOUT = 30
REQUEST_RETRIES = 3


def load_environment() -> None:
    if load_dotenv:
        # Primary container configuration file.
        # Existing Docker environment variables take precedence.
        load_dotenv("/config/.env", override=True)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


PLEX_SERVER = ""
PLEX_TOKEN = ""
EPG_DAYS = 7
EPG_OUTPUT = ""
CHANNEL_MAP_OUTPUT = ""
EPG_ID_MODE = "callsign"
EPG_SOURCE = "dvr"
EPG_LINEUP_ID = ""
EPG_M3U_PATH = ""


def configure() -> None:
    global PLEX_SERVER, PLEX_TOKEN, EPG_DAYS, EPG_OUTPUT
    global CHANNEL_MAP_OUTPUT, EPG_ID_MODE, EPG_SOURCE, EPG_LINEUP_ID, EPG_M3U_PATH

    load_environment()

    PLEX_SERVER = env("PLEX_SERVER", required=True).rstrip("/")
    PLEX_TOKEN = env("PLEX_TOKEN", required=True)
    EPG_DAYS = max(1, int(env("EPG_DAYS", "7")))
    EPG_OUTPUT = env("EPG_OUTPUT", "/output/plex-epg.xml")
    CHANNEL_MAP_OUTPUT = env("CHANNEL_MAP_OUTPUT", "./channel_map.csv")
    EPG_ID_MODE = env("EPG_ID_MODE", "callsign").strip().lower()
    EPG_SOURCE = env("EPG_SOURCE", "dvr").strip().lower()
    EPG_LINEUP_ID = env("EPG_LINEUP_ID", "").strip()
    EPG_M3U_PATH = env("EPG_M3U_PATH", "").strip()

    if EPG_ID_MODE not in {"callsign", "plex", "vcn"}:
        raise RuntimeError(
            "EPG_ID_MODE must be one of: callsign, plex, vcn"
        )


session = requests.Session()


def plex_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": PLEX_TOKEN,
        "X-Plex-Client-Identifier": CLIENT_IDENTIFIER,
        "X-Plex-Product": CLIENT_PRODUCT,
        "X-Plex-Version": CLIENT_VERSION,
    }


def plex_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{PLEX_SERVER}{path}"
    last_error: Exception | None = None

    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = session.get(
                url,
                headers=plex_headers(),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < REQUEST_RETRIES:
                time.sleep(attempt)

    raise RuntimeError(f"GET failed: {url}: {last_error}")


def media_container(payload: dict[str, Any]) -> dict[str, Any]:
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        raise RuntimeError("Unexpected Plex response: MediaContainer missing")
    return container


def discover_dvr() -> dict[str, Any]:
    """
    Return the configured DVR that owns an EPG lineup.

    Plex /livetv/dvrs normally contains both the tuner device and an EPG/DVR
    entry. The EPG entry has a 'lineup' value and a numeric 'key'.
    """
    container = media_container(plex_get("/livetv/dvrs"))
    dvrs = container.get("Dvr") or []

    epg_dvrs = [
        dvr for dvr in dvrs
        if isinstance(dvr, dict) and dvr.get("lineup") and dvr.get("key")
    ]

    if not epg_dvrs:
        raise RuntimeError(
            "No Plex DVR with an EPG lineup was found in /livetv/dvrs"
        )

    if len(epg_dvrs) > 1:
        print(
            f"Found {len(epg_dvrs)} EPG DVRs; using DVR key "
            f"{epg_dvrs[0].get('key')}."
        )

    return epg_dvrs[0]


def epg_provider_from_dvr(dvr: dict[str, Any]) -> tuple[str, str, str]:
    """Convert the existing Plex DVR lineup into its PMS EPG provider path."""
    lineup = str(dvr.get("lineup", ""))
    device_id = str(dvr.get("key", "")).strip()

    match = re.search(r"tv\.plex\.providers\.epg\.([a-zA-Z0-9_-]+)", lineup)
    if not match:
        raise RuntimeError(
            f"Could not determine Plex EPG provider type from lineup: {lineup}"
        )

    identifier = match.group(1)
    provider = f"/tv.plex.providers.epg.{identifier}:{device_id}"
    return identifier, device_id, provider

def fetch_channels(provider: str) -> list[dict[str, Any]]:
    payload = plex_get(f"{provider}/lineups/dvr/channels")
    container = media_container(payload)
    channels = container.get("Channel") or []

    if not isinstance(channels, list):
        channels = [channels]

    channels = [c for c in channels if isinstance(c, dict)]

    if not channels:
        raise RuntimeError("Plex returned zero EPG channels")

    return channels


def clean_id(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"[\x00-\x1f\x7f]", "", text)


def lineup_channels_for_matching(current_channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if EPG_SOURCE == "dvr":
        return current_channels
    if EPG_SOURCE != "zipcode" or not EPG_LINEUP_ID:
        return current_channels
    from setup_lineup import plex_epg_get
    payload = plex_epg_get(f"/lineups/{EPG_LINEUP_ID}/channels", PLEX_TOKEN)
    channels = payload.get("Channel") or []
    if isinstance(channels, dict):
        channels = [channels]
    return [c for c in channels if isinstance(c, dict)]


def write_lineup_matches(current_channels: list[dict[str, Any]]) -> None:
    if not EPG_M3U_PATH:
        print("Lineup matches: skipped (EPG_M3U_PATH not configured)")
        return
    try:
        from setup_lineup import read_m3u, match_channels
        networks = read_m3u(EPG_M3U_PATH)
        channels = lineup_channels_for_matching(current_channels)
        matches = match_channels(networks, channels)
        output = Path("/config/lineup_matches.csv")
        temp = output.with_suffix(".csv.tmp")
        with temp.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["M3U_callsign", "M3U_number", "Guide_callsign", "Guide_number", "Method"])
            for m in matches:
                c = m["channel"] or {}
                writer.writerow([m["network"]["callsign"], m["network"]["number"], c.get("callSign", ""), c.get("vcn", ""), m["method"]])
        temp.replace(output)
        matched = sum(m["channel"] is not None for m in matches)
        print(f"Lineup matches: {matched}/{len(matches)} -> {output}")
    except Exception as exc:
        print(f"Lineup matches: failed ({exc})", file=sys.stderr)


def choose_xmltv_ids(channels: list[dict[str, Any]]) -> dict[str, str]:
    """
    Return mapping: Plex channel id/gridKey -> XMLTV channel id.

    Callsigns are convenient for an M3U because you can set:
        tvg-id="WGHPDT"

    If a callsign/VCN is duplicated, the Plex channel id is appended to keep
    XMLTV channel IDs unique.
    """
    chosen: dict[str, str] = {}
    used: dict[str, int] = {}

    for channel in channels:
        plex_id = clean_id(channel.get("id") or channel.get("gridKey"))
        grid_key = clean_id(channel.get("gridKey") or plex_id)
        callsign = clean_id(channel.get("callSign"))
        vcn = clean_id(channel.get("vcn"))

        if EPG_ID_MODE == "plex":
            base = plex_id or grid_key
        elif EPG_ID_MODE == "vcn":
            base = vcn or callsign or plex_id or grid_key
        else:
            base = callsign or vcn or plex_id or grid_key

        if not base:
            continue

        count = used.get(base, 0)
        used[base] = count + 1

        xmltv_id = base if count == 0 else f"{base}.{plex_id or count + 1}"
        chosen[grid_key] = xmltv_id

        if plex_id and plex_id != grid_key:
            chosen[plex_id] = xmltv_id

    return chosen


def write_channel_map(
    channels: list[dict[str, Any]],
    xmltv_ids: dict[str, str],
) -> None:
    output = Path(CHANNEL_MAP_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "xmltv_id",
            "vcn",
            "call_sign",
            "title",
            "plex_channel_id",
            "grid_key",
            "logo",
        ])

        for channel in channels:
            grid_key = clean_id(
                channel.get("gridKey") or channel.get("id")
            )
            writer.writerow([
                xmltv_ids.get(grid_key, ""),
                clean_id(channel.get("vcn")),
                clean_id(channel.get("callSign")),
                clean_id(channel.get("title")),
                clean_id(channel.get("id")),
                grid_key,
                clean_id(channel.get("thumb")),
            ])


def xmltv_timestamp(epoch: int | float) -> str:
    """
    XMLTV permits YYYYMMDDHHMMSS +0000.
    Using UTC avoids host/DST ambiguity.
    """
    dt = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return dt.strftime("%Y%m%d%H%M%S +0000")


def add_text(parent: ET.Element, tag: str, value: Any, **attrs: str) -> None:
    if value is None:
        return
    text = str(value).strip()
    if not text:
        return
    element = ET.SubElement(parent, tag, attrs)
    element.text = text


def channel_xml(
    root: ET.Element,
    channel: dict[str, Any],
    xmltv_id: str,
) -> None:
    node = ET.SubElement(root, "channel", {"id": xmltv_id})

    title = clean_id(channel.get("title"))
    callsign = clean_id(channel.get("callSign"))
    vcn = clean_id(channel.get("vcn"))
    language = clean_id(channel.get("language")) or "en"

    # Put callsign first because many IPTV clients show the first display-name.
    if callsign:
        add_text(node, "display-name", callsign, lang=language)
    if title and title != callsign:
        add_text(node, "display-name", title, lang=language)
    if vcn:
        add_text(node, "display-name", vcn)

    logo = clean_id(channel.get("thumb"))
    if logo:
        ET.SubElement(node, "icon", {"src": logo})


def find_airing_media(
    metadata: dict[str, Any],
    channel: dict[str, Any],
) -> dict[str, Any] | None:
    media_entries = metadata.get("Media") or []
    if not isinstance(media_entries, list):
        media_entries = [media_entries]

    wanted_grid_key = clean_id(channel.get("gridKey"))
    wanted_id = clean_id(channel.get("id"))
    wanted_callsign = clean_id(channel.get("callSign"))

    for media in media_entries:
        if not isinstance(media, dict):
            continue

        identifiers = {
            clean_id(media.get("gridKey")),
            clean_id(media.get("channelIdentifier")),
        }
        callsign = clean_id(media.get("channelCallSign"))

        if (
            wanted_grid_key in identifiers
            or wanted_id in identifiers
            or (wanted_callsign and callsign == wanted_callsign)
        ):
            return media

    for media in media_entries:
        if isinstance(media, dict) and media.get("beginsAt") and media.get("endsAt"):
            return media

    return None


def episode_number_xmltv(metadata: dict[str, Any]) -> str | None:
    season = metadata.get("parentIndex")
    episode = metadata.get("index")

    try:
        if season is None or episode is None:
            return None

        # XMLTV xmltv_ns is zero-based: season.episode.part
        season_zero = max(0, int(season) - 1)
        episode_zero = max(0, int(episode) - 1)
        return f"{season_zero}.{episode_zero}."
    except (TypeError, ValueError):
        return None


def programme_xml(
    root: ET.Element,
    metadata: dict[str, Any],
    media: dict[str, Any],
    xmltv_id: str,
    seen: set[tuple[str, int, int, str]],
) -> bool:
    try:
        begins_at = int(media["beginsAt"])
        ends_at = int(media["endsAt"])
    except (KeyError, TypeError, ValueError):
        return False

    if ends_at <= begins_at:
        return False

    programme_title = (
        metadata.get("grandparentTitle")
        if metadata.get("type") == "episode"
        else metadata.get("title")
    ) or metadata.get("title") or "Unknown"

    subtitle = (
        metadata.get("title")
        if metadata.get("type") == "episode"
        else None
    )

    dedupe_key = (
        xmltv_id,
        begins_at,
        ends_at,
        clean_id(metadata.get("ratingKey") or programme_title),
    )
    if dedupe_key in seen:
        return False
    seen.add(dedupe_key)

    node = ET.SubElement(
        root,
        "programme",
        {
            "start": xmltv_timestamp(begins_at),
            "stop": xmltv_timestamp(ends_at),
            "channel": xmltv_id,
        },
    )

    language = clean_id(metadata.get("language")) or "en"

    add_text(node, "title", programme_title, lang=language)
    add_text(node, "sub-title", subtitle, lang=language)
    add_text(node, "desc", metadata.get("summary"), lang=language)

    original_date = metadata.get("originallyAvailableAt")
    if original_date:
        # XMLTV date convention commonly accepts YYYYMMDD.
        compact = re.sub(r"[^0-9]", "", str(original_date))[:8]
        if len(compact) == 8:
            add_text(node, "date", compact)

    year = metadata.get("year")
    if year and not original_date:
        add_text(node, "date", year)

    content_rating = clean_id(metadata.get("contentRating"))
    if content_rating:
        rating = ET.SubElement(node, "rating")
        add_text(rating, "value", content_rating)

    genres = metadata.get("Genre") or []
    if not isinstance(genres, list):
        genres = [genres]

    for genre in genres:
        if isinstance(genre, dict):
            add_text(node, "category", genre.get("tag"))

    episode_ns = episode_number_xmltv(metadata)
    if episode_ns:
        add_text(node, "episode-num", episode_ns, system="xmltv_ns")

        season = metadata.get("parentIndex")
        episode = metadata.get("index")
        try:
            add_text(
                node,
                "episode-num",
                f"S{int(season):02d}E{int(episode):02d}",
                system="onscreen",
            )
        except (TypeError, ValueError):
            pass

    icon = (
        metadata.get("thumb")
        or metadata.get("art")
        or metadata.get("grandparentThumb")
    )
    if icon:
        ET.SubElement(node, "icon", {"src": str(icon)})

    return True


def fetch_channel_day(
    provider: str,
    channel: dict[str, Any],
    day: date,
) -> list[dict[str, Any]]:
    grid_key = clean_id(channel.get("gridKey") or channel.get("id"))
    if not grid_key:
        return []

    payload = plex_get(
        f"{provider}/grid",
        params={
            "channelGridKey": grid_key,
            "date": day.isoformat(),
        },
    )
    container = media_container(payload)
    metadata = container.get("Metadata") or []

    if not isinstance(metadata, list):
        metadata = [metadata]

    return [item for item in metadata if isinstance(item, dict)]


def indent_xml(tree: ET.ElementTree) -> None:
    # Python 3.9+
    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass


def export_epg() -> None:
    dvr = discover_dvr()
    identifier, device_id, provider = epg_provider_from_dvr(dvr)

    print(f"Plex server : {PLEX_SERVER}")
    print(f"DVR key     : {device_id}")
    print(f"EPG type    : {identifier}")
    print(f"Provider    : {provider}")
    print(f"Lineup      : {dvr.get('lineup')}")
    print()

    channels = fetch_channels(provider)
    xmltv_ids = choose_xmltv_ids(channels)

    usable_channels = [
        channel for channel in channels
        if clean_id(channel.get("gridKey") or channel.get("id")) in xmltv_ids
    ]

    print(f"Channels    : {len(usable_channels)}")
    print(f"Guide days  : {EPG_DAYS}")
    print(f"ID mode     : {EPG_ID_MODE}")
    print()

    write_channel_map(usable_channels, xmltv_ids)
    write_lineup_matches(usable_channels)

    root = ET.Element(
        "tv",
        {
            "generator-info-name": CLIENT_PRODUCT,
            "generator-info-url": "https://www.plex.tv/",
        },
    )

    for channel in usable_channels:
        grid_key = clean_id(channel.get("gridKey") or channel.get("id"))
        channel_xml(root, channel, xmltv_ids[grid_key])

    today = datetime.now().astimezone().date()
    seen_programmes: set[tuple[str, int, int, str]] = set()
    programme_count = 0

    total_requests = len(usable_channels) * EPG_DAYS
    request_number = 0

    for channel_index, channel in enumerate(usable_channels, start=1):
        grid_key = clean_id(channel.get("gridKey") or channel.get("id"))
        xmltv_id = xmltv_ids[grid_key]
        label = (
            clean_id(channel.get("callSign"))
            or clean_id(channel.get("title"))
            or xmltv_id
        )

        channel_programmes = 0

        for day_offset in range(EPG_DAYS):
            request_number += 1
            day = today + timedelta(days=day_offset)

            print(
                f"[{request_number:>4}/{total_requests}] "
                f"{label:<15} {day.isoformat()}",
                end="",
                flush=True,
            )

            try:
                metadata_items = fetch_channel_day(provider, channel, day)
            except Exception as exc:
                print(f"  ERROR: {exc}")
                continue

            added = 0
            for metadata in metadata_items:
                media = find_airing_media(metadata, channel)
                if not media:
                    continue

                if programme_xml(
                    root,
                    metadata,
                    media,
                    xmltv_id,
                    seen_programmes,
                ):
                    added += 1
                    programme_count += 1
                    channel_programmes += 1

            print(f"  {added} programmes")

        if channel_programmes == 0:
            print(f"Warning: no programmes returned for {label}")

    output = Path(EPG_OUTPUT)
    output.parent.mkdir(parents=True, exist_ok=True)

    tree = ET.ElementTree(root)
    indent_xml(tree)
    tree.write(
        output,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )

    print()
    print(f"Wrote XMLTV      : {output}")
    print(f"Wrote channel map: {CHANNEL_MAP_OUTPUT}")
    print(f"Channels         : {len(usable_channels)}")
    print(f"Programmes       : {programme_count}")


def main() -> int:
    try:
        configure()
        export_epg()
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
