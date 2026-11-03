/**
 * P12: the Markdown renderer never emits an element outside the allow-list
 * and lets only Portal routes and guide links survive as anchors (spec
 * portal-ai-assistant Requirement 3.7, 3.8).
 */
import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import fc from 'fast-check';
import MessageMarkdown, { ALLOWED_ELEMENTS } from './MessageMarkdown';

vi.mock('../../config', () => ({
  getConfig: () => ({ branding: { documentationUrl: 'https://docs.example.com/guide' } }),
  getBuildInfo: () => ({ version: 'test', gitSha: 'abc', buildTime: '' }),
}));

function renderMarkdown(text: string) {
  return render(
    <MemoryRouter>
      <MessageMarkdown text={text} />
    </MemoryRouter>
  );
}

const ALLOWED = new Set(ALLOWED_ELEMENTS.map((e) => e.toUpperCase()).concat(['SPAN']));

describe('MessageMarkdown', () => {
  it('renders raw HTML as nothing and keeps text', () => {
    const { container } = renderMarkdown('Hello <img src=x onerror=alert(1)> <script>alert(1)</script> world');
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(container.textContent).toContain('Hello');
    expect(container.textContent).toContain('world');
  });

  it('routes Portal links in-app, opens guide links externally, and drops other links', () => {
    const { container } = renderMarkdown(
      '[Devices](/devices) and [§ 15.1](guide:15-devices.md#151-x) and [evil](https://evil.example.com) and [js](javascript:alert(1))'
    );
    const anchors = [...container.querySelectorAll('a')];
    expect(anchors.map((a) => a.getAttribute('href'))).toEqual([
      '/devices',
      'https://docs.example.com/guide/15-devices.md#151-x',
    ]);
    expect(anchors[1].getAttribute('target')).toBe('_blank');
    expect(anchors[1].getAttribute('rel')).toBe('noopener noreferrer');
    expect(container.textContent).toContain('evil');
    expect(container.textContent).toContain('js');
  });

  it('renders GFM tables and code, with task-list checkboxes inert', () => {
    const { container } = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |\n\n- [ ] todo\n\n`code`');
    expect(container.querySelector('table')).not.toBeNull();
    expect(container.querySelector('input')).toBeNull();
    expect(container.querySelector('code')?.textContent).toBe('code');
  });

  it('property: no element outside the allow-list for arbitrary Markdown and HTML', () => {
    const html = fc.oneof(
      fc.constantFrom('<img src=x>', '<script>x</script>', '<iframe src=x></iframe>', '<a href="javascript:1">j</a>',
        '<div onclick=1>d</div>', '<svg onload=1></svg>', '<style>*{}</style>'),
      fc.string({ maxLength: 40 }),
    );
    fc.assert(
      fc.property(fc.array(html, { maxLength: 6 }), (parts) => {
        const { container, unmount } = renderMarkdown(parts.join(' '));
        try {
          for (const element of container.querySelectorAll('*')) {
            expect(ALLOWED.has(element.tagName), element.tagName).toBe(true);
          }
          expect(container.querySelector('[onclick], [onerror], [onload]')).toBeNull();
        } finally {
          unmount();
        }
      }),
      { numRuns: 40 }
    );
  });
});
