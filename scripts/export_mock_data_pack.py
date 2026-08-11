#!/usr/bin/env python3
"""Export the corpus-derived data pack for the Vercel mock server.

The shared mock (jnpa-mock-server, Express on Vercel) shipped with index
metadata captured from the live dt.jnpa.in API but WITHOUT the file bytes —
its /v2/files/{ref} endpoint generated placeholder stubs whose sha256 never
matched the advertised checksumSha256, so the uc3 client's download
verification failed on every indexed record. The live bytes are gone (the
API is closed and the EC2 raw store died with a container recreation), so
the pack is regenerated instead from the Digital Twin data corpus using the
SAME deterministic indexer the offline simulator uses
(ingest/jnpa_portdata_sim/seed.build_index): real corpus bytes, real
sha256 checksums, live-faithful record/file ids, original filenames (the
routing layer keys on filenames).

Emits into the mock repo:
  data/responses/group-<slug>.json   one per indexed group (items[] in the
                                     live envelope shape)
  data/files/<fileRef>/<filename>    the real bytes for every record (one
                                     directory per ref — fileRef tags are
                                     base64url and may themselves end in
                                     '_', so a separator inside one flat
                                     name would be ambiguous)
  data/responses/discovery-groups.json  updated messageTypes/records counts
                                     (names/descriptions/links preserved)

Report groups (berthing-reports, daily-reports) and bathymetry are left
untouched — their existing JSON packs already match what report_ingest
consumes.

Usage:
  python scripts/export_mock_data_pack.py \
      --corpus "/path/to/Digital Twin Data Corpus - Updated/Data" \
      --out    "/path/to/jnpa-mock-server"
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.jnpa_portdata_sim.seed import (  # noqa: E402
    GROUP_FOLDERS,
    REPORT_GROUPS,
    STATIC_GROUPS,
    build_index,
)

URL_PREFIX = "/poc-api-data-access"   # cosmetic parity with the live links


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True,
                        help="the corpus Data/ directory")
    parser.add_argument("--out", required=True,
                        help="the jnpa-mock-server repo root")
    args = parser.parse_args()

    corpus = Path(args.corpus)
    out = Path(args.out)
    responses = out / "data" / "responses"
    files_dir = out / "data" / "files"
    if not corpus.is_dir():
        parser.error(f"corpus dir not found: {corpus}")
    if not responses.is_dir():
        parser.error(f"{responses} missing — is --out the mock repo root?")

    index = build_index(corpus)

    if files_dir.exists():
        shutil.rmtree(files_dir)
    files_dir.mkdir(parents=True)

    counts = {}
    for group in GROUP_FOLDERS:
        if group in REPORT_GROUPS or group in STATIC_GROUPS:
            continue
        records = index.group_records(group)
        items = []
        for rec in records:
            item = rec.as_item()
            item["file"]["url"] = f"{URL_PREFIX}/v2/files/{rec.file_ref}"
            items.append(item)
            src = corpus / rec.rel_path
            ref_dir = files_dir / rec.file_ref
            ref_dir.mkdir()
            shutil.copyfile(src, ref_dir / rec.filename)
        doc = {"group": group, "delivery": "indexed", "items": items}
        (responses / f"group-{group}.json").write_text(
            json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
        counts[group] = len(items)

    # Refresh discovery metadata: keep names/descriptions/links, update the
    # per-group record counts and messageTypes to match the new items.
    discovery_path = responses / "discovery-groups.json"
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    for meta in discovery.get("groups", []):
        group = meta.get("group")
        if group not in counts:
            continue
        meta["records"] = counts[group]
        types = sorted({r.message_type for r in index.group_records(group)})
        if types:
            meta["messageTypes"] = types
    discovery_path.write_text(
        json.dumps(discovery, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8")

    total = sum(counts.values())
    size_mb = sum(f.stat().st_size for f in files_dir.rglob("*")
                  if f.is_file()) / 1e6
    print(f"exported {total} records across {len(counts)} indexed groups "
          f"({size_mb:.1f} MB of file bytes)")
    for group, n in sorted(counts.items()):
        print(f"  {group:20s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
