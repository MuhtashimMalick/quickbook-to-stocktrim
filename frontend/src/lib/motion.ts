import { useGSAP } from "@gsap/react";
import gsap from "gsap";
import { useRef, type RefObject } from "react";

gsap.registerPlugin(useGSAP);

/** Motion tokens (seconds / px / scale): see DESIGN.md. */
export const MOTION = { enter: 0.25, stagger: 0.05, rise: 8, ease: "power2.out", progress: 0.4, press: 0.2, pressFrom: 1.15 } as const;

const NO_PREFERENCE = "(prefers-reduced-motion: no-preference)";
const REDUCE = "(prefers-reduced-motion: reduce)";

/**
 * Route enter: the container's children fade in with an 8 px rise, staggered.
 * With prefers-reduced-motion nothing is registered, so everything is already in its final state.
 */
export function useRouteEnter(scope: RefObject<HTMLElement | null>): void {
  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add(NO_PREFERENCE, () => {
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

/** Progress bar fill: tweens scaleX to the new ratio; with reduced motion it jumps straight to the final state. */
export function useProgressTween(ref: RefObject<HTMLElement | null>, ratio: number): void {
  const previous = useRef(0);
  useGSAP(
    () => {
      const el = ref.current;
      if (!el) return;
      const from = previous.current;
      previous.current = ratio;
      const mm = gsap.matchMedia();
      mm.add(NO_PREFERENCE, () => {
        gsap.fromTo(el, { scaleX: from }, { scaleX: ratio, duration: MOTION.progress, ease: MOTION.ease });
      });
      mm.add(REDUCE, () => {
        gsap.set(el, { scaleX: ratio });
      });
      return () => mm.revert();
    },
    { dependencies: [ratio], revertOnUpdate: true },
  );
}

/** A brief "stamp press" when the status text changes (never on first render). */
export function useStampPress(ref: RefObject<HTMLElement | null>, status: string): void {
  const first = useRef(true);
  useGSAP(
    () => {
      if (first.current) {
        first.current = false;
        return;
      }
      const mm = gsap.matchMedia();
      mm.add(NO_PREFERENCE, () => {
        gsap.fromTo(ref.current, { scale: MOTION.pressFrom }, { scale: 1, duration: MOTION.press, ease: MOTION.ease, clearProps: "transform" });
      });
      return () => mm.revert();
    },
    { dependencies: [status] },
  );
}
