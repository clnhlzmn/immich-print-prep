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
// Bumped on every redraw of the grid; see proofKey.
let renderVersion = 0;

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
    renderVersion += 1;
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
        el("button", {
            class: "proof",
            title: "See this print larger",
            "aria-label": `See ${item.info.filename || "this print"} larger`,
            onClick: () => openViewer(store.selection.items.indexOf(item)),
        },
            el("img", {
                loading: "lazy",
                src: imageUrl.proof(item.id, { max_edge: 420, v: proofKey(item) }),
                alt: item.info.filename || item.id,
            }),
        ),
        el("div", { class: "meta" },
            el("div", { class: "name", title: item.info.filename || item.id }, item.info.filename || item.id),
            el("div", { class: "muted small" },
                summaryText(adj),
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

// The browser reuses an image whose URL it has seen, whatever the headers say.
// An uncaptioned proof depends only on its settings, so a settings key is
// enough. A caption also depends on Immich - descriptions, names, a lookup that
// failed - so captioned proofs get a fresh URL each time the grid is drawn,
// which keeps them in step with the Adjust dialog.
function proofKey(item) {
    const key = settingsKey(item.adjustments);
    return item.adjustments.caption ? `${key}.${renderVersion}` : key;
}

function summaryText(adj) {
    const fit = adj.caption ? "padded · caption" : adj.fit === "crop" ? "cropped to fill" : "padded";
    return `${trim(adj.width_in)} × ${trim(adj.height_in)} in · ${adj.dpi} dpi · ${fit}`;
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

// ---------- the larger proof ----------

function openViewer(startIndex) {
    const dialog = $("#viewer-dialog");
    let index = startIndex;

    const image = el("img", { alt: "" });
    const title = el("h2", {}, "");
    const position = el("span", { class: "muted small" }, "");
    const summary = el("span", { class: "muted small" }, "");

    const show = (wanted) => {
        const items = store.selection.items;
        if (!items.length) { dialog.close(); return; }
        index = (wanted + items.length) % items.length;    // wraps at either end
        const item = items[index];
        image.src = imageUrl.proof(item.id, { max_edge: 1400, v: proofKey(item) });
        image.alt = item.info.filename || item.id;
        title.textContent = item.info.filename || item.id;
        position.textContent = `${index + 1} of ${items.length}`;
        summary.textContent = summaryText(item.adjustments);
    };

    clear(dialog).append(
        el("div", { class: "head" },
            title,
            el("span", { class: "spacer", style: { flex: "1" } }),
            position,
            el("button", { class: "btn ghost small", onClick: () => dialog.close() }, "Close"),
        ),
        el("div", { class: "body viewer" }, image),
        el("div", { class: "foot" },
            el("button", { class: "btn", onClick: () => show(index - 1) }, "‹ Previous"),
            el("button", { class: "btn", onClick: () => show(index + 1) }, "Next ›"),
            summary,
            el("span", { class: "spacer", style: { flex: "1" } }),
            el("button", {
                class: "btn primary",
                onClick: () => {
                    const item = store.selection.items[index];
                    dialog.close();
                    openEditor(item);
                },
            }, "Adjust"),
        ),
    );
    dialog.addEventListener("keydown", (event) => {
        if (event.key === "ArrowRight") { event.preventDefault(); show(index + 1); }
        if (event.key === "ArrowLeft") { event.preventDefault(); show(index - 1); }
    });

    show(index);
    dialog.showModal();
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

    // Caption: Immich's text, unless the user writes their own for this print.
    let captionOverride = item.adjustments.caption_text ?? null;
    let captionAuto = "";
    const captionBox = el("textarea", {
        rows: 4, maxlength: 1000, spellcheck: true, placeholder: "No caption on this print",
        onInput: () => { captionOverride = captionBox.value; syncCaption(); refreshProof(); },
    });
    const captionNote = el("p", { class: "small muted", style: { margin: 0 } });
    const captionReset = el("button", {
        class: "btn small ghost",
        onClick: () => {
            captionOverride = null;
            captionBox.value = captionAuto;
            syncCaption();
            refreshProof();
        },
    }, "Use Immich's text");
    const captionField = el("fieldset", {},
        el("legend", {}, "Caption on this print"),
        captionBox,
        el("div", { class: "row", style: { marginTop: "6px" } }, captionReset),
        captionNote,
    );
    const syncCaption = () => {
        captionField.hidden = !values.caption;
        captionReset.hidden = captionOverride === null;
    };
    // Empty query values are dropped, so a deliberately blank caption travels as " ".
    const captionParams = () => ({
        caption: values.caption,
        caption_date: values.caption_date,
        caption_description: values.caption_description,
        caption_people: values.caption_people,
        ...(captionOverride === null
            ? { caption_auto: true }
            : { caption_text: captionOverride === "" ? " " : captionOverride }),
    });
    const loadCaption = debounce(async () => {
        syncCaption();
        if (!values.caption) return;
        try {
            const info = await api.caption(item.id, {
                ...captionParams(), rotate: values.rotate, crop: cropRectParam(),
            });
            captionAuto = info.auto;
            if (captionOverride === null) captionBox.value = info.auto;
            captionNote.textContent = info.notes.join(" ");
        } catch (error) {
            captionNote.textContent = error.message;
        }
    }, 250);

    const refreshProof = debounce(() => {
        proof.src = imageUrl.proof(item.id, {
            max_edge: 520,
            rotate: values.rotate,
            fit: values.fit,
            width_in: values.width_in,
            height_in: values.height_in,
            background: values.background,
            crop: cropRectParam(),
            ...captionParams(),
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
            loadCaption();      // toggles, rotation and crop all change Immich's caption
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
            captionField,
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
                adjustments: { ...values, crop: rect, caption_text: captionOverride },
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
        onCommit: () => { refreshProof(); loadCaption(); },
    });
    refreshProof();
    loadCaption();
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
        job = await api.prepare(null, {
            ...(record || {}),
            // Stamp names with this browser's clock, not the server's (often UTC),
            // so they match the preview shown beside each name.
            tz_offset_minutes: new Date().getTimezoneOffset(),
        });
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
        if (detail.caption_notes && detail.caption_notes.length) {
            extra.append(el("p", { class: "small muted" }, `Captions: ${detail.caption_notes.join("; ")}`));
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

    const state = el("span", { class: "muted small" }, "");
    const details = el("details", {},
        el("summary", {}, el("span", { class: "small" }, "Record this set in Immich"), state),
        el("div", { class: "record-body" }, album.node, tag.node, el("div", { class: "row" }, reset)),
    );
    // Open on a roomy screen; on a phone this bar would otherwise take half the
    // page, and its summary already says what will happen.
    details.open = window.matchMedia("(min-width: 700px)").matches;
    clear($("#set-record")).append(details);
    recordUi = { album, tag, reset, state };
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
    recordUi.state.textContent = "· %s · %s".replace("%s", record.create_album ? "album on" : "no album")
        .replace("%s", record.create_tag ? "tag on" : "no tag");
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
