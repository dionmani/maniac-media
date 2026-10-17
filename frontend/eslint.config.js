// ESLint is used only for the React hooks rules – tsc already covers types and unused code.
//
// Parser: Babel, not typescript-eslint. TypeScript 7 (the native compiler this project builds
// with) no longer ships the JS compiler API that typescript-eslint's parser needs, so Babel's
// TypeScript/JSX syntax plugins do the parsing instead. Parsing only – no type information.
import babelParser from "@babel/eslint-parser";
import reactHooks from "eslint-plugin-react-hooks";

const babel = (plugins) => ({
  parser: babelParser,
  parserOptions: {
    requireConfigFile: false,
    babelOptions: { babelrc: false, configFile: false, parserOpts: { plugins } },
  },
});

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  // .ts must not enable "jsx": it would misread `<T>value` type assertions as JSX
  { files: ["src/**/*.ts"], languageOptions: babel(["typescript"]) },
  { files: ["src/**/*.tsx"], languageOptions: babel(["typescript", "jsx"]) },
  {
    files: ["src/**/*.{ts,tsx}"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      // a hook after an early return crashes the component at runtime – fail CI on it
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
    },
  },
];
