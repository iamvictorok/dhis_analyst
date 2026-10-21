/**
 * Report editor — Tiptap core mounted in a Preact component.
 *
 * onChange(html | null): called with the editor's current HTML after the user
 * edits the report, and with null while the document equals the report as
 * generated (nothing edited, or edits undone). The parent uses this to send the
 * edited text to the PDF/DOCX exports, and the original HTML when unedited.
 */
import { useEffect, useRef } from 'preact/hooks';
import { Editor } from '@tiptap/core';
import { StarterKit } from '@tiptap/starter-kit';
import { Table } from '@tiptap/extension-table';
import { TableRow } from '@tiptap/extension-table-row';
import { TableCell } from '@tiptap/extension-table-cell';
import { TableHeader } from '@tiptap/extension-table-header';
import { editedHtmlOrNull } from '../lib/reportExport.js';

export default function ReportEditor({ html, onChange }) {
  const editorRef = useRef(null);
  const containerRef = useRef(null);
  // Latest onChange without re-creating the editor on every render.
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  // Document as loaded from the generated report; edits are measured against it.
  const baselineRef = useRef(null);
  // True while we load content ourselves, so that load is not taken as an edit.
  const syncingRef = useRef(false);

  useEffect(() => {
    if (!containerRef.current) return;

    syncingRef.current = true;
    const editor = new Editor({
      element: containerRef.current,
      extensions: [
        StarterKit,
        Table.configure({ resizable: true }),
        TableRow,
        TableCell,
        TableHeader,
      ],
      content: html || '<p>Report will appear here…</p>',
      editorProps: {
        attributes: {
          class: 'editor-area',
        },
      },
      onUpdate: ({ editor }) => {
        if (syncingRef.current) return;
        const cb = onChangeRef.current;
        if (!cb) return;
        cb(editedHtmlOrNull(
          baselineRef.current,
          JSON.stringify(editor.getJSON()),
          editor.getHTML(),
        ));
      },
    });

    baselineRef.current = JSON.stringify(editor.getJSON());
    syncingRef.current = false;
    editorRef.current = editor;

    return () => {
      editor.destroy();
    };
  }, []);

  // Update content when html prop changes externally (new report from SSE).
  // A newly loaded report is a fresh baseline: nothing edited yet.
  useEffect(() => {
    const editor = editorRef.current;
    if (editor && html && editor.getHTML() !== html) {
      syncingRef.current = true;
      try {
        editor.commands.setContent(html, { emitUpdate: false });
        baselineRef.current = JSON.stringify(editor.getJSON());
      } finally {
        syncingRef.current = false;
      }
      if (onChangeRef.current) onChangeRef.current(null);
    }
  }, [html]);

  return (
    <div
      ref={containerRef}
      id="report-editor"
      style="min-height:200px"
    />
  );
}
