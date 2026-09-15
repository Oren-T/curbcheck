// Flat config for the plain-ES-module frontend in web/. Written as CommonJS
// because the repo has no package.json, so Node reads a bare .js file as CJS.
// Nothing here is installed: node_modules stays empty and the frontend libs
// are vendored (web/vendor/MANIFEST.md). Editors and CI supply their own ESLint.

const browserGlobals = {
  AbortController: "readonly",
  Blob: "readonly",
  CSS: "readonly",
  CustomEvent: "readonly",
  Document: "readonly",
  Element: "readonly",
  Event: "readonly",
  FormData: "readonly",
  Headers: "readonly",
  HTMLElement: "readonly",
  Intl: "readonly",
  Request: "readonly",
  Response: "readonly",
  URL: "readonly",
  URLSearchParams: "readonly",
  Worker: "readonly",
  console: "readonly",
  customElements: "readonly",
  document: "readonly",
  fetch: "readonly",
  localStorage: "readonly",
  location: "readonly",
  navigator: "readonly",
  requestAnimationFrame: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  setInterval: "readonly",
  clearInterval: "readonly",
  structuredClone: "readonly",
  window: "readonly",
  // Defined by web/vendor/pmtiles/pmtiles.js, which is a classic script.
  pmtiles: "readonly",
};

module.exports = [
  {
    ignores: ["web/vendor/**", "data/**"],
  },
  {
    files: ["web/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: browserGlobals,
    },
    linterOptions: {
      reportUnusedDisableDirectives: "error",
    },
    rules: {
      // STYLE_GUIDE §4: sign text is untrusted and must never reach innerHTML.
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Use textContent or build DOM nodes; sign text is untrusted." },
        { property: "outerHTML", message: "Use textContent or build DOM nodes; sign text is untrusted." },
      ],
      // SPEC §3.3 and §3.4: no code built from data, no remote code.
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-script-url": "error",

      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "no-undef": "error",
      "no-var": "error",
      "prefer-const": "error",
      eqeqeq: ["error", "always"],
      curly: ["error", "all"],
    },
  },
];
