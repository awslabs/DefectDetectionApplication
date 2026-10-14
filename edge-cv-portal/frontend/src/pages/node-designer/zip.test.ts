/**
 * Unit tests for the scaffold zip writer (custom-node-designer task
 * 12.1, Requirement 1.5).
 */
import { describe, expect, it } from 'vitest';
import { buildZip, crc32 } from './zip';

const encoder = new TextEncoder();

describe('crc32', () => {
  it('matches known IEEE CRC-32 vectors', () => {
    // Standard check value for "123456789".
    expect(crc32(encoder.encode('123456789'))).toBe(0xcbf43926);
    expect(crc32(new Uint8Array(0))).toBe(0);
  });
});

describe('buildZip', () => {
  const read32 = (bytes: Uint8Array, offset: number) =>
    new DataView(bytes.buffer, bytes.byteOffset).getUint32(offset, true);
  const read16 = (bytes: Uint8Array, offset: number) =>
    new DataView(bytes.buffer, bytes.byteOffset).getUint16(offset, true);

  it('produces a well-formed archive with one entry per file', () => {
    const files = {
      'README.md': '# hello\n',
      'plugin/frame_processing_hook.py': 'def process_frame(frame, params):\n    return frame\n',
    };
    const archive = buildZip(files);

    // Starts with a local file header signature.
    expect(read32(archive, 0)).toBe(0x04034b50);

    // Ends with the end-of-central-directory record naming both entries.
    const eocd = archive.length - 22;
    expect(read32(archive, eocd)).toBe(0x06054b50);
    expect(read16(archive, eocd + 10)).toBe(2);

    // The central directory offset points at a central directory header.
    const centralOffset = read32(archive, eocd + 16);
    expect(read32(archive, centralOffset)).toBe(0x02014b50);
  });

  it('stores content uncompressed with a matching CRC', () => {
    const content = 'meson build configuration';
    const archive = buildZip({ 'builds/x86_64/meson.build': content });

    // Local header: method STORE (0), CRC and sizes of the content.
    expect(read16(archive, 8)).toBe(0);
    expect(read32(archive, 14)).toBe(crc32(encoder.encode(content)));
    expect(read32(archive, 18)).toBe(content.length);
    expect(read32(archive, 22)).toBe(content.length);

    // The stored bytes follow the header + name verbatim.
    const nameLength = read16(archive, 26);
    const data = archive.slice(30 + nameLength, 30 + nameLength + content.length);
    expect(new TextDecoder().decode(data)).toBe(content);
  });

  it('is deterministic across key insertion order', () => {
    const a = buildZip({ 'a.txt': '1', 'b.txt': '2' });
    const b = buildZip({ 'b.txt': '2', 'a.txt': '1' });
    expect(a).toEqual(b);
  });
});
