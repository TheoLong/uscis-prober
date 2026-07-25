// Copyright (C) 2026 the USCIS Prober contributors
// SPDX-License-Identifier: AGPL-3.0-or-later
// Harness for the row-key parity contract (tests/test_row_key_parity.py).
// Loads rowKeyOf from src/static/app.js in a vm sandbox, applies it to every
// event in tests/fixtures/row_key_cases.json, and prints the resulting keys as
// a JSON array on stdout. The Python side computes the same list with
// event_links._row_key and asserts byte-for-byte equality — so the two
// implementations can never silently drift.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadApp } from "./_appsandbox.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  fs.readFileSync(path.resolve(here, "../fixtures/row_key_cases.json"), "utf8"),
);
const { T } = loadApp(["rowKeyOf"]);
const keys = fixture.cases.map((ev) => T.rowKeyOf(ev));
process.stdout.write(JSON.stringify(keys));
