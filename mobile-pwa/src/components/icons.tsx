// Professional inline SVG icon set — one lightweight, stroke-based (currentColor)
// component per glyph so the whole app reads as one icon family instead of a mix
// of platform emoji. 24×24 grid, 2px round strokes (Feather/Lucide style). Size
// and stroke are props; colour comes from CSS `color`.

import type { SVGProps } from "react";

type P = { size?: number } & SVGProps<SVGSVGElement>;

function Svg({ size = 24, children, ...rest }: P & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconHome = (p: P) => (
  <Svg {...p}>
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5" />
  </Svg>
);

// Navigation arrow (Google-Maps style)
export const IconNavigate = (p: P) => (
  <Svg {...p}>
    <path d="M3 11 21 3l-8 18-2.5-7.5L3 11Z" />
  </Svg>
);

export const IconBell = (p: P) => (
  <Svg {...p}>
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
    <path d="M13.7 21a2 2 0 0 1-3.4 0" />
  </Svg>
);

export const IconParking = (p: P) => (
  <Svg {...p}>
    <rect x="3" y="3" width="18" height="18" rx="4" />
    <path d="M9 17V7h4a3 3 0 0 1 0 6H9" />
  </Svg>
);

export const IconTruck = (p: P) => (
  <Svg {...p}>
    <path d="M3 6h11v9H3z" />
    <path d="M14 9h4l3 3v3h-7z" />
    <circle cx="7" cy="18" r="1.6" />
    <circle cx="17.5" cy="18" r="1.6" />
  </Svg>
);

export const IconRoute = (p: P) => (
  <Svg {...p}>
    <circle cx="6" cy="19" r="2.4" />
    <circle cx="18" cy="5" r="2.4" />
    <path d="M8.4 19H14a3.5 3.5 0 0 0 0-7H10a3.5 3.5 0 0 1 0-7h5.6" />
  </Svg>
);

export const IconPin = (p: P) => (
  <Svg {...p}>
    <path d="M12 21s7-6.3 7-11a7 7 0 1 0-14 0c0 4.7 7 11 7 11Z" />
    <circle cx="12" cy="10" r="2.5" />
  </Svg>
);

export const IconFlag = (p: P) => (
  <Svg {...p}>
    <path d="M5 21V4" />
    <path d="M5 5h11l-2 3 2 3H5" />
  </Svg>
);

export const IconPhone = (p: P) => (
  <Svg {...p}>
    <path d="M4 5c0-1 .8-2 2-2h1.6c.5 0 1 .4 1.1.9l.9 3.2c.1.5-.05 1-.45 1.3l-1.4 1a12 12 0 0 0 5.3 5.3l1-1.4c.3-.4.8-.55 1.3-.45l3.2.9c.5.1.9.6.9 1.1V18c0 1.2-1 2-2 2A16 16 0 0 1 4 5Z" />
  </Svg>
);

export const IconShare = (p: P) => (
  <Svg {...p}>
    <circle cx="18" cy="5" r="2.5" />
    <circle cx="6" cy="12" r="2.5" />
    <circle cx="18" cy="19" r="2.5" />
    <path d="M8.2 10.8 15.8 6.4M8.2 13.2l7.6 4.4" />
  </Svg>
);

export const IconAlertTriangle = (p: P) => (
  <Svg {...p}>
    <path d="M10.3 3.3 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.3a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v5" />
    <path d="M12 17.5h.01" />
  </Svg>
);

export const IconChevronRight = (p: P) => (
  <Svg {...p}>
    <path d="m9 5 7 7-7 7" />
  </Svg>
);

export const IconChevronLeft = (p: P) => (
  <Svg {...p}>
    <path d="m15 5-7 7 7 7" />
  </Svg>
);

export const IconClose = (p: P) => (
  <Svg {...p}>
    <path d="M6 6 18 18M18 6 6 18" />
  </Svg>
);

export const IconClock = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </Svg>
);

export const IconGauge = (p: P) => (
  <Svg {...p}>
    <path d="M12 14 15 9" />
    <path d="M4 18a8 8 0 1 1 16 0" />
    <circle cx="12" cy="14" r="1.2" fill="currentColor" stroke="none" />
  </Svg>
);

export const IconGps = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M2 12h3M19 12h3" />
    <circle cx="12" cy="12" r="8" />
  </Svg>
);

export const IconWifi = (p: P) => (
  <Svg {...p}>
    <path d="M5 12.5a10 10 0 0 1 14 0" />
    <path d="M8.5 16a5 5 0 0 1 7 0" />
    <path d="M12 19.5h.01" />
  </Svg>
);

export const IconShield = (p: P) => (
  <Svg {...p}>
    <path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z" />
    <path d="m9 12 2 2 4-4" />
  </Svg>
);

export const IconLogout = (p: P) => (
  <Svg {...p}>
    <path d="M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3" />
    <path d="M10 16l-4-4 4-4M6 12h11" />
  </Svg>
);

export const IconGlobe = (p: P) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18M12 3c2.5 2.5 2.5 15 0 18M12 3c-2.5 2.5-2.5 15 0 18" />
  </Svg>
);

export const IconTraffic = (p: P) => (
  <Svg {...p}>
    <rect x="8" y="2" width="8" height="20" rx="3" />
    <circle cx="12" cy="7" r="1.4" />
    <circle cx="12" cy="12" r="1.4" />
    <circle cx="12" cy="17" r="1.4" />
  </Svg>
);
