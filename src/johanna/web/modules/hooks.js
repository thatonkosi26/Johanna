// Late-bound references to higher layers, filled once by app.js after every module has
// evaluated. Keeps the module graph acyclic; nothing reads it during module evaluation.
export const hooks = {};
