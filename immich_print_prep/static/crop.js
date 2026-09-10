// A draggable crop rectangle over an image, in normalised (0..1) coordinates.
//
// The rectangle can be locked to the print aspect ratio. Because the ratio is a
// ratio of *pixels* and the coordinates are fractions of the image, the height
// fraction is the width fraction times `imageWidth / (imageHeight * ratio)`.

import { el } from "./ui.js";

export function createCropper({ frame, aspect, crop, onChange, onCommit }) {
    let rect = crop ? { ...crop } : null;
    let ratio = aspect || null;

    const node = el("div", { class: "croprect" },
        el("span", { class: "handle nw", dataset: { handle: "nw" } }),
        el("span", { class: "handle ne", dataset: { handle: "ne" } }),
        el("span", { class: "handle sw", dataset: { handle: "sw" } }),
        el("span", { class: "handle se", dataset: { handle: "se" } }),
    );
    frame.append(node);

    const size = () => ({ w: frame.clientWidth || 1, h: frame.clientHeight || 1 });

    // Height fraction per unit of width fraction, for the locked ratio.
    function k() {
        const { w, h } = size();
        return ratio ? w / (h * ratio) : null;
    }

    function defaultRect() {
        const factor = k();
        if (!factor) return { x: 0.05, y: 0.05, w: 0.9, h: 0.9 };
        let w = 1;
        let h = w * factor;
        if (h > 1) { h = 1; w = h / factor; }
        return { x: (1 - w) / 2, y: (1 - h) / 2, w, h };
    }

    function clampRect(next) {
        const out = { ...next };
        out.w = Math.min(Math.max(out.w, 0.02), 1);
        out.h = Math.min(Math.max(out.h, 0.02), 1);
        out.x = Math.min(Math.max(out.x, 0), 1 - out.w);
        out.y = Math.min(Math.max(out.y, 0), 1 - out.h);
        return out;
    }

    function draw() {
        if (!rect) { node.hidden = true; return; }
        node.hidden = false;
        node.style.left = `${rect.x * 100}%`;
        node.style.top = `${rect.y * 100}%`;
        node.style.width = `${rect.w * 100}%`;
        node.style.height = `${rect.h * 100}%`;
    }

    function emit(commit) {
        if (onChange) onChange(rect ? { ...rect } : null);
        if (commit && onCommit) onCommit(rect ? { ...rect } : null);
    }

    function resizeFrom(handle, dx, dy, start) {
        const factor = k();
        let { x, y, w, h } = start;
        const right = x + w;
        const bottom = y + h;

        if (factor) {
            // Drive the box from the horizontal drag and derive the height, so
            // the rectangle can never drift off the print ratio.
            const growth = handle === "ne" || handle === "se" ? dx : -dx;
            w = Math.min(Math.max(start.w + growth, 0.02), 1);
            h = w * factor;
            if (h > 1) { h = 1; w = h / factor; }
            x = handle === "nw" || handle === "sw" ? right - w : x;
            y = handle === "nw" || handle === "ne" ? bottom - h : y;
        } else {
            if (handle === "se") { w = start.w + dx; h = start.h + dy; }
            if (handle === "sw") { w = start.w - dx; h = start.h + dy; x = right - w; }
            if (handle === "ne") { w = start.w + dx; h = start.h - dy; y = bottom - h; }
            if (handle === "nw") { w = start.w - dx; h = start.h - dy; x = right - w; y = bottom - h; }
        }
        return clampRect({ x, y, w, h });
    }

    function onPointerDown(event) {
        if (!rect) return;
        const handle = event.target.dataset ? event.target.dataset.handle : null;
        if (!handle && event.target !== node) return;
        event.preventDefault();
        const { w: frameW, h: frameH } = size();
        const startPoint = { x: event.clientX, y: event.clientY };
        const start = { ...rect };
        const target = event.currentTarget;
        target.setPointerCapture(event.pointerId);

        const move = (moveEvent) => {
            const dx = (moveEvent.clientX - startPoint.x) / frameW;
            const dy = (moveEvent.clientY - startPoint.y) / frameH;
            rect = handle
                ? resizeFrom(handle, dx, dy, start)
                : clampRect({ ...start, x: start.x + dx, y: start.y + dy });
            draw();
            emit(false);
        };
        const up = () => {
            target.removeEventListener("pointermove", move);
            target.removeEventListener("pointerup", up);
            emit(true);
        };
        target.addEventListener("pointermove", move);
        target.addEventListener("pointerup", up);
    }

    node.addEventListener("pointerdown", onPointerDown);

    draw();

    return {
        get() { return rect ? { ...rect } : null; },
        set(next) { rect = next ? clampRect(next) : null; draw(); emit(false); },
        reset() { rect = null; draw(); emit(true); },
        enable() { rect = clampRect(defaultRect()); draw(); emit(true); },
        setAspect(next) {
            ratio = next || null;
            if (rect) { rect = clampRect(resizeFrom("se", 0, 0, rect)); draw(); emit(true); }
        },
        redraw: draw,
        destroy() { node.remove(); },
    };
}
