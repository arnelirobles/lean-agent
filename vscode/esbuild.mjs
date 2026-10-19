import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";

const production = process.argv.includes("--production");
mkdirSync("out", { recursive: true });
copyFileSync("node_modules/sql.js/dist/sql-wasm.wasm", "out/sql-wasm.wasm");
mkdirSync("out/codicons", { recursive: true });
for (const f of ["codicon.css", "codicon.ttf"]) copyFileSync(`node_modules/@vscode/codicons/dist/${f}`, `out/codicons/${f}`);
await build({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node20",
  outfile: "out/extension.js",
  external: ["vscode"],
  sourcemap: !production,
  minify: production,
  logLevel: "info",
});
