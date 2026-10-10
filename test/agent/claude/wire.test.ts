/** Reading a wire value as `unknown`: each helper gives the value in its type, or null. */
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  integer,
  isRecord,
  number,
  records,
  string,
  strip,
  words,
} from "../../../src/agent/claude/wire.ts";

test("a record is an object that is no list and not null", () => {
  assert.equal(isRecord({}), true);
  assert.equal(isRecord({ type: "user" }), true);
  for (const value of [null, undefined, [], [{}], "text", 7, true]) {
    assert.equal(isRecord(value), false, JSON.stringify(value));
  }
});

test("a string is read as a string and anything else as none", () => {
  assert.equal(string("ok"), "ok");
  assert.equal(string(""), "");
  for (const value of [null, undefined, 7, true, {}, ["ok"]]) assert.equal(string(value), null);
});

test("words are a string that says something", () => {
  assert.equal(words("ok"), "ok");
  assert.equal(words(" "), " ");
  for (const value of ["", null, undefined, 7, {}]) assert.equal(words(value), null);
});

test("a whole number is read as one and a fraction as none", () => {
  assert.equal(integer(20742), 20742);
  assert.equal(integer(0), 0);
  for (const value of [3.5, "3", null, undefined, Number.NaN, Number.POSITIVE_INFINITY, true]) {
    assert.equal(integer(value), null);
  }
});

test("a number is read as one, a fraction included", () => {
  assert.equal(number(4886), 4886);
  assert.equal(number(0.5), 0.5);
  for (const value of ["4886", null, undefined, Number.NaN, Number.POSITIVE_INFINITY, false]) {
    assert.equal(number(value), null);
  }
});

test("the records of a list are its objects, in order", () => {
  assert.deepEqual(records([{ a: 1 }, "text", null, [{ b: 2 }], { c: 3 }]), [{ a: 1 }, { c: 3 }]);
  for (const value of [null, undefined, "list", { a: 1 }]) assert.deepEqual(records(value), []);
});

test("a text is cut of the white space at its ends as Python cuts it", () => {
  assert.equal(strip("  ok \n\t"), "ok");
  assert.equal(strip("a  b"), "a  b");
  assert.equal(strip(""), "");
  assert.equal(strip(" \n "), "");
  // Every character `str.isspace()` holds true for (CPython 3.12), and no other.
  const python = [
    0x9, 0xa, 0xb, 0xc, 0xd, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001,
    0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f,
    0x205f, 0x3000,
  ];
  for (const code of python) {
    const space = String.fromCodePoint(code);
    assert.equal(strip(`${space}ok${space}`), "ok", code.toString(16));
  }
  // White space to JavaScript's `trim` and not to Python.
  assert.equal(strip("﻿ok﻿"), "﻿ok﻿");
  // White space to neither.
  assert.equal(strip("​ok​"), "​ok​");
});
