// Shared state: who is signed in, and what is currently in the print set.

import { api } from "./api.js";

class Store extends EventTarget {
    constructor() {
        super();
        this.me = null;
        this.selection = { count: 0, items: [], defaults: {} };
        this.setIds = new Set();
    }

    async loadMe() {
        this.me = await api.me();
        this.dispatchEvent(new CustomEvent("me"));
        return this.me;
    }

    async refreshSelection() {
        this.selection = await api.selection();
        this.setIds = new Set(this.selection.items.map((item) => item.id));
        this.dispatchEvent(new CustomEvent("selection"));
        return this.selection;
    }

    get defaults() {
        return (this.me && this.me.prefs && this.me.prefs.defaults) || this.selection.defaults || {};
    }

    onSelection(handler) {
        this.addEventListener("selection", handler);
    }

    onMe(handler) {
        this.addEventListener("me", handler);
    }
}

export const store = new Store();
