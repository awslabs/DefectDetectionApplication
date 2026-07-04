import { useEffect, useState } from 'react';
import { subscribe } from '../services/loadingBus';

/**
 * App-wide activity indicator: a thin indeterminate progress bar fixed to the
 * very top of the viewport, shown whenever one or more API requests are in
 * flight. Gives the user immediate feedback that a page/data is still loading.
 *
 * A short hide delay avoids flicker for very fast back-to-back requests.
 */
export default function GlobalLoadingBar() {
  const [active, setActive] = useState(false);

  useEffect(() => {
    let hideTimer: ReturnType<typeof setTimeout> | undefined;
    const unsub = subscribe((count) => {
      if (count > 0) {
        if (hideTimer) {
          clearTimeout(hideTimer);
          hideTimer = undefined;
        }
        setActive(true);
      } else {
        // Small delay so rapid sequential calls don't flash the bar off/on.
        hideTimer = setTimeout(() => setActive(false), 250);
      }
    });
    return () => {
      if (hideTimer) clearTimeout(hideTimer);
      unsub();
    };
  }, []);

  if (!active) return null;

  return (
    <>
      <style>{`
        @keyframes dda-global-loading-slide {
          0%   { left: -40%; width: 40%; }
          50%  { left: 30%;  width: 45%; }
          100% { left: 100%; width: 40%; }
        }
      `}</style>
      <div
        role="progressbar"
        aria-label="Loading"
        aria-busy="true"
        style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          height: 3,
          zIndex: 10000,
          overflow: 'hidden',
          backgroundColor: 'rgba(9, 114, 211, 0.15)',
          pointerEvents: 'none',
        }}
      >
        <div
          style={{
            position: 'absolute',
            top: 0,
            height: '100%',
            backgroundColor: '#0972d3',
            borderRadius: 2,
            animation: 'dda-global-loading-slide 1.1s ease-in-out infinite',
          }}
        />
      </div>
    </>
  );
}
