/**
 * Minimal, dependency-free HTML sanitizer.
 *
 * Assistant markdown is rendered via marked + dangerouslySetInnerHTML, and its
 * content is conditioned on web-enrichment (attacker-influenceable) text. marked
 * passes raw inline HTML through untouched, so a prompt-injected page could get
 * `<script>` or `<img onerror=...>` echoed into the answer and executed in the
 * app origin (where the JWT lives). This strips the dangerous surface using the
 * browser's own parser rather than fragile regexes.
 */

const BLOCKED_TAGS = new Set([
  'SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE', 'FORM',
]);
const URL_ATTRS = new Set(['href', 'src', 'xlink:href', 'action', 'formaction']);

export function sanitizeHtml(dirty) {
  if (typeof dirty !== 'string' || dirty === '') return '';
  // No DOM available (SSR/tests) — fall back to escaping everything.
  if (typeof DOMParser === 'undefined') {
    return dirty.replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  const doc = new DOMParser().parseFromString(dirty, 'text/html');

  const walk = (node) => {
    for (const el of [...node.children]) {
      if (BLOCKED_TAGS.has(el.tagName)) {
        el.remove();
        continue;
      }
      for (const attr of [...el.attributes]) {
        const name = attr.name.toLowerCase();
        const value = attr.value || '';
        if (name.startsWith('on') || name === 'srcdoc') {
          el.removeAttribute(attr.name);
        } else if (URL_ATTRS.has(name) && /^\s*(javascript|data|vbscript):/i.test(value)) {
          el.removeAttribute(attr.name);
        } else if (name === 'style' && /expression\s*\(|javascript:|url\s*\(/i.test(value)) {
          el.removeAttribute(attr.name);
        }
      }
      walk(el);
    }
  };

  walk(doc.body);
  return doc.body.innerHTML;
}
