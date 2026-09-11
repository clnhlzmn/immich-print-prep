// Thin wrapper around the JSON API. Every call goes through here so that a
// dropped session lands on the login page instead of a stack of failed fetches.

export class ApiError extends Error {
    constructor(message, status, payload) {
        super(message);
        this.status = status;
        this.payload = payload || {};
    }
}

async function request(path, { method = "GET", body, params } = {}) {
    const url = new URL(path, window.location.origin);
    if (params) {
        for (const [key, value] of Object.entries(params)) {
            if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
        }
    }
    const options = { method, headers: {} };
    if (body !== undefined) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    if (response.status === 401) {
        window.location.href = "/login";
        throw new ApiError("Signed out", 401);
    }
    let payload = null;
    if (response.status !== 204) {
        try { payload = await response.json(); } catch (e) { payload = null; }
    }
    if (!response.ok) {
        const detail = payload && payload.detail;
        throw new ApiError(
            typeof detail === "string" ? detail : `Request failed (${response.status})`,
            response.status,
            payload,
        );
    }
    return payload;
}

export const api = {
    me: () => request("/api/me"),
    saveSettings: (body) => request("/api/settings", { method: "PUT", body }),
    immichStatus: () => request("/api/immich/status"),
    changePassword: (body) => request("/api/auth/change-password", { method: "POST", body }),
    logout: () => request("/api/auth/logout", { method: "POST", body: {} }),

    albums: () => request("/api/albums"),
    tags: () => request("/api/tags"),
    assets: (params) => request("/api/assets", { params }),

    selection: () => request("/api/selection"),
    addAssets: (assets) => request("/api/selection/add", { method: "POST", body: { assets } }),
    addSource: (body) => request("/api/selection/add-source", { method: "POST", body }),
    removeAssets: (ids) => request("/api/selection/remove", { method: "POST", body: { ids } }),
    clearSelection: () => request("/api/selection/clear", { method: "POST", body: {} }),
    setAdjustments: (body) => request("/api/selection/adjustments", { method: "PUT", body }),
    geometry: (id, params) => request(`/api/selection/${id}/geometry`, { params }),
    caption: (id, params) => request(`/api/selection/${id}/caption`, { params }),

    // `options` carries this set's album/tag choices; omitted keys use the saved settings.
    prepare: (ids, options = {}) =>
        request("/api/prepare", { method: "POST", body: { ...(ids ? { ids } : {}), ...options } }),
    job: (id) => request(`/api/jobs/${id}`),
    cancelJob: (id) => request(`/api/jobs/${id}/cancel`, { method: "POST", body: {} }),
};

// Image URLs (used as <img src>, so they are built rather than fetched).
export const imageUrl = {
    thumb: (id, size = "thumbnail") => `/api/assets/${id}/thumb?size=${size}`,
    proof: (id, params = {}) => withParams(`/api/selection/${id}/preview`, params),
    source: (id, params = {}) => withParams(`/api/selection/${id}/source`, params),
};

function withParams(path, params) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
    }
    return url.pathname + url.search;
}
