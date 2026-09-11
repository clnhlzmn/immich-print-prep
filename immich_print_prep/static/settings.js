// Settings dialog: Immich API key, what to record back in Immich, default
// print settings, and changing the account password.

import { api } from "./api.js";
import { buildControls } from "./controls.js";
import { store } from "./store.js";
import { $, clear, el, toast } from "./ui.js";

export async function openSettings() {
    const dialog = $("#settings-dialog");
    const me = store.me || (await store.loadMe());
    const prefs = me.prefs;

    const statusLine = el("p", { class: "small muted" }, "Checking the Immich connection…");
    const apiKeyInput = el("input", {
        type: "password",
        placeholder: me.has_api_key ? "•••••••• (stored)" : "Paste an Immich API key",
        autocomplete: "off",
        size: 40,
    });

    const saveKey = el("button", {
        class: "btn primary small",
        onClick: async () => {
            const key = apiKeyInput.value.trim();
            if (!key) return;
            saveKey.disabled = true;
            try {
                await api.saveSettings({ api_key: key });
                apiKeyInput.value = "";
                await store.loadMe();
                toast("Immich API key saved.", "ok");
                await refreshStatus();
            } catch (error) {
                toast(error.message, "error", 7000);
            } finally {
                saveKey.disabled = false;
            }
        },
    }, "Save key");

    const clearKey = el("button", {
        class: "btn small ghost danger",
        hidden: !me.has_api_key,
        onClick: async () => {
            await api.saveSettings({ clear_api_key: true });
            await store.loadMe();
            toast("Immich API key removed.", "ok");
            await refreshStatus();
        },
    }, "Remove key");

    async function refreshStatus() {
        statusLine.textContent = "Checking the Immich connection…";
        try {
            const status = await api.immichStatus();
            const who = status.user && (status.user.email || status.user.name);
            statusLine.textContent = status.connected
                ? `Connected to ${me.immich_url}${who ? ` as ${who}` : ""} (Immich ${status.version}).`
                : `Not connected: ${status.reason}.`;
            statusLine.style.color = status.connected ? "var(--ok)" : "var(--danger)";
        } catch (error) {
            statusLine.textContent = error.message;
            statusLine.style.color = "var(--danger)";
        }
        clearKey.hidden = !(store.me && store.me.has_api_key);
    }

    // --- recording downloads back in Immich ---
    const createAlbum = el("input", { type: "checkbox", checked: prefs.create_album });
    const albumTemplate = el("input", { type: "text", value: prefs.album_name_template, size: 28 });
    const createTag = el("input", { type: "checkbox", checked: prefs.create_tag });
    const tagTemplate = el("input", { type: "text", value: prefs.tag_name_template, size: 28 });
    const zipTemplate = el("input", { type: "text", value: prefs.zip_name_template, size: 28 });

    const defaults = buildControls(prefs.defaults, { compact: true });

    const savePrefs = el("button", {
        class: "btn primary small",
        onClick: async () => {
            savePrefs.disabled = true;
            try {
                await api.saveSettings({
                    create_album: createAlbum.checked,
                    album_name_template: albumTemplate.value.trim(),
                    create_tag: createTag.checked,
                    tag_name_template: tagTemplate.value.trim(),
                    zip_name_template: zipTemplate.value.trim(),
                    defaults: defaults.values(),
                });
                await store.loadMe();
                await store.refreshSelection();
                toast("Settings saved.", "ok");
            } catch (error) {
                toast(error.message, "error");
            } finally {
                savePrefs.disabled = false;
            }
        },
    }, "Save settings");

    // --- password ---
    const currentPassword = el("input", { type: "password", autocomplete: "current-password" });
    const newPassword = el("input", { type: "password", autocomplete: "new-password", minlength: "8" });
    const confirmPassword = el("input", { type: "password", autocomplete: "new-password", minlength: "8" });
    const passwordNote = el("div", { class: "error-text" });

    const changePassword = el("button", {
        class: "btn small",
        onClick: async () => {
            passwordNote.style.color = "";
            if (newPassword.value !== confirmPassword.value) {
                passwordNote.textContent = "The two new passwords do not match.";
                return;
            }
            changePassword.disabled = true;
            try {
                await api.changePassword({
                    current_password: currentPassword.value,
                    new_password: newPassword.value,
                });
                currentPassword.value = newPassword.value = confirmPassword.value = "";
                passwordNote.style.color = "var(--ok)";
                passwordNote.textContent = "Password changed. Other devices have been signed out.";
            } catch (error) {
                passwordNote.textContent = error.message;
            } finally {
                changePassword.disabled = false;
            }
        },
    }, "Change password");

    clear(dialog).append(
        el("div", { class: "head" },
            el("h2", {}, "Settings"),
            el("span", { class: "spacer", style: { flex: "1" } }),
            el("button", { class: "btn ghost small", onClick: () => dialog.close() }, "Close"),
        ),
        el("div", { class: "body", style: { display: "grid", gap: "18px" } },
            el("fieldset", { class: "controls" },
                el("legend", {}, "Immich account"),
                statusLine,
                el("p", { class: "small muted", style: { margin: "0" } },
                    "Create a key in Immich under Account Settings → API Keys. It is stored encrypted and never sent to your browser.",
                ),
                el("p", { class: "small muted", style: { margin: "0" } },
                    "The key needs these permissions: ",
                    el("code", {}, "album.read, asset.read, asset.view, asset.download"),
                    ". Add ", el("code", {}, "tag.read"), " to browse by tag, and ",
                    el("code", {}, "album.create, albumAsset.create, tag.create, tag.asset"),
                    " to record downloads back in Immich.",
                ),
                el("div", { class: "row" }, apiKeyInput, saveKey, clearKey),
            ),
            el("fieldset", { class: "controls" },
                el("legend", {}, "Recording downloads in Immich"),
                el("p", { class: "small muted", style: { margin: 0 } },
                    "These are the defaults for each new print set; you can change them for a single set on the Print set page."),
                el("label", { class: "check" }, createAlbum, "Add the downloaded photos to a new Immich album"),
                el("label", { class: "field" }, "Album name", albumTemplate),
                el("label", { class: "check" }, createTag, "Tag the downloaded photos in Immich"),
                el("p", { class: "small muted", style: { margin: 0 } },
                    "Immich only allows tagging photos you own; a partner's photos can go in the album but not the tag."),
                el("label", { class: "field" }, "Tag name", tagTemplate),
                el("label", { class: "field" }, "Zip file name", zipTemplate),
                el("p", { class: "small muted", style: { margin: 0 } },
                    "{datetime}, {date}, {time} and {count} are filled in when the set is prepared."),
            ),
            el("fieldset", { class: "controls" },
                el("legend", {}, "Default print settings for new photos"),
                defaults.node,
            ),
            el("div", { class: "row" }, el("span", { class: "spacer", style: { flex: "1" } }), savePrefs),
            el("fieldset", { class: "controls" },
                el("legend", {}, "Password"),
                el("label", { class: "field" }, "Current password", currentPassword),
                el("label", { class: "field" }, "New password", newPassword),
                el("label", { class: "field" }, "Confirm new password", confirmPassword),
                passwordNote,
                el("div", { class: "row" }, changePassword),
            ),
        ),
    );

    dialog.showModal();
    refreshStatus();
}
