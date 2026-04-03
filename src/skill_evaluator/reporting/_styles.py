"""Shared CSS, JS, and SVG icons for self-contained HTML evaluation reports.

Provides the SkillForge-inspired design system as plain string constants
that can be injected into any HTML report. Uses CSS custom properties for
3-theme support (DBX Dark, Neon, Light) with no external dependencies.
"""

from __future__ import annotations


def score_color(score: float) -> str:
    """Return a CSS variable reference for score-based coloring."""
    if score >= 0.8:
        return "var(--success)"
    elif score >= 0.5:
        return "var(--warning)"
    return "var(--error)"


def score_color_class(score: float) -> str:
    """Return a CSS class suffix for score-based coloring."""
    if score >= 0.8:
        return "pass"
    elif score >= 0.5:
        return "warn"
    return "fail"


# ---------------------------------------------------------------------------
# Theme toggle JavaScript
# ---------------------------------------------------------------------------

THEME_JS = """
(function() {
  var THEMES = ['dbx-dark', 'neon', 'light'];
  var LABELS = {'dbx-dark': 'DBX Dark', 'neon': 'Neon', 'light': 'Light'};
  var current = 'dbx-dark';
  try { current = localStorage.getItem('dse-theme') || 'dbx-dark'; } catch(e) {}

  function apply(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    try { localStorage.setItem('dse-theme', mode); } catch(e) {}
    current = mode;
    var btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = LABELS[mode];
  }
  apply(current);

  window.toggleTheme = function() {
    var idx = THEMES.indexOf(current);
    apply(THEMES[(idx + 1) % THEMES.length]);
  };
})();
"""

# ---------------------------------------------------------------------------
# SVG icon definitions (referenced via <svg><use href="#icon-name"/></svg>)
# ---------------------------------------------------------------------------

SVG_ICONS = """
<svg xmlns="http://www.w3.org/2000/svg" style="display:none">
  <symbol id="icon-gauge" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 16v-4"/><path d="M12 8h.01"/>
    <circle cx="12" cy="12" r="10"/>
  </symbol>
  <symbol id="icon-layers" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 2 2 7l10 5 10-5-10-5Z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>
  </symbol>
  <symbol id="icon-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
  </symbol>
  <symbol id="icon-hash" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/>
  </symbol>
  <symbol id="icon-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
  </symbol>
  <symbol id="icon-x" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>
  </symbol>
  <symbol id="icon-bulb" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/>
    <path d="M9 18h6"/><path d="M10 22h4"/>
  </symbol>
  <symbol id="icon-palette" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/>
    <circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/>
    <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.9 0 1.7-.7 1.7-1.5 0-.4-.2-.7-.4-1-.2-.3-.3-.7-.3-1 0-.8.7-1.5 1.5-1.5H16c3.3 0 6-2.7 6-6 0-5.5-4.5-10-10-10z"/>
  </symbol>
  <symbol id="icon-brain" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>
    <path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>
    <path d="M12 5v13"/>
  </symbol>
  <symbol id="icon-target" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>
  </symbol>
  <symbol id="icon-flask" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M9 3h6"/><path d="M10 9V3"/><path d="M14 9V3"/>
    <path d="M7.5 21h9"/><path d="M5.2 16.7 10 9h4l4.8 7.7a1 1 0 0 1-.8 1.3H6a1 1 0 0 1-.8-1.3z"/>
  </symbol>
</svg>
"""

# ---------------------------------------------------------------------------
# Full CSS with 3 themes + component classes
# ---------------------------------------------------------------------------

THEME_CSS = """
/* ── Theme: DBX Dark (default) ── */
[data-theme="dbx-dark"], :root {
  --bg-l0: #11171c;
  --bg-l1: #1f272d;
  --bg-l2: #283035;
  --bg-l3: #34414b;
  --bg-l4: #37444f;
  --text-primary: #e8ecf0;
  --text-secondary: #92a4b3;
  --text-muted: #5f7281;
  --text-link: #8acaff;
  --accent: #4299e0;
  --accent-hover: #8acaff;
  --success: #3ba65e;
  --warning: #de7921;
  --error: #e65b77;
  --info: #4299e0;
  --border: #283035;
}

/* ── Theme: Neon ── */
[data-theme="neon"] {
  --bg-l0: oklch(0.08 0.03 260);
  --bg-l1: oklch(0.12 0.04 260);
  --bg-l2: oklch(0.16 0.05 260);
  --bg-l3: oklch(0.20 0.06 260);
  --bg-l4: oklch(0.16 0.05 260);
  --text-primary: oklch(0.96 0.02 260);
  --text-secondary: oklch(0.62 0.06 260);
  --text-muted: oklch(0.45 0.08 260);
  --text-link: oklch(0.68 0.18 230);
  --accent: oklch(0.88 0.18 165);
  --accent-hover: oklch(0.75 0.16 165);
  --success: oklch(0.88 0.18 165);
  --warning: oklch(0.62 0.28 330);
  --error: oklch(0.62 0.24 10);
  --info: oklch(0.68 0.18 230);
  --border: oklch(0.16 0.05 260);
}

/* ── Theme: Light ── */
[data-theme="light"] {
  --bg-l0: #FFFFFF;
  --bg-l1: #F7F9FC;
  --bg-l2: #F0F2F5;
  --bg-l3: #E8EBF0;
  --bg-l4: #E5E7EB;
  --text-primary: #1A1A1A;
  --text-secondary: #6B7280;
  --text-muted: #9CA3AF;
  --text-link: #2563EB;
  --accent: #FF6A00;
  --accent-hover: #FF8A33;
  --success: #16A34A;
  --warning: #CA8A04;
  --error: #DC2626;
  --info: #2563EB;
  --border: #E5E7EB;
}

/* ── Reset & base ── */
* { box-sizing: border-box; margin: 0; padding: 0; }
html { -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg-l0); color: var(--text-primary);
  max-width: 1200px; margin: 0 auto; padding: 0 20px 40px;
  transition: background-color 0.2s ease, color 0.2s ease;
}
::selection { background: color-mix(in srgb, var(--accent) 30%, transparent); }
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--bg-l3); border-radius: 3px; }

/* ── Typography ── */
.mono { font-family: ui-monospace, 'SF Mono', 'Fira Code', 'Cascadia Code', monospace; }

/* ── Top bar ── */
.top-bar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 10px;
  padding: 12px 0; margin-bottom: 20px;
  background: var(--bg-l0);
  border-bottom: 1px solid var(--border);
}
.top-bar-title { font-size: 15px; font-weight: 700; color: var(--text-primary); letter-spacing: -0.02em; }
.top-bar-meta { font-size: 11px; color: var(--text-muted); margin-left: auto; }

/* ── Buttons ── */
.btn-primary {
  padding: 6px 14px; font-size: 12px; font-weight: 500;
  background: var(--accent); color: #fff; border: none;
  border-radius: 4px; cursor: pointer; font-family: inherit;
  transition: background 0.15s ease;
}
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 5px 12px; font-size: 11px; font-weight: 500;
  color: var(--text-secondary); background: transparent;
  border: 1px solid var(--border); border-radius: 4px;
  cursor: pointer; font-family: inherit;
  transition: all 0.15s ease;
}
.btn-secondary:hover { border-color: var(--accent); color: var(--accent); }

/* ── Section titles ── */
.section { display: flex; flex-direction: column; gap: 10px; margin-bottom: 20px; }
.section-title {
  font-size: 11px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.5px;
}

/* ── Cards ── */
.card {
  display: flex; flex-direction: column; gap: 8px;
  padding: 14px; background: var(--bg-l1);
  border: 1px solid var(--border); border-radius: 6px;
  animation: card-reveal 0.3s ease-out both;
}
.card-header { display: flex; align-items: center; gap: 6px; }
.card-header .icon { color: var(--text-muted); width: 14px; height: 14px; flex-shrink: 0; }
.card-title { font-size: 12px; font-weight: 600; color: var(--text-primary); }
.card-body { display: flex; flex-direction: column; gap: 4px; }
.card-row { display: flex; align-items: center; gap: 6px; font-size: 11px; }
.card-label { color: var(--text-muted); min-width: 50px; }
.card-value { color: var(--text-secondary); font-family: ui-monospace, 'SF Mono', monospace; }

/* ── Metric grid ── */
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; }
.hero-metric {
  font-size: 28px; font-weight: 700;
  font-family: ui-monospace, 'SF Mono', 'Fira Code', monospace;
  line-height: 1.2;
}
.hero-metric.color-pass { color: var(--success); }
.hero-metric.color-warn { color: var(--warning); }
.hero-metric.color-fail { color: var(--error); }

/* ── Badges ── */
.badge {
  display: inline-flex; align-items: center;
  padding: 2px 8px; border-radius: 10px;
  font-size: 10px; font-weight: 600;
  white-space: nowrap;
}
.badge-pass { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
.badge-fail { color: var(--error); background: color-mix(in srgb, var(--error) 12%, transparent); }
.badge-skip { color: var(--text-muted); background: var(--bg-l3); }
.badge-warn { color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
.badge-positive { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
.badge-regression { color: var(--error); background: color-mix(in srgb, var(--error) 12%, transparent); }
.badge-needs-skill { color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
.badge-neutral { color: var(--text-muted); background: var(--bg-l3); }
.badge-code {
  color: var(--text-muted); background: var(--bg-l3);
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 9px; padding: 1px 6px; border-radius: 3px;
}
.badge-llm {
  color: var(--info); background: color-mix(in srgb, var(--info) 12%, transparent);
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 9px; padding: 1px 6px; border-radius: 3px;
}
.badge-score {
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 12px; font-weight: 700; padding: 2px 10px;
}

/* ── Tables ── */
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th {
  background: var(--bg-l2); font-weight: 600; text-align: left;
  padding: 6px 10px; border: 1px solid var(--border);
  color: var(--text-secondary); font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.3px;
}
td {
  padding: 6px 10px; border: 1px solid var(--border);
  color: var(--text-secondary); vertical-align: top;
}

/* ── Pre / code ── */
pre {
  background: var(--bg-l2); border: 1px solid var(--border);
  border-radius: 4px; padding: 10px; overflow-x: auto;
  font-size: 12px; white-space: pre-wrap; word-wrap: break-word;
  font-family: ui-monospace, 'SF Mono', monospace;
  color: var(--text-secondary); max-height: 400px; overflow-y: auto;
  line-height: 1.5;
}

/* ── Output comparison (2-pane) ── */
.output-comparison {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1px;
  background: var(--border); border: 1px solid var(--border);
  border-radius: 6px; overflow: hidden;
}
.output-pane { display: flex; flex-direction: column; background: var(--bg-l0); }
.output-pane-header {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--border);
  background: var(--bg-l1);
}
.output-pane-header span { font-size: 11px; font-weight: 600; color: var(--text-primary); }
.output-pane-content { padding: 12px; overflow: auto; max-height: 400px; }
.output-pane-content pre {
  background: transparent; border: none; padding: 0;
  font-size: 12px; line-height: 1.6; max-height: none;
}

/* ── Dimension bars ── */
.dim-bar-row {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 0; font-size: 11px;
}
.dim-bar-label { min-width: 140px; color: var(--text-secondary); font-size: 11px; }
.dim-bar-track {
  flex: 1; height: 18px; background: var(--bg-l3);
  border-radius: 4px; overflow: hidden; position: relative;
}
.dim-bar-fill {
  height: 100%; border-radius: 4px; opacity: 0.85;
  transition: width 0.4s ease;
}
.dim-bar-value {
  min-width: 36px; text-align: right;
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 11px; font-weight: 600;
}

/* ── Accent-bordered card (suggestions/recommendations) ── */
.card-accent {
  border-left: 3px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 3%, var(--bg-l1));
}

/* ── Details/summary for expandable sections ── */
details { border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
details + details { margin-top: 8px; }
summary {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 14px; cursor: pointer;
  background: var(--bg-l1); font-size: 12px; font-weight: 500;
  color: var(--text-primary); list-style: none;
  transition: background 0.15s ease;
}
summary:hover { background: var(--bg-l2); }
summary::-webkit-details-marker { display: none; }
summary::before {
  content: '\\25B6'; font-size: 9px; color: var(--text-muted);
  transition: transform 0.2s ease; display: inline-block;
}
details[open] > summary::before { transform: rotate(90deg); }
details > .details-body { padding: 14px; background: var(--bg-l0); }

/* ── Form elements ── */
textarea, select {
  font-size: 12px; color: var(--text-primary); background: var(--bg-l1);
  border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px;
  font-family: inherit; outline: none; width: 100%;
  transition: border-color 0.15s ease;
}
textarea { resize: vertical; min-height: 60px; }
textarea:focus, select:focus { border-color: var(--accent); }
select { max-width: 220px; cursor: pointer; }

/* ── Staggered card animation ── */
@keyframes card-reveal {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
.card:nth-child(1) { animation-delay: 0s; }
.card:nth-child(2) { animation-delay: 0.05s; }
.card:nth-child(3) { animation-delay: 0.1s; }
.card:nth-child(4) { animation-delay: 0.15s; }
.card:nth-child(5) { animation-delay: 0.2s; }
.card:nth-child(6) { animation-delay: 0.25s; }
.card:nth-child(7) { animation-delay: 0.3s; }
.card:nth-child(8) { animation-delay: 0.35s; }

/* ── Level score bar chart ── */
.score-bars { display: flex; flex-direction: column; gap: 6px; }
.score-bar-row { display: flex; align-items: center; gap: 8px; }
.score-bar-label {
  min-width: 100px; font-size: 11px; font-weight: 600;
  color: var(--text-secondary); text-transform: uppercase;
  letter-spacing: 0.3px;
}
.score-bar-track {
  flex: 1; height: 22px; background: var(--bg-l3);
  border-radius: 4px; overflow: hidden;
}
.score-bar-fill {
  height: 100%; border-radius: 4px; opacity: 0.85;
  transition: width 0.5s ease;
}
.score-bar-value {
  min-width: 40px; text-align: right;
  font-family: ui-monospace, 'SF Mono', monospace;
  font-size: 12px; font-weight: 700;
}

/* ── Mini metric cards (for L5 task scores) ── */
.mini-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; }
.mini-metric {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 10px; background: var(--bg-l2);
  border-radius: 4px; border: 1px solid var(--border);
}
.mini-metric-value {
  font-size: 18px; font-weight: 700;
  font-family: ui-monospace, 'SF Mono', monospace;
}
.mini-metric-label {
  font-size: 9px; font-weight: 600; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.3px;
}

/* ── Responsive ── */
@media (max-width: 700px) {
  .metric-grid { grid-template-columns: 1fr 1fr; }
  .output-comparison { grid-template-columns: 1fr; }
  .mini-metrics { grid-template-columns: 1fr 1fr; }
}
"""
