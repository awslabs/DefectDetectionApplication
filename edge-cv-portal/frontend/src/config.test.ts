/**
 * The frontend side of the RF-DETR-Seg switch (rfdetr-seg-support DD7,
 * Requirement 15.1): `config.json`'s `rfDetrSegEnabled` is read as ON when
 * absent and OFF only for an explicit `false` — the same default the backend
 * applies to a missing PORTAL_RFDETR_SEG_ENABLED.
 */
import { describe, expect, it } from 'vitest';
import { getConfig, isRfDetrSegEnabled } from './config';

describe('isRfDetrSegEnabled', () => {
  it('is on when the field is absent (an older stack writes none)', () => {
    expect(isRfDetrSegEnabled({})).toBe(true);
    expect(isRfDetrSegEnabled({ rfDetrSegEnabled: undefined })).toBe(true);
    expect(isRfDetrSegEnabled(getConfig())).toBe(true);
  });

  it('is on for an explicit true', () => {
    expect(isRfDetrSegEnabled({ rfDetrSegEnabled: true })).toBe(true);
  });

  it('is off only for an explicit false', () => {
    expect(isRfDetrSegEnabled({ rfDetrSegEnabled: false })).toBe(false);
  });

  it('tolerates a missing config object (treated as absent = on)', () => {
    expect(isRfDetrSegEnabled(null)).toBe(true);
    expect(isRfDetrSegEnabled(undefined)).toBe(true);
  });
});
