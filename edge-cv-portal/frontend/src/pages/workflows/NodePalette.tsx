/**
 * Node_Palette sidebar: available node types grouped into the five
 * categories (input, preprocessing, model inference, post-processing,
 * output), each item draggable via HTML5 drag-and-drop onto the canvas
 * (Requirements 1.1, 1.2).
 */

import Box from '@cloudscape-design/components/box';
import { categoryMeta, CATEGORY_META } from './builderGraph';
import { CATEGORIES, type NodeTypeDescriptor } from './types';

/** dataTransfer type carrying the dragged node type id. */
export const PALETTE_DRAG_MIME = 'application/x-dda-workflow-node-type';

function PaletteItem({ descriptor }: { descriptor: NodeTypeDescriptor }) {
  const meta = categoryMeta(descriptor.category);
  return (
    <div
      draggable
      role="listitem"
      aria-label={`${descriptor.displayName} node type`}
      onDragStart={(event) => {
        event.dataTransfer.setData(PALETTE_DRAG_MIME, descriptor.typeId);
        event.dataTransfer.effectAllowed = 'move';
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '6px 8px',
        marginBottom: 4,
        background: '#ffffff',
        border: '1px solid #d1d5db',
        borderLeft: `4px solid ${meta.color}`,
        borderRadius: 4,
        cursor: 'grab',
        fontSize: 13,
        userSelect: 'none',
      }}
    >
      {descriptor.displayName}
    </div>
  );
}

export interface NodePaletteProps {
  catalog: NodeTypeDescriptor[];
}

export default function NodePalette({ catalog }: NodePaletteProps) {
  return (
    <nav
      aria-label="Node palette"
      style={{
        width: 240,
        flexShrink: 0,
        overflowY: 'auto',
        padding: 8,
        background: '#fafafa',
        borderRight: '1px solid #d1d5db',
      }}
    >
      {CATEGORIES.map((category) => {
        const items = catalog.filter((descriptor) => descriptor.category === category);
        return (
          <section key={category} aria-label={CATEGORY_META[category].label}>
            <Box variant="h4" padding={{ top: 's', bottom: 'xxs' }}>
              {CATEGORY_META[category].label}
            </Box>
            <div role="list">
              {items.map((descriptor) => (
                <PaletteItem key={descriptor.typeId} descriptor={descriptor} />
              ))}
            </div>
          </section>
        );
      })}
    </nav>
  );
}
