import { useCallback, useEffect, useRef, useState } from "react";
import { errorText, httpText } from "./lib.js";

const REFRESH_MS = 10_000;

async function getJSON(path, signal) {
  const r = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!r.ok) throw Object.assign(new Error(httpText(r.status)), { status: r.status });
  return r.json();
}

// A write to the settings API. Every one carries the session's CSRF token.
export async function send(method, path, body, csrf) {
  const r = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json",
               ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const text = data.detail?.why ?? errorText(data.detail);
    throw Object.assign(new Error(text || httpText(r.status)), { status: r.status, detail: data.detail });
  }
  return data;
}

export async function getAlerts(signal) {
  return getJSON("/api/alerts", signal);
}

// Who is signed in: { setup_needed, signed_in, csrf, view_requires_login }.
export function useSession() {
  const [session, setSession] = useState(null);
  const refresh = useCallback(() => getJSON("/api/session").then(setSession, () => {}), []);
  useEffect(() => { refresh(); }, [refresh]);
  return [session, setSession, refresh];
}

// Polls the read-only API. Keeps the last good data when a refresh fails, so a
// blip shows as "stale" rather than an empty page.
export function useArgus() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [fetchedAt, setFetchedAt] = useState(null);
  const timer = useRef(null);
  const loader = useRef(null);
  const reload = useCallback(() => loader.current?.(), []);

  useEffect(() => {
    let ctrl;
    const load = async () => {
      clearTimeout(timer.current);
      ctrl = new AbortController();
      try {
        const [info, checks, heartbeats, updates, jobs, companions, start, version] = await Promise.all([
          getJSON("/api/info", ctrl.signal),
          getJSON("/api/checks", ctrl.signal),
          getJSON("/api/heartbeats", ctrl.signal),
          getJSON("/api/updates", ctrl.signal),
          getJSON("/api/jobs", ctrl.signal),
          getJSON("/api/companions", ctrl.signal),
          getJSON("/api/start", ctrl.signal),
          getJSON("/api/argus/version", ctrl.signal).catch(() => null),
        ]);
        setData({ info, checks, heartbeats, updates, jobs, companions, start, version });
        setError(null);
        setFetchedAt(Date.now());
      } catch (e) {
        if (e.name !== "AbortError") setError(e.message);
      }
      timer.current = setTimeout(load, REFRESH_MS);
    };
    loader.current = load;
    load();
    return () => {
      clearTimeout(timer.current);
      ctrl?.abort();
    };
  }, []);

  return { data, error, fetchedAt, reload };
}

export async function getCheck(name, signal) {
  return getJSON(`/api/checks/${encodeURIComponent(name)}`, signal);
}

export async function getJob(name, signal) {
  return getJSON(`/api/jobs/${encodeURIComponent(name)}`, signal);
}
