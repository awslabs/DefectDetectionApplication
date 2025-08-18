/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

import { RefObject, useEffect, useRef } from "react";

export type Point = {
  x: number;
  y: number;
};

/**
 * React hook for drawing on canvas.
 *
 * This sets up all necessary mouse listeners to react to a draw event for drawing simple
 * rectangle shapes on a canvas, and returns the canvas ref.
 *
 * The onDraw callback is called whenever the mouse is click-and-dragged, and the rectangle is drawn.
 * The draw event end when the mouse is released
 *
 */
export function useOnDraw(onDraw: {
  (
    canvas: RefObject<HTMLCanvasElement>,
    pointA: Point,
    previousPoint: Point,
  ): void;
  (
    arg0: RefObject<HTMLCanvasElement>,
    arg1: { x: number; y: number },
    arg2: { x: number; y: number },
  ): void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const isDrawingRef = useRef(false);

  useEffect(() => {
    initMouseMoveListener();
    initMouseDownListener();
    initMouseUpListener();
  }, []);

  const previousPointRef = useRef({ x: 0, y: 0 });

  /**
   * Checks if drawingMode is enabled, and if so, calls the onDraw callback.
   *
   * This can be extended to react to different click-and-drag events, for example, translate to shift
   * existing annotations.
   */
  function initMouseMoveListener() {
    const mouseMoveListener = (e: any) => {
      if (isDrawingRef.current) {
        const point = computePointInCanvas(e.clientX, e.clientY);

        if (onDraw) {
          onDraw(canvasRef, point, previousPointRef.current);
        }
      }
    };

    canvasRef?.current?.addEventListener("mousemove", mouseMoveListener);
  }

  /**
   * Add event listener to set drawingMode to true when a mouse is clicked on the canvas.
   *
   * This can be extended to enable drawing mode, translation mode (move annotations),
   * or any type of click-and-drag operation we need to support later.
   */
  function initMouseDownListener() {
    const listener = (e: any) => {
      isDrawingRef.current = true;
      previousPointRef.current = computePointInCanvas(e.clientX, e.clientY);
    };

    canvasRef?.current?.addEventListener("mousedown", listener);
  }

  /**
   * Add event listener to disable drawingMode mouse is released from canvas.
   *
   * This can be extended to only enable drawing mode, translation mode (move annotations),
   * or any type of click-and-drag operation we need to support later.
   */
  function initMouseUpListener() {
    const listener = () => {
      isDrawingRef.current = false;
    };

    canvasRef?.current?.addEventListener("mouseup", listener);
  }

  function computePointInCanvas(clientX: number, clientY: number) {
    if (canvasRef && canvasRef.current) {
      const canvasBoundingRect = canvasRef.current.getBoundingClientRect();

      return {
        x: clientX - canvasBoundingRect.left,
        y: clientY - canvasBoundingRect.top,
      };
    } else {
      return {
        x: 0,
        y: 0,
      };
    }
  }

  return canvasRef;
}
