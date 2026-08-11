// A render-error boundary scoped to ONE panel.
//
// This app had no error boundary anywhere, which turned every render exception
// into a white screen: a single unclassified truck made `humanizeState(null)`
// throw inside one card, React unmounted the whole tree, and /live went blank
// with the shell still painted around it. The operator saw nothing and had no
// way to tell a crashed panel from an empty one.
//
// The boundary is deliberately NARROW. Wrapping a whole page would hide the
// failure and satisfy nobody — the point is that the other panels keep working
// and the broken one says so, by name, with its error text visible so a bug
// report can quote it. It is not a substitute for fixing the throw; it is what
// stops one panel's bug from costing the operator every other panel.
import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

interface Props {
  /** Panel name, shown in the fallback so the failure is identifiable. */
  name: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class PanelBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the stack in the console: the fallback is for the operator, this is
    // for whoever has to fix it.
    console.error(`[PanelBoundary] ${this.props.name} failed to render`, error, info);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div
        role="alert"
        className="min-w-0 rounded-lg border border-severity-critical/40 bg-severity-critical/5 p-3"
      >
        <p className="flex items-start gap-1.5 text-[12px] font-semibold text-severity-critical">
          <AlertTriangle className="mt-px h-4 w-4 shrink-0" aria-hidden />
          {this.props.name} could not be displayed
        </p>
        <p className="mt-1 break-words text-[11px] leading-snug text-muted-foreground">
          {error.message}
        </p>
        <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
          The rest of this screen is unaffected. This panel is showing an error rather than stale or
          partial figures.
        </p>
      </div>
    );
  }
}

export default PanelBoundary;
