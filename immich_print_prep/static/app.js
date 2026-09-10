// Boot: load the session, wire the two tabs, mount the views.

import { api } from "./api.js";
import * as library from "./library.js";
import * as printset from "./printset.js";
import { openSettings } from "./settings.js";
import { store } from "./store.js";
import { $, toast } from "./ui.js";

function showView(name) {
    const isLibrary = name === "library";
    $("#view-library").hidden = !isLibrary;
    $("#view-set").hidden = isLibrary;
    $("#tab-library").setAttribute("aria-selected", String(isLibrary));
    $("#tab-set").setAttribute("aria-selected", String(!isLibrary));
    if (!isLibrary) printset.render();
    window.location.hash = isLibrary ? "" : "#print-set";
}

async function boot() {
    $("#tab-library").addEventListener("click", () => showView("library"));
    $("#tab-set").addEventListener("click", () => showView("set"));
    $("#btn-settings").addEventListener("click", () => openSettings());
    $("#btn-logout").addEventListener("click", async () => {
        await api.logout().catch(() => {});
        window.location.href = "/login";
    });

    store.onSelection(() => {
        $("#set-count").textContent = String(store.selection.count);
    });
    store.onMe(() => {
        document.title = store.me.site_title;
        $("#site-title").textContent = store.me.site_title;
        $("#who").textContent = store.me.username;
    });

    library.mount();
    printset.mount();

    try {
        await store.loadMe();
        await store.refreshSelection();
    } catch (error) {
        toast(error.message, "error");
        return;
    }

    if (!store.me.has_api_key) {
        toast("Add your Immich API key in Settings to start browsing.", "info", 9000);
        openSettings();
    }
    if (window.location.hash === "#print-set") showView("set");
}

boot();
