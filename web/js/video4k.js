import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// Mirrors MODEL_PRESETS and RESOLUTIONS in nodes.py, so the node can show what a
// setting of 0 will actually resolve to before anything runs.
const PRESETS = {
    "none": {},
    "MiniMax-H3": { fps: 24, frames: [17, 5], multiple: 32 },
    "LTXV": { fps: 24, frames: [8, 1], multiple: 32 },
    "Wan": { fps: 16, frames: [4, 1], multiple: 16 },
    "Hunyuan": { fps: 24, frames: [4, 1], multiple: 16 },
};
const RESOLUTIONS = {
    "2160p (4K)": ["height", 2160], "1440p": ["height", 1440], "1080p": ["height", 1080],
    "720p": ["height", 720], "480p": ["height", 480], "2048 wide": ["width", 2048],
    "1920 wide": ["width", 1920], "1024 wide": ["width", 1024],
};

const MARGIN = 15;
const RESET_GLYPH = "↺";      // reset to the default
const DISABLE_GLYPH = "⊘";    // switch the limit off

function widget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}
function value(node, name) {
    return widget(node, name)?.value;
}
function number(node, name) {
    return Number(value(node, name)) || 0;
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
        const [edge, size] = RESOLUTIONS[resolution];
        if (edge === "height") { width = width * size / height; height = size; }
        else { height = height * size / width; width = size; }
    }
    const round = (v) => Math.max(multiple, Math.round(v / multiple) * multiple);
    return [round(width), round(height)];
}

function effectiveRate(node, info) {
    const preset = PRESETS[value(node, "model_preset")] ?? {};
    const rate = preset.fps || number(node, "force_frame_rate") || info.fps;
    return rate / Math.max(1, number(node, "select_every_nth") || 1);
}

function effectiveFrames(node, info) {
    const preset = PRESETS[value(node, "model_preset")] ?? {};
    const every = Math.max(1, number(node, "select_every_nth") || 1);
    const rate = preset.fps || number(node, "force_frame_rate") || info.fps;
    const available = info.duration - number(node, "skip_first_seconds");
    if (available <= 0) return 0;
    let cap = Math.floor(available * rate / every);
    const seconds = number(node, "seconds_cap");
    if (seconds > 0) cap = Math.min(cap, Math.floor(seconds * (rate / every)));
    const frames = number(node, "frame_load_cap");
    if (frames > 0) cap = Math.min(cap, frames);
    if (preset.frames) {
        const [divisor, remainder] = preset.frames;
        cap = cap < remainder ? 0 : Math.floor((cap - remainder) / divisor) * divisor + remainder;
    }
    return cap;
}

// What each widget shows in grey, and what its little button does.
const ANNOTATED = {
    force_frame_rate: { glyph: RESET_GLYPH, reset: 0, of: (n, i) => Math.round(effectiveRate(n, i) * 100) / 100 },
    custom_width: { glyph: RESET_GLYPH, reset: 0, of: (n, i) => effectiveSize(n, i)[0] },
    custom_height: { glyph: RESET_GLYPH, reset: 0, of: (n, i) => effectiveSize(n, i)[1] },
    frame_load_cap: { glyph: DISABLE_GLYPH, reset: 0, of: (n, i) => effectiveFrames(n, i) },
    seconds_cap: { glyph: DISABLE_GLYPH, reset: 0, of: (n, i) => Math.round(effectiveFrames(n, i) / effectiveRate(n, i) * 100) / 100 },
};

function describe(info) {
    if (!info) return "";
    if (info.error) return "file not readable";
    const lines = [
        info.file,
        `${info.width}x${info.height}  ${info.fps} fps  ${info.duration}s  ${info.frames} frames`,
        info.has_audio ? "audio" : "no audio",
    ];
    if (info.videos_in_folder > 1) lines[2] += `  ${info.videos_in_folder} videos in folder`;
    return lines.join("\n");
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
    node.setDirtyCanvas(true, true);
}

// The file's own details, as a widget so litegraph gives it a row of its own
// rather than letting it land on top of the video preview.
function addInfoWidget(node) {
    const lines = () => describe(node.v4kInfo).split("\n").filter(Boolean);
    const info = {
        name: "v4k_info",
        type: "v4k_info",
        value: "",
        options: { serialize: false },
        serializeValue: () => undefined,
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
    };
    if (node.addCustomWidget) node.addCustomWidget(info);
    else (node.widgets ??= []).push(info);
    return info;
}

// The grey "what this will actually be" value, and the little button beside it.
// Drawn straight after litegraph paints the widgets, because a node's foreground
// hook runs before them and anything it draws ends up underneath.
function drawAnnotations(ctx, node) {
    const info = node.v4kInfo;
    if (node.flags?.collapsed || !info || info.error) return;
    const height = window.LiteGraph?.NODE_WIDGET_HEIGHT ?? 20;
    const width = node.size[0];
    ctx.save();
    ctx.font = "12px Arial";
    for (const [name, spec] of Object.entries(ANNOTATED)) {
        const w = widget(node, name);
        if (!w || w.last_y == null || w.hidden) continue;
        let shown;
        try { shown = spec.of(node, info); } catch (e) { continue; }
        if (!Number.isFinite(shown)) continue;
        const y = w.last_y;
        ctx.textAlign = "center";
        ctx.fillStyle = "#aaa";
        ctx.fillText(spec.glyph, width - MARGIN - 26, y + height * 0.7);
        if (String(shown) !== String(w.value)) {
            const valueWidth = ctx.measureText(String(w.value)).width;
            ctx.textAlign = "right";
            ctx.fillStyle = "#8a8a8a";
            ctx.fillText(`${shown}←`, width - 50 - valueWidth - 8, y + height * 0.7);
        }
    }
    ctx.restore();
}

app.registerExtension({
    name: "Video4K.Core",
    setup() {
        const canvasType = app.canvas?.constructor ?? window.LGraphCanvas;
        if (!canvasType?.prototype?.drawNodeWidgets) return;
        const original = canvasType.prototype.drawNodeWidgets;
        canvasType.prototype.drawNodeWidgets = function (node, posY, ctx, activeWidget) {
            const result = original.apply(this, arguments);
            if (node?.type === "V4K_LoadVideo") drawAnnotations(ctx, node);
            return result;
        };
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "V4K_LoadVideo") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onCreated?.apply(this, arguments);
            addInfoWidget(this);
            for (const name of ["source", "video", "path", "video_index"]) {
                const w = widget(this, name);
                if (!w) continue;
                const previous = w.callback;
                w.callback = (...args) => {
                    const out = previous?.apply(this, args);
                    refresh(this);
                    return out;
                };
            }
            requestAnimationFrame(() => refresh(this));
            return result;
        };

        const onMouseDown = nodeType.prototype.onMouseDown;
        nodeType.prototype.onMouseDown = function (event, pos, canvas) {
            const height = window.LiteGraph?.NODE_WIDGET_HEIGHT ?? 20;
            const width = this.size[0];
            if (pos[0] > width - MARGIN - 34 && pos[0] < width - MARGIN - 18) {
                for (const [name, spec] of Object.entries(ANNOTATED)) {
                    const w = widget(this, name);
                    if (!w || w.last_y == null || w.hidden) continue;
                    if (pos[1] < w.last_y || pos[1] > w.last_y + height) continue;
                    if (w.value !== spec.reset) {
                        w.value = spec.reset;
                        w.callback?.(w.value);
                    }
                    this.setDirtyCanvas(true, true);
                    return true;
                }
            }
            return onMouseDown?.apply(this, arguments);
        };
    },
});
