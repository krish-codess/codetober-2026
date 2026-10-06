import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { resetCachesForTests } from "./api";

afterEach(() => {
  cleanup();
  resetCachesForTests();
});
