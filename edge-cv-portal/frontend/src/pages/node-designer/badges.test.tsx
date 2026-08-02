/**
 * Unit tests for the Node_Designer badge mappings (custom-node-designer
 * task 12.1, Requirements 3.5, 15.1).
 */
import { describe, expect, it } from 'vitest';
import {
  buildStatusType,
  classificationBadgeColor,
  lifecycleBadgeColor,
  logExcerpt,
} from './badges';

describe('lifecycleBadgeColor', () => {
  it('maps each Lifecycle_State to a distinct badge color', () => {
    expect(lifecycleBadgeColor('dev')).toBe('grey');
    expect(lifecycleBadgeColor('test')).toBe('blue');
    expect(lifecycleBadgeColor('prod')).toBe('green');
    expect(lifecycleBadgeColor(undefined)).toBe('grey');
  });
});

describe('classificationBadgeColor', () => {
  it('maps each classification value to a risk color (15.1)', () => {
    const colors = ['good', 'bad', 'ugly', 'unclassified'].map(classificationBadgeColor);
    expect(colors[0]).toBe('green');
    // Each classification renders a distinct color.
    expect(new Set(colors).size).toBe(4);
  });
});

describe('buildStatusType', () => {
  it('maps build statuses to StatusIndicator types (3.5)', () => {
    expect(buildStatusType('succeeded')).toBe('success');
    expect(buildStatusType('failed')).toBe('error');
    expect(buildStatusType('building')).toBe('in-progress');
    expect(buildStatusType('queued')).toBe('pending');
    expect(buildStatusType(null)).toBe('stopped');
  });
});

describe('logExcerpt', () => {
  it('keeps the last lines of a long log tail', () => {
    const tail = Array.from({ length: 30 }, (_, i) => `line ${i}`).join('\n');
    const excerpt = logExcerpt(tail, 5);
    expect(excerpt.split('\n')).toEqual([
      'line 25',
      'line 26',
      'line 27',
      'line 28',
      'line 29',
    ]);
  });

  it('drops blank lines and handles empty input', () => {
    expect(logExcerpt('')).toBe('');
    expect(logExcerpt(null)).toBe('');
    expect(logExcerpt('a\n\n\nb')).toBe('a\nb');
  });
});
