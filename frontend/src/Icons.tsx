import type { SVGProps } from "react";

const base: SVGProps<SVGSVGElement> = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

type P = SVGProps<SVGSVGElement>;

export const PlusIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);
export const SendIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M12 19V5M5 12l7-7 7 7" />
  </svg>
);
export const StopIcon = (p: P) => (
  <svg {...base} fill="currentColor" stroke="none" {...p}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
  </svg>
);
export const CopyIcon = (p: P) => (
  <svg {...base} {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V6a2 2 0 0 1 2-2h9" />
  </svg>
);
export const CheckIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M5 12.5l4.5 4.5L19 7.5" />
  </svg>
);
export const ArrowDownIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M12 5v14M5 12l7 7 7-7" />
  </svg>
);
export const MenuIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </svg>
);
export const SearchIcon = (p: P) => (
  <svg {...base} {...p}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="M20 20l-4-4" />
  </svg>
);
export const TrashIcon = (p: P) => (
  <svg {...base} {...p}>
    <path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" />
  </svg>
);

/** Brand mark: a prompt chevron followed by a streaming cursor. */
export const LogoMark = (p: P) => (
  <svg width={28} height={28} viewBox="0 0 32 32" fill="none" aria-hidden {...p}>
    <rect x="1" y="1" width="30" height="30" rx="8" fill="#0a1218" stroke="#22d3ee" strokeOpacity=".45" />
    <path d="M9 11l6 5-6 5" stroke="#22d3ee" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
    <rect x="17.5" y="19.5" width="6" height="2.4" rx="1.2" fill="#22d3ee" />
  </svg>
);
