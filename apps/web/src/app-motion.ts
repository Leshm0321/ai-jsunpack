import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import type { RefObject } from "react";

gsap.registerPlugin(useGSAP, ScrollTrigger);

export const appMotion = {
  duration: {
    fast: 0.28,
    normal: 0.52,
    slow: 0.82
  },
  ease: {
    enter: "power3.out",
    move: "power3.inOut",
    soft: "sine.out"
  },
  stagger: {
    compact: 0.035,
    normal: 0.065
  }
} as const;

export function useApplicationMotion(
  root: RefObject<HTMLElement>,
  dependencies: ReadonlyArray<unknown>
) {
  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add(
        {
          desktop: "(min-width: 761px)",
          mobile: "(max-width: 760px)",
          reduceMotion: "(prefers-reduced-motion: reduce)"
        },
        (context) => {
          const rootElement = root.current;
          if (!rootElement) return;
          const reduceMotion = Boolean(context.conditions?.reduceMotion);
          const desktop = Boolean(context.conditions?.desktop);
          const topbar = rootElement.querySelector<HTMLElement>(".application-topbar");
          const sidebar = rootElement.querySelector<HTMLElement>(".application-sidebar");
          const heading = rootElement.querySelector<HTMLElement>(".page-heading");
          const panels = gsap.utils.toArray<HTMLElement>(
            ".route-panel, .overview-metric, .settings-section, .readiness-card",
            rootElement
          );
          const targets = [topbar, sidebar, heading, ...panels].filter(
            (target): target is HTMLElement => target !== null
          );
          if (reduceMotion) {
            if (targets.length > 0) {
              gsap.set(targets, { autoAlpha: 1, clearProps: "transform" });
            }
            return;
          }

          const timeline = gsap.timeline({
            defaults: { duration: appMotion.duration.normal, ease: appMotion.ease.enter }
          });
          if (topbar) {
            timeline.from(topbar, { y: -14, autoAlpha: 0, duration: appMotion.duration.fast }, 0);
          }
          if (sidebar) {
            timeline.from(sidebar, { x: desktop ? -22 : 0, y: desktop ? 0 : -10, autoAlpha: 0 }, 0.04);
          }
          if (heading) {
            timeline.from(heading, { y: 22, autoAlpha: 0 }, 0.08);
          }
          if (panels.length > 0) {
            timeline.from(panels, { y: 26, autoAlpha: 0, stagger: appMotion.stagger.normal }, 0.1);
          }

          return () => timeline.kill();
        }
      );
      return () => mm.revert();
    },
    { dependencies: [...dependencies], revertOnUpdate: true, scope: root }
  );
}

export function useApplicationScrollMotion(
  root: RefObject<HTMLElement>,
  dependencies: ReadonlyArray<unknown>
) {
  useGSAP(
    () => {
      const scroller = root.current?.querySelector<HTMLElement>(".application-content");
      if (!scroller) return;
      const targets = gsap.utils
        .toArray<HTMLElement>(
          ".settings-field, .agent-record, .stage-step, .guidance-list > div, .effective-config-list > div, .runtime-run-card, .report-row",
          root.current
        )
        .slice(0, 80);
      if (targets.length === 0) return;
      const mm = gsap.matchMedia();
      mm.add(
        {
          desktop: "(min-width: 761px)",
          reduceMotion: "(prefers-reduced-motion: reduce)"
        },
        (context) => {
          const reduceMotion = Boolean(context.conditions?.reduceMotion);
          const desktop = Boolean(context.conditions?.desktop);
          if (reduceMotion) {
            gsap.set(targets, { autoAlpha: 1, clearProps: "transform" });
            return;
          }
          gsap.set(targets, { autoAlpha: 0, y: desktop ? 30 : 16, scale: desktop ? 0.985 : 1 });
          const triggers = ScrollTrigger.batch(targets, {
            scroller,
            start: "top 90%",
            once: true,
            onEnter: (batch) => {
              gsap.to(batch, {
                autoAlpha: 1,
                y: 0,
                scale: 1,
                duration: appMotion.duration.normal,
                ease: appMotion.ease.enter,
                stagger: appMotion.stagger.compact,
                overwrite: "auto"
              });
            }
          });
          requestAnimationFrame(() => ScrollTrigger.refresh());
          return () => triggers.forEach((trigger) => trigger.kill());
        }
      );
      return () => mm.revert();
    },
    { dependencies: [...dependencies], revertOnUpdate: true, scope: root }
  );
}

export function useActiveNavMotion(
  root: RefObject<HTMLElement>,
  activeKey: string
) {
  useGSAP(
    () => {
      const indicator = root.current?.querySelector<HTMLElement>(".sidebar-active-indicator");
      const active = root.current?.querySelector<HTMLElement>(".sidebar-link.active");
      if (!indicator || !active) {
        if (indicator) gsap.set(indicator, { autoAlpha: 0 });
        return;
      }
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      gsap.to(indicator, {
        y: active.offsetTop,
        height: active.offsetHeight,
        autoAlpha: 1,
        duration: reduceMotion ? 0 : appMotion.duration.normal,
        ease: appMotion.ease.move,
        overwrite: "auto"
      });
    },
    { dependencies: [activeKey], scope: root }
  );
}

export function useMetricMotion(
  root: RefObject<HTMLElement>,
  valueKey: string
) {
  useGSAP(
    () => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const targets = gsap.utils.toArray<HTMLElement>(".motion-metric-value", root.current);
      if (targets.length === 0) return;
      gsap.fromTo(
        targets,
        { y: 8, autoAlpha: 0, scale: 0.96 },
        {
          y: 0,
          autoAlpha: 1,
          scale: 1,
          duration: appMotion.duration.fast,
          ease: appMotion.ease.enter,
          stagger: appMotion.stagger.compact,
          overwrite: "auto"
        }
      );
    },
    { dependencies: [valueKey], scope: root }
  );
}
