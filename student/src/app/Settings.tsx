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
 */

import { useCallback, useEffect, useState } from "react";

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

export function useDisplaySettings(): DisplaySettings {
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
