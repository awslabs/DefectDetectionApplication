/**
 * The TypeScript reference-list rules mirror the shared Python ones
 * (AC-43, mirror).
 *
 * `utils/referenceFiles.ts` parses a Bedrock Inference node's
 * `reference_payload_path` into kinds and labels and states its problems,
 * so the Portal shows exactly what the validator reports and the device
 * records. The oracle is `referenceListCases` of the fixture
 * `edge-cv-portal/backend/tests/fixtures/inspection_tuning_cases.json`,
 * which `edge-cv-portal/backend/tests/test_property_tuning_inspection_format.py`
 * writes from the Python functions' own answers: for each value and
 * `answer_format`, the kinds, the labels and the problems.
 *
 * The module is new with this change, so it is loaded inside the tests: at
 * c299a6e1 every test fails on the missing module.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const REFERENCE_FILES_MODULE = './referenceFiles';
function loadReferenceFiles(): Promise<typeof import('./referenceFiles')> {
  return import(/* @vite-ignore */ REFERENCE_FILES_MODULE);
}

interface ReferenceListCase {
  id: string;
  value: unknown;
  answerFormat: unknown;
  kinds: string[];
  labels: string[];
  problems: string[];
}

const FIXTURE_RELATIVE_PATH = path.join(
  'backend',
  'tests',
  'fixtures',
  'inspection_tuning_cases.json'
);

function resolveFixturePath(): string {
  let directory = process.cwd();
  for (let depth = 0; depth < 8; depth += 1) {
    const candidate = path.join(directory, FIXTURE_RELATIVE_PATH);
    if (existsSync(candidate)) {
      return candidate;
    }
    const parent = path.dirname(directory);
    if (parent === directory) {
      break;
    }
    directory = parent;
  }
  throw new Error(
    `cannot find the shared case table (${FIXTURE_RELATIVE_PATH}) above `
      + `${process.cwd()}`
  );
}

function referenceListCases(): ReferenceListCase[] {
  const fixture = JSON.parse(readFileSync(resolveFixturePath(), 'utf-8'));
  return (fixture.referenceListCases ?? []) as ReferenceListCase[];
}

describe('the reference-list mirror', () => {
  it('gives the kinds, labels and problems of every published case', async () => {
    const { parseReferenceList, referenceListProblems } = await loadReferenceFiles();
    const cases = referenceListCases();
    expect(cases.length).toBeGreaterThan(60);
    const disagreements: string[] = [];
    for (const item of cases) {
      const specs = parseReferenceList(item.value);
      const actual = {
        kinds: specs.map((spec) => spec.kind),
        labels: specs.map((spec) => spec.label),
        problems: referenceListProblems(specs, item.answerFormat),
      };
      const expected = { kinds: item.kinds, labels: item.labels, problems: item.problems };
      if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        disagreements.push(`${item.id}: ${JSON.stringify(actual)}`);
      }
    }
    expect(disagreements).toEqual([]);
  });

  it('labels a single image "Reference image" and any other list by position', async () => {
    const { referenceLabels } = await loadReferenceFiles();
    expect(referenceLabels(['image'])).toEqual(['Reference image']);
    expect(referenceLabels(['document'])).toEqual(['Reference 1']);
    expect(referenceLabels(['image', 'document'])).toEqual(['Reference 1', 'Reference 2']);
    expect(referenceLabels([])).toEqual([]);
  });

  it('reads upload lines as the shared pattern does', async () => {
    const { UPLOAD_LINE_RE, parseUploadLine } = await loadReferenceFiles();
    const digest = 'a'.repeat(64);
    expect(parseUploadLine(`upload://${digest}.png`)).toEqual([digest, 'png']);
    expect(parseUploadLine(`upload://${digest}.docx`)).toEqual([digest, 'docx']);
    for (const malformed of [
      `upload://${'A'.repeat(64)}.png`,
      `upload://${'a'.repeat(63)}.png`,
      `upload://${digest}.gif`,
      `upload://${digest}.png\n`,
      `upload://${digest}.jpeg`,
    ]) {
      expect(parseUploadLine(malformed)).toBeNull();
      expect(UPLOAD_LINE_RE.test(malformed)).toBe(false);
    }
  });

  it('accepts the fourteen upload extensions with their caps', async () => {
    const module = await loadReferenceFiles();
    expect([...module.ACCEPTED_REFERENCE_EXTENSIONS]).toEqual([
      '.jpg', '.jpeg', '.png', '.pdf', '.txt', '.md', '.markdown', '.csv',
      '.html', '.htm', '.doc', '.docx', '.xls', '.xlsx',
    ]);
    expect(module.MAX_REFERENCE_IMAGE_BYTES).toBe(8_388_608);
    expect(module.MAX_REFERENCE_DOCUMENT_BYTES).toBe(4_500_000);
    expect(module.MAX_DOCUMENT_REFERENCES).toBe(5);
    expect(module.MAX_IMAGE_REFERENCES).toBe(19);
  });
});
