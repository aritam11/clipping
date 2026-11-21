"""Final encode: reframe + cuts + captions + loudness normalisation -> upload-ready MP4."""

from __future__ import annotations

from pathlib import Path

from .reframe import OUT_H, OUT_W, Layout, video_filter
from .util import run


def render(src: Path, out: Path, lay: Layout, ass: Path | None, has_audio: bool,
           start: float | None = None, end: float | None = None,
           keep: list[tuple[float, float]] | None = None, first_frame: Path | None = None) -> Path:
    """keep: ranges of the clip (relative to start) to keep, for jump cuts; captions in `ass` must
    already be on the cut timeline. first_frame: image shown as the very first frame (the frame
    YouTube uses as a Short's default thumbnail)."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start is not None and end is not None:
        cmd += ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
    cmd += ["-i", str(src.resolve())]
    if first_frame:
        cmd += ["-i", str(first_frame.resolve())]

    fc = [video_filter(lay, None).replace("[vout]", "[vr]")]
    v, a = "[vr]", "[0:a:0]" if has_audio else None
    if keep and (len(keep) > 1 or keep[0][0] > 0.01 or (end is not None and start is not None
                                                        and keep[-1][1] < end - start - 0.01)):
        n = len(keep)
        fc.append(f"{v}split={n}" + "".join(f"[vs{i}]" for i in range(n)))
        if a:
            fc.append(f"{a}asplit={n}" + "".join(f"[as{i}]" for i in range(n)))
        pairs = []
        for i, (x, y) in enumerate(keep):
            fc.append(f"[vs{i}]trim=start={x:.3f}:end={y:.3f},setpts=PTS-STARTPTS[vk{i}]")
            pairs.append(f"[vk{i}]")
            if a:
                fc.append(f"[as{i}]atrim=start={x:.3f}:end={y:.3f},asetpts=PTS-STARTPTS[ak{i}]")
                pairs.append(f"[ak{i}]")
        fc.append("".join(pairs) + f"concat=n={n}:v=1:a={1 if a else 0}[vc]" + ("[ac]" if a else ""))
        v, a = "[vc]", ("[ac]" if a else None)
    if ass:
        # ass= takes a filename; running in its folder avoids FFmpeg filter-path escaping.
        fc.append(f"{v}ass={ass.name}[vsub]")
        v = "[vsub]"
    if first_frame:
        fc.append(f"[1:v]scale={OUT_W}:{OUT_H},setsar=1[ff]")
        fc.append(f"{v}[ff]overlay=0:0:enable='eq(n,0)'[vff]")
        v = "[vff]"
    if a:
        fc.append(f"{a}loudnorm=I=-14:TP=-1.5:LRA=11[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", v]
    if a:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-fps_mode", "cfr", "-r", "30", "-movflags", "+faststart", str(out.resolve())]
    run(cmd, cwd=ass.parent if ass else None)
    return out
