/**
 * Example-level tests for the per-deployment LocalServer floor helpers of
 * `lib/context-helpers.ts` (car-assembly-check tasks 12.2 and 12.5; design §7,
 * DD9): the env-then-SSM context defaults and the floor-map parser, for the
 * Layout_Check key and the RF-DETR-Seg key. Zero CDK imports, zero synth cost;
 * the template consequences are pinned by layout-check-infra.test.ts and
 * rfdetr-seg-infra.test.ts.
 */
import {
  LAYOUT_CHECK_FLOOR_SSM_PARAMETER,
  MIN_VERSIONS_FLOOR_ARCHES,
  RFDETR_SEG_FLOOR_SSM_PARAMETER,
  layoutCheckFloorDefault,
  minVersionsFloorDefault,
  parseLayoutCheckFloor,
  parseMinVersionsFloor,
  rfDetrSegFloorDefault,
} from '../lib/context-helpers';

const ssmReturning = (value: string) => {
  const reads: string[] = [];
  const read = (name: string) => {
    reads.push(name);
    return value;
  };
  return { read, reads };
};
const ssmFailing = () => {
  throw new Error('ParameterNotFound');
};

const DEFAULTS = [
  {
    name: 'layoutCheckFloorDefault',
    fn: layoutCheckFloorDefault,
    envVar: 'LAYOUT_CHECK_FLOOR',
    parameter: LAYOUT_CHECK_FLOOR_SSM_PARAMETER,
  },
  {
    name: 'rfDetrSegFloorDefault',
    fn: rfDetrSegFloorDefault,
    envVar: 'RFDETR_SEG_FLOOR',
    parameter: RFDETR_SEG_FLOOR_SSM_PARAMETER,
  },
];

describe('the floor SSM parameters', () => {
  test('carry the documented names', () => {
    expect(LAYOUT_CHECK_FLOOR_SSM_PARAMETER).toBe('/dda-portal/layout-check-min-local-server-versions');
    expect(RFDETR_SEG_FLOOR_SSM_PARAMETER).toBe('/dda-portal/rfdetr-seg-min-local-server-versions');
  });

  test('the accepted arch ids are the workflow_core ones', () => {
    expect(MIN_VERSIONS_FLOOR_ARCHES).toEqual(['arm64_cpu', 'arm64_jp5', 'arm64_jp6', 'arm64_jp7', 'x86_64', 'x86_64_nvidia']);
  });
});

describe.each(DEFAULTS)('$name (env, then SSM)', ({ fn, envVar, parameter }) => {
  const VALUE = '{"arm64_jp5":"1.0.4"}';
  const OTHER = '{"arm64_jp5":"1.0.9"}';

  test(`env ${envVar} wins and SSM is not read`, () => {
    const ssm = ssmReturning(OTHER);
    expect(fn({ [envVar]: ` ${VALUE} ` }, ssm.read)).toBe(VALUE);
    expect(ssm.reads).toEqual([]);
  });

  test('otherwise the SSM parameter, trimmed', () => {
    const ssm = ssmReturning(`${OTHER}\n`);
    expect(fn({}, ssm.read)).toBe(OTHER);
    expect(ssm.reads).toEqual([parameter]);
  });

  test('a blank env value falls through to SSM', () => {
    const ssm = ssmReturning(OTHER);
    expect(fn({ [envVar]: '   ' }, ssm.read)).toBe(OTHER);
  });

  test('SSM failure, "None" and blank give no default', () => {
    expect(fn({}, ssmFailing)).toBeUndefined();
    expect(fn({}, ssmReturning('None').read)).toBeUndefined();
    expect(fn({}, ssmReturning('  ').read)).toBeUndefined();
  });

  test('the Lambda variable name is not read', () => {
    const lambdaVar =
      envVar === 'LAYOUT_CHECK_FLOOR'
        ? 'WORKFLOW_LAYOUT_CHECK_MIN_LOCAL_SERVER_VERSIONS'
        : 'WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS';
    expect(fn({ [lambdaVar]: VALUE }, ssmFailing)).toBeUndefined();
  });

  test('is the generic helper with its own variable and parameter', () => {
    const ssm = ssmReturning(OTHER);
    expect(fn({}, ssm.read)).toBe(minVersionsFloorDefault(envVar, parameter, {}, ssm.read));
  });
});

describe('parseLayoutCheckFloor', () => {
  test('absent, null and blank resolve to {}', () => {
    for (const value of [undefined, null, '', '  ']) {
      expect(parseLayoutCheckFloor(value)).toEqual({});
    }
  });

  test("'{}' is {}", () => {
    expect(parseLayoutCheckFloor('{}')).toEqual({});
  });

  test('a valid map round-trips as a string and as an object', () => {
    const floor = { arm64_jp5: '1.0.4', x86_64: '1.0.60' };
    expect(parseLayoutCheckFloor(JSON.stringify(floor))).toEqual(floor);
    expect(parseLayoutCheckFloor(floor)).toEqual(floor);
  });

  test.each([['nonsense'], ['[]'], ['{"arm64_jp9": "1.0.0"}'], ['{"arm64_jp5": "1.0"}'], [{ arm64_jp5: 4 }], [42]])(
    '%p throws naming the key and the parameter',
    (value) => {
      expect(() => parseLayoutCheckFloor(value)).toThrow(/layoutCheckMinLocalServerVersions/);
      expect(() => parseLayoutCheckFloor(value)).toThrow(LAYOUT_CHECK_FLOOR_SSM_PARAMETER);
    },
  );

  test('the message names the offending entry', () => {
    expect(() => parseLayoutCheckFloor('{"arm64_jp9": "1.0.0"}')).toThrow(/arm64_jp9: "1\.0\.0"/);
    expect(() => parseLayoutCheckFloor('{"arm64_jp5": "1.0"}')).toThrow(/arm64_jp5: "1\.0"/);
  });
});

describe('parseMinVersionsFloor with the Seg key', () => {
  const parseSeg = (value: unknown) =>
    parseMinVersionsFloor('rfdetrSegMinLocalServerVersions', RFDETR_SEG_FLOOR_SSM_PARAMETER, value);

  test('absent, null and blank are undefined (the committed default applies)', () => {
    for (const value of [undefined, null, '', '  ']) {
      expect(parseSeg(value)).toBeUndefined();
    }
  });

  test("'{}' is a configured empty map", () => {
    expect(parseSeg('{}')).toEqual({});
    expect(parseSeg({})).toEqual({});
  });

  test('a valid map round-trips as a string and as an object', () => {
    const floor = { arm64_jp5: '1.0.4' };
    expect(parseSeg(JSON.stringify(floor))).toEqual(floor);
    expect(parseSeg(floor)).toEqual(floor);
  });

  test.each([['nonsense'], ['[]'], ['{"arm64_jp9": "1.0.0"}'], ['{"arm64_jp5": "1.0"}']])(
    '%p throws naming the key and the parameter',
    (value) => {
      expect(() => parseSeg(value)).toThrow(/rfdetrSegMinLocalServerVersions/);
      expect(() => parseSeg(value)).toThrow(RFDETR_SEG_FLOOR_SSM_PARAMETER);
    },
  );
});
