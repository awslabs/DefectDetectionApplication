/*
 * Unit tests for the image-settings bounds helpers.
 *
 * deviceSeedValues backs the fix for the edit form presenting the stored
 * static default (exposure 500) as though it were the camera's actual
 * exposure: the form now starts from the device's reported `current` values.
 * Verified against the real readings from the Basler acA4600-10uc on
 * adlink-dlap-701 (exposure 35..1460000 us, gain 0..20.197).
 */

import { CameraFeatureBound, CameraFeatureBounds } from "api/CameraAPI";
import { deviceSeedValues, toSettingsBounds } from "./bounds";

function bound(overrides: Partial<CameraFeatureBound> = {}): CameraFeatureBound {
  return {
    type: "float",
    min: null,
    max: null,
    increment: null,
    current: null,
    unit: null,
    options: [],
    available: true,
    feature: "Feature",
    advanced: false,
    ...overrides,
  } as CameraFeatureBound;
}

describe("deviceSeedValues", () => {
  it("returns nothing when the device reported no bounds", () => {
    expect(deviceSeedValues(undefined)).toEqual({});
  });

  it("seeds gain and exposure from the device's current readings", () => {
    const bounds: CameraFeatureBounds = {
      exposure: bound({ min: 35, max: 1460000, current: 297986, unit: "us" }),
      gain: bound({ min: 0, max: 20.197512674243203, current: 12 }),
    };

    expect(deviceSeedValues(bounds)).toEqual({ gain: 12, exposure: 297986 });
  });

  it("rounds fractional device readings to integers", () => {
    // The real camera reports gain as a float (0.9843604534036329).
    const bounds: CameraFeatureBounds = {
      gain: bound({ min: 0, max: 20.197512674243203, current: 0.9843604534 }),
    };

    expect(deviceSeedValues(bounds).gain).toBe(1);
  });

  it("clamps a reading into the same integer range the form validates against", () => {
    // toSettingsBounds rounds the range inward, so an at-the-limit reading
    // must be clamped the same way or the seeded value fails validation.
    const bounds: CameraFeatureBounds = {
      exposure: bound({ min: 35.4, max: 1460000.7, current: 1460000.7 }),
      gain: bound({ min: 0.6, max: 20.197, current: 0.6 }),
    };

    const seed = deviceSeedValues(bounds);
    const settings = toSettingsBounds(bounds);

    expect(seed.exposure).toBe(1460000);
    expect(seed.exposure!).toBeLessThanOrEqual(settings.exposureMax);
    expect(seed.gain).toBe(1);
    expect(seed.gain!).toBeGreaterThanOrEqual(settings.gainMin);
  });

  it("omits a control the device did not report a usable number for", () => {
    const bounds: CameraFeatureBounds = {
      exposure: bound({ current: null }),
      gain: bound({ current: "Off" }),
    };

    expect(deviceSeedValues(bounds)).toEqual({
      gain: undefined,
      exposure: undefined,
    });
  });

  it("leaves the existing bounds mapping untouched", () => {
    const bounds: CameraFeatureBounds = {
      exposure: bound({ min: 35, max: 1460000, current: 35, unit: "us" }),
      gain: bound({ min: 0, max: 20.197512674243203, current: 1 }),
    };

    expect(toSettingsBounds(bounds)).toEqual({
      gainMin: 0,
      gainMax: 20,
      exposureMin: 35,
      exposureMax: 1460000,
      exposureUnit: "microseconds",
    });
  });
});
