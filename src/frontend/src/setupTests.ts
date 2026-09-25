// Jest setup file, automatically picked up by Create React App (react-scripts).
// Adds custom jest-dom matchers such as toBeInTheDocument().
import "@testing-library/jest-dom";
import { TextDecoder, TextEncoder } from "util";

// react-router 7 uses TextEncoder/TextDecoder when its modules load; the
// jsdom environment of Jest 27 (react-scripts 5) does not provide them.
Object.assign(globalThis, { TextEncoder, TextDecoder });
