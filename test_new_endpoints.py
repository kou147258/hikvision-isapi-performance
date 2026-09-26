#!/usr/bin/env python3
"""Test new ISAPI endpoints on real Hikvision devices.

Usage:
    python test_new_endpoints.py --host 10.18.176.18 --port 80 --username admin --password YOUR_PASSWORD
    python test_new_endpoints.py --host 192.168.10.10 --port 80 --username admin --password YOUR_PASSWORD

This script probes the following NEW endpoints that are NOT yet in the integration:
1. PTZ presets list
2. Per-channel recording status
3. Motion detection (VMD) configuration
4. Event alert stream (5-second sample)
5. Device log search
6. NVR proxied snapshot
7. Smart detection status (field/line/region)
8. Video input channels info
9. Event notification hosts
"""

import argparse
import asyncio
import sys
import re
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIClient,
    ISAPIError,
)


def strip_xmlns(xml_text: str) -> str:
    return re.sub(r'\s+xmlns(?::\w+)?\s*=\s*["\'][^"\']*["\']', "", xml_text)


async def test_endpoint(client: ISAPIClient, method: str, path: str, body: str = "", label: str = ""):
    """Test a single endpoint and print the result."""
    print(f"\n{'='*70}")
    print(f"[{label}] {method} {path}")
    print(f"{'='*70}")
    try:
        if method == "GET":
            result = await client.get_xml(path)
        elif method == "PUT":
            result = await client.put_xml(path, body)
        elif method == "POST":
            # Use put_xml for POST (same logic, different method)
            import httpx
            url = f"{client.base_url}{path}"
            resp = await client._client.post(
                url,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/xml"},
            )
            result = strip_xmlns(resp.text)
        else:
            print(f"  Unknown method: {method}")
            return
        print(f"  Status: OK")
        # Print first 2000 chars of response
        if len(result) > 2000:
            print(f"  Response (first 2000 chars):\n{result[:2000]}")
            print(f"  ... [truncated, total {len(result)} chars]")
        else:
            print(f"  Response:\n{result}")
        return result
    except ISAPIError as e:
        print(f"  Error: {e}")
        return None
    except Exception as e:
        print(f"  Unexpected error: {type(e).__name__}: {e}")
        return None


async def main():
    parser = argparse.ArgumentParser(description="Test new ISAPI endpoints")
    parser.add_argument("--host", required=True, help="Device IP")
    parser.add_argument("--port", type=int, default=80, help="Device port")
    parser.add_argument("--username", default="admin", help="Username")
    parser.add_argument("--password", required=True, help="Password")
    parser.add_argument("--https", action="store_true", help="Use HTTPS")
    args = parser.parse_args()

    client = ISAPIClient(
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
        use_https=args.https,
        verify_ssl=False,
    )

    print(f"\n{'#'*70}")
    print(f"# Testing new ISAPI endpoints on {args.host}:{args.port}")
    print(f"{'#'*70}")

    async with client:
        # 1. PTZ Presets List
        await test_endpoint(client, "GET",
            "/ISAPI/PTZCtrl/channels/1/presets",
            label="1. PTZ Presets List (channel 1)")

        # 2. Per-channel Recording Status (channels 1-8)
        for ch in range(1, 9):
            await test_endpoint(client, "GET",
                f"/ISAPI/ContentMgmt/Recording/channels/{ch}/status",
                label=f"2. Recording Status (channel {ch})")

        # 3. Motion Detection (VMD) - channels 1-4
        for ch in range(1, 5):
            await test_endpoint(client, "GET",
                f"/ISAPI/Event/triggers/VMD-{ch}",
                label=f"3. Motion Detection VMD-{ch}")

        # 4. Event Triggers List (all triggers)
        await test_endpoint(client, "GET",
            "/ISAPI/Event/triggers",
            label="4. All Event Triggers")

        # 5. Alert Stream (just connect and read for 5 seconds)
        print(f"\n{'='*70}")
        print(f"[5. Alert Stream] GET /ISAPI/Event/notification/alertStream (5s sample)")
        print(f"{'='*70}")
        try:
            import httpx
            url = f"{client.base_url}/ISAPI/Event/notification/alertStream"
            async with client._client.stream("GET", url, timeout=8.0) as resp:
                print(f"  Stream status: {resp.status_code}")
                chunk_count = 0
                async for chunk in resp.aiter_text():
                    chunk_count += 1
                    if chunk_count <= 3:
                        print(f"  Chunk {chunk_count} ({len(chunk)} chars):")
                        print(f"  {chunk[:500]}")
                    if chunk_count >= 5:
                        break
                print(f"  Total chunks received: {chunk_count}")
        except httpx.ReadTimeout:
            print(f"  Stream timeout (expected for alert stream - it's a persistent connection)")
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {e}")

        # 6. Device Log Search (last 24 hours)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(hours=24)
        log_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<CMSearchDescription>
  <searchID>test-log-search</searchID>
  <searchResultPosition>0</searchResultPosition>
  <maxResults>10</maxResults>
  <majorEventType>0</majorEventType>
  <minorEventType>0</minorEventType>
  <startTime>{yesterday.strftime('%Y-%m-%dT%H:%M:%SZ')}</startTime>
  <endTime>{now.strftime('%Y-%m-%dT%H:%M:%SZ')}</endTime>
</CMSearchDescription>"""
        await test_endpoint(client, "POST",
            "/ISAPI/ContentMgmt/logSearch",
            body=log_body,
            label="6. Device Log Search (last 24h)")

        # 7. NVR Proxied Snapshot (just check if endpoint exists)
        await test_endpoint(client, "GET",
            "/ISAPI/ContentMgmt/StreamingProxy/channels/101/picture",
            label="7. NVR Proxied Snapshot (ch 101)")

        # 8. Smart Detection - Field Detection
        for ch in range(1, 5):
            await test_endpoint(client, "GET",
                f"/ISAPI/Smart/FieldDetection/{ch}",
                label=f"8. Smart Field Detection (channel {ch})")

        # 9. Smart Detection - Line Detection
        for ch in range(1, 5):
            await test_endpoint(client, "GET",
                f"/ISAPI/Smart/LineDetection/{ch}",
                label=f"9. Smart Line Detection (channel {ch})")

        # 10. Video Input Channels
        await test_endpoint(client, "GET",
            "/ISAPI/System/Video/inputs/channels",
            label="10. Video Input Channels")

        # 11. Event Notification Hosts
        await test_endpoint(client, "GET",
            "/ISAPI/Event/notification/httpHosts",
            label="11. Event Notification Hosts")

        # 12. Recording Control channels (for per-channel recording status)
        await test_endpoint(client, "GET",
            "/ISAPI/ContentMgmt/Recording/channels",
            label="12. All Recording Channels")

        # 13. Security users list
        await test_endpoint(client, "GET",
            "/ISAPI/Security/users",
            label="13. Security Users")

        # 14. Reboot count from system status (some devices have it)
        await test_endpoint(client, "GET",
            "/ISAPI/System/status",
            label="14. System Status (full)")

    print(f"\n{'#'*70}")
    print(f"# Testing complete for {args.host}:{args.port}")
    print(f"{'#'*70}")


if __name__ == "__main__":
    asyncio.run(main())
