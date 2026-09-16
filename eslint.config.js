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

// The static site's worker (docs/STATIC_SITE.md) runs the engine off the main
// thread: no DOM, but the worker scope and the streams it inflates the pack with.
const workerGlobals = {
  self: "readonly",
  postMessage: "readonly",
  onmessage: "writable",
  fetch: "readonly",
  Response: "readonly",
  DecompressionStream: "readonly",
  TextDecoder: "readonly",
  URL: "readonly",
  Intl: "readonly",
  console: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  structuredClone: "readonly",
  Worker: "readonly",
  AbortController: "readonly",
};

// `node --test` files: the Node globals the tests and the harness helpers use.
const nodeGlobals = {
  process: "readonly",
  Buffer: "readonly",
  console: "readonly",
  URL: "readonly",
  Intl: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  structuredClone: "readonly",
  TextDecoder: "readonly",
  DecompressionStream: "readonly",
  Response: "readonly",
};

const sharedRules = {
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
};

module.exports = [
  {
    ignores: ["web/vendor/**", "data/**", "build/**", "dist/**", "site/tests/fixtures/**"],
  },
  {
    files: ["site/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...browserGlobals, ...workerGlobals },
    },
    linterOptions: {
      reportUnusedDisableDirectives: "error",
    },
    rules: sharedRules,
  },
  {
    files: ["site/tests/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: nodeGlobals,
    },
    linterOptions: {
      reportUnusedDisableDirectives: "error",
    },
    rules: sharedRules,
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
    rules: sharedRules,
  },
];
