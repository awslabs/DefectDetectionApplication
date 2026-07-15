/**
 * Minimal ZIP writer (custom-node-designer).
 *
 * Builds an uncompressed (STORE) ZIP archive from a scaffold file map
 * for the create wizard's "download scaffold" action (Requirement 1.5).
 * Dependency-free: local file headers + central directory + end record
 * per the PKZIP application note. Scaffolds are a handful of small text
 * files, so no compression is warranted.
 */

const textEncoder = new TextEncoder();

// ------------------------------------------------------------------ CRC-32

const CRC_TABLE: Uint32Array = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

/** Standard CRC-32 (IEEE 802.3) over `data`. */
export function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < data.length; i++) {
    crc = CRC_TABLE[(crc ^ data[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

// -------------------------------------------------------------- structures

function writeUint16(view: DataView, offset: number, value: number): void {
  view.setUint16(offset, value & 0xffff, true);
}

function writeUint32(view: DataView, offset: number, value: number): void {
  view.setUint32(offset, value >>> 0, true);
}

interface ZipEntry {
  nameBytes: Uint8Array;
  data: Uint8Array;
  crc: number;
  offset: number;
}

/**
 * Build a STORE-method ZIP archive from `{path: content}`. Paths are
 * stored as-is (forward-slash relative paths); entries are emitted in
 * sorted path order so output is deterministic.
 */
export function buildZip(files: Record<string, string>): Uint8Array {
  const entries: ZipEntry[] = [];
  const chunks: Uint8Array[] = [];
  let offset = 0;

  for (const path of Object.keys(files).sort()) {
    const nameBytes = textEncoder.encode(path);
    const data = textEncoder.encode(files[path]);
    const crc = crc32(data);

    // Local file header (30 bytes) + name + data.
    const header = new Uint8Array(30 + nameBytes.length);
    const view = new DataView(header.buffer);
    writeUint32(view, 0, 0x04034b50); // local file header signature
    writeUint16(view, 4, 20); // version needed
    writeUint16(view, 6, 0x0800); // general purpose flags: UTF-8 names
    writeUint16(view, 8, 0); // method: STORE
    writeUint16(view, 10, 0); // mod time
    writeUint16(view, 12, 0x21); // mod date (1980-01-01)
    writeUint32(view, 14, crc);
    writeUint32(view, 18, data.length); // compressed size
    writeUint32(view, 22, data.length); // uncompressed size
    writeUint16(view, 26, nameBytes.length);
    writeUint16(view, 28, 0); // extra field length
    header.set(nameBytes, 30);

    entries.push({ nameBytes, data, crc, offset });
    chunks.push(header, data);
    offset += header.length + data.length;
  }

  const centralStart = offset;
  for (const entry of entries) {
    // Central directory file header (46 bytes) + name.
    const header = new Uint8Array(46 + entry.nameBytes.length);
    const view = new DataView(header.buffer);
    writeUint32(view, 0, 0x02014b50); // central directory signature
    writeUint16(view, 4, 20); // version made by
    writeUint16(view, 6, 20); // version needed
    writeUint16(view, 8, 0x0800); // flags: UTF-8 names
    writeUint16(view, 10, 0); // method: STORE
    writeUint16(view, 12, 0); // mod time
    writeUint16(view, 14, 0x21); // mod date
    writeUint32(view, 16, entry.crc);
    writeUint32(view, 20, entry.data.length);
    writeUint32(view, 24, entry.data.length);
    writeUint16(view, 28, entry.nameBytes.length);
    // extra/comment/disk/attrs stay zero
    writeUint32(view, 42, entry.offset); // local header offset
    header.set(entry.nameBytes, 46);
    chunks.push(header);
    offset += header.length;
  }

  // End of central directory record (22 bytes).
  const eocd = new Uint8Array(22);
  const eocdView = new DataView(eocd.buffer);
  writeUint32(eocdView, 0, 0x06054b50);
  writeUint16(eocdView, 8, entries.length); // entries on this disk
  writeUint16(eocdView, 10, entries.length); // total entries
  writeUint32(eocdView, 12, offset - centralStart); // central dir size
  writeUint32(eocdView, 16, centralStart); // central dir offset
  chunks.push(eocd);

  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const archive = new Uint8Array(total);
  let position = 0;
  for (const chunk of chunks) {
    archive.set(chunk, position);
    position += chunk.length;
  }
  return archive;
}

/** Trigger a browser download of `files` as `<name>.zip`. */
export function downloadZip(files: Record<string, string>, name: string): void {
  const archive = buildZip(files);
  const blob = new Blob([archive.buffer as ArrayBuffer], { type: 'application/zip' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${name}.zip`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}
