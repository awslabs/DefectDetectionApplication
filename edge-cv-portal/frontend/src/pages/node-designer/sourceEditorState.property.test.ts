/**
 * **Feature: custom-node-source-lifecycle, Property 1: Save-request reduction is exact**
 * **Feature: custom-node-source-lifecycle, Property 17: Editor reducer invariants**
 *
 * Property 1 — for any baseline file map, sequence of edits/additions/
 * deletions, and source revision, `toSaveRequest` contains exactly the
 * paths whose content differs from the baseline or were added under
 * `files`, exactly the removed paths under `delete`, uses `replace` mode
 * (with the full current map) iff at least one path was deleted (and the
 * tree has no binary files), and carries `expected_source_revision`;
 * applying the request to the baseline reproduces the editor state.
 * **Validates: Requirements 1.3, 1.4, 1.12**
 *
 * Property 17 — for any event sequence, `dirtyPaths` is empty iff the
 * current map equals the baseline and no deletions are pending;
 * `save-failed` leaves the state unchanged; `save-succeeded` rebaselines
 * and clears deletions; a deleted path is never also in `files`.
 * **Validates: Requirements 1.11, 1.12**
 */
import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  applySaveRequest,
  dirtyPaths,
  fromSourceTree,
  isDirty,
  sourceEditorReducer,
  toSaveRequest,
  type SourceEditorEvent,
  type SourceEditorState,
} from './sourceEditorState';

const pathArb = fc
  .tuple(fc.constantFrom('plugin', 'builds/x86_64', 'docs'), fc.stringMatching(/^[a-z]{1,6}$/), fc.constantFrom('py', 'c', 'build', 'md'))
  .map(([dir, name, ext]) => `${dir}/${name}.${ext}`);
const contentArb = fc.string({ maxLength: 30 });
const filesArb = fc.dictionary(pathArb, contentArb, { maxKeys: 6 });

function eventArb(knownPaths: string[]): fc.Arbitrary<SourceEditorEvent> {
  const anyPath = knownPaths.length > 0 ? fc.oneof(fc.constantFrom(...knownPaths), pathArb) : pathArb;
  return fc.oneof(
    fc.record({ type: fc.constant('edit' as const), path: anyPath, content: contentArb }),
    fc.record({ type: fc.constant('add' as const), path: anyPath, content: fc.option(contentArb, { nil: undefined }) }),
    fc.record({ type: fc.constant('delete' as const), path: anyPath }),
    fc.record({ type: fc.constant('save-failed' as const) }),
    fc.record({ type: fc.constant('save-succeeded' as const), sourceRevision: fc.integer({ min: 1, max: 50 }) })
  );
}

function initialState(baseline: Record<string, string>, revision: number): SourceEditorState {
  return fromSourceTree(
    Object.entries(baseline).map(([file, content]) => ({ file, size: content.length, content })),
    revision
  );
}

describe('Property 1: Save-request reduction is exact', () => {
  it('reduces exactly the changed/added/deleted paths and round-trips the editor state', () => {
    fc.assert(
      fc.property(
        filesArb,
        fc.integer({ min: 1, max: 50 }),
        fc.array(fc.constant(null), { minLength: 0, maxLength: 12 }),
        fc.infiniteStream(fc.nat()),
        (baseline, revision, slots, seeds) => {
          let state = initialState(baseline, revision);
          const iterator = seeds[Symbol.iterator]();
          for (let i = 0; i < slots.length; i++) {
            const known = [...Object.keys(state.files), ...state.deleted];
            const event = fc.sample(eventArb(known), { seed: iterator.next().value, numRuns: 1 })[0];
            if (event.type === 'save-succeeded' || event.type === 'save-failed') continue;
            state = sourceEditorReducer(state, event);
          }

          const request = toSaveRequest(state);
          const dirty = dirtyPaths(state);
          if (dirty.length === 0) {
            expect(request).toBeNull();
            return;
          }
          expect(request).not.toBeNull();
          const req = request!;
          expect(req.expected_source_revision).toBe(revision);

          const changedOrAdded = Object.keys(state.files).filter(
            (p) => !(p in state.baseline) || state.baseline[p] !== state.files[p]
          );
          expect([...(req.delete ?? [])].sort()).toEqual([...state.deleted].sort());
          if (state.deleted.length > 0) {
            // Text-only trees: replace with the full map.
            expect(req.mode).toBe('replace');
            expect(req.files).toEqual(state.files);
          } else {
            expect(req.mode).toBe('merge');
            expect(Object.keys(req.files).sort()).toEqual(changedOrAdded.sort());
          }
          // Applying the request to the baseline reproduces the editor.
          expect(applySaveRequest(state.baseline, req)).toEqual(state.files);
        }
      ),
      { numRuns: 100 }
    );
  });

  it('uses merge + explicit deletions when binary files exist', () => {
    fc.assert(
      fc.property(filesArb.filter((f) => Object.keys(f).length > 0), (baseline) => {
        const [victim] = Object.keys(baseline);
        let state = fromSourceTree(
          [
            ...Object.entries(baseline).map(([file, content]) => ({ file, size: 1, content })),
            { file: 'assets/blob.bin', size: 99, binary: true },
          ],
          1
        );
        state = sourceEditorReducer(state, { type: 'delete', path: victim });
        const req = toSaveRequest(state)!;
        expect(req.mode).toBe('merge');
        expect(req.delete).toEqual([victim]);
        expect(req.files).toEqual({});
      }),
      { numRuns: 100 }
    );
  });
});

describe('Property 17: Editor reducer invariants', () => {
  it('holds dirty/clean, save-failed identity, rebaseline, and delete/files disjointness', () => {
    fc.assert(
      fc.property(
        filesArb,
        fc.integer({ min: 1, max: 50 }),
        fc.array(fc.nat(), { minLength: 0, maxLength: 15 }),
        (baseline, revision, seeds) => {
          let state = initialState(baseline, revision);
          for (const seed of seeds) {
            const known = [...Object.keys(state.files), ...state.deleted, ...Object.keys(state.baseline)];
            const event = fc.sample(eventArb(known), { seed, numRuns: 1 })[0];
            const next = sourceEditorReducer(state, event);

            if (event.type === 'save-failed') {
              expect(next).toBe(state);
            }
            if (event.type === 'save-succeeded') {
              expect(next.baseline).toEqual(state.files);
              expect(next.deleted).toEqual([]);
              expect(next.sourceRevision).toBe(event.sourceRevision);
              expect(isDirty(next)).toBe(false);
            }
            // A deleted path is never also present in files.
            for (const deleted of next.deleted) {
              expect(deleted in next.files).toBe(false);
            }
            // dirty iff files != baseline or deletions pending.
            const sameAsBaseline =
              JSON.stringify(Object.entries(next.files).sort()) ===
              JSON.stringify(Object.entries(next.baseline).sort());
            expect(isDirty(next)).toBe(!(sameAsBaseline && next.deleted.length === 0));
            state = next;
          }
        }
      ),
      { numRuns: 100 }
    );
  });
});
