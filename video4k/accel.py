"""GPU paths for the raw frame conversions that dominate 4K load and save.

At 4K a single frame is 25M samples, so the per-pixel float work in the load and
save paths costs more than the codec does. These helpers move that work to the
torch device ComfyUI is already using and overlap it with the ffmpeg subprocess.
"""
import queue
import threading

import torch

import comfy.model_management

# (Kr, Kb) for the matrices ffmpeg tags on real footage. Everything else derives.
MATRIX_COEFFS = {
    "bt709": (0.2126, 0.0722),
    "bt601": (0.299, 0.114),
    "bt2020nc": (0.2627, 0.0593),
}
# ffmpeg's stream-line spelling for each of those.
MATRIX_ALIASES = {
    "bt709": "bt709",
    "bt470bg": "bt601",
    "smpte170m": "bt601",
    "smpte240m": "bt601",
    "bt2020nc": "bt2020nc",
    "bt2020c": "bt2020nc",
}
# 8 bit 4:2:0 sources, where nv12 is a chroma relayout rather than a resample.
NV12_COMPATIBLE = {"yuv420p", "yuvj420p", "nv12", "nv21"}

device = comfy.model_management.get_torch_device()


def nv12_to_rgb(frame, width, height, matrix, full_range):
    """One nv12 frame to an HWC float32 RGB tensor on the compute device."""
    plane = torch.frombuffer(frame, dtype=torch.uint8).to(device, non_blocking=True)
    y = plane[:width * height].view(1, 1, height, width).float()
    uv = plane[width * height:].view(1, height // 2, width // 2, 2).permute(0, 3, 1, 2).float()
    uv = uv.repeat_interleave(2, -2).repeat_interleave(2, -1)
    if full_range:
        y = y / 255
        u = (uv[:, 0:1] - 128) / 255
        v = (uv[:, 1:2] - 128) / 255
    else:
        y = (y - 16) / 219
        u = (uv[:, 0:1] - 128) / 224
        v = (uv[:, 1:2] - 128) / 224
    kr, kb = MATRIX_COEFFS[matrix]
    kg = 1 - kr - kb
    r = y + (2 - 2 * kr) * v
    b = y + (2 - 2 * kb) * u
    g = y - (2 * kr * (1 - kr) / kg) * v - (2 * kb * (1 - kb) / kg) * u
    return torch.cat((r, g, b), 1).clamp_(0, 1).squeeze(0).permute(1, 2, 0)


def packed_to_rgb(frame, width, height, channels, dtype):
    """One packed rgb/rgba frame to an HWC float32 tensor on the compute device."""
    full_scale = 255 if dtype == torch.uint8 else 65535
    plane = torch.frombuffer(frame, dtype=dtype).to(device, non_blocking=True)
    return plane.view(height, width, channels).float().div_(full_scale)


def bgr_to_rgb(frame):
    """One HWC uint8 BGR frame from OpenCV to an HWC float32 RGB tensor.

    The channel flip runs while the frame is still uint8, so it moves a quarter
    of the bytes it would after the widening.
    """
    return torch.from_numpy(frame).to(device, non_blocking=True).flip(-1).float().div_(255)


def tensor_to_int(tensor, bits):
    """Scale a 0-1 float image to `bits` integer range, rounding as numpy did."""
    scale = 2 ** bits - 1
    # The first op must be out of place: .to() is a no-op for a tensor already here.
    scaled = tensor.to(device, non_blocking=True) * scale
    return scaled.add_(0.5).clamp_(0, scale)


def tensor_to_shorts(tensor):
    return tensor_to_int(tensor, 16).to(torch.uint16).cpu().numpy()


def tensor_to_bytes(tensor):
    return tensor_to_int(tensor, 8).to(torch.uint8).cpu().numpy()


def prefetch(iterable, depth=4):
    """Yield from `iterable` while a worker thread runs ahead of the consumer.

    Used to overlap our per-frame conversion with the ffmpeg subprocess, which is
    otherwise fully serialized against it by the pipe.
    """
    results = queue.Queue(maxsize=depth)
    done = object()

    def produce():
        try:
            for item in iterable:
                results.put(item)
        except BaseException as e:
            results.put(e)
        else:
            results.put(done)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    while True:
        item = results.get()
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        yield item


def read_frames(stream, frame_bytes, depth=3):
    """Yield fixed size frames from a pipe, reading ahead on a worker thread.

    Buffers are recycled, and the one being yielded is only reused once the
    consumer asks for the next frame.
    """
    filled = queue.Queue()
    free = queue.Queue()
    for _ in range(depth):
        free.put(bytearray(frame_bytes))

    def read():
        try:
            while True:
                frame = free.get()
                if frame is None:
                    return
                view = memoryview(frame)
                offset = 0
                while offset < frame_bytes:
                    read_bytes = stream.readinto(view[offset:])
                    if not read_bytes:
                        break
                    offset += read_bytes
                if offset < frame_bytes:
                    break
                filled.put(frame)
        except (ValueError, OSError):
            # A consumer that stops early closes the pipe out from under this
            # read. That is how it is told to stop, so it is not an error.
            pass
        filled.put(None)

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    try:
        while True:
            frame = filled.get()
            if frame is None:
                return
            yield frame
            free.put(frame)
    finally:
        # Release the reader if the consumer stopped early, so it can't sit on
        # the pipe and wedge ffmpeg.
        free.put(None)
