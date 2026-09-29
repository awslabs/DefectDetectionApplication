/**
 * **Feature: rtsp-rtmp-stream-cameras, Property 2: Stream_URL rules are sound and complete**
 * (the TypeScript port, over a fixture corpus generated from the Python module)
 *
 * `streamUrl.ts` must return the same verdict, problem code and message
 * as `workflow_core.stream_url.check_stream_url` for every value.
 * `__fixtures__/streamUrlCorpus.json` records the Python verdicts for a
 * deterministic corpus (every scheme x authority, every tail, the
 * Python/JavaScript whitespace and Unicode-digit edge cases, non-string
 * values, custom scheme lists, seeded random strings); a Python test
 * keeps it in step with the module. The generated properties then check
 * the rules themselves over structured URLs.
 *
 * **Validates: Requirements 2.1, 2.2, 2.6**
 */
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import corpus from './__fixtures__/streamUrlCorpus.json';
import {
  checkStreamUrl,
  normalizeStreamUrl,
  SCHEMES_BY_NODE_TYPE,
  SCHEMES_BY_SOURCE_TYPE,
  SECRET_QUERY_PARAMETERS,
  STREAM_URL_PATTERN,
} from './streamUrl';

interface CorpusEntry {
  url: unknown;
  code: string | null;
  m: number | null;
  nodeType?: string;
  schemes?: string | unknown[];
}

const { entries, messages } = corpus as unknown as {
  entries: CorpusEntry[];
  messages: string[];
};

const NUM_RUNS = { numRuns: 100 };

describe('Property 2: the TypeScript port reproduces the Python verdicts', () => {
  it('covers every problem code and accepted values', () => {
    const codes = new Set(entries.map((entry) => entry.code));
    expect(codes).toEqual(
      new Set([
        null,
        'invalid_url',
        'scheme_not_allowed',
        'no_host',
        'user_info',
        'secret_query_parameter',
      ])
    );
    expect(entries.length).toBeGreaterThan(1000);
  });

  it('returns the same code and message for every corpus value', () => {
    const mismatches: string[] = [];
    for (const entry of entries) {
      const schemes =
        entry.nodeType !== undefined ? SCHEMES_BY_NODE_TYPE[entry.nodeType] : entry.schemes!;
      const problem = checkStreamUrl(entry.url, schemes);
      const expected = entry.m === null ? null : messages[entry.m];
      if ((problem?.code ?? null) !== entry.code || (problem?.message ?? null) !== expected) {
        mismatches.push(
          `${JSON.stringify(entry.url)} [${JSON.stringify(schemes)}]: ` +
            `expected ${entry.code} / ${expected}, got ${problem?.code} / ${problem?.message}`
        );
      }
    }
    expect(mismatches).toEqual([]);
  });

  it('shares the scheme maps with the Python module', () => {
    expect(SCHEMES_BY_NODE_TYPE).toEqual({
      rtsp_camera_source: ['rtsp', 'rtsps'],
      rtmp_stream_source: ['rtmp', 'rtmps'],
    });
    expect(SCHEMES_BY_SOURCE_TYPE).toEqual({ RTSP: ['rtsp', 'rtsps'], RTMP: ['rtmp', 'rtmps'] });
  });
});

// --------------------------------------------------------------------------
// Generated properties over structured URLs
// --------------------------------------------------------------------------

const hostArb = fc.oneof(
  fc.stringMatching(/^[a-z0-9][a-z0-9.-]{0,20}$/),
  fc.ipV4(),
  fc.constant('[::1]'),
  fc.constant('[2001:db8::7]')
);
const portArb = fc.option(fc.integer({ min: 1, max: 65535 }), { nil: null });
const pathArb = fc.option(fc.stringMatching(/^\/[A-Za-z0-9/_.-]{0,24}$/), { nil: null });
const safeQueryArb = fc.option(
  fc.constantFrom('channel=1', 'profile=main', 'subtype=0', 'keyframe=1', 'passthrough=0'),
  { nil: null }
);

function build(
  scheme: string,
  host: string,
  port: number | null,
  path: string | null,
  query: string | null
): string {
  let url = `${scheme}://${port === null ? host : `${host}:${port}`}`;
  if (path !== null) url += path;
  if (query !== null) url += `?${query}`;
  return url;
}

const nodeTypeArb = fc.constantFrom('rtsp_camera_source', 'rtmp_stream_source');

describe('Property 2: the rules themselves (generated)', () => {
  it('accepts a well-formed URL of the node type, and it matches the catalog pattern', () => {
    fc.assert(
      fc.property(nodeTypeArb, hostArb, portArb, pathArb, safeQueryArb, (type, host, port, path, query) => {
        const schemes = SCHEMES_BY_NODE_TYPE[type];
        for (const scheme of schemes) {
          const url = build(scheme, host, port, path, query);
          expect(checkStreamUrl(url, schemes)).toBeNull();
          expect(new RegExp(STREAM_URL_PATTERN).test(url)).toBe(true);
        }
      }),
      NUM_RUNS
    );
  });

  it("rejects the other type's schemes", () => {
    fc.assert(
      fc.property(nodeTypeArb, hostArb, pathArb, (type, host, path) => {
        const other = type === 'rtsp_camera_source' ? 'rtmp_stream_source' : 'rtsp_camera_source';
        for (const scheme of SCHEMES_BY_NODE_TYPE[other]) {
          const problem = checkStreamUrl(build(scheme, host, null, path, null), SCHEMES_BY_NODE_TYPE[type]);
          expect(problem?.code).toBe('scheme_not_allowed');
        }
      }),
      NUM_RUNS
    );
  });

  it('rejects user information without echoing it', () => {
    fc.assert(
      fc.property(
        nodeTypeArb,
        hostArb,
        fc.stringMatching(/^[A-Za-z0-9._~-]{1,12}$/),
        // A sentinel prefix, so a hit in the message can only be a leak.
        fc.stringMatching(/^[A-Za-z0-9._~-]{1,12}$/).map((s) => `Zq7${s}`),
        (type, host, user, password) => {
          const scheme = SCHEMES_BY_NODE_TYPE[type][0];
          const url = `${scheme}://${user}:${password}@${host}/live`;
          const problem = checkStreamUrl(url, SCHEMES_BY_NODE_TYPE[type]);
          expect(problem?.code).toBe('user_info');
          expect(problem!.message).not.toContain(password);
        }
      ),
      NUM_RUNS
    );
  });

  it('rejects a Secret_Query_Parameter in any case, naming it but not its value', () => {
    fc.assert(
      fc.property(
        nodeTypeArb,
        hostArb,
        fc.constantFrom(...SECRET_QUERY_PARAMETERS),
        fc.array(fc.boolean(), { minLength: 12, maxLength: 12 }),
        fc.stringMatching(/^[A-Za-z0-9]{1,12}$/).map((s) => `Vk3${s}`),
        (type, host, name, upper, value) => {
          const spelled = [...name].map((c, i) => (upper[i] ? c.toUpperCase() : c)).join('');
          const url = `${SCHEMES_BY_NODE_TYPE[type][0]}://${host}/x?a=1&${spelled}=${value}`;
          const problem = checkStreamUrl(url, SCHEMES_BY_NODE_TYPE[type]);
          expect(problem?.code).toBe('secret_query_parameter');
          expect(problem!.message).toContain(`'${spelled}'`);
          expect(problem!.message).not.toContain(value);
        }
      ),
      NUM_RUNS
    );
  });

  it('normalization is idempotent and identifies URLs that differ only by case or default port', () => {
    fc.assert(
      fc.property(nodeTypeArb, hostArb, pathArb, (type, host, path) => {
        const scheme = SCHEMES_BY_NODE_TYPE[type][0];
        const defaultPort = scheme === 'rtsp' ? 554 : 1935;
        const plain = build(scheme, host, null, path, null);
        const variant = build(scheme.toUpperCase(), host.toUpperCase(), defaultPort, path, null);
        expect(normalizeStreamUrl(variant)).toBe(normalizeStreamUrl(plain));
        expect(normalizeStreamUrl(normalizeStreamUrl(variant))).toBe(normalizeStreamUrl(variant));
      }),
      NUM_RUNS
    );
  });
});
