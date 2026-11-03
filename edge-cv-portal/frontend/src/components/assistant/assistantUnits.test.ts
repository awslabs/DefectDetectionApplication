/**
 * Pure helpers of the assistant frontend (spec portal-ai-assistant; tasks.md
 * 4.5): the route -> chapter map pinned to the backend copy (P11), the
 * first-visit drawer rule (Requirement 2.4), the poll interval, the link
 * classifier, the suggested prompts, the trace rows and the failure texts.
 */
import * as fs from 'fs';
import * as path from 'path';
import { describe, expect, it } from 'vitest';
import fc from 'fast-check';
import { allRouteChapters, chapterForRoute, NON_ROUTE_CHAPTERS, routeEntry } from './routeChapters';
import { initialOpenState, pollIntervalFor } from './AssistantProvider';
import type { LiveTurn } from './AssistantProvider';
import { classifyLink, guideHref } from './MessageMarkdown';
import { suggestedPrompts } from './suggestedPrompts';
import { traceRows } from './ActivityTrace';
import { failureText } from './AssistantDrawer';
import {
  FIRST_VISIT_MIN_WIDTH, POLL_INTERVAL_HIDDEN_MS, POLL_INTERVAL_MS, POLL_INTERVAL_WAITING_MS, WAITING_SLOWDOWN_MS,
} from './constants';

const BACKEND_MAP = path.resolve(__dirname, '../../../../backend/functions/assistant/route-chapters.json');
const FRONTEND_MAP = path.resolve(__dirname, 'route-chapters.json');
const GUIDE_DIR = path.resolve(__dirname, '../../../../../docs/user-guide');

describe('route -> chapter map (P11)', () => {
  it('is byte-identical to the backend copy', () => {
    expect(fs.readFileSync(FRONTEND_MAP, 'utf-8')).toBe(fs.readFileSync(BACKEND_MAP, 'utf-8'));
  });

  it('names only shipped chapters and covers every chapter with the non-route list', () => {
    const shipped = fs.readdirSync(GUIDE_DIR).filter((n) => /^[0-9]{2}-[a-z0-9-]+\.md$/.test(n)).sort();
    const mapped = new Set(allRouteChapters().map((r) => r.chapter));
    for (const chapter of mapped) expect(shipped).toContain(chapter);
    const covered = new Set([...mapped, ...NON_ROUTE_CHAPTERS]);
    expect([...covered].sort()).toEqual(shipped);
  });

  it('matches the longest prefix and ignores query strings', () => {
    expect(chapterForRoute('/node-designer/review')).toBe('17-plugin-review.md');
    expect(chapterForRoute('/node-designer/plugins/p1')).toBe('10-node-designer.md');
    expect(chapterForRoute('/labeling/datasets?x=1')).toBe('03-data-management.md');
    expect(chapterForRoute('/labeling/job-1/review')).toBe('04-labeling.md');
    expect(chapterForRoute('/devices/thing-1')).toBe('15-devices.md');
    expect(routeEntry('/devices')?.domain).toBe('devices');
    expect(chapterForRoute('/nope')).toBeUndefined();
  });

  it('covers every authenticated route of App.tsx except the labeler workspace rule exceptions', () => {
    const app = fs.readFileSync(path.resolve(__dirname, '../../App.tsx'), 'utf-8');
    const routes = [...app.matchAll(/path="([^"]+)"/g)].map((m) => m[1]).filter((p) => p !== '/login' && p !== '/');
    for (const route of routes) {
      const concrete = `/${route.replace(/:[A-Za-z]+/g, 'x')}`;
      expect(chapterForRoute(concrete), concrete).toBeDefined();
    }
  });
});

describe('first-visit drawer rule (Requirement 2.4)', () => {
  it('honours a remembered choice everywhere', () => {
    expect(initialOpenState('/workflows/builder', 'open', 500)).toBe(true);
    expect(initialOpenState('/dashboard', 'closed', 2000)).toBe(false);
  });

  it('opens on wide viewports, not on the builder, not on narrow viewports', () => {
    expect(initialOpenState('/dashboard', null, FIRST_VISIT_MIN_WIDTH)).toBe(true);
    expect(initialOpenState('/dashboard', null, FIRST_VISIT_MIN_WIDTH - 1)).toBe(false);
    expect(initialOpenState('/workflows/builder/wf-1', null, 3000)).toBe(false);
    fc.assert(fc.property(fc.integer({ min: 0, max: 5000 }), fc.string(), (width, route) => {
      const open = initialOpenState(`/${route}`, null, width);
      if (`/${route}`.startsWith('/workflows/builder')) return open === false;
      return open === width >= FIRST_VISIT_MIN_WIDTH;
    }));
  });
});

describe('poll interval', () => {
  const turn = (status: LiveTurn['status'], waitingSince: number | null = null): LiveTurn => ({
    turnId: 't', status, version: 1, prompt: 'p', text: '', events: [], confirmation: null, failure: null,
    startedAt: 0, waitingSince,
  });
  it('stops on terminal statuses and slows down when hidden or waiting long', () => {
    expect(pollIntervalFor(null, false, 0)).toBeNull();
    expect(pollIntervalFor(turn('completed'), false, 0)).toBeNull();
    expect(pollIntervalFor(turn('running'), false, 0)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(turn('running'), true, 0)).toBe(POLL_INTERVAL_HIDDEN_MS);
    expect(pollIntervalFor(turn('awaiting_confirmation', 0), false, WAITING_SLOWDOWN_MS - 1)).toBe(POLL_INTERVAL_MS);
    expect(pollIntervalFor(turn('awaiting_confirmation', 0), false, WAITING_SLOWDOWN_MS + 1)).toBe(POLL_INTERVAL_WAITING_MS);
  });
});

describe('link classification (Requirement 3.8)', () => {
  it('accepts root-relative Portal paths and guide links only', () => {
    expect(classifyLink('/devices')).toBe('portal');
    expect(classifyLink('/devices/d-1?x=1')).toBe('portal');
    expect(classifyLink('//evil.example.com')).toBe('none');
    expect(classifyLink('https://example.com')).toBe('none');
    expect(classifyLink('javascript:alert(1)')).toBe('none');
    expect(classifyLink('guide:15-devices.md#151-the-list')).toBe('guide');
    expect(classifyLink(undefined)).toBe('none');
    expect(guideHref('guide:15-devices.md#151-the-list')).toMatch(/\/15-devices\.md#151-the-list$/);
    expect(guideHref('guide:README.md')).toMatch(/\/README\.md$/);
    expect(guideHref('guide:../etc/passwd')).toBeUndefined();
    expect(guideHref('guide:15-devices.md#<img>')).toBeUndefined();
  });
});

describe('suggested prompts (Requirement 3.6)', () => {
  it('gives at most four prompts for every route', () => {
    for (const entry of allRouteChapters()) {
      const prompts = suggestedPrompts(entry.prefix);
      expect(prompts.length).toBeGreaterThan(0);
      expect(prompts.length).toBeLessThanOrEqual(4);
    }
    expect(suggestedPrompts('/devices')[0]).toMatch(/devices/i);
    expect(suggestedPrompts('/unknown')).toHaveLength(3);
  });
});

describe('trace rows and failure texts', () => {
  it('folds a tool start and end into one row with the outcome', () => {
    const rows = traceRows([
      { seq: 1, ts: 0, type: 'tool_start', text_offset: 0, tool: 'portal_devices_list', title: 'List devices', args_summary: 'usecase_id=u' },
      { seq: 2, ts: 0, type: 'tool_end', text_offset: 0, tool: 'portal_devices_list', duration_ms: 1500 },
      { seq: 3, ts: 0, type: 'confirmation_requested', text_offset: 0, tool: 'portal_devices_delete' },
      { seq: 4, ts: 0, type: 'budget', text_offset: 0, limit: 'tool_calls_per_turn' },
    ]);
    expect(rows.map((r) => r.type)).toEqual(['success', 'warning', 'warning']);
    expect(rows[0].text).toBe('List devices (usecase_id=u): done · 1.5 s');
    expect(rows[2].text).toBe('Limit reached: tool calls per turn');
    const denied = traceRows([
      { seq: 1, ts: 0, type: 'tool_start', text_offset: 0, tool: 'x', title: 'X' },
      { seq: 2, ts: 0, type: 'tool_end', text_offset: 0, tool: 'x', category: 'not_authorized' },
    ]);
    expect(denied[0].type).toBe('error');
    expect(denied[0].text).toContain('not authorized');
  });

  it('maps failure codes to sentences and keeps the timeout value', () => {
    expect(failureText(null)).toBeNull();
    expect(failureText({ code: 'bedrock_throttled', message: 'x' })).toMatch(/throttled/);
    expect(failureText({ code: 'bedrock_timeout', message: 'timeout 60' })).toBe('The model did not answer within 60 seconds.');
    expect(failureText({ code: 'something_else', message: 'custom' })).toBe('custom');
  });
});
