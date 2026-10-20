/**
 * Pins the frontend's `schemas.json` fixture to the committed backend
 * contracts (workflow-telemetry-dashboards design §8.1-§8.2, §20 "two
 * copies"): the catalog, the three templates and the registry's model
 * states must be byte-for-byte the backend's, so a template or catalog
 * change on either side fails here until the fixture is regenerated.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import schemas from './schemas.json';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BACKEND = path.resolve(HERE, '../../../../../backend/functions/telemetry');

function readJson(relative: string): unknown {
  return JSON.parse(readFileSync(path.join(BACKEND, relative), 'utf-8'));
}

describe('the schemas fixture equals the backend contracts', () => {
  it('measure catalog', () => {
    expect(schemas.catalog).toEqual(readJson('measure_catalog.json'));
  });

  it.each(['generic-detector', 'anomaly', 'ppe-compliance'] as const)('template %s', (id) => {
    expect((schemas.templates as Record<string, unknown>)[id]).toEqual(readJson(`templates/${id}.json`));
  });

  it('registry version, kinds and model states', () => {
    const registry = readJson('registry.json') as { version: number; kinds: Record<string, string>; modelStates: string[]; modelStateOrder: string[] };
    expect(schemas.registry.version).toBe(registry.version);
    expect(schemas.registry.kinds).toEqual(registry.kinds);
    expect(schemas.registry.modelStates).toEqual(registry.modelStates);
    expect(schemas.registry.modelStateOrder).toEqual(registry.modelStateOrder);
  });

  it('every template measure exists in the catalog', () => {
    const ids = new Set(schemas.catalog.measures.map((m) => m.id));
    for (const template of Object.values(schemas.templates)) {
      for (const panel of template.panels) {
        for (const measure of panel.measures) expect(ids.has(measure), `${template.id}/${panel.id}/${measure}`).toBe(true);
      }
    }
  });
});
