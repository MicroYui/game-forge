import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { ArtifactDetail } from "../../components/artifacts";
import type { CursorPaginationState } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  artifactDetailApi,
  type ArtifactDetailApi,
  type ArtifactDetailSnapshot,
  type LineagePage,
} from "./api";
import { artifactDetailQueryOptions } from "./queries";

interface LineageState {
  error?: Error;
  expiresAt: string;
  generation: number;
  items: LineagePage["items"];
  loading: boolean;
  nextCursor: string | null;
  ownerArtifactId: string;
  pageSchemaVersion: LineagePage["page_schema_version"];
  readSnapshotId: string;
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("血缘页读取失败。");
}

function lineageFromPage(page: LineagePage, ownerArtifactId: string, generation: number): LineageState {
  return {
    expiresAt: page.expires_at,
    generation,
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    ownerArtifactId,
    pageSchemaVersion: page.page_schema_version,
    readSnapshotId: page.read_snapshot_id,
  };
}

function pageFromLineage(state: LineageState): LineagePage {
  return {
    expires_at: state.expiresAt,
    items: state.items,
    next_cursor: state.nextCursor,
    page_schema_version: state.pageSchemaVersion,
    read_snapshot_id: state.readSnapshotId,
  };
}

function paginationState(state: LineageState): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

export function ArtifactDetailPage({
  api = artifactDetailApi,
  artifactId,
  routeMode = "detail",
}: {
  api?: ArtifactDetailApi;
  artifactId: string;
  routeMode?: "detail" | "lineage";
}) {
  const detail = useQuery(artifactDetailQueryOptions(artifactId, api));
  const [lineage, setLineage] = useState<LineageState | null>(null);
  const activeArtifactIdRef = useRef(artifactId);
  activeArtifactIdRef.current = artifactId;
  const detailIdentityRef = useRef<ArtifactDetailSnapshot>();
  const detailGenerationRef = useRef(0);
  if (detail.data && detailIdentityRef.current !== detail.data) {
    detailIdentityRef.current = detail.data;
    detailGenerationRef.current += 1;
  }
  const detailGeneration = detailGenerationRef.current;

  useEffect(() => {
    if (!detail.data) return;
    setLineage(lineageFromPage(detail.data.lineagePage, artifactId, detailGeneration));
  }, [artifactId, detail.data, detailGeneration]);

  async function readLineagePage(cursor: string | null, restart: boolean) {
    const current = lineage;
    if (!current || current.ownerArtifactId !== artifactId) return;
    const ownerArtifactId = current.ownerArtifactId;
    const generation = current.generation;
    const readSnapshotId = current.readSnapshotId;
    const matchesRequest = (candidate: LineageState | null): candidate is LineageState =>
      candidate !== null &&
      candidate.ownerArtifactId === ownerArtifactId &&
      candidate.generation === generation &&
      candidate.readSnapshotId === readSnapshotId;

    setLineage((latest) =>
      matchesRequest(latest) ? { ...latest, error: undefined, loading: true } : latest,
    );
    try {
      const nextPage = await api.loadLineagePage(ownerArtifactId, cursor);
      if (activeArtifactIdRef.current !== ownerArtifactId) return;
      setLineage((latest) => {
        if (!matchesRequest(latest)) return latest;
        if (!restart && nextPage.read_snapshot_id !== latest.readSnapshotId) {
          return {
            ...latest,
            error: new Error("血缘分页快照发生变化，请重新查询。"),
            loading: false,
          };
        }
        return {
          ...lineageFromPage(nextPage, ownerArtifactId, generation),
          items: restart ? nextPage.items : [...latest.items, ...nextPage.items],
        };
      });
    } catch (error) {
      if (activeArtifactIdRef.current !== ownerArtifactId) return;
      setLineage((latest) =>
        matchesRequest(latest) ? { ...latest, error: normalizedError(error), loading: false } : latest,
      );
    }
  }

  if (detail.isPending) {
    return (
      <div className="gf-page">
        <StatePanel
          description="正在读取安全工件摘要与第一页血缘。"
          headingLevel={1}
          state="loading"
          title="正在读取工件详情"
        />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="gf-page">
        <header className="gf-page-header">
          <h1>工件详情</h1>
        </header>
        {detail.error instanceof ApiProblemError ? (
          <ProblemPanel problem={detail.error.problem} />
        ) : (
          <StatePanel
            action={
              <button onClick={() => void detail.refetch()} type="button">
                重试
              </button>
            }
            description="工件详情读取失败，请稍后重试。"
            state="error"
            title="无法读取工件"
          />
        )}
      </div>
    );
  }

  return (
    <div className="gf-page">
      <nav aria-label="工件详情视图" className="gf-cluster">
        {routeMode === "detail" ? (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}/lineage`}>打开独立血缘视图</a>
        ) : (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}`}>返回工件详情</a>
        )}
      </nav>
      <ArtifactDetail
        artifact={detail.data.artifact}
        lineagePage={lineage ? pageFromLineage(lineage) : detail.data.lineagePage}
        lineagePaginationState={lineage ? paginationState(lineage) : "ready"}
        onLoadMoreLineage={(cursor) => void readLineagePage(cursor, false)}
        onRestartLineage={() => void readLineagePage(null, true)}
      />
    </div>
  );
}
