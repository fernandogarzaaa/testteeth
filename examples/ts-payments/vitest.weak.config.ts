import { defineConfig } from "vitest/config";

export default defineConfig({ test: { include: ["tests_weak/**/*.test.ts"] } });
