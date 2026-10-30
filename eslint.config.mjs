// @ts-check
import js from "@eslint/js";
import eslintConfigPrettier from "eslint-config-prettier/flat";
import eslintPluginAstro from "eslint-plugin-astro";
import globals from "globals";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

export default defineConfig([
  {
    name: "ignores",
    ignores: ["dist/**", ".astro/**", "node_modules/**"],
  },

  {
    name: "node-config-files",
    files: ["*.config.{js,mjs,ts}"],
    languageOptions: {
      globals: globals.node,
    },
  },

  js.configs.recommended,
  ...tseslint.configs["recommended"],

  ...eslintPluginAstro.configs["recommended"],
  ...eslintPluginAstro.configs["flat/jsx-a11y-strict"],

  eslintConfigPrettier,
]);
