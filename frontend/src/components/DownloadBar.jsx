/**
 * Download bar — DOCX / PDF / PPTX / XLSX / CSV buttons.
 * POSTs the current report HTML or slide manifest to backend generator endpoints.
 */
import { useState, useCallback } from 'preact/hooks';
import { getAuth } from '../auth/standaloneAuth.js';

const FORMATS = [
  { label: 'DOCX', endpoint: '/api/generate/docx', key: 'report' },
  { label: 'PDF', endpoint: '/api/generate/pdf', key: 'report' },
  { label: 'PPTX', endpoint: '/api/generate/pptx', key: 'slides' },
  { label: 'XLSX', endpoint: '/api/export/xlsx', key: 'export' },
  { label: 'CSV', endpoint: '/api/export/csv', key: 'export' },
];

export default function DownloadBar({ reportHtml, slides, exportData }) {
  const [busy, setBusy] = useState('');

  const handleDownload = useCallback(async (fmt) => {
    setBusy(fmt.label);
    try {
      let body;
      if (fmt.key === 'report') {
        body = JSON.stringify({ html: reportHtml || '<p>No report content</p>', title: 'DHIS2 Report' });
      } else if (fmt.key === 'slides') {
        body = JSON.stringify({ slides: slides || [], title: 'DHIS2 Briefing' });
      } else {
        body = JSON.stringify(exportData || { rows: [], headers: [] });
      }

      const auth = getAuth();
      const headers = { 'Content-Type': 'application/json' };
      if (auth?.token) headers['Authorization'] = `Bearer ${auth.token}`;

      const res = await fetch(fmt.endpoint, { method: 'POST', headers, body });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);

      const data = await res.json();
      if (data.file_id) {
        // Fetch the file with auth headers to avoid 401
        const downloadHeaders = {};
        if (auth?.token) downloadHeaders['Authorization'] = `Bearer ${auth.token}`;

        const downloadRes = await fetch(`/api/download/${data.file_id}`, { headers: downloadHeaders });
        if (!downloadRes.ok) throw new Error(`Download failed: ${downloadRes.status} ${downloadRes.statusText}`);

        const blob = await downloadRes.blob();
        const url = window.URL.createObjectURL(blob);

        const link = document.createElement('a');
        link.href = url;
        link.download = data.filename || `${data.file_id}.${fmt.label.toLowerCase()}`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.URL.revokeObjectURL(url);
      }
    } catch (err) {
      console.error('Download failed:', err);
    } finally {
      setBusy('');
    }
  }, [reportHtml, slides, exportData]);

  const hasContent = reportHtml || (slides && slides.length) || exportData;
  if (!hasContent) return null;

  return (
    <div class="download-bar">
      {FORMATS.map((fmt) => {
        const enabled =
          (fmt.key === 'report' && reportHtml) ||
          (fmt.key === 'slides' && slides?.length) ||
          (fmt.key === 'export' && exportData);

        return (
          <button
            key={fmt.label}
            class="btn-sm"
            disabled={!enabled || busy === fmt.label}
            onClick={() => handleDownload(fmt)}
            title={`Download as ${fmt.label}`}
          >
            {busy === fmt.label ? '…' : fmt.label}
          </button>
        );
      })}
    </div>
  );
}
