/**
 * The chrome shared by Home, Result and Review — everything App.tsx calls the
 * "drill surface", as opposed to the exam runner.
 *
 * One line of purpose: put the Settings entry point somewhere on all three,
 * in the same place, without touching any of their own markup. Each already
 * renders its own `<main className="page">`; this sits above it via
 * react-router's layout-route pattern (`<Route element={<DrillLayout/>}>`
 * wrapping the three as children, rendered through `<Outlet/>`), rather than
 * three copies of the same button pasted into three files that would drift
 * the next time one of them changes.
 */

import { Outlet } from "react-router-dom";

import { SettingsButton } from "./Settings";

export function DrillLayout() {
  return (
    <>
      <div
        style={{
          display: "flex", justifyContent: "flex-end",
          maxWidth: "60rem", margin: "0 auto", padding: "1.25rem 1.25rem 0",
        }}
      >
        <SettingsButton />
      </div>
      <Outlet />
    </>
  );
}
