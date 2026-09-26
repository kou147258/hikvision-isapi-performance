#!/usr/bin/env python3
"""Probe new ISAPI endpoints on real Hikvision devices.

Usage:
    python test_new_endpoints.py --host 10.18.176.18 --port 80 --username admin --password YOURPASS
    python test_new_endpoints.py --host 192.168.10.10 --port 80 --username admin --password YOURPASS

Replace YOURPASS with your actual device password.

This script probes NEW endpoints not yet in the integration:
 1. PTZ presets list
 2. Per-channel recording status
 3. Motion detection (VMD) configuration
 4. Alert stream (5-second sample)
 5. Device log search
 6. NVR proxied snapshot
 7. Smart detection status (field/line)
 8. Video input channels
 9. Event notification hosts
10. Recording channels status

Each endpoint is probed and the raw XML response is printed.
Responses are saved to {host}_test_results.txt for review.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIClient,
    ISAPIError,
)


async def probe_get(client: ISAPIClient, path: str, label: str, results: list):
    """Probe a GET endpoint and record the result."""
    print(f"\n{'='*60}")
    print(f"[GET] {label}")
    print(f"  Path: {path}")
    print(f"{'='*60}")
    try:
        xml = await client.get_xml(path)
        print(f"  Status: OK ({len(xml)} chars)")
        # Print first 500 chars
        preview = xml[:500]
        print(f"  Preview: {preview}")
        if len(xml) > 500:
            print(f"  ... (truncated, full response in output file)")
        results.append(f"\n{'='*60}\n[GET] {label}\nPath: {path}\nStatus: OK ({len(xml)} chars)\n{'='*60}\n{xml}\n")
    except ISAPIError as err:
        print(f"  Error: {err}")
        results.append(f"\n{'='*60}\n[GET] {label}\nPath: {path}\nStatus: ERROR - {err}\n{'='*60}\n")
    except Exception as err:
        print(f"  Exception: {type(err).__name__}: {err}")
        results.append(f"\n{'='*60}\n[GET] {label}\nPath: {path}\nStatus: EXCEPTION - {type(err).__name__}: {err}\n{'='*60}\n")


async def probe_post(client: ISAPIClient, path: str, body: str, label: str, results: list):
    """Probe a POST endpoint and record the result."""
    print(f"\n{'='*60}")
    print(f"[POST] {label}")
    print(f"  Path: {path}")
    print(f"{'='*60}")
    try:
        url = f"{client.base_url}{path}"
        response = await client._client.post(
            url,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )
        xml = response.text
        if response.status_code == 401:
            # Try with digest re-auth
            xml = await client._read_response_text(response)
        print(f"  Status: {response.status_code} ({len(xml)} chars)")
        preview = xml[:500]
        print(f"  Preview: {preview}")
        results.append(f"\n{'='*60}\n[POST] {label}\nPath: {path}\nStatus: {response.status_code} ({len(xml)} chars)\n{'='*60}\n{xml}\n")
    except ISAPIError as err:
        print(f"  Error: {err}")
        results.append(f"\n{'='*60}\n[POST] {label}\nPath: {path}\nStatus: ERROR - {err}\n{'='*60}\n")
    except Exception as err:
        print(f"  Exception: {type(err).__name__}: {err}")
        results.append(f"\n{'='*60}\n[POST] {label}\nPath: {path}\nStatus: EXCEPTION - {type(err).__name__}: {err}\n{'='*60}\n")


async def probe_get_bytes(client: ISAPIClient, path: str, label: str, results: list):
    """Probe a GET endpoint that returns binary data (e.g. JPEG)."""
    print(f"\n{'='*60}")
    print(f"[GET BYTES] {label}")
    print(f"  Path: {path}")
    print(f"{'='*60}")
    try:
        data = await client.get_bytes(path)
        print(f"  Status: OK ({len(data)} bytes)")
        print(f"  Content type: {'JPEG' if data[:2] == b'\\xff\\xd8' else 'Unknown'}")
        results.append(f"\n{'='*60}\n[GET BYTES] {label}\nPath: {path}\nStatus: OK ({len(data)} bytes)\nContent: {'JPEG image' if data[:2] == b'\\xff\\xd8' else 'Unknown format'}\n{'='*60}\n")
    except ISAPIError as err:
        print(f"  Error: {err}")
        results.append(f"\n{'='*60}\n[GET BYTES] {label}\nPath: {path}\nStatus: ERROR - {err}\n{'='*60}\n")
    except Exception as err:
        print(f"  Exception: {type(err).__name__}: {err}")
        results.append(f"\n{'='*60}\n[GET BYTES] {label}\nPath: {path}\nStatus: EXCEPTION - {type(err).__name__}: {err}\n{'='*60}\n")


async def main():
    parser = argparse.ArgumentParser(description="Probe new ISAPI endpoints")
    parser.add_argument("--host", required=True, help="Device IP address")
    parser.add_argument("--port", type=int, default=80, help="Device port (default: 80)")
    parser.add_argument("--username", default="admin", help="Username (default: admin)")
    parser.add_argument("--password", required=True, help="Password")
    parser.add_argument("--https", action="store_true", help="Use HTTPS")
    parser.add_argument("--channel", type=int, default=1, help="Primary channel ID (default: 1)")
    args = parser.parse_args()

    client = ISAPIClient(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        use_https=args.https,
        verify_ssl=False,
    )

    results: list[str] = []
    ch = args.channel

    print(f"\n{'#'*60}")
    print(f"# Probing ISAPI endpoints on {args.host}:{args.port}")
    print(f"# Primary channel: {ch}")
    print(f"{'#'*60}")

    async with client:
        # First, get device info to identify device type
        await probe_get(client, "/ISAPI/System/deviceInfo", "Device Info (baseline)", results)

        # 1. PTZ Presets List
        await probe_get(client, f"/ISAPI/PTZCtrl/channels/{ch}/presets",
                        f"1. PTZ Presets (channel {ch})", results)

        # 2. Per-channel recording status
        for i in range(1, 9):
            await probe_get(client, f"/ISAPI/ContentMgmt/Recording/channels/{i}/status",
                            f"2. Recording Status (channel {i})", results)
            if i == 4:
                # Only test 1-4 to avoid too many requests
                break

        # Also try the bulk endpoint
        await probe_get(client, "/ISAPI/ContentMgmt/Recording/channels",
                        "2b. All Recording Channels", results)

        # 3. Motion Detection (VMD) configuration
        for i in range(1, 5):
            await probe_get(client, f"/ISAPI/Event/triggers/VMD-{i}",
                            f"3. Motion Detection VMD-{i}", results)

        # 4. Alert stream (just check if endpoint exists, don't actually stream)
        await probe_get(client, "/ISAPI/Event/notification/alertStream",
                        "4. Alert Stream (endpoint check only)", results)

        # 5. Device log search
        log_search_body = """<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>probe-test</searchID>
  <searchResultPosition>0</searchResultPosition>
  <maxResults>5</maxResults>
  <majorEventType>0</majorEventType>
  <minorEventType>0</minorEventType>
</CMSearchDescription>"""
        await probe_post(client, "/ISAPI/ContentMgmt/logSearch", log_search_body,
                         "5. Log Search", results)

        # 6. NVR proxied snapshot
        await probe_get_bytes(client, f"/ISAPI/ContentMgmt/StreamingProxy/channels/{ch}01/picture",
                              f"6. NVR Proxied Snapshot (ch {ch}01)", results)

        # 7. Smart detection - field detection
        await probe_get(client, f"/ISAPI/Smart/FieldDetection/{ch}",
                        f"7. Field Detection (channel {ch})", results)

        # Smart detection - line detection
        await probe_get(client, f"/ISAPI/Smart/LineDetection/{ch}",
                        f"7b. Line Detection (channel {ch})", results)

        # 8. Video input channels
        await probe_get(client, "/ISAPI/System/Video/inputs/channels",
                        "8. Video Input Channels", results)

        # 9. Event notification hosts
        await probe_get(client, "/ISAPI/Event/notification/httpHosts",
                        "9. Event Notification Hosts", results)

        # 10. Recording control channels (alternative to per-channel status)
        for i in range(1, 5):
            await probe_get(client, f"/ISAPI/ContentMgmt/Recording/channels/{i}01",
                            f"10. Recording Control (ch {i}01)", results)

    # Save results to file
    output_file = Path(f"{args.host}_test_results.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"ISAPI Endpoint Probe Results for {args.host}:{args.port}\n")
        f.write(f"{'='*60}\n\n")
        for r in results:
            f.write(r)
    print(f"\n{'#'*60}")
    print(f"# Results saved to: {output_file}")
    print(f"{'#'*60}")


if __name__ == "__main__":
    asyncio.run(main())
