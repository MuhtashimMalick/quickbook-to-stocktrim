import "@testing-library/jest-dom/vitest";
import { cleanup, configure } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";
import { setToken } from "../api/session";
import { mockMotion } from "./motion";
import { server, unhandled } from "./server";

configure({ asyncUtilTimeout: 8000 }); // lazy route chunks are slow on a cold run
beforeAll(() => server.listen({
    onUnhandledFrame: ({ defaults }) => {
      unhandled.push("unhandled request");
      defaults.error();
    },
  }));
afterEach(async () => {
  cleanup();
  await new Promise((r) => setTimeout(r, 40)); // let requests fired just before unmount finish against their handlers
  const missed = unhandled.splice(0).length;
  server.resetHandlers();
  setToken(null);
  localStorage.clear();
  sessionStorage.clear();
  if (missed > 0) throw new Error(`${missed} request(s) had no MSW handler`);
});
afterAll(() => server.close());
beforeEach(() => mockMotion("reduce")); // tests default to the reduced-motion (final state) path
