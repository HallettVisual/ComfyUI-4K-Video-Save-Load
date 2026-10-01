import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// Mirrors MODEL_PRESETS, RESOLUTIONS and VIDEO_EXTENSIONS in nodes.py, so the node
// can show what the file will actually load as before anything runs.
const PRESETS = {
    "none": {},
    "MiniMax-H3": { fps: 24, frames: [17, 5], multiple: 32 },
    "LTXV": { fps: 24, frames: [8, 1], multiple: 32 },
    "Wan": { fps: 16, frames: [4, 1], multiple: 16 },
    "Hunyuan": { fps: 24, frames: [4, 1], multiple: 16 },
};
const RESOLUTIONS = {
    "2160p (4K)": 3840, "1440p": 2560, "1080p": 1920, "720p": 1280, "480p": 854,
    "2048 wide": 2048, "1920 wide": 1920, "1024 wide": 1024,
};
const VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif"];

const MARGIN = 15;
const RESET_GLYPH = "↺";
// A number widget steps its value on any click within 40px of either edge, so the
// button sits clear of that, in room taken from beside the value.
const BUTTON_X = 54;
const BUTTON_ROOM = 20;
// What the reset-all button restores. The choice of clip is left alone.
const SETTINGS = ["resolution", "custom_width", "custom_height", "divisible_by", "model_preset",
    "force_frame_rate", "seconds_cap", "frame_load_cap", "skip_first_seconds", "select_every_nth",
    "audio_when_missing"];
// The caps show what will load rather than 0. While one follows the clip it sends 0,
// so a longer clip picked later is not cut short.
const CAPS = ["frame_load_cap", "seconds_cap"];

function widget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}
function value(node, name) {
    return widget(node, name)?.value;
}
function number(node, name) {
    return Number(value(node, name)) || 0;
}
function tenths(seconds) {
    return Math.floor(seconds * 10) / 10;
}
function following(node, name) {
    return node.properties?.[`v4k_follow_${name}`] ?? number(node, name) === 0;
}
function cap(node, name) {
    return following(node, name) ? 0 : number(node, name);
}

function effectiveSize(node, info) {
    const preset = PRESETS[value(node, "model_preset")] ?? {};
    const multiple = Math.max(Number(value(node, "divisible_by")) || 1, preset.multiple ?? 1);
    let width = info.width;
    let height = info.height;
    const resolution = value(node, "resolution");
    if (resolution === "custom") {
        const w = number(node, "custom_width");
        const h = number(node, "custom_height");
        if (w && h) { width = w; height = h; }
        else if (w) { height = height * w / width; width = w; }
        else if (h) { width = width * h / height; height = h; }
    } else if (RESOLUTIONS[resolution]) {
        const scale = RESOLUTIONS[resolution] / Math.max(width, height);
        width *= scale;
        height *= scale;
    }
    const round = (v) => Math.max(multiple, Math.round(v / multiple) * multiple);
    return [round(width), round(height)];
}

// The rate frames are read at, before select_every_nth thins them.
function readRate(node, info) {
    return PRESETS[value(node, "model_preset")]?.fps || number(node, "force_frame_rate") || info.fps;
}
function every(node) {
    return Math.max(1, number(node, "select_every_nth") || 1);
}

function effectiveRate(node, info) {
    return readRate(node, info) / every(node);
}

function effectiveFrames(node, info) {
    const preset = PRESETS[value(node, "model_preset")] ?? {};
    const rate = readRate(node, info);
    const available = info.duration - number(node, "skip_first_seconds");
    if (available <= 0) return 0;
    let frames = Math.floor(available * rate);
    const seconds = cap(node, "seconds_cap");
    if (seconds > 0) frames = Math.min(frames, Math.floor(seconds * rate));
    let count = Math.ceil(frames / every(node));
    const limit = cap(node, "frame_load_cap");
    if (limit > 0) count = Math.min(count, limit);
    if (preset.frames) {
        const [divisor, remainder] = preset.frames;
        count = count < remainder ? 0 : Math.floor((count - remainder) / divisor) * divisor + remainder;
    }
    return count;
}

// What each widget shows in grey, and what its button goes back to: the clip's own
// size, the clip's own rate, or the whole clip for the caps.
const ANNOTATED = {
    force_frame_rate: { kind: "rate", of: (n, i) => Math.round(effectiveRate(n, i) * 100) / 100 },
    custom_width: { kind: "size", of: (n, i) => effectiveSize(n, i)[0] },
    custom_height: { kind: "size", of: (n, i) => effectiveSize(n, i)[1] },
    frame_load_cap: { kind: "cap", of: (n, i) => effectiveFrames(n, i) },
    seconds_cap: { kind: "cap", of: (n, i) => tenths(effectiveFrames(n, i) / effectiveRate(n, i)) },
};

function canReset(node, w, spec) {
    if (spec.kind === "size") return value(node, "resolution") !== "source";
    if (spec.kind === "cap") return !following(node, w.name);
    return w.value !== 0;
}

function reset(node, w, spec) {
    if (spec.kind === "cap") {
        node.properties[`v4k_follow_${w.name}`] = true;
        syncCaps(node);
    } else {
        const target = spec.kind === "size" ? widget(node, "resolution") : w;
        target.value = spec.kind === "size" ? "source" : 0;
        target.callback?.(target.value, app.canvas, node);
    }
    node.setDirtyCanvas(true, true);
}

function resetAll(node, defaults) {
    for (const [name, v] of Object.entries(defaults)) widget(node, name).value = v;
    for (const name of CAPS) node.properties[`v4k_follow_${name}`] = true;
    applyFrameGrid(node);
    syncSize(node);
    syncCaps(node);
    node.setDirtyCanvas(true, true);
}

// Outside custom, the size fields show the size the file will load at.
function syncSize(node) {
    const info = node.v4kInfo;
    if (!info?.width || value(node, "resolution") === "custom") return;
    const [width, height] = effectiveSize(node, info);
    widget(node, "custom_width").value = width;
    widget(node, "custom_height").value = height;
}

function syncCaps(node) {
    const info = node.v4kInfo;
    if (!info?.width) return;
    for (const name of CAPS) node.properties[`v4k_follow_${name}`] = following(node, name);
    const frames = effectiveFrames(node, info);
    if (following(node, "frame_load_cap")) widget(node, "frame_load_cap").value = frames;
    if (following(node, "seconds_cap")) widget(node, "seconds_cap").value = tenths(frames / effectiveRate(node, info));
}

// As VHS does it, a model preset makes the frame cap step through the counts the
// model accepts: 5, 22, 39... for H3's 17k+5. The widget snaps to min + k * step2.
function applyFrameGrid(node) {
    const w = widget(node, "frame_load_cap");
    const [divisor, remainder] = PRESETS[value(node, "model_preset")]?.frames ?? [1, 0];
    Object.assign(w.options, { min: remainder, step: divisor * 10, step2: divisor });
    if (!following(node, w.name)) {
        w.value = Math.max(remainder, Math.floor((w.value - remainder) / divisor) * divisor + remainder);
    }
}

// Typing a size takes the node off its preset; the other side follows the aspect.
function toCustom(node, other) {
    const resolution = widget(node, "resolution");
    if (resolution.value === "custom") return;
    resolution.value = "custom";
    widget(node, other).value = 0;
}

// The stretch of the clip that will load, in the clip's own seconds.
function loadedRange(node) {
    const info = node.v4kInfo;
    if (!info?.width) return null;
    const start = number(node, "skip_first_seconds");
    const span = effectiveFrames(node, info) * every(node) / readRate(node, info);
    return [start, Math.min(info.duration, start + span)];
}

function describe(node) {
    const info = node.v4kInfo;
    if (!info) return "";
    if (info.note) return info.note;
    if (info.error) return "file not readable";
    const lines = [
        info.file,
        `${info.width}x${info.height}  ${info.fps} fps  ${tenths(info.duration)}s  ${info.frames} frames`,
        info.has_audio ? "audio" : "no audio",
    ];
    if (info.videos_in_folder > 1) lines[2] += `  ${info.videos_in_folder} videos in folder`;
    const [start, end] = loadedRange(node);
    lines.push(`loads ${tenths(start).toFixed(1)}s to ${tenths(end).toFixed(1)}s  `
        + `(${tenths(end - start).toFixed(1)}s, ${effectiveFrames(node, info)} frames)`);
    return lines.join("\n");
}

function previewVideo(node) {
    return value(node, "source") === "upload" ? node.videoContainer?.querySelector("video") : null;
}

function showTime(node, seconds) {
    const video = previewVideo(node);
    if (video && Number.isFinite(seconds)) video.currentTime = seconds;
}

// Unless the preview is set to the whole clip, keep it inside the part that will
// load, looping there while it plays. Checked on every frame the video presents.
function confine(node, video) {
    const check = () => {
        const range = node.properties.v4k_preview_whole ? null : loadedRange(node);
        if (range && previewVideo(node) === video
                && (video.currentTime < range[0] - 0.02 || video.currentTime >= range[1])) {
            video.currentTime = range[0];
        }
        video.requestVideoFrameCallback(check);
    };
    video.requestVideoFrameCallback(check);
}

// Sets skip_first_seconds or seconds_cap from where the preview is, to 0.1s.
function markAt(node, edge) {
    const video = previewVideo(node);
    if (!video) return;
    const from = edge === "start" ? 0 : number(node, "skip_first_seconds");
    const seconds = Math.round((video.currentTime - from) * 10) / 10;
    if (edge === "end" && seconds <= 0) return;
    const w = widget(node, edge === "start" ? "skip_first_seconds" : "seconds_cap");
    w.value = seconds;
    w.callback?.(seconds, app.canvas, node);
}

async function refresh(node) {
    const params = new URLSearchParams({
        source: value(node, "source") ?? "upload",
        video: value(node, "video") ?? "",
        path: value(node, "path") ?? "",
        video_index: value(node, "video_index") ?? 0,
    });
    const token = (node.v4kToken = (node.v4kToken ?? 0) + 1);
    try {
        const response = await api.fetchApi(`/video4k/probe?${params}`);
        const info = await response.json();
        if (token !== node.v4kToken) return;   // a newer request already answered
        node.v4kInfo = response.ok ? info : { error: info.error || "unavailable" };
    } catch (e) {
        if (token === node.v4kToken) node.v4kInfo = { error: "unavailable" };
    }
    syncSize(node);
    syncCaps(node);
    showTime(node, loadedRange(node)?.[0]);
    node.setDirtyCanvas(true, true);
}

function isVideoFile(file) {
    const name = file.name.toLowerCase();
    return VIDEO_EXTENSIONS.some((ext) => name.endsWith(ext));
}

// Our own upload, so the picker offers every format the node reads, .mov included,
// and a drop is judged by its extension rather than the MIME type Windows reports.
async function upload(node, file) {
    node.v4kInfo = { note: `uploading ${file.name}` };
    node.setDirtyCanvas(true, true);
    const body = new FormData();
    body.append("image", file);
    const response = await api.fetchApi("/upload/image", { method: "POST", body });
    if (response.status !== 200) {
        node.v4kInfo = { note: `upload failed: ${response.statusText}` };
        node.setDirtyCanvas(true, true);
        return false;
    }
    const { name, subfolder } = await response.json();
    const path = subfolder ? `${subfolder}/${name}` : name;
    const combo = widget(node, "video");
    if (!combo.options.values.includes(path)) combo.options.values.push(path);
    combo.value = path;
    combo.callback?.(path, app.canvas, node);
    return true;
}

function chain(node, name, after) {
    const w = widget(node, name);
    if (!w) return;
    const previous = w.callback;
    w.callback = function () {
        const out = previous?.apply(this, arguments);
        after();
        return out;
    };
}

// The file's own details, as a widget so litegraph gives it a row of its own
// rather than letting it land on top of the video preview.
function addInfoWidget(node) {
    const lines = () => describe(node).split("\n").filter(Boolean);
    node.addCustomWidget({
        name: "v4k_info",
        type: "v4k_info",
        value: "",
        serialize: false,
        options: { serialize: false },
        draw(ctx, _node, width, y) {
            ctx.save();
            ctx.fillStyle = "#999";
            ctx.font = "11px monospace";
            ctx.textAlign = "left";
            let text_y = y + 11;
            for (const line of lines()) {
                ctx.fillText(line, 14, text_y);
                text_y += 13;
            }
            ctx.restore();
        },
        computeSize(width) {
            const count = lines().length;
            return [width, count ? count * 13 + 4 : 0];
        },
    });
}

// Two buttons in one row that mark the loaded part at the preview's position.
function addMarkWidget(node) {
    const labels = ["set start here", "set end here"];
    node.addCustomWidget({
        name: "v4k_marks",
        type: "v4k_marks",
        value: "",
        serialize: false,
        options: { serialize: false },
        draw(ctx, _node, width, y, height) {
            const half = (width - 2 * MARGIN - 6) / 2;
            ctx.save();
            ctx.textAlign = "center";
            labels.forEach((label, i) => {
                const x = MARGIN + i * (half + 6);
                ctx.fillStyle = LiteGraph.WIDGET_BGCOLOR;
                ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR;
                ctx.beginPath();
                ctx.roundRect(x, y, half, height, height / 2);
                ctx.fill();
                ctx.stroke();
                ctx.fillStyle = LiteGraph.WIDGET_TEXT_COLOR;
                ctx.fillText(label, x + half / 2, y + height * 0.7);
            });
            ctx.restore();
        },
        mouse(event, [x]) {
            if (event.type !== "pointerdown") return false;
            markAt(node, x < node.size[0] / 2 ? "start" : "end");
            return true;
        },
    });
}

// Clears room between a number widget's value and its right arrow, and draws the
// grey effective value and the button there. The widget would otherwise take a
// click on the button as an edit, so the button claims its clicks first.
function decorate(node, w, spec) {
    const drawText = w.drawTruncatingText;
    if (typeof drawText !== "function") return;
    w.drawTruncatingText = function (options) {
        const rightPadding = (options.rightPadding ?? 20) + BUTTON_ROOM;
        drawText.call(this, { ...options, rightPadding });
        if (!this.computedDisabled) drawExtras(options.ctx, node, this, spec, options.width, rightPadding);
    };
    w.onPointerDown = function (pointer, _node, canvas) {
        const x = canvas.graph_mouse[0] - node.pos[0];
        if (Math.abs(x - ((this.width || node.size[0]) - BUTTON_X)) > 10) return false;
        pointer.onClick = () => reset(node, this, spec);
        return true;
    };
}

function drawExtras(ctx, node, w, spec, width, rightPadding) {
    const y = w.y + w.height * 0.7;
    ctx.save();
    ctx.textAlign = "center";
    ctx.fillStyle = canReset(node, w, spec) ? "#aaa" : "#555";
    ctx.fillText(RESET_GLYPH, width - BUTTON_X, y);

    const info = node.v4kInfo;
    const shown = info?.width ? spec.of(node, info) : NaN;
    if (Number.isFinite(shown) && String(shown) !== String(w.value)) {
        const text = `${shown}←`;
        const right = width - 2 * MARGIN - rightPadding
            - ctx.measureText(w._displayValue ?? String(w.value)).width - 6;
        const labelEnd = 2 * MARGIN + 10 + ctx.measureText(w.label || w.name).width;
        if (right - ctx.measureText(text).width > labelEnd) {
            ctx.textAlign = "right";
            ctx.fillStyle = "#8a8a8a";
            ctx.fillText(text, right, y);
        }
    }
    ctx.restore();
}

app.registerExtension({
    name: "Video4K.Core",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "V4K_LoadVideo") return;
        const required = nodeData.input.required;
        const defaults = Object.fromEntries(SETTINGS.map((name) => [name, required[name][1].default]));

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onCreated?.apply(this, arguments);
            addInfoWidget(this);
            for (const [name, spec] of Object.entries(ANNOTATED)) {
                const w = widget(this, name);
                if (w) decorate(this, w, spec);
            }

            // Picking, uploading or dropping a clip loads it with default settings,
            // even if the node was on a path.
            chain(this, "video", () => {
                widget(this, "source").value = "upload";
                resetAll(this, defaults);
            });
            for (const name of ["source", "video", "path", "video_index"]) {
                chain(this, name, () => refresh(this));
            }
            chain(this, "model_preset", () => applyFrameGrid(this));
            for (const name of ["resolution", "divisible_by", "model_preset"]) {
                chain(this, name, () => syncSize(this));
            }
            chain(this, "custom_width", () => toCustom(this, "custom_height"));
            chain(this, "custom_height", () => toCustom(this, "custom_width"));
            for (const name of CAPS) {
                const w = widget(this, name);
                // Typing a cap sets it; typing 0 hands it back to the clip.
                chain(this, name, () => { this.properties[`v4k_follow_${name}`] = w.value === 0; });
                w.serializeValue = () => (following(this, name) ? 0 : w.value);
            }
            for (const name of ["model_preset", "force_frame_rate", "skip_first_seconds", "select_every_nth", ...CAPS]) {
                chain(this, name, () => syncCaps(this));
            }
            // Show the new in or out point in the preview.
            chain(this, "skip_first_seconds", () => showTime(this, loadedRange(this)?.[0]));
            for (const name of CAPS) chain(this, name, () => showTime(this, loadedRange(this)?.[1] - 0.01));

            // Unsaved, so it can sit under the settings without shifting saved values.
            const resetAllButton = this.addWidget("button", "v4k_reset_all", null,
                () => resetAll(this, defaults), { serialize: false });
            resetAllButton.label = "reset all to defaults";
            resetAllButton.serialize = false;
            this.widgets.splice(this.widgets.indexOf(widget(this, "audio_when_missing")) + 1, 0, this.widgets.pop());

            const previewMode = this.addWidget("toggle", "v4k_preview", true, (v) => {
                this.properties.v4k_preview_whole = !v;
                showTime(this, loadedRange(this)?.[0]);
            }, { on: "loaded part", off: "whole clip", serialize: false });
            previewMode.label = "preview";
            previewMode.serialize = false;
            addMarkWidget(this);

            const picker = Object.assign(document.createElement("input"), {
                type: "file",
                accept: [...VIDEO_EXTENSIONS, "video/*"].join(","),
            });
            picker.onchange = () => {
                if (picker.files.length) upload(this, picker.files[0]);
                picker.value = "";
            };
            const button = widget(this, "upload");
            if (button) button.callback = () => picker.click();

            const drop = this.onDragDrop;
            this.onDragDrop = async function (e) {
                const file = [...(e.dataTransfer?.files ?? [])].find(isVideoFile);
                if (file) return upload(this, file);
                return (await drop?.apply(this, arguments)) ?? false;
            };

            // The preview is a page element over the canvas, so the canvas never
            // sees a drop on it. The browser only fires drop where dragover was
            // accepted, so accept it here and take the drop straight to the node.
            const addDOMWidget = this.addDOMWidget;
            this.addDOMWidget = function (name, type, element) {
                element.addEventListener("dragover", (e) => {
                    if (!this.onDragOver?.(e)) return;
                    e.preventDefault();
                    e.dataTransfer.dropEffect = "copy";
                });
                element.addEventListener("drop", (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    this.onDragDrop(e);
                });
                // Each clip gets a new video element, added once it has loaded.
                new MutationObserver(() => {
                    const video = element.querySelector("video");
                    if (!video || video.v4kConfined) return;
                    video.v4kConfined = true;
                    confine(this, video);
                    showTime(this, loadedRange(this)?.[0]);
                }).observe(element, { childList: true, subtree: true });
                return addDOMWidget.apply(this, arguments);
            };

            requestAnimationFrame(() => {
                previewMode.value = !this.properties.v4k_preview_whole;
                applyFrameGrid(this);
                refresh(this);
            });
            return result;
        };
    },
});
