"""Two video nodes: one to load, one to save. Both do their pixel work on the GPU."""
import datetime
import json
import os
import re

import numpy as np
import torch
from PIL import Image
from PIL.PngImagePlugin import PngInfo

import folder_paths
from comfy.utils import ProgressBar

from . import accel, media

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif")

# fps, the frame-count grid the model accepts as (divisor, remainder), and the
# multiple its VAE needs each dimension rounded to.
MODEL_PRESETS = {
    "none": {},
    "MiniMax-H3": {"fps": 24, "frames": (17, 5), "multiple": 32},
    "LTXV": {"fps": 24, "frames": (8, 1), "multiple": 32},
    "Wan": {"fps": 16, "frames": (4, 1), "multiple": 16},
    "Hunyuan": {"fps": 24, "frames": (4, 1), "multiple": 16},
}

# Target the named edge, keeping aspect ratio.
RESOLUTIONS = {
    "source": None,
    "2160p (4K)": ("height", 2160),
    "1440p": ("height", 1440),
    "1080p": ("height", 1080),
    "720p": ("height", 720),
    "480p": ("height", 480),
    "2048 wide": ("width", 2048),
    "1920 wide": ("width", 1920),
    "1024 wide": ("width", 1024),
    "custom": None,
}

ENCODERS = {
    "h264 (nvenc)": {"codec": "h264_nvenc", "ext": "mp4", "quality": "cq", "pix_fmt": "yuv420p"},
    "hevc (nvenc)": {"codec": "hevc_nvenc", "ext": "mp4", "quality": "cq", "pix_fmt": "yuv420p",
                     "extra": ["-tag:v", "hvc1"]},
    "av1 (nvenc)": {"codec": "av1_nvenc", "ext": "mp4", "quality": "cq", "pix_fmt": "yuv420p"},
    "h264 (cpu)": {"codec": "libx264", "ext": "mp4", "quality": "crf", "pix_fmt": "yuv420p"},
    "hevc (cpu)": {"codec": "libx265", "ext": "mp4", "quality": "crf", "pix_fmt": "yuv420p",
                   "extra": ["-tag:v", "hvc1"]},
    "prores": {"codec": "prores_ks", "ext": "mov", "quality": None, "pix_fmt": "yuv422p10le",
               "extra": ["-profile:v", "3"]},
}


def round_to(value, multiple):
    return max(multiple, int(round(value / multiple)) * multiple)


def target_size(width, height, resolution, custom_width, custom_height, multiple):
    """Work out the output size, keeping aspect ratio unless both customs are set."""
    preset = RESOLUTIONS.get(resolution)
    if resolution == "custom":
        if custom_width and custom_height:
            width, height = custom_width, custom_height
        elif custom_width:
            height, width = height * custom_width / width, custom_width
        elif custom_height:
            width, height = width * custom_height / height, custom_height
    elif preset is not None:
        edge, size = preset
        if edge == "height":
            width, height = width * size / height, size
        else:
            height, width = height * size / width, size
    return round_to(width, multiple), round_to(height, multiple)


def fit_frame_count(count, grid):
    """Largest frame count the model accepts that is no more than `count`."""
    divisor, remainder = grid
    if count < remainder:
        return 0
    return ((count - remainder) // divisor) * divisor + remainder


def list_videos(path):
    if os.path.isdir(path):
        return [os.path.join(path, f) for f in sorted(os.listdir(path))
                if f.lower().endswith(VIDEO_EXTENSIONS)]
    return [path]


class LoadVideo4K:
    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        files = sorted(f for f in os.listdir(input_dir)
                       if os.path.isfile(os.path.join(input_dir, f))
                       and f.lower().endswith(VIDEO_EXTENSIONS))
        return {
            "required": {
                "source": (["upload", "path"],),
                "video": (files or ["(no videos in input folder)"], {"video_upload": True}),
                "path": ("STRING", {"default": "", "tooltip": "A video file, or a folder to step through with video_index. Used when source is 'path'."}),
                "video_index": ("INT", {"default": 0, "min": 0, "max": 9999, "tooltip": "Which video in the folder to load. video_count tells you how many there are."}),
                "resolution": (list(RESOLUTIONS), {"default": "source"}),
                "custom_width": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 8}),
                "custom_height": ("INT", {"default": 0, "min": 0, "max": 16384, "step": 8}),
                "divisible_by": ([1, 2, 8, 16, 32, 64], {"default": 16}),
                "model_preset": (list(MODEL_PRESETS), {"default": "none", "tooltip": "Sets frame rate, rounds the size, and snaps the frame count to what the model accepts."}),
                "force_rate": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 240.0, "step": 0.01, "tooltip": "0 keeps the source rate. The model preset overrides this."}),
                "seconds_cap": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1, "tooltip": "Load at most this many seconds. 0 is unlimited."}),
                "frame_load_cap": ("INT", {"default": 0, "min": 0, "max": 100000, "tooltip": "Load at most this many frames. 0 is unlimited."}),
                "skip_first_seconds": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 36000.0, "step": 0.01}),
                "select_every_nth": ("INT", {"default": 1, "min": 1, "max": 100}),
                "audio_when_missing": (["silence", "none"], {"default": "silence", "tooltip": "What to output when the file has no audio track. 'silence' keeps downstream nodes working."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT", "FLOAT", "INT", "INT", "INT", "STRING")
    RETURN_NAMES = ("images", "audio", "frame_count", "fps", "width", "height", "video_count", "info")
    FUNCTION = "load"
    CATEGORY = "Video 4K"

    def load(self, source, video, path, video_index, resolution, custom_width, custom_height,
             divisible_by, model_preset, force_rate, seconds_cap, frame_load_cap,
             skip_first_seconds, select_every_nth, audio_when_missing):
        if media.ffmpeg_path is None:
            raise RuntimeError("ffmpeg was not found. Install it, or put it on PATH.")

        if source == "path":
            if not path.strip():
                raise RuntimeError("source is set to 'path' but no path was given.")
            candidates = list_videos(path.strip().strip('"'))
            if not candidates:
                raise RuntimeError(f"No video files found in {path}")
            file_path = candidates[video_index % len(candidates)]
        else:
            candidates = [video]
            file_path = folder_paths.get_annotated_filepath(video)
        if not os.path.isfile(file_path):
            raise RuntimeError(f"Not a file: {file_path}")

        info = media.probe(file_path)
        preset = MODEL_PRESETS[model_preset]
        multiple = max(int(divisible_by), preset.get("multiple", 1))
        size = target_size(info.width, info.height, resolution, custom_width, custom_height, multiple)

        rate = preset.get("fps") or force_rate or 0.0
        output_fps = (rate or info.fps) / select_every_nth

        available = info.duration - skip_first_seconds
        if available <= 0:
            raise RuntimeError(f"skip_first_seconds ({skip_first_seconds}) is past the end of a "
                               f"{info.duration:.2f}s video.")
        cap = int(available * (rate or info.fps) / select_every_nth)
        if seconds_cap > 0:
            cap = min(cap, int(seconds_cap * output_fps))
        if frame_load_cap > 0:
            cap = min(cap, frame_load_cap)
        if "frames" in preset:
            fitted = fit_frame_count(cap, preset["frames"])
            if fitted == 0:
                divisor, remainder = preset["frames"]
                raise RuntimeError(f"{model_preset} needs at least {remainder} frames but only "
                                   f"{cap} are available. Lower skip_first_seconds, or raise the caps.")
            cap = fitted

        pbar = ProgressBar(cap)
        # ffmpeg's -frames:v counts frames it outputs, which is after select.
        frames = media.decode(file_path, info, size, start_time=skip_first_seconds,
                              force_rate=rate, frame_cap=cap,
                              select_every_nth=select_every_nth, pbar=pbar)
        images = torch.from_numpy(np.fromiter(frames, np.dtype((np.float32, (size[1], size[0], 3)))))
        if len(images) == 0:
            raise RuntimeError(f"No frames were decoded from {os.path.basename(file_path)}.")
        if "frames" in preset and len(images) != cap:
            fitted = fit_frame_count(len(images), preset["frames"])
            images = images[:fitted]

        duration = len(images) / output_fps
        waveform = None
        if info.has_audio:
            waveform = media.read_audio(file_path, skip_first_seconds, duration)
        if waveform is None and audio_when_missing == "silence":
            waveform = media.silence(duration)
        audio = ({"waveform": waveform.unsqueeze(0), "sample_rate": 44100}
                 if waveform is not None else None)

        summary = json.dumps({
            "file": os.path.basename(file_path),
            "source_size": f"{info.width}x{info.height}",
            "source_fps": round(info.fps, 3),
            "source_duration": round(info.duration, 3),
            "loaded_size": f"{size[0]}x{size[1]}",
            "loaded_fps": round(output_fps, 3),
            "loaded_frames": len(images),
            "loaded_duration": round(len(images) / output_fps, 3),
            "has_audio": info.has_audio,
        }, indent=2)
        return (images, audio, len(images), float(output_fps), size[0], size[1],
                len(candidates), summary)

    @classmethod
    def IS_CHANGED(cls, source, video, path, video_index, **kwargs):
        target = path if source == "path" else video
        try:
            if source == "path":
                files = list_videos(path.strip().strip('"'))
                target = files[video_index % len(files)] if files else path
            else:
                target = folder_paths.get_annotated_filepath(video)
            return f"{target}:{os.path.getmtime(target)}"
        except Exception:
            return target


class SaveVideo4K:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "fps": ("FLOAT", {"default": 24.0, "min": 0.01, "max": 240.0, "step": 0.01}),
                "filename_prefix": ("STRING", {"default": "video4k/clip"}),
                "encoder": (list(ENCODERS), {"default": "h264 (nvenc)"}),
                "quality": ("INT", {"default": 19, "min": 1, "max": 51, "tooltip": "Lower is better quality and a bigger file. Ignored by prores."}),
                "save_output": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "audio": ("AUDIO",),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("path",)
    OUTPUT_NODE = True
    FUNCTION = "save"
    CATEGORY = "Video 4K"

    def save(self, images, fps, filename_prefix, encoder, quality, save_output,
             audio=None, prompt=None, extra_pnginfo=None):
        if media.ffmpeg_path is None:
            raise RuntimeError("ffmpeg was not found. Install it, or put it on PATH.")
        if len(images) == 0:
            raise RuntimeError("No frames to save.")

        spec = ENCODERS[encoder]
        output_dir = (folder_paths.get_output_directory() if save_output
                      else folder_paths.get_temp_directory())
        full_folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, output_dir, images.shape[2], images.shape[1])
        os.makedirs(full_folder, exist_ok=True)
        # get_save_image_path only counts .png, so find our own next free number.
        matcher = re.compile(re.escape(filename) + r"_(\d+)\D*\..+", re.IGNORECASE)
        used = [int(m.group(1)) for m in
                (matcher.fullmatch(f) for f in os.listdir(full_folder)) if m]
        counter = max(used or [0]) + 1

        height, width = images.shape[1], images.shape[2]
        # Encoders need even dimensions; pad rather than refuse to save.
        pad_w, pad_h = width % 2, height % 2
        if pad_w or pad_h:
            images = torch.nn.functional.pad(
                images.permute(0, 3, 1, 2), (0, pad_w, 0, pad_h), mode="replicate"
            ).permute(0, 2, 3, 1)
            width, height = width + pad_w, height + pad_h

        args = [media.ffmpeg_path, "-v", "error", "-y", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
                "-c:v", spec["codec"], "-pix_fmt", spec["pix_fmt"]]
        if spec["quality"] == "cq":
            args += ["-rc", "vbr", "-cq", str(quality), "-b:v", "0", "-preset", "p5"]
        elif spec["quality"] == "crf":
            args += ["-crf", str(quality), "-preset", "medium"]
        args += spec.get("extra", [])

        video_path = os.path.join(full_folder, f"{filename}_{counter:05}.{spec['ext']}")
        frames = (accel.tensor_to_bytes(image).tobytes() for image in images)
        pbar = ProgressBar(len(images))

        def counted():
            for frame in frames:
                pbar.update(1)
                yield frame

        media.encode(video_path, args, counted())
        final_path = video_path

        waveform = audio.get("waveform") if isinstance(audio, dict) else None
        if waveform is not None and waveform.numel() > 0:
            track = waveform[0] if waveform.dim() == 3 else waveform
            muxed = os.path.join(full_folder, f"{filename}_{counter:05}-audio.{spec['ext']}")
            media.mux_audio(video_path, muxed, track,
                            int(audio.get("sample_rate", 44100)), ["-c:a", "aac", "-b:a", "192k"])
            final_path = muxed

        # A sidecar png so the workflow can be dragged back in, like Save Image.
        if prompt is not None or extra_pnginfo is not None:
            metadata = PngInfo()
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            for key, value in (extra_pnginfo or {}).items():
                metadata.add_text(key, json.dumps(value))
            metadata.add_text("CreationTime", datetime.datetime.now().isoformat(" ")[:19])
            Image.fromarray(accel.tensor_to_bytes(images[0])).save(
                os.path.join(full_folder, f"{filename}_{counter:05}.png"),
                pnginfo=metadata, compress_level=1)

        preview = {"filename": os.path.basename(final_path), "subfolder": subfolder,
                   "type": "output" if save_output else "temp", "format": f"video/{spec['ext']}",
                   "frame_rate": fps, "fullpath": final_path}
        return {"ui": {"gifs": [preview]}, "result": (final_path,)}


NODE_CLASS_MAPPINGS = {
    "V4K_LoadVideo": LoadVideo4K,
    "V4K_SaveVideo": SaveVideo4K,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "V4K_LoadVideo": "Load Video 4K",
    "V4K_SaveVideo": "Save Video 4K",
}
