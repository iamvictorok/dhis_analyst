/**
 * Report editor — Tiptap core mounted in a Preact component.
 * Provides getHTML() for the download bar and live editing.
 */
import { useEffect, useRef, useCallback } from 'preact/hooks';
import { Editor } from '@tiptap/core';
import { StarterKit } from '@tiptap/starter-kit';
import { Table } from '@tiptap/extension-table';
import { TableRow } from '@tiptap/extension-table-row';
import { TableCell } from '@tiptap/extension-table-cell';
import { TableHeader } from '@tiptap/extension-table-header';

export default function ReportEditor({ html, onChange }) {
  const editorRef = useRef(null);
  const containerRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current) return;

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
        if (onChange) {
          onChange(editor.getHTML());
        }
      },
    });

    editorRef.current = editor;

    return () => {
      editor.destroy();
    };
  }, []);

  // Update content when html prop changes externally (new report from SSE)
  useEffect(() => {
    const editor = editorRef.current;
    if (editor && html && editor.getHTML() !== html) {
      editor.commands.setContent(html, false);
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
