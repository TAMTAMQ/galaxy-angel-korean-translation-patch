#!/usr/bin/env python3
"""Patch all localized MINI.DAT AGI/TAG images and their IDX.DAT mirrors."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from PIL import Image
import numpy as np

import galaxy_angel_build as builder
import galaxy_angel_gadat032 as texcodec
import ikusa_lz
from galaxy_angel_extract_minigame_images import (
    AGI_HEADER_SIZE, PSMT4, PSMT8, STORED_MAGIC,
    decode_agi, decode_ps2_palette, decode_tag_transfer,
    parse_tag_transfers, read_resource,
)
from galaxy_angel_patch_minigame_recipe import parse_named_nodes


def pixel_hash(image: Image.Image) -> str:
    im=image.convert("RGBA")
    h=hashlib.sha256()
    h.update(im.width.to_bytes(4,"little"))
    h.update(im.height.to_bytes(4,"little"))
    h.update(im.tobytes())
    return h.hexdigest()


def palette_tuples(values) -> list[tuple[int,int,int,int]]:
    return [tuple(value) for value in values]


def nearest_indices(image: Image.Image, palette: list[tuple[int,int,int,int]]) -> bytes:
    """Match the historical memoized quantizer, but vectorize palette distance.

    The old implementation bucketed source pixels by 4-level RGBA keys and used
    the first real pixel seen in each bucket to choose the nearest palette entry.
    Preserve that exact order-dependent rule so rebuilt MINI textures remain
    byte-for-byte compatible with previous results, while replacing the nested
    Python palette scan with NumPy broadcasting.
    """
    rgba=image.convert("RGBA")
    pixels=list(rgba.getdata())
    transparent=min(range(len(palette)),key=lambda i:palette[i][3])

    keys=[]
    first_pixels: dict[tuple[int,int,int,int], tuple[int,int,int,int]]={}
    for p in pixels:
        if p[3]<8:
            keys.append(None)
            continue
        key=(p[0]//4,p[1]//4,p[2]//4,p[3]//4)
        keys.append(key)
        first_pixels.setdefault(key,p)

    if not first_pixels:
        return bytes([transparent])*len(pixels)

    ordered_keys=list(first_pixels)
    source=np.asarray([first_pixels[key] for key in ordered_keys],dtype=np.int32)
    pal=np.asarray(palette,dtype=np.int32)
    diff=pal[None,:,:]-source[:,None,:]
    distance=(diff[:,:,0]**2 + diff[:,:,1]**2 + diff[:,:,2]**2 + 2*diff[:,:,3]**2)
    nearest=np.argmin(distance,axis=1)
    memo={key:int(nearest[i]) for i,key in enumerate(ordered_keys)}
    return bytes(transparent if key is None else memo[key] for key in keys)


def image_from_indices(indices: bytes, palette: list[tuple[int,int,int,int]],
                       size: tuple[int,int]) -> Image.Image:
    pixels=bytearray()
    for idx in indices:
        pixels.extend(palette[idx])
    return Image.frombytes("RGBA",size,bytes(pixels))


def encode_agi(raw: bytes, target: Image.Image) -> tuple[bytes,Image.Image]:
    width,height=struct.unpack_from("<HH",raw,0x18)
    if target.size!=(width,height):
        raise ValueError(f"AGI canvas mismatch {target.size}!={(width,height)}")
    count=width*height
    packed4=(count+1)//2
    expected4=AGI_HEADER_SIZE+packed4+16*4
    expected8=AGI_HEADER_SIZE+count+256*4
    out=bytearray(raw)
    if len(raw)==expected4:
        palette=palette_tuples(decode_ps2_palette(raw[AGI_HEADER_SIZE+packed4:expected4]))
        indices=nearest_indices(target,palette)
        packed=bytes((indices[i]&15)|((indices[i+1]&15)<<4) for i in range(0,count,2))
        out[AGI_HEADER_SIZE:AGI_HEADER_SIZE+packed4]=packed
    elif len(raw)==expected8:
        palette=palette_tuples(decode_ps2_palette(raw[AGI_HEADER_SIZE+count:expected8]))
        palette=[tuple(x) for x in texcodec.swizzle_ps2_clut([bytes(p) for p in palette])]
        indices=nearest_indices(target,palette)
        out[AGI_HEADER_SIZE:AGI_HEADER_SIZE+count]=indices
    else:
        raise ValueError(f"unsupported AGI layout {width}x{height} raw={len(raw)}")
    return bytes(out),image_from_indices(indices,palette,(width,height))


def encode_tag_transfer(raw: bytearray, meta: dict, target: Image.Image) -> Image.Image:
    width,height=int(meta["width"]),int(meta["height"])
    if target.size!=(width,height):
        raise ValueError(f"TAG canvas mismatch {target.size}!={(width,height)}")
    entries=int(meta.get("palette_entries") or 0)
    palette_offset=meta.get("palette_offset")
    if entries not in (16,256) or palette_offset is None:
        raise ValueError(f"indexed TAG transfer lacks palette: {meta}")
    palette_raw=bytes(raw[int(palette_offset):int(palette_offset)+entries*4])
    palette=palette_tuples(decode_ps2_palette(palette_raw))
    if entries==256:
        palette=[tuple(x) for x in texcodec.swizzle_ps2_clut([bytes(p) for p in palette])]
    indices=nearest_indices(target,palette)
    offset=int(meta["data_offset"])
    psm=int(str(meta["psm"]),16) if isinstance(meta["psm"],str) else int(meta["psm"])
    if psm==PSMT8:
        payload=indices
    elif psm==PSMT4:
        payload=bytes((indices[i]&15)|((indices[i+1]&15)<<4)
                      for i in range(0,len(indices),2))
    else:
        raise ValueError(f"unsupported translated TAG PSM {psm:#x}")
    if len(payload)!=int(meta["data_size"]):
        raise ValueError(f"TAG payload size changed {len(payload)}!={meta['data_size']}")
    raw[offset:offset+len(payload)]=payload
    return image_from_indices(indices,palette,(width,height))


def encode_resource(raw: bytes, resource: dict, patches: list[dict]) -> tuple[bytes,dict]:
    expected={}
    if resource["type"]=="agi":
        if len(patches)!=1:
            raise ValueError(f"AGI has {len(patches)} translated occurrences")
        rebuilt,quantized=encode_agi(raw,patches[0]["target"])
        expected[patches[0]["key"]]=pixel_hash(quantized)
        return rebuilt,expected
    if resource["type"]!="tag":
        raise ValueError(f"unsupported MINI image resource {resource['type']}")
    out=bytearray(raw)
    by_index={int(image["image_index"]):image for image in resource["images"]}
    for patch in patches:
        meta=by_index[int(patch["image_index"])]
        quantized=encode_tag_transfer(out,meta,patch["target"])
        expected[patch["key"]]=pixel_hash(quantized)
    return bytes(out),expected


def encode_block(raw: bytes, original_magic: int) -> bytes:
    if original_magic==ikusa_lz.MAGIC:
        return ikusa_lz.compress_optimal(raw)
    if original_magic==STORED_MAGIC:
        return struct.pack("<II",STORED_MAGIC,len(raw))+bytes(value^ikusa_lz.KEY for value in raw)
    raise ValueError(f"unknown MINI codec {original_magic:#x}")


def patch_iso(
    iso_path: Path,
    extraction: Path,
    translations: list[Path],
    report_path: Path,
    seed_iso_path: Path | None = None,
) -> None:
    manifest=json.loads((extraction/"manifest.json").read_text(encoding="utf-8"))
    localizations=[json.loads(path.read_text(encoding="utf-8")) for path in translations]
    resources={item["source_path"]:item for item in manifest["resources"]}

    patches_by_source=defaultdict(list)
    for localization in localizations:
      for item in localization["items"]:
        target=Image.open(extraction/"translated_png"/Path(*item["translated_png"].split("/"))).convert("RGBA")
        for occurrence in item["occurrences"]:
            key=f"{occurrence['source_path']}#{occurrence.get('image_index')}"
            patches_by_source[occurrence["source_path"]].append({
                "key":key,
                "image_index":occurrence.get("image_index"),
                "transfer_index":occurrence.get("transfer_index"),
                "original_hash":item["original_pixel_sha256"],
                "target":target,
                "audit_id":item["audit_id"],
                "translation":item["translation"],
            })

    seed_container=None
    seed_nodes=None
    seed_paths=None
    if seed_iso_path is not None:
        seed_image=seed_iso_path.read_bytes()
        seed_files=builder.iso_files(seed_image)
        seed_mini=builder.resolve_iso_file(seed_files,"MINI")
        seed_begin=seed_mini.extent*builder.SECTOR
        seed_container=seed_image[seed_begin:seed_begin+seed_mini.size]
        seed_nodes,_seed_string_base,seed_paths=parse_named_nodes(seed_container)

    image=bytearray(iso_path.read_bytes())
    files=builder.iso_files(image)
    mini_file=builder.resolve_iso_file(files,"MINI")
    begin=mini_file.extent*builder.SECTOR
    end=begin+mini_file.size
    container=bytearray(image[begin:end])
    original_records=builder.records(container)
    nodes,_string_base,paths=parse_named_nodes(container)
    manifest_by_path=resources

    leaf_offsets=sorted({node[3] for node in nodes if node[0]==0 and node[3]>0})
    next_offset={}
    for i,offset in enumerate(leaf_offsets):
        next_offset[offset]=leaf_offsets[i+1] if i+1<len(leaf_offsets) else len(container)

    central_updates={}
    expected_hashes={}
    resource_results=[]
    for source_path,patches in sorted(patches_by_source.items()):
        node_index=paths.get(source_path)
        if node_index is None:
            raise SystemExit(f"MINI path missing from ISO: {source_path}")
        kind,_name,_children,data_offset,raw_size,compressed_size=nodes[node_index]
        if kind!=0:
            raise SystemExit(f"MINI translated path is not a file: {source_path}")
        raw=read_resource(container,data_offset,raw_size,compressed_size)
        resource=manifest_by_path[source_path]

        # Refuse to patch if this rebuilt ISO no longer matches the reviewed PNG.
        if resource["type"]=="agi":
            current=[(None,decode_agi(raw))]
        else:
            transfers=parse_tag_transfers(raw)
            current=[]
            for meta in resource["images"]:
                transfer=next(t for t in transfers if t.index==int(meta["transfer_index"]))
                decoded=decode_tag_transfer(raw,transfer)
                if decoded is not None:
                    current.append((int(meta["image_index"]),decoded))
        current_by_index={idx:im for idx,im in current}
        for patch in patches:
            idx=patch["image_index"]
            current_image=current_by_index[idx]
            if pixel_hash(current_image)!=patch["original_hash"]:
                raise SystemExit(f"reviewed source changed: {patch['key']}")

        patched_raw,expected=encode_resource(raw,resource,patches)
        if len(patched_raw)!=raw_size:
            raise SystemExit(f"MINI raw size changed: {source_path}")
        magic=struct.unpack_from("<I",container,data_offset)[0]
        compression_source="recompressed"
        if seed_container is None:
            compressed=encode_block(patched_raw,magic)
        else:
            assert seed_nodes is not None and seed_paths is not None
            seed_index=seed_paths.get(source_path)
            if seed_index is None:
                raise SystemExit(f"MINI seed path missing: {source_path}")
            seed_kind,_seed_name,_seed_children,seed_offset,seed_raw_size,seed_size=seed_nodes[seed_index]
            if seed_kind!=0 or seed_raw_size!=raw_size:
                raise SystemExit(f"MINI seed resource identity mismatch: {source_path}")
            seed_raw=read_resource(seed_container,seed_offset,seed_raw_size,seed_size)
            if resource["type"]=="agi":
                seed_decoded={None:decode_agi(seed_raw)}
            else:
                seed_transfers=parse_tag_transfers(seed_raw)
                seed_by_transfer={item.index:item for item in seed_transfers}
                seed_decoded={}
                by_image={int(item["image_index"]):item for item in resource["images"]}
                for patch in patches:
                    idx=int(patch["image_index"])
                    meta=by_image[idx]
                    seed_decoded[idx]=decode_tag_transfer(
                        seed_raw,seed_by_transfer[int(meta["transfer_index"])]
                    )
            for patch in patches:
                if pixel_hash(seed_decoded[patch["image_index"]])!=expected[patch["key"]]:
                    raise SystemExit(f"MINI seed translation mismatch: {patch['key']}")
            compressed=bytes(seed_container[seed_offset:seed_offset+seed_size])
            if struct.unpack_from("<I",compressed,0)[0]!=magic:
                raise SystemExit(f"MINI seed codec mismatch: {source_path}")
            compression_source="verified_seed"
        capacity=next_offset[data_offset]-data_offset
        if len(compressed)>capacity:
            raise SystemExit(f"MINI slot overflow {source_path}: {len(compressed)}>{capacity}")
        container[data_offset:data_offset+capacity]=bytes(capacity)
        container[data_offset:data_offset+len(compressed)]=compressed
        record_pos=0x50+node_index*24+12
        struct.pack_into("<III",container,record_pos,data_offset,raw_size,len(compressed))
        central_updates[record_pos]=(data_offset,raw_size,len(compressed))
        expected_hashes.update(expected)
        resource_results.append({
            "source_path":source_path,
            "node_index":node_index,
            "data_offset":data_offset,
            "raw_size":raw_size,
            "compressed_before":compressed_size,
            "compressed_after":len(compressed),
            "slot_capacity":capacity,
            "translated_images":len(patches),
            "compression_source":compression_source,
        })

    image[begin:end]=container
    file_id,central_count=builder.patch_central_idx_records(
        image,files,original_records,central_updates)
    if central_count!=len(central_updates):
        raise SystemExit(f"central MINI mirror count mismatch {central_count}!={len(central_updates)}")

    # Decode every translated occurrence from the final container and compare
    # it with the palette-quantized expectation produced by the encoder.
    verify_container=image[begin:end]
    verified=0
    for source_path,patches in sorted(patches_by_source.items()):
        node_index=paths[source_path]
        _kind,_name,_children,data_offset,raw_size,compressed_size=struct.unpack_from(
            "<6I",verify_container,0x50+node_index*24)
        raw=read_resource(verify_container,data_offset,raw_size,compressed_size)
        resource=resources[source_path]
        if resource["type"]=="agi":
            decoded={None:decode_agi(raw)}
        else:
            transfers=parse_tag_transfers(raw)
            decoded={}
            by_transfer={t.index:t for t in transfers}
            for meta in resource["images"]:
                idx=int(meta["image_index"])
                if any(p["image_index"]==idx for p in patches):
                    decoded[idx]=decode_tag_transfer(raw,by_transfer[int(meta["transfer_index"])])
        for patch in patches:
            actual=pixel_hash(decoded[patch["image_index"]])
            if actual!=expected_hashes[patch["key"]]:
                raise SystemExit(f"MINI image round-trip failed: {patch['key']}")
            verified+=1

    image[begin:end]=verify_container
    iso_path.write_bytes(image)
    report={
        "schema":"galaxy-angel-mini-image-patch/v1",
        "mini_file_id":file_id,
        "translated_unique":sum(int(item["translated_unique"]) for item in localizations),
        "translated_occurrences":verified,
        "patched_resources":len(resource_results),
        "central_idx_records":central_count,
        "resources":resource_results,
    }
    report_path.parent.mkdir(parents=True,exist_ok=True)
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"MINI image patch: unique={report['translated_unique']} occurrences={verified} resources={len(resource_results)} IDX={central_count} file_id={file_id}")


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--iso",type=Path,required=True)
    parser.add_argument("--extraction",type=Path,required=True)
    parser.add_argument("--translations",type=Path,action="append",required=True)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--seed-iso",type=Path)
    args=parser.parse_args()
    patch_iso(args.iso,args.extraction,args.translations,args.report,args.seed_iso)


if __name__=="__main__":
    main()
