#!/usr/bin/env python3
"""Live smoke check of the JNPA Simulated Port-Data API (dt.jnpa.in).

Exercises every endpoint class of Reference v2.0 through the production
client (integrations.jnpa_portdata) — NOT raw HTTP — so auth caching,
retry/backoff, D-defect defenses and the rate budget are all on the wire
path being proven:

  GET  /                          service description   (unauthenticated)
  GET  /v2/health                 liveness              (unauthenticated)
  POST /v2/auth/token             client key -> 1 h bearer
  GET  /v2/groups                 catalogue vs the 13 published slugs
  GET  /v2/groups/{g}/records     one page per indexed group (limit=3)
  GET  /v2/groups/{g}/records     both report groups (limit=5)
  GET  /v2/groups/bathymetry/...  static group (served empty)
  GET  /v2/files/{ref}            first file found: download + sha256 verify,
                                  then If-None-Match revalidation (expect 304)

Config comes from the usual JNPA_PORTDATA_* env vars; unset ones are read
from --env-file (default .env.local) without overriding the environment.
While the allowlisted egress IP is only reachable through the jnpa3 tunnel,
JNPA_PORTDATA_PROXY=socks5://65.2.212.121:1080 routes everything there.

Usage:
  .venv/bin/python scripts/jnpa_live_check.py
  .venv/bin/python scripts/jnpa_live_check.py --skip-file   # no file download

Exit status 0 iff every check passed.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_env_file(path: Path) -> None:
    """Export KEY=VALUE lines that are not already in the environment."""
    import os

    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


async def _run(skip_file: bool) -> int:
    from integrations.jnpa_portdata.client import JnpaPortDataClient
    from integrations.jnpa_portdata.exceptions import JnpaError
    from integrations.jnpa_portdata.schemas import (
        EXPECTED_GROUP_SLUGS,
        INDEXED_GROUPS,
        REPORT_GROUPS,
        STATIC_GROUPS,
    )

    client = JnpaPortDataClient()
    print(f"base URL : {client.api_url}")
    print(f"proxy    : {client.proxy or '(direct)'}")
    print(f"key set  : {client.configured}")
    print()

    results: List[Tuple[str, bool, str]] = []

    async def check(name: str, coro) -> Optional[object]:
        try:
            value = await coro
        except JnpaError as exc:
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 — smoke check must not die
            results.append((name, False, f"{type(exc).__name__}: {exc}"))
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
            return None
        results.append((name, True, ""))
        return value

    desc = await check("GET / (service description)", client.service_description())
    if desc:
        print(f"  ok   service: {desc.get('service') or desc.get('name') or '?'} "
              f"version={desc.get('version', '?')}")

    health = await check("GET /v2/health", client.health())
    if health:
        print(f"  ok   health: {health.get('status', '?')}")

    token = await check("POST /v2/auth/token", client.get_token())
    if token:
        print(f"  ok   token: client={token.client_id} org={token.organisation} "
              f"scopes={token.scopes} expires={token.expires_at}")
    if token is None:
        # Nothing authenticated can work; report and stop.
        _summary(results)
        return 1

    groups = await check("GET /v2/groups", client.list_groups())
    if groups:
        slugs = sorted(g.group for g in groups.groups)
        missing = sorted(set(EXPECTED_GROUP_SLUGS) - set(slugs))
        extra = sorted(set(slugs) - set(EXPECTED_GROUP_SLUGS))
        print(f"  ok   catalogue: {len(slugs)} groups"
              + (f" missing={missing}" if missing else "")
              + (f" extra={extra}" if extra else ""))

    file_candidate = None  # (fileRef, checksumSha256)
    for group in INDEXED_GROUPS:
        page = await check(
            f"records {group}",
            client.records_page(group, limit=3, order="desc"))
        if page is None:
            continue
        newest = page.items[0].publishedAt if page.items else None
        print(f"  ok   {group}: count={page.count} matched={page.matched} "
              f"hasMore={page.hasMore} newest={newest}")
        if file_candidate is None:
            for item in page.items:
                if item.file and item.file.fileRef:
                    file_candidate = (item.file.fileRef,
                                      item.file.checksumSha256)
                    break

    for group in sorted(REPORT_GROUPS):
        report = await check(f"report {group}",
                             client.get_report(group, limit=5))
        if report:
            print(f"  ok   {group}: count={report.count} "
                  f"delivery={report.delivery}")

    for group in sorted(STATIC_GROUPS):
        page = await check(f"records {group} (static)",
                           client.records_page(group, limit=3))
        if page:
            print(f"  ok   {group}: count={page.count} (static group)")

    if skip_file:
        print("  skip file download (--skip-file)")
    elif file_candidate is None:
        results.append(("file download", False,
                        "no record with a file reference found"))
        print("  FAIL file download: no record with a file reference found")
    else:
        ref, checksum = file_candidate
        fetch = await check(
            f"GET /v2/files/{ref}",
            client.fetch_file(ref, expected_sha256=checksum))
        if fetch:
            print(f"  ok   file {ref}: {fetch.size_bytes} B "
                  f"name={fetch.filename} sha256 verified")
            reval = await check(
                f"GET /v2/files/{ref} (If-None-Match)",
                client.fetch_file(ref, etag=fetch.etag or checksum))
            if reval:
                mark = "ok  " if reval.status == 304 else "warn"
                print(f"  {mark} revalidation: HTTP {reval.status} "
                      f"(304 expected)")

    observations = client.drain_observations()
    if observations:
        print(f"\ndefect observations ({len(observations)}):")
        for obs in observations:
            print(f"  [{obs.severity}] {obs.code} @ {obs.endpoint}: {obs.detail}")

    stats = client.request_stats()
    print(f"\nrequests={stats.request_count} retries={stats.retry_count} "
          f"rate-limit floor={stats.rate_limit_remaining_min}")

    return _summary(results)


def _summary(results: List[Tuple[str, bool, str]]) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(name, err) for name, ok, err in results if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'}: "
          f"{passed}/{len(results)} checks passed")
    for name, err in failed:
        print(f"  failed: {name} — {err}")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env.local"),
                        help="file supplying unset JNPA_PORTDATA_* vars")
    parser.add_argument("--skip-file", action="store_true",
                        help="skip the /v2/files download check")
    args = parser.parse_args()
    _load_env_file(Path(args.env_file))
    sys.exit(asyncio.run(_run(args.skip_file)))


if __name__ == "__main__":
    main()
