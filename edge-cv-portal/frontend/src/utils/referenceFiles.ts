/**
 * Reference lists of a Bedrock Inference node's `reference_payload_path`
 * — the TypeScript mirror of the shared Python rules
 * (`workflow_core.anomaly_invocation.parse_reference_list`,
 * `reference_labels`, `reference_list_problems`, `parse_upload_line`,
 * `document_format_of`), so the Portal shows the same kinds, labels and
 * problem texts the validator reports and the device records.
 *
 * One reference per line; blank lines and surrounding spaces are ignored. A
 * line starting with `upload://` is a file uploaded in the workflow editor
 * (`upload://<64 lowercase hex>.<ext>`); a line starting with `s3://`,
 * `http://`, `https://` or `file://` is a fixed URI, a document when its
 * path ends in a document extension; any other line is a dotted path into
 * the trigger payload, always an image. A single image is labelled
 * "Reference image"; any other list "Reference 1", "Reference 2" and so on.
 *
 * The agreement is checked against the Python functions' own answers by
 * `referenceFiles.mirror.test.ts`, over the `referenceListCases` of the
 * fixture `edge-cv-portal/backend/tests/fixtures/inspection_tuning_cases.json`.
 */

export type ReferenceKind = 'image' | 'document';

/** One line of a reference list (`ReferenceSpec`). */
export interface ReferenceSpec {
  /** 1-based position in the list. */
  index: number;
  /** The trimmed line. */
  text: string;
  /** A fixed URI or an uploaded file, not a payload path. */
  static: boolean;
  uploaded: boolean;
  /** `{sha256}.{ext}` of a well-formed upload line, else null. */
  uploadName: string | null;
  kind: ReferenceKind;
  /** `jpeg` for an image, else the Converse document format. */
  format: string;
  label: string;
}

export const REFERENCE_IMAGE_LABEL = 'Reference image';
export const UPLOAD_REFERENCE_SCHEME = 'upload://';

/** `UPLOAD_LINE_RE` of the shared module. */
export const UPLOAD_LINE_RE =
  /^upload:\/\/([0-9a-f]{64})\.(jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx)$/;

/** The schemes of a fixed URI, matched case-sensitively. */
export const REFERENCE_URI_SCHEMES: readonly string[] = [
  's3://',
  'http://',
  'https://',
  'file://',
];

/** Upload extensions that are documents (already the Converse format). */
export const UPLOAD_DOCUMENT_EXTENSIONS: readonly string[] = [
  'pdf', 'txt', 'md', 'csv', 'html', 'doc', 'docx', 'xls', 'xlsx',
];

/** Extension of a fixed URI's path -> Converse document format. */
export const DOCUMENT_FORMATS_BY_EXTENSION: Readonly<Record<string, string>> = {
  '.pdf': 'pdf',
  '.txt': 'txt',
  '.md': 'md',
  '.markdown': 'md',
  '.csv': 'csv',
  '.html': 'html',
  '.htm': 'html',
  '.doc': 'doc',
  '.docx': 'docx',
  '.xls': 'xls',
  '.xlsx': 'xlsx',
};

/** Converse limits of one request. */
export const MAX_DOCUMENT_REFERENCES = 5;
export const MAX_IMAGE_REFERENCES = 19;

/** File extensions the workflow editor accepts for an upload. */
export const REFERENCE_IMAGE_EXTENSIONS: readonly string[] = ['.jpg', '.jpeg', '.png'];
export const REFERENCE_DOCUMENT_EXTENSIONS: readonly string[] = [
  '.pdf', '.txt', '.md', '.markdown', '.csv', '.html', '.htm',
  '.doc', '.docx', '.xls', '.xlsx',
];
export const ACCEPTED_REFERENCE_EXTENSIONS: readonly string[] = [
  ...REFERENCE_IMAGE_EXTENSIONS,
  ...REFERENCE_DOCUMENT_EXTENSIONS,
];

/** Upload size caps: an image as the device loads it, a document as Converse takes it. */
export const MAX_REFERENCE_IMAGE_BYTES = 8_388_608;
export const MAX_REFERENCE_DOCUMENT_BYTES = 4_500_000;

/** The exact string that selects the inspection format. */
const ANSWER_FORMAT_INSPECTION = 'inspection';

/** `[sha256, ext]` of a well-formed upload line, else null. */
export function parseUploadLine(text: unknown): [string, string] | null {
  if (typeof text !== 'string') return null;
  const match = UPLOAD_LINE_RE.exec(text);
  return match ? [match[1], match[2]] : null;
}

function isStaticReference(line: string): boolean {
  return REFERENCE_URI_SCHEMES.some((scheme) => line.startsWith(scheme));
}

/**
 * The Converse document format a line names, or null: an upload line's
 * document extension, or the extension of a fixed URI's path (before any
 * `?` or `#`, compared case-insensitively). Payload paths are never
 * documents.
 */
export function documentFormatOf(text: unknown): string | null {
  if (typeof text !== 'string') return null;
  const line = text.trim();
  const upload = parseUploadLine(line);
  if (upload) {
    return UPLOAD_DOCUMENT_EXTENSIONS.includes(upload[1]) ? upload[1] : null;
  }
  const scheme = REFERENCE_URI_SCHEMES.find((s) => line.startsWith(s));
  if (scheme === undefined) return null;
  const rest = line.slice(scheme.length).split(/[?#]/)[0];
  const slash = rest.indexOf('/');
  if (slash < 0) return null;
  // The extension as Python's posixpath.splitext reads it: leading dots of
  // the file name are not an extension.
  const name = rest.slice(slash).split('/').pop()!.replace(/^\.+/, '');
  const dot = name.lastIndexOf('.');
  const extension = dot >= 0 ? name.slice(dot).toLowerCase() : '';
  return DOCUMENT_FORMATS_BY_EXTENSION[extension] ?? null;
}

/** `["Reference image"]` for exactly one image, else "Reference 1".."Reference N". */
export function referenceLabels(kinds: readonly ReferenceKind[]): string[] {
  if (kinds.length === 1 && kinds[0] === 'image') return [REFERENCE_IMAGE_LABEL];
  return kinds.map((_kind, index) => `Reference ${index + 1}`);
}

/** The non-blank, trimmed lines of a `reference_payload_path` value. */
export function parseReferenceList(value: unknown): ReferenceSpec[] {
  if (typeof value !== 'string' || !value.trim()) return [];
  const lines = value
    .split(/\r\n|\r|\n/)
    .map((line) => line.trim())
    .filter((line) => line !== '');
  const entries = lines.map((line) => {
    const uploaded = line.startsWith(UPLOAD_REFERENCE_SCHEME);
    const upload = uploaded ? parseUploadLine(line) : null;
    const format = documentFormatOf(line);
    return {
      text: line,
      static: uploaded || isStaticReference(line),
      uploaded,
      uploadName: upload ? `${upload[0]}.${upload[1]}` : null,
      kind: (format !== null ? 'document' : 'image') as ReferenceKind,
      format: format ?? 'jpeg',
    };
  });
  const labels = referenceLabels(entries.map((entry) => entry.kind));
  return entries.map((entry, index) => ({
    index: index + 1,
    ...entry,
    label: labels[index],
  }));
}

/**
 * The statically known problems of a list, in the shared order: each
 * malformed upload line; a list or a document on a node that is not in
 * inspection format; too many documents; too many images.
 */
export function referenceListProblems(
  specs: readonly ReferenceSpec[],
  answerFormat: unknown
): string[] {
  const problems: string[] = [];
  for (const spec of specs) {
    if (spec.uploaded && spec.uploadName === null) {
      problems.push(
        `line ${spec.index} of reference_payload_path is not a valid `
          + 'uploaded-file reference: expected upload://<64 lowercase hex '
          + 'characters>.<jpg|png|pdf|txt|md|csv|html|doc|docx|xls|xlsx>'
      );
    }
  }
  const documents = specs.filter((spec) => spec.kind === 'document').length;
  const images = specs.length - documents;
  if (answerFormat !== ANSWER_FORMAT_INSPECTION && (specs.length > 1 || documents > 0)) {
    problems.push(
      `reference_payload_path lists ${specs.length} reference(s) or a `
        + 'document; only answer_format inspection accepts several '
        + 'references or documents'
    );
  }
  if (documents > MAX_DOCUMENT_REFERENCES) {
    problems.push(
      `reference_payload_path lists ${documents} documents; Bedrock Converse `
        + `accepts at most ${MAX_DOCUMENT_REFERENCES} per request`
    );
  }
  if (images > MAX_IMAGE_REFERENCES) {
    problems.push(
      `reference_payload_path lists ${images} images; with the input image `
        + `that is more than the ${MAX_IMAGE_REFERENCES + 1} images Bedrock `
        + 'Converse accepts per request'
    );
  }
  return problems;
}

/**
 * A reference file's size as the Portal shows it: below 1 MiB in whole
 * kilobytes, otherwise in megabytes with one decimal (12,288 bytes is
 * "12 KB", 182,004 bytes "178 KB", 4,500,000 bytes "4.3 MB").
 */
export function formatReferenceSize(bytes: number): string {
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}
