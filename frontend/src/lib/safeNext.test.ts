import { describe, expect, it } from "vitest";
import { safeNext } from "./safeNext";

const backslash = String.fromCharCode(92);

describe("safeNext", () => {
  it.each(["https://evil.example", "//evil.example", `/${backslash}evil.example`, "javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x", "evil", "", "/login", "/login?next=/x", "	//evil", " /evil"])(
    "rejects %j",
    (next) => {
      expect(safeNext(next)).toBe("/");
    },
  );
  it("accepts a same-origin relative path with query and hash", () => {
    expect(safeNext("/runs?status=failed#top")).toBe("/runs?status=failed#top");
  });
  it("keeps encoded slashes and backslashes as literal same-origin paths", () => {
    expect(safeNext("/%5Cevil")).toBe("/%5Cevil");
    expect(safeNext("/%2F%2Fevil.example")).toBe("/%2F%2Fevil.example");
  });
  it("falls back when missing", () => {
    expect(safeNext(null)).toBe("/");
  });
});
