// The hub's one way to show what a session or a project is doing: a pixel cube, three faces in three tones of one
// colour, drawn on 2px pixels like the projects' marks. app.js is a classic script, so this module hands it
// `window.hubOrb` once loaded (the name stays, so every call site does).
//   hubOrb(state, { size, label }) -> <svg class="orb cube orb-<state>">
// `state` is a hub state (working, needs, finished, shell, ended, closing, closed, offline, no-claude, error), what a
// working Claude is doing (reasoning, searching, using, compacting, retrying, background, waiting, idle), or dreaming.
// Colour comes from CSS `color` (app.css, .orb-<state>); every motion is a CSS animation in app.css, with a still
// variant under reduced motion that still differs by shape. Nothing runs for a cube that is off screen or gone.
const PX = 2;  // one pixel of the art, in CSS pixels: the projects' marks use the same grain
// Each state's look: its shape, and the order its pixels light in when it moves.
const LOOK = {
  working: "build", searching: "build", using: "build", background: "build", reasoning: "turn", compacting: "settle", closing: "settle",
  needs: "lift", waiting: "lift", finished: "lit", idle: "solid", shell: "solid", "no-claude": "solid",
  ended: "outline", closed: "outline", offline: "outline", error: "broken", retrying: "broken", dreaming: "shimmer",
};
const WORDS = { working: "Working", searching: "Searching", using: "Using a tool", background: "Working in the background", reasoning: "Thinking",
  compacting: "Compacting", closing: "Closing", needs: "Needs you", waiting: "Needs you", finished: "Done", idle: "Idle", shell: "Terminal",
  "no-claude": "Claude isn't running", ended: "Ended", closed: "Closed", offline: "Not connected", error: "Stopped with an error", retrying: "Retrying", dreaming: "Dreaming" };

// The cube, drawn by hand: at 7 and 8 pixels a computed isometric shape turns into a blob, so each size has its art.
// T top, L left, R right face. 7 for the 14px marks of the sidebar and the jump list, 8 for everything from 16px up.
const ART = {
  7: ["..TTT..", "TTTTTTT", "LLTTTRR", "LLLLRRR", "LLLLRRR", "LLLLRRR", ".LLLRR."],
  8: ["..TTTT..", "TTTTTTTT", "LLTTTTRR", "LLLLRRRR", "LLLLRRRR", "LLLLRRRR", "LLLLRRRR", "..LLRR.."],
};
const FACE = { T: "top", L: "left", R: "right" };
const grids = new Map();
function grid(n) {
  if (!grids.has(n)) grids.set(n, ART[n].flatMap((row, r) => [...row].map((ch, c) => FACE[ch] ? { c, r, face: FACE[ch] } : null)).filter(Boolean));
  return grids.get(n);
}
// On the outline: a pixel with a side that isn't the cube, or on the edges where the faces meet.
const edge = (cells, cell) => {
  const at = (c, r) => cells.find((o) => o.c === c && o.r === r);
  return [[1, 0], [-1, 0], [0, 1], [0, -1]].some(([dc, dr]) => !at(cell.c + dc, cell.r + dr) || at(cell.c + dc, cell.r + dr).face !== cell.face && cell.face !== "top");
};
// The order pixels light in: bottom up for a build, face by face for a turn, top down for settling, across for a shimmer.
const ORDER = {
  build: (cell, n) => (n - 1 - cell.r) * n + (cell.face === "left" ? cell.c : n - cell.c),
  turn: (cell, n) => ({ top: 0, right: 1, left: 2 })[cell.face] * n * n + cell.r * n + cell.c,
  settle: (cell, n) => cell.r * n + cell.c,
  shimmer: (cell, n) => cell.c + cell.r,
};

const SVG = "http://www.w3.org/2000/svg";
const shown = new Set();
const watch = "IntersectionObserver" in window
  ? new IntersectionObserver((seen) => seen.forEach((e) => e.target.classList.toggle("cube-away", !e.isIntersecting)))
  : null;
// Cubes a redraw took off the page are let go, so the observer holds only what is on it.
setInterval(() => { for (const svg of shown) if (!svg.isConnected) { if (watch) watch.unobserve(svg); shown.delete(svg); } }, 5000);

window.hubOrb = (state, { size = 14, label } = {}) => {
  const look = LOOK[state] || "solid";
  const n = size < 16 ? 7 : 8, px = Math.max(PX, Math.floor(size / n));  // whole pixels only: a bigger mark has bigger pixels
  const cells = grid(n);
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", `0 0 ${n} ${n}`);
  svg.setAttribute("width", n * px);
  svg.setAttribute("height", n * px);
  svg.setAttribute("shape-rendering", "crispEdges");
  svg.classList.add("orb", "cube", `orb-${state}`, `cube-${look}`);
  svg.dataset.orb = state;
  if (label === false) svg.setAttribute("aria-hidden", "true");
  else { svg.setAttribute("role", "img"); svg.setAttribute("aria-label", label || WORDS[state] || state); }
  const order = ORDER[look];
  const ranks = order ? [...new Set(cells.map((cell) => order(cell, n)))].sort((a, b) => a - b) : [];
  // What a failed cube has lost: a 2×2 block from the top of its right face, fallen to the corner below it.
  const lost = look === "broken" ? new Set(cells.filter((cell) => cell.face === "right" && cell.c >= n - 3 && cell.c < n - 1 && cell.r >= Math.floor(n / 4) && cell.r < Math.floor(n / 4) + 2).map((cell) => `${cell.c},${cell.r}`)) : null;
  const rect = (c, r, face, extra = "") => {
    const el = document.createElementNS(SVG, "rect");
    el.setAttribute("x", c); el.setAttribute("y", r); el.setAttribute("width", 1); el.setAttribute("height", 1);
    el.setAttribute("class", `${face}${extra}`);
    return el;
  };
  for (const cell of cells) {
    if (lost && lost.has(`${cell.c},${cell.r}`)) continue;
    const el = rect(cell.c, cell.r, cell.face, look === "outline" && !edge(cells, cell) ? " hollow" : "");
    if (order) {
      const at = ranks.indexOf(order(cell, n)) / ranks.length;
      el.style.setProperty("--i", at.toFixed(3));
      if (at >= 0.6) el.classList.add("late");  // what a still cube leaves unlit, under reduced motion
    }
    svg.append(el);
  }
  if (lost) [...lost].forEach((_, k) => svg.append(rect(n - 2 + (k % 2), n - 2 + Math.floor(k / 2), "right", " fallen")));
  if (look !== "solid" && look !== "outline" && look !== "lit") { shown.add(svg); if (watch) watch.observe(svg); }
  return svg;
};
window.dispatchEvent(new Event("hub-orbs-ready"));
