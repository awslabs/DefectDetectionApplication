import fc from "fast-check";

// Design requirement: every fast-check property runs a minimum of 100
// iterations (see design.md "Correctness Properties" and tasks.md notes).
fc.configureGlobal({ numRuns: 100 });
