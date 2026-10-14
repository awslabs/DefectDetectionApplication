/**
 * The compilation targets the portal offers, defined once. The Compilation
 * tab's target picker and the auto-compile pickers on Smart Import and Model
 * Import all read this list, so a target is offered everywhere or nowhere.
 * Before, the import pages kept their own copies, which lacked the ONNX
 * export target the Compilation tab offered.
 *
 * The ids are the backend's `compilation.COMPILATION_TARGETS` keys; the
 * import routes pass them to that handler unchanged. JetPack 4 (the bare
 * 'jetson-xavier' id) is retired.
 */
import type { MultiselectProps } from '@cloudscape-design/components';

export interface CompilationTarget {
  id: string;
  name: string;
  description: string;
  recommended: boolean;
}

export const COMPILATION_TARGETS: readonly CompilationTarget[] = [
  {
    id: 'jetson-xavier-jp5',
    name: 'NVIDIA Jetson Xavier / Orin (JetPack 5.x)',
    description: 'ARM64 Jetson Xavier or Orin on JetPack 5 — device runtime CUDA 11.4, TensorRT 8.5.2',
    recommended: true,
  },
  {
    id: 'jetson-xavier-jp6',
    name: 'NVIDIA Jetson Orin (JetPack 6.x)',
    description: 'ARM64 Jetson Orin on JetPack 6 — device runtime CUDA 12.2, TensorRT 8.6.2',
    recommended: false,
  },
  {
    id: 'x86_64-cpu',
    name: 'x86_64 CPU',
    description: 'Standard x86 64-bit CPU-only inference',
    recommended: true,
  },
  {
    id: 'x86_64-cuda',
    name: 'x86_64 with CUDA',
    description: 'x86 64-bit with NVIDIA GPU acceleration',
    recommended: false,
  },
  {
    id: 'arm64-cpu',
    name: 'ARM64 CPU',
    description: 'ARM 64-bit CPU-only inference (e.g., AWS Graviton)',
    recommended: false,
  },
  {
    id: 'onnx',
    name: 'ONNX Runtime (portable)',
    description: 'Export the trained model to ONNX (.onnx) for the pluggable ONNX Runtime engine — runs on Jetson/x86 without Neo/DLR. GPU acceleration (CUDA/TensorRT) is available on JetPack 5 and 6. ONNX is the vision route for JetPack 7. See docs/multi-runtime-inference.md.',
    recommended: false,
  },
];

/** The same targets as Multiselect options, for the import pages' auto-compile picker. */
export const COMPILATION_TARGET_OPTIONS: MultiselectProps.Option[] = COMPILATION_TARGETS.map(target => ({
  label: target.name,
  value: target.id,
  description: target.description,
}));
