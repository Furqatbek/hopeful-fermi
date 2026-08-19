/**
 * Text size and background colour, from the Settings button in the top right.
 *
 * `docs/design/0013` §5: the real client offers both, and they are not
 * decoration — a student who has practised at extra-large will look for that
 * control under pressure, and the real test has it.
 *
 * Both ride on attributes set on `<html>` rather than on React state threaded
 * through components, so every pane changes together and a pane added later
 * cannot forget to participate.
 *
 * Both lists are now VERIFIED rather than guessed (design 0013 §5): three text
 * sizes named Standard / Large / Extra large, and four colour options under a
 * heading spelled "Colours". The one thing still unknown is whether the real
 * client persists these across sections — its own settings live in module-scoped
 * variables that reset on every page load, which suggests not. We persist them,
 * deliberately: a student who needs extra-large needs it in every section, and
 * making them set it four times is a worse product than the thing we are
 * imitating.
 *
 * **Persisting to localStorage was half of that promise, and only the exam
 * runner ever kept it.** `useDisplaySettings()` used to be called from inside
 * `Runner` alone, so the `useEffect`s that write `data-theme`/`data-size` onto
 * `<html>` only ran while a student was actually sitting a paper — the moment
 * they returned to Home, submitted to Result, or opened Review, the attribute
 * was gone and those screens rendered in the light default regardless of what
 * had been chosen, even though the CHOICE was still sitting in localStorage the
 * whole time. `DisplaySettingsProvider` below calls the hook exactly ONCE, at
 * the top of the app, so the attribute — and the choice — hold across every
 * screen. It has to be exactly once: the hook owns `useState` plus the
 * `useEffect`s that write it, and a second call site is a second, independent
 * copy that can drift from the first the moment either one's setter fires.
 */

import {
  createContext, useCallback, useContext, useEffect, useState,
} from "react";

export type TextSize = "standard" | "large" | "x-large";
export type Theme = "default" | "inverse" | "cream" | "yellow-on-black";

const SIZE_KEY = "ielts.student.size";
const THEME_KEY = "ielts.student.theme";

export type DisplaySettings = {
  size: TextSize;
  theme: Theme;
  setSize: (s: TextSize) => void;
  setTheme: (t: Theme) => void;
};

/**
 * Not exported. `DisplaySettingsProvider` below is the one legitimate caller —
 * exporting this would let a second component call it, and a second call site
 * is a second, independent copy of this state that can drift from the first
 * the moment either one's setter fires. Everything else reaches this through
 * `useDisplay()`.
 */
function useDisplaySettings(): DisplaySettings {
  const [size, setSizeState] = useState<TextSize>(
    () => (localStorage.getItem(SIZE_KEY) as TextSize | null) ?? "standard");
  const [theme, setThemeState] = useState<Theme>(
    () => (localStorage.getItem(THEME_KEY) as Theme | null) ?? "default");

  useEffect(() => {
    // `standard`/`default` clear the attribute rather than writing a value, so
    // the stylesheet's plain `:root` block is the single source of the defaults
    // and there is no second place for them to drift.
    const root = document.documentElement;
    if (size === "standard") root.removeAttribute("data-size");
    else root.setAttribute("data-size", size);
    localStorage.setItem(SIZE_KEY, size);
  }, [size]);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "default") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  return {
    size, theme,
    setSize: useCallback((s: TextSize) => setSizeState(s), []),
    setTheme: useCallback((t: Theme) => setThemeState(t), []),
  };
}

const DisplayContext = createContext<DisplaySettings | null>(null);

/**
 * Wraps the whole app — sign-in included, since the choice lives in
 * localStorage and outlives a session, not just the authenticated routes.
 */
export function DisplaySettingsProvider({ children }: { children: React.ReactNode }) {
  const settings = useDisplaySettings();
  return <DisplayContext.Provider value={settings}>{children}</DisplayContext.Provider>;
}

/**
 * The one way to read or change the setting from anywhere under the provider.
 * Throws rather than defaulting if that provider is missing — a screen
 * silently rendering the wrong theme because it forgot to mount inside it is a
 * worse failure than a crash naming exactly what was skipped.
 */
export function useDisplay(): DisplaySettings {
  const settings = useContext(DisplayContext);
  if (!settings) {
    throw new Error("useDisplay() was called outside <DisplaySettingsProvider>");
  }
  return settings;
}

/**
 * The entry point outside the exam runner, which keeps its own — imitating the
 * real client's chrome — inside `Chrome`'s top bar. Without this, the choice
 * held app-wide once `DisplaySettingsProvider` moved up, but there was still
 * only one door to CHANGE it: starting a mock. A student who wants "yellow on
 * black" should not have to sit a paper to ask for it, and one who set it
 * mid-exam should not lose the only control that changes it back the moment
 * they land on Home.
 */
export function SettingsButton() {
  const { size, theme, setSize, setTheme } = useDisplay();
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        style={{
          background: "none", color: "var(--ink)",
          border: "1px solid var(--line)", borderRadius: "0.35rem",
          padding: "0.35rem 0.8rem", font: "inherit", cursor: "pointer",
        }}
      >
        Settings
      </button>
      {open && (
        <Settings size={size} theme={theme} setSize={setSize} setTheme={setTheme}
                  onClose={() => setOpen(false)} />
      )}
    </>
  );
}

// Three steps with these exact labels, verified against the real Settings panel.
// They are zoom multipliers there — 1.0, 1.2, 1.4 — applied to the whole
// interface rather than to body copy alone, which is why `styles.css` scales the
// root font-size and every length in the sheet is in rem.
const SIZES: { value: TextSize; label: string }[] = [
  { value: "standard", label: "Standard" },
  { value: "large", label: "Large" },
  { value: "x-large", label: "Extra large" },
];

// FOUR options, which is what the real panel offers, and the group is labelled
// "Colours" in British spelling there. "Standard" is deliberately not
// black-on-white: the real engine's default is black on a pale blue-grey.
const THEMES: { value: Theme; label: string }[] = [
  { value: "default", label: "Standard" },
  { value: "inverse", label: "White on black" },
  { value: "cream", label: "Black on cream" },
  { value: "yellow-on-black", label: "Yellow on black" },
];

export function Settings({
  size, theme, setSize, setTheme, onClose,
}: DisplaySettings & { onClose: () => void }) {
  // Escape closes it. During a timed exam, a dialog that traps you because you
  // reached for the obvious key is a real cost.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Display settings"
      style={{
        position: "fixed", inset: 0, display: "grid", placeItems: "center",
        background: "rgba(0,0,0,0.45)", zIndex: 10,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--paper)", color: "var(--ink)",
          border: "1px solid var(--line)", borderRadius: "0.5rem",
          padding: "1.25rem", minWidth: "20rem",
        }}
      >
        <h2 style={{ marginTop: 0 }}>Settings</h2>
        <p className="muted" style={{ marginTop: 0 }}>
          If you wish, you can change these settings to make the test easier to
          read.
        </p>

        <fieldset style={{ border: 0, padding: 0, marginBottom: "1rem" }}>
          <legend style={{ fontWeight: 600, marginBottom: "0.4rem" }}>Text size</legend>
          {SIZES.map((s) => (
            <label key={s.value} style={{ display: "block", padding: "0.15rem 0" }}>
              <input
                type="radio" name="size" value={s.value}
                checked={size === s.value}
                onChange={() => setSize(s.value)}
              />{" "}
              {s.label}
            </label>
          ))}
        </fieldset>

        <fieldset style={{ border: 0, padding: 0, marginBottom: "1rem" }}>
          <legend style={{ fontWeight: 600, marginBottom: "0.4rem" }}>Colours</legend>
          {THEMES.map((t) => (
            <label key={t.value} style={{ display: "block", padding: "0.15rem 0" }}>
              <input
                type="radio" name="theme" value={t.value}
                checked={theme === t.value}
                onChange={() => setTheme(t.value)}
              />{" "}
              {t.label}
            </label>
          ))}
        </fieldset>

        <button type="button" className="exam-top__button" onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
