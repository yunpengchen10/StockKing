import { readFileSync, writeFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
const quotes = readFileSync(new URL('./quotes.ts', import.meta.url), 'utf8');
const worker = readFileSync(new URL('./worker.ts', import.meta.url), 'utf8').replace(/^import .*;\r?\n/, '');
writeFileSync(new URL('./worker.mjs', import.meta.url), stripTypeScriptTypes(quotes + '\n' + worker));
