/** Line icons in one style (24px grid, round caps), drawn in currentColor. */
const S = 'fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"';
export const ICONS = {
  shield: `<path ${S} d="M12 3.5 5 6v5.5c0 4.2 2.9 7.7 7 9 4.1-1.3 7-4.8 7-9V6z"/><path ${S} d="m9 12 2 2 4-4"/>`,
  memories: `<path ${S} d="M4 5.5A1.5 1.5 0 0 1 5.5 4h4l2 2h7A1.5 1.5 0 0 1 20 7.5v10a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/>`,
  changes: `<circle ${S} cx="12" cy="12" r="8"/><path ${S} d="m8.5 12 2.5 2.5 4.5-5"/>`,
  eye: `<path ${S} d="M3 12s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z"/><circle ${S} cx="12" cy="12" r="2.5"/>`,
  plug: `<path ${S} d="M9 7V4M15 7V4M6 7h12v3a6 6 0 0 1-12 0zM12 16v4"/>`,
  clock: `<circle ${S} cx="12" cy="12" r="8"/><path ${S} d="M12 8v4l3 2"/>`,
  download: `<path ${S} d="M12 4v10m-4-4 4 4 4-4M5 16v2a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2"/>`,
  lock: `<rect ${S} x="6" y="10" width="12" height="10" rx="2"/><path ${S} d="M9 10V7a3 3 0 0 1 6 0v3"/>`,
  star: `<path ${S} d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6-5.4-3-5.4 3 1.2-6L3.3 9.3l6.1-.7z"/>`,
  starFill: `<path fill="currentColor" d="m12 3 2.6 5.6 6.1.7-4.5 4.2 1.2 6-5.4-3-5.4 3 1.2-6L3.3 9.3l6.1-.7z"/>`,
  pencil: `<path ${S} d="M4 20h4l10-10-4-4L4 16z"/>`,
  more: `<g fill="currentColor"><circle cx="6" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="18" cy="12" r="1.8"/></g>`,
  search: `<circle ${S} cx="11" cy="11" r="6"/><path ${S} d="m16 16 4 4"/>`,
  plus: `<path ${S} d="M12 5v14M5 12h14"/>`,
  x: `<path ${S} d="M6 6l12 12M18 6 6 18"/>`,
  copy: `<rect ${S} x="9" y="9" width="11" height="11" rx="2"/><path ${S} d="M5 15V5a1 1 0 0 1 1-1h10"/>`,
  chevronDown: `<path ${S} d="m6 9 6 6 6-6"/>`,
  chevronRight: `<path ${S} d="m9 6 6 6-6 6"/>`,
  arrowRight: `<path ${S} d="M5 12h14m-6-6 6 6-6 6"/>`,
  check: `<path ${S} d="m5 12 5 5 9-10"/>`,
  warning: `<path ${S} d="M12 9v4M12 17h.01M10.3 4.6 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/>`,
  info: `<circle ${S} cx="12" cy="12" r="9"/><path ${S} d="M12 8v4M12 16h.01"/>`,
  refresh: `<path ${S} d="M20 11a8 8 0 0 0-14.6-4.5M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5M20 20v-4h-4"/>`,
  file: `<path ${S} d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/><path ${S} d="M14 3v5h5"/>`,
  sidebar: `<rect ${S} x="3.5" y="5" width="17" height="14" rx="2.5"/><path ${S} d="M9.5 5v14"/>`,
  filter: `<path ${S} d="M5 7h14M8 12h8M11 17h2"/>`,
  sliders: `<path ${S} d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle ${S} cx="16" cy="7" r="2"/><circle ${S} cx="10" cy="17" r="2"/>`,
  grid: `<rect ${S} x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect ${S} x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/><rect ${S} x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect ${S} x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>`,
} as const;

export type IconName = keyof typeof ICONS;
