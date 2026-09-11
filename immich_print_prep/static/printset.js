// The Print set view: proofs of every selected photo, bulk and per-photo
// adjustments, and the prepare/download flow.

import { api, ApiError, imageUrl } from "./api.js";
import { buildControls } from "./controls.js";
import { createCropper } from "./crop.js";
import { store } from "./store.js";
import { $, clear, confirmDialog, debounce, el, plural, toast } from "./ui.js";

let toolbar, content, actions;

// This set's "record in Immich" choices. Pre-filled from the saved settings and
// following them until the user edits something here; sent with Prepare.
let record = null;
let recordEdited = false;
let recordUi = null;
// Remembers the bulk toolbar's state across re-renders of the print set.
let toolbarValues = null;

export function mount() {
    toolbar = $("#set-toolbar");
    content = $("#set-content");
    actions = $("#set-actions");

    $("#btn-clear-set").addEventListener("click", async () => {
        if (!store.selection.count) return;
        const ok = await confirmDialog(
            "Empty the print set?",
            `This removes all ${plural(store.selection.count, "photo", "photos")} from the print set. Nothing is deleted from Immich.`,
            "Empty it",
        );
        if (!ok) return;
        await api.clearSelection();
        recordEdited = false;       // a new set starts from the saved settings again
        await store.refreshSelection();
        toast("Print set emptied.", "ok");
    });
    $("#btn-prepare").addEventListener("click", startPrepare);

    store.onSelection(render);
    store.onMe(syncRecord);
}

export function render() {
    const selection = store.selection;
    renderToolbar(selection);
    actions.hidden = selection.count === 0;
    syncRecord();
    $("#set-status").textContent = selection.count
        ? `${plural(selection.count, "photo", "photos")} ready`
        : "";

    if (!selection.count) {
        clear(content).append(el("div", { class: "empty" },
            "The print set is empty. Pick photos in the Library tab, then come back here to adjust and download them."));
        return;
    }

    const grid = el("div", { class: "set-grid" }, selection.items.map(card));
    clear(content).append(grid);
}

function card(item) {
    const adj = item.adjustments;
    return el("div", { class: "set-card" },
        el("div", { class: "proof" },
            el("img", {
                loading: "lazy",
                src: imageUrl.proof(item.id, { max_edge: 420, v: settingsKey(item.adjustments) }),
                alt: item.info.filename || item.id,
            }),
        ),
        el("div", { class: "meta" },
            el("div", { class: "name", title: item.info.filename || item.id }, item.info.filename || item.id),
            el("div", { class: "muted small" },
                `${trim(adj.width_in)} × ${trim(adj.height_in)} in · ${adj.dpi} dpi · ${adj.fit === "crop" ? "cropped to fill" : "padded"}`,
                item.customised ? el("span", { class: "pill", style: { marginLeft: "6px" } }, "custom") : null,
            ),
            el("div", { class: "row" },
                el("button", { class: "btn small", onClick: () => openEditor(item) }, "Adjust"),
                el("button", {
                    class: "btn small ghost danger",
                    onClick: async () => {
                        await api.removeAssets([item.id]);
                        await store.refreshSelection();
                    },
                }, "Remove"),
            ),
        ),
    );
}

// A short stable key for one photo's settings, so the browser can cache a proof
// until those settings actually change.
function settingsKey(adjustments) {
    const text = JSON.stringify(adjustments);
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) hash = (Math.imul(31, hash) + text.charCodeAt(i)) | 0;
    return (hash >>> 0).toString(36);
}

const trim = (value) => String(Number(value).toFixed(2)).replace(/\.?0+$/, "");

function renderToolbar(selection) {
    const controls = buildControls(toolbarValues || selection.defaults || store.defaults, {
        onChange: (values) => { toolbarValues = values; },
    });
    const applyAll = el("button", {
        class: "btn primary small",
        disabled: !selection.count,
        onClick: async () => {
            applyAll.disabled = true;
            try {
                await api.setAdjustments({ adjustments: controls.values() });
                await store.refreshSelection();
                toast("Applied to every photo in the print set.", "ok");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                applyAll.disabled = false;
            }
        },
    }, "Apply to all");

    const saveDefaults = el("button", {
        class: "btn small ghost",
        onClick: async () => {
            try {
                await api.saveSettings({ defaults: controls.values() });
                await store.loadMe();
                toast("Saved as your default print settings.", "ok");
            } catch (error) {
                toast(error.message, "error");
            }
        },
    }, "Save as my defaults");

    clear(toolbar).append(
        controls.node,
        el("span", { class: "spacer", style: { flex: "1" } }),
        saveDefaults,
        applyAll,
    );
}

// ---------- per-photo editor ----------

async function openEditor(item) {
    const dialog = $("#editor-dialog");
    const values = { ...item.adjustments };
    let cropper = null;

    const frame = el("div", { class: "frame" });
    const stage = el("div", { class: "stage" }, frame);
    const proof = el("img", { alt: "Print proof" });

    const image = el("img", {
        alt: item.info.filename || "",
        onLoad: () => { if (cropper) cropper.redraw(); },
    });
    frame.append(image);

    const lockCrop = el("input", {
        type: "checkbox", checked: true,
        onChange: () => cropper && cropper.setAspect(lockCrop.checked ? aspect() : null),
    });

    const aspect = () => values.width_in / values.height_in;

    const refreshProof = debounce(() => {
        proof.src = imageUrl.proof(item.id, {
            max_edge: 520,
            rotate: values.rotate,
            fit: values.fit,
            width_in: values.width_in,
            height_in: values.height_in,
            background: values.background,
            crop: cropRectParam(),
            v: Date.now(),
        });
    }, 200);

    const refreshSource = debounce(() => {
        image.src = imageUrl.source(item.id, { rotate: values.rotate, v: Date.now() });
    }, 100);

    const cropRectParam = () => {
        const rect = cropper ? cropper.get() : null;
        // "none" rather than "" because empty parameters are dropped, which
        // would fall back to the stored crop instead of clearing it.
        return rect ? [rect.x, rect.y, rect.w, rect.h].map((n) => n.toFixed(5)).join(",") : "none";
    };

    const controls = buildControls(item.adjustments, {
        compact: true,
        onChange: (next) => {
            const rotated = next.rotate !== values.rotate;
            Object.assign(values, next);
            if (lockCrop.checked && cropper) cropper.setAspect(aspect());
            if (rotated) { if (cropper) cropper.reset(); refreshSource(); }
            refreshProof();
        },
    });

    const body = el("div", { class: "editor" },
        el("div", {}, stage, el("p", { class: "muted small" },
            "Drag inside the box to move the crop, or the corners to resize it. Leave it off to use the whole photo.")),
        el("div", { class: "controls" },
            el("fieldset", {},
                el("legend", {}, "Proof"),
                el("div", { class: "proof-pane" }, proof),
            ),
            el("fieldset", {},
                el("legend", {}, "Crop"),
                el("label", { class: "check" }, lockCrop, "Lock to print ratio"),
                el("div", { class: "row", style: { marginTop: "8px" } },
                    el("button", { class: "btn small", onClick: () => cropper && cropper.enable() }, "Crop to frame"),
                    el("button", { class: "btn small ghost", onClick: () => cropper && cropper.reset() }, "Whole photo"),
                ),
            ),
            controls.node,
        ),
    );

    clear(dialog).append(
        el("div", { class: "head" },
            el("h2", {}, item.info.filename || "Adjust photo"),
            el("span", { class: "spacer", style: { flex: "1" } }),
            el("button", { class: "btn ghost small", onClick: () => dialog.close() }, "Close"),
        ),
        el("div", { class: "body" }, body),
        el("div", { class: "foot" },
            el("button", {
                class: "btn ghost small",
                onClick: async () => {
                    await api.setAdjustments({ ids: [item.id], reset: true });
                    await store.refreshSelection();
                    dialog.close();
                    toast("Reset to your default settings.", "ok");
                },
            }, "Reset to defaults"),
            el("span", { class: "spacer", style: { flex: "1" } }),
            el("button", { class: "btn", onClick: () => dialog.close() }, "Cancel"),
            el("button", { class: "btn primary", onClick: save }, "Save"),
        ),
    );

    async function save() {
        const rect = cropper ? cropper.get() : null;
        try {
            await api.setAdjustments({
                ids: [item.id],
                adjustments: { ...values, crop: rect },
            });
            await store.refreshSelection();
            dialog.close();
        } catch (error) {
            toast(error.message, "error");
        }
    }

    dialog.showModal();
    image.src = imageUrl.source(item.id, { rotate: values.rotate });
    cropper = createCropper({
        frame,
        aspect: aspect(),
        crop: item.adjustments.crop,
        onCommit: refreshProof,
    });
    refreshProof();
}

// ---------- prepare and download ----------

async function startPrepare() {
    const dialog = $("#job-dialog");
    const bar = el("progress", { max: store.selection.count, value: 0 });
    const message = el("p", { class: "small muted" }, "Starting…");
    const extra = el("div", { class: "small" });
    let job = null;
    let polling = true;

    const cancelButton = el("button", {
        class: "btn small danger",
        onClick: async () => {
            if (job) await api.cancelJob(job.id).catch(() => {});
        },
    }, "Cancel");
    const closeButton = el("button", { class: "btn", onClick: () => { polling = false; dialog.close(); } }, "Close");
    const foot = el("div", { class: "foot" }, cancelButton, el("span", { class: "spacer", style: { flex: "1" } }), closeButton);

    const heading = el("h2", {}, "Preparing your print set");
    clear(dialog).append(
        el("div", { class: "head" }, heading),
        el("div", { class: "body" }, bar, message, extra),
        foot,
    );
    dialog.showModal();

    try {
        job = await api.prepare(null, record ? { ...record } : {});
    } catch (error) {
        message.textContent = describe(error);
        cancelButton.hidden = true;
        return;
    }

    while (polling) {
        await new Promise((resolve) => setTimeout(resolve, 900));
        try {
            job = await api.job(job.id);
        } catch (error) {
            message.textContent = describe(error);
            break;
        }
        bar.max = job.total || 1;
        bar.value = job.done || 0;
        message.textContent = `${job.message || ""} (${job.done}/${job.total})`;
        if (["done", "error", "cancelled"].includes(job.status)) break;
    }

    cancelButton.hidden = true;
    if (job && job.status === "done") {
        heading.textContent = "Your print set is ready";
        bar.value = bar.max;
        message.textContent = `Ready: ${job.filename} (${formatSize(job.size)})`;
        const link = el("a", { class: "btn primary", href: job.download_url, download: job.filename }, "Download zip");
        clear(extra).append(el("div", { class: "row", style: { marginTop: "10px" } }, link));
        const detail = job.detail || {};
        if (detail.album && detail.album.added) {
            extra.append(el("p", { class: "small muted" },
                `Added to a new Immich album: ${detail.album.name} (${plural(detail.album.added, "photo", "photos")})`));
        }
        if (detail.tag && detail.tag.added) {
            extra.append(el("p", { class: "small muted" },
                `Tagged in Immich as: ${detail.tag.name} (${plural(detail.tag.added, "photo", "photos")})`));
        }
        if (detail.record_error) extra.append(el("p", { class: "small", style: { color: "var(--danger)" } }, `Could not record the set in Immich: ${detail.record_error}`));
        if (detail.failures && detail.failures.length) {
            extra.append(el("p", { class: "small", style: { color: "var(--danger)" } },
                `${plural(detail.failures.length, "photo", "photos")} could not be prepared: ` +
                detail.failures.map((failure) => `${failure.file} (${failure.error})`).join("; ")));
        }
        link.click();   // start the download without a second click
    } else if (job && job.status === "cancelled") {
        message.textContent = "Cancelled.";
    } else if (job) {
        message.textContent = `Failed: ${job.message || "unknown error"}`;
    }
}

// ---------- recording the set in Immich ----------

function recordDefaults() {
    const prefs = (store.me && store.me.prefs) || {};
    return {
        create_album: Boolean(prefs.create_album),
        album_name: prefs.album_name_template || "{datetime}-print-set",
        create_tag: Boolean(prefs.create_tag),
        tag_name: prefs.tag_name_template || "{datetime}-print-set",
    };
}

function buildRecordOptions() {
    const option = (label, checkKey, nameKey, hint) => {
        const check = el("input", {
            type: "checkbox",
            onChange: () => { record[checkKey] = check.checked; recordEdited = true; syncRecord(); },
        });
        const name = el("input", {
            type: "text", maxlength: 120, spellcheck: false, "aria-label": `${label} name`,
            onInput: () => { record[nameKey] = name.value; recordEdited = true; syncRecord(); },
        });
        const preview = el("span", { class: "muted small name-preview" });
        const node = el("div", { class: "record-row" },
            el("label", { class: "check" }, check, label),
            name,
            preview,
            hint ? el("span", { class: "muted small" }, hint) : null,
        );
        return { node, check, name, preview, checkKey, nameKey };
    };

    const album = option("Add to a new album", "create_album", "album_name");
    const tag = option("Tag the photos", "create_tag", "tag_name", "· only your own photos can be tagged");
    const reset = el("button", {
        class: "btn ghost small",
        onClick: () => { recordEdited = false; syncRecord(); },
    }, "Use my defaults");

    clear($("#set-record")).append(
        el("div", { class: "record-head" }, el("span", { class: "small muted" }, "Record this set in Immich"), reset),
        album.node,
        tag.node,
    );
    recordUi = { album, tag, reset };
}

function syncRecord() {
    if (!store.me) return;
    if (!record || !recordEdited) record = recordDefaults();
    if (!recordUi) buildRecordOptions();

    const defaults = recordDefaults();
    for (const part of [recordUi.album, recordUi.tag]) {
        const on = record[part.checkKey];
        part.check.checked = on;
        // Leave the field alone while it is being typed in.
        if (document.activeElement !== part.name) part.name.value = record[part.nameKey];
        part.name.disabled = !on;
        const template = record[part.nameKey].trim() || defaults[part.nameKey];
        part.preview.textContent = on ? `→ ${previewName(template, store.selection.count)}` : "";
    }
    recordUi.reset.hidden = !recordEdited;
}

// Mirrors render_name_template on the server, so the preview shows the name
// Immich will get. The time is the browser's; the real one is taken when the
// set is prepared.
function previewName(template, count) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
    const time = `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const out = template
        .replaceAll("{datetime}", `${date}_${time}`)
        .replaceAll("{date}", date)
        .replaceAll("{time}", time)
        .replaceAll("{count}", String(count))
        .replace(/[^\p{L}\p{N}_ \-.()\[\]{}#@+,']/gu, "_")
        .trim();
    return out.slice(0, 120);
}

function formatSize(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

function describe(error) {
    if (error instanceof ApiError && error.status === 409) {
        return `${error.message}. Add your Immich API key in Settings.`;
    }
    return error.message || String(error);
}
