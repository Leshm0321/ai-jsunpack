import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import type { MarketingRoute } from "./routes";

const MOTION_EASE = "power3.out";

function revealOnScroll(targets: HTMLElement[], start = "top 84%") {
  targets.forEach((target, index) => {
    gsap.from(target, {
      autoAlpha: 0,
      duration: 0.65,
      ease: MOTION_EASE,
      scrollTrigger: {
        trigger: target,
        start,
        once: true,
        refreshPriority: index
      },
      y: 34
    });
  });
}

function setupHeroStory(root: HTMLElement) {
  const hero = root.querySelector<HTMLElement>("[data-motion-hero]");
  const heroCopy = root.querySelector<HTMLElement>("[data-motion-hero-copy]");
  const product = root.querySelector<HTMLElement>("[data-motion-product]");
  const steps = Array.from(root.querySelectorAll<HTMLElement>("[data-motion-product-step]"));
  if (!hero || !product || steps.length === 0) {
    return;
  }

  gsap.set(steps, { autoAlpha: 0.38, scale: 0.96, transformOrigin: "50% 50%" });

  const story = gsap.timeline({
    defaults: { ease: "none" },
    scrollTrigger: {
      trigger: hero,
      start: "top top+=78",
      end: "+=165%",
      pin: hero,
      scrub: 0.65
    }
  });

  story
    .addLabel("open")
    .fromTo(product, { scale: 0.92, y: 28 }, { scale: 1, y: 0, duration: 0.85 }, "open");
  if (heroCopy) {
    story.to(heroCopy, { autoAlpha: 0.72, duration: 0.5, y: -18 }, "open+=0.25");
  }

  steps.forEach((step, index) => {
    const label = `stage-${index}`;
    story
      .addLabel(label)
      .to(steps, { autoAlpha: 0.32, duration: 0.18, scale: 0.96 }, label)
      .to(step, { autoAlpha: 1, duration: 0.34, scale: 1.04 }, label);
  });

  story.to(steps, { autoAlpha: 1, duration: 0.5, scale: 1, stagger: 0.04 });
}

function setupWorkflowJourney(root: HTMLElement) {
  const page = root.querySelector<HTMLElement>("[data-motion-workflow]");
  const rail = root.querySelector<HTMLElement>("[data-motion-workflow-rail]");
  const cards = Array.from(root.querySelectorAll<HTMLElement>("[data-motion-workflow-card]"));
  if (!page || !rail || cards.length < 2) {
    return;
  }

  const cardWidth = Math.min(window.innerWidth * 0.68, 820);
  const gap = 24;
  const railWidth = cards.length * cardWidth + (cards.length - 1) * gap;
  const travel = Math.max(0, railWidth - page.clientWidth);

  gsap.set(page, { minHeight: "calc(100vh - 104px)", overflow: "hidden" });
  gsap.set(rail, {
    display: "flex",
    gap,
    overflow: "visible",
    paddingRight: Math.max(0, page.clientWidth - cardWidth),
    width: railWidth
  });
  gsap.set(cards, { flex: `0 0 ${cardWidth}px`, width: cardWidth });

  const journey = gsap.timeline({
    scrollTrigger: {
      trigger: page,
      start: "top top+=82",
      end: () => `+=${Math.max(travel, window.innerHeight * 2.4)}`,
      invalidateOnRefresh: true,
      pin: true,
      scrub: 0.75
    }
  });

  journey.to(rail, {
    ease: "none",
    x: () => -Math.max(0, rail.scrollWidth - page.clientWidth)
  });

  cards.forEach((card) => {
    gsap.fromTo(
      card,
      { autoAlpha: 0.48, scale: 0.94 },
      {
        autoAlpha: 1,
        duration: 0.35,
        ease: MOTION_EASE,
        scale: 1,
        scrollTrigger: {
          containerAnimation: journey,
          trigger: card,
          start: "left 76%",
          toggleActions: "play none none reverse"
        }
      }
    );
  });
}

function setupEvidenceReveal(root: HTMLElement) {
  const matrix = root.querySelector<HTMLElement>("[data-motion-evidence-matrix]");
  const cells = Array.from(root.querySelectorAll<HTMLElement>("[data-motion-evidence-cell]"));
  const ledger = Array.from(root.querySelectorAll<HTMLElement>("[data-motion-evidence-ledger] > div"));
  if (!matrix || cells.length === 0) {
    return;
  }

  gsap
    .timeline({
      defaults: { duration: 0.48, ease: MOTION_EASE },
      scrollTrigger: { trigger: matrix, start: "top 78%", once: true }
    })
    .from(cells, { autoAlpha: 0, scale: 0.94, stagger: { amount: 0.48, from: "start" } })
    .from(ledger, { autoAlpha: 0, stagger: 0.1, x: 28 }, "-=0.16");
}

function setupRuntimeReveal(root: HTMLElement) {
  const showcase = root.querySelector<HTMLElement>("[data-motion-runtime]");
  const browser = root.querySelector<HTMLElement>("[data-motion-runtime-browser]");
  const metrics = Array.from(root.querySelectorAll<HTMLElement>("[data-motion-runtime-metric]"));
  const capture = root.querySelector<HTMLElement>("[data-motion-runtime-capture]");
  if (!showcase || !browser || metrics.length === 0) {
    return;
  }

  const reveal = gsap.timeline({
    defaults: { duration: 0.7, ease: MOTION_EASE },
    scrollTrigger: { trigger: showcase, start: "top 78%", once: true }
  });
  reveal
    .from(browser, { autoAlpha: 0, x: -52 })
    .from(metrics, { autoAlpha: 0, stagger: 0.1, x: 46 }, "<0.12");
  if (capture) {
    reveal.from(capture, { autoAlpha: 0, rotation: -8, scale: 0.76 }, "<0.08");
  }
}

function setupDesktopMotion(root: HTMLElement, route: MarketingRoute) {
  if (route === "/") {
    setupHeroStory(root);
  } else if (route === "/workflow") {
    setupWorkflowJourney(root);
  } else if (route === "/evidence") {
    setupEvidenceReveal(root);
  } else if (route === "/runtime") {
    setupRuntimeReveal(root);
  }
}

export function setupMarketingMotion(root: HTMLElement, route: MarketingRoute): () => void {
  const select = gsap.utils.selector(root);
  const media = gsap.matchMedia();

  media.add(
    {
      desktop: "(min-width: 900px)",
      reduceMotion: "(prefers-reduced-motion: reduce)"
    },
    (context) => {
      const reduceMotion = Boolean(context.conditions?.reduceMotion);
      const desktop = Boolean(context.conditions?.desktop);
      const motionTargets = select<HTMLElement>(
        "[data-motion-hero-copy], [data-motion-product], [data-motion-reveal], [data-motion-product-step], [data-motion-workflow-card], [data-motion-evidence-cell], [data-motion-runtime-browser], [data-motion-runtime-metric]"
      );

      if (reduceMotion) {
        gsap.set(motionTargets, { clearProps: "opacity,transform,visibility" });
        return;
      }

      const header = root.querySelector<HTMLElement>(".site-header");
      const introItems = Array.from(
        root.querySelectorAll<HTMLElement>("[data-motion-hero-copy] > *, .detail-intro > *")
      );
      const productTargets = select<HTMLElement>("[data-motion-product]");
      const intro = gsap.timeline({ defaults: { duration: 0.62, ease: MOTION_EASE } });
      if (header) {
        intro.from(header, { autoAlpha: 0, y: -18, duration: 0.38 });
      }
      intro.from(introItems, { autoAlpha: 0, stagger: 0.07, y: 26 }, "<0.1");
      if (productTargets.length > 0) {
        intro.from(productTargets, { autoAlpha: 0, scale: 0.96, y: 28 }, "<0.12");
      }

      if (desktop) {
        setupDesktopMotion(root, route);
      } else {
        revealOnScroll(select<HTMLElement>("[data-motion-reveal], [data-motion-workflow-card]"));
        if (route === "/evidence") {
          revealOnScroll(select<HTMLElement>("[data-motion-evidence-cell]"), "top 88%");
        }
        if (route === "/runtime") {
          revealOnScroll(select<HTMLElement>("[data-motion-runtime-browser], [data-motion-runtime-metric]"));
        }
      }

      if (desktop && route === "/") {
        revealOnScroll(select<HTMLElement>("[data-motion-reveal]"));
      }

      ScrollTrigger.refresh();
    }
  );

  return () => media.revert();
}
