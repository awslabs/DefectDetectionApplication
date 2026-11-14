/**
 * The reference-files field of a Bedrock Inference node's
 * `reference_payload_path` parameter in the workflow editor.
 *
 * The parameter is a list, one reference per line, sent in order after the
 * input image: a fixed URI, a payload path, or a file uploaded here, which
 * appears as the line `upload://<sha256>.<ext>`. Each row shows the label
 * the request carries ("Reference image", or "Reference 1", "Reference 2"
 * and so on) and either the uploaded file's name, format, size and
 * dimensions, or a text input with the typed line. Rows move, are replaced
 * and removed; the value is the rows' lines joined by "\n", without empty
 * rows.
 *
 * An upload is a presigned PUT to a staging key followed by the Portal's
 * content check (`POST /workflows/{id}/reference-files/upload-url`, the PUT
 * with `uploadHeaders`, `POST /workflows/{id}/reference-files`). The URL is
 * SigV4 and signs only `host` and `x-amz-expected-bucket-owner`, so the
 * `Content-Type` the browser sets from the File cannot invalidate it.
 * Uploading needs a saved workflow (its id) and a role that may edit
 * workflows; other roles see the rows read-only.
 */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import FileUpload from '@cloudscape-design/components/file-upload';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { apiService, type ReferenceFileRecord } from '../../services/api';
import type { UserRole } from '../../types';
import {
  ACCEPTED_REFERENCE_EXTENSIONS,
  MAX_REFERENCE_DOCUMENT_BYTES,
  MAX_REFERENCE_IMAGE_BYTES,
  REFERENCE_IMAGE_EXTENSIONS,
  formatReferenceSize,
  parseReferenceList,
  parseUploadLine,
  referenceListProblems,
} from '../../utils/referenceFiles';
import { ANSWER_FORMAT_PARAMETER } from '../workflow-tuning/eligibility';
import { canEditWorkflows } from './WorkflowToolbar';
import type { JsonValue, ParameterDescriptor } from './types';

/** The Bedrock Inference parameter this field edits. */
export const REFERENCE_PAYLOAD_PATH_PARAMETER = 'reference_payload_path';

export const UPLOAD_FILE_TEXT = 'Upload file';
export const ADD_TYPED_LINE_TEXT = 'Add URI or payload path';
export const REFERENCE_FILES_HINT =
  'The prompt refers to these files by their labels. Uploaded files are shipped with the ' +
  'workflow and need no allowed_uri_prefixes entry; file:// lines do.';
export const MISSING_RECORD_TEXT =
  'This uploaded file is not stored in this workflow. Upload it again.';
export const SAVE_FIRST_TEXT = 'Save the workflow once to upload reference files.';
export const TYPED_LINE_PLACEHOLDER =
  'file:///…, s3://…, https://… or a payload path such as refs.0.image';

const ACCEPT = ACCEPTED_REFERENCE_EXTENSIONS.join(',');

const FILE_UPLOAD_I18N = {
  uploadButtonText: () => UPLOAD_FILE_TEXT,
  dropzoneText: () => 'Drop a file to upload',
  removeFileAriaLabel: (index: number) => `Remove file ${index + 1}`,
  errorIconAriaLabel: 'Error',
};

interface ReferenceRow {
  /** Stable identity of the row while it is edited (not saved). */
  key: number;
  /** The line as typed, or an uploaded file's `upload://` line. */
  text: string;
}

/** The rows' lines joined by "\n", each trimmed, empty rows dropped. */
export function serializeReferenceRows(rows: readonly { text: string }[]): string {
  return rows
    .map((row) => row.text.trim())
    .filter((text) => text !== '')
    .join('\n');
}

/** The file-name extension as the Portal reads it (".png"), lowercased. */
function extensionOf(fileName: string): string {
  const dot = fileName.lastIndexOf('.');
  return dot > 0 ? fileName.slice(dot).toLowerCase() : '';
}

/** "legend.png · PNG · 12 KB · 640 × 480 px" for an uploaded file's record. */
export function recordSummary(record: ReferenceFileRecord): string {
  const parts = [record.fileName, record.format.toUpperCase(), formatReferenceSize(record.bytes)];
  if (record.width !== undefined && record.height !== undefined) {
    parts.push(`${record.width} × ${record.height} px`);
  }
  return parts.join(' · ');
}

export interface ReferenceFilesFieldProps {
  /** The `reference_payload_path` parameter descriptor. */
  descriptor: ParameterDescriptor;
  /** The node's parameter descriptors (for the effective `answer_format`). */
  nodeParameterDescriptors: readonly ParameterDescriptor[];
  /** The FormField label. */
  label: ReactNode;
  /** The node's full parameters record. */
  parameters: Record<string, JsonValue>;
  /** The saved workflow's id; uploads are disabled while it is unknown. */
  workflowId?: string | null;
  /** The acting user's role; without workflow editing the rows are read-only. */
  role?: UserRole | null;
  /** Edits: the full updated parameters record. */
  onParametersChange: (parameters: Record<string, JsonValue>) => void;
}

export default function ReferenceFilesField({
  descriptor,
  nodeParameterDescriptors,
  label,
  parameters,
  workflowId,
  role,
  onParametersChange,
}: ReferenceFilesFieldProps) {
  const raw = Object.prototype.hasOwnProperty.call(parameters, descriptor.name)
    ? parameters[descriptor.name]
    : descriptor.default;
  const value = typeof raw === 'string' ? raw : '';
  const answerFormatDescriptor = nodeParameterDescriptors.find(
    (parameter) => parameter.name === ANSWER_FORMAT_PARAMETER
  );
  const answerFormat = Object.prototype.hasOwnProperty.call(parameters, ANSWER_FORMAT_PARAMETER)
    ? parameters[ANSWER_FORMAT_PARAMETER]
    : answerFormatDescriptor?.default;

  const keyCounter = useRef(0);
  const nextKey = () => {
    keyCounter.current += 1;
    return keyCounter.current;
  };
  const rowsFromValue = (text: string): ReferenceRow[] =>
    text
      .split(/\r\n|\r|\n/)
      .map((line) => line.trim())
      .filter((line) => line !== '')
      .map((line) => ({ key: nextKey(), text: line }));

  const [rows, setRows] = useState<ReferenceRow[]>(() => rowsFromValue(value));
  const [records, setRecords] = useState<Record<string, ReferenceFileRecord>>({});
  const [recordsLoaded, setRecordsLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Latest values for the asynchronous upload sequence.
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const parametersRef = useRef(parameters);
  parametersRef.current = parameters;
  const valueRef = useRef(value);
  valueRef.current = value;
  // The value this field emitted last; any other incoming value is an
  // outside change and resets the rows.
  const emitted = useRef(value);
  const loadStarted = useRef(false);
  const replaceKey = useRef<number | null>(null);
  const replaceInput = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (value !== emitted.current) {
      emitted.current = value;
      const next = rowsFromValue(value);
      rowsRef.current = next;
      setRows(next);
    }
    // rowsFromValue only allocates keys.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const editable = canEditWorkflows(role);
  const savedWorkflowId = typeof workflowId === 'string' && workflowId !== '' ? workflowId : null;
  const uploadNames = rows
    .map((row) => parseUploadLine(row.text.trim()))
    .filter((upload): upload is [string, string] => upload !== null)
    .map(([sha256, ext]) => `${sha256}.${ext}`);
  const unknownUpload = uploadNames.some((name) => records[name] === undefined);

  // The workflow's upload records, loaded once when a row names an uploaded
  // file this field has not stored itself.
  useEffect(() => {
    if (savedWorkflowId === null || !unknownUpload || loadStarted.current) {
      return;
    }
    loadStarted.current = true;
    apiService
      .listReferenceFiles(savedWorkflowId)
      .then(({ files }) => {
        setRecords((previous) => {
          const loaded: Record<string, ReferenceFileRecord> = {};
          for (const file of files) loaded[file.name] = file;
          return { ...loaded, ...previous };
        });
        setRecordsLoaded(true);
      })
      .catch((err: unknown) => {
        setError(
          err instanceof Error && err.message
            ? err.message
            : 'The uploaded files of this workflow could not be loaded'
        );
      });
  }, [savedWorkflowId, unknownUpload]);

  const commit = (next: ReferenceRow[]) => {
    rowsRef.current = next;
    setRows(next);
    const serialized = serializeReferenceRows(next);
    if (serialized !== valueRef.current) {
      emitted.current = serialized;
      onParametersChange({ ...parametersRef.current, [descriptor.name]: serialized });
    }
  };

  const move = (index: number, offset: number) => {
    const next = [...rowsRef.current];
    const [row] = next.splice(index, 1);
    next.splice(index + offset, 0, row);
    commit(next);
  };

  const upload = async (file: File, targetKey: number | null) => {
    if (savedWorkflowId === null) return;
    const extension = extensionOf(file.name);
    if (!ACCEPTED_REFERENCE_EXTENSIONS.includes(extension)) {
      setError(
        `${file.name} is not an accepted reference file. Accepted: ` +
          `${ACCEPTED_REFERENCE_EXTENSIONS.join(', ')}.`
      );
      return;
    }
    const limit = REFERENCE_IMAGE_EXTENSIONS.includes(extension)
      ? MAX_REFERENCE_IMAGE_BYTES
      : MAX_REFERENCE_DOCUMENT_BYTES;
    if (file.size > limit) {
      setError(
        `${file.name} is ${formatReferenceSize(file.size)}; files of this type may be at ` +
          `most ${formatReferenceSize(limit)}.`
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const target = await apiService.getReferenceFileUploadUrl(savedWorkflowId, file.name);
      // uploadHeaders carries x-amz-expected-bucket-owner, which the presign
      // signs: S3 refuses the PUT unless the portal account owns the bucket.
      const put = await fetch(target.uploadUrl, {
        method: 'PUT',
        body: file,
        headers: target.uploadHeaders ?? {},
      });
      if (!put.ok) {
        throw new Error(`Upload failed (HTTP ${put.status})`);
      }
      const { file: record } = await apiService.completeReferenceFileUpload(savedWorkflowId, {
        stagingKey: target.stagingKey,
        fileName: file.name,
      });
      setRecords((previous) => ({ ...previous, [record.name]: record }));
      const current = rowsRef.current;
      const replacing = targetKey !== null && current.some((row) => row.key === targetKey);
      commit(
        replacing
          ? current.map((row) => (row.key === targetKey ? { ...row, text: record.line } : row))
          : [...current, { key: nextKey(), text: record.line }]
      );
    } catch (err: unknown) {
      setError(err instanceof Error && err.message ? err.message : 'Upload failed');
    } finally {
      setBusy(false);
    }
  };

  const lines = rows.map((row) => row.text.trim());
  const specs = parseReferenceList(lines.filter((line) => line !== '').join('\n'));
  let specIndex = 0;
  const rowLabels = lines.map((line) => (line === '' ? 'New line' : specs[specIndex++].label));
  const problems = referenceListProblems(specs, answerFormat);
  const uploadDisabled = busy || savedWorkflowId === null;

  return (
    <FormField label={label} description={descriptor.description ?? undefined} stretch>
      <SpaceBetween size="xs">
        {rows.length === 0 && (
          <Box fontSize="body-s" color="text-body-secondary">
            No reference files.
          </Box>
        )}
        {rows.map((row, index) => {
          const line = lines[index];
          const uploadLine = parseUploadLine(line);
          const name = uploadLine ? `${uploadLine[0]}.${uploadLine[1]}` : null;
          const record = name ? records[name] : undefined;
          const lineNumber = index + 1;
          return (
            <div
              key={row.key}
              data-testid={`reference-row-${lineNumber}`}
              style={{ borderBottom: '1px solid #e9ebed', paddingBottom: 4 }}
            >
              <Box fontSize="body-s" fontWeight="bold">
                {rowLabels[index]}
              </Box>
              {uploadLine ? (
                <>
                  <Box fontSize="body-s">
                    {record
                      ? recordSummary(record)
                      : `upload://${uploadLine[0].slice(0, 12)}….${uploadLine[1]}`}
                  </Box>
                  {!record && recordsLoaded && (
                    <Box fontSize="body-s" color="text-status-warning">
                      {MISSING_RECORD_TEXT}
                    </Box>
                  )}
                </>
              ) : (
                <Input
                  value={row.text}
                  readOnly={!editable}
                  placeholder={TYPED_LINE_PLACEHOLDER}
                  ariaLabel={`Reference line ${lineNumber}`}
                  onChange={({ detail }) =>
                    commit(
                      rowsRef.current.map((r) => (r.key === row.key ? { ...r, text: detail.value } : r))
                    )
                  }
                />
              )}
              {editable && (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="inline-link"
                    ariaLabel={`Move up line ${lineNumber}`}
                    disabled={busy || index === 0}
                    onClick={() => move(index, -1)}
                  >
                    Move up
                  </Button>
                  <Button
                    variant="inline-link"
                    ariaLabel={`Move down line ${lineNumber}`}
                    disabled={busy || index === rows.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    Move down
                  </Button>
                  {uploadLine && (
                    <Button
                      variant="inline-link"
                      ariaLabel={`Replace line ${lineNumber}`}
                      disabled={uploadDisabled}
                      onClick={() => {
                        replaceKey.current = row.key;
                        replaceInput.current?.click();
                      }}
                    >
                      Replace
                    </Button>
                  )}
                  <Button
                    variant="inline-link"
                    ariaLabel={`Remove line ${lineNumber}`}
                    disabled={busy}
                    onClick={() => commit(rowsRef.current.filter((r) => r.key !== row.key))}
                  >
                    Remove
                  </Button>
                </SpaceBetween>
              )}
            </div>
          );
        })}
        {problems.map((problem) => (
          <Box key={problem} fontSize="body-s" color="text-status-error">
            {problem}
          </Box>
        ))}
        {error !== null && (
          <Alert type="error" statusIconAriaLabel="Error">
            {error}
          </Alert>
        )}
        {editable && (
          <SpaceBetween size="xs">
            {uploadDisabled ? (
              <Button iconName="upload" disabled>
                {UPLOAD_FILE_TEXT}
              </Button>
            ) : (
              <FileUpload
                value={[]}
                accept={ACCEPT}
                i18nStrings={FILE_UPLOAD_I18N}
                onChange={({ detail }) => {
                  const file = detail.value[0];
                  if (file) void upload(file, null);
                }}
              />
            )}
            {savedWorkflowId === null && (
              <Box fontSize="body-s" color="text-body-secondary">
                {SAVE_FIRST_TEXT}
              </Box>
            )}
            <Button
              iconName="add-plus"
              disabled={busy}
              onClick={() => {
                const next = [...rowsRef.current, { key: nextKey(), text: '' }];
                rowsRef.current = next;
                setRows(next);
              }}
            >
              {ADD_TYPED_LINE_TEXT}
            </Button>
            <input
              ref={replaceInput}
              type="file"
              accept={ACCEPT}
              hidden
              aria-label="Replacement reference file"
              data-testid="reference-replace-input"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = '';
                const key = replaceKey.current;
                replaceKey.current = null;
                if (file && key !== null) void upload(file, key);
              }}
            />
          </SpaceBetween>
        )}
        <Box fontSize="body-s" color="text-body-secondary">
          {REFERENCE_FILES_HINT}
        </Box>
      </SpaceBetween>
    </FormField>
  );
}
