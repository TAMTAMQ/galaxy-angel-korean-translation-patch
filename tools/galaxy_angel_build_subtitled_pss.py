from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from galaxy_angel_pss_analyze import Packet, parse_program_stream

PICTURE_START = b"\x00\x00\x01\x00"


@dataclass(frozen=True)
class Picture:
    offset: int
    temporal_reference: int
    picture_type: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_pictures(es: bytes) -> list[Picture]:
    out: list[Picture] = []
    pos = 0
    while True:
        pos = es.find(PICTURE_START, pos)
        if pos < 0:
            break
        if pos + 6 > len(es):
            raise ValueError(f"truncated picture header at ES offset 0x{pos:x}")
        temporal_reference = (es[pos + 4] << 2) | (es[pos + 5] >> 6)
        picture_type = (es[pos + 5] >> 3) & 0x07
        out.append(Picture(pos, temporal_reference, picture_type))
        pos += 4
    return out


def reconstruct_video_es(data: bytes, packets: list[Packet]) -> tuple[bytes, list[tuple[int, int, Packet]]]:
    es = bytearray()
    ranges: list[tuple[int, int, Packet]] = []
    for p in packets:
        if not (0xE0 <= p.stream_id <= 0xEF):
            continue
        start = len(es)
        es.extend(data[p.payload_offset : p.payload_offset + p.payload_size])
        ranges.append((start, len(es), p))
    return bytes(es), ranges


def original_timeline(data: bytes, packets: list[Packet]) -> tuple[bytes, list[Picture], list[tuple[int, int, Packet]]]:
    es, ranges = reconstruct_video_es(data, packets)
    pics = parse_pictures(es)
    timed_ranges = [(s, e, p) for s, e, p in ranges if p.pts is not None]
    if len(pics) != len(timed_ranges):
        raise ValueError(f"original picture/timestamp count mismatch: pictures={len(pics)} timed_pes={len(timed_ranges)}")
    for i, (pic, (s, e, _p)) in enumerate(zip(pics, timed_ranges)):
        if not (s <= pic.offset < e):
            raise ValueError(f"original picture {i} is outside its timed PES: pic={pic.offset}, pes=[{s},{e})")
    return es, pics, timed_ranges


def get_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("ffmpeg is unavailable; install imageio-ffmpeg or put ffmpeg on PATH") from exc


def encode_m2v(
    ffmpeg: str,
    source: Path,
    subtitle: Path,
    output: Path,
    i_display_frames: list[int],
    bitrate_kbps: int,
    vbv_bytes: int,
    natural_gop: bool = False,
) -> list[str]:
    force_times = ",".join(f"{frame / 24:.6f}" for frame in i_display_frames)
    # Use a relative subtitle path from the movie directory when possible. This avoids
    # Windows drive-colon escaping inside libavfilter's ass= argument.
    cwd = source.parent.parent
    try:
        sub_arg = subtitle.resolve().relative_to(cwd.resolve()).as_posix()
        src_arg = source.resolve().relative_to(cwd.resolve()).as_posix()
        out_arg = output.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:
        sub_arg = subtitle.as_posix().replace(":", "\\:")
        src_arg = str(source)
        out_arg = str(output)

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        src_arg,
        "-map",
        "0:v:0",
        "-vf",
        f"ass={sub_arg}",
        "-an",
        "-c:v",
        "mpeg2video",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "24",
        "-aspect",
        "4:3",
        "-g",
        "12",
        "-bf",
        "2",
    ]
    if not natural_gop:
        cmd.extend([
            "-sc_threshold",
            "1000000000",
            "-force_key_frames",
            force_times,
        ])
    cmd.extend([
        "-b:v",
        f"{bitrate_kbps}k",
        "-minrate",
        f"{bitrate_kbps}k",
        "-maxrate",
        "6000k",
        "-bufsize",
        str(vbv_bytes),
        "-qmin",
        "2",
        "-qmax",
        "31",
        "-trellis",
        "1",
        "-f",
        "mpeg2video",
        out_arg,
    ])
    subprocess.run(cmd, cwd=cwd, check=True)
    return cmd


def encode_ts(value: int, prefix: int) -> bytes:
    if value < 0:
        raise ValueError(f"negative MPEG timestamp: {value}")
    value &= (1 << 33) - 1
    return bytes(
        [
            ((prefix & 0x0F) << 4) | (((value >> 30) & 0x07) << 1) | 1,
            (value >> 22) & 0xFF,
            (((value >> 15) & 0x7F) << 1) | 1,
            (value >> 7) & 0xFF,
            ((value & 0x7F) << 1) | 1,
        ]
    )


def build_video_pes(payload: bytes, pts: int | None = None, dts: int | None = None) -> bytes:
    if pts is None:
        optional = b"\x83\x00\x00"
    elif dts is None:
        optional = b"\x83\x80\x0a" + encode_ts(pts, 0x2) + (b"\xff" * 5)
    else:
        optional = b"\x83\xc0\x0a" + encode_ts(pts, 0x3) + encode_ts(dts, 0x1)
    packet_len = len(optional) + len(payload)
    if packet_len > 0xFFFF:
        raise ValueError(f"video PES too large: {packet_len + 6}")
    return b"\x00\x00\x01\xe0" + packet_len.to_bytes(2, "big") + optional + payload


def parse_gop_timecode_frame(es: bytes, offset: int, fps: int = 24) -> tuple[int, bool]:
    if es[offset : offset + 4] != b"\x00\x00\x01\xb8" or offset + 8 > len(es):
        raise ValueError(f"invalid GOP header at ES offset 0x{offset:x}")
    b4, b5, b6, b7 = es[offset + 4 : offset + 8]
    hours = (b4 >> 2) & 0x1F
    minutes = ((b4 & 0x03) << 4) | (b5 >> 4)
    seconds = ((b5 & 0x07) << 3) | (b6 >> 5)
    pictures = ((b6 & 0x1F) << 1) | (b7 >> 7)
    closed_gop = bool((b7 >> 6) & 1)
    return (((hours * 60 + minutes) * 60 + seconds) * fps + pictures), closed_gop


def picture_display_frames(es: bytes, pics: list[Picture], fps: int = 24) -> tuple[list[int], list[bool]]:
    gops: list[tuple[int, int, bool]] = []
    pos = 0
    while True:
        pos = es.find(b"\x00\x00\x01\xb8", pos)
        if pos < 0:
            break
        frame, closed = parse_gop_timecode_frame(es, pos, fps)
        gops.append((pos, frame, closed))
        pos += 4
    if not gops:
        raise ValueError("encoded MPEG-2 stream has no GOP headers")

    display_frames: list[int] = []
    closed_flags: list[bool] = []
    gop_i = 0
    for pic in pics:
        while gop_i + 1 < len(gops) and gops[gop_i + 1][0] < pic.offset:
            gop_i += 1
        gop_offset, base_frame, closed = gops[gop_i]
        if gop_offset > pic.offset:
            raise ValueError(f"picture at 0x{pic.offset:x} precedes first GOP header")
        display_frames.append(base_frame + pic.temporal_reference)
        closed_flags.append(closed)

    expected = list(range(len(pics)))
    if sorted(display_frames) != expected:
        raise ValueError(
            "GOP timecode/temporal_reference does not form a complete CFR display timeline: "
            f"min={min(display_frames)} max={max(display_frames)} unique={len(set(display_frames))}/{len(pics)}"
        )
    return display_frames, closed_flags


def build_padding_packet(total_size: int) -> bytes:
    if total_size < 7:
        raise ValueError(f"padding packet requires at least 7 bytes, got {total_size}")
    payload_len = total_size - 6
    if payload_len > 0xFFFF:
        raise ValueError(f"padding packet too large: {total_size}")
    return b"\x00\x00\x01\xBE" + payload_len.to_bytes(2, "big") + (b"\xFF" * payload_len)


def fit_payload_length(capacity: int, wanted: int) -> int:
    if wanted >= capacity:
        return capacity
    if wanted <= 0:
        return 0
    remainder = capacity - wanted
    if 0 < remainder < 7:
        wanted -= 7 - remainder
    return max(0, wanted)


def repack_pss(original_data: bytes, packets: list[Packet], new_es: bytes, new_pics: list[Picture]) -> tuple[bytes, dict[str, int]]:
    out = bytearray(original_data)
    video_packets = [p for p in packets if 0xE0 <= p.stream_id <= 0xEF]
    timed_video = [p for p in video_packets if p.pts is not None]
    if len(timed_video) != len(new_pics):
        raise ValueError(f"new picture/timestamp mismatch: pictures={len(new_pics)} timed_pes={len(timed_video)}")

    es_pos = 0
    next_picture = 0
    padding_packet_count = 0
    padding_bytes = 0
    partial_video_packets = 0
    full_video_packets = 0
    converted_video_packets = 0

    for p in video_packets:
        timed = p.pts is not None
        current_picture = next_picture if timed else None
        if timed:
            if next_picture >= len(new_pics):
                raise ValueError("extra timed video PES after final picture")
            pic = new_pics[next_picture]
            if es_pos > pic.offset:
                raise ValueError(f"picture {next_picture} was emitted before its timed PES: es_pos={es_pos}, pic={pic.offset}")
            barrier = new_pics[next_picture + 1].offset if next_picture + 1 < len(new_pics) else len(new_es)
        else:
            barrier = new_pics[next_picture].offset if next_picture < len(new_pics) else len(new_es)

        wanted = min(p.payload_size, max(0, barrier - es_pos), len(new_es) - es_pos)
        take = fit_payload_length(p.payload_size, wanted)

        if timed:
            pic = new_pics[next_picture]
            # The picture start plus the two picture-header bytes used for temporal/type
            # must be present in the PES that carries its timestamp.
            if es_pos + take < pic.offset + 6:
                raise ValueError(
                    f"picture {next_picture} misses its timed PES capacity: "
                    f"es_pos={es_pos}, take={take}, pic={pic.offset}, cap={p.payload_size}, pss=0x{p.offset:x}"
                )

        header_size = p.payload_offset - p.offset
        if take == p.payload_size:
            out[p.payload_offset : p.payload_offset + take] = new_es[es_pos : es_pos + take]
            full_video_packets += 1
        elif take == 0:
            replacement = build_padding_packet(p.size)
            out[p.offset : p.offset + p.size] = replacement
            padding_packet_count += 1
            padding_bytes += p.size
            converted_video_packets += 1
        else:
            video_size = header_size + take
            remainder = p.size - video_size
            if remainder < 6:
                raise AssertionError("fit_payload_length failed to leave room for padding packet")
            video_packet = bytearray(original_data[p.offset : p.payload_offset])
            pes_len = video_size - 6
            if not (0 <= pes_len <= 0xFFFF):
                raise ValueError(f"invalid shortened PES length {pes_len}")
            video_packet[4:6] = pes_len.to_bytes(2, "big")
            video_packet.extend(new_es[es_pos : es_pos + take])
            replacement = bytes(video_packet) + build_padding_packet(remainder)
            if len(replacement) != p.size:
                raise AssertionError("replacement packet size changed")
            out[p.offset : p.offset + p.size] = replacement
            partial_video_packets += 1
            padding_packet_count += 1
            padding_bytes += remainder

        es_pos += take
        if timed:
            next_picture += 1

    if next_picture != len(new_pics):
        raise ValueError(f"not all pictures reached timed PES slots: {next_picture}/{len(new_pics)}")
    if es_pos != len(new_es):
        raise ValueError(f"video ES does not fit original schedule/capacity: placed={es_pos}, total={len(new_es)}")

    return bytes(out), {
        "full_video_packets": full_video_packets,
        "partial_video_packets": partial_video_packets,
        "converted_video_packets": converted_video_packets,
        "program_stream_padding_packets_added": padding_packet_count,
        "program_stream_padding_bytes_added": padding_bytes,
    }


def repack_pss_stream_fill(original_data: bytes, packets: list[Packet], new_es: bytes) -> tuple[bytes, dict[str, int]]:
    """Test-only repack: fill original video PES payload slots sequentially.

    This preserves every non-video packet and the original PTS/DTS-bearing PES headers,
    but does not require encoded picture starts to line up with the original timed PES
    boundaries. It is intended for in-game compatibility testing only.
    """
    out = bytearray(original_data)
    es_pos = 0
    padding_packet_count = 0
    padding_bytes = 0
    partial_video_packets = 0
    full_video_packets = 0
    converted_video_packets = 0

    for p in packets:
        if not (0xE0 <= p.stream_id <= 0xEF):
            continue
        wanted = min(p.payload_size, len(new_es) - es_pos)
        take = fit_payload_length(p.payload_size, wanted)
        header_size = p.payload_offset - p.offset

        if take == p.payload_size:
            out[p.payload_offset : p.payload_offset + take] = new_es[es_pos : es_pos + take]
            full_video_packets += 1
        elif take == 0:
            replacement = build_padding_packet(p.size)
            out[p.offset : p.offset + p.size] = replacement
            padding_packet_count += 1
            padding_bytes += p.size
            converted_video_packets += 1
        else:
            video_size = header_size + take
            remainder = p.size - video_size
            if remainder < 6:
                raise AssertionError("fit_payload_length failed to leave room for padding packet")
            video_packet = bytearray(original_data[p.offset : p.payload_offset])
            pes_len = video_size - 6
            if not (0 <= pes_len <= 0xFFFF):
                raise ValueError(f"invalid shortened PES length {pes_len}")
            video_packet[4:6] = pes_len.to_bytes(2, "big")
            video_packet.extend(new_es[es_pos : es_pos + take])
            replacement = bytes(video_packet) + build_padding_packet(remainder)
            if len(replacement) != p.size:
                raise AssertionError("replacement packet size changed")
            out[p.offset : p.offset + p.size] = replacement
            partial_video_packets += 1
            padding_packet_count += 1
            padding_bytes += remainder

        es_pos += take

    if es_pos != len(new_es):
        raise ValueError(f"video ES does not fit original payload capacity: placed={es_pos}, total={len(new_es)}")

    return bytes(out), {
        "full_video_packets": full_video_packets,
        "partial_video_packets": partial_video_packets,
        "converted_video_packets": converted_video_packets,
        "program_stream_padding_packets_added": padding_packet_count,
        "program_stream_padding_bytes_added": padding_bytes,
    }


def repack_pss_retimestamp(
    original_data: bytes,
    packets: list[Packet],
    new_es: bytes,
    new_pics: list[Picture],
    first_pts: int,
    first_dts: int,
    frame_duration: int = 3750,
) -> tuple[bytes, dict[str, object], list[tuple[int, int | None]]]:
    """Repacketize video into the original PSS slots and timestamp the PES that contains each picture start."""
    out = bytearray(original_data)
    display_frames, closed_flags = picture_display_frames(new_es, new_pics)
    expected_timestamps: list[tuple[int, int | None]] = []
    for decode_i, (pic, display_frame) in enumerate(zip(new_pics, display_frames)):
        pts = first_pts + display_frame * frame_duration
        dts = first_dts + decode_i * frame_duration if pic.picture_type in (1, 2) else None
        expected_timestamps.append((pts, dts))

    es_pos = 0
    pic_i = 0
    timed_packets = 0
    untimed_packets = 0
    padding_packets = 0
    padding_bytes = 0
    partial_slots = 0
    converted_slots = 0

    def replacement_for_slot(slot_size: int, timed: bool) -> tuple[bytes, int, bool]:
        nonlocal es_pos, pic_i, timed_packets, untimed_packets, padding_packets, padding_bytes, partial_slots
        if es_pos >= len(new_es):
            pad = build_padding_packet(slot_size)
            padding_packets += 1
            padding_bytes += slot_size
            return pad, 0, False

        header_size = 19 if timed else 9
        capacity = slot_size - header_size
        if capacity <= 0:
            raise ValueError(f"video slot too small for PES header: {slot_size}")
        end = min(len(new_es), es_pos + capacity)
        if pic_i < len(new_pics):
            next_pic = new_pics[pic_i].offset
            if not timed and next_pic < end:
                end = next_pic
            if timed and pic_i + 1 < len(new_pics):
                next_next = new_pics[pic_i + 1].offset
                if next_next < end:
                    end = next_next
        take = end - es_pos
        if take < 0:
            raise ValueError(f"negative video payload take at ES {es_pos}")

        remainder = slot_size - (header_size + take)
        if 0 < remainder < 7:
            take -= 7 - remainder
            if take < 0:
                raise ValueError("cannot leave legal padding packet in video slot")
            remainder = slot_size - (header_size + take)

        if timed:
            if pic_i >= len(new_pics):
                raise ValueError("timed packet requested after final picture")
            pic = new_pics[pic_i]
            if not (es_pos <= pic.offset < es_pos + take):
                raise ValueError(
                    f"timed PES cannot contain picture {pic_i}: es=[{es_pos},{es_pos + take}) pic={pic.offset}"
                )
            pts, dts = expected_timestamps[pic_i]
            payload = new_es[es_pos : es_pos + take]
            video_packet = build_video_pes(payload, pts, dts)
            pic_i += 1
            timed_packets += 1
        else:
            payload = new_es[es_pos : es_pos + take]
            video_packet = build_video_pes(payload)
            untimed_packets += 1

        es_pos += take
        if remainder:
            pad = build_padding_packet(remainder)
            padding_packets += 1
            padding_bytes += remainder
            partial_slots += 1
            replacement = video_packet + pad
        else:
            replacement = video_packet
        if len(replacement) != slot_size:
            raise AssertionError(f"retimestamp replacement size changed: {len(replacement)} != {slot_size}")
        return replacement, take, timed

    for p in packets:
        if not (0xE0 <= p.stream_id <= 0xEF):
            continue
        if es_pos >= len(new_es):
            replacement = build_padding_packet(p.size)
            out[p.offset : p.offset + p.size] = replacement
            padding_packets += 1
            padding_bytes += p.size
            converted_slots += 1
            continue

        timed = False
        if pic_i < len(new_pics):
            next_pic = new_pics[pic_i].offset
            timed_capacity = p.size - 19
            if es_pos <= next_pic < es_pos + timed_capacity:
                timed = True

        if not timed and pic_i < len(new_pics):
            next_pic = new_pics[pic_i].offset
            untimed_capacity = p.size - 9
            if es_pos < next_pic < es_pos + untimed_capacity:
                # The 10-byte timestamp header shrink would exclude the picture start.
                # Stop before it so the following slot can carry the timestamp cleanly.
                take = next_pic - es_pos
                remainder = p.size - (9 + take)
                if 0 < remainder < 7:
                    take -= 7 - remainder
                    remainder = p.size - (9 + take)
                packet = build_video_pes(new_es[es_pos : es_pos + take])
                replacement = packet + build_padding_packet(remainder)
                if len(replacement) != p.size:
                    raise AssertionError("boundary-shift replacement size changed")
                out[p.offset : p.offset + p.size] = replacement
                es_pos += take
                untimed_packets += 1
                padding_packets += 1
                padding_bytes += remainder
                partial_slots += 1
                continue

        replacement, _take, _timed = replacement_for_slot(p.size, timed)
        out[p.offset : p.offset + p.size] = replacement

    if es_pos != len(new_es):
        raise ValueError(f"retimestamp video ES does not fit original PSS slots: placed={es_pos}, total={len(new_es)}")
    if pic_i != len(new_pics):
        raise ValueError(f"retimestamp did not assign all pictures: {pic_i}/{len(new_pics)}")

    return bytes(out), {
        "timed_video_packets": timed_packets,
        "untimed_video_packets": untimed_packets,
        "converted_video_slots": converted_slots,
        "partial_video_slots": partial_slots,
        "program_stream_padding_packets_added": padding_packets,
        "program_stream_padding_bytes_added": padding_bytes,
        "closed_gop_picture_count": sum(1 for flag, pic in zip(closed_flags, new_pics) if flag and pic.picture_type == 1),
        "display_frame_min": min(display_frames),
        "display_frame_max": max(display_frames),
    }, expected_timestamps


def verify_output(
    original_path: Path,
    output_path: Path,
    expected_es: bytes,
    *,
    require_picture_timestamp_alignment: bool = True,
    expected_timestamps: list[tuple[int, int | None]] | None = None,
) -> dict[str, object]:
    original_data, original_packets = parse_program_stream(original_path)
    output_data, output_packets = parse_program_stream(output_path)
    if len(output_data) != len(original_data):
        raise ValueError("PSS size changed")

    # Every original non-video packet must remain byte-for-byte at the same file offset.
    nonvideo_checked = 0
    audio_checked = 0
    for p in original_packets:
        if 0xE0 <= p.stream_id <= 0xEF:
            continue
        if output_data[p.offset : p.offset + p.size] != original_data[p.offset : p.offset + p.size]:
            raise ValueError(f"non-video packet changed at 0x{p.offset:x}, stream=0x{p.stream_id:02x}")
        nonvideo_checked += 1
        if p.stream_id == 0xBD:
            audio_checked += 1

    rebuilt_es, output_video_ranges = reconstruct_video_es(output_data, output_packets)
    if rebuilt_es != expected_es:
        raise ValueError(f"reconstructed output video ES mismatch: expected={len(expected_es)} got={len(rebuilt_es)}")

    original_timed = [(p.pts, p.dts) for p in original_packets if 0xE0 <= p.stream_id <= 0xEF and p.pts is not None]
    output_timed = [(p.pts, p.dts) for p in output_packets if 0xE0 <= p.stream_id <= 0xEF and p.pts is not None]
    target_timed = expected_timestamps if expected_timestamps is not None else original_timed
    if output_timed != target_timed:
        mismatch_i = next(
            (i for i, (a, b) in enumerate(zip(output_timed, target_timed)) if a != b),
            min(len(output_timed), len(target_timed)),
        )
        raise ValueError(
            f"output video PTS/DTS list differs at {mismatch_i}: "
            f"output_count={len(output_timed)} expected_count={len(target_timed)}"
        )

    output_pics = parse_pictures(rebuilt_es)
    output_timed_ranges = [(s, e, p) for s, e, p in output_video_ranges if p.pts is not None]
    if len(output_pics) != len(output_timed_ranges):
        raise ValueError("output picture/timed-PES count mismatch")
    same_pes = 0
    for i, (pic, (s, e, _p)) in enumerate(zip(output_pics, output_timed_ranges)):
        if s <= pic.offset < e:
            same_pes += 1
        elif require_picture_timestamp_alignment:
            raise ValueError(f"output picture {i} is outside timed PES: pic={pic.offset}, range=[{s},{e})")

    audio_hash = hashlib.sha256()
    for p in output_packets:
        if p.stream_id == 0xBD:
            audio_hash.update(output_data[p.offset : p.offset + p.size])

    return {
        "pss_size": len(output_data),
        "packet_count": len(output_packets),
        "nonvideo_packets_byte_exact": nonvideo_checked,
        "audio_packets_byte_exact": audio_checked,
        "private_audio_packet_sha256": audio_hash.hexdigest(),
        "video_es_size": len(rebuilt_es),
        "video_picture_count": len(output_pics),
        "timed_picture_same_pes_count": same_pes,
        "video_timestamp_count": len(output_timed),
        "video_pts_dts_exact": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a PS2 PSS with burned Korean subtitles while preserving original audio/timestamps.")
    ap.add_argument("--original", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--subtitle", type=Path, required=True)
    ap.add_argument("--output-m2v", type=Path, required=True)
    ap.add_argument("--output-pss", type=Path, required=True)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--bitrate-kbps", type=int, default=6000)
    ap.add_argument("--vbv-bytes", type=int, default=1835008)
    ap.add_argument("--skip-encode", action="store_true")
    ap.add_argument(
        "--allow-gop-mismatch",
        action="store_true",
        help="Build a test PSS even when the encoded MPEG-2 GOP/picture structure differs from the original.",
    )
    ap.add_argument(
        "--force-stream-fill",
        action="store_true",
        help="Test-only: sequentially fill original video PES slots without picture/timestamp boundary alignment.",
    )
    ap.add_argument(
        "--natural-gop",
        action="store_true",
        help="Let the MPEG-2 encoder choose scene-cut I frames instead of forcing the original I timestamps.",
    )
    ap.add_argument(
        "--retimestamp-pictures",
        action="store_true",
        help="Repacketize video and regenerate PTS/DTS from the encoded GOP timecode/temporal_reference so every picture start carries its own timestamp.",
    )
    args = ap.parse_args()

    original_data, original_packets = parse_program_stream(args.original)
    original_es, original_ranges = reconstruct_video_es(original_data, original_packets)
    original_pics = parse_pictures(original_es)
    timed_ranges = [(s, e, p) for s, e, p in original_ranges if p.pts is not None]
    if not timed_ranges:
        raise ValueError("original PSS has no timed video PES packets")
    first_pts = timed_ranges[0][2].pts
    first_dts = next((p.dts for _s, _e, p in timed_ranges if p.dts is not None), None)
    if first_pts is None:
        raise ValueError("first original timed video PES lacks PTS")
    if first_dts is None:
        raise ValueError("original PSS has no video DTS")

    # Some movies (notably GADAT101) legitimately have fewer timed video PES packets
    # than MPEG pictures. Natural-GOP retimestamp mode does not need a 1:1 original
    # picture/timestamp mapping: it regenerates one timestamped PES per encoded picture
    # while preserving every non-video packet at its original byte offset. Keep the
    # stricter historical mapping for modes that depend on original I-frame timing.
    if len(original_pics) == len(timed_ranges):
        for i, (pic, (s, e, _p)) in enumerate(zip(original_pics, timed_ranges)):
            if not (s <= pic.offset < e):
                raise ValueError(f"original picture {i} is outside its timed PES: pic={pic.offset}, pes=[{s},{e})")
        i_display_frames = [
            round((int(timed_ranges[i][2].pts) - int(first_pts)) / 3750)
            for i, pic in enumerate(original_pics)
            if pic.picture_type == 1 and timed_ranges[i][2].pts is not None
        ]
    elif args.natural_gop and args.retimestamp_pictures:
        i_display_frames = []
    else:
        raise ValueError(
            f"original picture/timestamp count mismatch: pictures={len(original_pics)} "
            f"timed_pes={len(timed_ranges)}; use --natural-gop --retimestamp-pictures"
        )

    ffmpeg = get_ffmpeg()
    encode_cmd: list[str] | None = None
    args.output_m2v.parent.mkdir(parents=True, exist_ok=True)
    args.output_pss.parent.mkdir(parents=True, exist_ok=True)
    if not args.skip_encode:
        encode_cmd = encode_m2v(
            ffmpeg,
            args.source,
            args.subtitle,
            args.output_m2v,
            i_display_frames,
            args.bitrate_kbps,
            args.vbv_bytes,
            args.natural_gop,
        )

    new_es = args.output_m2v.read_bytes()
    new_pics = parse_pictures(new_es)
    if len(new_pics) != len(original_pics):
        raise ValueError(f"encoded picture count differs: original={len(original_pics)} new={len(new_pics)}")

    original_structure = [(p.temporal_reference, p.picture_type) for p in original_pics]
    new_structure = [(p.temporal_reference, p.picture_type) for p in new_pics]
    mismatch = next((i for i, (a, b) in enumerate(zip(original_structure, new_structure)) if a != b), None)
    if mismatch is not None and not (args.allow_gop_mismatch or args.retimestamp_pictures):
        lo = max(0, mismatch - 6)
        hi = min(len(original_structure), mismatch + 8)
        original_window = original_structure[lo:hi]
        new_window = new_structure[lo:hi]
        raise ValueError(
            f"GOP/picture structure differs at decode-order picture {mismatch}: "
            f"original={original_structure[mismatch]} new={new_structure[mismatch]}; "
            f"window[{lo}:{hi}] original={original_window} new={new_window}"
        )

    if len(new_es) > len(original_es):
        raise ValueError(f"encoded video exceeds original video payload capacity: {len(new_es)} > {len(original_es)}")

    expected_timestamps: list[tuple[int, int | None]] | None = None
    if args.retimestamp_pictures:
        output_data, packing, expected_timestamps = repack_pss_retimestamp(
            original_data,
            original_packets,
            new_es,
            new_pics,
            int(first_pts),
            int(first_dts),
        )
    elif args.force_stream_fill:
        output_data, packing = repack_pss_stream_fill(original_data, original_packets, new_es)
    else:
        output_data, packing = repack_pss(original_data, original_packets, new_es, new_pics)
    temp_output = args.output_pss.with_suffix(args.output_pss.suffix + ".tmp")
    temp_output.write_bytes(output_data)
    verify = verify_output(
        args.original,
        temp_output,
        new_es,
        require_picture_timestamp_alignment=not args.force_stream_fill,
        expected_timestamps=expected_timestamps,
    )
    temp_output.replace(args.output_pss)

    report = {
        "schema": "galaxy-angel-subtitled-pss/v2",
        "source_mkv": str(args.source.resolve()),
        "subtitle": str(args.subtitle.resolve()),
        "original_pss": str(args.original.resolve()),
        "output_pss": str(args.output_pss.resolve()),
        "output_m2v": str(args.output_m2v.resolve()),
        "ffmpeg": ffmpeg,
        "bitrate_kbps": args.bitrate_kbps,
        "vbv_bytes": args.vbv_bytes,
        "pss_size": len(output_data),
        "video_payload_capacity": len(original_es),
        "encoded_video_size": len(new_es),
        "remaining_video_capacity": len(original_es) - len(new_es),
        "frame_count": len(new_pics),
        "i_frame_count": sum(p.picture_type == 1 for p in new_pics),
        "p_frame_count": sum(p.picture_type == 2 for p in new_pics),
        "b_frame_count": sum(p.picture_type == 3 for p in new_pics),
        "gop_structure_exact": mismatch is None,
        "force_stream_fill": args.force_stream_fill,
        "retimestamp_pictures": args.retimestamp_pictures,
        "packing": packing,
        "verify": verify,
        "encoded_video_sha256": sha256_bytes(new_es),
        "output_pss_sha256": sha256_bytes(output_data),
        "original_pss_sha256": sha256_file(args.original),
        "encode_command": encode_cmd,
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report_text + "\n", encoding="utf-8")
    print(report_text)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg failed with exit code {exc.returncode}", file=sys.stderr)
        raise
