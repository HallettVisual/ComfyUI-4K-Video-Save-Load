# ComfyUI Video 4K

Two video nodes that do their per-pixel work on the GPU, so 4K load and save stop
being the slow part of a workflow. Separate from VideoHelperSuite, not a fork of it.

## Speed

Measured on an RTX 5090 at 3840x2160 with an idle GPU, milliseconds per frame.
Load is 100 frames, best of two runs; save is 60 frames.

| | VideoHelperSuite | Video 4K | |
| --- | --- | --- | --- |
| Load, full resolution | 377.9 | **20.1** | 18.8x |
| Load, scaled to 1080p | 45.6 | **9.0** | 5.1x |
| Save, h264 nvenc | 87.8 | **30.9** | 2.8x |
| Save, h264 cpu | 104.2 | **39.4** | 2.6x |

VHS's OpenCV loader reads full-resolution 4K at 43.5 ms/frame, so the ffmpeg path
here is faster than either of the loaders it replaces. Measure with an idle GPU:
a render in the background inflates these several times over.

## Load Video 4K

One node in place of VHS's four loaders.

- **source** — `upload` uses the picker, `path` takes a file **or a folder**. For a
  folder, `video_index` chooses which clip and `video_count` tells you how many
  there are, so a queue can walk a whole directory.
- **resolution** — `source`, `2160p`, `1440p`, `1080p`, `720p`, `480p`,
  `2048 wide`, `1920 wide`, `1024 wide`, or `custom`. Aspect ratio is kept; set one
  of `custom_width` / `custom_height` and the other follows. Scaling happens inside
  ffmpeg, which is far faster than resizing after decode.
- **divisible_by** — rounds both dimensions. The model preset raises it if needed.
- **model_preset** — sets the frame rate, the size rounding, and snaps the frame
  count to what the model actually accepts:

  | preset | fps | frame counts | size multiple |
  | --- | --- | --- | --- |
  | MiniMax-H3 | 24 | 17n + 5 (5, 22, 39, 56 …) | 32 |
  | LTXV | 24 | 8n + 1 | 32 |
  | Wan | 16 | 4n + 1 | 16 |
  | Hunyuan | 24 | 4n + 1 | 16 |

- **seconds_cap** — load at most this many seconds. Combined with a model preset the
  frame maths is done for you: 3 s of H3 becomes 56 frames, not 72.
- **frame_load_cap**, **skip_first_seconds**, **select_every_nth** — as usual.
- **audio_when_missing** — `silence` emits a silent track matching the clip length so
  downstream nodes never break on a file with no audio. `none` outputs nothing.

Outputs: `images`, `audio`, `frame_count`, `fps`, `width`, `height`, `video_count`,
and `info` (a JSON summary of source and loaded properties). `fps` already accounts
for `force_rate`, the model preset and `select_every_nth`, so it can be wired
straight into Save Video 4K.

## Save Video 4K

`h264`, `hevc` and `av1` on NVENC, `h264`/`hevc` on CPU, or `prores`. `quality` is
the cq/crf value, lower being better. Audio is muxed when connected. Odd dimensions
are padded rather than refused. A sidecar `.png` carries the workflow, so the result
can be dragged back into ComfyUI.

## Colour

For 8-bit 4:2:0 sources the YUV to RGB conversion runs on the GPU from `nv12`, which
moves a third of the bytes `rgb24` would and is measurably closer to the source than
ffmpeg's own converter, which carries a systematic -1.24/255 darkening.

Untagged files get the matrix every player assumes (bt709 at 720p and above, bt601
below) rather than ffmpeg's blanket bt601, which tints HD footage. Sources with
alpha, more than 8 bits, or other chroma layouts use a matching ffmpeg conversion.

## Requirements

ffmpeg on PATH, or `imageio-ffmpeg` installed. NVENC needs an NVIDIA GPU; without
CUDA everything still runs on the CPU, just slower.
