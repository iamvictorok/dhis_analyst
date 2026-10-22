/**
 * Which report HTML an export (PDF / DOCX / Copy HTML) should use.
 *
 * The report editor reports `null` while the document is unchanged and the
 * editor's current HTML once the user has edited it. Exports must use the
 * edited text; when nothing was edited they must send the ORIGINAL generated
 * HTML, so an unedited download is exactly what it was before editing existed
 * (Tiptap re-serialises HTML, e.g. it drops <thead>, so its output is not
 * byte-identical to the original even with no edits).
 *
 * Pure functions, no DOM: unit-tested with `node --test`.
 */

/** True when the editor HTML holds no visible text (e.g. "<p></p>"). */
export function isBlankHtml(html) {
  if (typeof html !== 'string') return true;
  const text = html
    .replace(/<[^>]*>/g, ' ')
    .replace(/&nbsp;|&#160;/gi, ' ')
    .trim();
  return text === '';
}

/**
 * Drop empty paragraphs at the very end. The editor keeps an empty trailing
 * paragraph after a final heading/table (so the cursor can go there); it is
 * not content and would add a blank line to the DOCX.
 */
export function stripTrailingEmptyParagraphs(html) {
  return html.replace(/(?:<p>(?:\s|&nbsp;|<br\s*\/?>)*<\/p>\s*)+$/i, '');
}

/**
 * @param {string} originalHtml  the generated report (from the SSE stream)
 * @param {string|null|undefined} editedHtml  the editor's HTML, or null if unedited
 * @returns {string} the HTML to export
 */
export function reportHtmlForExport(originalHtml, editedHtml) {
  if (typeof editedHtml === 'string' && editedHtml !== '') {
    // An editor emptied by the user still exports something valid; the
    // backend rejects an empty body.
    if (isBlankHtml(editedHtml)) return '<p></p>';
    return stripTrailingEmptyParagraphs(editedHtml);
  }
  return originalHtml || '';
}

/**
 * Decide what the editor should report upward after a change.
 * Returns null when the document is back to its baseline (unedited, including
 * edit-then-undo), otherwise the current HTML.
 *
 * @param {string} baselineJson  JSON.stringify(editor.getJSON()) right after load
 * @param {string} currentJson   JSON.stringify(editor.getJSON()) now
 * @param {string} currentHtml   editor.getHTML() now
 */
export function editedHtmlOrNull(baselineJson, currentJson, currentHtml) {
  if (baselineJson != null && baselineJson === currentJson) return null;
  return currentHtml;
}
