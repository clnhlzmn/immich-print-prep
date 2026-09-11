// Shared adjustment controls: the same widgets drive the bulk toolbar, the
// per-photo editor and the default settings panel.

import { el } from "./ui.js";

const SIZES = [
    { label: '4 × 6 in', w: 4, h: 6 },
    { label: '5 × 7 in', w: 5, h: 7 },
    { label: '8 × 10 in', w: 8, h: 10 },
    { label: '8.5 × 11 in', w: 8.5, h: 11 },
    { label: '11 × 14 in', w: 11, h: 14 },
    { label: 'A4 (8.27 × 11.69 in)', w: 8.27, h: 11.69 },
];

const ROTATIONS = [
    { value: "auto", label: "Auto (landscape → 90° CCW)" },
    { value: "none", label: "Leave as shot" },
    { value: "ccw", label: "90° counter-clockwise" },
    { value: "cw", label: "90° clockwise" },
    { value: "180", label: "180°" },
];

export function buildControls(initial, { compact = false, onChange = null } = {}) {
    const values = { ...initial };
    const fire = () => onChange && onChange({ ...values });

    const sizeSelect = el("select", {
        onChange: () => {
            const choice = sizeSelect.value;
            if (choice === "custom") {
                customRow.hidden = false;
            } else {
                customRow.hidden = true;
                const [w, h] = choice.split("x").map(Number);
                values.width_in = w;
                values.height_in = h;
                widthInput.value = w;
                heightInput.value = h;
            }
            fire();
        },
    },
        SIZES.map((size) => el("option", { value: `${size.w}x${size.h}` }, size.label)),
        el("option", { value: "custom" }, "Custom size"),
    );

    const widthInput = el("input", {
        type: "number", min: "0.5", max: "60", step: "0.01", value: initial.width_in,
        onChange: () => { values.width_in = Number(widthInput.value); fire(); },
    });
    const heightInput = el("input", {
        type: "number", min: "0.5", max: "60", step: "0.01", value: initial.height_in,
        onChange: () => { values.height_in = Number(heightInput.value); fire(); },
    });
    const customRow = el("div", { class: "grid2" },
        el("label", { class: "field" }, "Width (in)", widthInput),
        el("label", { class: "field" }, "Height (in)", heightInput),
    );

    const matched = SIZES.find((size) => size.w === initial.width_in && size.h === initial.height_in);
    sizeSelect.value = matched ? `${matched.w}x${matched.h}` : "custom";
    customRow.hidden = Boolean(matched);

    const rotateSelect = el("select", {
        onChange: () => { values.rotate = rotateSelect.value; fire(); },
    }, ROTATIONS.map((option) => el("option", { value: option.value }, option.label)));
    rotateSelect.value = initial.rotate;

    const fitSelect = el("select", {
        onChange: () => { values.fit = fitSelect.value; fire(); },
    },
        el("option", { value: "pad" }, "Pad to fit (keeps the whole photo)"),
        el("option", { value: "crop" }, "Crop to fill"),
    );
    fitSelect.value = initial.fit;

    const backgroundInput = el("input", {
        type: "color", value: initial.background,
        onInput: () => { values.background = backgroundInput.value; fire(); },
    });

    const dpiInput = el("input", {
        type: "number", min: "36", max: "1200", step: "1", value: initial.dpi,
        onChange: () => { values.dpi = Number(dpiInput.value); fire(); },
    });
    const qualityInput = el("input", {
        type: "number", min: "1", max: "100", step: "1", value: initial.quality,
        onChange: () => { values.quality = Number(qualityInput.value); fire(); },
    });
    const formatSelect = el("select", {
        onChange: () => { values.fmt = formatSelect.value; fire(); },
    },
        el("option", { value: "jpeg" }, "JPEG"),
        el("option", { value: "png" }, "PNG"),
        el("option", { value: "tiff" }, "TIFF"),
    );
    formatSelect.value = initial.fmt;

    const enlargeInput = el("input", {
        type: "checkbox", checked: initial.allow_enlarge,
        onChange: () => { values.allow_enlarge = enlargeInput.checked; fire(); },
    });

    // Caption in the border padding leaves; the parts come from Immich.
    const captionParts = el("div", { class: "row caption-parts", hidden: !initial.caption });
    const captionInput = el("input", {
        type: "checkbox", checked: Boolean(initial.caption),
        onChange: () => {
            values.caption = captionInput.checked;
            captionParts.hidden = !captionInput.checked;
            fire();
        },
    });
    const part = (key, label) => {
        const input = el("input", {
            type: "checkbox", checked: initial[key] !== false,
            onChange: () => { values[key] = input.checked; fire(); },
        });
        return el("label", { class: "check" }, input, label);
    };
    captionParts.append(
        part("caption_date", "Date & time"),
        part("caption_description", "Description"),
        part("caption_people", "People"),
    );

    const node = el("div", { class: compact ? "controls" : "row" },
        el("label", { class: "field" }, "Print size", sizeSelect),
        customRow,
        el("label", { class: "field" }, "Orientation", rotateSelect),
        el("label", { class: "field" }, "Fit", fitSelect),
        el("label", { class: "field" }, "Pad colour", backgroundInput),
        el("label", { class: "field" }, "DPI", dpiInput),
        el("label", { class: "field" }, "JPEG quality", qualityInput),
        el("label", { class: "field" }, "Format", formatSelect),
        el("label", { class: "check" }, enlargeInput, "Enlarge photos smaller than the print"),
        el("label", { class: "check" }, captionInput, "Caption in the border"),
        captionParts,
    );

    return { node, values: () => ({ ...values }) };
}
