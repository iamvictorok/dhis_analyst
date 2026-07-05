/**
 * CopyButton — inline copy-to-clipboard with visual feedback.
 * Uses navigator.clipboard with execCommand fallback.
 */
import { useState, useCallback } from 'preact/hooks';

export default function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [text]);

  return (
    <button
      class={`btn-sm copy-btn ${copied ? 'copy-btn--copied' : ''}`}
      onClick={handleCopy}
      title={copied ? 'Copied!' : label}
    >
      {copied ? '✓' : '⎘'}
    </button>
  );
}
