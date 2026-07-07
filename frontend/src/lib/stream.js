/**
 * SSE stream reader — parses Server-Sent Events from a fetch Response.
 * Handles all 8 event types defined in the spec.
 */

/**
 * Read an SSE stream from a fetch Response and invoke a callback per event.
 *
 * @param {Response} response - Fetch response with SSE body
 * @param {(type: string, data: any) => void} onEvent - Callback per event
 */
export async function readSseStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  // Parser state MUST persist across reads. An SSE block spans multiple lines
  // (`event:`, `data:`, blank); network/proxy chunking can split a block across
  // two reader.read() calls. Declaring these inside the loop reset them on every
  // chunk, silently dropping any event whose block crossed a read boundary —
  // large payloads (reports, charts) failed intermittently for exactly this.
  let currentEvent = '';
  const dataLines = [];

  const dispatch = () => {
    if (!currentEvent && dataLines.length === 0) return false;
    const rawData = dataLines.join('\n');
    let payload = rawData;
    try {
      payload = JSON.parse(rawData);
    } catch {
      payload = rawData;
    }
    onEvent(currentEvent, payload);
    const wasDone = currentEvent === 'done';
    currentEvent = '';
    dataLines.length = 0;
    return wasDone;
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const rawLine of lines) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (line.startsWith('event:')) {
        currentEvent = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        // Per the SSE spec, accumulate multiple data: lines (one leading space
        // after the colon is stripped) instead of overwriting.
        dataLines.push(line.slice(5).replace(/^ /, ''));
      } else if (line === '') {
        // Blank line terminates the current SSE block.
        if (dispatch()) {
          reader.cancel();
          return;
        }
      }
    }
  }

  // Flush any trailing block that arrived without a final blank line.
  if (dispatch()) {
    reader.cancel();
  }
}
