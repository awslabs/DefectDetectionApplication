/*
 * The LocalServer's Stream_URL rules are a verbatim copy of the Portal's
 * (rtsp-rtmp-stream-cameras Requirements 4.1, 4.2). The Portal copy is
 * checked against the Python source of truth by its parity test; this test
 * keeps the two TypeScript copies identical past their header comments, so
 * the station form and the API always agree.
 *
 * The Portal tree is absent when the LocalServer frontend is built on its
 * own, and the comparison is skipped then.
 */
import * as fs from "fs";
import * as path from "path";
import { SCHEMES_BY_SOURCE_TYPE, checkStreamUrl, normalizeStreamUrl } from "./streamUrl";

const PORTAL_COPY = path.resolve(
  __dirname,
  "../../../../../edge-cv-portal/frontend/src/pages/workflows/streamUrl.ts",
);
const LOCAL_COPY = path.resolve(__dirname, "streamUrl.ts");

/** The file from its first top-level statement on (header comments dropped). */
function body(file: string): string {
  const lines = fs.readFileSync(file, "utf8").split("\n");
  const start = lines.findIndex((line) => /^(import|export|const|function)\b/.test(line));
  if (start < 0) {
    throw new Error(`${file} has no top-level statement`);
  }
  // The doc comment right above the first statement belongs to the body.
  let docStart = start;
  if (lines[start - 1]?.trim() === "*/") {
    docStart = start - 1;
    while (docStart > 0 && !lines[docStart].trim().startsWith("/**")) {
      docStart -= 1;
    }
  }
  return lines.slice(docStart).join("\n");
}

const describeIfPortal = fs.existsSync(PORTAL_COPY) ? describe : describe.skip;

describeIfPortal("streamUrl.ts is the Portal's copy", () => {
  it("matches the Portal file past the header comments", () => {
    expect(body(LOCAL_COPY)).toEqual(body(PORTAL_COPY));
  });
});

describe("the LocalServer copy's Image_Source schemes", () => {
  it("accepts rtsp and rtsps for RTSP, rtmp and rtmps for RTMP", () => {
    expect(SCHEMES_BY_SOURCE_TYPE.RTSP).toEqual(["rtsp", "rtsps"]);
    expect(SCHEMES_BY_SOURCE_TYPE.RTMP).toEqual(["rtmp", "rtmps"]);
    expect(checkStreamUrl("rtsps://cam.local/live", SCHEMES_BY_SOURCE_TYPE.RTSP)).toBeNull();
    expect(checkStreamUrl("rtmp://media.local/live/a", SCHEMES_BY_SOURCE_TYPE.RTMP)).toBeNull();
    expect(
      checkStreamUrl("rtmp://media.local/live/a", SCHEMES_BY_SOURCE_TYPE.RTSP)?.code,
    ).toBeDefined();
    expect(normalizeStreamUrl("  rtsp://cam.local/live ")).toBe("rtsp://cam.local/live");
  });
});
