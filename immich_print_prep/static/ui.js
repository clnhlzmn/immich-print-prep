// Minimal DOM helpers - enough structure to keep the views readable.

export function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
        if (key === "class") node.className = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key === "style") Object.assign(node.style, value);
        else if (key.startsWith("on") && typeof value === "function") {
            node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (value === true) node.setAttribute(key, "");
        else if (value === false || value === null || value === undefined) continue;
        else if (key in node && key !== "list") node[key] = value;
        else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
}

export function toast(message, kind = "info", timeout = 4500) {
    const host = $("#toasts");
    if (!host) return;
    const node = el("div", { class: `toast ${kind}` }, message);
    host.append(node);
    setTimeout(() => {
        node.style.transition = "opacity .3s";
        node.style.opacity = "0";
        setTimeout(() => node.remove(), 300);
    }, timeout);
}

export function debounce(fn, ms = 250) {
    let handle = null;
    return (...args) => {
        clearTimeout(handle);
        handle = setTimeout(() => fn(...args), ms);
    };
}

export function spinner(label = "Loading") {
    return el("div", { class: "empty" }, el("span", { class: "spinner" }), " ", label);
}

export function plural(count, one, many) {
    return `${count} ${count === 1 ? one : many}`;
}

// A dialog that resolves true/false, for destructive actions.
export function confirmDialog(title, message, confirmLabel = "Confirm") {
    return new Promise((resolve) => {
        const dialog = el("dialog", { style: { maxWidth: "460px" } },
            el("div", { class: "head" }, el("h2", {}, title)),
            el("div", { class: "body" }, el("p", { style: { margin: 0 } }, message)),
            el("div", { class: "foot" },
                el("span", { class: "spacer", style: { flex: "1" } }),
                el("button", { class: "btn", onClick: () => close(false) }, "Cancel"),
                el("button", { class: "btn primary", onClick: () => close(true) }, confirmLabel),
            ),
        );
        const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
        document.body.append(dialog);
        dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(false); });
        dialog.showModal();
    });
}
