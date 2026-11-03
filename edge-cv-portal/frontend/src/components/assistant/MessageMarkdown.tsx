/**
 * Safe Markdown of an assistant response (spec portal-ai-assistant
 * Requirement 3.7, 3.8; design §12).
 *
 * react-markdown without a raw-HTML plugin never renders HTML; `skipHtml`
 * drops HTML nodes from the output altogether. Elements outside the
 * allow-list are unwrapped. Links are of three kinds: root-relative Portal
 * routes navigate in-app; `guide:<chapter>#<anchor>` opens the user guide
 * chapter in a new tab; anything else is rendered as plain text.
 */
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useNavigate } from 'react-router-dom';
import { getConfig } from '../../config';
import { DEFAULT_GUIDE_BASE_URL } from './constants';

export const ALLOWED_ELEMENTS = [
  'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'a', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'input',
];

export type LinkKind = 'portal' | 'guide' | 'none';

export function classifyLink(href: string | undefined): LinkKind {
  if (!href) return 'none';
  if (href.startsWith('guide:')) return 'guide';
  // root-relative, not protocol-relative, no scheme, no backslash tricks
  if (/^\/(?!\/)[^\s\\]*$/.test(href)) return 'portal';
  return 'none';
}

export function guideBaseUrl(): string {
  const config = getConfig();
  return (config.assistantGuideBaseUrl || config.branding?.documentationUrl || DEFAULT_GUIDE_BASE_URL).replace(/\/$/, '');
}

/** `guide:02-use-cases.md#anchor` -> the external chapter URL, or undefined when malformed. */
export function guideHref(href: string): string | undefined {
  const target = href.slice('guide:'.length);
  const match = /^((?:README|[0-9]{2}-[a-z0-9-]+)\.md)(#[A-Za-z0-9_-]*)?$/.exec(target);
  if (!match) return undefined;
  return `${guideBaseUrl()}/${match[1]}${match[2] ?? ''}`;
}

interface Props {
  text: string;
}

export default function MessageMarkdown({ text }: Props) {
  const navigate = useNavigate();
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      allowedElements={ALLOWED_ELEMENTS}
      unwrapDisallowed
      // react-markdown's default transform strips unknown schemes, which
      // would drop the `guide:` citations before `classifyLink` sees them.
      // The classifier below is the only gate: Portal paths and guide links
      // render as anchors, everything else as plain text.
      urlTransform={(url) => url}
      components={{
        a: ({ href, children }: { href?: string; children?: ReactNode }) => {
          const kind = classifyLink(href);
          if (kind === 'portal') {
            return (
              <a
                href={href}
                onClick={(event) => {
                  event.preventDefault();
                  navigate(href as string);
                }}
              >
                {children}
              </a>
            );
          }
          if (kind === 'guide') {
            const external = guideHref(href as string);
            if (external) {
              return (
                <a href={external} target="_blank" rel="noopener noreferrer">
                  {children}
                </a>
              );
            }
          }
          return <span>{children}</span>;
        },
        // checkboxes of GFM task lists stay inert
        input: () => null,
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
