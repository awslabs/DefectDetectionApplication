/**
 * **Feature: rtsp-rtmp-stream-cameras, Property 6: Inline-check parity**
 *
 * For any graph, the frontend inline checks produce the same
 * (code, node id, severity) triples as the backend validator for V7, V11,
 * V12, V13 and W3 (and V9, whose continuous-stream skip belongs to the
 * same rule set). `__fixtures__/inlineParityCorpus.json` holds graphs
 * generated and validated by the Python validator, with the served
 * catalog descriptors those graphs use; a Python test keeps it current.
 * Messages are compared too, so the ported problem texts cannot drift.
 * The generated properties then pin the rule structure: which nodes a
 * code can target, one finding per node, and that a graph without the
 * new node types gets no new findings.
 *
 * **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 13.7, 13.8**
 */
import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import corpus from './__fixtures__/inlineParityCorpus.json';
import {
  CODE_V11_STREAM_URL,
  CODE_V12_CONTINUOUS_ACTIVATION,
  CODE_V13_ANALYTICS_CONFIG_INVALID,
  CODE_W3_ANALYTICS_NO_DETECTOR,
  effectiveNodeType,
  runInlineChecks,
  type GraphLike,
} from './inlineChecks';
import type { NodeTypeDescriptor, ValidationFinding, WorkflowNode } from './types';

interface CorpusEntry {
  name: string;
  graph: GraphLike;
  findings: [string, string, string, string][];
}

const { catalog, entries, mirroredCodes } = corpus as unknown as {
  catalog: NodeTypeDescriptor[];
  entries: CorpusEntry[];
  mirroredCodes: string[];
};
const MIRRORED = new Set(mirroredCodes);

type Tuple = [string, string, string, string];

function compare(a: Tuple, b: Tuple): number {
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return 0;
}

function mirroredFindings(graph: GraphLike): Tuple[] {
  return runInlineChecks(graph, catalog)
    .filter((finding) => MIRRORED.has(finding.code))
    .map((finding): Tuple => [finding.code, finding.nodeId ?? '', finding.severity, finding.message])
    .sort(compare);
}

describe('Property 6: inline checks reproduce the backend findings', () => {
  it('replays every corpus graph with identical findings', () => {
    const mismatches: string[] = [];
    for (const entry of entries) {
      const expected = [...entry.findings].sort(compare);
      const actual = mirroredFindings(entry.graph);
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        mismatches.push(
          `${entry.name}:\n  expected ${JSON.stringify(expected)}\n  actual   ${JSON.stringify(actual)}`
        );
      }
    }
    expect(mismatches).toEqual([]);
  });

  it('the corpus exercises every mirrored code', () => {
    const codes = new Set(entries.flatMap((entry) => entry.findings.map((finding) => finding[0])));
    expect(codes).toEqual(MIRRORED);
  });
});

// --------------------------------------------------------------------------
// Generated structural properties over the corpus catalog
// --------------------------------------------------------------------------

const STREAM_TYPES = new Set(['rtsp_camera_source', 'rtmp_stream_source']);
const PRE_FEATURE_TYPES = catalog
  .map((descriptor) => descriptor.typeId)
  .filter(
    (typeId) =>
      !STREAM_TYPES.has(typeId) &&
      !['detection_counter', 'object_association', 'event_gate', 'unified_input'].includes(typeId)
  );

function graphArb(types: readonly string[]): fc.Arbitrary<GraphLike> {
  return fc
    .array(
      fc.record({
        type: fc.constantFrom(...types),
        url: fc.constantFrom('rtsp://10.0.0.5/live', 'rtmp://m/live', 'rtsp://u:p@h/x', '', 'x'),
        mode: fc.constantFrom('continuous', 'on_trigger', undefined),
        sourceKind: fc.constantFrom('rtsp_camera', 'rtmp_stream', 'folder', undefined),
        classes: fc.constantFrom('person', 'a,,b', '!!', undefined),
      }),
      { minLength: 1, maxLength: 6 }
    )
    .chain((specs) =>
      fc
        .array(
          fc.record({
            from: fc.nat({ max: specs.length - 1 }),
            to: fc.nat({ max: specs.length - 1 }),
            port: fc.constantFrom('in', 'activation'),
          }),
          { maxLength: 8 }
        )
        .map((edges) => {
          const nodes: WorkflowNode[] = specs.map((spec, index) => {
            const parameters: Record<string, string> = { url: spec.url };
            if (spec.mode !== undefined) parameters.processing_mode = spec.mode;
            if (spec.sourceKind !== undefined) parameters.source_kind = spec.sourceKind;
            if (spec.classes !== undefined) parameters.classes = spec.classes;
            return { id: `n${index}`, type: spec.type, position: { x: 0, y: 0 }, parameters };
          });
          const connections = edges
            .filter((edge) => edge.from !== edge.to)
            .map((edge, index) => ({
              id: `c${index}`,
              from: { node: `n${edge.from}`, port: 'out' },
              to: { node: `n${edge.to}`, port: edge.port },
            }));
          return { nodes, connections };
        })
    );
}

function codesByNode(findings: ValidationFinding[], code: string): Map<string, number> {
  const counts = new Map<string, number>();
  for (const finding of findings) {
    if (finding.code === code) {
      counts.set(finding.nodeId!, (counts.get(finding.nodeId!) ?? 0) + 1);
    }
  }
  return counts;
}

describe('Property 6: rule structure (generated)', () => {
  const allTypes = catalog.map((descriptor) => descriptor.typeId);

  it('V11 and V12 target only stream nodes, at most once each', () => {
    fc.assert(
      fc.property(graphArb(allTypes), (graph) => {
        const findings = runInlineChecks(graph, catalog);
        const byId = new Map(graph.nodes.map((node) => [node.id, node]));
        for (const code of [CODE_V11_STREAM_URL, CODE_V12_CONTINUOUS_ACTIVATION]) {
          for (const [nodeId, count] of codesByNode(findings, code)) {
            expect(count).toBe(1);
            expect(STREAM_TYPES.has(effectiveNodeType(byId.get(nodeId)!))).toBe(true);
          }
        }
      }),
      { numRuns: 100 }
    );
  });

  it('V13 and W3 target only scene analytics nodes', () => {
    fc.assert(
      fc.property(graphArb(allTypes), (graph) => {
        const findings = runInlineChecks(graph, catalog);
        const byId = new Map(graph.nodes.map((node) => [node.id, node]));
        for (const [nodeId] of codesByNode(findings, CODE_V13_ANALYTICS_CONFIG_INVALID)) {
          expect(['detection_counter', 'object_association']).toContain(byId.get(nodeId)!.type);
        }
        for (const [nodeId, count] of codesByNode(findings, CODE_W3_ANALYTICS_NO_DETECTOR)) {
          expect(count).toBe(1);
          expect(['detection_counter', 'object_association']).toContain(byId.get(nodeId)!.type);
        }
      }),
      { numRuns: 100 }
    );
  });

  it('a graph without the new node types gets no new finding codes', () => {
    fc.assert(
      fc.property(graphArb(PRE_FEATURE_TYPES), (graph) => {
        const codes = new Set(runInlineChecks(graph, catalog).map((finding) => finding.code));
        for (const code of [
          CODE_V11_STREAM_URL,
          CODE_V12_CONTINUOUS_ACTIVATION,
          CODE_V13_ANALYTICS_CONFIG_INVALID,
          CODE_W3_ANALYTICS_NO_DETECTOR,
        ]) {
          expect(codes.has(code)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });
});
