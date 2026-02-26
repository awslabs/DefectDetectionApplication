import { useState } from 'react';
import api from '../services/api';

interface ValidationIssue {
  type: string;
  severity: 'error' | 'warning' | 'info';
  message: string;
  example?: string;
  fix?: string;
}

interface ValidationStats {
  total_entries: number;
  entries_with_masks: number;
  entries_with_labels: number;
  unique_labels: string[];
  timestamp_colon_issues: number;
}

interface ValidationResult {
  valid: boolean;
  issues: ValidationIssue[];
  warnings: ValidationIssue[];
  stats: ValidationStats;
  manifest_type: string;
  needs_transformation: boolean;
  detected_attributes: {
    label: string | null;
    mask: string | null;
    metadata: string | null;
  };
}

export default function ManifestValidator() {
  const [manifestPath, setManifestPath] = useState('');
  const [region, setRegion] = useState('us-east-1');
  const [loading, setLoading] = useState(false);
  const [validationResult, setValidationResult] = useState<ValidationResult | null>(null);
  const [transformResult, setTransformResult] = useState<any>(null);
  const [activeTab, setActiveTab] = useState<'validate' | 'transform' | 'compare'>('validate');

  const handleValidate = async () => {
    setLoading(true);
    try {
      const response = await api.manifestValidator({
        action: 'validate',
        manifestPath,
        usecaseId: region,
      });
      setValidationResult(response.data);
      setActiveTab('validate');
    } catch (error: any) {
      alert(`Validation failed: ${error.response?.data?.error || error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleTransform = async () => {
    setLoading(true);
    try {
      const response = await api.manifestValidator({
        action: 'transform',
        manifestPath,
        usecaseId: region,
      });
      setTransformResult(response.data);
      setActiveTab('compare');
    } catch (error: any) {
      alert(`Transformation failed: ${error.response?.data?.error || error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleFixTimestamps = async () => {
    if (!confirm('This will modify the manifest in S3. Continue?')) return;
    
    setLoading(true);
    try {
      const response = await api.manifestValidator({
        action: 'fix_timestamps',
        manifestPath,
        usecaseId: region,
      });
      alert(`Fixed ${response.data.changes_made} entries with timestamp issues`);
      // Re-validate after fix
      await handleValidate();
    } catch (error: any) {
      alert(`Fix failed: ${error.response?.data?.error || error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const handleValidateAndTransform = async () => {
    setLoading(true);
    try {
      const response = await api.manifestValidator({
        action: 'validate_and_transform',
        manifestPath,
        usecaseId: region,
      });
      setValidationResult(response.data.validation);
      setTransformResult(response.data.transformation);
      setActiveTab('compare');
    } catch (error: any) {
      alert(`Workflow failed: ${error.response?.data?.error || error.message}`);
    } finally {
      setLoading(false);
    }
  };

  const getSeverityColor = (severity: string) => {
    switch (severity) {
      case 'error': return 'text-red-600 bg-red-50';
      case 'warning': return 'text-yellow-600 bg-yellow-50';
      case 'info': return 'text-blue-600 bg-blue-50';
      default: return 'text-gray-600 bg-gray-50';
    }
  };

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Manifest Validator & Transformer</h1>
        <p className="mt-2 text-sm text-gray-600">
          Validate Ground Truth manifests, fix common issues, and transform to DDA format
        </p>
      </div>

      {/* Input Section */}
      <div className="bg-white shadow rounded-lg p-6 mb-6">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Manifest S3 Path
            </label>
            <input
              type="text"
              value={manifestPath}
              onChange={(e) => setManifestPath(e.target.value)}
              placeholder="s3://bucket-name/path/to/output.manifest"
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Region
            </label>
            <select
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="us-east-1">us-east-1</option>
              <option value="us-east-2">us-east-2</option>
              <option value="us-west-2">us-west-2</option>
              <option value="eu-west-1">eu-west-1</option>
            </select>
          </div>

          <div className="flex gap-3">
            <button
              onClick={handleValidate}
              disabled={loading || !manifestPath}
              className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              {loading ? 'Validating...' : 'Validate'}
            </button>

            <button
              onClick={handleTransform}
              disabled={loading || !manifestPath}
              className="px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              Transform
            </button>

            <button
              onClick={handleValidateAndTransform}
              disabled={loading || !manifestPath}
              className="px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              Validate & Transform
            </button>

            {validationResult?.stats?.timestamp_colon_issues && validationResult.stats.timestamp_colon_issues > 0 && (
              <button
                onClick={handleFixTimestamps}
                disabled={loading}
                className="px-4 py-2 bg-orange-600 text-white rounded-md hover:bg-orange-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
              >
                Fix Timestamps
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Tabs */}
      {(validationResult || transformResult) && (
        <div className="bg-white shadow rounded-lg">
          <div className="border-b border-gray-200">
            <nav className="flex -mb-px">
              <button
                onClick={() => setActiveTab('validate')}
                className={`px-6 py-3 text-sm font-medium ${
                  activeTab === 'validate'
                    ? 'border-b-2 border-blue-500 text-blue-600'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
              >
                Validation Results
              </button>
              <button
                onClick={() => setActiveTab('compare')}
                className={`px-6 py-3 text-sm font-medium ${
                  activeTab === 'compare'
                    ? 'border-b-2 border-blue-500 text-blue-600'
                    : 'text-gray-500 hover:text-gray-700'
                }`}
                disabled={!transformResult}
              >
                Before/After Comparison
              </button>
            </nav>
          </div>

          <div className="p-6">
            {/* Validation Tab */}
            {activeTab === 'validate' && validationResult && (
              <div className="space-y-6">
                {/* Status Banner */}
                <div className={`p-4 rounded-md ${validationResult.valid ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
                  <div className="flex items-center">
                    <span className={`text-lg font-semibold ${validationResult.valid ? 'text-green-800' : 'text-red-800'}`}>
                      {validationResult.valid ? '✓ Manifest is valid' : '✗ Manifest has issues'}
                    </span>
                  </div>
                </div>

                {/* Stats */}
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  <div className="bg-gray-50 p-4 rounded-md">
                    <div className="text-2xl font-bold text-gray-900">{validationResult.stats.total_entries}</div>
                    <div className="text-sm text-gray-600">Total Entries</div>
                  </div>
                  <div className="bg-gray-50 p-4 rounded-md">
                    <div className="text-2xl font-bold text-gray-900">{validationResult.stats.entries_with_masks}</div>
                    <div className="text-sm text-gray-600">With Masks</div>
                  </div>
                  <div className="bg-gray-50 p-4 rounded-md">
                    <div className="text-2xl font-bold text-gray-900">{validationResult.stats.entries_with_labels}</div>
                    <div className="text-sm text-gray-600">With Labels</div>
                  </div>
                  <div className="bg-gray-50 p-4 rounded-md">
                    <div className="text-2xl font-bold text-gray-900">{validationResult.stats.timestamp_colon_issues}</div>
                    <div className="text-sm text-gray-600">Timestamp Issues</div>
                  </div>
                </div>

                {/* Manifest Type */}
                <div className="bg-blue-50 p-4 rounded-md border border-blue-200">
                  <div className="font-medium text-blue-900">Manifest Type</div>
                  <div className="text-sm text-blue-700 mt-1">{validationResult.manifest_type}</div>
                  {validationResult.needs_transformation && (
                    <div className="text-sm text-blue-600 mt-2">
                      ℹ️ This manifest needs transformation before training
                    </div>
                  )}
                </div>

                {/* Issues */}
                {validationResult.issues.length > 0 && (
                  <div>
                    <h3 className="text-lg font-semibold text-gray-900 mb-3">Issues</h3>
                    <div className="space-y-3">
                      {validationResult.issues.map((issue, idx) => (
                        <div key={idx} className={`p-4 rounded-md border ${getSeverityColor(issue.severity)}`}>
                          <div className="font-medium">{issue.message}</div>
                          {issue.example && (
                            <div className="text-sm mt-2 font-mono bg-white p-2 rounded overflow-x-auto">
                              {issue.example}
                            </div>
                          )}
                          {issue.fix && (
                            <div className="text-sm mt-2">
                              <span className="font-medium">Fix:</span> {issue.fix}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Warnings */}
                {validationResult.warnings.length > 0 && (
                  <div>
                    <h3 className="text-lg font-semibold text-gray-900 mb-3">Warnings</h3>
                    <div className="space-y-3">
                      {validationResult.warnings.map((warning, idx) => (
                        <div key={idx} className={`p-4 rounded-md border ${getSeverityColor(warning.severity)}`}>
                          <div className="font-medium">{warning.message}</div>
                          {warning.fix && (
                            <div className="text-sm mt-2">
                              <span className="font-medium">Note:</span> {warning.fix}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Comparison Tab */}
            {activeTab === 'compare' && transformResult && (
              <div className="space-y-6">
                <div className="bg-green-50 p-4 rounded-md border border-green-200">
                  <div className="font-medium text-green-900">
                    ✓ Transformation Complete
                  </div>
                  <div className="text-sm text-green-700 mt-1">
                    Showing first 3 entries. Total: {transformResult.comparison.total_entries}
                  </div>
                </div>

                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  {/* Before */}
                  <div>
                    <h3 className="text-lg font-semibold text-gray-900 mb-3">Before (Ground Truth)</h3>
                    <div className="bg-gray-50 p-4 rounded-md border border-gray-200 overflow-x-auto">
                      <pre className="text-xs">
                        {JSON.stringify(transformResult.comparison.before, null, 2)}
                      </pre>
                    </div>
                  </div>

                  {/* After */}
                  <div>
                    <h3 className="text-lg font-semibold text-gray-900 mb-3">After (DDA Format)</h3>
                    <div className="bg-green-50 p-4 rounded-md border border-green-200 overflow-x-auto">
                      <pre className="text-xs">
                        {JSON.stringify(transformResult.comparison.after, null, 2)}
                      </pre>
                    </div>
                  </div>
                </div>

                {transformResult.saved_path && (
                  <div className="bg-blue-50 p-4 rounded-md border border-blue-200">
                    <div className="font-medium text-blue-900">Saved To</div>
                    <div className="text-sm text-blue-700 mt-1 font-mono">{transformResult.saved_path}</div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
