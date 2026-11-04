/**
 * Isolates a rendering failure of the assistant to the drawer (spec
 * portal-ai-assistant Requirement 1.4): the page keeps rendering, the drawer
 * shows a compact error with Retry.
 */
import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';
import { Alert, Button } from '@cloudscape-design/components';

interface Props {
  children: ReactNode;
}

interface State {
  failed: boolean;
}

export default class AssistantErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The message only; never the conversation content.
    console.error('assistant drawer failed to render', error.name, info.componentStack?.split('\n')[1]);
  }

  render(): ReactNode {
    if (this.state.failed) {
      return (
        <Alert
          type="error"
          header="The assistant hit an error"
          action={<Button onClick={() => this.setState({ failed: false })}>Retry</Button>}
        >
          The rest of the page is unaffected.
        </Alert>
      );
    }
    return this.props.children;
  }
}
