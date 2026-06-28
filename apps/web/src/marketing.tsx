import { useRef } from "react";
import { useGSAP } from "@gsap/react";
import { gsap } from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import {
  Activity,
  ArrowRight,
  Binary,
  Braces,
  CheckCircle2,
  FileCode2,
  GitBranch,
  Languages,
  LockKeyhole,
  Network,
  Radar,
  SearchCode,
  ShieldCheck,
  Sparkles,
  Upload,
  Workflow
} from "lucide-react";
import { useLocalization } from "./i18n";
import type { Language } from "./i18n";
import type { AppRoute } from "./routes";

gsap.registerPlugin(useGSAP, ScrollTrigger);

interface MarketingPageProps {
  route: AppRoute;
  onNavigate: (route: AppRoute) => void;
}

function navigateLabel(route: AppRoute): string {
  if (route === "/workflow") {
    return "site.nav.workflow";
  }
  if (route === "/evidence") {
    return "site.nav.evidence";
  }
  if (route === "/runtime") {
    return "site.nav.runtime";
  }
  if (route === "/workbench") {
    return "site.nav.workbench";
  }
  return "site.nav.home";
}

function SiteHeader({
  activeRoute,
  language,
  onLanguageChange,
  onNavigate,
  t
}: {
  activeRoute: AppRoute;
  language: Language;
  onLanguageChange: (language: Language) => void;
  onNavigate: (route: AppRoute) => void;
  t: (key: string) => string;
}) {
  const navItems: AppRoute[] = ["/", "/workflow", "/evidence", "/runtime", "/workbench"];
  return (
    <header className="site-header">
      <button className="site-brand" type="button" onClick={() => onNavigate("/")} aria-label={t("site.aria.home")}>
        <span className="brand-mark">
          <Binary size={18} aria-hidden="true" />
        </span>
        <span>AI JS Unpack</span>
      </button>
      <nav className="site-nav" aria-label={t("app.aria.primaryNav")}>
        {navItems.map((route) => (
          <button
            aria-current={activeRoute === route ? "page" : undefined}
            className={activeRoute === route ? "site-nav-link active" : "site-nav-link"}
            key={route}
            type="button"
            onClick={() => onNavigate(route)}
          >
            {t(navigateLabel(route))}
          </button>
        ))}
      </nav>
      <div className="site-language" aria-label={t("app.aria.toggleLanguage")}>
        <Languages size={16} aria-hidden="true" />
        {(["en", "zh"] as const).map((option) => (
          <button
            aria-pressed={language === option}
            className={language === option ? "language-option language-option-active" : "language-option"}
            key={option}
            type="button"
            onClick={() => onLanguageChange(option)}
          >
            {t(`language.${option}`)}
          </button>
        ))}
      </div>
    </header>
  );
}

function ProductVisual({ t }: { t: (key: string) => string }) {
  const steps = [
    { label: t("site.visual.input"), icon: Upload },
    { label: t("site.visual.ast"), icon: GitBranch },
    { label: t("site.visual.agent"), icon: Sparkles },
    { label: t("site.visual.runtime"), icon: Radar },
    { label: t("site.visual.report"), icon: ShieldCheck }
  ];
  return (
    <div className="product-visual" aria-label={t("app.aria.pipelineOverview")}>
      <div className="visual-terminal">
        <div className="terminal-topline">
          <span>restore.js</span>
          <strong>local_only</strong>
        </div>
        <pre>{`const app = unpack(bundle);
trace(app.routes);
validate(runtime.capture);`}</pre>
      </div>
      <div className="visual-pipeline">
        {steps.map((step) => {
          const Icon = step.icon;
          return (
            <div className="visual-step" key={step.label}>
              <Icon size={18} aria-hidden="true" />
              <span>{step.label}</span>
            </div>
          );
        })}
      </div>
      <div className="visual-metrics" aria-label={t("site.aria.metrics")}>
        <div>
          <span>{t("site.metric.evidence")}</span>
          <strong>100%</strong>
        </div>
        <div>
          <span>{t("site.metric.runtime")}</span>
          <strong>3x</strong>
        </div>
        <div>
          <span>{t("site.metric.mode")}</span>
          <strong>safe</strong>
        </div>
      </div>
    </div>
  );
}

function HomePage({ onNavigate, t }: { onNavigate: (route: AppRoute) => void; t: (key: string) => string }) {
  const proof = [
    { label: t("site.proof.local"), icon: LockKeyhole },
    { label: t("site.proof.traceable"), icon: SearchCode },
    { label: t("site.proof.browser"), icon: Radar }
  ];
  return (
    <>
      <section className="site-hero">
        <div className="site-hero-copy site-motion">
          <p className="site-eyebrow">{t("site.home.eyebrow")}</p>
          <h1>AI JS Unpack</h1>
          <p className="site-hero-lede">{t("site.home.lede")}</p>
          <div className="site-actions">
            <button className="primary-action" type="button" onClick={() => onNavigate("/workbench")}>
              <Upload size={18} aria-hidden="true" />
              {t("site.cta.openWorkbench")}
            </button>
            <button className="secondary-action" type="button" onClick={() => onNavigate("/workflow")}>
              <Workflow size={18} aria-hidden="true" />
              {t("site.cta.viewWorkflow")}
            </button>
          </div>
        </div>
        <div className="site-motion">
          <ProductVisual t={t} />
        </div>
      </section>

      <section className="site-band proof-band site-reveal">
        {proof.map((item) => {
          const Icon = item.icon;
          return (
            <div className="proof-item" key={item.label}>
              <Icon size={20} aria-hidden="true" />
              <span>{item.label}</span>
            </div>
          );
        })}
      </section>

      <FeatureGrid t={t} />
    </>
  );
}

function FeatureGrid({ t }: { t: (key: string) => string }) {
  const features = [
    { title: t("site.feature.deobfuscate.title"), text: t("site.feature.deobfuscate.text"), icon: Braces },
    { title: t("site.feature.lineage.title"), text: t("site.feature.lineage.text"), icon: GitBranch },
    { title: t("site.feature.runtime.title"), text: t("site.feature.runtime.text"), icon: Radar },
    { title: t("site.feature.audit.title"), text: t("site.feature.audit.text"), icon: ShieldCheck }
  ];
  return (
    <section className="site-grid-section site-reveal">
      <div className="section-heading-block">
        <p className="site-eyebrow">{t("site.home.sectionKicker")}</p>
        <h2>{t("site.home.sectionTitle")}</h2>
      </div>
      <div className="feature-grid">
        {features.map((feature) => {
          const Icon = feature.icon;
          return (
            <article className="feature-card" key={feature.title}>
              <Icon size={22} aria-hidden="true" />
              <h3>{feature.title}</h3>
              <p>{feature.text}</p>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function WorkflowPage({ onNavigate, t }: { onNavigate: (route: AppRoute) => void; t: (key: string) => string }) {
  const steps = [
    { title: t("site.workflow.step1.title"), text: t("site.workflow.step1.text"), icon: Upload },
    { title: t("site.workflow.step2.title"), text: t("site.workflow.step2.text"), icon: GitBranch },
    { title: t("site.workflow.step3.title"), text: t("site.workflow.step3.text"), icon: Sparkles },
    { title: t("site.workflow.step4.title"), text: t("site.workflow.step4.text"), icon: Radar },
    { title: t("site.workflow.step5.title"), text: t("site.workflow.step5.text"), icon: ShieldCheck }
  ];
  return (
    <section className="detail-page">
      <div className="detail-intro site-motion">
        <p className="site-eyebrow">{t("site.workflow.eyebrow")}</p>
        <h1>{t("site.workflow.title")}</h1>
        <p>{t("site.workflow.lede")}</p>
      </div>
      <div className="workflow-rail">
        {steps.map((step, index) => {
          const Icon = step.icon;
          return (
            <article className="workflow-card site-reveal" key={step.title}>
              <span className="workflow-index">{String(index + 1).padStart(2, "0")}</span>
              <Icon size={22} aria-hidden="true" />
              <h2>{step.title}</h2>
              <p>{step.text}</p>
            </article>
          );
        })}
      </div>
      <button className="primary-action site-end-cta" type="button" onClick={() => onNavigate("/workbench")}>
        {t("site.cta.startAnalysis")}
        <ArrowRight size={18} aria-hidden="true" />
      </button>
    </section>
  );
}

function EvidencePage({ onNavigate, t }: { onNavigate: (route: AppRoute) => void; t: (key: string) => string }) {
  const rows = [
    [t("site.evidence.matrix.input"), t("site.evidence.matrix.artifacts"), t("site.evidence.matrix.review")],
    [t("site.evidence.matrix.trace"), t("site.evidence.matrix.audit"), t("site.evidence.matrix.package")],
    [t("site.evidence.matrix.runtime"), t("site.evidence.matrix.diff"), t("site.evidence.matrix.decision")]
  ];
  return (
    <section className="detail-page evidence-page">
      <div className="detail-intro site-motion">
        <p className="site-eyebrow">{t("site.evidence.eyebrow")}</p>
        <h1>{t("site.evidence.title")}</h1>
        <p>{t("site.evidence.lede")}</p>
      </div>
      <div className="evidence-matrix site-reveal" aria-label={t("site.aria.evidenceMatrix")}>
        {rows.flat().map((item) => (
          <div className="evidence-cell" key={item}>
            <CheckCircle2 size={18} aria-hidden="true" />
            <span>{item}</span>
          </div>
        ))}
      </div>
      <div className="evidence-ledger site-reveal">
        <div>
          <Activity size={20} aria-hidden="true" />
          <span>{t("site.evidence.ledger.inference")}</span>
        </div>
        <div>
          <SearchCode size={20} aria-hidden="true" />
          <span>{t("site.evidence.ledger.review")}</span>
        </div>
        <div>
          <FileCode2 size={20} aria-hidden="true" />
          <span>{t("site.evidence.ledger.attachments")}</span>
        </div>
      </div>
      <button className="secondary-action site-end-cta" type="button" onClick={() => onNavigate("/runtime")}>
        {t("site.cta.inspectRuntime")}
        <ArrowRight size={18} aria-hidden="true" />
      </button>
    </section>
  );
}

function RuntimePage({ onNavigate, t }: { onNavigate: (route: AppRoute) => void; t: (key: string) => string }) {
  return (
    <section className="detail-page runtime-story">
      <div className="detail-intro site-motion">
        <p className="site-eyebrow">{t("site.runtime.eyebrow")}</p>
        <h1>{t("site.runtime.title")}</h1>
        <p>{t("site.runtime.lede")}</p>
      </div>
      <div className="runtime-showcase site-reveal">
        <div className="browser-frame">
          <div className="browser-bar">
            <span />
            <span />
            <span />
            <strong>127.0.0.1/runtime-smoke</strong>
          </div>
          <div className="browser-capture">
            <Network size={36} aria-hidden="true" />
            <p>{t("site.runtime.capture")}</p>
          </div>
        </div>
        <div className="runtime-diff-preview">
          <div>
            <span>{t("site.runtime.original")}</span>
            <strong>pass</strong>
          </div>
          <div>
            <span>{t("site.runtime.rebuilt")}</span>
            <strong>pass</strong>
          </div>
          <div>
            <span>{t("site.runtime.delta")}</span>
            <strong>0.8%</strong>
          </div>
        </div>
      </div>
      <button className="primary-action site-end-cta" type="button" onClick={() => onNavigate("/workbench")}>
        {t("site.cta.openWorkbench")}
        <ArrowRight size={18} aria-hidden="true" />
      </button>
    </section>
  );
}

export function MarketingPage({ route, onNavigate }: MarketingPageProps) {
  const { language, setLanguage, t } = useLocalization();
  const rootRef = useRef<HTMLDivElement>(null);

  useGSAP(
    () => {
      const mm = gsap.matchMedia();
      mm.add(
        {
          reduceMotion: "(prefers-reduced-motion: reduce)",
          desktop: "(min-width: 800px)"
        },
        (context) => {
          const reduceMotion = Boolean(context.conditions?.reduceMotion);
          if (reduceMotion) {
            gsap.set(".site-motion, .site-reveal, .visual-step, .evidence-cell", {
              autoAlpha: 1,
              x: 0,
              y: 0,
              scale: 1
            });
            return;
          }

          const intro = gsap.timeline({ defaults: { duration: 0.55, ease: "power3.out" } });
          intro
            .from(".site-header", { y: -18, autoAlpha: 0, duration: 0.35 })
            .from(".site-motion", { y: 28, autoAlpha: 0, stagger: 0.08 }, "<0.08")
            .from(".visual-step", { x: 18, autoAlpha: 0, stagger: 0.05 }, "<0.08");

          gsap.utils.toArray<HTMLElement>(".site-reveal").forEach((element, index) => {
            gsap.from(element, {
              y: 34,
              autoAlpha: 0,
              duration: 0.58,
              ease: "power3.out",
              scrollTrigger: {
                trigger: element,
                start: "top 82%",
                once: true,
                refreshPriority: index
              }
            });
          });

          gsap.utils.toArray<HTMLElement>(".evidence-cell").forEach((element, index) => {
            gsap.from(element, {
              scale: 0.96,
              autoAlpha: 0,
              duration: 0.42,
              delay: index * 0.035,
              ease: "power2.out",
              scrollTrigger: {
                trigger: ".evidence-matrix",
                start: "top 78%",
                once: true
              }
            });
          });

          return () => {
            intro.kill();
            ScrollTrigger.getAll().forEach((trigger) => trigger.kill());
          };
        }
      );

      return () => mm.revert();
    },
    { dependencies: [route, language], revertOnUpdate: true, scope: rootRef }
  );

  return (
    <div className="site-shell" ref={rootRef}>
      <SiteHeader
        activeRoute={route}
        language={language}
        onLanguageChange={setLanguage}
        onNavigate={onNavigate}
        t={t}
      />
      <main className="site-main">
        {route === "/" ? <HomePage onNavigate={onNavigate} t={t} /> : null}
        {route === "/workflow" ? <WorkflowPage onNavigate={onNavigate} t={t} /> : null}
        {route === "/evidence" ? <EvidencePage onNavigate={onNavigate} t={t} /> : null}
        {route === "/runtime" ? <RuntimePage onNavigate={onNavigate} t={t} /> : null}
      </main>
    </div>
  );
}

