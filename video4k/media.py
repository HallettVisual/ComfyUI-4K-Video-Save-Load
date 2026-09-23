"""ffmpeg probing, decoding and encoding for the Video 4K nodes.

Decoding always goes through ffmpeg: it scales far faster than doing it after the
fact in Python, and for 8-bit 4:2:0 sources it can hand us nv12, which is a third
of the bytes rgb24 would be and leaves the colour conversion to the GPU.
"""
import os
import re
import shutil
import subprocess

import torch

from . import accel

ENCODE_ARGS = ("utf-8", "backslashreplace")


def _find_ffmpeg():
    if "VHS_FORCE_FFMPEG_PATH" in os.environ:
        return os.environ["VHS_FORCE_FFMPEG_PATH"]
    system = shutil.which("ffmpeg")
    if system is not None:
        return system
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except ImportError:
        return None


ffmpeg_path = _find_ffmpeg()

# Sources where nv12 is a chroma relayout rather than a resample.
NV12_SOURCES = {"yuv420p", "yuvj420p", "nv12", "nv21"}
DEEP_PIX_FMT = re.compile(r"(9|10|12|14|16)(le|be)$")


class Probe:
    """What one ffmpeg -i pass tells us about a file."""

    def __init__(self, width, height, fps, duration, pix_fmt, tags, alpha, has_audio):
        self.width = width
        self.height = height
        self.fps = fps
        self.duration = duration
        self.pix_fmt = pix_fmt
        self.tags = tags
        self.alpha = alpha
        self.has_audio = has_audio

    @property
    def frame_count(self):
        return int(round(self.duration * self.fps))

    def matrix(self):
        """The colour matrix to use, or None when we should let ffmpeg decide."""
        for tag in self.tags:
            if tag in accel.MATRIX_ALIASES:
                return accel.MATRIX_ALIASES[tag]
        if self.pix_fmt in NV12_SOURCES:
            # Untagged. Every player assumes bt709 for HD and bt601 below it;
            # ffmpeg's own converter assumes bt601 everywhere, which tints HD.
            return "bt709" if self.height >= 720 else "bt601"
        return None


def probe(path):
    args = [ffmpeg_path, "-hide_banner", "-i", path, "-frames:v", "1", "-f", "null", "-"]
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    text = result.stderr.decode(*ENCODE_ARGS)

    for line in text.split("\n"):
        match = re.search(r"^ *Stream .* Video.*, ([1-9]|\d{2,})x(\d+)", line)
        if match is None:
            continue
        width, height = int(match.group(1)), int(match.group(2))
        fps_match = re.search(r", ([\d.]+) fps", line)
        fps = float(fps_match.group(1)) if fps_match else 1.0
        fmt = re.search(r", (\w+)(\(([^)]*)\))?, ([1-9]|\d{2,})x(\d+)", line)
        pix_fmt = fmt.group(1) if fmt else None
        # ffmpeg collapses matrix/primaries/transfer when they agree and prints
        # "bt709/unknown/unknown" when they don't, so cut at the first slash.
        tags = [t.strip().split("/")[0] for t in ((fmt.group(3) if fmt else "") or "").split(",")]
        alpha = re.search(r"(yuva|rgba|bgra|gbra)", line) is not None
        break
    else:
        raise RuntimeError(f"Could not read video information from {path}.\nffmpeg said:\n{text}")

    duration = 0.0
    dur_match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
    if dur_match:
        duration = int(dur_match.group(1)) * 3600 + int(dur_match.group(2)) * 60 + float(dur_match.group(3))
    has_audio = re.search(r"^ *Stream .* Audio:", text, re.M) is not None
    return Probe(width, height, fps, duration, pix_fmt, tags, alpha, has_audio)


def decode(path, info, size, start_time=0.0, force_rate=0.0, frame_cap=0,
           select_every_nth=1, pbar=None):
    """Yield HWC float32 RGB numpy frames, scaled to `size` by ffmpeg."""
    matrix = info.matrix()
    deep = DEEP_PIX_FMT.search(info.pix_fmt or "") is not None
    even = size[0] % 2 == 0 and size[1] % 2 == 0

    if info.alpha:
        pix_fmt, channels = ("rgba64le", 4) if deep else ("rgba", 4)
    elif deep:
        pix_fmt, channels = "rgb48le", 3
    elif info.pix_fmt in NV12_SOURCES and matrix is not None and even:
        pix_fmt, channels = "nv12", 3
    else:
        pix_fmt, channels = "rgb24", 3

    args = [ffmpeg_path, "-v", "error", "-an"]
    if start_time > 0:
        # Coarse seek before the input is near-instant; the rest is exact.
        args += (["-ss", str(start_time - 4), "-i", path, "-ss", "4"]
                 if start_time > 4 else ["-i", path, "-ss", str(start_time)])
    else:
        args += ["-i", path]

    filters = []
    if force_rate:
        filters.append(f"fps=fps={force_rate}")
    if select_every_nth > 1:
        filters.append(f"select=not(mod(n\\,{select_every_nth}))")
    if (size[0], size[1]) != (info.width, info.height):
        aspect = size[0] / size[1]
        if abs(info.width / info.height - aspect) > 1e-4:
            filters.append(f"crop=if(gt({aspect}\\,a)\\,iw\\,ih*{aspect})"
                           f":if(gt({aspect}\\,a)\\,iw/{aspect}\\,ih)")
        filters.append(f"scale={size[0]}:{size[1]}:flags=lanczos")
    if filters:
        args += ["-vf", ",".join(filters)]
    if frame_cap > 0:
        args += ["-frames:v", str(frame_cap)]
    args += ["-pix_fmt", pix_fmt, "-f", "rawvideo", "-"]

    if pix_fmt == "nv12":
        frame_bytes = size[0] * size[1] * 3 // 2
        full_range = "pc" in info.tags
        to_rgb = lambda buf: accel.nv12_to_rgb(buf, size[0], size[1], matrix, full_range)
    else:
        dtype = torch.uint8 if pix_fmt in ("rgba", "rgb24") else torch.uint16
        frame_bytes = size[0] * size[1] * channels * dtype.itemsize
        to_rgb = lambda buf: accel.packed_to_rgb(buf, size[0], size[1], channels, dtype)

    with subprocess.Popen(args, stdout=subprocess.PIPE, bufsize=0) as proc:
        try:
            for index, frame in enumerate(accel.read_frames(proc.stdout, frame_bytes)):
                yield to_rgb(frame).cpu().numpy()
                if pbar is not None:
                    pbar.update(1)
        finally:
            proc.kill()


def encode(path, args, frames, env=None):
    """Feed already-converted frame bytes to ffmpeg, reporting its errors."""
    with subprocess.Popen(args + [path], stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env) as proc:
        try:
            for frame in accel.prefetch(frames):
                proc.stdin.write(frame)
            proc.stdin.close()
        except BrokenPipeError:
            raise RuntimeError("ffmpeg stopped while encoding:\n"
                               + proc.stderr.read().decode(*ENCODE_ARGS))
        message = proc.stderr.read().decode(*ENCODE_ARGS)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed writing {os.path.basename(path)}:\n{message}")
    return message


def mux_audio(video_path, output_path, waveform, sample_rate, audio_args):
    """Remux an encoded video with a raw float32 waveform, without re-encoding video."""
    channels = waveform.size(0)
    args = [ffmpeg_path, "-v", "error", "-y", "-i", video_path,
            "-ar", str(sample_rate), "-ac", str(channels), "-f", "f32le", "-i", "-",
            "-c:v", "copy"] + audio_args + ["-shortest", output_path]
    data = waveform.transpose(0, 1).contiguous().numpy().tobytes()
    result = subprocess.run(args, input=data, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg failed muxing audio:\n" + result.stderr.decode(*ENCODE_ARGS))


def read_audio(path, start_time, duration, sample_rate=44100):
    """Decode a slice of a file's audio to a [channels, samples] float32 tensor."""
    args = [ffmpeg_path, "-v", "error"]
    if start_time > 0:
        args += ["-ss", str(start_time)]
    args += ["-i", path]
    if duration > 0:
        args += ["-t", str(duration)]
    args += ["-vn", "-ac", "2", "-ar", str(sample_rate), "-f", "f32le", "-"]
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0 or len(result.stdout) == 0:
        return None
    audio = torch.frombuffer(bytearray(result.stdout), dtype=torch.float32)
    return audio.reshape(-1, 2).transpose(0, 1)


def silence(seconds, sample_rate=44100, channels=2):
    return torch.zeros((channels, max(1, int(round(seconds * sample_rate)))))
