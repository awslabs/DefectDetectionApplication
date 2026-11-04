/**
 * Portal route -> user-guide chapter (spec portal-ai-assistant, design §5).
 * The JSON is the same file the backend ships (`assistant/route-chapters.json`);
 * `routeChapters.test.ts` pins the two copies equal.
 */
import map from './route-chapters.json';

export interface RouteChapter {
  prefix: string;
  chapter: string;
  domain: string;
}

const ROUTES: RouteChapter[] = [...(map.routes as RouteChapter[])].sort(
  (a, b) => b.prefix.length - a.prefix.length
);

export const NON_ROUTE_CHAPTERS: string[] = map.non_route_chapters as string[];

function normalize(path: string): string {
  let route = (path || '/').split('?')[0].split('#')[0];
  if (!route.startsWith('/')) route = `/${route}`;
  route = route.replace(/\/+$/, '');
  return route || '/';
}

/** The longest matching route entry, or undefined. */
export function routeEntry(path: string): RouteChapter | undefined {
  const route = normalize(path);
  return ROUTES.find((entry) => route === entry.prefix || route.startsWith(`${entry.prefix}/`));
}

export function chapterForRoute(path: string): string | undefined {
  return routeEntry(path)?.chapter;
}

export function allRouteChapters(): RouteChapter[] {
  return [...ROUTES];
}
