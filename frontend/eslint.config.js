import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-explicit-any": "error",
      // the access token lives in memory only
      "no-restricted-globals": [
        "error",
        { name: "localStorage", message: "Tokens and session data stay in memory." },
        { name: "sessionStorage", message: "Tokens and session data stay in memory." },
      ],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "localStorage", message: "Tokens and session data stay in memory." },
        { object: "window", property: "sessionStorage", message: "Tokens and session data stay in memory." },
        { object: "document", property: "cookie", message: "No JS-visible cookies." },
      ],
    },
  },
  // tests assert that nothing is stored
  { files: ["**/*.test.{ts,tsx}", "src/test/**"], rules: { "no-restricted-globals": "off", "no-restricted-properties": "off" } },
  { files: ["*.config.{js,ts}"], languageOptions: { globals: globals.node } },
);
