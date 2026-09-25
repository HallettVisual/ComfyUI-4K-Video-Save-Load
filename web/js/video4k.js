import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// Widgets whose value the reset arrow pulls from the file itself.
const SOURCE_FIELDS = ["custom_width", "custom_height", "force_frame_rate"];

function widget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function describe(info) {
    if (!info) return "";
    if (info.error) return "file not readable";
    const lines = [
        `${info.file}`,
        `${info.width}x${info.height}  ${info.fps} fps  ${info.duration}s`,
        `${info.frames} frames  ${info.has_audio ? "audio" : "no audio"}`,
    ];
    if (info.videos_in_folder > 1) lines.push(`${info.videos_in_folder} videos in folder`);
    return lines.join("\n");
}

async function refresh(node) {
    const params = new URLSearchParams({
        source: widget(node, "source")?.value ?? "upload",
        video: widget(node, "video")?.value ?? "",
        path: widget(node, "path")?.value ?? "",
        video_index: widget(node, "video_index")?.value ?? 0,
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
    node.setDirtyCanvas(true, false);
}

function resetToSource(node) {
    const info = node.v4kInfo;
    if (!info || info.error) return;
    const resolution = widget(node, "resolution");
    if (resolution) resolution.value = "custom";
    const values = {
        custom_width: info.width,
        custom_height: info.height,
        force_frame_rate: Math.round(info.fps),
    };
    for (const name of SOURCE_FIELDS) {
        const w = widget(node, name);
        if (w) {
            w.value = values[name];
            w.callback?.(w.value);
        }
    }
    node.setDirtyCanvas(true, true);
}

// A widget rather than a foreground overlay, so litegraph gives it its own row
// instead of letting it land on top of the video preview.
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

app.registerExtension({
    name: "Video4K.Core",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "V4K_LoadVideo") return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = onCreated?.apply(this, arguments);
            this.addWidget("button", "⟲ reset to file", null, () => resetToSource(this));
            addInfoWidget(this);
            // Re-probe whenever the choice of file could have changed.
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

    },
});
