# ComfyUI 4K Faster Video Loader and Saver with Fun Features

Similar to the VideoHelperSuite video nodes, but with the file's information exposed on the node and much faster load and save for large files. Two nodes that do their per-pixel work on the GPU, so 4K stops being the slow part of a workflow.  

Independent of VideoHelperSuite: it is not a fork and shares no code with it, so both can be installed at once.

<img width="344" height="1085" alt="4K_Load_Screencap" src="https://github.com/user-attachments/assets/989aaa4a-3030-4026-8521-a1860ee8ab19" />

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
  folder, `video_index` chooses which clip and the file details show how many there
  are, so a queue can walk a whole directory.
- **Drag and drop** — drop a clip anywhere on the node, preview included. Drops and
  the picker take `.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v` and `.gif`.
- **resolution** — `source`, `2160p`, `1440p`, `1080p`, `720p`, `480p`,
  `2048 wide`, `1920 wide`, `1024 wide`, or `custom`. Each preset sets the
  **longest** side (2160p is 3840, 1080p is 1920), so portrait clips get the same
  class of size as landscape ones. Aspect ratio is kept. Picking one fills in
  `custom_width` / `custom_height` with the size the clip will load at; typing in
  either switches to `custom`, and leaving one at 0 makes it follow the aspect.
  Scaling happens inside ffmpeg, which is far faster than resizing after decode.
- **divisible_by** — rounds both dimensions. The model preset raises it if needed.
- **model_preset** — sets the frame rate, the size rounding, and snaps the frame
  count to what the model actually accepts:

  | preset | fps | frame counts | size multiple |
  | --- | --- | --- | --- |
  | MiniMax-H3 | 24 | 17n + 5 (5, 22, 39, 56 …) | 32 |
  | LTXV | 24 | 8n + 1 | 32 |
  | Wan | 16 | 4n + 1 | 16 |
  | Hunyuan | 24 | 4n + 1 | 16 |

  As in VHS, the preset also makes `frame_load_cap` step through those counts: with
  H3 its arrows go 5, 22, 39, 56 and a typed value snaps to the nearest one.
- **seconds_cap**, **frame_load_cap** — show the length of the whole clip until you
  change one, and the other follows it. Combined with a model preset the frame maths
  is done for you: 3 s of H3 becomes 56 frames, not 72. ↺ goes back to the whole clip.
- **skip_first_seconds**, **select_every_nth** — as usual.
- **force_frame_rate** — whole numbers only. 0 keeps the source rate.
- **audio_when_missing** — `silence` emits a silent track matching the clip length so
  downstream nodes never break on a file with no audio. `none` outputs nothing.

When a widget's own value is not what will load, the real value shows beside it in
grey, recomputed live from every other setting: ask for 158 frames of a clip that
has 141 and `frame_load_cap` reads `141← 158`. The ↺ beside the size, rate and cap
widgets puts the clip's own value back, and is dimmed when there is nothing to reset.
**reset all to defaults** does every setting at once and keeps the chosen clip.

The file's own details also appear in grey at the bottom of the node as soon as you
pick it, before running anything, with the part that will load: `loads 1.5s to 4.2s
(2.7s, 65 frames)`. Picking, uploading or dropping a new clip resets the settings to
their defaults; opening a saved workflow does not.

The preview plays only that part, looping it, and jumps to the new start or end as
you change `skip_first_seconds` or a cap. Switch **preview** to `whole clip` to scrub
the entire file, then **set start here** and **set end here** mark the part to load
from where the preview is.

Outputs: `images`, `audio`, `frame_count`, `fps`, `width`, `height`, `info` (a
JSON summary of source and loaded properties) and `video`, the same frames and audio
as a VIDEO for nodes that take one. `fps` already accounts for
`force_frame_rate`, the model preset and `select_every_nth`, so it can be wired
straight into Save Video 4K.

## Save Video 4K

`images` takes an IMAGE batch, a LATENT, or a VIDEO, such as Load Video 4K's `video`
output or ComfyUI's own Load Video. A VIDEO brings its own frame rate and audio; a
connected `audio` input still wins.

`h264`, `hevc` and `av1` on NVENC, `h264`/`hevc` on CPU, `prores`, or
`hevc 10-bit (nvenc)`. `quality` is the cq/crf value, lower being better. ProRes and
10-bit HEVC are fed 16 bits per channel, so gradients keep their full 10 bits, and
ProRes carries uncompressed 24-bit audio. Audio is muxed when connected, trimmed to
the picture so no frames are lost. 23.976, 29.97 and 59.94 fps are written as the
exact NTSC rates. Odd dimensions are padded rather than refused. The finished video
plays on the node itself.

**save_metadata** (on by default) stores the workflow in the video file, so the
result can be dragged back into ComfyUI to rebuild the graph.

**vae** accepts a LATENT on `images` and decodes it in batches, so a long 4K clip
does not have to fit in VRAM in one go.

## Colour

For 8-bit 4:2:0 sources the YUV to RGB conversion runs on the GPU from `nv12`, which
moves a third of the bytes `rgb24` would and is measurably closer to the source than
ffmpeg's own converter, which carries a systematic -1.24/255 darkening.

Untagged files get the matrix every player assumes (bt709 at 720p and above, bt601
below) rather than ffmpeg's blanket bt601, which tints HD footage. Sources with
alpha, more than 8 bits, or other chroma layouts use a matching ffmpeg conversion.

Saved files are converted with the bt709 matrix and tagged bt709 / sRGB, as ComfyUI's
own Save Video does, so players show the colours that were rendered.

## Requirements

ffmpeg on PATH, or `imageio-ffmpeg` installed. NVENC needs an NVIDIA GPU; without
CUDA everything still runs on the CPU, just slower.
