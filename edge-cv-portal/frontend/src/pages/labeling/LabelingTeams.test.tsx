/**
 * Unit tests for the LabelingTeams page's create-team name validation
 * (dda-data-labeling task 16.2), which mirrors the backend rules in
 * dda_labeling.create_labeling_team: non-empty after trimming and at
 * most 128 characters (Requirement 3.1 / 3.2).
 */

import { describe, it, expect } from 'vitest';
import { validateTeamName } from './LabelingTeams';

describe('validateTeamName', () => {
  it('rejects an empty name', () => {
    expect(validateTeamName('')).toBe('Team name must not be empty');
  });

  it('rejects a whitespace-only name (matches backend trim)', () => {
    expect(validateTeamName('   ')).toBe('Team name must not be empty');
  });

  it('accepts a typical name', () => {
    expect(validateTeamName('Night Shift Labelers')).toBeNull();
  });

  it('accepts a name of exactly 128 characters (boundary)', () => {
    expect(validateTeamName('a'.repeat(128))).toBeNull();
  });

  it('rejects a name of 129 characters', () => {
    expect(validateTeamName('a'.repeat(129))).toBe(
      'Team name must be at most 128 characters'
    );
  });

  it('trims before measuring length, like the backend', () => {
    // 128 significant characters padded with whitespace is still valid.
    expect(validateTeamName(`  ${'a'.repeat(128)}  `)).toBeNull();
  });
});
