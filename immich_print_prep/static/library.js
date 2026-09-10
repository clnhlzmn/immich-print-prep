// The Library view: albums, tags, the whole timeline, and search.

import { api, ApiError, imageUrl } from "./api.js";
import { store } from "./store.js";
import { $, clear, el, plural, spinner, toast } from "./ui.js";

const PAGE_SIZE = 120;

const state = {
    source: "albums",
    albums: null,
    tags: null,
    context: null,      // {type: "album"|"tag", id, name} while drilled into one
    query: "",
    smart: true,
    items: [],
    page: 1,
    nextPage: null,
    nextCursor: null,
    exhausted: false,
    loading: false,
    selected: new Set(),
    lastIndex: null,
};

let content, actions, status, crumb, backButton, searchForm;

export function mount() {
    content = $("#library-content");
    actions = $("#library-actions");
    status = $("#library-status");
    crumb = $("#crumb");
    backButton = $("#btn-back");
    searchForm = $("#search-form");

    for (const button of document.querySelectorAll("#source-tabs button")) {
        button.addEventListener("click", () => switchSource(button.dataset.source));
    }
    backButton.addEventListener("click", () => {
        state.context = null;
        switchSource(state.source, true);
    });
    searchForm.addEventListener("submit", (event) => {
        event.preventDefault();
        state.query = $("#search-input").value.trim();
        state.smart = $("#search-smart").checked;
        loadAssets(true);
    });

    $("#btn-select-all").addEventListener("click", () => {
        state.items.forEach((item) => state.selected.add(item.id));
        renderAssets();
    });
    $("#btn-select-none").addEventListener("click", () => {
        state.selected.clear();
        renderAssets();
    });
    $("#btn-add-selected").addEventListener("click", addSelected);
    $("#btn-add-everything").addEventListener("click", addEverything);

    store.onSelection(() => {
        if (state.source !== "albums" && state.source !== "tags") renderAssets();
        else if (state.context) renderAssets();
    });

    // The library cannot load anything until an Immich key is stored, so reload
    // it the moment one is saved in the settings panel.
    let hadKey = null;
    store.onMe(() => {
        const hasKey = Boolean(store.me && store.me.has_api_key);
        if (hadKey !== null && hasKey && !hadKey) {
            state.albums = null;
            state.tags = null;
            switchSource(state.source, true);
        }
        hadKey = hasKey;
    });

    switchSource("albums");
}

function switchSource(source, keepContext = false) {
    state.source = source;
    if (!keepContext) state.context = null;
    state.selected.clear();
    state.items = [];
    state.lastIndex = null;
    for (const button of document.querySelectorAll("#source-tabs button")) {
        button.setAttribute("aria-selected", String(button.dataset.source === source));
    }
    searchForm.hidden = source !== "search";
    backButton.hidden = !state.context;
    crumb.textContent = "";
    actions.hidden = true;

    if (source === "albums" && !state.context) renderAlbums();
    else if (source === "tags" && !state.context) renderTags();
    else if (source === "search") {
        clear(content).append(
            el("div", { class: "empty" }, "Search your library. Contextual search finds photos by what is in them; switch it off to match file names."),
        );
        if (state.query) loadAssets(true);
    } else loadAssets(true);
}

// ---------- albums and tags ----------

async function renderAlbums() {
    clear(content).append(spinner("Loading albums"));
    try {
        if (!state.albums) state.albums = (await api.albums()).albums;
    } catch (error) {
        return showError(error);
    }
    if (!state.albums.length) {
        return void clear(content).append(el("div", { class: "empty" }, "No albums in this Immich account."));
    }
    const grid = el("div", { class: "card-grid" },
        state.albums.map((album) => el("button", {
            class: "album-card",
            onClick: () => openContext({ type: "album", id: album.id, name: album.name }),
        },
            album.thumbnailAssetId
                ? el("img", { class: "thumb", loading: "lazy", src: imageUrl.thumb(album.thumbnailAssetId), alt: "" })
                : el("div", { class: "thumb" }),
            el("div", { class: "meta" },
                el("div", { class: "name", title: album.name }, album.name),
                el("div", { class: "muted small" }, plural(album.assetCount || 0, "photo", "photos")),
            ),
        )),
    );
    clear(content).append(grid);
}

async function renderTags() {
    clear(content).append(spinner("Loading tags"));
    try {
        if (!state.tags) state.tags = (await api.tags()).tags;
    } catch (error) {
        return showError(error);
    }
    if (!state.tags.length) {
        return void clear(content).append(el("div", { class: "empty" }, "No tags in this Immich account."));
    }
    clear(content).append(el("div", { class: "row" },
        state.tags.map((tag) => el("button", {
            class: "btn",
            onClick: () => openContext({ type: "tag", id: tag.id, name: tag.value || tag.name }),
        }, tag.value || tag.name)),
    ));
}

function openContext(context) {
    state.context = context;
    backButton.hidden = false;
    crumb.textContent = context.name;
    loadAssets(true);
}

// ---------- asset grids ----------

async function loadAssets(reset) {
    if (state.loading) return;
    if (reset) {
        state.items = [];
        state.page = 1;
        state.nextPage = null;
        state.nextCursor = null;
        state.exhausted = false;
        state.selected.clear();
        state.lastIndex = null;
        clear(content).append(spinner("Loading photos"));
    }
    state.loading = true;
    renderStatus();

    const params = { size: PAGE_SIZE, page: state.page };
    if (state.nextCursor) params.cursor = state.nextCursor;
    if (state.context && state.context.type === "album") params.album_id = state.context.id;
    if (state.context && state.context.type === "tag") params.tag_id = state.context.id;
    if (state.source === "search") {
        params.q = state.query;
        params.smart = state.smart;
    }

    try {
        const result = await api.assets(params);
        const known = new Set(state.items.map((item) => item.id));
        state.items.push(...result.items.filter((item) => !known.has(item.id)));
        state.nextPage = result.nextPage;
        state.nextCursor = result.nextCursor;
        state.page = result.nextPage || state.page + 1;
        state.exhausted = !result.nextPage && !result.nextCursor;
        state.total = result.total;
    } catch (error) {
        state.loading = false;
        return showError(error);
    }
    state.loading = false;
    renderAssets();
}

function renderAssets() {
    if (!state.items.length && state.loading) return;
    const grid = el("div", { class: "asset-grid" },
        state.items.map((item, index) => assetTile(item, index)),
    );
    clear(content).append(grid);
    if (!state.items.length) {
        clear(content).append(el("div", { class: "empty" },
            state.source === "search" && state.query ? "No photos matched that search." : "Nothing here."));
    } else if (!state.exhausted) {
        content.append(el("div", { class: "row", style: { justifyContent: "center", marginTop: "14px" } },
            el("button", {
                class: "btn",
                disabled: state.loading,
                onClick: () => loadAssets(false),
            }, state.loading ? "Loading..." : "Load more"),
        ));
    }
    actions.hidden = false;
    $("#btn-add-everything").hidden = !(state.context || (state.source === "search" && state.query));
    renderStatus();
}

function assetTile(item, index) {
    const chosen = state.selected.has(item.id);
    const inSet = store.setIds.has(item.id);
    return el("button", {
        class: `asset${inSet ? " in-set" : ""}`,
        "aria-pressed": String(chosen),
        title: item.filename || "",
        onClick: (event) => toggle(item, index, event),
    },
        el("img", { loading: "lazy", src: imageUrl.thumb(item.id), alt: item.filename || "" }),
        el("span", { class: "mark" }, chosen ? "✓" : ""),
        inSet ? el("span", { class: "badge" }, "in set") : null,
    );
}

function toggle(item, index, event) {
    if (event.shiftKey && state.lastIndex !== null) {
        const [from, to] = [state.lastIndex, index].sort((a, b) => a - b);
        const adding = !state.selected.has(item.id);
        for (let i = from; i <= to; i += 1) {
            const id = state.items[i].id;
            if (adding) state.selected.add(id);
            else state.selected.delete(id);
        }
    } else {
        if (state.selected.has(item.id)) state.selected.delete(item.id);
        else state.selected.add(item.id);
        state.lastIndex = index;
    }
    renderAssets();
}

function renderStatus() {
    const parts = [];
    if (state.items.length) {
        parts.push(`${state.items.length}${state.total && state.total > state.items.length ? ` of ${state.total}` : ""} shown`);
    }
    if (state.selected.size) parts.push(`${state.selected.size} selected`);
    status.textContent = parts.join(" · ");
    $("#btn-add-selected").disabled = state.selected.size === 0;
    $("#btn-add-selected").textContent = state.selected.size
        ? `Add ${state.selected.size} to print set`
        : "Add to print set";
}

// ---------- adding to the print set ----------

async function addSelected() {
    const assets = state.items.filter((item) => state.selected.has(item.id));
    if (!assets.length) return;
    const button = $("#btn-add-selected");
    button.disabled = true;
    try {
        const result = await api.addAssets(assets);
        state.selected.clear();
        await store.refreshSelection();
        toast(`Added ${plural(result.added, "photo", "photos")} to the print set.`, "ok");
        renderAssets();
    } catch (error) {
        showToastError(error);
    } finally {
        button.disabled = false;
    }
}

async function addEverything() {
    const button = $("#btn-add-everything");
    button.disabled = true;
    const previous = button.textContent;
    button.textContent = "Adding...";
    try {
        const body = {};
        if (state.context && state.context.type === "album") body.album_id = state.context.id;
        else if (state.context && state.context.type === "tag") body.tag_id = state.context.id;
        else { body.q = state.query; body.smart = state.smart; }
        const result = await api.addSource(body);
        await store.refreshSelection();
        toast(
            result.added
                ? `Added ${plural(result.added, "photo", "photos")} to the print set.`
                : "Everything here was already in the print set.",
            "ok",
        );
        renderAssets();
    } catch (error) {
        showToastError(error);
    } finally {
        button.disabled = false;
        button.textContent = previous;
    }
}

function showError(error) {
    clear(content).append(el("div", { class: "empty" }, describe(error)));
    if (error instanceof ApiError && error.status === 409) {
        content.append(el("div", { class: "row", style: { justifyContent: "center" } },
            el("button", { class: "btn primary", onClick: () => $("#btn-settings").click() }, "Open settings"),
        ));
    }
}

function showToastError(error) {
    toast(describe(error), "error", 7000);
}

function describe(error) {
    if (error instanceof ApiError && error.status === 409) {
        return `${error.message}. Add your Immich API key in Settings.`;
    }
    return error.message || String(error);
}
