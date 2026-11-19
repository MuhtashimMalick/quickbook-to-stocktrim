import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { type RefObject } from "react";

gsap.registerPlugin(useGSAP);

/** Motion tokens (seconds / px): see DESIGN.md. */
export const MOTION = { enter: 0.25, stagger: 0.05, rise: 8, ease: "power2.out" } as const;

/**
 * Route enter: the container's children fade in with an 8 px rise, staggered.
 * With prefers-reduced-motion nothing is registered, so everything is already in its final state.
 */
export function useRouteEnter(scope: RefObject<HTMLElement | null>): void {
  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add("(prefers-reduced-motion: no-preference)", () => {
        gsap.from(scope.current?.children ?? [], {
          autoAlpha: 0,
          y: MOTION.rise,
          duration: MOTION.enter,
          stagger: MOTION.stagger,
          ease: MOTION.ease,
          clearProps: "transform,opacity,visibility",
        });
      });
      return () => mm.revert();
    },
    { scope },
  );
}
