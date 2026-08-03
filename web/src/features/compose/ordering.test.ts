/**
 * Ordering is where this product has already been bitten: `tests_authoring.py`
 * carried a section move that "500'd in one direction and silently left a hole
 * in the section sequence in the other". A hole is a student who cannot enter
 * the next section of a timed exam, so these are the tests that matter most in
 * the console.
 *
 * Both directions are exercised deliberately. The original defect was
 * DIRECTIONAL — correct moving one way, broken the other — which a test that
 * only ever drags downward would have passed.
 */

import { describe, expect, it } from "vitest";

import { isContiguous, reorder, sectionMoves } from "./ordering";

const at = (...xids: string[]) =>
  xids.map((xid, index) => ({ xid, position: index + 1 }));

describe("reorder", () => {
  it("moves an item down", () => {
    expect(reorder(["a", "b", "c", "d"], 0, 2)).toEqual(["b", "c", "a", "d"]);
  });

  it("moves an item up — the direction the server got wrong", () => {
    expect(reorder(["a", "b", "c", "d"], 2, 0)).toEqual(["c", "a", "b", "d"]);
  });

  it("keeps every element, so a move can never drop one", () => {
    const moved = reorder(["a", "b", "c", "d"], 3, 1);
    expect([...moved].sort()).toEqual(["a", "b", "c", "d"]);
  });

  it("leaves the source untouched", () => {
    const source = ["a", "b", "c"];
    reorder(source, 0, 2);
    expect(source).toEqual(["a", "b", "c"]);
  });

  it("is a no-op when from and to are the same", () => {
    expect(reorder(["a", "b", "c"], 1, 1)).toEqual(["a", "b", "c"]);
  });
});

describe("sectionMoves", () => {
  it("asks for nothing when the order is already right", () => {
    expect(sectionMoves(at("a", "b", "c"))).toEqual([]);
  });

  it("numbers from 1, matching the server's `position: { minimum: 1 }`", () => {
    const desired = [
      { xid: "c", position: 3 },
      { xid: "a", position: 1 },
      { xid: "b", position: 2 },
    ];
    expect(sectionMoves(desired)).toEqual([
      { xid: "c", position: 1 },
      { xid: "a", position: 2 },
      { xid: "b", position: 3 },
    ]);
  });

  it("only PATCHes what actually moved", () => {
    // Swapping the last two leaves the first alone. Sending a no-op PATCH for it
    // would be a second audit-log write for one intent.
    const desired = [
      { xid: "a", position: 1 },
      { xid: "c", position: 3 },
      { xid: "b", position: 2 },
    ];
    expect(sectionMoves(desired).map((m) => m.xid)).toEqual(["c", "b"]);
  });

  it("produces a contiguous 1..n whatever the input positions were", () => {
    // Positions arriving with a gap already — the state the historical bug left
    // behind. The repair must close it rather than preserve it.
    const damaged = [
      { xid: "a", position: 1 },
      { xid: "b", position: 3 },
      { xid: "c", position: 7 },
    ];
    expect(sectionMoves(damaged).map((m) => m.position)).toEqual([2, 3]);
    const repaired = [1, ...sectionMoves(damaged).map((m) => m.position)];
    expect(isContiguous(repaired)).toBe(true);
  });
});

describe("isContiguous", () => {
  it("accepts 1..n", () => {
    expect(isContiguous([1, 2, 3, 4])).toBe(true);
  });

  it("accepts it out of order — this is about the SET, not the sequence", () => {
    expect(isContiguous([3, 1, 4, 2])).toBe(true);
  });

  it("rejects a hole", () => {
    expect(isContiguous([1, 2, 4])).toBe(false);
  });

  it("rejects a duplicate", () => {
    expect(isContiguous([1, 2, 2])).toBe(false);
  });

  it("rejects a missing position", () => {
    expect(isContiguous([1, undefined, 3])).toBe(false);
  });

  it("rejects zero-based, which is the off-by-one that produces a hole at the top", () => {
    expect(isContiguous([0, 1, 2])).toBe(false);
  });

  it("accepts empty", () => {
    expect(isContiguous([])).toBe(true);
  });

  it("rejects a list that is ALL missing positions", () => {
    // Found by sabotage. Removing the length guard changed nothing under any
    // other input — `every` catches holes and duplicates on its own — because
    // the one case it alone decides is when the filter empties the array and
    // `[].every()` answers true. A version whose sections all came back without
    // a position is exactly the corrupt state this function exists to notice,
    // and it was the one state it would have called healthy.
    expect(isContiguous([undefined, undefined])).toBe(false);
    expect(isContiguous([undefined])).toBe(false);
  });
});
