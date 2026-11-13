// Build gate (ADR-0022): every language file must carry every key of the
// German lead file. Missing keys fail the build - a language is complete
// or it is not shipped as complete.
import { readFileSync } from "node:fs";
const dir = new URL("../src/i18n/", import.meta.url);
const load = (l) => JSON.parse(readFileSync(new URL(`${l}.json`, dir), "utf8"));
const keys = (o, p = "") => Object.entries(o).flatMap(([k, v]) => (v && typeof v === "object" ? keys(v, `${p}${k}.`) : [`${p}${k}`]));
const lead = new Set(keys(load("de")));
let failed = false;
for (const lang of ["fr", "it", "en"]) {
  const have = new Set(keys(load(lang)));
  const missing = [...lead].filter((k) => !have.has(k));
  const extra = [...have].filter((k) => !lead.has(k));
  if (missing.length || extra.length) {
    failed = true;
    console.error(`[i18n] ${lang}: missing ${missing.join(", ") || "-"}; extra ${extra.join(", ") || "-"}`);
  }
}
// Every literal key used in the sources must exist (dynamic keys built
// with template strings are not checked - they are few and deliberate).
import { readdirSync, statSync } from "node:fs";
const srcDir = new URL("../src/", import.meta.url);
const walk = (d) =>
  readdirSync(d).flatMap((n) => {
    const u = new URL(n, d);
    return statSync(u).isDirectory() ? walk(new URL(`${n}/`, d)) : n.endsWith(".tsx") || n.endsWith(".ts") ? [u] : [];
  });
const used = new Set();
for (const file of walk(srcDir)) {
  for (const m of readFileSync(file, "utf8").matchAll(/\bt\(\s*"([^"]+)"/g)) used.add(m[1]);
}
const unknown = [...used].filter((k) => !lead.has(k));
if (unknown.length) {
  failed = true;
  console.error(`[i18n] keys used but not defined in de.json: ${unknown.join(", ")}`);
}
if (failed) process.exit(1);
console.log(`[i18n] ${lead.size} keys, de/fr/it/en complete, ${used.size} literal keys used`);
