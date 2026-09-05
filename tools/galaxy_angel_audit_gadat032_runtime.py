#!/usr/bin/env python3
"""Audit Japanese GADAT032 assets against runtime cache containers."""

from __future__ import annotations

import argparse
import json
import mmap
from pathlib import Path

import galaxy_angel_build as builder
from galaxy_angel_gadat032 import named_records
from galaxy_angel_patch_gadat032_images import (
    RuntimeRegion,
    region_hits,
    runtime_capacity,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime-container", action="append")
    parser.add_argument("--strict-name-data-container", action="append")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    runtime_names = args.runtime_container or ["SLGRES"]
    strict_names = set(args.strict_name_data_container or runtime_names)
    selected = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = []
    with args.iso.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_READ
    ) as image:
        files = builder.iso_files(image)
        gadat = builder.resolve_iso_file(files, "GADAT032")
        gadat_begin = gadat.extent * builder.SECTOR
        container = bytearray(image[gadat_begin : gadat_begin + gadat.size])
        records = builder.records(container)
        by_name = named_records(container, records)
        regions = []
        for runtime_name in runtime_names:
            runtime = builder.resolve_iso_file(files, runtime_name)
            runtime_begin = runtime.extent * builder.SECTOR
            regions.append(
                RuntimeRegion(runtime_name, runtime_begin, runtime_begin + runtime.size)
            )
        missing_data = []
        for item in selected:
            name = item["name"].lower()
            if name not in by_name:
                raise SystemExit(f"manifest texture is missing from GADAT032: {name}")
            offset, _record, _raw_size, compressed_size = by_name[name]
            compressed = bytes(container[offset : offset + compressed_size])
            path = f"dat/gadat032/{name}".encode("ascii")
            name_hits = []
            data_hits = []
            runtime_slots = []
            for region in regions:
                region_name_hits = region_hits(image, path, region)
                region_data_hits = region_hits(image, compressed, region)
                name_hits.extend((region.name, hit) for hit in region_name_hits)
                data_hits.extend((region.name, hit) for hit in region_data_hits)
                if (
                    region.name in strict_names
                    and region_name_hits
                    and not region_data_hits
                ):
                    missing_data.append(f"{region.name}:{name}")
                runtime_slots.extend(
                    {
                        "container": region.name,
                        "iso_offset": hit,
                        "capacity": runtime_capacity(
                            image, hit, compressed_size, region
                        ),
                    }
                    for hit in region_data_hits
                )
            result.append(
                {
                    "name": name,
                    "category": item["category"],
                    "runtime_name_references": len(name_hits),
                    "runtime_data_copies": len(data_hits),
                    "runtime_slots": runtime_slots,
                }
            )
    if missing_data:
        raise SystemExit(
            "runtime names exist without matching compressed blocks: "
            + ", ".join(missing_data)
        )
    report = {
        "iso": str(args.iso.resolve()),
        "runtime_containers": runtime_names,
        "strict_name_data_containers": sorted(strict_names),
        "selected_count": len(result),
        "runtime_asset_count": sum(bool(item["runtime_data_copies"]) for item in result),
        "runtime_copy_count": sum(item["runtime_data_copies"] for item in result),
        "entries": result,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"audited {len(result)} Japanese image assets: "
        f"{report['runtime_asset_count']} assets / "
        f"{report['runtime_copy_count']} runtime copies in {', '.join(runtime_names)}"
    )


if __name__ == "__main__":
    main()
