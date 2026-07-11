import { useEffect, useMemo, useState } from "react";
import { Download, Filter, Save, Trash2, XCircle } from "lucide-react";
import type { Artifact, Job } from "@ai-jsunpack/shared";
import { useLocalization } from "./i18n";
import type { AuditCategory, AuditFilterState, AuditRiskGroup, AuditStatusFilter, JobEvidence, SavedAuditFilter } from "./workbench-types";
import { defaultAuditFilters } from "./workbench-types";
import {
  auditFilterLabel,
  auditRecordMatches,
  auditRecordNeedsAttention,
  buildAuditRecords,
  downloadJsonFile,
  formatIdList,
  groupAuditRecordsByRisk,
  persistSavedAuditFilters,
  readSavedAuditFilters
} from "./workbench-logic";
import { EmptyState, EvidenceRefButtons, StatusToken } from "./workbench-common";

export function AuditPanel({
  artifacts,
  currentJob,
  evidence,
  onArtifactSelect
}: {
  artifacts: Artifact[];
  currentJob: Job | null;
  evidence: JobEvidence;
  onArtifactSelect: (artifactId: string) => void;
}) {
  const { t } = useLocalization();
  const [filters, setFilters] = useState<AuditFilterState>(defaultAuditFilters);
  const [savedFilters, setSavedFilters] = useState<SavedAuditFilter[]>(() => readSavedAuditFilters());
  const [selectedSavedFilterId, setSelectedSavedFilterId] = useState("");
  const [filterName, setFilterName] = useState("");
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<string>>(() => new Set());
  const auditRecords = useMemo(() => buildAuditRecords(evidence), [evidence]);
  const filteredRecords = useMemo(
    () => auditRecords.filter((record) => auditRecordMatches(record, filters)),
    [auditRecords, filters]
  );
  const groupedRecords = useMemo(() => groupAuditRecordsByRisk(filteredRecords), [filteredRecords]);
  const selectedRecords = useMemo(
    () => auditRecords.filter((record) => selectedRecordIds.has(record.id)),
    [auditRecords, selectedRecordIds]
  );
  const visibleSelectedCount = filteredRecords.filter((record) => selectedRecordIds.has(record.id)).length;
  const allVisibleSelected = filteredRecords.length > 0 && visibleSelectedCount === filteredRecords.length;
  const attentionCount = auditRecords.filter((record) => auditRecordNeedsAttention(record)).length;

  useEffect(() => {
    const knownIds = new Set(auditRecords.map((record) => record.id));
    setSelectedRecordIds((current) => {
      const next = new Set([...current].filter((recordId) => knownIds.has(recordId)));
      return next.size === current.size ? current : next;
    });
  }, [auditRecords]);

  const handleFilterSave = () => {
    const name = filterName.trim() || auditFilterLabel(filters, t);
    const existingId = selectedSavedFilterId && savedFilters.some((saved) => saved.id === selectedSavedFilterId)
      ? selectedSavedFilterId
      : "";
    const savedFilter: SavedAuditFilter = {
      createdAt: new Date().toISOString(),
      filters,
      id: existingId || `audit-filter-${Date.now()}`,
      name
    };
    const nextSavedFilters = existingId
      ? savedFilters.map((saved) => (saved.id === existingId ? savedFilter : saved))
      : [...savedFilters, savedFilter];
    setSavedFilters(nextSavedFilters);
    setSelectedSavedFilterId(savedFilter.id);
    setFilterName(name);
    persistSavedAuditFilters(nextSavedFilters);
  };

  const handleSavedFilterLoad = (filterId: string) => {
    setSelectedSavedFilterId(filterId);
    const saved = savedFilters.find((item) => item.id === filterId);
    if (!saved) {
      return;
    }
    setFilters(saved.filters);
    setFilterName(saved.name);
  };

  const handleSavedFilterDelete = () => {
    if (!selectedSavedFilterId) {
      return;
    }
    const nextSavedFilters = savedFilters.filter((saved) => saved.id !== selectedSavedFilterId);
    setSavedFilters(nextSavedFilters);
    setSelectedSavedFilterId("");
    setFilterName("");
    persistSavedAuditFilters(nextSavedFilters);
  };

  const handleVisibleSelectionToggle = () => {
    const visibleIds = filteredRecords.map((record) => record.id);
    setSelectedRecordIds((current) => {
      const next = new Set(current);
      if (allVisibleSelected) {
        visibleIds.forEach((recordId) => next.delete(recordId));
      } else {
        visibleIds.forEach((recordId) => next.add(recordId));
      }
      return next;
    });
  };

  const handleRecordSelectionToggle = (recordId: string) => {
    setSelectedRecordIds((current) => {
      const next = new Set(current);
      if (next.has(recordId)) {
        next.delete(recordId);
      } else {
        next.add(recordId);
      }
      return next;
    });
  };

  return (
    <div className="audit-sections">
      <section className="audit-section" aria-label={t("app.aria.auditFilters")}>
        <div className="audit-toolbar">
          <div className="audit-filter-icon">
            <Filter size={18} aria-hidden="true" />
          </div>
          <label htmlFor="audit-search">
            <span>{t("audit.search")}</span>
            <input
              id="audit-search"
              name="audit-search"
              value={filters.query}
              onChange={(event) => setFilters((current) => ({ ...current, query: event.currentTarget.value }))}
              placeholder={t("audit.searchPlaceholder")}
            />
          </label>
          <label htmlFor="audit-category">
            <span>{t("audit.recordType")}</span>
            <select
              id="audit-category"
              name="audit-category"
              value={filters.category}
              onChange={(event) =>
                setFilters((current) => ({ ...current, category: event.currentTarget.value as AuditCategory }))
              }
            >
              <option value="all">{t("audit.allRecords")}</option>
              <option value="inference">{t("audit.inference")}</option>
              <option value="review">{t("audit.review")}</option>
              <option value="tool">{t("audit.toolCalls")}</option>
            </select>
          </label>
          <label htmlFor="audit-status">
            <span>{t("audit.status")}</span>
            <select
              id="audit-status"
              name="audit-status"
              value={filters.status}
              onChange={(event) =>
                setFilters((current) => ({ ...current, status: event.currentTarget.value as AuditStatusFilter }))
              }
            >
              <option value="all">{t("audit.allStatuses")}</option>
              <option value="attention">{t("audit.needsAttention")}</option>
              <option value="pass">{t("audit.passing")}</option>
              <option value="fail">{t("audit.failing")}</option>
            </select>
          </label>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!currentJob || filteredRecords.length === 0}
            onClick={() =>
              currentJob
                ? downloadJsonFile(`ai-jsunpack-${currentJob.id}-audit-filtered.json`, {
                    exportedAt: new Date().toISOString(),
                    jobId: currentJob.id,
                    filters,
                    artifactCount: artifacts.length,
                    records: filteredRecords
                  })
                : undefined
            }
          >
            <Download size={16} aria-hidden="true" />
            {t("action.exportView")}
          </button>
        </div>
        <div className="audit-saved-toolbar" aria-label={t("app.aria.savedFilters")}>
          <label htmlFor="audit-saved-filter">
            <span>{t("audit.savedFilter")}</span>
            <select
              id="audit-saved-filter"
              name="audit-saved-filter"
              value={selectedSavedFilterId}
              onChange={(event) => handleSavedFilterLoad(event.currentTarget.value)}
            >
              <option value="">{t("audit.manualFilters")}</option>
              {savedFilters.map((saved) => (
                <option key={saved.id} value={saved.id}>
                  {saved.name}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="audit-filter-name">
            <span>{t("audit.filterName")}</span>
            <input
              id="audit-filter-name"
              name="audit-filter-name"
              value={filterName}
              onChange={(event) => setFilterName(event.currentTarget.value)}
              placeholder={auditFilterLabel(filters, t)}
            />
          </label>
          <button className="secondary-action compact" type="button" onClick={handleFilterSave}>
            <Save size={16} aria-hidden="true" />
            {t("action.saveFilter")}
          </button>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!selectedSavedFilterId}
            onClick={handleSavedFilterDelete}
          >
            <Trash2 size={16} aria-hidden="true" />
            {t("action.delete")}
          </button>
        </div>
        <div className="audit-bulk-toolbar" aria-label={t("app.aria.bulkAudit")}>
          <button className="secondary-action compact" type="button" disabled={filteredRecords.length === 0} onClick={handleVisibleSelectionToggle}>
            <Filter size={16} aria-hidden="true" />
            {allVisibleSelected ? t("action.clearVisible") : t("action.selectVisible")}
          </button>
          <button className="secondary-action compact" type="button" disabled={selectedRecordIds.size === 0} onClick={() => setSelectedRecordIds(new Set())}>
            <XCircle size={16} aria-hidden="true" />
            {t("action.clearSelection")}
          </button>
          <button
            className="secondary-action compact"
            type="button"
            disabled={!currentJob || selectedRecords.length === 0}
            onClick={() =>
              currentJob
                ? downloadJsonFile(`ai-jsunpack-${currentJob.id}-audit-selected.json`, {
                    exportedAt: new Date().toISOString(),
                    jobId: currentJob.id,
                    filters,
                    selectedCount: selectedRecords.length,
                    records: selectedRecords
                  })
                : undefined
            }
          >
            <Download size={16} aria-hidden="true" />
            {t("action.exportSelected")}
          </button>
          <span>
            {visibleSelectedCount} {t("audit.visibleSelected")} / {selectedRecords.length} {t("audit.totalSelected")}
          </span>
        </div>
        <div className="audit-summary-grid">
          <div>
            <span>{t("audit.total")}</span>
            <strong>{auditRecords.length}</strong>
          </div>
          <div>
            <span>{t("audit.attention")}</span>
            <strong>{attentionCount}</strong>
          </div>
          <div>
            <span>{t("audit.visible")}</span>
            <strong>{filteredRecords.length}</strong>
          </div>
          <div>
            <span>{t("audit.selected")}</span>
            <strong>{selectedRecords.length}</strong>
          </div>
        </div>
      </section>

      <section className="audit-section" aria-label={t("app.aria.filteredAudit")}>
        <div className="section-heading">
          <h3>{t("audit.records")}</h3>
          <span>{filteredRecords.length}</span>
        </div>
        {filteredRecords.length > 0 ? (
          <div className="table-shell">
            <table className="data-table audit-ledger-table">
              <thead>
                <tr>
                  <th>
                    <input
                      aria-label={allVisibleSelected ? t("audit.clearAll") : t("audit.selectAll")}
                      checked={allVisibleSelected}
                      disabled={filteredRecords.length === 0}
                      onChange={handleVisibleSelectionToggle}
                      type="checkbox"
                    />
                  </th>
                  <th>{t("audit.table.type")}</th>
                  <th>{t("audit.table.subject")}</th>
                  <th>{t("audit.table.status")}</th>
                  <th>{t("audit.table.evidence")}</th>
                  <th>{t("audit.table.artifacts")}</th>
                </tr>
              </thead>
              <tbody>
                {groupedRecords.map((group) =>
                  group.records.length > 0 ? (
                    <AuditRiskGroupRows
                      group={group}
                      key={group.id}
                      onArtifactSelect={onArtifactSelect}
                      onRecordSelectionToggle={handleRecordSelectionToggle}
                      selectedRecordIds={selectedRecordIds}
                    />
                  ) : null
                )}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title={t("empty.noMatchingAudit.title")} detail={t("empty.noMatchingAudit.detail")} />
        )}
      </section>
    </div>
  );
}

export function AuditRiskGroupRows({
  group,
  onArtifactSelect,
  onRecordSelectionToggle,
  selectedRecordIds
}: {
  group: AuditRiskGroup;
  onArtifactSelect: (artifactId: string) => void;
  onRecordSelectionToggle: (recordId: string) => void;
  selectedRecordIds: Set<string>;
}) {
  const { t } = useLocalization();
  return (
    <>
      <tr className={`audit-group-row audit-group-${group.id}`}>
        <td colSpan={6}>
          <strong>{group.title}</strong>
          <span>
            {group.records.length} {group.records.length === 1 ? t("audit.record") : t("audit.recordsPlural")} / {group.detail}
          </span>
        </td>
      </tr>
      {group.records.map((record) => (
        <tr className={selectedRecordIds.has(record.id) ? "audit-selected-row" : undefined} key={record.id}>
          <td>
            <input
              aria-label={`${t("audit.selectRecord")} ${record.label}`}
              checked={selectedRecordIds.has(record.id)}
              onChange={() => onRecordSelectionToggle(record.id)}
              type="checkbox"
            />
          </td>
          <td>
            <strong>{record.category}</strong>
            <span>{record.label}</span>
          </td>
          <td>
            <strong>{record.secondary}</strong>
            <span>{record.detail}</span>
          </td>
          <td>
            <StatusToken status={record.status} />
            {record.failureClass !== "none" ? <small>{record.failureClass}</small> : null}
          </td>
          <td>
            <EvidenceRefButtons refs={record.evidenceRefs} onArtifactSelect={onArtifactSelect} />
          </td>
          <td>{record.artifactIds.length > 0 ? formatIdList(record.artifactIds, t) : t("common.none")}</td>
        </tr>
      ))}
    </>
  );
}

