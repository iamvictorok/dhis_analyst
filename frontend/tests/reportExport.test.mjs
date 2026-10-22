// Run with: npm test  (node's built-in test runner; no extra dependencies)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  reportHtmlForExport,
  editedHtmlOrNull,
  isBlankHtml,
  stripTrailingEmptyParagraphs,
} from '../src/lib/reportExport.js';

const ORIGINAL = '<h1>ANC 1 Coverage Review</h1>\n<table><thead><tr><th>Period</th></tr></thead><tbody><tr><td>202501</td></tr></tbody></table>';

test('unedited report exports the original HTML unchanged', () => {
  assert.equal(reportHtmlForExport(ORIGINAL, null), ORIGINAL);
  assert.equal(reportHtmlForExport(ORIGINAL, undefined), ORIGINAL);
  assert.equal(reportHtmlForExport(ORIGINAL, ''), ORIGINAL);
});

test('edited report exports the edited HTML', () => {
  const edited = '<h1>My new title</h1><p>Added sentence.</p>';
  assert.equal(reportHtmlForExport(ORIGINAL, edited), edited);
});

test('a fully cleared editor still exports a valid (non-empty) body', () => {
  assert.equal(reportHtmlForExport(ORIGINAL, '<p></p>'), '<p></p>');
  assert.equal(reportHtmlForExport(ORIGINAL, '<p>&nbsp;</p>'), '<p></p>');
});

test('missing original with no edits gives an empty string', () => {
  assert.equal(reportHtmlForExport(undefined, null), '');
});

test('editor reports null when the document equals its baseline (incl. edit then undo)', () => {
  const base = JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] });
  assert.equal(editedHtmlOrNull(base, base, '<p></p>'), null);
});

test('editor reports the HTML once the document differs from its baseline', () => {
  const base = JSON.stringify({ type: 'doc', content: [] });
  const now = JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] });
  assert.equal(editedHtmlOrNull(base, now, '<p>x</p>'), '<p>x</p>');
});

test('isBlankHtml', () => {
  assert.equal(isBlankHtml('<p></p>'), true);
  assert.equal(isBlankHtml('<p> &nbsp; </p>'), true);
  assert.equal(isBlankHtml('<p>a</p>'), false);
  assert.equal(isBlankHtml(null), true);
});

test('trailing empty editor paragraphs are not exported', () => {
  const edited = '<h1>T</h1><h2>Data Sources</h2><p></p><p><br></p>';
  assert.equal(reportHtmlForExport(ORIGINAL, edited), '<h1>T</h1><h2>Data Sources</h2>');
  assert.equal(stripTrailingEmptyParagraphs('<p>a</p><p></p>'), '<p>a</p>');
  assert.equal(stripTrailingEmptyParagraphs('<p></p><p>a</p>'), '<p></p><p>a</p>');
});
