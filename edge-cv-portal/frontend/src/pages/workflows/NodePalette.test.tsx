/**
 * Component tests for the Node_Palette sidebar (Requirements 1.1, 1.2):
 * grouping into the five categories and HTML5 drag payloads.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import NodePalette, { PALETTE_DRAG_MIME } from './NodePalette';
import type { NodeTypeDescriptor } from './types';

function descriptor(
  typeId: string,
  category: string,
  displayName: string
): NodeTypeDescriptor {
  return {
    typeId,
    category,
    displayName,
    inputs: [],
    outputs: [],
    parameters: [],
    mappings: [],
    hardwareDependent: false,
  };
}

const CATALOG: NodeTypeDescriptor[] = [
  descriptor('camera_source', 'input', 'Camera source'),
  descriptor('crop', 'preprocessing', 'Crop'),
  descriptor('custom_python_preprocess', 'preprocessing', 'Custom Python (Frames)'),
  descriptor('model_inference', 'inference', 'Model inference'),
  descriptor('llm_inference', 'inference', 'VLM/LLM Inference'),
  descriptor('inference_filter', 'post_processing', 'Inference filter'),
  descriptor('mqtt_publish', 'output', 'MQTT publish'),
];

describe('NodePalette', () => {
  it('renders the five category sections in order', () => {
    render(<NodePalette catalog={CATALOG} />);
    const palette = screen.getByRole('navigation', { name: 'Node palette' });
    const sections = ['Input', 'Preprocessing', 'Model inference', 'Post-processing', 'Output'];
    for (const label of sections) {
      expect(within(palette).getByRole('region', { name: label })).toBeInTheDocument();
    }
  });

  it('lists each node type under its category', () => {
    render(<NodePalette catalog={CATALOG} />);
    const inputSection = screen.getByRole('region', { name: 'Input' });
    expect(within(inputSection).getByText('Camera source')).toBeInTheDocument();
    const outputSection = screen.getByRole('region', { name: 'Output' });
    expect(within(outputSection).getByText('MQTT publish')).toBeInTheDocument();
  });

  it('renders the custom_python_preprocess node in the Preprocessing section (custom-python-frames Requirement 7.1)', () => {
    render(<NodePalette catalog={CATALOG} />);
    const preprocessingSection = screen.getByRole('region', { name: 'Preprocessing' });
    expect(within(preprocessingSection).getByText('Custom Python (Frames)')).toBeInTheDocument();
    // It appears only under Preprocessing, not in any other section.
    expect(screen.getAllByText('Custom Python (Frames)')).toHaveLength(1);
  });

  it('renders the llm_inference node with the "VLM/LLM Inference" label (Bug 4, Requirement 2.4)', () => {
    render(<NodePalette catalog={CATALOG} />);
    const inferenceSection = screen.getByRole('region', { name: 'Model inference' });
    expect(within(inferenceSection).getByText('VLM/LLM Inference')).toBeInTheDocument();
    // The palette renders the catalog display label verbatim, so the old
    // "LLM Inference" label no longer appears anywhere.
    expect(screen.queryByText('LLM Inference')).not.toBeInTheDocument();
  });

  it('sets the node type id as the drag payload', () => {
    render(<NodePalette catalog={CATALOG} />);
    const item = screen.getByRole('listitem', { name: 'Camera source node type' });
    expect(item).toHaveAttribute('draggable', 'true');

    const setData = vi.fn();
    fireEvent.dragStart(item, { dataTransfer: { setData, effectAllowed: '' } });
    expect(setData).toHaveBeenCalledWith(PALETTE_DRAG_MIME, 'camera_source');
  });
});
