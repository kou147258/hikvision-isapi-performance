"""Cross-entry duplicate detection (v0.8).

The same physical camera can be reachable two ways at once:

  * directly, as its own HA config entry (``10.18.176.10``), and
  * as a channel of an NVR entry that proxies it (``10.18.176.64`` ch1).

Probed on the fleet: NVR 176.64's 13 channels include **9 whose
``serialNumber`` is identical to 9 separately-configured IPCs** (ch1
摄像机12 = 10.18.176.10, ch3 摄像机09 = 10.18.176.12, ch4 摄像机10 =
10.18.176.13, ch5 摄像机14 = 10.18.176.16, ch9 = 10.18.176.51, ch10 =
10.18.176.52, ch11 = 10.18.176.53, ch12 = 10.18.176.17, ch13 =
10.18.176.18). Adding both the NVR and those IPCs therefore created two
full sets of entities per camera.

Chosen policy (user decision D1): keep both, but register the NVR-side
duplicates with ``entity_registry_enabled_default = False``. Nothing is
lost — the user can enable the NVR view (which is the only place that
sees all 13 channels' recording state) from the entity registry.

Identity-key reliability varies by firmware, which is why matching has
two tiers:
  * ``serialNumber`` — authoritative, but **absent on 176.65**
    (DS-7708N-I4 V4.1.18 returns an empty element for every channel).
  * ``sourceInputPortDescriptor/ipAddress`` — fallback for those. It is
    used ONLY when the channel reports no serial number; if a channel has
    a serial that doesn't match, we must not fall back to IP, because an
    address can be reassigned to different hardware over time.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "extract_identity",
    "collect_other_identities",
    "find_duplicate_channels",
    "should_disable_channel",
    "describe_duplicates",
]


def extract_identity(device_info: dict[str, Any], host: str) -> dict[str, Any]:
    """Build this entry's identity record from parsed ``deviceInfo``.

    ``host`` is kept because it is the fallback match key for channels
    whose firmware omits ``serialNumber``.
    """
    di = device_info or {}
    return {
        "serial_number": str(di.get("serialNumber") or "").strip(),
        "host": str(host or "").strip(),
        "model": str(di.get("model") or "").strip(),
    }


def collect_other_identities(hass: Any, own_entry_id: str) -> list[dict[str, Any]]:
    """Identities of every OTHER config entry in this domain.

    Entries whose device hasn't been probed yet contribute their host
    only (``serial_number`` empty). That is still useful: an NVR added
    second can match its channels against an IPC entry's host before the
    IPC's first refresh has recorded a serial number.

    The own entry is always excluded — otherwise every channel of an NVR
    would look like a duplicate of itself.
    """
    from .const import DOMAIN

    entries = hass.config_entries.async_entries(DOMAIN)
    others: list[dict[str, Any]] = []
    for other in entries:
        if getattr(other, "entry_id", None) == own_entry_id:
            continue
        data = getattr(other, "data", None) or {}
        host = str(data.get("host") or "").strip()
        if not host:
            continue
        others.append({
            "serial_number": str(data.get("identity_serial") or "").strip(),
            "host": host,
            "model": str(data.get("identity_model") or "").strip(),
        })
    return others


def find_duplicate_channels(
    channels: list[dict[str, Any]],
    other_identities: list[dict[str, Any]],
) -> set[str]:
    """Channel ids on this device that are the same hardware as another entry.

    Two-tier matching, per the firmware differences documented above:
      1. serial number (authoritative, when the channel reports one)
      2. source IP (only when the channel's serial is empty)
    """
    if not channels or not other_identities:
        return set()

    other_serials = {
        o["serial_number"] for o in other_identities if o.get("serial_number")
    }
    other_hosts = {o["host"] for o in other_identities if o.get("host")}

    duplicates: set[str] = set()
    for ch in channels:
        ch_id = str(ch.get("id", "")).strip()
        if not ch_id:
            continue
        serial = str(ch.get("serial_number") or "").strip()
        source_ip = str(ch.get("source_ip") or "").strip()

        if serial:
            if serial in other_serials:
                duplicates.add(ch_id)
            # A channel with a serial that matches nothing is NOT a
            # duplicate, even if its IP collides with another entry's
            # host — IPs get reassigned, serials don't.
            continue
        if source_ip and source_ip in other_hosts:
            duplicates.add(ch_id)
    return duplicates


def should_disable_channel(channel_id: Any, duplicates: set[str]) -> bool:
    """Whether entities for this channel should be disabled by default.

    Tolerates int/str channel ids (firmware and parsers differ).
    """
    if not duplicates:
        return False
    normalized = {str(d).strip() for d in duplicates}
    return str(channel_id).strip() in normalized


def describe_duplicates(
    channels: list[dict[str, Any]],
    other_identities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Human-readable duplicate report for logs and the diagnostics download.

    One record per duplicated channel: which channel, its camera name, and
    which other entry it is the same hardware as. Without this the user
    sees 9 unexpectedly-disabled entity groups and no explanation.
    """
    duplicates = find_duplicate_channels(channels, other_identities)
    if not duplicates:
        return []

    by_serial = {
        o["serial_number"]: o for o in other_identities if o.get("serial_number")
    }
    by_host = {o["host"]: o for o in other_identities if o.get("host")}

    report: list[dict[str, Any]] = []
    for ch in channels:
        ch_id = str(ch.get("id", "")).strip()
        if ch_id not in duplicates:
            continue
        serial = str(ch.get("serial_number") or "").strip()
        source_ip = str(ch.get("source_ip") or "").strip()
        match = by_serial.get(serial) or by_host.get(source_ip) or {}
        report.append({
            "channel_id": ch_id,
            "channel_name": str(ch.get("name") or f"Channel {ch_id}"),
            "duplicate_of": match.get("host", "") or "",
            "matched_by": "serial" if serial and serial in by_serial else "ip",
        })
    report.sort(key=lambda r: (len(r["channel_id"]), r["channel_id"]))
    return report
